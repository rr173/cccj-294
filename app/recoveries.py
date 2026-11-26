"""Compatibility alias for recovery/clawback callers."""

from __future__ import annotations

from .settlement import create_recovery, register_clawback, register_recovery

__all__ = ["register_recovery", "create_recovery", "register_clawback"]
