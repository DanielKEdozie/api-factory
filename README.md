# API-Factory

A modular, extensible Flask REST API factory combining declarative endpoints, Flask `MethodView` architectures, automatic `SchemaBuilder` reflection, sliding-window rate limiting, declarative eager-loading, CSV exports, dual `int_or_str` converters, nested sub-resources, dynamic `?include=` / `?exclude=` relations, and `SingletonView` endpoints.

## Installation

```bash
pip install -e ./packages/api-factory
```

## Quick Start

```python
from flask import Flask, Blueprint
from flask_sqlalchemy import SQLAlchemy
from api_factory import ApiFactory

app = Flask(__name__)
db = SQLAlchemy(app)
factory = ApiFactory(app, db=db)

api_bp = Blueprint('api', __name__, url_prefix='/api')

# 1. Register a resource with explicit HTTP methods allowed
equipment_api = factory.create_resource(
    model=Equipment,
    blueprint=api_bp,
    url_prefix='/equipment',
    methods=['GET', 'POST', 'PUT', 'DELETE'],
    pk_name='id',
    pk_type='int_or_str',  # Matches /equipment/1 or /equipment/cat-320-excavator
    rate_limits={'collection': '30/minute', 'create': '5/minute'}
)

# 2. Register nested sub-resources (/equipment/<equipment_id>/specifications)
spec_api = equipment_api.sub_resource(
    model=Specification,
    sub_path='specifications',
    args_key='equipment_id',
    resolve_keys={'equipment_id': Equipment}
)

# Custom URL param name and resolve_value alias (/equipment/<e_id>/images):
image_api = equipment_api.sub_resource(
    model=EquipmentImage,
    sub_path='images',
    args_key={'e_id': 'equipment_id'},
    resolve_keys={'e_id': (Equipment, 'id')},
    resolve_value={'e_id': 'equipment'}  # Names object as value.equipment
)

# 3. Lifecycle Hooks with Context Values (item, data, value)
@image_api.hook('before_create')
def before_image_create(data, value):
    # Access parent record via custom alias, .parent_record, or dict access
    data['equipment_id'] = value.equipment.id # or value.parent_record.id
    data['uploaded_by'] = value.equipment.vendor_id
    return data

@image_api.hook('after_create')
def after_image_create(item, data, value):
    # Flexible signatures: (item), (item, value), or (item, data, value)
    log.info(f"Image {item.id} attached to equipment {value.equipment.name}")

# 4. Add custom routes
@equipment_api.route('/featured', methods=['GET'])
def featured():
    return {'featured': [...]}

app.register_blueprint(api_bp)
```

## Context Values & Lifecycle Hooks

Hooks support dynamic signatures and receive a `ContextValues` container `value` (also stored in `flask.g.values` and `flask.g.parent_record`):

- `before_create(data)` or `before_create(data, value)`
- `after_create(item)`, `after_create(item, value)`, or `after_create(item, data, value)`
- `before_update(item, data)` or `before_update(item, data, value)`
- `after_update(item)`, `after_update(item, value)`, or `after_update(item, data, value)`
- `after_delete(item)` or `after_delete(item, value)`

### `ContextValues` Access:
- **`value.parent_record`** / `value['parent_record']`: Resolved parent model instance.
- **`value.<alias>`** / `value['<alias>']`: Custom alias configured via `resolve_value={'e_id': 'equipment'}` (defaults automatically to the parent model's lowercase name, e.g. `value.equipment`).
- **`value.view_args`**: Dictionary of URL arguments extracted from the request route.


## Query Parameters Supported

- `?paginate=false`: Disable pagination and return the complete list of results.
- `?include=images,vendor` or `?include=["images","vendor"]`: Eagerly loads and bundles nested relationship data.
- `?exclude=description,secret_code`: Dynamically excludes fields from the JSON response.
- `?export=csv`: Streams a CSV download with custom or auto-selected fields.
- `?page=1&per_page=20`: Standard pagination.
- `?sort=price&order=desc`: Sorting.
- `?q=excavator`: Full-text search across configured `search_fields`.
- `?<field>=<val>`: Direct and typed filtering across configured `filter_fields`.

