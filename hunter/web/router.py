"""Tiny route registry — decorators over a flat table, no framework.

Permissions are stored inside each route table entry (never mutated onto
the handler function), so stacking multiple route decorators on one function
keeps every route's own permission spec intact.
"""
from __future__ import annotations

from collections.abc import Callable

from hunter.auth import Perm

_EXACT: dict[str, dict[str, tuple[Callable, Perm]]] = {"GET": {}, "POST": {}}
_PREFIX: dict[str, list[tuple[str, Callable, Perm]]] = {"GET": [], "POST": []}
_DEFAULT_ROLE = {"GET": "viewer", "POST": "operator"}


def _register(
    method: str,
    path: str,
    prefix: bool,
    role: str | None,
    audit: bool,
):
    perm = Perm(role or _DEFAULT_ROLE[method], audit and method == "POST")

    def deco(fn: Callable) -> Callable:
        if prefix:
            _PREFIX.setdefault(method, []).append((path, fn, perm))
            _PREFIX[method].sort(key=lambda p: -len(p[0]))
        else:
            _EXACT.setdefault(method, {})[path] = (fn, perm)
        return fn

    return deco


def get(
    path: str,
    prefix: bool = False,
    role: str | None = None,
    audit: bool = True,
):
    return _register("GET", path, prefix, role, audit)


def post(
    path: str,
    prefix: bool = False,
    role: str | None = None,
    audit: bool = True,
):
    return _register("POST", path, prefix, role, audit)


def resolve(method: str, path: str) -> tuple[Callable, Perm] | None:
    """Return ``(handler, perm)`` for *method* and *path*, or None."""
    exact = _EXACT.get(method, {}).get(path)
    if exact is not None:
        return exact
    for pre, fn, perm in _PREFIX.get(method, []):
        if path.startswith(pre):
            return fn, perm
    return None


def allowed_methods(path: str) -> list[str]:
    """Return sorted HTTP methods registered for *path*."""
    methods: set[str] = set()
    for method in set(_EXACT) | set(_PREFIX):
        if resolve(method, path) is not None:
            methods.add(method)
            if method == "GET":
                methods.add("HEAD")
    return sorted(methods)


def perm_of(
    fn: Callable, method: str | None = None, path: str | None = None
) -> Perm:
    if method and path:
        hit = resolve(method, path)
        if hit is not None:
            return hit[1]
    for method_map in _EXACT.values():
        for entry_fn, entry_perm in method_map.values():
            if entry_fn is fn:
                return entry_perm
    for plist in _PREFIX.values():
        for _pre, entry_fn, entry_perm in plist:
            if entry_fn is fn:
                return entry_perm
    return Perm()


def routes() -> list[tuple[str, str, str]]:
    out = [
        (m, p, perm.role)
        for m in _EXACT
        for p, (_fn, perm) in _EXACT[m].items()
    ]
    out += [
        (m, p + "*", perm.role)
        for m in _PREFIX
        for p, _fn, perm in _PREFIX[m]
    ]
    return sorted(out)


__all__ = [
    "allowed_methods",
    "get",
    "perm_of",
    "post",
    "resolve",
    "routes",
]
