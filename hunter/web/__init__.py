"""hunter's web layer.

    server.py    HTTP server, request context, static assets
    router.py    @get/@post registry
    common.py    owned-oracle cache, state helpers, pass spawning
    routes/      one module per domain — every endpoint lives there
    static/      the single-page app (ES modules, no build step)
"""
from hunter.web.server import start_ui  # noqa: F401
