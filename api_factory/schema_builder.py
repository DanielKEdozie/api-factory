"""SchemaBuilder integration for api_factory."""
from typing import Any, Optional

try:
    from marshmallow_sqlalchemy import SQLAlchemyAutoSchema
except ImportError:
    SQLAlchemyAutoSchema = None


class SchemaBuilder:
    """Dynamic schema generator with automatic relationship support."""

    def __init__(
        self,
        model: Any,
        auto_relationships: bool = True,
        depth: int = 1,
        exclude: Optional[list] = None,
        dump_only: Optional[list] = None,
        ma: Optional[Any] = None,
        **kwargs
    ):
        self.model = model
        self.schema = None

        # 1. Try flask_api_builder's SchemaBuilder if ma is available or configured
        try:
            from flask_api_builder.schema_builder import SchemaBuilder as _BaseSchemaBuilder
            builder = _BaseSchemaBuilder(
                model,
                auto_relationships=auto_relationships,
                depth=depth,
                exclude=exclude,
                dump_only=dump_only,
                ma=ma,
                **kwargs
            )
            self.schema = builder.schema
            return
        except Exception:
            pass

        # 2. Standalone marshmallow_sqlalchemy fallback
        if SQLAlchemyAutoSchema is not None:
            meta_attrs = {
                "model": model,
                "include_relationships": auto_relationships,
                "include_fk": True,
                "load_instance": True,
            }
            if exclude:
                meta_attrs["exclude"] = tuple(exclude)
            if dump_only:
                meta_attrs["dump_only"] = tuple(dump_only)

            Meta = type("Meta", (), meta_attrs)
            self.schema = type(
                f"{model.__name__}Schema",
                (SQLAlchemyAutoSchema,),
                {"Meta": Meta}
            )
        else:
            raise RuntimeError(
                "Neither flask_api_builder nor marshmallow_sqlalchemy is available to build a schema."
            )

    def __call__(self, *args, **kwargs):
        if self.schema is not None:
            return self.schema(*args, **kwargs)
        raise RuntimeError(f"No schema created for model {self.model}")

