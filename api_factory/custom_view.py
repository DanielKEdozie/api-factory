import inspect
from functools import wraps
from typing import Any, Callable, List, Optional
from flask import current_app, g, jsonify, make_response, request, Response
from flask.views import View

try:
    from marshmallow import ValidationError
except ImportError:
    class ValidationError(Exception):
        def __init__(self, messages):
            self.messages = messages
            super().__init__(str(messages))

from .context import ContextValues


class ResourceContext:
    """Request-scoped execution context providing access to resource configuration,
    database session, validated data, and resolved parent records.
    """

    def __init__(
        self,
        builder: Any = None,
        item: Optional[Any] = None,
        data: Optional[Any] = None,
        value: Optional[ContextValues] = None,
        schema: Optional[Any] = None,
        session: Optional[Any] = None,
    ):
        self.builder = builder
        self.item = item
        self.data = data
        self.value = value if value is not None else ContextValues()
        self.values = self.value
        self.schema = schema or getattr(builder, "schema", None)
        self.model = getattr(builder, "model", None)
        self.session = session or getattr(builder, "session", None)
        self.db = getattr(builder, "db", None) or getattr(builder, "_db", None)
        self.endpoint_name = getattr(builder, "endpoint_name", None)
        self.route_prefix = getattr(builder, "route_prefix", None)

    @property
    def parent_record(self) -> Optional[Any]:
        return self.value.get("parent_record") if self.value else None

    @property
    def parent(self) -> Optional[Any]:
        return self.parent_record

    def __getitem__(self, item):
        if self.value and item in self.value:
            return self.value[item]
        raise KeyError(item)

    def __getattr__(self, name):
        # 1. Check value container first (e.g. ctx.equipment)
        if "value" in self.__dict__ and self.value and name in self.value:
            return self.value[name]
        # 2. Delegate to underlying builder
        if "builder" in self.__dict__ and self.builder is not None:
            return getattr(self.builder, name)
        raise AttributeError(f"'ResourceContext' object has no attribute '{name}'")


def create_resource_view(
    target: Any,
    rule: str,
    view_or_fn: Optional[Any] = None,
    fn: Optional[Callable] = None,
    methods: Optional[List[str]] = None,
    schema: Optional[Any] = None,
    default_schema: Optional[Any] = None,
    schema_validation: Optional[bool] = None,
    validate: Optional[bool] = None,
    detail: bool = False,
    model: Optional[Any] = None,
    pk_name: str = "id",
    db_session: Optional[Any] = None,
    builder: Optional[Any] = None,
    ctx: Optional[Any] = None,
    decorators: Optional[List[Callable]] = None,
    endpoint: Optional[str] = None,
    envelope: Optional[str] = None,
    **options
):
    """Registers a custom resource view (function or Class-Based View) on a Flask App or Blueprint.

    Supports:
      - Automatic view name resolution: pass view=OrderView or view=OrderView.as_view('orders')
      - Injected ResourceContext ('ctx' / 'builder') providing:
          * ctx.model: SQLAlchemy model
          * ctx.schema: Marshmallow schema
          * ctx.session: SQLAlchemy session
          * ctx.value / ctx.values: ContextValues container
          * ctx.parent_record / ctx.parent: resolved parent record
          * ctx.item: resolved model instance (when detail=True)
          * ctx.data: validated request payload
      - Signature-based parameter injection:
          * 'data': validated request JSON (or raw JSON if validation skipped)
          * 'schema': instantiated Marshmallow Schema
          * 'item': resolved model instance (when detail=True)
          * 'ctx' / 'builder' / 'resource': ResourceContext instance
          * 'value' / 'values': ContextValues container
    """
    if validate is not None and schema_validation is None:
        schema_validation = validate

    actual_view = view_or_fn or fn
    context_obj = builder or ctx

    def register_endpoint(handler_callable: Any):
        # Determine if handler is a Class-Based View class or already an as_view wrapper
        is_cbv_class = isinstance(handler_callable, type) and issubclass(handler_callable, View)
        has_view_class = hasattr(handler_callable, "view_class")

        ep = endpoint or getattr(handler_callable, "__name__", None)
        if not ep or ep == "<lambda>":
            if hasattr(handler_callable, "__name__"):
                ep = f"{handler_callable.__name__.lower()}_{rule.strip('/').replace('/', '_')}"
            else:
                ep = f"custom_{rule.strip('/').replace('/', '_')}"

        # Instantiate schema if provided (or default_schema)
        active_schema = schema if schema is not None else default_schema
        schema_instance = None
        if active_schema is not None:
            schema_instance = active_schema() if isinstance(active_schema, type) else active_schema

        target_model = model or getattr(context_obj, "model", None)

        if is_cbv_class:
            # Auto-resolve view name: attach context to class and call .as_view(ep)
            setattr(handler_callable, "schema", schema_instance)
            setattr(handler_callable, "ctx", context_obj)
            setattr(handler_callable, "builder", context_obj)
            setattr(handler_callable, "model", target_model)
            view_func = handler_callable.as_view(ep)
        elif has_view_class:
            # Already called .as_view(...): attach context to underlying view_class
            view_cls = handler_callable.view_class
            setattr(view_cls, "schema", schema_instance)
            setattr(view_cls, "ctx", context_obj)
            setattr(view_cls, "builder", context_obj)
            setattr(view_cls, "model", target_model)
            view_func = handler_callable
        else:
            view_func = handler_callable

        # Wrap handler with schema validation, injection, and response formatting
        @wraps(view_func)
        def wrapper(*view_args, **view_kwargs):
            req_method = request.method.upper()

            # Determine whether to validate: only auto-validate if schema was explicitly provided
            should_val = schema_validation
            if should_val is None:
                should_val = (schema is not None) and (req_method in ("POST", "PUT", "PATCH"))

            validated_data = None
            if should_val and schema_instance is not None:
                payload = request.get_json(silent=True) or {}
                try:
                    validated_data = schema_instance.load(payload)
                except ValidationError as err:
                    return jsonify({"error": "Validation Error", "messages": err.messages}), 400
            else:
                validated_data = request.get_json(silent=True) or {}

            # If detail=True and model is known, resolve item
            item = None
            active_model = target_model
            active_session = db_session or getattr(context_obj, "session", None)
            if active_session is None and hasattr(current_app, "extensions") and "sqlalchemy" in current_app.extensions:
                active_session = current_app.extensions["sqlalchemy"].session

            if detail and active_model is not None:
                pk_val = (
                    view_kwargs.get(pk_name)
                    or (view_kwargs.get(f"{active_model.__name__.lower()}_{pk_name}") if active_model else None)
                    or (view_kwargs.get(f"{active_model.__name__.lower()}_id") if active_model else None)
                    or view_kwargs.get("id")
                    or view_kwargs.get("pk")
                )
                if active_session is not None and pk_val is not None:
                    lookup = int(pk_val) if isinstance(pk_val, str) and pk_val.isdigit() else pk_val
                    item = active_session.get(active_model, lookup)

                    if item is None:
                        return jsonify({"error": "Not Found", "message": f"{active_model.__name__} {pk_val} not found"}), 404

            # Resolve parent records into ContextValues
            val_ctx = None
            if hasattr(context_obj, "_validate_view_args"):
                val_ctx = context_obj._validate_view_args(view_kwargs)
                val_ctx.item = item
                val_ctx.data = validated_data
            else:
                val_ctx = ContextValues(item=item, data=validated_data)

            # Create request-scoped ResourceContext
            req_ctx = ResourceContext(
                builder=context_obj,
                item=item,
                data=validated_data,
                value=val_ctx,
                schema=schema_instance,
                session=active_session,
            )
            g.ctx = req_ctx

            if is_cbv_class or has_view_class:
                view_cls = handler_callable if is_cbv_class else handler_callable.view_class
                setattr(view_cls, "ctx", req_ctx)
                setattr(view_cls, "builder", req_ctx)

            # Inspect callable parameters to inject what was requested
            if not is_cbv_class and not has_view_class and callable(view_func):
                call_kwargs = _build_injected_kwargs(
                    view_func, view_kwargs, validated_data, schema_instance,
                    item, active_model, req_ctx, val_ctx
                )
                res = view_func(*view_args, **call_kwargs)
            else:
                res = view_func(*view_args, **view_kwargs)

            # Format response
            return _format_response(res, schema_instance, envelope)

        # Apply any explicit decorators
        final_func = wrapper
        if decorators:
            for dec in reversed(decorators):
                final_func = dec(final_func)

        req_methods = methods or (["GET"] if not (is_cbv_class or has_view_class) else None)
        target.add_url_rule(rule, endpoint=ep, view_func=final_func, methods=req_methods, **options)
        return handler_callable

    if actual_view is not None:
        return register_endpoint(actual_view)

    # Used as a decorator: @resource_view(...)
    return register_endpoint


def _build_injected_kwargs(
    view_func: Callable,
    view_kwargs: dict,
    validated_data: Any,
    schema_instance: Optional[Any],
    item: Optional[Any],
    active_model: Optional[Any],
    req_ctx: "ResourceContext",
    val_ctx: ContextValues,
) -> dict:
    """Build the keyword arguments a custom view explicitly asked for by name."""
    sig = inspect.signature(view_func)
    params = sig.parameters
    has_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    call_kwargs = dict(view_kwargs)
    if "data" in params or has_var_kwargs:
        call_kwargs["data"] = validated_data
    if "schema" in params or has_var_kwargs:
        call_kwargs["schema"] = schema_instance
    if "item" in params or has_var_kwargs:
        call_kwargs["item"] = item
    if active_model is not None and active_model.__name__.lower() in params:
        call_kwargs[active_model.__name__.lower()] = item
    if "ctx" in params or has_var_kwargs:
        call_kwargs["ctx"] = req_ctx
    if "builder" in params:
        call_kwargs["builder"] = req_ctx
    if "resource" in params:
        call_kwargs["resource"] = req_ctx
    if "value" in params:
        call_kwargs["value"] = val_ctx
    if "values" in params:
        call_kwargs["values"] = val_ctx
    if "parent" in params:
        call_kwargs["parent"] = req_ctx.parent_record
    if "parent_record" in params:
        call_kwargs["parent_record"] = req_ctx.parent_record
    for k, v in val_ctx.items():
        if k in params:
            call_kwargs[k] = v

    if not has_var_kwargs:
        call_kwargs = {k: v for k, v in call_kwargs.items() if k in params}
    return call_kwargs


def _format_response(res: Any, schema_instance: Optional[Any], envelope: Optional[str] = None) -> Any:
    """Helper to standardize response formatting."""
    if isinstance(res, Response):
        return res

    status_code = None
    headers = None
    body = res

    if isinstance(res, tuple):
        if len(res) == 2:
            body, status_code = res
        elif len(res) == 3:
            body, status_code, headers = res

    # Serialization if body is not already JSON or Response
    formatted_body = body
    if schema_instance is not None and not isinstance(body, (dict, list, str, bytes, type(None))):
        try:
            formatted_body = schema_instance.dump(body)
        except Exception:
            pass

    if envelope and isinstance(formatted_body, (dict, list)):
        formatted_body = {envelope: formatted_body}

    if isinstance(formatted_body, (dict, list)):
        response = jsonify(formatted_body)
    elif isinstance(formatted_body, str):
        response = make_response(formatted_body)
    elif formatted_body is None:
        response = make_response("", status_code or 204)
    else:
        response = make_response(str(formatted_body))

    if status_code is not None:
        response.status_code = status_code
    if headers is not None:
        response.headers.extend(headers)

    return response
