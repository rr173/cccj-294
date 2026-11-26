"""Prize configuration, payment commitments, bank results and recoveries.

The settlement module is deliberately snapshot based:

* a payment order stores the recipient, amount and effective final result
  version finance saw when it was issued;
* readjudication changes the *current award view*, but never the order;
* pending orders remain encumbrances because a bank may still confirm them;
* only a successful bank result moves real cash, and only an actual recovery
  puts recovered cash back into this tournament's available balance.

All write entry points use ``BEGIN IMMEDIATE`` through :func:`service.write_tx`,
so issue/recover/confirm/appeal operations cannot interleave their
read-check-write critical sections.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Sequence

from . import service
from .db import read_tx, write_tx

# Statuses are short and wire-facing.  The richer aliases below let clients use
# "succeeded"/"paid" vocabulary while keeping one canonical stored value.
PENDING = "pending"
SUCCESS = "success"
FAILED = "failed"

SETTLED = "decided"
SUSPENDED = "suspended"
AWAITING_FINAL = "awaiting_final"


# ---------------------------------------------------------------------------
# Request parsing helpers


def _error(status: int, code: str, message: str, **extra: Any) -> service.TournamentError:
    return service.TournamentError(status, code, message, **extra)


def _first(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _as_int_id(value: Any, field: str = "id") -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _error(422, "invalid_id", f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return _as_int_id(
            _first(
                value,
                "id",
                "result_version_id",
                "final_result_version_id",
                "player_id",
                "tournament_id",
            ),
            field,
        )
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise _error(422, "invalid_id", f"{field} must be an integer") from exc


def parse_cents(value: Any, field: str, *, minimum: int = 1) -> int:
    """Parse an integer minor-unit amount (cents/fen)."""
    if value is None:
        raise _error(422, f"{field}_required", f"{field} is required in integer cents")
    if isinstance(value, bool):
        raise _error(422, "invalid_amount", f"{field} must be an integer number of cents")
    if isinstance(value, dict):
        inner = _first(
            value,
            "amount_cents",
            "amount_minor",
            "value_cents",
            "cents",
            "fen",
            "amount",
        )
        return parse_cents(inner, field)

    amount: int
    if isinstance(value, int):
        amount = value
    elif isinstance(value, float) and float(value).is_integer():
        amount = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            # Integer cents are the contract.  Accept "10000" but not "100.50".
            amount = int(text)
        except ValueError as exc:
            raise _error(
                422,
                "invalid_amount",
                f"{field} must be an integer number of minor units",
                value=value,
            ) from exc
    else:
        raise _error(
            422,
            "invalid_amount",
            f"{field} must be an integer number of minor units",
            value=value,
        )

    if amount < minimum:
        raise _error(
            422,
            "invalid_amount",
            f"{field} must be >= {minimum} integer cents",
            amount_cents=amount,
            minimum_cents=minimum,
        )
    return amount


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _money(body: dict[str, Any], *keys: str, minimum: int = 1) -> int | None:
    aliases = list(keys)
    for key in keys:
        if key.endswith("_cents"):
            aliases.append(f"{key[: -len('_cents')]}_fen")
    value = _first(body, *aliases)
    if value is None:
        return None
    return parse_cents(value, keys[0], minimum=minimum)


def with_fen_aliases(value: Any) -> Any:
    """Expose every ``*_cents`` field under the equivalent ``*_fen`` name.

    The domain contract is integer minor units.  ``cents`` and ``fen`` are both
    common names for that unit, so output includes both without duplicating
    calculation logic.
    """
    if isinstance(value, dict):
        for key, item in list(value.items()):
            value[key] = with_fen_aliases(item)
            if isinstance(key, str) and key.endswith("_cents"):
                fen_key = f"{key[: -len('_cents')]}_fen"
                value.setdefault(fen_key, value[key])
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = with_fen_aliases(item)
    return value


def _idempotency_key(conn_unused: Any, header_key: str | None, body: dict[str, Any] | None) -> str | None:
    raw = header_key or _first(body or {}, "idempotency_key", "idempotencyKey")
    text = _clean_text(raw)
    return text


# ---------------------------------------------------------------------------
# Current final / settlement state


def get_final_context(
    conn: sqlite3.Connection, tournament_id: int
) -> dict[str, Any]:
    """Return the current final state and whether money writes may open.

    ``decided`` means the existing bracket rules currently permit a champion
    declaration.  ``suspended`` means a pending appeal or a superseded final
    blocks new payments/recoveries.  ``awaiting_final`` is the ordinary
    not-yet-finished state.
    """
    final = service._current_match(conn, tournament_id, "F")
    latest = service._latest_version(conn, final["id"]) if final is not None else None
    pending = service._pending_appeals(conn, tournament_id)
    invalidated_rows = list(
        conn.execute(
            """
            SELECT id FROM matches
            WHERE tournament_id = ? AND slot_id = 'F' AND is_current = 0
            ORDER BY id
            """,
            (tournament_id,),
        ).fetchall()
    )
    invalidated_ids = [int(r["id"]) for r in invalidated_rows]
    pending_ids = [int(a["id"]) for a in pending]

    context: dict[str, Any] = {
        "state": AWAITING_FINAL,
        "settlement_status": AWAITING_FINAL,
        "paused": False,
        "payments_open": False,
        "recoveries_open": False,
        "can_create_payment": False,
        "can_register_recovery": False,
        "reason": "final_not_ready",
        "blocking_appeal_ids": pending_ids,
        "pending_appeal_ids": pending_ids,
        "final_match_id": int(final["id"]) if final is not None else None,
        "match_id": int(final["id"]) if final is not None else None,
        "result_version_id": int(latest["id"]) if latest is not None else None,
        "final_result_version_id": int(latest["id"]) if latest is not None else None,
        "result_version": int(latest["version"]) if latest is not None else None,
        "winner_player_id": int(latest["winner_player_id"]) if latest is not None else None,
        "loser_player_id": int(latest["loser_player_id"]) if latest is not None else None,
        "champion_player_id": int(latest["winner_player_id"]) if latest is not None else None,
        "runner_up_player_id": int(latest["loser_player_id"]) if latest is not None else None,
        "invalidated_final_match_ids": invalidated_ids,
    }

    if pending:
        context.update(
            {
                "state": SUSPENDED,
                "settlement_status": SUSPENDED,
                "paused": True,
                "reason": "pending_appeal_blocks_champion",
            }
        )
        context["suspension"] = service._suspension_view(pending)
    elif final is None and invalidated_ids:
        context.update(
            {
                "state": SUSPENDED,
                "settlement_status": SUSPENDED,
                "paused": True,
                "reason": "final_superseded",
            }
        )
        context["suspension"] = {
            "suspended": True,
            "reason": "final_superseded",
            "invalidated_final_match_ids": invalidated_ids,
            "appeal_ids": [],
        }
    elif final is None:
        context["reason"] = "final_not_started"
        context["suspension"] = {"suspended": False, "reasons": [], "appeal_ids": []}
    elif final["status"] != "completed":
        context["reason"] = "final_in_progress"
        context["suspension"] = {"suspended": False, "reasons": [], "appeal_ids": []}
    elif latest is None:
        context["reason"] = "final_result_missing"
        context["suspension"] = {"suspended": False, "reasons": [], "appeal_ids": []}
    else:
        context.update(
            {
                "state": SETTLED,
                "settlement_status": SETTLED,
                "reason": None,
                "payments_open": True,
                "recoveries_open": True,
                "can_create_payment": True,
                "can_register_recovery": True,
            }
        )
        context["suspension"] = {"suspended": False, "reasons": [], "appeal_ids": []}

    return context


def _require_decided_context(
    conn: sqlite3.Connection,
    tournament_id: int,
    expected_result_version_id: int | None = None,
) -> dict[str, Any]:
    context = get_final_context(conn, tournament_id)
    if context["state"] == SUSPENDED:
        raise _error(
            409,
            "settlement_suspended",
            "New payment orders and recoveries are paused while the final is "
            "invalid or an appeal blocks champion declaration; existing "
            "payment orders can still be confirmed",
            settlement=context,
            suspension=context.get("suspension"),
            blocking_appeal_ids=context["blocking_appeal_ids"],
        )
    if context["state"] != SETTLED:
        raise _error(
            409,
            "no_effective_final",
            "A champion and runner-up are not currently determined by an "
            "effective completed final",
            settlement=context,
        )
    current_version = context["result_version_id"]
    if (
        expected_result_version_id is not None
        and expected_result_version_id != current_version
    ):
        raise _error(
            409,
            "result_version_changed",
            "The effective final result version differs from the version finance saw",
            current_result_version_id=current_version,
            expected_result_version_id=expected_result_version_id,
        )
    return context


# ---------------------------------------------------------------------------
# Views and balances


def _prize_settings_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    champion = int(row["champion_amount_cents"])
    runner = int(row["runner_up_amount_cents"])
    return {
        "tournament_id": int(row["tournament_id"]),
        "champion_amount_cents": champion,
        "runner_up_amount_cents": runner,
        "champion_amount": champion,
        "runner_up_amount": runner,
        "amount_unit": "integer_minor_units",
        "currency_minor_unit": "cents",
        "set_by": row["set_by"],
        "created_at": row["created_at"],
        # Convenience aliases used by different finance clients.
        "champion_prize_cents": champion,
        "runner_up_prize_cents": runner,
        "champion_prize_amount_cents": champion,
        "runner_up_prize_amount_cents": runner,
    }


def _raw_balances(
    conn: sqlite3.Connection, tournament_id: int
) -> tuple[dict[str, int], dict[int, dict[str, int]]]:
    totals: dict[str, int] = defaultdict(int)
    for key in (
        "funding_cents",
        "payment_total_cents",
        "pending_cents",
        "paid_cents",
        "success_cents",
        "failed_cents",
        "recovered_cents",
    ):
        totals[key] = 0
    player_totals: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "paid_cents": 0,
            "success_cents": 0,
            "pending_cents": 0,
            "failed_cents": 0,
            "recovered_cents": 0,
            "payment_count": 0,
            "pending_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "recovery_count": 0,
        }
    )

    totals["funding_cents"] = int(
        conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s "
            "FROM funding_additions WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()["s"]
    )

    for row in conn.execute(
        """
        SELECT player_id, status, amount_cents
        FROM payment_orders WHERE tournament_id = ?
        ORDER BY id
        """,
        (tournament_id,),
    ).fetchall():
        amount = int(row["amount_cents"])
        pid = int(row["player_id"])
        b = player_totals[pid]
        b["payment_count"] += 1
        totals["payment_total_cents"] += amount
        if row["status"] == PENDING:
            b["pending_cents"] += amount
            b["pending_count"] += 1
            totals["pending_cents"] += amount
        elif row["status"] == SUCCESS:
            b["paid_cents"] += amount
            b["success_cents"] += amount
            b["success_count"] += 1
            totals["paid_cents"] += amount
            totals["success_cents"] += amount
        else:
            b["failed_cents"] += amount
            b["failed_count"] += 1
            totals["failed_cents"] += amount

    totals["recovered_cents"] = int(
        conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s "
            "FROM payment_recoveries WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()["s"]
    )
    for row in conn.execute(
        """
        SELECT player_id, amount_cents
        FROM payment_recoveries WHERE tournament_id = ?
        """,
        (tournament_id,),
    ).fetchall():
        pid = int(row["player_id"])
        amount = int(row["amount_cents"])
        player_totals[pid]["recovered_cents"] += amount
        player_totals[pid]["recovery_count"] += 1

    totals["cash_balance_cents"] = (
        totals["funding_cents"]
        + totals["recovered_cents"]
        - totals["paid_cents"]
    )
    totals["available_funds_cents"] = (
        totals["cash_balance_cents"] - totals["pending_cents"]
    )
    totals["committed_unconfirmed_cents"] = totals["pending_cents"]
    totals["total_commitments_cents"] = totals["paid_cents"] + totals["pending_cents"]
    return dict(totals), {pid: dict(v) for pid, v in player_totals.items()}


def _payment_view(
    row: sqlite3.Row, players: dict[int, sqlite3.Row]
) -> dict[str, Any]:
    amount = int(row["amount_cents"])
    pid = int(row["player_id"])
    player = players.get(pid)
    player_ref = service._player_view(player) if player is not None else {"id": pid}
    status = row["status"]
    final = status != PENDING
    successful = status == SUCCESS
    failed = status == FAILED
    payment_id = int(row["id"])
    tournament_id = int(row["tournament_id"])
    receipt = row["bank_receipt_number"]
    return {
        "id": payment_id,
        "payment_id": payment_id,
        "order_id": payment_id,
        "payment_no": f"PAY-{tournament_id}-{payment_id:06d}",
        "payment_number": str(payment_id),
        "order_number": payment_id,
        "tournament_id": tournament_id,
        "player_id": pid,
        "recipient_player_id": pid,
        "payee_player_id": pid,
        "recipient_id": pid,
        "payee_id": pid,
        "player": player_ref,
        "recipient": player_ref,
        "amount_cents": amount,
        "amount": amount,
        "currency": "minor_units",
        "amount_unit": "cents",
        "final_match_id": int(row["final_match_id"]),
        "match_id": int(row["final_match_id"]),
        "result_version_id": int(row["result_version_id"]),
        "final_result_version_id": int(row["result_version_id"]),
        "snapshot_result_version_id": int(row["result_version_id"]),
        "award_type": row["award_type"],
        "prize_type": row["award_type"],
        "role": row["award_type"],
        "award_amount_snapshot_cents": int(row["award_amount_snapshot_cents"]),
        "snapshot_prize_amount_cents": int(row["award_amount_snapshot_cents"]),
        "status": status,
        "state": status,
        "payment_status": "succeeded" if successful else status,
        "bank_status": SUCCESS if successful else (FAILED if failed else PENDING),
        "outcome": status if final else None,
        "result": status if final else None,
        "successful": successful,
        "succeeded": successful,
        "paid": successful,
        "is_paid": successful,
        "failed": failed,
        "is_failed": failed,
        "pending": status == PENDING,
        "is_pending": status == PENDING,
        "final": final,
        "is_final": final,
        "confirmed": final,
        "bank_receipt_number": receipt,
        "receipt_number": receipt,
        "bank_receipt": receipt,
        "receipt": receipt,
        "bank_reference": row["bank_reference"],
        "confirmed_by": row["confirmed_by"],
        "confirmation_note": row["confirmation_note"],
        "failure_reason": row["failure_reason"],
        "created_by": row["created_by"],
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
    }


def _recovery_view(
    row: sqlite3.Row, players: dict[int, sqlite3.Row]
) -> dict[str, Any]:
    pid = int(row["player_id"])
    player = players.get(pid)
    ref = service._player_view(player) if player is not None else {"id": pid}
    amount = int(row["amount_cents"])
    recovery_id = int(row["id"])
    return {
        "id": recovery_id,
        "recovery_id": recovery_id,
        "clawback_id": recovery_id,
        "tournament_id": int(row["tournament_id"]),
        "player_id": pid,
        "player": ref,
        "amount_cents": amount,
        "amount": amount,
        "final_match_id": int(row["final_match_id"]),
        "result_version_id": int(row["result_version_id"]),
        "final_result_version_id": int(row["result_version_id"]),
        "current_award_amount_snapshot_cents": int(
            row["current_award_amount_snapshot_cents"]
        ),
        "award_amount_snapshot_cents": int(
            row["current_award_amount_snapshot_cents"]
        ),
        "received_by": row["received_by"],
        "note": row["note"],
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        # Aliases for clients using clawback vocabulary.
        "clawback": True,
        "actual_recovery": True,
        "status": "received",
    }


def _funding_view(row: sqlite3.Row) -> dict[str, Any]:
    amount = int(row["amount_cents"])
    return {
        "id": int(row["id"]),
        "funding_id": int(row["id"]),
        "top_up_id": int(row["id"]),
        "tournament_id": int(row["tournament_id"]),
        "amount_cents": amount,
        "amount": amount,
        "source": row["source"],
        "note": row["note"],
        "funded_by": row["funded_by"],
        "received_by": row["funded_by"],
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        "type": "funding_added",
    }


def _player_financial_views(
    conn: sqlite3.Connection,
    tournament_id: int,
    players: dict[int, sqlite3.Row],
    context: dict[str, Any],
    prize: sqlite3.Row | None,
    balances: dict[int, dict[str, int]],
) -> list[dict[str, Any]]:
    champion_id = context.get("winner_player_id") if context["state"] == SETTLED else None
    runner_id = context.get("loser_player_id") if context["state"] == SETTLED else None

    rows: list[dict[str, Any]] = []
    for pid, player in players.items():
        b = balances.get(
            pid,
            {
                "paid_cents": 0,
                "success_cents": 0,
                "pending_cents": 0,
                "failed_cents": 0,
                "recovered_cents": 0,
                "payment_count": 0,
                "pending_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "recovery_count": 0,
            },
        )
        paid = int(b["paid_cents"])
        recovered = int(b["recovered_cents"])
        net = paid - recovered
        pending_amount = int(b["pending_cents"])

        role: str | None = None
        prize_amount: int | None
        recoverable: int | None
        capacity_raw: int | None
        if context["state"] == SETTLED and prize is not None:
            if pid == champion_id:
                role = "champion"
                prize_amount = int(prize["champion_amount_cents"])
            elif pid == runner_id:
                role = "runner_up"
                prize_amount = int(prize["runner_up_amount_cents"])
            else:
                role = None
                prize_amount = 0
            recoverable = max(0, net - prize_amount)
            capacity_raw = prize_amount - net - pending_amount
        elif context["state"] == SUSPENDED:
            # Explicitly not judged during an appeal/final invalidation.
            prize_amount = None
            recoverable = None
            capacity_raw = None
        else:
            # Before an effective final, nobody has a current award.  There
            # cannot legally be payments/recoveries in this state.
            prize_amount = 0
            recoverable = 0
            capacity_raw = 0

        rows.append(
            {
                "player_id": pid,
                "id": pid,
                **service._player_view(player),
                "role": role,
                "award_type": role,
                "current_award": role,
                "current_prize_amount_cents": prize_amount,
                "current_prize_cents": prize_amount,
                "prize_amount_cents": prize_amount,
                "prize_cents": prize_amount,
                "award_amount_cents": prize_amount,
                "award_cents": prize_amount,
                "current_award_amount_cents": prize_amount,
                "paid_cents": paid,
                "confirmed_paid_cents": paid,
                "successful_payment_cents": paid,
                "success_cents": paid,
                "recovered_cents": recovered,
                "actual_recovered_cents": recovered,
                "recovery_cents": recovered,
                "net_paid_cents": net,
                "net_paid_amount_cents": net,
                "net_payment_cents": net,
                "unconfirmed_payment_cents": pending_amount,
                "unconfirmed_payments_cents": pending_amount,
                "unconfirmed_amount_cents": pending_amount,
                "pending_amount_cents": pending_amount,
                "pending_payment_cents": pending_amount,
                "pending_payments_cents": pending_amount,
                "pending_commitment_cents": pending_amount,
                "committed_but_unconfirmed_cents": pending_amount,
                "outgoing_pending_cents": pending_amount,
                "recoverable_amount_cents": recoverable,
                "recoverable_cents": recoverable,
                "amount_recoverable_cents": recoverable,
                "pending_recovery_cents": recoverable,
                "pending_recovery_amount_cents": recoverable,
                "receivable_cents": recoverable,
                "receivable_amount_cents": recoverable,
                "overpaid_amount_cents": recoverable,
                "outstanding_recovery_cents": recoverable,
                "outstanding_recovery_amount_cents": recoverable,
                "outstanding_clawback_cents": recoverable,
                "clawback_due_cents": recoverable,
                "remaining_award_balance_cents": capacity_raw,
                "award_balance_after_commitments_cents": capacity_raw,
                "issuable_balance_cents": max(0, capacity_raw) if capacity_raw is not None else None,
                "available_to_promise_cents": max(0, capacity_raw) if capacity_raw is not None else None,
                "payment_count": b["payment_count"],
                "pending_count": b["pending_count"],
                "success_count": b["success_count"],
                "failed_count": b["failed_count"],
                "recovery_count": b["recovery_count"],
            }
        )

    # Put awardees first, then seed order.
    rows.sort(
        key=lambda r: (
            0 if r["role"] == "champion" else 1 if r["role"] == "runner_up" else 2,
            r["seed"],
        )
    )
    return rows


def _award_history(
    conn: sqlite3.Connection,
    tournament_id: int,
    players: dict[int, sqlite3.Row],
    prize: sqlite3.Row | None,
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    version_rows = list(
        conn.execute(
            """
            SELECT rv.*, m.attempt, m.is_current AS match_is_current
            FROM result_versions rv
            JOIN matches m ON m.id = rv.match_id
            WHERE m.tournament_id = ? AND m.slot_id = 'F'
            ORDER BY rv.id
            """,
            (tournament_id,),
        ).fetchall()
    )

    history: list[dict[str, Any]] = []
    entitlement_maps: list[dict[int, int]] = []
    for sequence, rv in enumerate(version_rows):
        winner = int(rv["winner_player_id"])
        loser = int(rv["loser_player_id"])
        entitlements: dict[int, int] = {}
        if prize is not None:
            entitlements[winner] = int(prize["champion_amount_cents"])
            entitlements[loser] = int(prize["runner_up_amount_cents"])
        entitlement_maps.append(entitlements)
        is_effective = (
            context["state"] == SETTLED
            and int(rv["id"]) == context["result_version_id"]
        )
        history.append(
            {
                "sequence": sequence,
                "result_version_id": int(rv["id"]),
                "version": int(rv["version"]),
                "final_result_version_id": int(rv["id"]),
                "match_id": int(rv["match_id"]),
                "final_match_id": int(rv["match_id"]),
                "attempt": int(rv["attempt"]),
                "match_is_current": bool(rv["match_is_current"]),
                "is_effective_for_awards": is_effective,
                "winner_player_id": winner,
                "loser_player_id": loser,
                "champion_player_id": winner,
                "runner_up_player_id": loser,
                "winner": service._player_ref(players, winner),
                "loser": service._player_ref(players, loser),
                "champion": service._player_ref(players, winner),
                "runner_up": service._player_ref(players, loser),
                "source": rv["source"],
                "created_at": rv["created_at"],
                "adjudication_id": rv["adjudication_id"],
                "entitlements_cents": {str(pid): amount for pid, amount in entitlements.items()},
            }
        )

    diffs: list[dict[str, Any]] = []
    for index in range(1, len(history)):
        old_h = history[index - 1]
        new_h = history[index]
        old_map = entitlement_maps[index - 1]
        new_map = entitlement_maps[index]
        changed_players = sorted(set(old_map) | set(new_map))
        changes = []
        for pid in changed_players:
            old_amount = old_map.get(pid)
            new_amount = new_map.get(pid)
            delta = (new_amount or 0) - (old_amount or 0)
            if old_amount == new_amount and old_amount is not None:
                continue
            old_role = (
                "champion"
                if pid == old_h["winner_player_id"]
                else "runner_up"
                if pid == old_h["loser_player_id"]
                else None
            )
            new_role = (
                "champion"
                if pid == new_h["winner_player_id"]
                else "runner_up"
                if pid == new_h["loser_player_id"]
                else None
            )
            changes.append(
                {
                    "player_id": pid,
                    "player": service._player_ref(players, pid),
                    "old_role": old_role,
                    "new_role": new_role,
                    "old_amount_cents": old_amount,
                    "new_amount_cents": new_amount,
                    "delta_cents": delta if prize is not None else None,
                    "difference_cents": delta if prize is not None else None,
                    "kind": "readjudication_difference",
                    "is_real_cash_movement": False,
                }
            )
        same_match = old_h["match_id"] == new_h["match_id"]
        diffs.append(
            {
                "from_result_version_id": old_h["result_version_id"],
                "to_result_version_id": new_h["result_version_id"],
                "same_final_match": same_match,
                "reason": (
                    "final_readjudicated"
                    if same_match
                    else "final_replayed_or_replaced"
                ),
                "is_cash_movement": False,
                "changes": changes,
            }
        )

    return history, diffs


def _fund_movements(
    funding_rows: Sequence[sqlite3.Row],
    payment_rows: Sequence[sqlite3.Row],
    recovery_rows: Sequence[sqlite3.Row],
    players: dict[int, sqlite3.Row],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for row in funding_rows:
        amount = int(row["amount_cents"])
        events.append(
            {
                "movement_key": f"funding-{row['id']}",
                "movement_type": "funding_received",
                "type": "funding_added",
                "kind": "funding",
                "is_actual_cash_movement": True,
                "cash_movement": True,
                "tournament_id": int(row["tournament_id"]),
                "funding_id": int(row["id"]),
                "amount_cents": amount,
                "cash_delta_cents": amount,
                "signed_amount_cents": amount,
                "pending_commitment_delta_cents": 0,
                "available_funds_delta_cents": amount,
                "occurred_at": row["created_at"],
                "created_at": row["created_at"],
                "source": row["source"],
                "note": row["note"],
                "staff": row["funded_by"],
            }
        )

    for row in payment_rows:
        amount = int(row["amount_cents"])
        pid = int(row["player_id"])
        ref = service._player_ref(players, pid) if pid in players else {"id": pid}
        common = {
            "tournament_id": int(row["tournament_id"]),
            "payment_id": int(row["id"]),
            "player_id": pid,
            "player": ref,
            "amount_cents": amount,
            "result_version_id": int(row["result_version_id"]),
            "bank_receipt_number": row["bank_receipt_number"],
        }
        events.append(
            {
                **common,
                "movement_key": f"payment-{row['id']}-committed",
                "movement_type": "payment_committed",
                "type": "payment_committed",
                "kind": "commitment",
                "is_actual_cash_movement": False,
                "cash_movement": False,
                "cash_delta_cents": 0,
                "signed_amount_cents": 0,
                "pending_commitment_delta_cents": amount,
                "available_funds_delta_cents": -amount,
                "occurred_at": row["created_at"],
                "created_at": row["created_at"],
            }
        )
        if row["status"] != PENDING and row["confirmed_at"]:
            successful = row["status"] == SUCCESS
            events.append(
                {
                    **common,
                    "movement_key": f"payment-{row['id']}-{row['status']}",
                    "movement_type": (
                        "payment_succeeded" if successful else "payment_failed"
                    ),
                    "type": "payment_succeeded" if successful else "payment_failed",
                    "kind": "payment" if successful else "commitment_released",
                    "is_actual_cash_movement": successful,
                    "cash_movement": successful,
                    "cash_delta_cents": -amount if successful else 0,
                    "signed_amount_cents": -amount if successful else 0,
                    # The commitment was included in pending until the final
                    # bank result.  Success converts it to cash paid; failure
                    # releases it back to available funds.
                    "pending_commitment_delta_cents": -amount,
                    "available_funds_delta_cents": 0 if successful else amount,
                    "occurred_at": row["confirmed_at"],
                    "created_at": row["confirmed_at"],
                    "failure_reason": row["failure_reason"],
                    "confirmed_by": row["confirmed_by"],
                }
            )

    for row in recovery_rows:
        amount = int(row["amount_cents"])
        pid = int(row["player_id"])
        ref = service._player_ref(players, pid) if pid in players else {"id": pid}
        events.append(
            {
                "movement_key": f"recovery-{row['id']}",
                "movement_type": "recovery_received",
                "type": "recovery_received",
                "kind": "recovery",
                "is_actual_cash_movement": True,
                "cash_movement": True,
                "tournament_id": int(row["tournament_id"]),
                "recovery_id": int(row["id"]),
                "clawback_id": int(row["id"]),
                "player_id": pid,
                "player": ref,
                "amount_cents": amount,
                "cash_delta_cents": amount,
                "signed_amount_cents": amount,
                "pending_commitment_delta_cents": 0,
                "available_funds_delta_cents": amount,
                "result_version_id": int(row["result_version_id"]),
                "occurred_at": row["created_at"],
                "created_at": row["created_at"],
                "note": row["note"],
                "staff": row["received_by"],
            }
        )

    events.sort(key=lambda e: (e["occurred_at"], e["movement_key"]))

    cash = 0
    pending = 0
    for event in events:
        cash += int(event["cash_delta_cents"])
        pending += int(event["pending_commitment_delta_cents"])
        event["running_cash_balance_cents"] = cash
        event["running_pending_commitment_cents"] = pending
        event["running_available_funds_cents"] = cash - pending
    return events


def build_settlement_view(
    conn: sqlite3.Connection, tournament_id: int
) -> dict[str, Any]:
    service._require_tournament(conn, tournament_id)
    players = service._players_by_id(conn, tournament_id)
    context = get_final_context(conn, tournament_id)

    prize_row = conn.execute(
        "SELECT * FROM prize_settings WHERE tournament_id = ?", (tournament_id,)
    ).fetchone()
    prize = _prize_settings_view(prize_row)
    totals, player_balances = _raw_balances(conn, tournament_id)

    payment_rows = list(
        conn.execute(
            "SELECT * FROM payment_orders WHERE tournament_id = ? ORDER BY id",
            (tournament_id,),
        ).fetchall()
    )
    recovery_rows = list(
        conn.execute(
            "SELECT * FROM payment_recoveries WHERE tournament_id = ? ORDER BY id",
            (tournament_id,),
        ).fetchall()
    )
    funding_rows = list(
        conn.execute(
            "SELECT * FROM funding_additions WHERE tournament_id = ? ORDER BY id",
            (tournament_id,),
        ).fetchall()
    )

    payments = [_payment_view(r, players) for r in payment_rows]
    recoveries = [_recovery_view(r, players) for r in recovery_rows]
    funding = [_funding_view(r) for r in funding_rows]
    player_rows = _player_financial_views(
        conn, tournament_id, players, context, prize_row, player_balances
    )
    award_versions, award_diffs = _award_history(
        conn, tournament_id, players, prize_row, context
    )
    movements = _fund_movements(
        funding_rows, payment_rows, recovery_rows, players
    )
    cash_ledger = [e for e in movements if e["is_actual_cash_movement"]]

    current_final = None
    if context["final_match_id"] is not None:
        match = service._match_by_id(conn, context["final_match_id"])
        if match is not None:
            latest = service._latest_version(conn, match["id"])
            current_final = {
                "match_id": int(match["id"]),
                "id": int(match["id"]),
                "slot_id": "F",
                "attempt": int(match["attempt"]),
                "status": match["status"],
                "is_current": bool(match["is_current"]),
                "result_version_id": int(latest["id"]) if latest else None,
                "version": int(latest["version"]) if latest else None,
                "winner": (
                    service._player_ref(players, latest["winner_player_id"])
                    if latest
                    else None
                ),
                "loser": (
                    service._player_ref(players, latest["loser_player_id"])
                    if latest
                    else None
                ),
                "source": latest["source"] if latest else None,
                "created_at": latest["created_at"] if latest else None,
            }

    pool = {
        "funding_total_cents": totals["funding_cents"],
        "total_funds_added_cents": totals["funding_cents"],
        "added_funds_cents": totals["funding_cents"],
        "recovered_total_cents": totals["recovered_cents"],
        "total_recovered_cents": totals["recovered_cents"],
        "paid_total_cents": totals["paid_cents"],
        "successful_payment_total_cents": totals["paid_cents"],
        "pending_payment_total_cents": totals["pending_cents"],
        "unconfirmed_payment_total_cents": totals["pending_cents"],
        "committed_unconfirmed_cents": totals["pending_cents"],
        "failed_payment_total_cents": totals["failed_cents"],
        "total_commitments_cents": totals["total_commitments_cents"],
        "cash_balance_cents": totals["cash_balance_cents"],
        "cash_on_hand_cents": totals["cash_balance_cents"],
        "available_funds_cents": totals["available_funds_cents"],
        "available_balance_cents": totals["available_funds_cents"],
        "funds_available_cents": totals["available_funds_cents"],
        "spendable_funds_cents": totals["available_funds_cents"],
        "available_prize_funds_cents": totals["available_funds_cents"],
        "currency": "minor_units",
        "amount_unit": "cents",
    }

    awards = None
    if context["state"] == SETTLED and prize_row is not None:
        awards = [
            {
                "role": "champion",
                "award_type": "champion",
                "player_id": context["winner_player_id"],
                "player": service._player_ref(players, context["winner_player_id"]),
                "result_version_id": context["result_version_id"],
                "amount_cents": int(prize_row["champion_amount_cents"]),
                "prize_amount_cents": int(prize_row["champion_amount_cents"]),
            },
            {
                "role": "runner_up",
                "award_type": "runner_up",
                "player_id": context["loser_player_id"],
                "player": service._player_ref(players, context["loser_player_id"]),
                "result_version_id": context["result_version_id"],
                "amount_cents": int(prize_row["runner_up_amount_cents"]),
                "prize_amount_cents": int(prize_row["runner_up_amount_cents"]),
            },
        ]

    result = {
        "tournament_id": tournament_id,
        "id": tournament_id,
        "settlement_status": context["state"],
        "status": context["state"],
        "state": context["state"],
        "paused": context["paused"],
        "suspended": context["paused"],
        "payments_open": context["payments_open"],
        "recoveries_open": context["recoveries_open"],
        "can_create_payment": context["can_create_payment"],
        "can_register_recovery": context["can_register_recovery"],
        "reason": context["reason"],
        "blocking_appeal_ids": context["blocking_appeal_ids"],
        "pending_appeal_ids": context["pending_appeal_ids"],
        "suspension": context.get("suspension"),
        "settlement_state": context,
        "final_context": context,
        "current_final": current_final,
        "effective_final": current_final if context["state"] == SETTLED else None,
        "effective_final_result_version_id": (
            context["result_version_id"] if context["state"] == SETTLED else None
        ),
        "observed_final": current_final,
        "prize_settings": prize,
        "prize": prize,
        "prizes": prize,
        "prize_pool": pool,
        "fund_pool": pool,
        "fund_summary": pool,
        "balance": pool,
        "fund_balance": pool,
        "available_funds_cents": pool["available_funds_cents"],
        "available_balance_cents": pool["available_balance_cents"],
        "funds_available_cents": pool["funds_available_cents"],
        "cash_balance_cents": pool["cash_balance_cents"],
        "pending_payment_total_cents": pool["pending_payment_total_cents"],
        "players": player_rows,
        "player_settlements": player_rows,
        "player_balances": player_rows,
        "balances": player_rows,
        "awards": awards,
        "current_awards": awards,
        "payments": payments,
        "payment_orders": payments,
        "payment_history": payments,
        "historical_payments": payments,
        "orders": payments,
        "recoveries": recoveries,
        "recovery_orders": recoveries,
        "clawbacks": recoveries,
        "funding_additions": funding,
        "funding_history": funding,
        "fund_additions": funding,
        "funds": funding,
        "top_ups": funding,
        "fund_movements": movements,
        "money_movements": movements,
        "transactions": movements,
        "account_ledger": movements,
        "funding_ledger": movements,
        "ledger": movements,
        "cash_ledger": cash_ledger,
        "actual_cash_movements": cash_ledger,
        "award_versions": award_versions,
        "final_result_versions": award_versions,
        "entitlement_history": award_versions,
        "award_differences": award_diffs,
        "differences": award_diffs,
        "entitlement_differences": award_diffs,
        "readjudication_differences": award_diffs,
    }
    return with_fen_aliases(result)


def get_settlement(
    conn: sqlite3.Connection, tournament_id: int
) -> dict[str, Any]:
    with read_tx(conn):
        return build_settlement_view(conn, tournament_id)


def get_payment(
    conn: sqlite3.Connection, payment_id: int, tournament_id: int | None = None
) -> dict[str, Any]:
    with read_tx(conn):
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE id = ?", (payment_id,)
        ).fetchone()
        if row is None or (
            tournament_id is not None and int(row["tournament_id"]) != tournament_id
        ):
            raise _error(404, "payment_not_found", "Payment order does not exist")
        players = service._players_by_id(conn, int(row["tournament_id"]))
        view = _payment_view(row, players)
        return with_fen_aliases(
            {"payment": view, "payment_order": view, "order": view, **view}
        )


# ---------------------------------------------------------------------------
# Prize settings and funding


def set_prize_settings_from_body(
    conn: sqlite3.Connection,
    tournament_id: int,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    key = _idempotency_key(None, idempotency_key, body)
    nested = _first(
        body, "prize_settings", "prize_amounts", "amounts", "prizes", "awards"
    )
    source = {**(nested if isinstance(nested, dict) else {}), **body}

    champion = _money(
        source,
        "champion_amount_cents",
        "champion_prize_amount_cents",
        "champion_prize_cents",
        "champion_amount",
        "champion_prize",
        "winner_amount_cents",
        "winner_prize_cents",
        "first_place_amount_cents",
        "first_place",
        "first",
        "champion",
        "winner",
        minimum=0,
    )
    runner_up = _money(
        source,
        "runner_up_amount_cents",
        "runner_up_prize_amount_cents",
        "runner_up_prize_cents",
        "runner_up_amount",
        "runner_up_prize",
        "runnerup_amount_cents",
        "second_place_amount_cents",
        "second_place",
        "second",
        "loser_amount_cents",
        "runner_up",
        "runnerup",
        minimum=0,
    )
    if champion is None or runner_up is None:
        raise _error(
            422,
            "prize_amounts_required",
            "Both champion_amount_cents and runner_up_amount_cents are required",
        )
    set_by = _clean_text(
        _first(body, "set_by", "configured_by", "created_by", "updated_by", "staff")
    ) or "finance"

    scope = f"prize-settings:{tournament_id}"
    request_hash = service._request_hash(
        {
            "champion_amount_cents": champion,
            "runner_up_amount_cents": runner_up,
            "set_by": set_by,
        }
    )

    with write_tx(conn):
        service._require_tournament(conn, tournament_id)
        if key:
            cached = service._check_idempotency(conn, key, scope, request_hash)
            if cached is not None:
                return cached

        existing = conn.execute(
            "SELECT * FROM prize_settings WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()
        if existing is not None:
            raise _error(
                409,
                "prize_settings_already_set",
                "Prize amounts can be configured only once for a tournament",
                prize_settings=_prize_settings_view(existing),
            )

        timestamp = service.now_iso()
        conn.execute(
            """
            INSERT INTO prize_settings
                (tournament_id, champion_amount_cents, runner_up_amount_cents,
                 set_by, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tournament_id, champion, runner_up, set_by, key, timestamp),
        )
        view = build_settlement_view(conn, tournament_id)
        response = {
            "prize_settings": view["prize_settings"],
            "prize": view["prize_settings"],
            "prizes": view["prize_settings"],
            "awards": view["prize_settings"],
            "settlement": view,
            "tournament_id": tournament_id,
        }
        if key:
            service._store_idempotency(
                conn, key, scope, request_hash, 201, response
            )
        return 201, response


def add_funds_from_body(
    conn: sqlite3.Connection,
    tournament_id: int,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    key = _idempotency_key(None, idempotency_key, body)
    amount = _money(
        body,
        "amount_cents",
        "funding_amount_cents",
        "top_up_amount_cents",
        "added_amount_cents",
        "amount",
        "value_cents",
    )
    if amount is None:
        raise _error(422, "amount_required", "A positive amount_cents is required")
    source = _clean_text(_first(body, "source", "funding_source", "account"))
    note = _clean_text(_first(body, "note", "reason", "description", "memo"))
    funded_by = _clean_text(
        _first(body, "funded_by", "received_by", "created_by", "staff", "operator")
    ) or "finance"

    scope = f"fund-add:{tournament_id}"
    request_hash = service._request_hash(
        {"amount_cents": amount, "source": source, "note": note, "funded_by": funded_by}
    )

    with write_tx(conn):
        service._require_tournament(conn, tournament_id)
        if key:
            cached = service._check_idempotency(conn, key, scope, request_hash)
            if cached is not None:
                return cached

        timestamp = service.now_iso()
        cur = conn.execute(
            """
            INSERT INTO funding_additions
                (tournament_id, amount_cents, source, note, funded_by,
                 idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tournament_id, amount, source, note, funded_by, key, timestamp),
        )
        funding_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT * FROM funding_additions WHERE id = ?", (funding_id,)
        ).fetchone()
        assert row is not None
        funding = _funding_view(row)
        settlement = build_settlement_view(conn, tournament_id)
        response = {
            "fund": funding,
            "funding": funding,
            "funding_addition": funding,
            "top_up": funding,
            **funding,
            "settlement": settlement,
        }
        if key:
            service._store_idempotency(
                conn, key, scope, request_hash, 201, response
            )
        return 201, response


# ---------------------------------------------------------------------------
# Payment orders


def _extract_player_id(body: dict[str, Any]) -> int | None:
    raw = _first(
        body,
        "player_id",
        "recipient_player_id",
        "playerId",
        "recipient_id",
        "payee_player_id",
        "payee_id",
        "winner_player_id",
        "player",
        "recipient",
        "payee",
    )
    pid = _as_int_id(raw, "player_id")
    if pid is not None:
        return pid
    name = _clean_text(_first(body, "player_name", "recipient_name"))
    if name is None:
        return None
    return None  # looked up by caller with tournament scope


def create_payment_from_body(
    conn: sqlite3.Connection,
    tournament_id: int,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    key = _idempotency_key(None, idempotency_key, body)

    player_id = _extract_player_id(body)
    player_name = _clean_text(_first(body, "player_name", "recipient_name"))
    amount = _money(
        body,
        "amount_cents",
        "payment_amount_cents",
        "amount_minor",
        "amount",
        "value_cents",
    )
    if amount is None:
        raise _error(422, "amount_required", "A positive amount_cents is required")
    expected_version = _as_int_id(
        _first(
            body,
            "result_version_id",
            "final_result_version_id",
            "expected_result_version_id",
            "snapshot_result_version_id",
            "effective_final_result_version_id",
            "final_result_version",
            "result_version",
            "version_id",
        ),
        "result_version_id",
    )
    issued_by = _clean_text(
        _first(body, "issued_by", "created_by", "requested_by", "finance_staff", "staff")
    ) or "finance"
    note = _clean_text(_first(body, "note", "reason", "memo", "description"))

    scope = f"payment-create:{tournament_id}"
    request_hash = service._request_hash(
        {
            "player_id": player_id,
            "player_name": player_name,
            "amount_cents": amount,
            "result_version_id": expected_version,
            "issued_by": issued_by,
            "note": note,
        }
    )

    with write_tx(conn):
        service._require_tournament(conn, tournament_id)
        if key:
            cached = service._check_idempotency(conn, key, scope, request_hash)
            if cached is not None:
                return cached

        if player_id is None and player_name is not None:
            row = conn.execute(
                "SELECT id FROM players WHERE tournament_id = ? AND name = ?",
                (tournament_id, player_name),
            ).fetchone()
            if row is not None:
                player_id = int(row["id"])
        if player_id is None:
            raise _error(422, "player_id_required", "player_id is required")

        player = conn.execute(
            "SELECT * FROM players WHERE id = ? AND tournament_id = ?",
            (player_id, tournament_id),
        ).fetchone()
        if player is None:
            raise _error(
                404,
                "player_not_found",
                "Player does not belong to this tournament",
                player_id=player_id,
            )

        context = _require_decided_context(conn, tournament_id, expected_version)
        prize = conn.execute(
            "SELECT * FROM prize_settings WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()
        if prize is None:
            raise _error(
                409,
                "prize_settings_not_configured",
                "Set the fixed champion and runner-up prize amounts before issuing payments",
            )

        if player_id == context["winner_player_id"]:
            award_type = "champion"
            award_amount = int(prize["champion_amount_cents"])
        elif player_id == context["loser_player_id"]:
            award_type = "runner_up"
            award_amount = int(prize["runner_up_amount_cents"])
        else:
            raise _error(
                422,
                "player_not_prize_winner",
                "Payments can only be issued to the champion or runner-up of the current effective final",
                current_champion_id=context["winner_player_id"],
                current_runner_up_id=context["loser_player_id"],
                result_version_id=context["result_version_id"],
            )

        totals, balances = _raw_balances(conn, tournament_id)
        player_balance = balances.get(player_id, {})
        net_paid = int(player_balance.get("paid_cents", 0)) - int(
            player_balance.get("recovered_cents", 0)
        )
        pending_amount = int(player_balance.get("pending_cents", 0))
        player_capacity = award_amount - net_paid - pending_amount

        if amount > player_capacity:
            raise _error(
                409,
                "payment_exceeds_award_balance",
                "Payment would exceed the player's current prize minus net paid and unconfirmed payments",
                player_id=player_id,
                amount_cents=amount,
                current_prize_amount_cents=award_amount,
                net_paid_cents=net_paid,
                unconfirmed_payment_cents=pending_amount,
                available_award_balance_cents=max(0, player_capacity),
                raw_award_balance_cents=player_capacity,
            )

        available_funds = int(totals["available_funds_cents"])
        if amount > available_funds:
            raise _error(
                409,
                "insufficient_available_funds",
                "Tournament available funds are insufficient; add funds or register an actual recovery",
                amount_cents=amount,
                available_funds_cents=available_funds,
                cash_balance_cents=totals["cash_balance_cents"],
                pending_payment_total_cents=totals["pending_cents"],
            )

        timestamp = service.now_iso()
        cur = conn.execute(
            """
            INSERT INTO payment_orders
                (tournament_id, player_id, amount_cents, final_match_id,
                 result_version_id, award_type, award_amount_snapshot_cents,
                 status, created_by, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tournament_id,
                player_id,
                amount,
                context["final_match_id"],
                context["result_version_id"],
                award_type,
                award_amount,
                PENDING,
                issued_by,
                key,
                timestamp,
            ),
        )
        payment_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE id = ?", (payment_id,)
        ).fetchone()
        assert row is not None
        players = service._players_by_id(conn, tournament_id)
        payment = _payment_view(row, players)
        settlement = build_settlement_view(conn, tournament_id)
        response = {
            "payment": payment,
            "payment_order": payment,
            "order": payment,
            **payment,
            "settlement": settlement,
        }
        if key:
            service._store_idempotency(conn, key, scope, request_hash, 201, response)
        return 201, response


# ---------------------------------------------------------------------------
# Bank confirmation


def _requested_outcome(body: dict[str, Any], override: str | None) -> str | None:
    raw = override
    if raw is None:
        raw = _first(
            body,
            "outcome",
            "bank_outcome",
            "status",
            "result",
            "bank_status",
            "confirmation_status",
            "action",
            "decision",
        )
    if raw is not None:
        if isinstance(raw, bool):
            return SUCCESS if raw else FAILED
        text = str(raw).strip().lower().replace("-", "_")
        if text in {
            SUCCESS,
            "succeeded",
            "successful",
            "successfull",
            "paid",
            "confirmed",
            "confirm",
            "ok",
            "accepted",
            "complete",
            "completed",
        }:
            return SUCCESS
        if text in {
            FAILED,
            "fail",
            "failure",
            "rejected",
            "reject",
            "error",
            "returned",
            "cancelled",
            "canceled",
        }:
            return FAILED
        if text == PENDING:
            return PENDING

    for key in ("success", "successful", "is_success", "succeeded", "paid"):
        if key in body and isinstance(body[key], bool):
            return SUCCESS if body[key] else FAILED
    if "confirmed" in body and isinstance(body["confirmed"], bool):
        return SUCCESS if body["confirmed"] else FAILED
    if _first(body, "bank_receipt_number", "receipt_number", "bank_receipt", "receipt_no"):
        return SUCCESS
    if _first(body, "failure_reason", "failed_reason", "rejection_reason"):
        return FAILED
    return None


def confirm_payment_from_body(
    conn: sqlite3.Connection,
    payment_id: int,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    tournament_id: int | None = None,
    outcome_override: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    nested_bank = _first(
        body, "bank_confirmation", "bank_result", "bank", "confirmation", "result"
    )
    if isinstance(nested_bank, dict):
        body = {**nested_bank, **body}
    key = _idempotency_key(None, idempotency_key, body)
    requested_outcome = _requested_outcome(body, outcome_override)
    receipt = _clean_text(
        _first(
            body,
            "bank_receipt_number",
            "bank_receipt_id",
            "receipt_number",
            "receipt_id",
            "bank_receipt",
            "receipt_no",
            "receipt",
            "bank_reference_number",
            "bank_reference",
            "reference_number",
            "transaction_reference",
            "transaction_id",
        )
    )
    confirmed_by = _clean_text(
        _first(body, "confirmed_by", "recorded_by", "registered_by", "operator", "staff")
    ) or "finance"
    note = _clean_text(_first(body, "note", "confirmation_note", "memo"))
    failure_reason = _clean_text(
        _first(body, "failure_reason", "failed_reason", "rejection_reason", "reason")
    )
    if requested_outcome == FAILED and not failure_reason:
        failure_reason = note or "bank_rejected"

    scope = f"payment-confirm:{payment_id}"
    request_hash = service._request_hash(
        {
            "payment_id": payment_id,
            "outcome": requested_outcome,
            "bank_receipt_number": receipt,
            "confirmed_by": confirmed_by,
            "note": note,
            "failure_reason": failure_reason,
        }
    )

    with write_tx(conn):
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE id = ?", (payment_id,)
        ).fetchone()
        if row is None or (
            tournament_id is not None and int(row["tournament_id"]) != tournament_id
        ):
            raise _error(404, "payment_not_found", "Payment order does not exist")
        tournament_id = int(row["tournament_id"])

        if key:
            cached = service._check_idempotency(conn, key, scope, request_hash)
            if cached is not None:
                return cached

        if requested_outcome is None or requested_outcome == PENDING:
            raise _error(
                422,
                "confirmation_outcome_required",
                "Provide a final bank outcome: success or failed",
            )

        players = service._players_by_id(conn, tournament_id)

        # A payment has exactly one final result.  A repeated compatible
        # confirmation is the original fact, not a second ledger entry.
        if row["status"] != PENDING:
            if row["status"] != requested_outcome:
                raise _error(
                    409,
                    "payment_already_final",
                    "A payment order can only have one final bank result",
                    payment_id=payment_id,
                    existing_status=row["status"],
                    requested_status=requested_outcome,
                    bank_receipt_number=row["bank_receipt_number"],
                    confirmed_at=row["confirmed_at"],
                )
            if (
                requested_outcome == SUCCESS
                and receipt is not None
                and row["bank_receipt_number"] is not None
                and receipt != row["bank_receipt_number"]
            ):
                raise _error(
                    409,
                    "bank_receipt_conflict",
                    "The successful payment already has a different bank receipt number",
                    existing_receipt_number=row["bank_receipt_number"],
                    supplied_receipt_number=receipt,
                )
            view = _payment_view(row, players)
            response = {
                "confirmation": view,
                "payment": view,
                "payment_order": view,
                "order": view,
                **view,
            }
            if key:
                service._store_idempotency(
                    conn, key, scope, request_hash, 200, response
                )
            return 200, response

        if requested_outcome == SUCCESS:
            if not receipt:
                raise _error(
                    422,
                    "bank_receipt_required",
                    "A successful bank confirmation must include a bank_receipt_number",
                )
            other = conn.execute(
                """
                SELECT id, tournament_id FROM payment_orders
                WHERE bank_receipt_number = ? AND status = 'success' AND id <> ?
                """,
                (receipt, payment_id),
            ).fetchone()
            if other is not None:
                raise _error(
                    409,
                    "bank_receipt_already_used",
                    "This successful bank receipt is already attached to another payment",
                    bank_receipt_number=receipt,
                    existing_payment_id=int(other["id"]),
                    existing_tournament_id=int(other["tournament_id"]),
                )

        timestamp = service.now_iso()
        try:
            if requested_outcome == SUCCESS:
                conn.execute(
                    """
                    UPDATE payment_orders
                    SET status = 'success', bank_receipt_number = ?,
                        bank_reference = COALESCE(?, bank_reference),
                        confirmed_by = ?, confirmation_note = ?,
                        confirmed_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (receipt, receipt, confirmed_by, note, timestamp, payment_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE payment_orders
                    SET status = 'failed', confirmed_by = ?,
                        confirmation_note = ?, failure_reason = ?,
                        confirmed_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (confirmed_by, note, failure_reason, timestamp, payment_id),
                )
        except sqlite3.IntegrityError as exc:
            raise _error(
                409,
                "bank_receipt_already_used",
                "This successful bank receipt is already attached to another payment",
                bank_receipt_number=receipt,
            ) from exc

        row = conn.execute(
            "SELECT * FROM payment_orders WHERE id = ?", (payment_id,)
        ).fetchone()
        assert row is not None
        view = _payment_view(row, players)
        response = {
            "confirmation": {
                "payment_id": payment_id,
                "status": row["status"],
                "outcome": row["status"],
                "bank_receipt_number": row["bank_receipt_number"],
                "receipt_number": row["bank_receipt_number"],
                "confirmed_at": row["confirmed_at"],
            },
            **view,
            "payment": view,
            "payment_order": view,
            "order": view,
            "settlement": build_settlement_view(conn, tournament_id),
        }
        if key:
            service._store_idempotency(conn, key, scope, request_hash, 200, response)
        return 200, response


# ---------------------------------------------------------------------------
# Recoveries / clawbacks


def register_recovery_from_body(
    conn: sqlite3.Connection,
    tournament_id: int,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    key = _idempotency_key(None, idempotency_key, body)
    player_id = _extract_player_id(body)
    player_name = _clean_text(_first(body, "player_name", "recipient_name"))
    amount = _money(
        body,
        "amount_cents",
        "recovery_amount_cents",
        "recovered_amount_cents",
        "clawback_amount_cents",
        "amount",
    )
    if amount is None:
        raise _error(422, "amount_required", "A positive recovery amount_cents is required")
    expected_version = _as_int_id(
        _first(
            body,
            "result_version_id",
            "final_result_version_id",
            "expected_result_version_id",
            "current_result_version_id",
            "effective_final_result_version_id",
            "final_result_version",
            "result_version",
            "version_id",
        ),
        "result_version_id",
    )
    received_by = _clean_text(
        _first(body, "received_by", "recovered_by", "registered_by", "staff", "operator")
    ) or "finance"
    note = _clean_text(_first(body, "note", "reason", "memo"))

    scope = f"recovery-create:{tournament_id}"
    request_hash = service._request_hash(
        {
            "player_id": player_id,
            "player_name": player_name,
            "amount_cents": amount,
            "result_version_id": expected_version,
            "received_by": received_by,
            "note": note,
        }
    )

    with write_tx(conn):
        service._require_tournament(conn, tournament_id)
        if key:
            cached = service._check_idempotency(conn, key, scope, request_hash)
            if cached is not None:
                return cached

        if player_id is None and player_name is not None:
            row = conn.execute(
                "SELECT id FROM players WHERE tournament_id = ? AND name = ?",
                (tournament_id, player_name),
            ).fetchone()
            if row is not None:
                player_id = int(row["id"])
        if player_id is None:
            raise _error(422, "player_id_required", "player_id is required")

        player = conn.execute(
            "SELECT * FROM players WHERE id = ? AND tournament_id = ?",
            (player_id, tournament_id),
        ).fetchone()
        if player is None:
            raise _error(404, "player_not_found", "Player does not belong to this tournament")

        context = _require_decided_context(conn, tournament_id, expected_version)
        prize = conn.execute(
            "SELECT * FROM prize_settings WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()
        if prize is None:
            raise _error(
                409,
                "prize_settings_not_configured",
                "Prize amounts must be configured before determining recoverable overpayment",
            )

        if player_id == context["winner_player_id"]:
            current_award = int(prize["champion_amount_cents"])
        elif player_id == context["loser_player_id"]:
            current_award = int(prize["runner_up_amount_cents"])
        else:
            # A player no longer on the final has a current award of zero.
            current_award = 0

        _totals, balances = _raw_balances(conn, tournament_id)
        b = balances.get(player_id, {})
        net_paid = int(b.get("paid_cents", 0)) - int(b.get("recovered_cents", 0))
        recoverable = max(0, net_paid - current_award)
        if amount > recoverable:
            raise _error(
                409,
                "recovery_exceeds_recoverable_amount",
                "Only net paid above the player's current prize can be recovered",
                player_id=player_id,
                amount_cents=amount,
                net_paid_cents=net_paid,
                current_prize_amount_cents=current_award,
                recoverable_amount_cents=recoverable,
                pending_recovery_cents=recoverable,
            )

        timestamp = service.now_iso()
        cur = conn.execute(
            """
            INSERT INTO payment_recoveries
                (tournament_id, player_id, amount_cents, final_match_id,
                 result_version_id, current_award_amount_snapshot_cents,
                 received_by, note, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tournament_id,
                player_id,
                amount,
                context["final_match_id"],
                context["result_version_id"],
                current_award,
                received_by,
                note,
                key,
                timestamp,
            ),
        )
        recovery_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT * FROM payment_recoveries WHERE id = ?", (recovery_id,)
        ).fetchone()
        assert row is not None
        players = service._players_by_id(conn, tournament_id)
        recovery = _recovery_view(row, players)
        settlement = build_settlement_view(conn, tournament_id)
        response = {
            "recovery": recovery,
            "recovery_order": recovery,
            "recovery_record": recovery,
            "clawback": recovery,
            **recovery,
            "settlement": settlement,
        }
        if key:
            service._store_idempotency(conn, key, scope, request_hash, 201, response)
        return 201, response


# ---------------------------------------------------------------------------
# Flexible explicit-argument APIs
#
# HTTP uses dict bodies.  These wrappers also support the explicit positional
# style used by the rest of the service layer and by direct concurrency tests.


def set_prize_settings(
    conn: sqlite3.Connection,
    tournament_id: int,
    champion_amount_cents: Any = None,
    runner_up_amount_cents: Any = None,
    idempotency_key: str | None = None,
    *,
    body: dict[str, Any] | None = None,
    **extra: Any,
) -> tuple[int, dict[str, Any]]:
    if isinstance(champion_amount_cents, dict):
        return set_prize_settings_from_body(
            conn, tournament_id, champion_amount_cents, runner_up_amount_cents
        )
    payload = dict(body or {})
    if champion_amount_cents is not None:
        payload.setdefault("champion_amount_cents", champion_amount_cents)
    if runner_up_amount_cents is not None:
        payload.setdefault("runner_up_amount_cents", runner_up_amount_cents)
    payload.update({k: v for k, v in extra.items() if v is not None})
    return set_prize_settings_from_body(conn, tournament_id, payload, idempotency_key)


def add_funds(
    conn: sqlite3.Connection,
    tournament_id: int,
    amount_cents: Any = None,
    idempotency_key: str | None = None,
    *,
    body: dict[str, Any] | None = None,
    **extra: Any,
) -> tuple[int, dict[str, Any]]:
    if isinstance(amount_cents, dict):
        return add_funds_from_body(conn, tournament_id, amount_cents, idempotency_key)
    payload = dict(body or {})
    if amount_cents is not None:
        payload.setdefault("amount_cents", amount_cents)
    payload.update({k: v for k, v in extra.items() if v is not None})
    return add_funds_from_body(conn, tournament_id, payload, idempotency_key)


# Common domain-language alias.
add_prize_funds = add_funds
add_funding = add_funds


def create_payment(
    conn: sqlite3.Connection,
    tournament_id: int,
    player_id: Any = None,
    amount_cents: Any = None,
    result_version_id: Any = None,
    idempotency_key: str | None = None,
    *,
    body: dict[str, Any] | None = None,
    **extra: Any,
) -> tuple[int, dict[str, Any]]:
    # HTTP-style positional convention: (conn, tid, body, idempotency_key).
    if isinstance(player_id, dict):
        key = amount_cents if idempotency_key is None else idempotency_key
        return create_payment_from_body(conn, tournament_id, player_id, key)

    # Allow create_payment(conn, tid, player_id, amount, "idem-key").
    if idempotency_key is None and isinstance(result_version_id, str):
        text = result_version_id.strip()
        if not text.lstrip("-").isdigit():
            idempotency_key = result_version_id
            result_version_id = None

    payload = dict(body or {})
    if player_id is not None:
        payload.setdefault("player_id", player_id)
    if amount_cents is not None:
        payload.setdefault("amount_cents", amount_cents)
    if result_version_id is not None:
        payload.setdefault("result_version_id", result_version_id)
    payload.update({k: v for k, v in extra.items() if v is not None})
    return create_payment_from_body(conn, tournament_id, payload, idempotency_key)


issue_payment = create_payment
create_payment_order = create_payment


def confirm_payment(
    conn: sqlite3.Connection,
    payment_id: int,
    outcome: Any = None,
    bank_receipt_number: Any = None,
    idempotency_key: str | None = None,
    *,
    body: dict[str, Any] | None = None,
    tournament_id: int | None = None,
    outcome_override: str | None = None,
    **extra: Any,
) -> tuple[int, dict[str, Any]]:
    # HTTP-style positional convention: (conn, pid, body, idempotency_key).
    if isinstance(outcome, dict):
        key = idempotency_key
        if key is None and isinstance(bank_receipt_number, str):
            key = bank_receipt_number
        return confirm_payment_from_body(
            conn,
            payment_id,
            outcome,
            key,
            tournament_id=tournament_id,
            outcome_override=outcome_override,
        )

    payload = dict(body or {})
    if outcome is not None:
        payload.setdefault("outcome", outcome)
    if bank_receipt_number is not None:
        payload.setdefault("bank_receipt_number", bank_receipt_number)
    payload.update({k: v for k, v in extra.items() if v is not None})
    return confirm_payment_from_body(
        conn,
        payment_id,
        payload,
        idempotency_key,
        tournament_id=tournament_id,
        outcome_override=outcome_override,
    )


register_bank_result = confirm_payment
confirm_bank_result = confirm_payment


def register_recovery(
    conn: sqlite3.Connection,
    tournament_id: int,
    player_id: Any = None,
    amount_cents: Any = None,
    result_version_id: Any = None,
    idempotency_key: str | None = None,
    *,
    body: dict[str, Any] | None = None,
    **extra: Any,
) -> tuple[int, dict[str, Any]]:
    if isinstance(player_id, dict):
        return register_recovery_from_body(
            conn, tournament_id, player_id, amount_cents
        )

    if idempotency_key is None and isinstance(result_version_id, str):
        text = result_version_id.strip()
        if not text.lstrip("-").isdigit():
            idempotency_key = result_version_id
            result_version_id = None

    payload = dict(body or {})
    if player_id is not None:
        payload.setdefault("player_id", player_id)
    if amount_cents is not None:
        payload.setdefault("amount_cents", amount_cents)
    if result_version_id is not None:
        payload.setdefault("result_version_id", result_version_id)
    payload.update({k: v for k, v in extra.items() if v is not None})
    return register_recovery_from_body(
        conn, tournament_id, payload, idempotency_key
    )


create_recovery = register_recovery
register_clawback = register_recovery
create_clawback = register_recovery


# Attach the public service-layer API to the existing service module as well.
# This keeps direct callers working whether they import app.settlement or use
# the long-standing app.service namespace.
service.get_settlement = get_settlement  # type: ignore[attr-defined]
service.set_prize_settings = set_prize_settings  # type: ignore[attr-defined]
service.configure_prizes = set_prize_settings  # type: ignore[attr-defined]
service.add_funds = add_funds  # type: ignore[attr-defined]
service.add_prize_funds = add_prize_funds  # type: ignore[attr-defined]
service.create_payment = create_payment  # type: ignore[attr-defined]
service.issue_payment = issue_payment  # type: ignore[attr-defined]
service.confirm_payment = confirm_payment  # type: ignore[attr-defined]
service.register_recovery = register_recovery  # type: ignore[attr-defined]
service.register_clawback = register_clawback  # type: ignore[attr-defined]
