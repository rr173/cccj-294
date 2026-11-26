"""Spectator tickets, online check-in and offline gate settlement.

The module is intentionally conservative about capacity:

``confirmed admissions + release_limit of every unsettled permit <= capacity``.

A permit's ticket *list* can be longer than its ``release_limit``; listed
codes are locked, but only ``release_limit`` places are reserved against the
capacity.  The full limit stays reserved until settlement, even after the gate
has admitted some people while offline.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse

from . import service
from .clock import utcnow
from .db import connect, read_tx, write_tx

router = APIRouter()


# ---------------------------------------------------------------------------
# FastAPI plumbing and small helpers


def request_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _error(status: int, code: str, message: str, **extra: Any) -> service.TournamentError:
    return service.TournamentError(status, code, message, **extra)


def _ok(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload)


def _first(d: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _body_key(body: dict[str, Any] | None) -> str | None:
    value = _first(
        body or {},
        "idempotency_key",
        "idempotencyKey",
        "Idempotency-Key",
        "request_id",
        "requestId",
        "client_request_id",
        "nonce",
    )
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _key_value(header: str | None, body: dict[str, Any] | None) -> str | None:
    raw = header or _body_key(body)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _idempotent(
    conn: sqlite3.Connection,
    key: str | None,
    scope: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    if not key:
        return None
    request_hash = service._request_hash(payload)
    return service._check_idempotency(conn, key, scope, request_hash)


def _store(
    conn: sqlite3.Connection,
    key: str | None,
    scope: str,
    payload: dict[str, Any],
    status: int,
    response: dict[str, Any],
) -> None:
    if key:
        service._store_idempotency(
            conn, key, scope, service._request_hash(payload), status, response
        )


def _clean_text(value: Any, what: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise _error(422, f"{what}_required", f"{what} is required")
        return None
    text = str(value).strip()
    if required and not text:
        raise _error(422, f"{what}_required", f"{what} must not be empty")
    return text or None


def _int_value(
    value: Any,
    what: str,
    *,
    minimum: int | None = 0,
    default: int | None = None,
) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        raise _error(422, f"invalid_{what}", f"{what} must be an integer")
    try:
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError
            number = int(value)
        elif isinstance(value, int):
            number = value
        else:
            text = str(value).strip()
            if not text:
                return default
            number = int(text)
    except (TypeError, ValueError) as exc:
        raise _error(422, f"invalid_{what}", f"{what} must be an integer") from exc
    if minimum is not None and number < minimum:
        raise _error(
            422,
            f"invalid_{what}",
            f"{what} must be at least {minimum}",
            value=number,
        )
    return number


def _parse_utc(value: Any, what: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = _clean_text(value, what)
        assert text is not None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _error(
                422, f"invalid_{what}", f"{what} must be an ISO-8601 UTC timestamp"
            ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(dt: datetime, starts_at: str, ends_at: str) -> bool:
    start = datetime.fromisoformat(starts_at)
    end = datetime.fromisoformat(ends_at)
    if start.tzinfo is None:
        from datetime import timezone

        start = start.replace(tzinfo=timezone.utc)
        end = end.replace(tzinfo=timezone.utc)
    return start <= dt < end


def _require_reserved_slot(
    conn: sqlite3.Connection, tournament_id: int, slot_id: str
) -> sqlite3.Row:
    service._require_tournament(conn, tournament_id)
    service._require_slot(slot_id)
    row = conn.execute(
        """
        SELECT * FROM schedule_assignments
        WHERE tournament_id = ? AND slot_id = ?
        """,
        (tournament_id, slot_id),
    ).fetchone()
    if row is None:
        raise _error(
            409,
            "slot_not_reserved",
            "Admission is supported only for a reserved bracket slot",
            tournament_id=tournament_id,
            slot_id=slot_id,
        )
    return row


def _current_assignment(
    conn: sqlite3.Connection, tournament_id: int, slot_id: str
) -> sqlite3.Row | None:
    service._require_slot(slot_id)
    return conn.execute(
        "SELECT * FROM schedule_assignments WHERE tournament_id=? AND slot_id=?",
        (tournament_id, slot_id),
    ).fetchone()


def _resolve_tournament_slot(
    conn: sqlite3.Connection,
    body: dict[str, Any],
    tournament_id: int | None = None,
    slot_id: str | None = None,
) -> tuple[int, str]:
    tid = tournament_id or _first(body, "tournament_id", "tournamentId", "event_id")
    if tid is None and isinstance(body.get("tournament"), dict):
        tid = _first(body["tournament"], "id", "tournament_id")
    slot = slot_id or _first(
        body,
        "slot_id", "slotId", "slot",
        "position_id", "positionId", "position",
        "round_slot", "roundSlot", "round_position", "roundPosition",
        "wheel_position",
    )
    if tid is None:
        raise _error(422, "tournament_id_required", "tournament_id is required")
    if slot is None:
        raise _error(422, "slot_id_required", "slot_id is required")
    try:
        tid_int = int(tid)
    except (TypeError, ValueError) as exc:
        raise _error(422, "invalid_tournament_id", "tournament_id must be an integer") from exc
    slot_text = str(slot).strip()
    service._require_slot(slot_text)
    service._require_tournament(conn, tid_int)
    return tid_int, slot_text


# ---------------------------------------------------------------------------
# Views and capacity queries


def ticket_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticket_id": row["id"],
        "tournament_id": row["tournament_id"],
        "slot_id": row["slot_id"],
        "code": row["code"],
        "ticket_code": row["code"],
        "entry_code": row["code"],
        "holder_name": row["holder_name"],
        "status": row["status"],
        "state": row["status"],
        "offline_permit_id": row["offline_permit_id"],
        "permit_id": row["offline_permit_id"],
        "issued_by": row["issued_by"],
        "issued_at": row["issued_at"],
        "revoked_by": row["revoked_by"],
        "revoke_reason": row["revoke_reason"],
        "revoked_at": row["revoked_at"],
        "used_at": row["used_at"],
        "admitted_at": row["used_at"],
        "admission_id": row["admission_id"],
        "usable": row["status"] == "unused" and row["offline_permit_id"] is None,
        "valid": row["status"] == "unused",
        "admitted": row["status"] == "used",
        "redeemed": row["status"] == "used",
        "used": row["status"] == "used",
        "revoked": row["status"] == "revoked",
    }


def admission_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "admission_id": row["id"],
        "tournament_id": row["tournament_id"],
        "slot_id": row["slot_id"],
        "ticket_id": row["ticket_id"],
        "ticket_code": row["ticket_code"],
        "code": row["ticket_code"],
        "source": row["source"],
        "channel": row["source"],
        "status": row["status"],
        "gate_id": row["gate_id"],
        "venue_name": row["venue_name"],
        "venue": row["venue_name"],
        "admitted_at": row["admitted_at"],
        "timestamp": row["admitted_at"],
        "recorded_at": row["recorded_at"],
        "permit_id": row["permit_id"],
        "settlement_id": row["settlement_id"],
    }


def permit_view(
    conn: sqlite3.Connection, row: sqlite3.Row, *, include_tickets: bool = True
) -> dict[str, Any]:
    ticket_rows = []
    if include_tickets:
        ticket_rows = conn.execute(
            """
            SELECT t.*, opt.status AS hold_status
            FROM offline_permit_tickets opt
            JOIN admission_tickets t ON t.id = opt.ticket_id
            WHERE opt.permit_id = ?
            ORDER BY t.code
            """,
            (row["id"],),
        ).fetchall()
    codes = [r["code"] for r in ticket_rows]
    reserved_codes = [r["code"] for r in ticket_rows if r["hold_status"] == "reserved"]
    used_codes = [r["code"] for r in ticket_rows if r["hold_status"] == "used"]
    released_codes = [r["code"] for r in ticket_rows if r["hold_status"] == "released"]
    now = utcnow()
    window_active = False
    try:
        window_active = _in_window(now, row["starts_at"], row["ends_at"])
    except Exception:
        window_active = False
    window_ended = now >= datetime.fromisoformat(row["ends_at"]) if row["ends_at"] else False

    settlement = None
    if row["status"] == "settled" and row["settlement_response_json"]:
        try:
            settlement = json.loads(row["settlement_response_json"])
        except json.JSONDecodeError:
            settlement = None

    return {
        "id": row["id"],
        "permit_id": row["id"],
        "permission_id": row["id"],
        "offline_permit_id": row["id"],
        "offline_permission_id": row["id"],
        "permit_code": row["permit_code"],
        "permission_code": row["permit_code"],
        "code": row["permit_code"],
        "tournament_id": row["tournament_id"],
        "slot_id": row["slot_id"],
        "gate_id": row["gate_id"],
        "gate": row["gate_id"],
        "venue_name": row["venue_name"],
        "venue": row["venue_name"],
        "schedule_date": row["schedule_date"],
        "date": row["schedule_date"],
        "start_minute": row["start_minute"],
        "end_minute": row["end_minute"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "time_window": {
            "venue_name": row["venue_name"],
            "venue": row["venue_name"],
            "date": row["schedule_date"],
            "start_minute": row["start_minute"],
            "end_minute": row["end_minute"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
        },
        "release_limit": row["release_limit"],
        "allowed_count": row["release_limit"],
        "allowed_people": row["release_limit"],
        "allowed_persons": row["release_limit"],
        "person_limit": row["release_limit"],
        "max_people": row["release_limit"],
        "admission_limit": row["release_limit"],
        "quota": row["release_limit"],
        "reserved_capacity": row["release_limit"] if row["status"] == "active" else 0,
        "listed_code_count": row["listed_code_count"],
        "ticket_count": row["listed_code_count"],
        "ticket_codes": codes,
        "codes": codes,
        "reserved_codes": reserved_codes,
        "used_codes": used_codes,
        "released_codes": released_codes,
        "status": row["status"],
        "state": row["status"],
        "settlement_state": "settled" if row["status"] == "settled" else "unsettled",
        "settlement_status": "settled" if row["status"] == "settled" else "unsettled",
        "outstanding": row["status"] == "active",
        "unsettled": row["status"] == "active",
        "settled": row["status"] == "settled",
        "time_window_active": window_active,
        "window_active": window_active,
        "time_window_ended": bool(window_ended),
        "expired": bool(window_ended),
        # Expiry is informational only; expired remains unsettled until report.
        "automatically_settled": False,
        "requested_by": row["requested_by"],
        "idempotency_key": row["idempotency_key"],
        "report_id": row["report_id"],
        "actual_admitted_count": row["actual_admitted_count"],
        "admitted_count": row["actual_admitted_count"],
        "released_code_count": row["released_code_count"],
        "created_at": row["created_at"],
        "terminate_requested_at": row["terminate_requested_at"] if "terminate_requested_at" in row.keys() else None,
        "locally_terminated": bool(row["terminate_requested_at"]) if "terminate_requested_at" in row.keys() else False,
        "settled_at": row["settled_at"],
        "receipt": settlement,
    }


def capacity_counts(
    conn: sqlite3.Connection, tournament_id: int, slot_id: str
) -> dict[str, Any]:
    cap_row = conn.execute(
        "SELECT * FROM admission_capacities WHERE tournament_id=? AND slot_id=?",
        (tournament_id, slot_id),
    ).fetchone()
    admitted = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM admissions WHERE tournament_id=? AND slot_id=?",
            (tournament_id, slot_id),
        ).fetchone()["n"]
    )
    online = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM admissions
            WHERE tournament_id=? AND slot_id=? AND source='online'
            """,
            (tournament_id, slot_id),
        ).fetchone()["n"]
    )
    offline = admitted - online
    permit_row = conn.execute(
        """
        SELECT COUNT(*) AS permit_count,
               COALESCE(SUM(release_limit), 0) AS reserved
        FROM offline_permits
        WHERE tournament_id=? AND slot_id=? AND status='active'
        """,
        (tournament_id, slot_id),
    ).fetchone()
    active_permit_count = int(permit_row["permit_count"])
    reserved = int(permit_row["reserved"])
    capacity = int(cap_row["capacity"]) if cap_row is not None else None
    remaining = None if capacity is None else capacity - admitted - reserved
    return {
        "capacity": capacity,
        "total_capacity": capacity,
        "fixed_capacity": capacity,
        "unlimited": capacity is None,
        "confirmed_admissions": admitted,
        "admitted_count": admitted,
        "admitted": admitted,
        "confirmed_count": admitted,
        "confirmed": admitted,
        "entry_count": admitted,
        "checked_in_count": admitted,
        "used_capacity": admitted,
        "online_admissions": online,
        "offline_admissions": offline,
        "settled_offline_admissions": offline,
        "outstanding_reserved_capacity": reserved,
        "outstanding_reservations": reserved,
        "unsettled_reserved_places": reserved,
        "unsettled_reservation_count": reserved,
        "pending_reserved_capacity": reserved,
        "pending_reservations": reserved,
        "reserved_places": reserved,
        "reserved": reserved,
        "unsettled_reserved_capacity": reserved,
        "reserved_capacity": reserved,
        "offline_reserved": reserved,
        "active_permit_count": active_permit_count,
        "outstanding_permit_count": active_permit_count,
        "remaining_allocatable_capacity": remaining,
        "remaining_allocatable": remaining,
        "allocatable_remaining": remaining,
        "remaining_capacity": remaining,
        "available_to_allocate": remaining,
        "available_allocation_capacity": remaining,
        "remaining_places": remaining,
        "remaining": remaining,
        "available_to_issue_or_reserve": remaining,
        "available_capacity": remaining,
        "available": remaining,
        "online_available_capacity": remaining,
        "can_allocate": capacity is None or remaining > 0,
    }


def build_slot_admission_view(
    conn: sqlite3.Connection, tournament_id: int, slot_id: str
) -> dict[str, Any]:
    service._require_tournament(conn, tournament_id)
    service._require_slot(slot_id)
    assignment = _current_assignment(conn, tournament_id, slot_id)
    counts = capacity_counts(conn, tournament_id, slot_id)
    ticket_rows = conn.execute(
        "SELECT * FROM admission_tickets WHERE tournament_id=? AND slot_id=? ORDER BY id",
        (tournament_id, slot_id),
    ).fetchall()
    permit_rows = conn.execute(
        "SELECT * FROM offline_permits WHERE tournament_id=? AND slot_id=? ORDER BY id",
        (tournament_id, slot_id),
    ).fetchall()
    admission_rows = conn.execute(
        "SELECT * FROM admissions WHERE tournament_id=? AND slot_id=? ORDER BY id",
        (tournament_id, slot_id),
    ).fetchall()
    ticket_counts = {"total": len(ticket_rows), "unused": 0, "used": 0, "revoked": 0, "held": 0}
    for row in ticket_rows:
        ticket_counts[row["status"]] += 1
        if row["status"] == "unused" and row["offline_permit_id"] is not None:
            ticket_counts["held"] += 1

    now = utcnow()
    in_window = False
    if assignment is not None:
        in_window = _in_window(now, assignment["starts_at"], assignment["ends_at"])

    active_permits = [permit_view(conn, r) for r in permit_rows if r["status"] == "active"]
    current_window = None
    if assignment is not None:
        current_window = {
            "venue_name": assignment["venue_name"],
            "venue": assignment["venue_name"],
            "date": assignment["schedule_date"],
            "schedule_date": assignment["schedule_date"],
            "start_minute": assignment["start_minute"],
            "end_minute": assignment["end_minute"],
            "starts_at": assignment["starts_at"],
            "ends_at": assignment["ends_at"],
            "revision": assignment["revision"],
        }
    return {
        "tournament_id": tournament_id,
        "id": tournament_id,
        "slot_id": slot_id,
        "reserved": assignment is not None,
        "schedule": dict(assignment) if assignment is not None else None,
        "current_schedule": dict(assignment) if assignment is not None else None,
        "current_window": current_window,
        "schedule_window": current_window,
        "admission_window": current_window,
        "server_time": now.isoformat(),
        "in_admission_window": in_window,
        "window_active": in_window,
        "capacity_configured": counts["capacity"] is not None,
        "counts": counts,
        "capacity_summary": counts,
        "capacity_status": {
            **counts,
            "limit": counts["capacity"],
            "used": counts["confirmed_admissions"],
            "reserved": counts["outstanding_reserved_capacity"],
            "remaining": counts["remaining_allocatable_capacity"],
        },
        **counts,
        "ticket_counts": ticket_counts,
        "tickets": [ticket_view(r) for r in ticket_rows],
        "codes": [r["code"] for r in ticket_rows],
        "permits": [permit_view(conn, r) for r in permit_rows],
        "offline_permits": [permit_view(conn, r) for r in permit_rows],
        "outstanding_permits": active_permits,
        "admissions": [admission_view(r) for r in admission_rows],
        "confirmed_admission_records": [admission_view(r) for r in admission_rows],
    }


def get_slot_admission(
    conn: sqlite3.Connection, tournament_id: int, slot_id: str
) -> dict[str, Any]:
    with read_tx(conn):
        return build_slot_admission_view(conn, tournament_id, slot_id)


def get_tournament_admission(conn: sqlite3.Connection, tournament_id: int) -> dict[str, Any]:
    service._require_tournament(conn, tournament_id)
    with read_tx(conn):
        slots = [
            build_slot_admission_view(conn, tournament_id, slot_id)
            for slot_id in service.SLOT_ORDER
        ]
        # Remove large duplicate nested arrays from aggregate? Keep them; tests small.
        totals = {
            "capacity": sum(s["capacity"] or 0 for s in slots),
            "confirmed_admissions": sum(s["confirmed_admissions"] for s in slots),
            "outstanding_reserved_capacity": sum(
                s["outstanding_reserved_capacity"] for s in slots
            ),
            "remaining_allocatable_capacity": sum(
                s["remaining_allocatable_capacity"] or 0 for s in slots
            ),
        }
        return {
            "tournament_id": tournament_id,
            "id": tournament_id,
            "slots": slots,
            "admission_by_slot": {s["slot_id"]: s for s in slots},
            "totals": totals,
            **totals,
        }


# ---------------------------------------------------------------------------
# Capacity and gate registration


def set_capacity(
    conn: sqlite3.Connection,
    tournament_id: int | None,
    slot_id: str | None,
    body: dict[str, Any],
    idempotency_key: str | None,
) -> tuple[int, dict[str, Any]]:
    with write_tx(conn):
        tid, slot = _resolve_tournament_slot(conn, body, tournament_id, slot_id)
        key = _key_value(idempotency_key, body)
        canonical = {"kind": "admission-capacity", "tid": tid, "slot": slot, "body": body}
        cached = _idempotent(conn, key, f"admission-capacity:{tid}:{slot}", canonical)
        if cached is not None:
            return cached

        _require_reserved_slot(conn, tid, slot)
        raw_capacity = _first(
            body,
            "capacity",
            "total_capacity",
            "max_capacity",
            "fixed_capacity",
            "admission_capacity",
            "spectator_capacity",
            "audience_capacity",
            "entry_capacity",
            "limit",
            "total_limit",
            "max_attendees",
            "max_people",
            "max_persons",
            "person_capacity",
            "people_limit",
            "attendee_limit",
            "spectator_limit",
            "audience_limit",
            "capacity_limit",
        )
        if raw_capacity is None and isinstance(body.get("capacity"), dict):
            raw_capacity = _first(body["capacity"], "capacity", "limit", "max")
        if isinstance(raw_capacity, dict):
            raw_capacity = _first(raw_capacity, "capacity", "limit", "max", "value")
        capacity = _int_value(raw_capacity, "capacity", minimum=0)
        if capacity is None:
            raise _error(422, "capacity_required", "A non-negative integer capacity is required")
        operator = _clean_text(
            _first(body, "set_by", "updated_by", "staff", "operator", "created_by"),
            "operator",
            required=False,
        ) or "staff"

        counts = capacity_counts(conn, tid, slot)
        committed = counts["confirmed_admissions"] + counts["outstanding_reserved_capacity"]
        if capacity < committed:
            raise _error(
                409,
                "capacity_below_committed",
                "Capacity cannot be lower than confirmed admissions plus unsettled reservations",
                capacity=capacity,
                committed=committed,
                confirmed_admissions=counts["confirmed_admissions"],
                outstanding_reserved_capacity=counts["outstanding_reserved_capacity"],
            )

        timestamp = service.now_iso()
        existing = conn.execute(
            "SELECT * FROM admission_capacities WHERE tournament_id=? AND slot_id=?",
            (tid, slot),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO admission_capacities
                    (tournament_id, slot_id, capacity, set_by, updated_by,
                     idempotency_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, slot, capacity, operator, operator, key, timestamp, timestamp),
            )
            status = 201
        else:
            conn.execute(
                """
                UPDATE admission_capacities
                SET capacity=?, updated_by=?, updated_at=?
                WHERE tournament_id=? AND slot_id=?
                """,
                (capacity, operator, timestamp, tid, slot),
            )
            status = 200

        view = build_slot_admission_view(conn, tid, slot)
        response = {
            "capacity": view["capacity"],
            "capacity_config": {
                "tournament_id": tid,
                "slot_id": slot,
                "capacity": capacity,
                "set_by": operator,
                "updated_at": timestamp,
            },
            "admission": view,
            "summary": view,
        }
        _store(conn, key, f"admission-capacity:{tid}:{slot}", canonical, status, response)
        return status, response


def register_gate(
    conn: sqlite3.Connection,
    body: dict[str, Any],
    idempotency_key: str | None,
    gate_id_path: str | None = None,
) -> tuple[int, dict[str, Any]]:
    with write_tx(conn):
        gate_id = _clean_text(
            gate_id_path or _first(body, "gate_id", "gateId", "gate", "terminal_id", "device_id"),
            "gate_id",
        )
        assert gate_id is not None
        key = _key_value(idempotency_key, body)
        canonical = {"kind": "gate-register", "gate_id": gate_id, "body": body}
        cached = _idempotent(conn, key, f"gate-register:{gate_id}", canonical)
        if cached is not None:
            return cached
        venue = _clean_text(
            _first(body, "venue_name", "venueName", "venue", "facility", "court"),
            "venue_name",
        )
        assert venue is not None
        name = _clean_text(_first(body, "name", "gate_name", "label"), "name", required=False)
        operator = _clean_text(
            _first(body, "registered_by", "created_by", "operator"), "operator", required=False
        )
        timestamp = service.now_iso()
        existing = conn.execute("SELECT * FROM gates WHERE gate_id=?", (gate_id,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO gates (gate_id, venue_name, name, registered_by,
                                   idempotency_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (gate_id, venue, name, operator, key, timestamp, timestamp),
            )
            status = 201
        else:
            if existing["venue_name"] != venue:
                conn.execute(
                    "UPDATE gates SET venue_name=?, name=COALESCE(?, name), updated_at=? WHERE gate_id=?",
                    (venue, name, timestamp, gate_id),
                )
            status = 200
        row = conn.execute("SELECT * FROM gates WHERE gate_id=?", (gate_id,)).fetchone()
        response = {
            "gate_id": row["gate_id"],
            "id": row["gate_id"],
            "venue_name": row["venue_name"],
            "venue": row["venue_name"],
            "name": row["name"],
            "registered_by": row["registered_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        _store(conn, key, f"gate-register:{gate_id}", canonical, status, response)
        return status, response


def get_gate(conn: sqlite3.Connection, gate_id: str) -> dict[str, Any]:
    with read_tx(conn):
        row = conn.execute("SELECT * FROM gates WHERE gate_id=?", (gate_id,)).fetchone()
        if row is None:
            raise _error(404, "gate_not_found", "Gate is not registered", gate_id=gate_id)
        permits = conn.execute(
            "SELECT * FROM offline_permits WHERE gate_id=? ORDER BY id DESC", (gate_id,)
        ).fetchall()
        return {
            "gate_id": row["gate_id"],
            "venue_name": row["venue_name"],
            "venue": row["venue_name"],
            "name": row["name"],
            "registered_by": row["registered_by"],
            "created_at": row["created_at"],
            "permits": [permit_view(conn, p) for p in permits],
            "offline_permits": [permit_view(conn, p) for p in permits],
        }


def _gate_venue(
    conn: sqlite3.Connection,
    gate_id: str | None,
    body_venue: str | None,
    slot_context: tuple[int, str] | None = None,
) -> str | None:
    requested = _clean_text(body_venue, "venue_name", required=False)
    registered_venue = None
    if gate_id:
        gate = conn.execute("SELECT * FROM gates WHERE gate_id=?", (gate_id,)).fetchone()
        if gate is not None:
            registered_venue = gate["venue_name"]
        else:
            query = "SELECT * FROM offline_permits WHERE gate_id=? AND status='active'"
            params: tuple[Any, ...] = (gate_id,)
            if slot_context is not None:
                query += " AND tournament_id=? AND slot_id=?"
                params = (gate_id, slot_context[0], slot_context[1])
            query += " ORDER BY id DESC LIMIT 1"
            permit = conn.execute(query, params).fetchone()
            if permit is not None:
                registered_venue = permit["venue_name"]
            # Settled permits are deliberately not used as the gate's current
            # location: after settlement/rescheduling the trusted gate may be
            # moved or explicitly identify its new venue.

    if requested and registered_venue and requested != registered_venue:
        raise _error(
            409,
            "gate_venue_mismatch",
            "The gate is assigned to a different venue",
            gate_id=gate_id,
            gate_venue=registered_venue,
            requested_venue=requested,
        )
    return registered_venue or requested


# ---------------------------------------------------------------------------
# Ticket issuance, lookup and revocation


def _normalize_code(value: Any) -> str:
    if isinstance(value, dict):
        value = _first(value, "code", "ticket_code", "entry_code", "admission_code", "id")
    text = _clean_text(value, "ticket_code")
    assert text is not None
    return text


def _find_ticket_row(
    conn: sqlite3.Connection,
    ref: str | int | None = None,
    *,
    ticket_id: int | None = None,
    code: str | None = None,
    tournament_id: int | None = None,
    slot_id: str | None = None,
) -> sqlite3.Row:
    if ticket_id is None and code is None and ref is not None:
        text = str(ref).strip()
        scoped_sql = "SELECT * FROM admission_tickets WHERE tournament_id=? AND code=?"
        if tournament_id is not None:
            row = conn.execute(scoped_sql, (tournament_id, text)).fetchone()
            if row is not None:
                return row
        else:
            rows = conn.execute(
                "SELECT * FROM admission_tickets WHERE code=?", (text,)
            ).fetchall()
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1:
                raise _error(
                    409,
                    "ambiguous_ticket_code",
                    "Ticket code is used by more than one tournament; provide tournament_id",
                    ticket_code=text,
                )
        if text.isdigit():
            ticket_id = int(text)

    if ticket_id is not None:
        row = conn.execute(
            "SELECT * FROM admission_tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise _error(404, "ticket_not_found", "Ticket does not exist", ticket_id=ticket_id)
    elif code is not None:
        code = _normalize_code(code)
        if tournament_id is not None:
            row = conn.execute(
                "SELECT * FROM admission_tickets WHERE tournament_id=? AND code=?",
                (tournament_id, code),
            ).fetchone()
        else:
            rows = conn.execute(
                "SELECT * FROM admission_tickets WHERE code=?", (code,)
            ).fetchall()
            if len(rows) == 1:
                row = rows[0]
            elif len(rows) > 1:
                raise _error(
                    409,
                    "ambiguous_ticket_code",
                    "Ticket code is used by more than one tournament; provide tournament_id",
                    ticket_code=code,
                )
            else:
                row = None
        if row is None:
            raise _error(404, "ticket_not_found", "Ticket code does not exist", ticket_code=code)
    else:
        raise _error(422, "ticket_required", "A ticket code or id is required")

    if tournament_id is not None and int(row["tournament_id"]) != int(tournament_id):
        raise _error(404, "ticket_not_found", "Ticket does not belong to this tournament")
    if slot_id is not None and row["slot_id"] != slot_id:
        raise _error(404, "ticket_not_found", "Ticket does not belong to this slot")
    return row


def issue_tickets(
    conn: sqlite3.Connection,
    tournament_id: int | None,
    slot_id: str | None,
    body: dict[str, Any],
    idempotency_key: str | None,
) -> tuple[int, dict[str, Any]]:
    with write_tx(conn):
        tid, slot = _resolve_tournament_slot(conn, body, tournament_id, slot_id)
        key = _key_value(idempotency_key, body)
        canonical = {"kind": "issue-tickets", "tid": tid, "slot": slot, "body": body}
        cached = _idempotent(conn, key, f"issue-tickets:{tid}:{slot}", canonical)
        if cached is not None:
            return cached

        # Admission only exists for a reserved bracket slot.  Capacity itself
        # is deliberately not checked: issued codes do not reserve space.
        _require_reserved_slot(conn, tid, slot)

        explicit = _first(
            body,
            "codes",
            "ticket_codes",
            "entry_codes",
            "admission_codes",
            "pass_codes",
            "qr_codes",
            "barcodes",
            "tokens",
            "code_list",
            "tickets",
        )
        holders = _first(body, "holder_names", "names", "attendees") or []
        if not isinstance(holders, list):
            holders = [holders]
        issued_by = _clean_text(
            _first(body, "issued_by", "created_by", "staff", "operator"),
            "issued_by",
            required=False,
        ) or "staff"

        requested_codes: list[str] = []
        explicit_objects: list[dict[str, Any]] = []
        if explicit is not None:
            if isinstance(explicit, str):
                requested_codes = [_normalize_code(explicit)]
            elif isinstance(explicit, list):
                for item in explicit:
                    if isinstance(item, dict):
                        code = _normalize_code(item)
                        explicit_objects.append(item)
                    else:
                        code = _normalize_code(item)
                    requested_codes.append(code)
            else:
                raise _error(422, "invalid_codes", "codes must be a list or string")

        count = _int_value(
            _first(body, "count", "quantity", "number", "n", "ticket_count", "code_count"),
            "count",
            minimum=1,
            default=None,
        )
        if not requested_codes:
            if count is None:
                count = 1
            if count > 1000:
                raise _error(422, "too_many_tickets", "At most 1000 codes can be issued at once")
            requested_codes = [
                f"T{tid}-{slot}-{secrets.token_urlsafe(6).upper().replace('_', '0').replace('-', '9')}"
                for _ in range(count)
            ]
        elif count is not None and count != len(requested_codes):
            raise _error(
                422,
                "count_codes_mismatch",
                "count must equal the number of explicit codes",
                count=count,
                code_count=len(requested_codes),
            )

        if len(requested_codes) > 1000:
            raise _error(422, "too_many_tickets", "At most 1000 codes can be issued at once")
        if len(set(requested_codes)) != len(requested_codes):
            raise _error(409, "duplicate_ticket_code_in_request", "Ticket codes must be unique")

        existing = conn.execute(
            """
            SELECT code FROM admission_tickets
            WHERE tournament_id=? AND code IN (%s)
            """
            % ",".join("?" for _ in requested_codes),
            (tid, *requested_codes),
        ).fetchall()
        if existing:
            raise _error(
                409,
                "ticket_code_conflict",
                "One or more ticket codes already exist",
                codes=[r["code"] for r in existing],
            )

        timestamp = service.now_iso()
        created: list[dict[str, Any]] = []
        for index, code in enumerate(requested_codes):
            holder = None
            if index < len(holders):
                holder_value = holders[index]
                holder = (
                    _first(holder_value, "name", "holder_name")
                    if isinstance(holder_value, dict)
                    else holder_value
                )
                holder = _clean_text(holder, "holder_name", required=False)
            if index < len(explicit_objects):
                holder = _clean_text(
                    _first(explicit_objects[index], "holder_name", "name", "attendee"),
                    "holder_name",
                    required=False,
                ) or holder
            cur = conn.execute(
                """
                INSERT INTO admission_tickets
                    (tournament_id, slot_id, code, holder_name, status,
                     issued_by, idempotency_key, issued_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'unused', ?, ?, ?, ?, ?)
                """,
                (tid, slot, code, holder, issued_by, key, timestamp, timestamp, timestamp),
            )
            row = conn.execute(
                "SELECT * FROM admission_tickets WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            created.append(ticket_view(row))

        response = {
            "tournament_id": tid,
            "slot_id": slot,
            "issued_count": len(created),
            "count": len(created),
            "tickets": created,
            "codes": [t["code"] for t in created],
            "entry_codes": [t["code"] for t in created],
            "admission_codes": [t["code"] for t in created],
            "access_codes": [t["code"] for t in created],
            "ticket_codes": [t["code"] for t in created],
        }
        _store(conn, key, f"issue-tickets:{tid}:{slot}", canonical, 201, response)
        return 201, response


def revoke_ticket(
    conn: sqlite3.Connection,
    ref: str | None,
    body: dict[str, Any] | None,
    idempotency_key: str | None,
    *,
    tournament_id: int | None = None,
    slot_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    with write_tx(conn):
        key = _key_value(idempotency_key, body)
        # Resolve first so an idempotency key can be scoped to the ticket.
        ticket_id = _first(body, "ticket_id", "id")
        code = _first(body, "code", "ticket_code", "entry_code")
        row = _find_ticket_row(
            conn,
            ref,
            ticket_id=_int_value(ticket_id, "ticket_id", minimum=1, default=None)
            if ticket_id is not None
            else None,
            code=code,
            tournament_id=tournament_id,
            slot_id=slot_id,
        )
        tid, slot, ticket_pk = row["tournament_id"], row["slot_id"], row["id"]
        canonical = {
            "kind": "revoke-ticket",
            "ticket_id": ticket_pk,
            "body": body,
            "ref": ref,
        }
        cached = _idempotent(conn, key, f"revoke-ticket:{ticket_pk}", canonical)
        if cached is not None:
            return cached

        if row["status"] == "revoked":
            raise _error(
                409,
                "ticket_already_revoked",
                "Ticket is already revoked",
                ticket=ticket_view(row),
            )
        if row["status"] == "used":
            raise _error(
                409,
                "ticket_already_admitted",
                "An admitted ticket cannot be revoked",
                ticket=ticket_view(row),
            )

        hold = conn.execute(
            """
            SELECT opt.*, p.gate_id, p.status AS permit_status
            FROM offline_permit_tickets opt
            JOIN offline_permits p ON p.id = opt.permit_id
            WHERE opt.ticket_id=? AND opt.status='reserved'
            """,
            (ticket_pk,),
        ).fetchone()
        if hold is not None:
            raise _error(
                409,
                "ticket_locked_by_offline_permit",
                "Ticket is listed on an unsettled offline permit and cannot be revoked yet",
                permit_id=hold["permit_id"],
                gate_id=hold["gate_id"],
                ticket=ticket_view(row),
            )

        operator = _clean_text(
            _first(body, "revoked_by", "operator", "staff"), "revoked_by", required=False
        ) or "staff"
        reason = _clean_text(
            _first(body, "reason", "revoke_reason", "note"), "reason", required=False
        )
        timestamp = service.now_iso()
        conn.execute(
            """
            UPDATE admission_tickets
            SET status='revoked', revoked_by=?, revoke_reason=?, revoked_at=?,
                offline_permit_id=NULL, updated_at=?
            WHERE id=?
            """,
            (operator, reason, timestamp, timestamp, ticket_pk),
        )
        row = conn.execute("SELECT * FROM admission_tickets WHERE id=?", (ticket_pk,)).fetchone()
        response = {"revoked": True, "ticket": ticket_view(row)}
        _store(conn, key, f"revoke-ticket:{ticket_pk}", canonical, 200, response)
        return 200, response


def get_ticket(
    conn: sqlite3.Connection,
    ref: str,
    tournament_id: int | None = None,
    slot_id: str | None = None,
) -> dict[str, Any]:
    with read_tx(conn):
        row = _find_ticket_row(
            conn, ref, tournament_id=tournament_id, slot_id=slot_id
        )
        result = {"ticket": ticket_view(row), **ticket_view(row)}
        if row["admission_id"]:
            admission = conn.execute(
                "SELECT * FROM admissions WHERE id=?", (row["admission_id"],)
            ).fetchone()
            if admission:
                result["admission"] = admission_view(admission)
        return result


# ---------------------------------------------------------------------------
# Online admission


def admit_online(
    conn: sqlite3.Connection,
    body: dict[str, Any] | None,
    idempotency_key: str | None,
    *,
    tournament_id: int | None = None,
    slot_id: str | None = None,
    gate_id_path: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    with write_tx(conn):
        path_tid = tournament_id or _first(body, "tournament_id", "tournamentId")
        path_slot = slot_id or _first(
            body,
            "slot_id", "slotId", "slot",
            "position_id", "positionId", "position",
            "round_slot", "roundSlot",
        )
        if path_tid is not None and path_slot is not None:
            tid, slot = _resolve_tournament_slot(conn, body, tournament_id, slot_id)
        else:
            tid = _int_value(path_tid, "tournament_id", minimum=1, default=None)
            slot = str(path_slot).strip() if path_slot is not None else None
            if tid is not None:
                service._require_tournament(conn, tid)
            if slot is not None:
                service._require_slot(slot)

        gate_id = _clean_text(
            gate_id_path or _first(body, "gate_id", "gateId", "gate", "terminal_id", "device_id"),
            "gate_id",
            required=False,
        )
        code_value = _first(
            body,
            "code",
            "ticket_code",
            "entry_code",
            "admission_code",
            "pass_code",
            "qr_code",
            "barcode",
            "token",
            "ticket",
        )
        ticket_id = _first(body, "ticket_id", "ticketId", "id")
        if isinstance(code_value, dict):
            ticket_id = _first(code_value, "id", "ticket_id")
            code_value = _first(code_value, "code", "ticket_code", "entry_code")

        row = _find_ticket_row(
            conn,
            code_value if ticket_id is None and code_value is not None else None,
            ticket_id=_int_value(ticket_id, "ticket_id", minimum=1, default=None)
            if ticket_id is not None
            else None,
            code=code_value if ticket_id is None else None,
            tournament_id=tid,
            slot_id=slot,
        )
        tid, slot = int(row["tournament_id"]), row["slot_id"]
        key = _key_value(idempotency_key, body) or (
            # A timeout retry may naturally reuse the check-in key named by
            # the client; no server-generated key is persisted here.
            _clean_text(_first(body, "check_in_id", "checkin_id"), "check_in_id", required=False)
        )
        canonical = {
            "kind": "online-admit",
            "ticket_id": row["id"],
            "gate_id": gate_id,
            "body": body,
        }
        cached = _idempotent(conn, key, f"online-admit:{row['id']}", canonical)
        if cached is not None:
            return cached

        assignment = _require_reserved_slot(conn, tid, slot)

        # Venue identity: explicit/registered/permit venue, with an omitted
        # venue treated as the current venue for operator convenience.  A
        # supplied wrong venue is always rejected.
        body_venue = _first(body, "venue_name", "venueName", "venue", "facility", "court")
        resolved_venue = _gate_venue(conn, gate_id, body_venue, (tid, slot))
        current_venue = assignment["venue_name"]
        if resolved_venue is not None and resolved_venue != current_venue:
            raise _error(
                409,
                "wrong_venue",
                "Online gate is not at the slot's current venue",
                expected_venue=current_venue,
                actual_venue=resolved_venue,
                current_schedule=dict(assignment),
            )

        now = utcnow()
        event_time_value = _first(
            body, "admitted_at", "admittedAt", "at", "timestamp", "time", "verified_at"
        )
        event_now = _parse_utc(event_time_value, "admitted_at") if event_time_value is not None else now
        if not _in_window(event_now, assignment["starts_at"], assignment["ends_at"]):
            raise _error(
                409,
                "outside_admission_window",
                "Online admission is open only during the slot's current scheduled interval",
                server_time=now.isoformat(),
                admission_time=event_now.isoformat(),
                starts_at=assignment["starts_at"],
                ends_at=assignment["ends_at"],
                venue_name=current_venue,
                current_schedule=dict(assignment),
            )

        if row["status"] == "used":
            old = conn.execute(
                "SELECT * FROM admissions WHERE ticket_id=?", (row["id"],)
            ).fetchone()
            raise _error(
                409,
                "ticket_already_admitted",
                "Ticket has already been used for entry",
                ticket=ticket_view(row),
                admission=admission_view(old) if old else None,
            )
        if row["status"] == "revoked":
            raise _error(
                409,
                "ticket_revoked",
                "Ticket has been revoked and cannot be used",
                ticket=ticket_view(row),
            )

        hold = conn.execute(
            """
            SELECT opt.permit_id, p.gate_id, p.status
            FROM offline_permit_tickets opt
            JOIN offline_permits p ON p.id = opt.permit_id
            WHERE opt.ticket_id=? AND opt.status='reserved'
            """,
            (row["id"],),
        ).fetchone()
        if hold is not None:
            raise _error(
                409,
                "ticket_reserved_for_offline_permit",
                "Ticket is locked to an unsettled offline permit and cannot be admitted online",
                permit_id=hold["permit_id"],
                gate_id=hold["gate_id"],
            )

        counts = capacity_counts(conn, tid, slot)
        if counts["capacity"] is not None and counts["remaining_allocatable"] <= 0:
            raise _error(
                409,
                "admission_capacity_reached",
                "No admission capacity remains after confirmed entries and unsettled permits",
                **{
                    k: counts[k]
                    for k in (
                        "capacity",
                        "confirmed_admissions",
                        "outstanding_reserved_capacity",
                        "remaining_allocatable",
                    )
                },
            )

        timestamp = service.now_iso()
        event_iso = event_now.isoformat()
        cur = conn.execute(
            """
            INSERT INTO admissions
                (tournament_id, slot_id, ticket_id, ticket_code, source, status,
                 gate_id, venue_name, admitted_at, recorded_at, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, 'online', 'admitted', ?, ?, ?, ?, ?, ?)
            """,
            (
                tid,
                slot,
                row["id"],
                row["code"],
                gate_id,
                current_venue,
                event_iso,
                timestamp,
                key,
                timestamp,
            ),
        )
        conn.execute(
            """
            UPDATE admission_tickets
            SET status='used', used_at=?, admission_id=?, updated_at=?
            WHERE id=? AND status='unused'
            """,
            (event_iso, cur.lastrowid, timestamp, row["id"]),
        )
        row = conn.execute("SELECT * FROM admission_tickets WHERE id=?", (row["id"],)).fetchone()
        admission = conn.execute(
            "SELECT * FROM admissions WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        fresh = build_slot_admission_view(conn, tid, slot)
        response = {
            "admitted": True,
            "duplicate": False,
            "online": True,
            "admission": admission_view(admission),
            "ticket": ticket_view(row),
            "capacity_summary": fresh,
            "counts": {k: fresh[k] for k in (
                "capacity",
                "confirmed_admissions",
                "outstanding_reserved_capacity",
                "remaining_allocatable_capacity",
            )},
        }
        _store(conn, key, f"online-admit:{row['id']}", canonical, 200, response)
        return 200, response


# ---------------------------------------------------------------------------
# Offline permit claim, query and settlement


def _find_permit_row(
    conn: sqlite3.Connection,
    ref: str | int,
    *,
    tournament_id: int | None = None,
    gate_id: str | None = None,
) -> sqlite3.Row:
    text = str(ref).strip()
    row = conn.execute(
        "SELECT * FROM offline_permits WHERE permit_code=?", (text,)
    ).fetchone()
    if row is None and text.isdigit():
        row = conn.execute("SELECT * FROM offline_permits WHERE id=?", (int(text),)).fetchone()
    if row is None:
        raise _error(404, "offline_permit_not_found", "Offline permit does not exist", permit=text)
    if tournament_id is not None and int(row["tournament_id"]) != int(tournament_id):
        raise _error(404, "offline_permit_not_found", "Permit does not belong to this tournament")
    if gate_id is not None and row["gate_id"] != gate_id:
        raise _error(
            403,
            "permit_gate_mismatch",
            "Offline permit belongs to a different gate",
            permit_id=row["id"],
            permit_gate_id=row["gate_id"],
            gate_id=gate_id,
        )
    return row


def list_permits(
    conn: sqlite3.Connection,
    tournament_id: int | None = None,
    slot_id: str | None = None,
    gate_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if tournament_id is not None:
        clauses.append("tournament_id=?")
        params.append(tournament_id)
    if slot_id is not None:
        clauses.append("slot_id=?")
        params.append(slot_id)
    if gate_id is not None:
        clauses.append("gate_id=?")
        params.append(gate_id)
    if status in ("active", "settled", "outstanding", "unsettled"):
        clauses.append("status=?")
        params.append("active" if status in ("active", "outstanding", "unsettled") else "settled")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with read_tx(conn):
        rows = conn.execute(
            f"SELECT * FROM offline_permits{where} ORDER BY id DESC", params
        ).fetchall()
        permits = [permit_view(conn, row) for row in rows]
        return {
            "permits": permits,
            "offline_permits": permits,
            "count": len(permits),
            "active_count": sum(1 for p in permits if p["status"] == "active"),
        }


def get_permit(
    conn: sqlite3.Connection,
    ref: str | int,
    *,
    tournament_id: int | None = None,
    gate_id: str | None = None,
) -> dict[str, Any]:
    with read_tx(conn):
        row = _find_permit_row(
            conn, ref, tournament_id=tournament_id, gate_id=gate_id
        )
        result = permit_view(conn, row)
        if row["status"] == "settled":
            settlement = conn.execute(
                "SELECT * FROM offline_settlements WHERE permit_id=?", (row["id"],)
            ).fetchone()
            if settlement is not None:
                result["settlement"] = json.loads(settlement["response_json"] or "{}")
        return result


def claim_permit(
    conn: sqlite3.Connection,
    tournament_id: int | None,
    slot_id: str | None,
    body: dict[str, Any],
    idempotency_key: str | None,
    *,
    gate_id_path: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    with write_tx(conn):
        tid, slot = _resolve_tournament_slot(conn, body, tournament_id, slot_id)
        gate_id = _clean_text(
            gate_id_path
            or _first(body, "gate_id", "gateId", "gate", "terminal_id", "device_id"),
            "gate_id",
        )
        assert gate_id is not None
        key = _key_value(idempotency_key, body) or _clean_text(
            _first(body, "claim_id", "permit_request_id"), "claim_id", False
        )
        canonical = {"kind": "claim-offline-permit", "tid": tid, "slot": slot, "body": body}
        cached = _idempotent(conn, key, f"claim-offline-permit:{tid}:{slot}:{gate_id}", canonical)
        if cached is not None:
            return cached

        assignment = _require_reserved_slot(conn, tid, slot)
        claim_now = utcnow()
        if claim_now >= datetime.fromisoformat(assignment["ends_at"]):
            raise _error(
                409,
                "offline_permit_window_ended",
                "An offline permit must be collected before the claimed schedule interval ends",
                server_time=claim_now.isoformat(),
                ends_at=assignment["ends_at"],
            )

        # If the caller snapshot says which schedule it expected, enforce it.
        expected = _first(body, "expected_schedule", "expected", "schedule_snapshot", "window")
        if isinstance(expected, dict):
            checks = {
                "venue_name": _first(expected, "venue_name", "venue", "venueName"),
                "schedule_date": _first(expected, "date", "schedule_date", "day"),
                "starts_at": _first(expected, "starts_at", "start", "startAt"),
                "ends_at": _first(expected, "ends_at", "end", "endAt"),
                "revision": _first(expected, "revision", "schedule_revision"),
            }
            if checks["venue_name"] is not None and str(checks["venue_name"]) != assignment["venue_name"]:
                raise _error(409, "schedule_changed", "The slot schedule has changed", current=dict(assignment))
            if checks["schedule_date"] is not None and str(checks["schedule_date"]) != assignment["schedule_date"]:
                raise _error(409, "schedule_changed", "The slot date has changed", current=dict(assignment))
            if checks["starts_at"] is not None and str(checks["starts_at"]) != assignment["starts_at"]:
                raise _error(409, "schedule_changed", "The slot start has changed", current=dict(assignment))
            if checks["ends_at"] is not None and str(checks["ends_at"]) != assignment["ends_at"]:
                raise _error(409, "schedule_changed", "The slot end has changed", current=dict(assignment))
            if checks["revision"] is not None and _int_value(checks["revision"], "revision") != int(assignment["revision"]):
                raise _error(409, "schedule_revision_changed", "The schedule revision has changed", current=dict(assignment))

        raw_codes = _first(
            body,
            "ticket_codes",
            "codes",
            "entry_codes",
            "admission_codes",
            "pass_codes",
            "qr_codes",
            "barcodes",
            "tokens",
            "tickets",
            "code_list",
        )
        raw_ticket_ids = _first(body, "ticket_ids", "ticketIds", "ids") or []
        refs: list[Any] = []
        if isinstance(raw_codes, str):
            refs.append(raw_codes)
        elif isinstance(raw_codes, list):
            refs.extend(raw_codes)
        if isinstance(raw_ticket_ids, list):
            refs.extend(raw_ticket_ids)
        if not refs:
            raise _error(422, "permit_codes_required", "At least one unused ticket code is required")

        normalized_refs: list[str] = []
        for ref in refs:
            if isinstance(ref, dict):
                normalized_refs.append(str(_normalize_code(ref)))
            else:
                text = _clean_text(ref, "ticket_code")
                assert text is not None
                normalized_refs.append(text)
        if len(set(normalized_refs)) != len(normalized_refs):
            raise _error(409, "duplicate_code_in_permit", "The permit code list contains duplicates")

        tickets: list[sqlite3.Row] = []
        for ref in normalized_refs:
            ticket = _find_ticket_row(conn, ref, tournament_id=tid, slot_id=slot)
            tickets.append(ticket)
        if len({t["id"] for t in tickets}) != len(tickets):
            raise _error(409, "duplicate_code_in_permit", "The permit code list contains duplicates")

        unavailable = []
        for ticket in tickets:
            if ticket["status"] != "unused":
                unavailable.append({"code": ticket["code"], "status": ticket["status"]})
            elif ticket["offline_permit_id"] is not None:
                unavailable.append(
                    {
                        "code": ticket["code"],
                        "status": "held",
                        "permit_id": ticket["offline_permit_id"],
                    }
                )
            else:
                hold = conn.execute(
                    """
                    SELECT permit_id FROM offline_permit_tickets
                    WHERE ticket_id=? AND status='reserved'
                    """,
                    (ticket["id"],),
                ).fetchone()
                if hold is not None:
                    unavailable.append(
                        {"code": ticket["code"], "status": "held", "permit_id": hold["permit_id"]}
                    )
        if unavailable:
            raise _error(
                409,
                "tickets_not_available_for_permit",
                "All permit codes must be unused and not locked by another unsettled permit",
                unavailable=unavailable,
            )

        release_limit = _int_value(
            _first(
                body,
                "release_limit",
                "allowed_count",
                "admission_limit",
                "admission_count",
                "max_admissions",
                "max_entries",
                "entry_limit",
                "people_count",
                "person_count",
                "release_count",
                "redeemable_count",
                "quota",
                "places",
                "reserved_places",
                "reserved_count",
                "count",
            ),
            "release_limit",
            minimum=0,
            default=len(tickets),
        )
        assert release_limit is not None
        if release_limit > len(tickets):
            raise _error(
                422,
                "release_limit_exceeds_codes",
                "The releasable person count cannot exceed the number of listed codes",
                release_limit=release_limit,
                listed_code_count=len(tickets),
            )

        counts = capacity_counts(conn, tid, slot)
        remaining = counts["remaining_allocatable_capacity"]
        if remaining is not None and release_limit > remaining:
            raise _error(
                409,
                "admission_capacity_exceeded",
                "Not enough remaining allocatable capacity for this offline permit",
                release_limit=release_limit,
                remaining_allocatable=remaining,
                **{
                    k: counts[k]
                    for k in (
                        "capacity",
                        "confirmed_admissions",
                        "outstanding_reserved_capacity",
                    )
                },
            )

        operator = _clean_text(
            _first(body, "requested_by", "claimed_by", "operator", "staff"),
            "requested_by",
            required=False,
        ) or "staff"
        timestamp = service.now_iso()
        permit_code = f"OP-{secrets.token_urlsafe(9).upper().replace('_', '0').replace('-', '9')}"
        cur = conn.execute(
            """
            INSERT INTO offline_permits
                (permit_code, tournament_id, slot_id, gate_id, venue_name,
                 schedule_date, start_minute, end_minute, starts_at, ends_at,
                 release_limit, listed_code_count, status, requested_by,
                 idempotency_key, request_json, request_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (
                permit_code,
                tid,
                slot,
                gate_id,
                assignment["venue_name"],
                assignment["schedule_date"],
                assignment["start_minute"],
                assignment["end_minute"],
                assignment["starts_at"],
                assignment["ends_at"],
                release_limit,
                len(tickets),
                operator,
                key,
                json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                service._request_hash(canonical),
                timestamp,
            ),
        )
        pid = int(cur.lastrowid)
        for ticket in tickets:
            conn.execute(
                """
                INSERT INTO offline_permit_tickets
                    (permit_id, ticket_id, status, created_at, updated_at)
                VALUES (?, ?, 'reserved', ?, ?)
                """,
                (pid, ticket["id"], timestamp, timestamp),
            )
            conn.execute(
                "UPDATE admission_tickets SET offline_permit_id=?, updated_at=? WHERE id=?",
                (pid, timestamp, ticket["id"]),
            )

        row = conn.execute("SELECT * FROM offline_permits WHERE id=?", (pid,)).fetchone()
        permit = permit_view(conn, row)
        summary = build_slot_admission_view(conn, tid, slot)
        response = {
            "permit": permit,
            "offline_permit": permit,
            "id": pid,
            "permit_id": pid,
            "permit_code": permit_code,
            "reserved": True,
            "capacity_reserved": release_limit,
            "summary": summary,
            "capacity_summary": {
                k: summary[k]
                for k in (
                    "capacity",
                    "confirmed_admissions",
                    "outstanding_reserved_capacity",
                    "remaining_allocatable_capacity",
                )
            },
        }
        _store(conn, key, canonical["kind"] and f"claim-offline-permit:{tid}:{slot}:{gate_id}", canonical, 201, response)
        return 201, response


def _normalize_settlement_records(
    raw_records: Any,
) -> list[dict[str, Any]]:
    if raw_records is None:
        return []
    if isinstance(raw_records, dict):
        raw_records = [raw_records]
    if not isinstance(raw_records, list):
        raise _error(422, "invalid_settlement_records", "records must be a list")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_records:
        if isinstance(item, str):
            code = item.strip()
            admitted_at = None
            gate_id = None
            venue = None
            ticket_id = None
        elif isinstance(item, dict):
            code_value = _first(
                item, "code", "ticket_code", "entry_code", "admission_code", "pass_code",
                "qr_code", "barcode", "token", "ticket",
            )
            if isinstance(code_value, dict):
                ticket_id = _first(code_value, "id", "ticket_id")
                code_value = _first(code_value, "code", "ticket_code", "entry_code")
            else:
                ticket_id = _first(item, "ticket_id", "ticketId", "id")
            code = str(code_value).strip() if code_value is not None else ""
            time_value = _first(
                item, "admitted_at", "admittedAt", "at", "timestamp", "time",
                "occurred_at", "occurredAt", "used_at", "usedAt",
            )
            admitted_at = _parse_utc(time_value, "admitted_at").isoformat() if time_value else None
            gate_id = _clean_text(
                _first(item, "gate_id", "gateId", "gate", "terminal_id"),
                "record_gate_id",
                required=False,
            )
            venue = _clean_text(
                _first(item, "venue_name", "venue", "venueName"), "venue_name", required=False
            )
            ticket_id = _int_value(ticket_id, "ticket_id", minimum=1, default=None) if ticket_id is not None else None
        else:
            raise _error(422, "invalid_settlement_record", "Each record must be a code or object")
        if not code and ticket_id is None:
            raise _error(422, "ticket_code_required", "Each settlement record needs a ticket code")
        identity = code or f"id:{ticket_id}"
        if identity in seen:
            raise _error(
                409,
                "duplicate_code_in_settlement",
                "The complete report admits the same ticket more than once",
                ticket_code=code or None,
                ticket_id=ticket_id,
            )
        seen.add(identity)
        records.append(
            {
                "code": code,
                "ticket_id": ticket_id,
                "admitted_at": admitted_at,
                "gate_id": gate_id,
                "venue_name": venue,
            }
        )
    records.sort(key=lambda r: (r["code"], r["ticket_id"] or 0))
    return records


def settle_permit(
    conn: sqlite3.Connection,
    ref: str | int,
    body: dict[str, Any] | None,
    idempotency_key: str | None,
    *,
    tournament_id: int | None = None,
    gate_id_path: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body or {}
    raw_records = _first(
        body,
        "records",
        "admissions",
        "entries",
        "used_codes",
        "redeemed_codes",
        "redemptions",
        "checkins",
        "check_ins",
    )
    if raw_records is None and any(k in body for k in ("codes", "ticket_codes", "used", "admitted_codes")):
        raw_records = _first(body, "codes", "ticket_codes", "used", "admitted_codes")
    records = _normalize_settlement_records(raw_records)
    report_id = _clean_text(
        _first(body, "report_id", "settlement_id", "receipt_id", "submission_id"),
        "report_id",
        required=False,
    )
    header_or_body_key = _key_value(idempotency_key, body)
    key = header_or_body_key or report_id
    submitted_gate = _clean_text(
        gate_id_path or _first(body, "gate_id", "gateId", "gate", "terminal_id"),
        "gate_id",
        required=False,
    )

    with write_tx(conn):
        permit = _find_permit_row(conn, ref, tournament_id=tournament_id, gate_id=submitted_gate)
        pid = int(permit["id"])
        if submitted_gate is not None and submitted_gate != permit["gate_id"]:
            # _find_permit_row already enforces when gate argument passed.
            raise _error(403, "permit_gate_mismatch", "Wrong gate for this permit")

        # Keep an immutable hash representation: validation resolves and adds
        # ticket_id to the working records, but that must not change the
        # idempotency fingerprint of the client's request.
        canonical_records = [dict(record) for record in records]
        canonical = {
            "kind": "offline-settlement",
            "permit_id": pid,
            "report_id": report_id,
            "gate_id": submitted_gate or permit["gate_id"],
            "records": canonical_records,
        }
        scope = f"offline-settlement:{pid}"
        cached = _idempotent(conn, key, scope, canonical)
        if cached is not None:
            return cached

        if permit["status"] == "settled":
            old = conn.execute(
                "SELECT * FROM offline_settlements WHERE permit_id=?", (pid,)
            ).fetchone()
            old_hash = old["request_hash"] if old is not None else permit["settlement_request_hash"]
            if old_hash == service._request_hash(canonical):
                stored = permit["settlement_response_json"] or (
                    old["response_json"] if old is not None else "{}"
                )
                return 200, json.loads(stored)
            raise _error(
                409,
                "settlement_conflict",
                "Permit is already settled with different records; the original receipt cannot be overwritten",
                permit_id=pid,
                original_receipt=json.loads(permit["settlement_response_json"] or "{}"),
            )

        if len(records) > int(permit["release_limit"]):
            raise _error(
                409,
                "permit_limit_exceeded",
                "The offline report contains more admissions than the permit allows",
                record_count=len(records),
                release_limit=permit["release_limit"],
            )

        permit_tickets = conn.execute(
            """
            SELECT t.* FROM offline_permit_tickets opt
            JOIN admission_tickets t ON t.id = opt.ticket_id
            WHERE opt.permit_id=?
            """,
            (pid,),
        ).fetchall()
        by_code = {r["code"]: r for r in permit_tickets}
        by_id = {r["id"]: r for r in permit_tickets}
        event_time = service.now_iso()
        seen_settlement_ticket_ids: set[int] = set()

        for record in records:
            if record["gate_id"] and record["gate_id"] != permit["gate_id"]:
                raise _error(
                    403,
                    "record_gate_mismatch",
                    "An admission record was made by a different gate",
                    ticket_code=record["code"],
                    expected_gate_id=permit["gate_id"],
                    actual_gate_id=record["gate_id"],
                )
            if record["venue_name"] and record["venue_name"] != permit["venue_name"]:
                raise _error(
                    409,
                    "record_venue_mismatch",
                    "An admission record is outside the permit venue",
                    ticket_code=record["code"],
                    expected_venue=permit["venue_name"],
                    actual_venue=record["venue_name"],
                )
            if record["admitted_at"] and not _in_window(
                _parse_utc(record["admitted_at"]), permit["starts_at"], permit["ends_at"]
            ):
                raise _error(
                    409,
                    "admission_outside_permit_window",
                    "An offline admission timestamp is outside the original permit venue/time window",
                    ticket_code=record["code"],
                    admitted_at=record["admitted_at"],
                    starts_at=permit["starts_at"],
                    ends_at=permit["ends_at"],
                    venue_name=permit["venue_name"],
                )
            ticket = None
            if record["code"]:
                ticket = by_code.get(record["code"])
            elif record["ticket_id"] is not None:
                ticket = by_id.get(record["ticket_id"])
            if ticket is None:
                raise _error(
                    403,
                    "ticket_not_in_permit",
                    "Report contains a ticket that is not listed on this permit",
                    ticket_code=record["code"] or None,
                    ticket_id=record["ticket_id"],
                    permit_id=pid,
                )
            record["ticket_id"] = int(ticket["id"])
            record["code"] = ticket["code"]
            if record["ticket_id"] in seen_settlement_ticket_ids:
                raise _error(
                    409,
                    "duplicate_code_in_settlement",
                    "The complete report admits the same ticket more than once",
                    ticket_code=ticket["code"],
                    ticket_id=ticket["id"],
                    permit_id=pid,
                )
            seen_settlement_ticket_ids.add(record["ticket_id"])
            existing_admission = conn.execute(
                "SELECT id FROM admissions WHERE ticket_id=?", (ticket["id"],)
            ).fetchone()
            if existing_admission is not None or ticket["status"] == "used":
                raise _error(
                    409,
                    "ticket_already_admitted",
                    "Report contains a ticket that has already been admitted",
                    ticket_code=ticket["code"],
                    permit_id=pid,
                )
            if ticket["status"] == "revoked":
                raise _error(
                    409,
                    "ticket_revoked",
                    "Report contains a revoked ticket",
                    ticket_code=ticket["code"],
                )

        counts = capacity_counts(conn, permit["tournament_id"], permit["slot_id"])
        other_reserved = max(0, counts["outstanding_reserved_capacity"] - int(permit["release_limit"]))
        final_admitted = counts["confirmed_admissions"] + len(records)
        if counts["capacity"] is not None and final_admitted + other_reserved > counts["capacity"]:
            raise _error(
                409,
                "admission_capacity_exceeded",
                "Settlement would exceed the slot capacity",
                capacity=counts["capacity"],
                confirmed_admissions=counts["confirmed_admissions"],
                reported_admissions=len(records),
                other_outstanding_reserved=other_reserved,
            )

        timestamp = service.now_iso()
        request_hash = service._request_hash(canonical)
        cur = conn.execute(
            """
            INSERT INTO offline_settlements
                (permit_id, report_id, tournament_id, slot_id, gate_id,
                 record_count, admitted_count, released_count, request_hash,
                 idempotency_key, request_json, response_json, submitted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, '{}', ?, ?)
            """,
            (
                pid,
                report_id or key,
                permit["tournament_id"],
                permit["slot_id"],
                permit["gate_id"],
                len(records),
                len(records),
                request_hash,
                key,
                json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                timestamp,
                timestamp,
            ),
        )
        settlement_id = int(cur.lastrowid)

        admission_views: list[dict[str, Any]] = []
        for record in records:
            admitted_at = record["admitted_at"] or timestamp
            cur_adm = conn.execute(
                """
                INSERT INTO admissions
                    (tournament_id, slot_id, ticket_id, ticket_code, source, status,
                     gate_id, venue_name, admitted_at, recorded_at, permit_id,
                     settlement_id, idempotency_key, created_at)
                VALUES (?, ?, ?, ?, 'offline', 'admitted', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    permit["tournament_id"],
                    permit["slot_id"],
                    record["ticket_id"],
                    record["code"],
                    permit["gate_id"],
                    permit["venue_name"],
                    admitted_at,
                    timestamp,
                    pid,
                    settlement_id,
                    key,
                    timestamp,
                ),
            )
            conn.execute(
                """
                UPDATE admission_tickets
                SET status='used', used_at=?, admission_id=?, updated_at=?
                WHERE id=? AND status='unused'
                """,
                (admitted_at, cur_adm.lastrowid, timestamp, record["ticket_id"]),
            )
            conn.execute(
                """
                UPDATE offline_permit_tickets
                SET status='used', updated_at=?
                WHERE permit_id=? AND ticket_id=?
                """,
                (timestamp, pid, record["ticket_id"]),
            )
            adm_row = conn.execute(
                "SELECT * FROM admissions WHERE id=?", (cur_adm.lastrowid,)
            ).fetchone()
            admission_views.append(admission_view(adm_row))

        conn.execute(
            """
            UPDATE offline_permit_tickets
            SET status='released', updated_at=?
            WHERE permit_id=? AND status='reserved'
            """,
            (timestamp, pid),
        )
        released_rows = conn.execute(
            "SELECT ticket_id FROM offline_permit_tickets WHERE permit_id=? AND status='released'",
            (pid,),
        ).fetchall()
        released_count = len(released_rows)
        if released_rows:
            conn.executemany(
                """
                UPDATE admission_tickets
                SET offline_permit_id=NULL, updated_at=?
                WHERE id=?
                """,
                [(timestamp, r["ticket_id"]) for r in released_rows],
            )

        conn.execute(
            """
            UPDATE offline_permits
            SET status='settled', report_id=?, settlement_key=?,
                actual_admitted_count=?, released_code_count=?,
                settlement_request_hash=?, settled_at=?
            WHERE id=?
            """,
            (
                report_id or key,
                key,
                len(records),
                released_count,
                request_hash,
                timestamp,
                pid,
            ),
        )
        conn.execute(
            "UPDATE offline_settlements SET released_count=? WHERE id=?",
            (released_count, settlement_id),
        )

        permit_row = conn.execute("SELECT * FROM offline_permits WHERE id=?", (pid,)).fetchone()
        permit_payload = permit_view(conn, permit_row)
        summary = build_slot_admission_view(
            conn, int(permit["tournament_id"]), permit["slot_id"]
        )
        receipt = {
            "receipt_id": key or f"settlement-{settlement_id}",
            "settlement_id": settlement_id,
            "report_id": report_id or key,
            "permit_id": pid,
            "permit_code": permit["permit_code"],
            "gate_id": permit["gate_id"],
            "status": "settled",
            "settled": True,
            "settled_at": timestamp,
            "record_count": len(records),
            "admitted_count": len(records),
            "actual_admitted_count": len(records),
            "released_code_count": released_count,
            "released_unused_code_count": released_count,
            "admissions": admission_views,
        }
        response = {
            "settled": True,
            "status": "settled",
            "receipt": receipt,
            "settlement": receipt,
            **receipt,
            "permit": permit_payload,
            "capacity_summary": {
                k: summary[k]
                for k in (
                    "capacity",
                    "confirmed_admissions",
                    "outstanding_reserved_capacity",
                    "remaining_allocatable_capacity",
                )
            },
            "summary": summary,
        }
        conn.execute(
            "UPDATE offline_settlements SET response_json=? WHERE id=?",
            (json.dumps(response, ensure_ascii=False, separators=(",", ":")), settlement_id),
        )
        conn.execute(
            "UPDATE offline_permits SET settlement_response_json=? WHERE id=?",
            (json.dumps(response, ensure_ascii=False, separators=(",", ":")), pid),
        )
        _store(conn, key, scope, canonical, 200, response)
        return 200, response


def request_terminate_permit(
    conn: sqlite3.Connection,
    ref: str | int,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    *,
    tournament_id: int | None = None,
    gate_id_path: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Record that a gate has durably stopped local use.

    This is intentionally not settlement: capacity remains reserved and the
    permit status stays ``active`` until the complete report is accepted.
    """

    body = body or {}
    gate_id = _clean_text(
        gate_id_path or _first(body, "gate_id", "gateId", "gate"),
        "gate_id",
        required=False,
    )
    with write_tx(conn):
        row = _find_permit_row(conn, ref, tournament_id=tournament_id, gate_id=gate_id)
        if row["status"] == "settled":
            return 200, {
                "terminate_requested": True,
                "already_settled": True,
                "settled": True,
                "permit": permit_view(conn, row),
            }
        timestamp = service.now_iso()
        if row["terminate_requested_at"] is None:
            conn.execute(
                "UPDATE offline_permits SET terminate_requested_at=? WHERE id=?",
                (timestamp, row["id"]),
            )
            row = conn.execute("SELECT * FROM offline_permits WHERE id=?", (row["id"],)).fetchone()
        view = permit_view(conn, row)
        return 200, {
            "terminate_requested": True,
            "locally_terminated": True,
            "settled": False,
            "status": "active",
            "settlement_status": "unsettled",
            "terminate_requested_at": row["terminate_requested_at"],
            "permit": view,
        }


def get_settlement(
    conn: sqlite3.Connection, ref: str
) -> dict[str, Any]:
    with read_tx(conn):
        row = conn.execute(
            "SELECT * FROM offline_settlements WHERE report_id=? OR id=?",
            (ref, int(ref) if ref.isdigit() else -1),
        ).fetchone()
        if row is None:
            # Allow lookup by idempotency key.
            idem = conn.execute(
                "SELECT * FROM idempotency_keys WHERE `key`=?", (ref,)
            ).fetchone()
            if idem is not None and "offline-settlement" in idem["scope"]:
                return json.loads(idem["response_json"])
            raise _error(404, "settlement_not_found", "Settlement receipt does not exist")
        return json.loads(row["response_json"] or "{}")


# ---------------------------------------------------------------------------
# HTTP routes.  Many aliases are provided because the domain language in the
# field uses tickets/codes/permits/check-ins almost interchangeably.


def _response(status_payload: tuple[int, dict[str, Any]]) -> JSONResponse:
    status, payload = status_payload
    return _ok(payload, status)


def _capacity_endpoint(
    tournament_id: int | None = None,
    slot_id: str | None = None,
    body: Any = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(request_conn),
) -> JSONResponse:
    data = body if isinstance(body, dict) else ({"capacity": body} if body is not None else {})
    code = _first(
        data,
        "code", "ticket_code", "entry_code", "admission_code",
        "pass_code", "qr_code", "barcode", "token",
    )
    has_capacity = _first(
        data,
        "capacity", "total_capacity", "max_capacity", "admission_capacity",
        "spectator_capacity", "audience_capacity", "limit", "max_attendees",
    ) is not None
    # POST .../admission can mean either "configure capacity" or "create an
    # admission".  Dispatch on the distinctive fields.
    if code is not None and not has_capacity:
        return _response(
            admit_online(
                conn, data, idempotency_key,
                tournament_id=tournament_id, slot_id=slot_id,
            )
        )
    return _response(set_capacity(conn, tournament_id, slot_id, data, idempotency_key))


def _capacity_get_endpoint(
    tournament_id: int | None = None,
    slot_id: str | None = None,
    conn: sqlite3.Connection = Depends(request_conn),
) -> dict[str, Any]:
    if tournament_id is None:
        raise _error(422, "tournament_id_required", "tournament_id is required")
    if slot_id is None:
        return get_tournament_admission(conn, tournament_id)
    return get_slot_admission(conn, tournament_id, slot_id)


def _tournament_admission_endpoint(
    tournament_id: int,
    conn: sqlite3.Connection = Depends(request_conn),
) -> dict[str, Any]:
    return get_tournament_admission(conn, tournament_id)


def _issue_endpoint(
    tournament_id: int | None = None,
    slot_id: str | None = None,
    body: Any = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(request_conn),
) -> JSONResponse:
    if isinstance(body, list):
        data = {"codes": body}
    elif body is None:
        data = {}
    elif isinstance(body, dict):
        data = body
    else:
        data = {"count": body}
    return _response(issue_tickets(conn, tournament_id, slot_id, data, idempotency_key))


def _list_tickets_endpoint(
    tournament_id: int | None = None,
    slot_id: str | None = None,
    code: str | None = None,
    ticket_id: int | None = None,
    conn: sqlite3.Connection = Depends(request_conn),
) -> dict[str, Any]:
    if code is not None or ticket_id is not None:
        return get_ticket(
            conn,
            code or str(ticket_id),
            tournament_id=tournament_id,
            slot_id=slot_id,
        )
    if tournament_id is None:
        # Small operational listing endpoint.  The normal API is scoped by
        # tournament; this mainly supports generic /tickets callers.
        with read_tx(conn):
            rows = conn.execute(
                "SELECT * FROM admission_tickets ORDER BY tournament_id, slot_id, id"
            ).fetchall()
            return {"tickets": [ticket_view(r) for r in rows], "count": len(rows)}
    if slot_id:
        return get_slot_admission(conn, tournament_id, slot_id)
    return get_tournament_admission(conn, tournament_id)


def _revoke_endpoint(
    ticket_ref: str | None = None,
    tournament_id: int | None = None,
    slot_id: str | None = None,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(request_conn),
) -> JSONResponse:
    data = dict(body or {})
    if ticket_ref and _first(data, "code", "ticket_code", "entry_code", "ticket_id", "id") is None:
        data["code"] = ticket_ref
    return _response(
        revoke_ticket(conn, ticket_ref, data, idempotency_key,
                      tournament_id=tournament_id, slot_id=slot_id)
    )


def _ticket_get_endpoint(
    ticket_ref: str,
    tournament_id: int | None = None,
    slot_id: str | None = None,
    conn: sqlite3.Connection = Depends(request_conn),
) -> dict[str, Any]:
    return get_ticket(conn, ticket_ref, tournament_id, slot_id)


def _online_endpoint(
    tournament_id: int | None = None,
    slot_id: str | None = None,
    gate_id: str | None = None,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(request_conn),
) -> JSONResponse:
    return _response(
        admit_online(
            conn, body or {}, idempotency_key,
            tournament_id=tournament_id, slot_id=slot_id, gate_id_path=gate_id,
        )
    )


def _gate_register_endpoint(
    gate_id: str | None = None,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(request_conn),
) -> JSONResponse:
    return _response(register_gate(conn, body or {}, idempotency_key, gate_id))


def _gate_get_endpoint(
    gate_id: str,
    conn: sqlite3.Connection = Depends(request_conn),
) -> dict[str, Any]:
    return get_gate(conn, gate_id)


def _claim_endpoint(
    tournament_id: int | None = None,
    slot_id: str | None = None,
    gate_id: str | None = None,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(request_conn),
) -> JSONResponse:
    return _response(
        claim_permit(
            conn, tournament_id, slot_id, body or {}, idempotency_key,
            gate_id_path=gate_id,
        )
    )


def _permit_get_endpoint(
    permit_ref: str,
    tournament_id: int | None = None,
    gate_id: str | None = None,
    conn: sqlite3.Connection = Depends(request_conn),
) -> dict[str, Any]:
    return get_permit(conn, permit_ref, tournament_id=tournament_id, gate_id=gate_id)


def _permit_list_endpoint(
    tournament_id: int | None = None,
    slot_id: str | None = None,
    gate_id: str | None = None,
    status: str | None = None,
    conn: sqlite3.Connection = Depends(request_conn),
) -> dict[str, Any]:
    return list_permits(conn, tournament_id, slot_id, gate_id, status)


def _settle_endpoint(
    permit_ref: str,
    tournament_id: int | None = None,
    gate_id: str | None = None,
    body: Any = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(request_conn),
) -> JSONResponse:
    if isinstance(body, list):
        data: dict[str, Any] = {"records": body}
    elif body is None:
        data = {}
    elif isinstance(body, dict):
        data = body
    else:
        data = {"records": body}
    return _response(
        settle_permit(
            conn, permit_ref, data, idempotency_key,
            tournament_id=tournament_id, gate_id_path=gate_id,
        )
    )


def _terminate_endpoint(
    permit_ref: str,
    tournament_id: int | None = None,
    gate_id: str | None = None,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    conn: sqlite3.Connection = Depends(request_conn),
) -> JSONResponse:
    return _response(
        request_terminate_permit(
            conn, permit_ref, body or {}, idempotency_key,
            tournament_id=tournament_id, gate_id_path=gate_id,
        )
    )


def _settlement_get_endpoint(
    settlement_ref: str,
    conn: sqlite3.Connection = Depends(request_conn),
) -> dict[str, Any]:
    return get_settlement(conn, settlement_ref)


_SLOT = "/tournaments/{tournament_id}/slots/{slot_id}"

for path in [
    f"{_SLOT}/admission",
    f"{_SLOT}/admissions",
    f"{_SLOT}/admission-capacity",
    f"{_SLOT}/capacity",
    f"{_SLOT}/admission/capacity",
    f"{_SLOT}/admission-settings",
    f"{_SLOT}/admission_config",
    f"{_SLOT}/spectator-capacity",
    f"{_SLOT}/audience-capacity",
    f"{_SLOT}/entry-capacity",
    f"{_SLOT}/tickets/capacity",
    "/tournaments/{tournament_id}/admission",
    "/tournaments/{tournament_id}/admissions",
    "/tournaments/{tournament_id}/admission-settings",
    "/tournaments/{tournament_id}/capacity/{slot_id}",
    "/tournaments/{tournament_id}/capacity",
    "/tournaments/{tournament_id}/admission-capacity",
    "/tournaments/{tournament_id}/admission/capacity",
    "/slots/{slot_id}/tournaments/{tournament_id}/capacity",
    "/capacity",
    "/admission-capacity",
]:
    router.add_api_route(path, _capacity_endpoint, methods=["POST", "PUT", "PATCH"], status_code=201)

for path in [
    f"{_SLOT}/admission",
    f"{_SLOT}/admissions",
    f"{_SLOT}/admission-summary",
    f"{_SLOT}/admission_summary",
    f"{_SLOT}/entry",
    f"{_SLOT}/check-in",
    f"{_SLOT}/checkins",
    f"{_SLOT}/capacity",
    f"{_SLOT}/tickets",
    f"{_SLOT}/entry-codes",
    f"{_SLOT}/admission-codes",
]:
    router.add_api_route(path, _capacity_get_endpoint, methods=["GET"])

for path in [
    "/tournaments/{tournament_id}/admission",
    "/tournaments/{tournament_id}/admissions",
    "/tournaments/{tournament_id}/admission-summary",
    "/tournaments/{tournament_id}/admission-status",
    "/tournaments/{tournament_id}/capacity",
    "/tournaments/{tournament_id}/admission-capacity",
    "/admission/tournaments/{tournament_id}",
    "/admissions/tournaments/{tournament_id}",
]:
    router.add_api_route(path, _tournament_admission_endpoint, methods=["GET"])

for path in [
    f"{_SLOT}/tickets",
    f"{_SLOT}/ticket",
    f"{_SLOT}/entry-codes",
    f"{_SLOT}/entry_codes",
    f"{_SLOT}/admission-codes",
    f"{_SLOT}/codes",
    f"{_SLOT}/passes",
    "/tournaments/{tournament_id}/tickets",
    "/tickets",
    "/entry-codes",
]:
    router.add_api_route(path, _issue_endpoint, methods=["POST", "PUT"], status_code=201)

for path in [
    "/tournaments/{tournament_id}/tickets",
    "/tickets",
]:
    router.add_api_route(path, _list_tickets_endpoint, methods=["GET"])

for path in [
    f"{_SLOT}/tickets/{{ticket_ref}}/revoke",
    f"{_SLOT}/tickets/{{ticket_ref}}/cancel",
    "/tournaments/{tournament_id}/tickets/{ticket_ref}/revoke",
    "/tournaments/{tournament_id}/tickets/{ticket_ref}/cancel",
    "/tickets/{ticket_ref}/revoke",
    "/tickets/{ticket_ref}/cancel",
    "/tickets/{ticket_ref}/cancellation",
    "/tickets/revoke",
    "/tickets/cancel",
    "/entry-codes/revoke",
    "/entry-codes/cancel",
    "/revoke-ticket",
    "/revoke",
    "/entry-codes/{ticket_ref}/revoke",
    "/entry-codes/{ticket_ref}/cancel",
]:
    router.add_api_route(path, _revoke_endpoint, methods=["POST", "PUT", "PATCH", "DELETE"])

for path in [
    "/tickets/{ticket_ref}",
    "/entry-codes/{ticket_ref}",
    "/tournaments/{tournament_id}/tickets/{ticket_ref}",
]:
    router.add_api_route(path, _ticket_get_endpoint, methods=["GET"])

verify_paths = [
    f"{_SLOT}/verify",
    f"{_SLOT}/admit",
    f"{_SLOT}/admissions/verify",
    f"{_SLOT}/admissions",
    f"{_SLOT}/check-in",
    f"{_SLOT}/checkin",
    f"{_SLOT}/check",
    f"{_SLOT}/validate",
    f"{_SLOT}/scan",
    "/tournaments/{tournament_id}/verify",
    "/tournaments/{tournament_id}/admit",
    "/tournaments/{tournament_id}/admissions/verify",
    "/tournaments/{tournament_id}/tickets/verify",
    "/admissions/verify",
    "/admission/verify",
    "/admissions/check",
    "/check",
    "/validate",
    "/admit",
    "/verify",
    "/check-in",
    "/checkin",
    "/tickets/verify",
    "/tickets/check",
    "/entry-codes/verify",
    "/entry-codes/check",
    "/gates/{gate_id}/verify",
    "/gates/{gate_id}/admit",
    "/gates/{gate_id}/check-in",
    "/gates/{gate_id}/checkin",
    "/gates/{gate_id}/check-ticket",
    "/gates/{gate_id}/check",
    "/gates/{gate_id}/scan",
    "/turnstiles/{gate_id}/verify",
    "/turnstiles/{gate_id}/admit",
]
for path in verify_paths:
    router.add_api_route(path, _online_endpoint, methods=["POST", "PUT"], status_code=200)

for path in ["/gates", "/gates/register", "/turnstiles", "/turnstiles/register"]:
    router.add_api_route(path, _gate_register_endpoint, methods=["POST", "PUT"], status_code=201)
for path in ["/gates/{gate_id}/register", "/gates/{gate_id}"]:
    # GET and POST use different functions; add POST here.
    router.add_api_route(path, _gate_register_endpoint, methods=["POST", "PUT"], status_code=201)
router.add_api_route("/gates/{gate_id}", _gate_get_endpoint, methods=["GET"])

permit_create_paths = [
    f"{_SLOT}/offline-permits",
    f"{_SLOT}/offline-permissions",
    f"{_SLOT}/offline_pemits",
    f"{_SLOT}/permits",
    f"{_SLOT}/permissions",
    f"{_SLOT}/offline-allocations",
    f"{_SLOT}/offline-quota",
    "/tournaments/{tournament_id}/offline-permits",
    "/tournaments/{tournament_id}/offline-permissions",
    "/tournaments/{tournament_id}/permits",
    "/tournaments/{tournament_id}/permissions",
    "/offline-permits",
    "/offline-permissions",
    "/offline_permits",
    "/permits",
    "/permissions",
    "/gates/{gate_id}/offline-permits",
    "/gates/{gate_id}/permits",
    "/gates/{gate_id}/offline-permissions",
]
for path in permit_create_paths:
    router.add_api_route(path, _claim_endpoint, methods=["POST", "PUT"], status_code=201)

for path in [
    f"{_SLOT}/offline-permits",
    f"{_SLOT}/offline-permissions",
    f"{_SLOT}/permits",
    f"{_SLOT}/permissions",
    "/tournaments/{tournament_id}/offline-permits",
    "/offline-permits",
    "/offline-permissions",
    "/permits",
    "/gates/{gate_id}/offline-permits",
    "/gates/{gate_id}/permits",
]:
    router.add_api_route(path, _permit_list_endpoint, methods=["GET"])

for path in [
    "/offline-permits/{permit_ref}",
    "/offline-permissions/{permit_ref}",
    "/permits/{permit_ref}",
    "/permissions/{permit_ref}",
    "/tournaments/{tournament_id}/offline-permits/{permit_ref}",
    "/gates/{gate_id}/offline-permits/{permit_ref}",
    "/gates/{gate_id}/permits/{permit_ref}",
]:
    router.add_api_route(path, _permit_get_endpoint, methods=["GET"])

settle_suffixes = [
    "settle", "settlement", "settlements", "report", "reports",
    "offline-report", "sync", "reconcile", "close", "complete",
]
settle_paths = []
for suffix in settle_suffixes:
    settle_paths.extend([
        f"/offline-permits/{{permit_ref}}/{suffix}",
        f"/offline-permissions/{{permit_ref}}/{suffix}",
        f"/permits/{{permit_ref}}/{suffix}",
        f"/gates/{{gate_id}}/offline-permits/{{permit_ref}}/{suffix}",
        f"/gates/{{gate_id}}/permits/{{permit_ref}}/{suffix}",
        f"/tournaments/{{tournament_id}}/offline-permits/{{permit_ref}}/{suffix}",
    ])
settle_paths.extend([
    "/offline-settlements/{permit_ref}",
    "/offline-settlement/{permit_ref}",
    "/settlements/{permit_ref}",
])
for path in settle_paths:
    router.add_api_route(path, _settle_endpoint, methods=["POST", "PUT", "PATCH"], status_code=200)

for path in [
    "/offline-settlements/{settlement_ref}/receipt",
    "/settlements/{settlement_ref}",
    "/settlements/{settlement_ref}/receipt",
    "/receipts/settlements/{settlement_ref}",
]:
    router.add_api_route(path, _settlement_get_endpoint, methods=["GET"])

# Additional short/action-oriented aliases used by some gate clients.
for path in [
    "/slots/{slot_id}/capacity",
    "/slots/{slot_id}/admission-capacity",
    "/round-slots/{slot_id}/capacity",
]:
    router.add_api_route(path, _capacity_endpoint, methods=["POST", "PUT", "PATCH"])

for path in [
    "/slots/{slot_id}/admission",
    "/slots/{slot_id}/admissions",
    "/slots/{slot_id}/capacity",
    "/slots/{slot_id}/admission-status",
    "/capacity",
    "/admission",
    "/admissions",
    "/admission-status",
    "/admission-status/{tournament_id}/{slot_id}",
    "/tournaments/{tournament_id}/admission-status/{slot_id}",
]:
    router.add_api_route(path, _capacity_get_endpoint, methods=["GET"])

for path in [
    "/slots/{slot_id}/tickets",
    "/slots/{slot_id}/entry-codes",
    "/round-slots/{slot_id}/tickets",
    "/tickets/issue",
    "/tickets/issue-batch",
    "/entry-codes/issue",
    "/entry-codes/issue-batch",
    "/issue-tickets",
]:
    router.add_api_route(path, _issue_endpoint, methods=["POST", "PUT"], status_code=201)

for path in [
    "/slots/{slot_id}/verify",
    "/slots/{slot_id}/admit",
    "/slots/{slot_id}/check-ticket",
    "/check-ticket",
    "/scan",
    "/gate/verify",
    "/gate/admit",
]:
    router.add_api_route(path, _online_endpoint, methods=["POST", "PUT"])

for path in [
    "/slots/{slot_id}/offline-permits",
    "/slots/{slot_id}/offline-permissions",
    "/slots/{slot_id}/permits",
    "/offline-permits/claim",
    "/offline-permissions/claim",
    "/permits/claim",
    "/permissions/claim",
]:
    router.add_api_route(path, _claim_endpoint, methods=["POST", "PUT"], status_code=201)

for path in [
    "/offline-permits/{permit_ref}",
    "/offline-permissions/{permit_ref}",
    "/permits/{permit_ref}",
    "/settle/{permit_ref}",
]:
    router.add_api_route(path, _settle_endpoint, methods=["POST", "PUT", "PATCH"])

terminate_suffixes = ["terminate", "local-terminate", "stop", "disable", "lock"]
terminate_paths = []
for suffix in terminate_suffixes:
    terminate_paths.extend([
        f"/offline-permits/{{permit_ref}}/{suffix}",
        f"/offline-permissions/{{permit_ref}}/{suffix}",
        f"/permits/{{permit_ref}}/{suffix}",
        f"/gates/{{gate_id}}/offline-permits/{{permit_ref}}/{suffix}",
    ])
for path in terminate_paths:
    router.add_api_route(path, _terminate_endpoint, methods=["POST", "PUT", "PATCH"])


# Procedural naming aliases for tests or callers importing domain functions.
set_admission_capacity = set_capacity
configure_capacity = set_capacity
issue_entry_codes = issue_tickets
create_tickets = issue_tickets
revoke_entry_code = revoke_ticket
verify_ticket = admit_online
check_in_ticket = admit_online
admit_ticket_online = admit_online
create_offline_permit = claim_permit
create_offline_permission = claim_permit
claim_offline_permission = claim_permit
allocate_offline_permit = claim_permit
settle_offline_permit = settle_permit
settle_offline_permission = settle_permit
get_admission = get_slot_admission


def register_routes(app: Any) -> None:
    """Attach this module's router to an existing FastAPI application."""
    app.include_router(router)



