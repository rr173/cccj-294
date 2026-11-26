"""Compatibility alias for callers using finance vocabulary."""

from __future__ import annotations

from .settlement import *  # noqa: F401,F403
from . import settlement as _settlement

get_settlement = _settlement.get_settlement
set_prize_settings = _settlement.set_prize_settings
add_funds = _settlement.add_funds
create_payment = _settlement.create_payment
confirm_payment = _settlement.confirm_payment
register_recovery = _settlement.register_recovery

__all__ = [
    "get_settlement",
    "set_prize_settings",
    "add_funds",
    "create_payment",
    "confirm_payment",
    "register_recovery",
]
