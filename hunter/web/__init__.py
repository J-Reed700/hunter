"""hunter's web layer.

    server.py    HTTP server, request context, static assets
    router.py    @get/@post registry
    common.py    owned-oracle cache, state helpers, pass spawning
    routes/      one module per domain — every endpoint lives there
    static/      the single-page app (ES modules, no build step)
"""
from __future__ import annotations


def __getattr__(name: str):
    if name == "start_ui":
        from hunter.web.server import start_ui

        return start_ui
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["start_ui"]
