"""Resolve declared property aliases without changing serialized input values."""
from __future__ import annotations

from typing import Any

from .policies import PolicyViolation


class RecordView(dict):
    def __init__(self, values: dict, entity: Any) -> None:
        super().__init__(values)
        self._aliases: dict[str, str] = {}
        for prop in entity.properties:
            name, api_name = str(prop.name), str(prop.api_name or prop.name)
            supplied = [key for key in {name, api_name} if dict.__contains__(self, key)]
            if not supplied:
                continue
            if len(supplied) == 2 and dict.__getitem__(self, name) != dict.__getitem__(self, api_name):
                raise PolicyViolation(f'本体输入属性“{name}”的名称与 API 名取值冲突')
            target = api_name if api_name in supplied else name
            for alias in {name, api_name}:
                if alias in self._aliases and self._aliases[alias] != target:
                    raise PolicyViolation('本体输入属性别名存在冲突')
                self._aliases[alias] = target

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key) or key in self._aliases

    def __getitem__(self, key: str) -> Any:
        return dict.__getitem__(self, self._aliases.get(key, key))

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self else default

    def _readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError('ontology record views are read only')

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly
