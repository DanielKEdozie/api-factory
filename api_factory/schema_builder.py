"""SchemaBuilder integration for api_factory."""
import logging
from typing import Any, Optional

try:
    from marshmallow_sqlalchemy import SQLAlchemyAutoSchema
except ImportError:
    SQLAlchemyAutoSchema = None


_LOGGER = logging.getLogger(__name__)

_DEFAULT_FACTORY = None


def set_default_factory(factory: Any) -> None:
    """Remember the bound ``ApiFactory`` so schemas can resolve ``ma``/``db``.

    Schemas are usually declared while the application is being created, which
    happens outside an application context, so ``current_app.extensions`` is not
    reachable at that point.
    """
    global _DEFAULT_FACTORY
    _DEFAULT_FACTORY = factory


def _active_factory() -> Any:
    """Return the ``ApiFactory`` bound to the current app, if any."""
    try:
        from flask import current_app, has_app_context
    except Exception:
        return _DEFAULT_FACTORY
    if has_app_context():
        factory = current_app.extensions.get("api_factory")
        if factory is not None:
            return factory
    return _DEFAULT_FACTORY


def _prune_phantom_only_fields(schema_cls, seen=None):
    """Drop ``Meta.fields`` entries a schema never declares.

    ``flask_api_builder`` copies a relationship's ``only`` tuple down into
    every nested level, including the deepest one where nesting stops because
    the depth budget ran out. Marshmallow treats ``Meta.fields`` as the source
    of truth and raises ``KeyError`` for a name that is listed but absent from
    ``_declared_fields``, which breaks instantiation of the whole tree.
    """
    if seen is None:
        seen = set()
    if schema_cls in seen:
        return
    seen.add(schema_cls)

    declared = getattr(schema_cls, "_declared_fields", None)
    if not declared:
        return

    for container in (getattr(schema_cls, "Meta", None), getattr(schema_cls, "opts", None)):
        listed = getattr(container, "fields", None)
        if listed:
            pruned = tuple(name for name in listed if name in declared)
            if pruned != tuple(listed):
                container.fields = pruned

    for field in declared.values():
        _prune_nested(field, seen)


def _prune_nested(field, seen):
    """Descend into a field's nested schema class, if it has one."""
    nested = getattr(field, "nested", None)
    if nested is None:
        inner_field = getattr(field, "inner", None)
        if inner_field is not None:
            _prune_nested(inner_field, seen)
        return
    inner = nested if isinstance(nested, type) else getattr(nested, "schema", None)
    if not (isinstance(inner, type) and getattr(inner, "_declared_fields", None)):
        return
    declared = inner._declared_fields
    only = getattr(field, "only", None)
    if only:
        pruned = tuple(name for name in only if name in declared)
        if pruned != tuple(only):
            field.only = pruned
    _prune_phantom_only_fields(inner, seen)


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
        sqla_session: Optional[Any] = None,
        **kwargs
    ):
        self.model = model
        self.schema = None

        factory = _active_factory()

        if ma is None and factory is not None:
            ma = getattr(factory, "ma", None)
        if ma is None:
            try:
                from app.extensions import ma as _app_ma
                ma = _app_ma
            except Exception:
                pass

        # flask_api_builder raises when it cannot resolve a session, which used
        # to push every schema onto the degraded marshmallow_sqlalchemy fallback
        # below and silently discard `relationships`/`rel_fields`.
        if sqla_session is None and factory is not None:
            db = getattr(factory, "db", None)
            if db is not None:
                sqla_session = getattr(db, "session", None)
        if sqla_session is None:
            try:
                from app.extensions import db as _app_db
                sqla_session = _app_db.session
            except Exception:
                pass

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
                sqla_session=sqla_session,
                **kwargs
            )
            self.schema = builder.schema
            _prune_phantom_only_fields(self.schema)
            return
        except Exception:
            _LOGGER.exception(
                "flask_api_builder SchemaBuilder failed for %s; falling back to "
                "marshmallow_sqlalchemy (relationships/rel_fields are ignored by "
                "the fallback).", getattr(model, "__name__", model)
            )

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

