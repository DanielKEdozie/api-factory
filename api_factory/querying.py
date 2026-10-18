import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from sqlalchemy import asc, cast, desc, func, or_, select, text, String
from sqlalchemy.orm import joinedload, selectinload, subqueryload

log = logging.getLogger(__name__)

LOAD_STRATEGIES: dict[str, Callable] = {
    "joinedload": joinedload,
    "selectinload": selectinload,
    "subqueryload": subqueryload,
}


def apply_scopes(stmt: Any, model: Any, request_view_args: dict, view_kwargs: dict) -> Any:
    """Appends WHERE clauses matching URL route variables (e.g. equipment_id)."""
    for url_key, model_attr in (request_view_args or {}).items():
        if url_key in view_kwargs and hasattr(model, model_attr):
            val = view_kwargs[url_key]
            col = getattr(model, model_attr)
            stmt = stmt.where(col == val)
    return stmt


def apply_soft_delete_filter(stmt: Any, model: Any, soft_delete: bool) -> Any:
    """Appends WHERE deleted_at IS NULL when soft_delete=True."""
    if soft_delete and hasattr(model, "deleted_at"):
        stmt = stmt.where(model.deleted_at.is_(None))
    return stmt


def apply_eager_loads(stmt: Any, model: Any, action: str, eager_loads: Optional[Dict[str, List[Tuple[str, str]]]]) -> Any:
    """Applies declarative loader strategies for an action."""
    if not eager_loads:
        return stmt

    action_aliases = {
        "collection": ["collection", "get_all", "list"],
        "get_all": ["collection", "get_all", "list"],
        "item": ["item", "get_one", "retrieve"],
        "get_one": ["item", "get_one", "retrieve"],
    }
    keys = action_aliases.get(action, [action])

    strategies = []
    if "*" in eager_loads:
        strategies.extend(eager_loads["*"])

    for k in keys:
        if k in eager_loads:
            strategies.extend(eager_loads[k])
            break

    for strat_name, attr_name in strategies:
        loader = LOAD_STRATEGIES.get(strat_name.lower())
        if loader is None:
            log.warning("Unknown eager load strategy: %s for %s", strat_name, attr_name)
            continue
        if not hasattr(model, attr_name):
            log.warning("Model %s has no attribute %s for eager loading", model.__name__, attr_name)
            continue
        stmt = stmt.options(loader(getattr(model, attr_name)))
    return stmt


def apply_filters(
    stmt: Any,
    model: Any,
    filter_fields: Optional[List[str]],
    filter_types: Optional[Dict[str, Callable]],
    args: Dict[str, Any]
) -> Any:
    """Appends exact-match and type-coerced WHERE clauses from request query parameters."""
    allowed = set(filter_fields or [])
    coercions = filter_types or {}

    for key, raw_val in args.items():
        if key not in allowed or not hasattr(model, key):
            continue
        if raw_val is None or raw_val.lower() in ("undefined", ""):
            continue

        coerce_fn = coercions.get(key, str)
        try:
            if raw_val.lower() in ("null", "none"):
                val = None
            else:
                val = coerce_fn(raw_val)
        except (ValueError, TypeError):
            continue

        col = getattr(model, key)
        if val is None:
            stmt = stmt.where(col.is_(None))
        else:
            stmt = stmt.where(col == val)
    return stmt


def apply_search(stmt: Any, model: Any, search_fields: Optional[List[str]], q: Optional[str]) -> Any:
    """Appends OR ILIKE conditions across search_fields for ?q=."""
    if not q or not search_fields:
        return stmt

    q_clean = q.strip()
    if not q_clean:
        return stmt

    conditions = [
        cast(getattr(model, f), String).ilike(f"%{q_clean}%")
        for f in search_fields
        if hasattr(model, f)
    ]
    if conditions:
        stmt = stmt.where(or_(*conditions))
    return stmt


def apply_sorting(
    stmt: Any,
    model: Any,
    sort_fields: Optional[List[str]],
    raw_sort: bool,
    sort_param: Optional[str],
    order_param: Optional[str]
) -> Any:
    """Appends ORDER BY clauses from ?sort= and ?order=."""
    if not sort_param:
        return stmt

    sort_clean = sort_param.strip()
    order = (order_param or "asc").strip().lower()
    descending = order in ("desc", "descending")

    if raw_sort:
        try:
            direction = "DESC" if descending else "ASC"
            stmt = stmt.order_by(text(f"{sort_clean} {direction}"))
        except Exception as exc:
            log.warning("Invalid raw sort %s: %s", sort_clean, exc)
        return stmt

    allowed = set(sort_fields or [])
    if sort_clean in allowed and hasattr(model, sort_clean):
        col = getattr(model, sort_clean)
        stmt = stmt.order_by(desc(col) if descending else asc(col))

    return stmt
