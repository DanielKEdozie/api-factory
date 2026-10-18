from typing import Any, Callable, Dict, List, Optional, Union
from flask import jsonify, make_response
from .converters import register_converters


class ApiFactory:
    """Flask extension for managing centralized API configuration,
    database connections, custom converters, and automatic OpenAPI documentation.
    """

    def __init__(
        self,
        app=None,
        db=None,
        ma=None,
        docs: Union[bool, str, List[str]] = False,
        docs_url: str = "/docs",
        redoc_url: str = "/redoc",
        scalar_url: str = "/scalar",
        openapi_url: str = "/openapi.json",
        title: str = "API Documentation",
        version: str = "1.0.0",
        description: str = "RESTful API powered by api-factory",
        error_schema: Optional[Any] = None,
        error_schemas: Optional[Dict[Union[int, str], Any]] = None,
    ):
        self.app = app
        self.db = db
        self.ma = ma
        self.resources: List[Any] = []
        self.custom_views: List[Dict[str, Any]] = []

        self.error_schema = error_schema
        self.error_schemas: Dict[Union[int, str], Any] = dict(error_schemas or {})
        if error_schema and "default" not in self.error_schemas:
            self.error_schemas["default"] = error_schema

        providers = []
        if isinstance(docs, (list, tuple, set)):
            providers = [p.lower() for p in docs]
        elif isinstance(docs, str):
            providers = [docs.lower()]
        elif docs is True:
            providers = ["swagger", "redoc", "scalar"]

        self.docs_config = {
            "enabled": bool(providers),
            "providers": providers,
            "docs_url": docs_url,
            "redoc_url": redoc_url,
            "scalar_url": scalar_url,
            "openapi_url": openapi_url,
            "title": title,
            "version": version,
            "description": description,
        }

        if app is not None:
            self.init_app(app, db=db, ma=ma)

    def init_app(
        self,
        app,
        db=None,
        ma=None,
        docs: Optional[Union[bool, str, List[str]]] = None,
        title: Optional[str] = None,
        version: Optional[str] = None,
        description: Optional[str] = None,
        docs_url: Optional[str] = None,
        redoc_url: Optional[str] = None,
        scalar_url: Optional[str] = None,
        openapi_url: Optional[str] = None,
        error_schema: Optional[Any] = None,
        error_schemas: Optional[Dict[Union[int, str], Any]] = None,
    ):
        self.app = app
        if db is not None:
            self.db = db
        if ma is not None:
            self.ma = ma

        if error_schema is not None:
            self.error_schema = error_schema
            self.error_schemas["default"] = error_schema
        if error_schemas is not None:
            self.error_schemas.update(error_schemas)

        if docs is not None:
            providers = []
            if isinstance(docs, (list, tuple, set)):
                providers = [p.lower() for p in docs]
            elif isinstance(docs, str):
                providers = [docs.lower()]
            elif docs is True:
                providers = ["swagger", "redoc", "scalar"]
            self.docs_config["enabled"] = bool(providers)
            self.docs_config["providers"] = providers

        if title is not None:
            self.docs_config["title"] = title
        if version is not None:
            self.docs_config["version"] = version
        if description is not None:
            self.docs_config["description"] = description
        if docs_url is not None:
            self.docs_config["docs_url"] = docs_url
        if redoc_url is not None:
            self.docs_config["redoc_url"] = redoc_url
        if scalar_url is not None:
            self.docs_config["scalar_url"] = scalar_url
        if openapi_url is not None:
            self.docs_config["openapi_url"] = openapi_url

        # Register int_or_str converter automatically on the app
        register_converters(app)

        if not hasattr(app, "extensions"):
            app.extensions = {}
        app.extensions["api_factory"] = self

        if self.docs_config.get("enabled"):
            self.register_docs(app)

    def register_docs(
        self,
        app_or_bp: Optional[Any] = None,
        title: Optional[str] = None,
        version: Optional[str] = None,
        description: Optional[str] = None,
        providers: Optional[Union[str, List[str]]] = None,
        docs_url: Optional[str] = None,
        redoc_url: Optional[str] = None,
        scalar_url: Optional[str] = None,
        openapi_url: Optional[str] = None,
    ):
        """Registers OpenAPI 3 specification and interactive UI viewers (Swagger UI, ReDoc, Scalar)."""
        from .openapi import (
            generate_openapi_spec,
            get_swagger_ui_html,
            get_redoc_html,
            get_scalar_html,
        )

        target = app_or_bp or self.app
        if target is None:
            raise ValueError("An App or Blueprint must be provided to register_docs()")

        cfg = self.docs_config
        t_title = title or cfg["title"]
        t_version = version or cfg["version"]
        t_desc = description or cfg["description"]
        t_openapi = openapi_url or cfg["openapi_url"]
        t_swagger = docs_url or cfg["docs_url"]
        t_redoc = redoc_url or cfg["redoc_url"]
        t_scalar = scalar_url or cfg["scalar_url"]

        if providers is not None:
            p_list = [p.lower() for p in ([providers] if isinstance(providers, str) else providers)]
        else:
            p_list = cfg.get("providers") or ["swagger", "redoc", "scalar"]

        # 1. OpenAPI 3.0 JSON specification endpoint
        def openapi_json():
            spec = generate_openapi_spec(
                factory=self,
                title=t_title,
                version=t_version,
                description=t_desc,
            )
            return jsonify(spec)

        target.add_url_rule(t_openapi, endpoint="api_factory_openapi_json", view_func=openapi_json)

        # 2. Swagger UI
        if "swagger" in p_list:
            def swagger_ui():
                html = get_swagger_ui_html(openapi_url=t_openapi, title=t_title)
                return make_response(html, 200, {"Content-Type": "text/html"})
            target.add_url_rule(t_swagger, endpoint="api_factory_swagger_ui", view_func=swagger_ui)

        # 3. ReDoc
        if "redoc" in p_list:
            def redoc_ui():
                html = get_redoc_html(openapi_url=t_openapi, title=t_title)
                return make_response(html, 200, {"Content-Type": "text/html"})
            target.add_url_rule(t_redoc, endpoint="api_factory_redoc_ui", view_func=redoc_ui)

        # 4. Scalar
        if "scalar" in p_list:
            def scalar_ui():
                html = get_scalar_html(openapi_url=t_openapi, title=t_title)
                return make_response(html, 200, {"Content-Type": "text/html"})
            target.add_url_rule(t_scalar, endpoint="api_factory_scalar_ui", view_func=scalar_ui)

    def create_resource(self, model, blueprint=None, app=None, schema=None, docs=None, **kwargs):
        """Instantiate and register an ApiProvider resource for a model."""
        from .builder import ApiProvider

        target = blueprint or app or self.app
        if target is None:
            raise ValueError("A blueprint or app must be provided to create_resource()")

        if "db" not in kwargs and self.db is not None:
            kwargs["db"] = self.db

        res = ApiProvider(target, model, schema=schema, docs=docs, factory=self, **kwargs)
        self.resources.append(res)
        return res

    # Backward-compatible aliases
    create_api = create_resource
    build = create_resource

    def resource_view(
        self,
        path: str,
        view: Any = None,
        fn: Any = None,
        blueprint: Any = None,
        app: Any = None,
        methods: Any = None,
        schema: Any = None,
        schema_validation: Any = None,
        validate: Any = None,
        docs: Any = None,
        decorators: Any = None,
        endpoint: Any = None,
        envelope: Any = None,
        **options
    ):
        """Registers a custom resource view on a Blueprint or App."""
        from .custom_view import create_resource_view

        target = blueprint or app or self.app
        if target is None:
            raise ValueError("A blueprint or app must be provided to resource_view()")

        req_methods = [m.upper() for m in (methods or ["GET"])]
        self.custom_views.append({
            "rule": path,
            "methods": req_methods,
            "schema": schema,
            "docs": docs,
            "endpoint": endpoint,
        })

        return create_resource_view(
            target=target,
            rule=path,
            view_or_fn=view,
            fn=fn,
            methods=methods,
            schema=schema,
            schema_validation=schema_validation,
            validate=validate,
            db_session=self.db.session if self.db else None,
            builder=self,
            ctx=self,
            decorators=decorators,
            endpoint=endpoint,
            envelope=envelope,
            **options
        )
