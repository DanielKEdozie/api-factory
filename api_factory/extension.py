from .converters import register_converters
from .responses import register_json_error_handlers


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

        # Register int_or_str converter and JSON error handlers automatically on the app
        register_converters(app)
        register_json_error_handlers(app)

        if not hasattr(app, "extensions"):
            app.extensions = {}
        app.extensions["api_factory"] = self

    def create_resource(self, model, blueprint=None, app=None, schema=None, **kwargs):
        """Instantiate and register an ApiProvider resource for a model."""
        from .builder import ApiProvider

        target = blueprint or app or self.app
        if target is None:
            raise ValueError("A blueprint or app must be provided to create_resource()")

        register_json_error_handlers(target)

        if "db" not in kwargs and self.db is not None:
            kwargs["db"] = self.db

        return ApiProvider(target, model, schema=schema, **kwargs)

    # Backward-compatible aliases
    create_api = create_resource
    build = create_resource

