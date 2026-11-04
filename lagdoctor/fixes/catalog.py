"""The fix catalogue: every fix lag-doctor can apply, by id."""
from __future__ import annotations

from typing import Any, Callable, TypeVar

from lagdoctor.fixes.base import Fix

_REGISTRY: dict[str, type[Fix]] = {}
T = TypeVar("T", bound=type[Fix])


def register(cls: T) -> T:
    if not cls.id:
        raise ValueError(f"{cls.__name__} has no id")
    if cls.id in _REGISTRY and _REGISTRY[cls.id] is not cls:
        raise ValueError(f"duplicate fix id {cls.id!r}")
    _REGISTRY[cls.id] = cls
    return cls


def get(fix_id: str, **backends: Any) -> Fix:
    _load_builtin()
    try:
        return _REGISTRY[fix_id](**backends)
    except KeyError:
        raise KeyError(f"unknown fix {fix_id!r}; known: {sorted(_REGISTRY)}") from None


def ids() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


_loaders: list[Callable[[], None]] = []


def _load_builtin() -> None:
    """Import the built-in fix modules once, so their @register decorators run."""
    if _loaders:
        return
    import importlib
    import pkgutil

    import lagdoctor.fixes as pkg
    skip = {"base", "catalog", "journal", "registry"}
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name not in skip:
            importlib.import_module(f"lagdoctor.fixes.{mod.name}")
    _loaders.append(_load_builtin)
