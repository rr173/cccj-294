#!/usr/bin/env python3
"""Repeatable admission/offline-permit story.

Run from the repository root or via demo/admission_demo.sh:

    python3 demo/admission_demo.py

The script uses TestClient for setup, starts a short-lived real uvicorn socket
for gate settlement, and uses a durable SQLite gate state file. Closing and
reopening GateTerminal simulates a gate process restart. All data lives in a
temporary directory that is printed.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn
from fastapi.testclient import TestClient  # noqa: E402

from app.gate import GateError, GateTerminal, HttpTransport  # noqa: E402
from app.main import create_app  # noqa: E402

DATE = "2026-06-01"
BEFORE = "2026-06-01T09:00:00+00:00"
WINDOW = "2026-06-01T10:05:00+00:00"
SLOT_IDS = ["QF1", "QF2", "QF3", "QF4", "SF1", "SF2", "F"]


def pp(title: str, value: Any) -> None:
    print(f"\n=== {title} ===")
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def must(response: Any, expected: int | tuple[int, ...], context: str) -> dict[str, Any]:
    allowed = expected if isinstance(expected, tuple) else (expected,)
    if response.status_code not in allowed:
        raise AssertionError(
            f"{context}: expected {allowed}, got {response.status_code}: {response.text}"
        )
    return response.json()


def interval(venue: str, start: int, end: int) -> dict[str, Any]:
    return {"venue_name": venue, "date": DATE, "start_minute": start, "end_minute": end}


def reservation_body() -> dict[str, Any]:
    candidates: dict[str, list[dict[str, Any]]] = {}
    for slot in SLOT_IDS:
        if slot == "QF1":
            candidates[slot] = [interval("V1-Main", 600, 630), interval("V2-Overflow", 600, 630)]
        elif slot.startswith("QF"):
            candidates[slot] = [interval(f"V-{slot}", 600, 630)]
        elif slot == "SF1":
            candidates[slot] = [interval("V-SF1", 640, 670)]
        elif slot == "SF2":
            candidates[slot] = [interval("V-SF2", 640, 670)]
        else:
            candidates[slot] = [interval("V-F", 680, 710)]
    return {"candidates": candidates}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="admission-demo-"))
    os.environ["TOURNAMENT_NOW"] = BEFORE
    app = create_app(work / "tournament.db")
    client = TestClient(app)

    # Run the same FastAPI app in a real uvicorn socket so the gate's
    # reconnect/settlement step uses HttpTransport rather than an in-process
    # callable.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("demo uvicorn server did not start")

    tid = must(
        client.post(
            "/tournaments",
            json={"name": "Admission Demo Cup", "players": [f"Player {i}" for i in range(1, 9)]},
        ),
        201,
        "create tournament",
    )["tournament"]["id"]

    must(
        client.post(
            f"/tournaments/{tid}/reservations",
            json=reservation_body(),
            headers={"Idempotency-Key": "demo-reservation"},
        ),
        201,
        "reserve schedule",
    )
    must(
        client.put(
            f"/tournaments/{tid}/slots/QF1/admission-capacity",
            json={"capacity": 5, "set_by": "staff"},
            headers={"Idempotency-Key": "demo-capacity"},
        ),
        (200, 201),
        "set capacity",
    )
    must(
        client.post(
            f"/tournaments/{tid}/slots/QF1/tickets",
            json={"codes": ["A", "B", "C", "D", "E"]},
            headers={"Idempotency-Key": "demo-tickets"},
        ),
        201,
        "issue tickets",
    )

    claim = must(
        client.post(
            f"/tournaments/{tid}/slots/QF1/offline-permits",
            json={
                "gate_id": "GATE-1",
                "ticket_codes": ["A", "B", "C"],
                "release_limit": 2,
            },
            headers={"Idempotency-Key": "demo-permit"},
        ),
        201,
        "claim offline permit",
    )
    pid = claim["permit_id"]
    pp("Permit claimed: 3 codes listed, only 2 places reserved", claim["permit"]["time_window"])
    pp("Capacity after claim", claim["capacity_summary"])

    blocked = client.post(
        "/closures",
        json={
            "venue_name": "V1-Main",
            "date": DATE,
            "start_minute": 615,
            "end_minute": 645,
            "reason": "camera failure",
        },
        headers={"Idempotency-Key": "demo-blocked-closure"},
    )
    if blocked.status_code != 409:
        raise AssertionError(f"expected closure conflict, got {blocked.status_code}: {blocked.text}")
    pp("Reschedule blocked while permit is unsettled", blocked.json()["error"])

    locked_revoke = client.post(f"/tickets/A/revoke", json={"reason": "try"})
    pp(
        "Listed code cannot be revoked",
        {"http_status": locked_revoke.status_code, **locked_revoke.json()["error"]},
    )

    # Before the event starts, submit the first permit's empty report.  This
    # terminates its reservation and allows the ordinary scheduler to repair.
    empty_settled = must(
        client.post(
            f"/offline-permits/{pid}/settle",
            json={"gate_id": "GATE-1", "records": []},
            headers={"Idempotency-Key": "demo-empty-p1"},
        ),
        200,
        "empty first settlement",
    )
    pp("Empty report settles the schedule-lock permit", empty_settled["receipt"])

    repaired = must(
        client.post(
            "/closures",
            json={
                "venue_name": "V1-Main",
                "date": DATE,
                "start_minute": 615,
                "end_minute": 645,
                "reason": "camera failure",
            },
            headers={"Idempotency-Key": "demo-closure-after-settlement"},
        ),
        201,
        "closure after settlement",
    )
    qf1 = next(s for s in repaired["schedule"]["assignments"] if s["slot_id"] == "QF1")
    pp("After settlement, original tie-break solver moves QF1", {
        "venue_name": qf1["venue_name"],
        "starts_at": qf1["starts_at"],
        "ends_at": qf1["ends_at"],
    })

    # A later offline permit is claimed on the NEW current schedule.
    claim2 = must(
        client.post(
            f"/tournaments/{tid}/slots/QF1/offline-permits",
            json={
                "gate_id": "GATE-OFFLINE",
                "ticket_codes": ["A", "B", "C"],
                "release_limit": 2,
            },
            headers={"Idempotency-Key": "demo-permit-2"},
        ),
        201,
        "claim second offline permit",
    )
    pid = claim2["permit_id"]
    pp("Second permit snapshots the rescheduled venue/time", claim2["permit"]["time_window"])

    os.environ["TOURNAMENT_NOW"] = WINDOW
    online_d = must(
        client.post(
            "/gates/GATE-ONLINE/verify",
            json={
                "tournament_id": tid,
                "slot_id": "QF1",
                "code": "D",
                "venue_name": "V2-Overflow",
            },
            headers={"Idempotency-Key": "demo-online-D"},
        ),
        200,
        "online D",
    )
    pp("Other code D admitted online", online_d["counts"])

    gate_state = work / "gate-GATE-OFFLINE.db"
    transport = HttpTransport(base_url, timeout=10)

    gate = GateTerminal(gate_state, transport=transport)
    gate.import_permit(claim2)
    offline_a = gate.admit("A", at=WINDOW)
    pp("Gate admits A while offline", {k: offline_a[k] for k in ("admitted", "offline", "admitted_at")})
    try:
        gate.admit("A", at=WINDOW)
        raise AssertionError("duplicate local admission unexpectedly succeeded")
    except GateError as exc:
        pp("Immediate duplicate A is rejected", exc.payload())
    gate.close()

    restarted = GateTerminal(gate_state, transport=transport)
    try:
        restarted.admit("A", at=WINDOW)
        raise AssertionError("duplicate after restart unexpectedly succeeded")
    except GateError as exc:
        pp("After local restart, used A is still rejected", exc.payload())

    restarted.terminate_locally(pid)
    status, settled = restarted.settle(pid)
    if status != 200:
        raise AssertionError(f"settlement failed: {status} {settled}")
    pp("Network restored: complete report settled", settled["receipt"])
    pp("Capacity after settlement: one unused permit place released", settled["capacity_summary"])
    restarted.close()

    old_venue = client.post(
        "/verify",
        json={
            "tournament_id": tid,
            "slot_id": "QF1",
            "code": "B",
            "venue_name": "V1-Main",
        },
    )
    pp(
        "Unused B cannot use the old venue/schedule",
        {"http_status": old_venue.status_code, **old_venue.json()["error"]},
    )
    if old_venue.status_code != 409:
        raise AssertionError("old venue should be rejected")

    new_venue_b = must(
        client.post(
            "/verify",
            json={
                "tournament_id": tid,
                "slot_id": "QF1",
                "code": "B",
                "venue_name": "V2-Overflow",
            },
            headers={"Idempotency-Key": "demo-online-B-new-venue"},
        ),
        200,
        "B at new venue",
    )
    pp("Same unused code B follows the slot to the new schedule", new_venue_b["counts"])

    final = must(client.get(f"/tournaments/{tid}/slots/QF1/admission"), 200, "final query")
    pp(
        "Final query separates admissions, outstanding reservations and remaining allocation",
        {
            "capacity": final["capacity"],
            "confirmed_admissions": final["confirmed_admissions"],
            "outstanding_reserved_capacity": final["outstanding_reserved_capacity"],
            "remaining_allocatable_capacity": final["remaining_allocatable_capacity"],
            "ticket_counts": final["ticket_counts"],
        },
    )
    server.should_exit = True
    print(f"\nTemporary demo database/state retained at: {work}")
    print("Demo completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
