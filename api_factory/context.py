from typing import Any, Dict


class ContextValues(dict):
    """Context container passed to lifecycle hooks, supporting both dict indexing and attribute access.
    
    Contains resolved parent records, custom named objects from `resolve_value`
    (e.g. `value.equipment`), `.parent_record`, and `.view_args`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            return None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(key, default)
