import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from sqlalchemy import asc, cast, desc, func, or_, select, text, String
from sqlalchemy.orm import joinedload, selectinload, subqueryload

log = logging.getLogger(__name__)

LOAD_STRATEGIES: dict[str, Callable] = {
    "joinedload": joinedload,
    "selectinload": selectinload,
    "subqueryload": subqueryload,
}

#: A single ``raw_sort`` term: an optionally table-qualified column name.
#: Anything richer (functions, subqueries, operators, comments) is rejected so
#: user input is never interpolated into SQL as free text.
_RAW_SORT_TERM = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")


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


def _iter_eager_items(action: str, eager_loads: Any) -> list:
    """Flatten the accepted ``eager_loads`` shapes into a list of declarations."""
    action_aliases = {
        "collection": ["collection", "get_all", "list"],
        "get_all": ["collection", "get_all", "list"],
        "item": ["item", "get_one", "retrieve"],
        "get_one": ["item", "get_one", "retrieve"],
    }
    keys = action_aliases.get(action, [action])

    if isinstance(eager_loads, dict):
        items = list(eager_loads.get("*", []))
        for k in keys:
            if k in eager_loads:
                items.extend(eager_loads[k])
                break
        return items
    if isinstance(eager_loads, (list, tuple, set)):
        return list(eager_loads)
    return []


def configured_eager_load_names(eager_loads: Any, action: str) -> set[str]:
    """Return relationship attribute names already targeted by ``eager_loads``.

    Declaring the same relationship twice with different strategies raises
    ``InvalidRequestError``, so callers that add further loader options (such as
    the ``?include=`` handling) use this to skip what is already configured.
    """
    names = set()
    for item in _iter_eager_items(action, eager_loads):
        if isinstance(item, (tuple, list)) and len(item) == 2:
            names.add(str(item[1]))
        elif isinstance(item, str):
            names.add(item)
    return names


def apply_eager_loads(stmt: Any, model: Any, action: str, eager_loads: Any) -> Any:
    """Applies declarative loader strategies for an action.
    Supports:
        - dict: {'*': [('joinedload', 'category')], 'collection': [...]}
        - list of tuples: [('joinedload', 'category'), ('selectinload', 'images')]
        - list of strings: ['category', 'images'] (auto-chooses joinedload for single, selectinload for collections)
        - list of SQLAlchemy loader options: [joinedload(Model.category), selectinload(Model.images)]
    """
    if not eager_loads:
        return stmt

    items = _iter_eager_items(action, eager_loads)

    from sqlalchemy import inspect as sqla_inspect
    mapper = None
    try:
        mapper = sqla_inspect(model)
    except Exception:
        pass

    for item in items:
        # Case 1: Raw SQLAlchemy loader option already constructed (e.g. db.joinedload(Model.category))
        if hasattr(item, "_is_core") or hasattr(item, "is_core") or hasattr(item, "path") or hasattr(item, "propagate_to_loaders") or "Load" in type(item).__name__:
            stmt = stmt.options(item)
            continue

        # Case 2: Tuple (strat_name, attr_name)
        if isinstance(item, (tuple, list)) and len(item) == 2:
            strat_name, attr_name = item
            loader = LOAD_STRATEGIES.get(str(strat_name).lower(), selectinload)
            if hasattr(model, str(attr_name)):
                stmt = stmt.options(loader(getattr(model, str(attr_name))))
            continue

        # Case 3: String name of relationship (e.g. 'category', 'images')
        if isinstance(item, str) and hasattr(model, item):
            col = getattr(model, item)
            loader = selectinload
            if mapper and item in mapper.relationships:
                rel = mapper.relationships[item]
                if not rel.uselist:
                    loader = joinedload
            stmt = stmt.options(loader(col))
            continue

        # Case 4: SQLAlchemy attribute directly passed (e.g. Equipment.category)
        if hasattr(item, "property"):
            loader = selectinload
            if hasattr(item.property, "uselist") and not item.property.uselist:
                loader = joinedload
            stmt = stmt.options(loader(item))
            continue

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


def _is_safe_raw_sort(model: Any, term: str) -> bool:
    """Return True when ``term`` is a plain (optionally qualified) model column.

    ``raw_sort`` exists to allow ``?sort=table.column``, not arbitrary SQL.
    Accepting anything else lets a caller splice subqueries or comments into
    ``ORDER BY``, so the term is matched against a strict identifier pattern
    and then resolved against the model before it reaches ``text()``.
    """
    if not term or not _RAW_SORT_TERM.match(term):
        return False
    attr = term.rpartition(".")[2] or term
    return hasattr(model, attr)


def apply_sorting(
    stmt: Any,
    model: Any,
    sort_fields: Optional[List[str]],
    raw_sort: bool,
    sort_param: Optional[Any],
    order_param: Optional[str] = None,
    default_sort: Optional[str] = None,
    default_order: str = "asc",
) -> Any:
    """Appends ORDER BY clauses from ?sort= and ?order=, or falls back to default_sort."""
    allowed = set(sort_fields or [])
    if default_sort:
        allowed.add(default_sort)

    # If no sort parameter was supplied in query params, apply default sorting
    if not sort_param:
        fallback_field = default_sort or (sort_fields[0] if sort_fields else None)
        if not fallback_field:
            return stmt
        default_dir = (order_param or default_order or "asc").strip().lower()
        descending = default_dir in ("desc", "descending")
        if hasattr(model, fallback_field):
            col = getattr(model, fallback_field)
            stmt = stmt.order_by(desc(col) if descending else asc(col))
        return stmt

    # Tokenize sort_param (handles string "name,created_at", list ["name", "created_at"], "-name", etc.)
    tokens: List[str] = []
    if isinstance(sort_param, list):
        for item in sort_param:
            tokens.extend([t.strip() for t in str(item).split(",") if t.strip()])
    elif isinstance(sort_param, str):
        tokens = [t.strip() for t in sort_param.split(",") if t.strip()]

    global_order = (order_param or default_order or "asc").strip().lower()
    global_desc = global_order in ("desc", "descending")

    for token in tokens:
        descending = global_desc
        clean = token
        if clean.startswith("-"):
            descending = True
            clean = clean[1:].strip()
        elif clean.startswith("+"):
            descending = False
            clean = clean[1:].strip()

        if raw_sort:
            # Never interpolate the raw term into SQL text without validating it
            # first -- ``?sort=`` is user input and ``text()`` is not escaped.
            if _is_safe_raw_sort(model, clean):
                direction = "DESC" if descending else "ASC"
                stmt = stmt.order_by(text(f"{clean} {direction}"))
            else:
                log.warning("Rejected unsafe raw sort term: %r", clean)
        else:
            if clean in allowed and hasattr(model, clean):
                col = getattr(model, clean)
                stmt = stmt.order_by(desc(col) if descending else asc(col))

    return stmt
