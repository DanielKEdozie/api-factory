"""api_factory - A modular, extensible Flask REST API factory with MethodViews,

automated schemas, rate limiting, and eager loading.
"""

from .extension import ApiFactory
from .builder import ApiProvider, ApiBuilder
from .views import CollectionView, ItemView, SingletonView
from .schema_builder import SchemaBuilder
from .rate_limiter import rate_limit, RateLimitStore
from .responses import json_response, requires_json
from .converters import IntOrStrConverter
from .exporters import build_csv_response

from .context import ContextValues
from .custom_view import create_resource_view

__version__ = "1.0.0"

__all__ = [
    "ApiFactory",
    "ApiProvider",
    "ApiBuilder",
    "CollectionView",
    "ItemView",
    "SingletonView",
    "SchemaBuilder",
    "rate_limit",
    "RateLimitStore",
    "json_response",
    "requires_json",
    "IntOrStrConverter",
    "build_csv_response",
    "ContextValues",
    "create_resource_view",
]
