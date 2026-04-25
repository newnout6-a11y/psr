"""Parser exports with lazy loading.

Avoid importing every platform parser at package import time: Kwork parsing now
has a shared platform service, and eager imports create circular dependencies.
"""

from __future__ import annotations

from typing import Any

from .base_parser import BaseParser


_EXPORTS = {
    "KworkParser": (".kwork_parser", "KworkParser"),
    "KworkAPIParser": (".kwork_api_parser", "KworkAPIParser"),
    "FreelanceRuParser": (".freelanceru_parser", "FreelanceRuParser"),
    "HHParser": (".hh_parser", "HHParser"),
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attr_name = _EXPORTS[name]
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


__all__ = [
    "BaseParser",
    "KworkParser",
    "KworkAPIParser",
    "FreelanceRuParser",
    "HHParser",
]
