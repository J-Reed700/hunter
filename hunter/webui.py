"""Compatibility shim — the web layer moved to `hunter.web`.

    hunter/web/server.py   HTTP server + request context + static assets
    hunter/web/routes/*    every endpoint, one module per domain
    hunter/web/static/*    the single-page app

`start_ui` keeps its old import path so scheduler.py needs no change.
"""
from hunter.web.server import start_ui  # noqa: F401
