from functools import wraps
from flask import g, jsonify, make_response, request, Response
from werkzeug.exceptions import BadRequest


def requires_json(f):
    """Enforces Content-Type: application/json and injects json_data kwarg."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not request.is_json:
            raise BadRequest("Request body must have Content-Type: application/json")
        kwargs["json_data"] = request.get_json(silent=True) or {}
        return f(*args, **kwargs)
    return decorated


def json_response(default_status: int = 200, envelope: str | None = None):
    """Serializes view output into standard Flask JSON responses.

    Supports:
        - data -> (jsonify(data), default_status)
        - (data, code) -> (jsonify(data), code)
        - (data, code, headers) -> (jsonify(data), code, headers)
        - 204 -> empty body
        - Response instances pass through directly.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            result = f(*args, **kwargs)

            if isinstance(result, Response):
                return result

            data = result
            code = default_status
            headers = {}

            if isinstance(result, tuple):
                if len(result) == 3:
                    data, code, headers = result
                elif len(result) == 2:
                    data, code = result

            if code == 204:
                return make_response("", 204, headers)

            # Apply enveloping if enabled
            env_key = envelope or getattr(g, "envelope_key", None)
            if env_key and isinstance(data, (dict, list)):
                data = {env_key: data, "status": "ok", "code": code}

            return make_response(jsonify(data), code, headers)

        return decorated
    return decorator
