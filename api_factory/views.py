from datetime import date, datetime, timezone
from decimal import Decimal
import json
import math
from typing import Any, Optional
import uuid
from flask import abort, current_app, g, request
from flask.views import MethodView
from marshmallow import ValidationError
from sqlalchemy import func, inspect as sqla_inspect, or_, select
from sqlalchemy.orm import selectinload
from .context import ContextValues
from .exporters import build_csv_response
from .querying import (
    apply_eager_loads,
    apply_filters,
    apply_scopes,
    apply_search,
    apply_soft_delete_filter,
    apply_sorting,
    configured_eager_load_names,
)


def _apply_include_eager_loads(stmt: Any, model: Any, include_fields: list[str], builder: Any, action: str) -> Any:
    """Eager-load relationships named in ``?include=`` that ``eager_loads`` does not already cover.

    Re-declaring a relationship that the resource already eager-loads raises
    ``InvalidRequestError`` because SQLAlchemy refuses conflicting loader
    strategies for the same relationship, so those names are skipped.
    """
    if not include_fields:
        return stmt
    try:
        mapper = sqla_inspect(model)
    except Exception:
        return stmt

    already_loaded = configured_eager_load_names(builder.eager_loads, action)
    for field in include_fields:
        if field in already_loaded or field not in mapper.relationships:
            continue
        stmt = stmt.options(selectinload(getattr(model, field)))
    return stmt


def _rows(session: Any, stmt: Any) -> list:
    """Execute a statement returning rows, tolerating joined eager loads.

    SQLAlchemy raises ``InvalidRequestError`` unless ``unique()`` is applied when
    a ``joinedload`` targets a to-many relationship, because the join duplicates
    the parent row. ``unique()`` is a no-op for the other loader strategies, so
    it is applied unconditionally.
    """
    return session.execute(stmt).unique().scalars().all()


def _first_row(session: Any, stmt: Any) -> Any:
    """Return the first row of a statement, or ``None``.

    Uses ``first()`` rather than ``scalar_one_or_none()`` so a query that
    unexpectedly matches several rows degrades to a result instead of a 500.
    """
    return session.execute(stmt).unique().scalars().first()


def parse_param_list(param_val: Any) -> list[str]:
    """Parses 'a,b,c' or '[\"a\", \"b\"]' into a list of strings."""
    if not param_val:
        return []
    s = str(param_val).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    try:
        parsed = json.loads(f"[{s}]")
        if isinstance(parsed, list):
            return [str(x).strip().strip("'\"") for x in parsed if str(x).strip()]
    except Exception:
        pass
    return [p.strip().strip("'\"") for p in s.split(",") if p.strip()]


def to_primitive(val: Any) -> Any:
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, uuid.UUID):
        return str(val)
    return val


def serialize_model_instance(inst: Any) -> Any:
    if inst is None:
        return None
    if hasattr(inst, "to_dict") and callable(inst.to_dict):
        return inst.to_dict()
    if hasattr(inst, "__table__"):
        return {c.name: to_primitive(getattr(inst, c.name)) for c in inst.__table__.columns}
    return to_primitive(inst)


def format_item_with_inclusions_and_exclusions(
    item_obj: Any,
    base_dict: dict,
    include_fields: list[str],
    exclude_fields: list[str]
) -> dict:
    data = dict(base_dict)

    # Process dynamic nested includes
    for field in include_fields:
        if hasattr(item_obj, field):
            val = getattr(item_obj, field)
            if val is None:
                data[field] = None
            elif hasattr(val, "__iter__") and not isinstance(val, (str, bytes, dict)):
                data[field] = [serialize_model_instance(elem) for elem in val]
            else:
                data[field] = serialize_model_instance(val)

    # Process dynamic excludes
    for field in exclude_fields:
        data.pop(field, None)

    return data


class CollectionView(MethodView):
    """MethodView handling collection operations: GET (list / export) and POST (create)."""

    def __init__(self, builder):
        self.builder = builder

    def dispatch_request(self, *args, **kwargs):
        method = request.method.lower()
        meth = getattr(self, method, None)
        if meth is None and request.method == "HEAD":
            meth = getattr(self, "get", None)
            method = "get"
        assert meth is not None, f"Unimplemented method {request.method!r}"

        action = "create" if method == "post" else "collection"
        wrapped = self.builder._apply_decorators_to_method(meth, action)
        return current_app.ensure_sync(wrapped)(*args, **kwargs)

    def get(self, **kwargs):
        builder = self.builder
        model = builder.model
        session = builder.session

        builder._apply_envelope()

        # Validate parent references if nested view_args_config defined
        builder._validate_view_args(kwargs)

        stmt = select(model)
        stmt = apply_scopes(stmt, model, builder.request_view_args, kwargs)
        stmt = apply_soft_delete_filter(stmt, model, builder.soft_delete)
        stmt = builder._apply_query_override(stmt, "collection", **kwargs)
        stmt = apply_filters(stmt, model, builder.filter_fields, builder.filter_types, request.args)
        stmt = apply_search(stmt, model, builder.search_fields, request.args.get("q"))
        sort_arg = request.args.getlist("sort") if len(request.args.getlist("sort")) > 1 else request.args.get("sort")
        stmt = apply_sorting(
            stmt, model, builder.sort_fields, builder.raw_sort,
            sort_arg, request.args.get("order"),
            default_sort=builder.default_sort_field,
            default_order=builder.sort_order,
        )

        # Snapshot before eager loading: a joinedload against a to-many relationship
        # multiplies rows in the FROM clause and would inflate the pagination total.
        count_stmt = stmt.order_by(None)

        stmt = apply_eager_loads(stmt, model, "collection", builder.eager_loads)

        # Parse ?include= and ?exclude=
        include_fields = parse_param_list(request.args.get("include"))
        exclude_fields = parse_param_list(request.args.get("exclude"))

        # Automatically eager-load any relationship in include_fields
        stmt = _apply_include_eager_loads(stmt, model, include_fields, builder, "collection")

        # Check for CSV export request (?export=csv)
        if request.args.get("export", "").lower() == "csv":
            rows = _rows(session, stmt)
            raw_serialized = builder.schema.dump(rows, many=True)
            if include_fields or exclude_fields:
                serialized = [
                    format_item_with_inclusions_and_exclusions(r, d, include_fields, exclude_fields)
                    for r, d in zip(rows, raw_serialized)
                ]
            else:
                serialized = raw_serialized
            return build_csv_response(serialized, builder.export_fields, filename_prefix=builder.endpoint_name)

        # Check query param ?paginate=false
        paginate_param = request.args.get("paginate")
        disable_pagination = False
        if paginate_param is not None:
            disable_pagination = paginate_param.strip().lower() in ("false", "0", "no", "off")

        # Handle unpaginated list
        if not builder.paginate or disable_pagination:
            rows = _rows(session, stmt)
            raw_serialized = builder.schema.dump(rows, many=True)
            if include_fields or exclude_fields:
                serialized = [
                    format_item_with_inclusions_and_exclusions(r, d, include_fields, exclude_fields)
                    for r, d in zip(rows, raw_serialized)
                ]
            else:
                serialized = raw_serialized
            extra = builder.get_list_context(serialized, total=len(serialized), **kwargs)
            if extra:
                return {"items": serialized, **extra}
            return serialized

        # Handle paginated list
        page = max(request.args.get("page", 1, type=int), 1)
        req_per_page = request.args.get("per_page") or request.args.get("page_size")
        try:
            per_page_val = int(req_per_page) if req_per_page else builder.page_size
        except (ValueError, TypeError):
            per_page_val = builder.page_size
        per_page = min(max(per_page_val, 1), builder.max_page_size)

        total_stmt = select(func.count()).select_from(count_stmt.subquery())
        total = session.execute(total_stmt).scalar() or 0

        paged_stmt = stmt.limit(per_page).offset((page - 1) * per_page)
        rows = _rows(session, paged_stmt)
        raw_serialized = builder.schema.dump(rows, many=True)
        if include_fields or exclude_fields:
            serialized = [
                format_item_with_inclusions_and_exclusions(r, d, include_fields, exclude_fields)
                for r, d in zip(rows, raw_serialized)
            ]
        else:
            serialized = raw_serialized
        extra = builder.get_list_context(serialized, total=total, **kwargs)

        pages = math.ceil(total / per_page) if total and per_page else 0
        return {
            "items": serialized,
            "meta": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": pages,
            },
            **extra
        }

    def post(self, **kwargs):
        builder = self.builder
        session = builder.session

        builder._apply_envelope()
        values = builder._validate_view_args(kwargs) or getattr(g, "values", None) or ContextValues()

        if not request.is_json and not request.form:
            abort(400, description="Request body must be JSON or form-data")

        json_data = request.get_json(silent=True) or request.form.to_dict() or {}
        scoped_data = {**json_data}
        for url_key, model_attr in builder.request_view_args.items():
            if url_key in kwargs:
                scoped_data[model_attr] = kwargs[url_key]

        try:
            res = builder._invoke_hook(builder.before_create, scoped_data, values=values)
            if isinstance(res, dict):
                scoped_data = res

            item = builder.schema.load(scoped_data, session=session)
            session.add(item)
            session.flush()

            if builder.audit and hasattr(builder, "audit_log"):
                builder.audit_log(action="create", item=item, changes={"after": builder.schema.dump(item)})

            session.commit()
            builder._invoke_hook(builder.after_create, item, data=scoped_data, values=values)
            return builder.schema.dump(item), 201
        except ValidationError as exc:
            session.rollback()
            abort(400, description=exc.messages)
        except Exception:
            session.rollback()
            current_app.logger.exception("Error creating resource in %s", builder.endpoint_name)
            raise


class ItemView(MethodView):
    """MethodView handling item operations: GET (retrieve), PUT/PATCH (update), DELETE."""

    def __init__(self, builder):
        self.builder = builder

    def dispatch_request(self, *args, **kwargs):
        method = request.method.lower()
        meth = getattr(self, method, None)
        if meth is None and request.method == "HEAD":
            meth = getattr(self, "get", None)
            method = "get"
        assert meth is not None, f"Unimplemented method {request.method!r}"

        action_map = {
            "get": "item",
            "put": "update",
            "patch": "patch",
            "delete": "delete"
        }
        action = action_map.get(method, "item")
        wrapped = self.builder._apply_decorators_to_method(meth, action)
        return current_app.ensure_sync(wrapped)(*args, **kwargs)

    def _resolve_item(self, item_id: Any, view_kwargs: dict, action: str, include_fields: Optional[list[str]] = None):
        builder = self.builder
        model = builder.model
        session = builder.session

        builder._validate_view_args(view_kwargs)

        stmt = select(model)
        pk_name = builder.pk_name

        # Dual ID or slug lookup support when int_or_str is used
        if builder.pk_type == "int_or_str" and isinstance(item_id, str) and not item_id.isdigit():
            if hasattr(model, "slug"):
                stmt = stmt.where(model.slug == item_id)
            else:
                stmt = stmt.where(getattr(model, pk_name) == item_id)
        else:
            col = getattr(model, pk_name)
            stmt = stmt.where(col == item_id)

        stmt = apply_scopes(stmt, model, builder.request_view_args, view_kwargs)
        stmt = apply_soft_delete_filter(stmt, model, builder.soft_delete)
        stmt = builder._apply_query_override(stmt, action, **view_kwargs)
        stmt = apply_eager_loads(stmt, model, action, builder.eager_loads)
        stmt = _apply_include_eager_loads(stmt, model, include_fields or [], builder, action)

        item = _first_row(session, stmt)
        if item is None:
            abort(404, description=f"{model.__name__} not found")
        return item

    def get(self, **kwargs):
        item_id = kwargs.get(self.builder.pk_name)
        self.builder._apply_envelope()

        include_fields = parse_param_list(request.args.get("include"))
        exclude_fields = parse_param_list(request.args.get("exclude"))

        item = self._resolve_item(item_id, kwargs, "item", include_fields=include_fields)
        raw_serialized = self.builder.schema.dump(item)
        if include_fields or exclude_fields:
            return format_item_with_inclusions_and_exclusions(item, raw_serialized, include_fields, exclude_fields)
        return raw_serialized

    def put(self, **kwargs):
        return self._update_item(partial=False, **kwargs)

    def patch(self, **kwargs):
        return self._update_item(partial=True, **kwargs)

    def _update_item(self, partial: bool, **kwargs):
        builder = self.builder
        session = builder.session
        item_id = kwargs.get(builder.pk_name)

        builder._apply_envelope()
        item = self._resolve_item(item_id, kwargs, "update" if not partial else "patch")
        values = getattr(g, "values", None) or ContextValues()

        if not request.is_json and not request.form:
            abort(400, description="Request body must be JSON or form-data")

        json_data = request.get_json(silent=True) or request.form.to_dict() or {}

        try:
            before_snapshot = builder.schema.dump(item) if builder.audit else None
            res = builder._invoke_hook(builder.before_update, item, json_data, values=values)
            data = res if isinstance(res, dict) else json_data
            updated = builder.schema.load(data, instance=item, partial=partial, session=session)

            if builder.audit and hasattr(builder, "audit_log"):
                builder.audit_log(
                    action="update" if not partial else "patch",
                    item=updated,
                    changes={"before": before_snapshot, "after": builder.schema.dump(updated)}
                )

            session.commit()
            builder._invoke_hook(builder.after_update, updated, data=json_data, values=values)
            return builder.schema.dump(updated), 200
        except ValidationError as exc:
            session.rollback()
            abort(400, description=exc.messages)
        except Exception:
            session.rollback()
            current_app.logger.exception("Error updating resource in %s", builder.endpoint_name)
            raise

    def delete(self, **kwargs):
        builder = self.builder
        session = builder.session
        item_id = kwargs.get(builder.pk_name)

        item = self._resolve_item(item_id, kwargs, "delete")
        values = getattr(g, "values", None) or ContextValues()
        try:
            audit_snapshot = builder.schema.dump(item) if builder.audit else None

            if builder.soft_delete:
                if hasattr(item, "soft_delete"):
                    item.soft_delete()
                elif hasattr(item, "deleted_at"):
                    item.deleted_at = datetime.now(timezone.utc)
            else:
                builder._cleanup_relationships(item_id)
                session.delete(item)

            if builder.audit and hasattr(builder, "audit_log"):
                builder.audit_log(action="delete", item=item, changes={"before": audit_snapshot})

            session.commit()
            builder._invoke_hook(builder.after_delete, item, values=values)
            return "", 204
        except Exception:
            session.rollback()
            current_app.logger.exception("Error deleting resource in %s", builder.endpoint_name)
            raise


class SingletonView(MethodView):
    """MethodView handling single-resource / 1-to-1 endpoints (without <id> path variable)."""

    def __init__(self, builder):
        self.builder = builder

    def dispatch_request(self, *args, **kwargs):
        method = request.method.lower()
        meth = getattr(self, method, None)
        if meth is None and request.method == "HEAD":
            meth = getattr(self, "get", None)
            method = "get"
        assert meth is not None, f"Unimplemented method {request.method!r}"

        action_map = {
            "get": "get_singleton",
            "post": "create",
            "put": "update_singleton",
            "patch": "update_singleton",
            "delete": "delete_singleton"
        }
        action = action_map.get(method, "singleton")
        wrapped = self.builder._apply_decorators_to_method(meth, action)
        return current_app.ensure_sync(wrapped)(*args, **kwargs)

    def _resolve_singleton(self, view_kwargs: dict, action: str, include_fields: Optional[list[str]] = None):
        builder = self.builder
        model = builder.model
        session = builder.session

        builder._validate_view_args(view_kwargs)

        stmt = select(model)
        stmt = apply_scopes(stmt, model, builder.request_view_args, view_kwargs)
        stmt = apply_soft_delete_filter(stmt, model, builder.soft_delete)
        stmt = builder._apply_query_override(stmt, action, **view_kwargs)
        stmt = apply_eager_loads(stmt, model, action, builder.eager_loads)
        stmt = _apply_include_eager_loads(stmt, model, include_fields or [], builder, action)

        item = _first_row(session, stmt)
        return item

    def get(self, **kwargs):
        self.builder._apply_envelope()
        include_fields = parse_param_list(request.args.get("include"))
        exclude_fields = parse_param_list(request.args.get("exclude"))

        item = self._resolve_singleton(kwargs, "get_singleton", include_fields=include_fields)
        if item is None:
            abort(404, description=f"{self.builder.model.__name__} singleton not found")

        raw_serialized = self.builder.schema.dump(item)
        if include_fields or exclude_fields:
            return format_item_with_inclusions_and_exclusions(item, raw_serialized, include_fields, exclude_fields)
        return raw_serialized

    def post(self, **kwargs):
        # Create singleton or fail if already exists
        builder = self.builder
        session = builder.session

        builder._apply_envelope()
        values = builder._validate_view_args(kwargs) or getattr(g, "values", None) or ContextValues()
        existing = self._resolve_singleton(kwargs, "get_singleton")
        if existing is not None:
            return self.put(**kwargs)

        json_data = request.get_json(silent=True) or request.form.to_dict() or {}
        scoped_data = {**json_data}
        for url_key, model_attr in builder.request_view_args.items():
            if url_key in kwargs:
                scoped_data[model_attr] = kwargs[url_key]

        try:
            res = builder._invoke_hook(builder.before_create, scoped_data, values=values)
            if isinstance(res, dict):
                scoped_data = res
            item = builder.schema.load(scoped_data, session=session)
            session.add(item)
            session.commit()
            builder._invoke_hook(builder.after_create, item, data=scoped_data, values=values)
            return builder.schema.dump(item), 201
        except ValidationError as exc:
            session.rollback()
            abort(400, description=exc.messages)

    def put(self, **kwargs):
        return self._update_singleton(partial=False, **kwargs)

    def patch(self, **kwargs):
        return self._update_singleton(partial=True, **kwargs)

    def _update_singleton(self, partial: bool, **kwargs):
        builder = self.builder
        session = builder.session

        builder._apply_envelope()
        item = self._resolve_singleton(kwargs, "update_singleton")
        if item is None:
            abort(404, description=f"{builder.model.__name__} singleton not found")
        values = getattr(g, "values", None) or ContextValues()

        json_data = request.get_json(silent=True) or request.form.to_dict() or {}
        try:
            res = builder._invoke_hook(builder.before_update, item, json_data, values=values)
            data = res if isinstance(res, dict) else json_data
            updated = builder.schema.load(data, instance=item, partial=partial, session=session)
            session.commit()
            builder._invoke_hook(builder.after_update, updated, data=json_data, values=values)
            return builder.schema.dump(updated), 200
        except ValidationError as exc:
            session.rollback()
            abort(400, description=exc.messages)

    def delete(self, **kwargs):
        builder = self.builder
        session = builder.session

        item = self._resolve_singleton(kwargs, "delete_singleton")
        if item is None:
            abort(404, description=f"{builder.model.__name__} singleton not found")
        values = getattr(g, "values", None) or ContextValues()

        try:
            if builder.soft_delete and hasattr(item, "deleted_at"):
                item.deleted_at = datetime.now(timezone.utc)
            else:
                session.delete(item)
            session.commit()
            builder._invoke_hook(builder.after_delete, item, values=values)
            return "", 204
        except Exception:
            session.rollback()
            current_app.logger.exception("Error deleting resource in %s", builder.endpoint_name)
            raise
