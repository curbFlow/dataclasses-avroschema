import dataclasses
import enum
import inspect
import json
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

from dacite import Config, from_dict
from fastavro.validation import validate

from . import case
from .fields import EnumField, FieldType, RecordField, UnionField
from .schema_definition import AvroSchemaDefinition
from .serialization import deserialize, serialize, to_json
from .types import JsonDict
from .utils import SchemaMetadata, is_custom_type, is_pydantic_model

AVRO = "avro"
AVRO_JSON = "avro-json"


CT = TypeVar("CT", bound="AvroModel")


class AvroModel:
    schema_def: Optional[AvroSchemaDefinition] = None
    klass: Optional[Type] = None
    metadata: Optional[SchemaMetadata] = None
    user_defined_types: Tuple = ()
    root: Any = None
    rendered_schema: OrderedDict = dataclasses.field(default_factory=OrderedDict)

    @classmethod
    def generate_dataclass(cls: Type[CT]) -> Type[CT]:
        if dataclasses.is_dataclass(cls) or is_pydantic_model(cls):
            return cls
        return dataclasses.dataclass(cls)

    @classmethod
    def generate_metadata(cls: Any) -> SchemaMetadata:
        meta = getattr(cls.klass, "Meta", type)

        return SchemaMetadata.create(meta)

    @classmethod
    def generate_schema(cls: Type[CT], schema_type: str = "avro") -> Optional[OrderedDict]:
        if cls.schema_def is None:
            # Generate dataclass and metadata
            cls.klass = cls.generate_dataclass()

            # let's live open the possibility to define different
            # schema definitions like json
            if schema_type == "avro":
                # cache the schema
                cls.schema_def = cls._generate_avro_schema()
                cls.rendered_schema = cls.schema_def.render()
            else:
                raise ValueError("Invalid type. Expected avro schema type.")

        return cls.rendered_schema

    @classmethod
    def _generate_avro_schema(cls: Type[CT]) -> AvroSchemaDefinition:
        metadata = cls.generate_metadata()
        cls.metadata = metadata
        return AvroSchemaDefinition("record", cls.klass, metadata=metadata, parent=cls.root or cls)

    @classmethod
    def avro_schema(cls: Type[CT], case_type: Optional[str] = None) -> str:
        avro_schema = cls.generate_schema(schema_type=AVRO)

        # After generating the avro schema, reset the raw_fields to the init
        cls.user_defined_types = ()

        if case_type is not None:
            avro_schema = case.case_record(cls.rendered_schema, case_type)  # type: ignore

        return json.dumps(avro_schema)

    @classmethod
    def avro_schema_to_python(cls: Any, root: Any = None) -> Dict[str, Any]:
        if root is not None:
            cls.root = root

        return json.loads(cls.avro_schema())

    @classmethod
    def get_fields(cls: Type[CT]) -> List[FieldType]:
        if cls.schema_def is None:
            cls.generate_schema()
        return cls.schema_def.fields  # type: ignore

    @classmethod
    def _get_enum_type_map(cls: Type[CT]) -> Dict[str, enum.EnumMeta]:
        enum_types = {}
        for field_type in cls.get_fields():
            if isinstance(field_type, EnumField):
                enum_types[field_type.name] = field_type.type
            elif isinstance(field_type, UnionField):
                for sub_type in field_type.type.__args__:
                    if inspect.isclass(sub_type) and issubclass(sub_type, enum.Enum):
                        enum_types[field_type.name] = sub_type
            elif isinstance(field_type, RecordField):
                enum_types.update(field_type.type._get_enum_type_map())
        return enum_types

    @classmethod
    def _deserialize_complex_types(cls: Type[CT], payload: Dict[str, Any]) -> Dict:
        output = {}
        enum_type_map = cls._get_enum_type_map()
        for field, value in payload.items():
            if isinstance(value, dict):
                output[field] = cls._deserialize_complex_types(value)
            elif field in enum_type_map and isinstance(value, str):
                try:
                    enum_field = enum_type_map[field]
                    output[field] = enum_field(value)
                except ValueError as e:
                    raise ValueError(f"Value {value} is not a valid instance of {enum_type_map[field]}", e)
            else:
                output[field] = value
        return output

    @staticmethod
    def standardize_custom_type(value: Any) -> Any:
        if is_custom_type(value):
            return value["default"]
        elif isinstance(value, dict):
            return {k: AvroModel.standardize_custom_type(v) for k, v in value.items()}
        elif issubclass(type(value), enum.Enum):
            return value.value
        return value

    def asdict(self) -> JsonDict:
        data = dataclasses.asdict(self)

        # te standardize called can be replaced if we have a custom implementation of asdict
        # for now I think is better to use the native implementation
        return {key: self.standardize_custom_type(value) for key, value in data.items()}

    def serialize(self, serialization_type: str = AVRO) -> bytes:
        schema = self.avro_schema_to_python()

        return serialize(self.asdict(), schema, serialization_type=serialization_type)

    @classmethod
    def deserialize(
        cls: Type[CT],
        data: bytes,
        serialization_type: str = AVRO,
        create_instance: bool = True,
        writer_schema: Optional[Union[JsonDict, Type[CT]]] = None,
    ) -> Union[JsonDict, CT]:

        if inspect.isclass(writer_schema) and issubclass(writer_schema, AvroModel):
            # mypy does not undersdtand redefinitions
            writer_schema: JsonDict = writer_schema.avro_schema_to_python()  # type: ignore

        schema = cls.avro_schema_to_python()
        payload = deserialize(
            data, schema, serialization_type=serialization_type, writer_schema=writer_schema  # type: ignore
        )
        output = cls._deserialize_complex_types(payload)

        if create_instance:
            return cls.parse_obj(data=output)
        return output

    @classmethod
    def parse_obj(cls: Type[CT], data: Dict) -> Union[JsonDict, CT]:
        return from_dict(data_class=cls, data=data, config=Config(**cls.config()))

    def validate(self) -> bool:
        schema = self.avro_schema_to_python()
        return validate(self.asdict(), schema)

    def to_dict(self) -> JsonDict:
        # Serialize using the current AVRO schema to get proper field representations
        # and after that convert into python
        return self.asdict()

    def to_json(self) -> str:
        data = to_json(self.asdict())
        return json.dumps(data)

    @classmethod
    def config(cls: Type[CT]) -> JsonDict:
        """
        Get the default config for dacite and always include the self reference
        """
        # We need to make sure that the `avro schemas` has been generated, otherwise cls.klass is empty
        # It won't affect the performance because the rendered schema will be store in cls.rendered_schema
        if cls.klass is None:
            # Generate dataclass and metadata
            cls.klass = cls.generate_dataclass()

        return {
            "check_types": False,
            "forward_references": {
                cls.klass.__name__: cls.klass,
            },
        }

    @classmethod
    def fake(cls: Type[CT]) -> CT:
        payload = {field.name: field.fake() for field in cls.get_fields()}

        return from_dict(data_class=cls, data=payload, config=Config(**cls.config()))
