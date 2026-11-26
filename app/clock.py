"""Central UTC clock.

Tests and reproducible examples can freeze the service clock by setting
``TOURNAMENT_NOW`` to an ISO-8601 UTC instant.  In normal operation the
environment variable is absent and the real UTC clock is used.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def utcnow() -> datetime:
    forced = os.environ.get("TOURNAMENT_NOW")
    if forced:
        value = forced.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="microseconds")
