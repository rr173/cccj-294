"""ASGI entry point: ``uvicorn app.asgi:app``.

Set ``TOURNAMENT_DB`` to choose the SQLite file (default ``tournament.db``).
"""

from __future__ import annotations

import os

from .main import create_app

app = create_app(os.environ.get("TOURNAMENT_DB", "tournament.db"))
