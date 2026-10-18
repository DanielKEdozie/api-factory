import time
from collections import defaultdict
from functools import wraps
from typing import Callable, Optional, Tuple
from flask import request, make_response, jsonify


class RateLimitStore:
    """Sliding-window in-memory counter with timestamp eviction."""

    def __init__(self):
        self._windows: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        # Clean expired hits
        self._windows[key] = [t for t in self._windows[key] if t > cutoff]

        if len(self._windows[key]) >= limit:
            return False

        self._windows[key].append(now)
        return True

    def clear(self):
        self._windows.clear()


default_rate_store = RateLimitStore()


def parse_rate_string(rate_str: str) -> Tuple[int, int]:
    """Parse '10/minute', '2/second', '100/hour', '500/day' into (limit, window_seconds)."""
    parts = rate_str.strip().lower().split("/")
    count = int(parts[0])
    if len(parts) == 1:
        return count, 60
    unit = parts[1].strip()
    multiplier = 60
    if unit in ("s", "sec", "second", "seconds"):
        multiplier = 1
    elif unit in ("m", "min", "minute", "minutes"):
        multiplier = 60
    elif unit in ("h", "hr", "hour", "hours"):
        multiplier = 3600
    elif unit in ("d", "day", "days"):
        multiplier = 86400
    return count, multiplier


def rate_limit(
    limit: int,
    window: int = 60,
    key_func: Optional[Callable[[], str]] = None,
    store: Optional[RateLimitStore] = None
):
    """Per-route sliding-window rate limit decorator.

    Returns HTTP 429 with Retry-After header when exceeded.
    """
    limiter = store or default_rate_store

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            resolver = key_func or (lambda: request.remote_addr or "anonymous")
            endpoint = request.endpoint or f"{f.__module__}.{f.__qualname__}"
            bucket = f"{endpoint}:{resolver()}"

            if not limiter.is_allowed(bucket, limit, window):
                resp = make_response(
                    jsonify({
                        "error": "Too Many Requests",
                        "message": f"Rate limit exceeded. Try again in {window} seconds.",
                        "retry_after": window
                    }),
                    429
                )
                resp.headers["Retry-After"] = str(window)
                return resp

            return f(*args, **kwargs)

        return decorated

    return decorator


def resolve_rate_limit_decorator(cfg: Any, store: Optional[RateLimitStore] = None):
    """Normalize (limit, window), string ('10/minute'), or integer limit into a rate_limit decorator."""
    if isinstance(cfg, str):
        limit, window = parse_rate_string(cfg)
        return rate_limit(limit=limit, window=window, store=store)
    elif isinstance(cfg, tuple) and len(cfg) >= 2:
        return rate_limit(limit=cfg[0], window=cfg[1], store=store)
    elif isinstance(cfg, int):
        return rate_limit(limit=cfg, window=60, store=store)
    return None

