"""Compatibility alias for prize configuration callers."""

from __future__ import annotations

from .settlement import set_prize_settings

configure_prizes = set_prize_settings

__all__ = ["set_prize_settings", "configure_prizes"]
