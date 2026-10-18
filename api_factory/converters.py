from werkzeug.routing import BaseConverter


class IntOrStrConverter(BaseConverter):
    """Matches any non-empty, non-slash path segment.

    Coerces to int when purely numeric (preserving integer PK query semantics),
    otherwise passes through as str (supporting slugs, UUIDs, and codes).
    """
    regex = r"[^/]+"

    def to_python(self, value: str) -> int | str:
        try:
            return int(value)
        except ValueError:
            return value

    def to_url(self, value: int | str) -> str:
        return str(value)


def register_converters(app_or_bp):
    """Register custom URL converters on a Flask app or Blueprint."""
    if hasattr(app_or_bp, "record_once"):
        app_or_bp.record_once(lambda state: register_converters(state.app))
    elif hasattr(app_or_bp, "url_map") and hasattr(app_or_bp.url_map, "converters"):
        if "int_or_str" not in app_or_bp.url_map.converters:
            app_or_bp.url_map.converters["int_or_str"] = IntOrStrConverter


def build_pk_route_pattern(pk_name: str = "id", pk_type: str = "int_or_str") -> str:
    """Build URL route variable pattern, e.g. /<int_or_str:id> or /<int:pk>."""
    pk_name = pk_name or "id"
    pk_type = pk_type or "int_or_str"
    if pk_type in ("int_or_str", "int", "string", "str", "uuid"):
        norm_type = "string" if pk_type == "str" else pk_type
        return f"/<{norm_type}:{pk_name}>"
    return f"/<{pk_name}>"
