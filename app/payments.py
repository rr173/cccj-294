"""Compatibility alias for payment-order service callers."""

from __future__ import annotations

from .settlement import *  # noqa: F401,F403
from . import settlement as _settlement

create_payment = _settlement.create_payment
create_payment_order = _settlement.create_payment_order
issue_payment = _settlement.issue_payment
confirm_payment = _settlement.confirm_payment
get_payment = _settlement.get_payment

__all__ = [
    "create_payment",
    "create_payment_order",
    "issue_payment",
    "confirm_payment",
    "get_payment",
]
