"""Importing this package registers every route with the router."""
from hunter.web.routes import (  # noqa: F401
    auth, automation, artists, beets, core, discover, downloads, jobs, library,
    recovery, spotify, wants,
)
