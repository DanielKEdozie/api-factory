from functools import wraps
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from flask import abort, current_app, g, has_app_context, request
from sqlalchemy import select
from .converters import build_pk_route_pattern, register_converters
from .rate_limiter import resolve_rate_limit_decorator
from .responses import json_response
from .schema_builder import SchemaBuilder
from .views import CollectionView, ItemView, SingletonView

log = logging.getLogger(__name__)


from .context import ContextValues


class ApiProvider:
    """Orchestrates MethodView registration, querying, schemas, rate limits,

    and lifecycle hooks for a SQLAlchemy model.
    """

    def __init__(
        self,
        app_or_bp: Any,
        model: Any,
        schema: Any = None,
        db: Any = None,
        *,
        endpoint_name: Optional[str] = None,
        route_prefix: Optional[str] = None,
        url_prefix: Optional[str] = None,
        pk_name: str = "id",
        pk_type: str = "int_or_str",
        singleton: bool = False,
        paginate: bool = True,
        page_size: int = 20,
        per_page: Optional[int] = None,
        max_page_size: int = 100,
        filter_fields: Optional[List[str]] = None,
        filter_types: Optional[Dict[str, Callable]] = None,
        sort_fields: Optional[List[str]] = None,
        sort_field: Optional[str] = None,
        sort_order: Optional[str] = "asc",
        raw_sort: bool = False,
        search_fields: Optional[List[str]] = None,
        request_view_args: Optional[Dict[str, str]] = None,
        view_args: Optional[Dict[str, str]] = None,
        args_key: Optional[Union[str, Dict[str, str]]] = None,
        view_args_key: Optional[Union[str, Dict[str, str]]] = None,
        resolve_keys: Optional[Dict[str, Any]] = None,
        resolve_view_args: Optional[Dict[str, Any]] = None,
        resolve_value: Optional[Union[str, Dict[str, str]]] = None,
        resolve_name: Optional[Union[str, Dict[str, str]]] = None,
        resolve_as: Optional[Union[str, Dict[str, str]]] = None,
        view_args_config: Optional[Dict[str, Any]] = None,
        view_args_ref: Optional[Dict[str, Tuple[Any, str]]] = None,
        route_configs: Optional[Union[Dict[str, Any], List[Callable], Callable]] = None,
        decorators: Optional[Union[Dict[str, Any], List[Callable], Callable]] = None,
        rate_limits: Optional[Dict[str, Tuple[int, int]]] = None,
        eager_loads: Optional[Dict[str, List[Tuple[str, str]]]] = None,
        export_fields: Optional[List[str]] = None,
        soft_delete: bool = False,
        audit: bool = False,
        envelope: Optional[str] = None,
        query_overrides: Optional[Dict[str, Callable]] = None,
        query_override: Optional[Dict[str, Callable]] = None,
        overrides: Optional[Dict[str, Callable]] = None,
        methods: Optional[List[str]] = None,
        exclude_routes: Optional[List[str]] = None,
        include_routes: Optional[List[str]] = None,
        related_models: Optional[List[Tuple[Any, str]]] = None,
    ):
        self.app_or_bp = app_or_bp
        self.model = model
        if schema is None:
            sb = SchemaBuilder(model)
            self.schema = sb.schema() if hasattr(sb, "schema") and isinstance(sb.schema, type) else sb()
        else:
            self.schema = schema() if isinstance(schema, type) else schema
        self._db = db

        # Endpoint naming & route prefixing
        self.endpoint_name = endpoint_name or model.__tablename__
        prefix = route_prefix if route_prefix is not None else url_prefix
        self.route_prefix = prefix if prefix is not None else f"/{self.endpoint_name}"

        # Primary key routing configuration
        self.pk_name = pk_name
        self.pk_type = pk_type
        self.singleton = singleton

        # Pagination
        self.paginate = paginate
        self.page_size = per_page or page_size
        self.max_page_size = max_page_size

        # Query options
        self.filter_fields = list(filter_fields or [])
        self.filter_types = dict(filter_types or {})
        self.sort_fields = list(sort_fields or ([sort_field] if sort_field else []))
        self.sort_order = sort_order or "asc"
        self.raw_sort = raw_sort
        self.search_fields = list(search_fields or [])

        # Nested / scoped routes
        req_args = dict(request_view_args or view_args or {})
        raw_key = view_args_key or args_key
        if isinstance(raw_key, dict):
            req_args.update(raw_key)
        elif isinstance(raw_key, str):
            req_args[raw_key] = raw_key
        self.request_view_args = req_args

        resolvers = dict(resolve_keys or resolve_view_args or view_args_config or view_args_ref or {})
        self.resolve_keys = resolvers
        self.resolve_view_args = resolvers
        self.view_args_config = resolvers
        self.view_args_ref = resolvers
        self.resolve_value = resolve_value or resolve_name or resolve_as
        self.related_models = list(related_models or [])

        # Decorators and route_configs interoperability
        self.route_configs = self._normalize_decorators(route_configs, decorators)

        # Rate limiting and eager loading
        self.rate_limits = dict(rate_limits or {})
        self.eager_loads = dict(eager_loads or {})
        self.export_fields = list(export_fields or [])
        self.soft_delete = soft_delete
        self.audit = audit
        self.envelope = envelope

        # Query and action overrides
        self.query_overrides = dict(query_overrides or query_override or {})
        self.overrides = dict(overrides or {})

        # Allowed HTTP methods / Route inclusion & exclusion
        self.allowed_methods = {m.upper() for m in methods} if methods is not None else None
        self.exclude_routes = set(exclude_routes or [])
        self.include_routes = set(include_routes or [])

        self._register_routes()

    @property
    def db(self):
        if self._db is not None:
            return self._db
        if has_app_context():
            ext = current_app.extensions.get("api_factory")
            if ext and ext.db:
                return ext.db
            sqla = current_app.extensions.get("sqlalchemy")
            if sqla:
                return sqla
        return None

    @property
    def session(self):
        db_instance = self.db
        if db_instance is not None and hasattr(db_instance, "session"):
            return db_instance.session
        raise RuntimeError("No SQLAlchemy session found for ApiProvider. Pass db=db or initialize ApiFactory(app, db=db).")

    def _normalize_decorators(self, route_configs, decorators) -> Dict[str, List[Callable]]:
        """Normalizes route_configs and decorators aliases into a unified action -> list[Callable] map."""
        source = decorators if decorators is not None else route_configs
        if source is None:
            return {}

        if isinstance(source, (list, tuple)):
            decs = list(source)
            return {action: list(decs) for action in ("collection", "item", "create", "update", "patch", "delete", "get_singleton", "update_singleton", "delete_singleton")}
        elif callable(source):
            return {action: [source] for action in ("collection", "item", "create", "update", "patch", "delete", "get_singleton", "update_singleton", "delete_singleton")}
        elif isinstance(source, dict):
            normalized = {}
            for k, v in source.items():
                action_list = [v] if callable(v) else list(v or [])
                # map aliases
                if k in ("list", "get_all", "collection"):
                    normalized["collection"] = action_list
                    normalized["get_all"] = action_list
                elif k in ("retrieve", "get_one", "item"):
                    normalized["item"] = action_list
                    normalized["get_one"] = action_list
                else:
                    normalized[k] = action_list
            return normalized
        return {}

    def _apply_envelope(self):
        if self.envelope:
            g.envelope_key = self.envelope

    def _validate_view_args(self, kwargs: dict) -> ContextValues:
        """Validates that parent referenced records exist when view_args_config/view_args_ref/resolve_keys is provided.
        Populates ContextValues with resolved parent objects and aliases, storing in flask.g.values,
        flask.g.view_args_resolved, and flask.g.parent_record.
        """
        values = ContextValues()
        values.view_args = dict(kwargs)
        resolved_dict = getattr(g, "view_args_resolved", {})

        config = self.resolve_keys or self.resolve_view_args or self.view_args_config or self.view_args_ref
        if not config:
            g.values = values
            return values

        session = self.session

        for param, handler in config.items():
            if param in kwargs:
                val = kwargs[param]
                parent = None
                model_name = getattr(handler, "__name__", None) or param

                if callable(handler) and not isinstance(handler, type):
                    parent = handler(val)
                elif isinstance(handler, tuple) and len(handler) >= 2:
                    ref_model, ref_field = handler[0], handler[1]
                    model_name = getattr(ref_model, "__name__", model_name)
                    col = getattr(ref_model, ref_field)
                    stmt = select(ref_model).where(col == val)
                    parent = session.execute(stmt).scalar_one_or_none()
                elif isinstance(handler, type):  # Model class
                    ref_model = handler
                    model_name = getattr(ref_model, "__name__", model_name)
                    if hasattr(ref_model, "slug") and isinstance(val, str) and not val.isdigit():
                        stmt = select(ref_model).where(ref_model.slug == val)
                        parent = session.execute(stmt).scalar_one_or_none()
                    else:
                        parent = session.get(ref_model, val)

                if parent is None:
                    abort(404, description=f"Parent {model_name} ({param}={val}) not found.")

                resolved_dict[param] = parent
                values[param] = parent
                values["parent_record"] = parent

                # Lowercase class name alias (e.g. values.equipment)
                if hasattr(parent, "__class__"):
                    cls_alias = parent.__class__.__name__.lower()
                    if cls_alias not in values:
                        values[cls_alias] = parent

                # Custom alias from resolve_value / resolve_name / resolve_as
                if self.resolve_value:
                    if isinstance(self.resolve_value, str):
                        values[self.resolve_value] = parent
                    elif isinstance(self.resolve_value, dict):
                        alias = self.resolve_value.get(param)
                        if alias:
                            values[alias] = parent

                g.parent_record = parent

        g.view_args_resolved = resolved_dict
        g.values = values
        return values

    def _apply_query_override(self, stmt: Any, action: str, **kwargs: Any) -> Any:
        override_fn = self.query_overrides.get(action)
        if not override_fn:
            # check aliases
            if action in ("collection", "get_all"):
                override_fn = self.query_overrides.get("collection") or self.query_overrides.get("list") or self.query_overrides.get("get_all")
            elif action in ("item", "get_one"):
                override_fn = self.query_overrides.get("item") or self.query_overrides.get("retrieve") or self.query_overrides.get("get_one")

        if override_fn and callable(override_fn):
            # If function expects (stmt) or (query, route_values, request, builder)
            import inspect
            sig = inspect.signature(override_fn)
            params_count = len(sig.parameters)
            if params_count == 1:
                return override_fn(stmt)
            else:
                return override_fn(stmt, kwargs, request, self)
        return stmt

    def _apply_decorators_to_method(self, method_func: Callable, action_name: str) -> Callable:
        """Applies configured rate limits and decorators to a MethodView action."""
        fn = method_func

        action_aliases = {
            "collection": ["collection", "get_all", "list", "all", "index"],
            "get_all": ["collection", "get_all", "list", "all", "index"],
            "item": ["item", "get_one", "retrieve", "show"],
            "get_one": ["item", "get_one", "retrieve", "show"],
            "create": ["create", "post"],
            "update": ["update", "put"],
            "patch": ["patch"],
            "delete": ["delete", "destroy"],
        }
        keys = action_aliases.get(action_name, [action_name])

        # 1. Automatic Rate Limit
        rl_cfg = None
        for k in keys + ["*", "global"]:
            if k in self.rate_limits:
                rl_cfg = self.rate_limits[k]
                break

        if rl_cfg:
            rl_dec = resolve_rate_limit_decorator(rl_cfg)
            if rl_dec:
                fn = rl_dec(fn)

        # 2. Configured Decorators
        decs = []
        for k in keys:
            if k in self.route_configs:
                decs.extend(self.route_configs[k])
                break
        for dec in reversed(decs):
            fn = dec(fn)

        # 3. JSON response formatting wrapper
        status_map = {"create": 201, "delete": 204, "delete_singleton": 204}
        fn = json_response(default_status=status_map.get(action_name, 200), envelope=self.envelope)(fn)

        return fn

    def _register_routes(self):
        """Constructs and binds MethodViews to the Flask application or Blueprint."""
        register_converters(self.app_or_bp)

        if self.singleton:
            self._register_singleton_routes()
        else:
            self._register_collection_and_item_routes()

    def _is_method_allowed(self, http_method: str, action: Optional[str] = None) -> bool:
        http_method = http_method.upper()
        if self.allowed_methods is not None:
            return http_method in self.allowed_methods

        action_map = {
            "GET": ["collection", "item", "get_all", "get_one", "list", "retrieve"],
            "POST": ["create", "post"],
            "PUT": ["update", "put"],
            "PATCH": ["patch"],
            "DELETE": ["delete", "destroy"]
        }
        actions = [action] if action else action_map.get(http_method, [http_method.lower()])
        if self.include_routes:
            if not any(a in self.include_routes for a in actions):
                return False
        if self.exclude_routes:
            if any(a in self.exclude_routes for a in actions):
                return False
        return True

    def _register_collection_and_item_routes(self):
        # 1. Collection View
        collection_methods = []
        if self._is_method_allowed("GET", action="collection"):
            collection_methods.extend(["GET", "HEAD"])
        if self._is_method_allowed("POST", action="create"):
            collection_methods.append("POST")

        if collection_methods:
            self.app_or_bp.add_url_rule(
                self.route_prefix,
                view_func=CollectionView.as_view(f"{self.endpoint_name}_collection", self),
                methods=collection_methods
            )

        # 2. Item View
        item_methods = []
        if self._is_method_allowed("GET", action="item"):
            item_methods.extend(["GET", "HEAD"])
        if self._is_method_allowed("PUT", action="update"):
            item_methods.append("PUT")
        if self._is_method_allowed("PATCH", action="patch"):
            item_methods.append("PATCH")
        if self._is_method_allowed("DELETE", action="delete"):
            item_methods.append("DELETE")

        if item_methods:
            pk_pattern = build_pk_route_pattern(self.pk_name, self.pk_type)
            item_url = f"{self.route_prefix.rstrip('/')}{pk_pattern}"
            self.app_or_bp.add_url_rule(
                item_url,
                view_func=ItemView.as_view(f"{self.endpoint_name}_item", self),
                methods=item_methods
            )

    def _register_singleton_routes(self):
        all_methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
        methods = [m for m in all_methods if self._is_method_allowed(m)]
        if methods:
            self.app_or_bp.add_url_rule(
                self.route_prefix,
                view_func=SingletonView.as_view(f"{self.endpoint_name}_singleton", self),
                methods=methods
            )

    def _cleanup_relationships(self, parent_id: Any):
        """Deletes related child models before removing parent when related_models is supplied."""
        session = self.session
        for rel_model, fk_field in self.related_models:
            stmt = select(rel_model).where(getattr(rel_model, fk_field) == parent_id)
            children = session.execute(stmt).scalars().all()
            for child in children:
                if hasattr(child, "before_parent_delete"):
                    child.before_parent_delete()
                session.delete(child)

    # Lifecycle hooks (can be overridden on subclass or passed as functions)
    def before_create(self, data: dict) -> dict:
        return data

    def after_create(self, item: Any):
        pass

    def before_update(self, item: Any, data: dict) -> dict:
        return data

    def after_update(self, item: Any):
        pass

    def after_delete(self, item: Any):
        pass

    def _invoke_hook(self, hook_fn: Callable, *args: Any, data: Any = None, values: Any = None) -> Any:
        """Invokes a lifecycle hook function, inspecting its parameter signature to dynamically pass
        available arguments (such as 'item', 'data', 'value', 'values') while remaining 100%
        backward compatible with legacy positional signatures.
        """
        if not hook_fn or not callable(hook_fn):
            return args[0] if args else None

        if values is None:
            values = getattr(g, "values", None) or ContextValues()

        import inspect
        try:
            sig = inspect.signature(hook_fn)
            params = list(sig.parameters.values())
            param_names = [p.name for p in params]
            num_params = len(params)

            has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
            has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

            # 1. If explicit kwargs are accepted
            if has_varkw:
                call_kwargs = {}
                if data is not None and "data" not in [p.name for p in params[:len(args)]]:
                    call_kwargs["data"] = data
                call_kwargs["value"] = values
                call_kwargs["values"] = values
                return hook_fn(*args, **call_kwargs)

            # 2. If parameter names match named args: e.g. def hook(item, data, value):
            if num_params > len(args):
                if num_params == 2:
                    second_param = param_names[1]
                    if second_param in ("value", "values"):
                        return hook_fn(args[0], values)
                    elif second_param == "data" and data is not None:
                        return hook_fn(args[0], data)
                    elif len(args) >= 2:
                        return hook_fn(args[0], args[1])
                    else:
                        return hook_fn(args[0], values)
                elif num_params == 3:
                    item_arg = args[0] if len(args) > 0 else None
                    data_arg = args[1] if len(args) > 1 else data
                    return hook_fn(item_arg, data_arg, values)

            # 3. If accepts *args
            if has_varargs:
                return hook_fn(*args, values)

            # 4. Standard positional call matching num_params
            if num_params == len(args):
                return hook_fn(*args)
            elif num_params < len(args):
                return hook_fn(*args[:num_params])
            else:
                return hook_fn(*args)
        except Exception:
            return hook_fn(*args)

    def get_list_context(self, items: list, total: int, **kwargs) -> dict:
        return {}

    # Route Extensibility
    def route(self, rule: str = "", **options):
        """Decorator to register a custom route relative to this API's route_prefix."""
        def decorator(f):
            full_rule = self.route_prefix.rstrip("/") + ("/" + rule.lstrip("/") if rule else "")
            endpoint = options.pop("endpoint", f"{self.endpoint_name}_{f.__name__}")
            self.app_or_bp.add_url_rule(full_rule, endpoint=endpoint, view_func=f, **options)
            return f
        return decorator

    def add_route(self, rule: str, view_func: Callable, **options):
        """Register a custom route under this API's route_prefix."""
        return self.route(rule, **options)(view_func)

    def override_action(self, action: str):
        """Decorator to override a built-in CRUD action (list, get_one, create, update, delete)."""
        def decorator(f):
            self.overrides[action] = f
            return f
        return decorator

    def hook(self, hook_name: str):
        """Decorator to register lifecycle hooks (before_create, after_create, before_update, etc.)."""
        def decorator(f):
            setattr(self, hook_name, f)
            return f
        return decorator

    def sub_resource(
        self,
        model: Any,
        sub_path: str,
        args_key: Optional[Union[str, Dict[str, str]]] = None,
        view_args_key: Optional[Union[str, Dict[str, str]]] = None,
        foreign_key: Optional[str] = None,
        parent_arg: Optional[str] = None,
        parent_pk_name: Optional[str] = None,
        resolve_keys: Optional[Dict[str, Any]] = None,
        resolve_view_args: Optional[Dict[str, Any]] = None,
        resolve_value: Optional[Union[str, Dict[str, str]]] = None,
        resolve_name: Optional[Union[str, Dict[str, str]]] = None,
        resolve_as: Optional[Union[str, Dict[str, str]]] = None,
        schema: Any = None,
        **kwargs
    ):
        """Registers a nested child sub-resource under /<parent_pk>/sub_path."""
        target_key = args_key or view_args_key or foreign_key
        if not target_key:
            raise ValueError("sub_resource requires 'args_key' (or 'view_args_key')")

        view_resolvers = dict(
            resolve_keys
            or resolve_view_args
            or kwargs.pop("resolve_keys", {})
            or kwargs.pop("resolve_view_args", {})
            or kwargs.pop("view_args_config", {})
            or kwargs.pop("view_args_ref", {})
        )

        explicit_parent = parent_arg or parent_pk_name or kwargs.pop("parent_arg", None)
        if explicit_parent:
            p_pk_name = explicit_parent
        elif isinstance(target_key, dict) and len(target_key) == 1:
            p_pk_name = list(target_key.keys())[0]
        elif view_resolvers and len(view_resolvers) == 1:
            p_pk_name = list(view_resolvers.keys())[0]
        elif isinstance(target_key, str) and not hasattr(model, target_key) and hasattr(model, f"{self.model.__name__.lower()}_{self.pk_name}"):
            p_pk_name = target_key
        else:
            p_pk_name = f"{self.model.__name__.lower()}_{self.pk_name}"

        pk_pattern = build_pk_route_pattern(p_pk_name, self.pk_type)
        parent_prefix = f"{self.route_prefix.rstrip('/')}{pk_pattern}"
        child_prefix = f"{parent_prefix}/{sub_path.lstrip('/')}"

        request_view_args = dict(kwargs.pop("request_view_args", {}))
        if isinstance(target_key, dict):
            request_view_args.update(target_key)
        elif isinstance(target_key, str):
            default_fk = f"{self.model.__name__.lower()}_{self.pk_name}"
            if not hasattr(model, target_key) and hasattr(model, default_fk):
                request_view_args[p_pk_name] = default_fk
            else:
                request_view_args[p_pk_name] = target_key

        if p_pk_name not in view_resolvers:
            view_resolvers[p_pk_name] = self.model

        target_resolve_value = (
            resolve_value
            or resolve_name
            or resolve_as
            or kwargs.pop("resolve_value", None)
            or kwargs.pop("resolve_name", None)
            or kwargs.pop("resolve_as", None)
        )
        if not target_resolve_value:
            target_resolve_value = {p_pk_name: self.model.__name__.lower()}
        elif isinstance(target_resolve_value, str):
            target_resolve_value = {p_pk_name: target_resolve_value}

        endpoint_name = kwargs.pop("endpoint_name", f"{self.endpoint_name}_{sub_path.strip('/').replace('-', '_')}")

        return ApiProvider(
            self.app_or_bp,
            model=model,
            schema=schema,
            db=kwargs.pop("db", self.db),
            endpoint_name=endpoint_name,
            url_prefix=child_prefix,
            request_view_args=request_view_args,
            resolve_keys=view_resolvers,
            resolve_value=target_resolve_value,
            **kwargs
        )

    # Backward-compatible alias
    register_nested = sub_resource


# Alias ApiBuilder for drop-in compatibility with existing code
ApiBuilder = ApiProvider
