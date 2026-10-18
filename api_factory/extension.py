from .converters import register_converters


class ApiFactory:
    """Flask extension for managing centralized API configuration,
    database connections, and custom converters.
    """

    def __init__(self, app=None, db=None, ma=None):
        self.app = app
        self.db = db
        self.ma = ma
        if app is not None:
            self.init_app(app, db=db, ma=ma)

    def init_app(self, app, db=None, ma=None):
        self.app = app
        if db is not None:
            self.db = db
        if ma is not None:
            self.ma = ma

        # Register int_or_str converter automatically on the app
        register_converters(app)

        if not hasattr(app, "extensions"):
            app.extensions = {}
        app.extensions["api_factory"] = self

    def create_resource(self, model, blueprint=None, app=None, schema=None, **kwargs):
        """Instantiate and register an ApiProvider resource for a model."""
        from .builder import ApiProvider

        target = blueprint or app or self.app
        if target is None:
            raise ValueError("A blueprint or app must be provided to create_resource()")

        if "db" not in kwargs and self.db is not None:
            kwargs["db"] = self.db

        return ApiProvider(target, model, schema=schema, **kwargs)

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
            decorators=decorators,
            endpoint=endpoint,
            envelope=envelope,
            **options
        )

