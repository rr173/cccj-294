"""Tests for spectator tickets, offline permits, settlement and scheduling locks."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import admissions, scheduling
from app.db import connect
from app.gate import GateNetworkError, GateTerminal
from app.main import create_app

DATE = "2026-06-01"
BEFORE = "2026-06-01T09:00:00+00:00"
WINDOW = "2026-06-01T10:05:00+00:00"
SLOT_IDS = ["QF1", "QF2", "QF3", "QF4", "SF1", "SF2", "F"]


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOURNAMENT_NOW", BEFORE)


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path))


def make_tournament(client: TestClient) -> int:
    response = client.post(
        "/tournaments",
        json={"name": "Admission Cup", "players": [f"P{i}" for i in range(1, 9)]},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["tournament"]["id"])


def interval(venue: str, start: int, end: int, date: str = DATE) -> dict[str, Any]:
    return {
        "venue_name": venue,
        "date": date,
        "start_minute": start,
        "end_minute": end,
    }


def reservation_body(tid: int) -> dict[str, Any]:
    candidates: dict[str, list[dict[str, Any]]] = {}
    for slot in SLOT_IDS:
        if slot == "QF1":
            # V1 is lexicographically selected; V2 is the closure repair.
            candidates[slot] = [interval("V1", 600, 630), interval("V2", 600, 630)]
        elif slot.startswith("QF"):
            candidates[slot] = [interval(f"V-{slot}", 600, 630)]
        elif slot == "SF1":
            candidates[slot] = [interval("V-SF1", 640, 670)]
        elif slot == "SF2":
            candidates[slot] = [interval("V-SF2", 640, 670)]
        else:
            candidates[slot] = [interval("V-F", 680, 710)]
    return {"candidates": candidates}


@pytest.fixture
def reserved(client: TestClient) -> int:
    tid = make_tournament(client)
    response = client.post(
        f"/tournaments/{tid}/reservations",
        json=reservation_body(tid),
        headers={"Idempotency-Key": f"reserve-{tid}"},
    )
    assert response.status_code == 201, response.text
    return tid


def set_capacity(client: TestClient, tid: int, capacity: int) -> dict[str, Any]:
    response = client.put(
        f"/tournaments/{tid}/slots/QF1/admission-capacity",
        json={"capacity": capacity, "set_by": "boss"},
        headers={"Idempotency-Key": f"capacity-{tid}-{capacity}"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def issue_codes(client: TestClient, tid: int, codes: list[str]) -> None:
    response = client.post(
        f"/tournaments/{tid}/slots/QF1/tickets",
        json={"codes": codes, "issued_by": "box-office"},
        headers={"Idempotency-Key": f"issue-{'-'.join(codes)}"},
    )
    assert response.status_code == 201, response.text


def summary(client: TestClient, tid: int) -> dict[str, Any]:
    response = client.get(f"/tournaments/{tid}/slots/QF1/admission")
    assert response.status_code == 200, response.text
    return response.json()


def transport_for(client: TestClient):
    def transport(method: str, path: str, payload: dict | None, headers: dict | None):
        response = client.request(method, path, json=payload, headers=headers or {})
        try:
            data = response.json()
        except Exception:
            data = {"text": response.text}
        return response.status_code, data

    return transport


def claim_permit_http(
    client: TestClient,
    tid: int,
    codes: list[str],
    limit: int,
    gate: str = "G1",
    key: str = "claim-1",
):
    return client.post(
        f"/tournaments/{tid}/slots/QF1/offline-permits",
        json={
            "gate_id": gate,
            "ticket_codes": codes,
            "release_limit": limit,
            "requested_by": "staff",
        },
        headers={"Idempotency-Key": key},
    )


def test_online_admission_enforces_window_venue_and_single_use(
    client: TestClient, reserved: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = reserved
    set_capacity(client, tid, 2)
    issue_codes(client, tid, ["A", "B", "C"])

    # Too early at 09:00.
    early = client.post(
        "/gates/G1/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "A", "venue_name": "V1"},
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "outside_admission_window"

    monkeypatch.setenv("TOURNAMENT_NOW", WINDOW)
    ok = client.post(
        "/gates/G1/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "A", "venue_name": "V1"},
        headers={"Idempotency-Key": "online-A"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["admitted"] is True

    duplicate = client.post(
        "/gates/G1/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "A", "venue_name": "V1"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "ticket_already_admitted"

    # Same idempotency key/body returns the original successful receipt.
    replay = client.post(
        "/gates/G1/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "A", "venue_name": "V1"},
        headers={"Idempotency-Key": "online-A"},
    )
    assert replay.status_code == 200
    assert replay.json()["admission"]["id"] == ok.json()["admission"]["id"]

    wrong_venue = client.post(
        "/gates/G2/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "B", "venue_name": "V2"},
    )
    assert wrong_venue.status_code == 409
    assert wrong_venue.json()["error"]["code"] == "wrong_venue"

    second = client.post(
        "/admissions/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "B", "venue_name": "V1"},
        headers={"Idempotency-Key": "online-B"},
    )
    assert second.status_code == 200, second.text
    full = client.post(
        "/admissions/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "C", "venue_name": "V1"},
    )
    assert full.status_code == 409
    assert full.json()["error"]["code"] == "admission_capacity_reached"

    view = summary(client, tid)
    assert view["confirmed_admissions"] == 2
    assert view["outstanding_reserved_capacity"] == 0
    assert view["remaining_allocatable_capacity"] == 0


def test_permit_locks_codes_reserves_limit_and_settlement_releases_quota(
    client: TestClient,
    reserved: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = reserved
    set_capacity(client, tid, 5)
    issue_codes(client, tid, ["A", "B", "C", "D", "E"])

    claimed = claim_permit_http(client, tid, ["A", "B", "C"], 2, key="claim-main")
    assert claimed.status_code == 201, claimed.text
    permit = claimed.json()["permit"]
    pid = permit["id"]
    assert permit["venue_name"] == "V1"
    assert permit["release_limit"] == 2

    view = summary(client, tid)
    assert view["outstanding_reserved_capacity"] == 2
    assert view["remaining_allocatable_capacity"] == 3

    monkeypatch.setenv("TOURNAMENT_NOW", WINDOW)
    assert client.post(f"/tickets/A/revoke", json={"reason": "x"}).status_code == 409
    locked_online = client.post(
        "/admissions/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "A", "venue_name": "V1"},
    )
    assert locked_online.status_code == 409
    assert locked_online.json()["error"]["code"] == "ticket_reserved_for_offline_permit"

    second_claim = claim_permit_http(client, tid, ["A"], 1, gate="G2", key="claim-A-again")
    assert second_claim.status_code == 409
    assert second_claim.json()["error"]["code"] == "tickets_not_available_for_permit"

    online_d = client.post(
        "/admissions/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "D", "venue_name": "V1"},
        headers={"Idempotency-Key": "online-D"},
    )
    assert online_d.status_code == 200, online_d.text
    assert summary(client, tid)["remaining_allocatable_capacity"] == 2

    state = tmp_path / "gate.db"
    gate = GateTerminal(state, transport=transport_for(client))
    gate.import_permit(claimed.json())
    admission = gate.admit("A", at=WINDOW)
    assert admission["admitted"] is True
    with pytest.raises(Exception) as exc1:
        gate.admit("A", at=WINDOW)
    assert exc1.value.code == "ticket_already_admitted_locally"
    gate.close()

    # A new terminal/process on the same durable state cannot forget A.
    restarted = GateTerminal(state, transport=transport_for(client))
    local = restarted.get_local_permit(pid)
    assert local["admitted_count"] == 1
    assert "A" in local["used_codes"]
    with pytest.raises(Exception) as exc2:
        restarted.admit("A", at=WINDOW)
    assert exc2.value.code == "ticket_already_admitted_locally"

    restarted.terminate_locally(pid)
    settle_key = restarted.get_local_permit(pid)["idempotency_key"]
    status, settled = restarted.settle(pid)
    assert status == 200, settled
    assert settled["admitted_count"] == 1
    assert settled["released_code_count"] == 2  # B and C were not physically used.
    restarted.close()

    view = summary(client, tid)
    assert view["confirmed_admissions"] == 2  # D online + A offline
    assert view["offline_admissions"] == 1
    assert view["outstanding_reserved_capacity"] == 0
    assert view["remaining_allocatable_capacity"] == 3

    # Unused listed codes are free again and can back another permit.
    reclaim = claim_permit_http(client, tid, ["B"], 1, gate="G3", key="claim-B")
    assert reclaim.status_code == 201, reclaim.text

    # Repeating the exact successful HTTP settlement/key is an idempotent replay.
    replay = client.post(
        f"/offline-permits/{pid}/settle",
        json={
            "gate_id": "G1",
            "report_id": settled["report_id"],
            "records": [
                {
                    "code": "A",
                    "admitted_at": WINDOW,
                    "gate_id": "G1",
                    "venue_name": "V1",
                }
            ],
        },
        headers={"Idempotency-Key": settle_key},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt"]["settlement_id"] == settled["settlement_id"]

    same_report = client.post(
        f"/offline-permits/{pid}/settle",
        json={
            "gate_id": "G1",
            "report_id": settled["report_id"],
            "records": [
                {
                    "code": "A",
                    "admitted_at": WINDOW,
                    "gate_id": "G1",
                    "venue_name": "V1",
                }
            ],
        },
    )
    assert same_report.status_code == 200
    assert same_report.json()["receipt"]["settlement_id"] == settled["settlement_id"]

    different = client.post(
        f"/offline-permits/{pid}/settle",
        json={"gate_id": "G1", "records": [{"code": "B", "admitted_at": WINDOW}]},
    )
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "settlement_conflict"


def test_invalid_settlement_fails_atomically_and_empty_report_settles(
    client: TestClient, reserved: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = reserved
    set_capacity(client, tid, 3)
    issue_codes(client, tid, ["A", "B", "C", "X"])
    claimed = claim_permit_http(client, tid, ["A", "B", "C"], 2, key="claim-atomic")
    pid = claimed.json()["permit"]["id"]
    monkeypatch.setenv("TOURNAMENT_NOW", WINDOW)

    duplicate = client.post(
        f"/offline-permits/{pid}/settle",
        json={
            "gate_id": "G1",
            "records": [
                {"code": "A", "admitted_at": WINDOW},
                {"code": "A", "admitted_at": WINDOW},
            ],
        },
        headers={"Idempotency-Key": "bad-duplicate"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_code_in_settlement"

    over_limit = client.post(
        f"/offline-permits/{pid}/settle",
        json={
            "gate_id": "G1",
            "records": [
                {"code": "A", "admitted_at": WINDOW},
                {"code": "B", "admitted_at": WINDOW},
                {"code": "C", "admitted_at": WINDOW},
            ],
        },
        headers={"Idempotency-Key": "bad-over"},
    )
    assert over_limit.status_code == 409
    assert over_limit.json()["error"]["code"] == "permit_limit_exceeded"

    outside = client.post(
        f"/offline-permits/{pid}/settle",
        json={
            "gate_id": "G1",
            "records": [{"code": "A", "admitted_at": "2026-06-01T08:00:00+00:00"}],
        },
        headers={"Idempotency-Key": "bad-time"},
    )
    assert outside.status_code == 409
    assert outside.json()["error"]["code"] == "admission_outside_permit_window"

    outsider = client.post(
        f"/offline-permits/{pid}/settle",
        json={"gate_id": "G9", "records": []},
    )
    assert outsider.status_code == 403
    assert outsider.json()["error"]["code"] == "permit_gate_mismatch"

    not_listed = client.post(
        f"/offline-permits/{pid}/settle",
        json={
            "gate_id": "G1",
            "records": [{"code": "X", "admitted_at": WINDOW}],
        },
        headers={"Idempotency-Key": "bad-code"},
    )
    assert not_listed.status_code == 403
    assert not_listed.json()["error"]["code"] == "ticket_not_in_permit"

    # Every failed report rolled back completely.
    view = summary(client, tid)
    assert view["confirmed_admissions"] == 0
    assert view["outstanding_reserved_capacity"] == 2
    assert client.get(f"/offline-permits/{pid}").json()["status"] == "active"

    terminate = client.post(
        f"/offline-permits/{pid}/terminate", json={"gate_id": "G1"}
    )
    assert terminate.status_code == 200, terminate.text
    assert terminate.json()["status"] == "active"
    assert terminate.json()["settled"] is False

    empty = client.post(
        f"/offline-permits/{pid}/settle",
        json={"gate_id": "G1", "records": []},
        headers={"Idempotency-Key": "empty-settle"},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["admitted_count"] == 0
    assert empty.json()["released_code_count"] == 3
    view = summary(client, tid)
    assert view["confirmed_admissions"] == 0
    assert view["outstanding_reserved_capacity"] == 0
    assert view["remaining_allocatable_capacity"] == 3


def test_unsettled_permit_blocks_reschedule_then_settlement_allows_code_to_follow(
    client: TestClient, reserved: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = reserved
    set_capacity(client, tid, 5)
    issue_codes(client, tid, ["A", "B"])
    claimed = claim_permit_http(client, tid, ["A"], 1, key="claim-lock")
    pid = claimed.json()["permit"]["id"]

    closure_body = {
        "venue_name": "V1",
        "date": DATE,
        "start_minute": 615,
        "end_minute": 645,
        "reason": "broken screen",
    }
    blocked = client.post(
        "/closures", json=closure_body, headers={"Idempotency-Key": "closure-blocked"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] in {
        "no_feasible_reschedule",
        "closure_conflicts_with_locked_slot",
    }
    schedule = client.get(f"/tournaments/{tid}/schedule").json()
    qf1 = next(s for s in schedule["slots"] if s["slot_id"] == "QF1")
    assert qf1["venue_name"] == "V1"

    # Empty settlement is valid even before the window occurs.
    settled = client.post(
        f"/offline-permits/{pid}/settle",
        json={"gate_id": "G1", "records": []},
        headers={"Idempotency-Key": "settle-lock"},
    )
    assert settled.status_code == 200, settled.text

    repaired = client.post(
        "/closures", json=closure_body, headers={"Idempotency-Key": "closure-ok"}
    )
    assert repaired.status_code == 201, repaired.text
    schedule = client.get(f"/tournaments/{tid}/schedule").json()
    qf1 = next(s for s in schedule["slots"] if s["slot_id"] == "QF1")
    assert qf1["venue_name"] == "V2"

    monkeypatch.setenv("TOURNAMENT_NOW", WINDOW)
    old_venue = client.post(
        "/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "B", "venue_name": "V1"},
    )
    assert old_venue.status_code == 409
    assert old_venue.json()["error"]["code"] == "wrong_venue"

    new_venue = client.post(
        "/verify",
        json={"tournament_id": tid, "slot_id": "QF1", "code": "B", "venue_name": "V2"},
        headers={"Idempotency-Key": "B-new-venue"},
    )
    assert new_venue.status_code == 200, new_venue.text


def test_concurrent_online_admits_and_permit_claims_do_not_overcommit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOURNAMENT_NOW", BEFORE)
    app = create_app(db_path)
    setup = TestClient(app)
    tid = make_tournament(setup)
    r = setup.post(
        f"/tournaments/{tid}/reservations",
        json=reservation_body(tid),
        headers={"Idempotency-Key": "reserve-concurrent"},
    )
    assert r.status_code == 201, r.text
    set_capacity(setup, tid, 1)
    issue_codes(setup, tid, ["RACE-1", "RACE-2", "RACE-3"])
    monkeypatch.setenv("TOURNAMENT_NOW", WINDOW)

    barrier = threading.Barrier(2)
    from app import service as tournament_service

    outcomes: list[Any] = []

    def admit_one(code: str, idx: int) -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            outcomes.append(
                admissions.admit_online(
                    conn,
                    {
                        "tournament_id": tid,
                        "slot_id": "QF1",
                        "code": code,
                        "venue_name": "V1",
                    },
                    f"race-online-{idx}",
                )
            )
        except tournament_service.TournamentError as exc:
            outcomes.append(exc)
        finally:
            conn.close()

    t1 = threading.Thread(target=admit_one, args=("RACE-1", 1))
    t2 = threading.Thread(target=admit_one, args=("RACE-2", 2))
    t1.start(); t2.start(); t1.join(); t2.join()
    success = [x for x in outcomes if not isinstance(x, Exception)]
    conflicts = [x for x in outcomes if isinstance(x, tournament_service.TournamentError)]
    assert len(success) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code in {"admission_capacity_reached", "ticket_already_admitted"}

    # Capacity is now consumed; a one-place permit cannot be allocated.
    conn = connect(db_path)
    with pytest.raises(tournament_service.TournamentError) as exc:
        admissions.claim_permit(
            conn,
            tid,
            "QF1",
            {"gate_id": "GRACE", "ticket_codes": ["RACE-3"], "release_limit": 1},
            "race-claim-late",
        )
    conn.close()
    assert exc.value.code == "admission_capacity_exceeded"


def test_concurrent_claim_and_revoke_have_one_winner(db_path: Path) -> None:
    app = create_app(db_path)
    client = TestClient(app)
    tid = make_tournament(client)
    assert client.post(
        f"/tournaments/{tid}/reservations",
        json=reservation_body(tid),
        headers={"Idempotency-Key": "reserve-claim-revoke"},
    ).status_code == 201
    set_capacity(client, tid, 5)
    issue_codes(client, tid, ["LOCK-RACE"])

    from app import service as tournament_service

    barrier = threading.Barrier(2)
    outcomes: list[Any] = []

    def claim() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            outcomes.append(
                admissions.claim_permit(
                    conn,
                    tid,
                    "QF1",
                    {
                        "gate_id": "RACE-G",
                        "ticket_codes": ["LOCK-RACE"],
                        "release_limit": 1,
                    },
                    "race-claim",
                )
            )
        except tournament_service.TournamentError as exc:
            outcomes.append(exc)
        finally:
            conn.close()

    def revoke() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            outcomes.append(
                admissions.revoke_ticket(
                    conn,
                    "LOCK-RACE",
                    {"reason": "race"},
                    "race-revoke",
                    tournament_id=tid,
                    slot_id="QF1",
                )
            )
        except tournament_service.TournamentError as exc:
            outcomes.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=claim), threading.Thread(target=revoke)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [x for x in outcomes if not isinstance(x, tournament_service.TournamentError)]
    errors = [x for x in outcomes if isinstance(x, tournament_service.TournamentError)]
    assert len(successes) == 1
    assert len(errors) == 1
    assert errors[0].code in {
        "ticket_locked_by_offline_permit",
        "tickets_not_available_for_permit",
    }


def test_settlement_and_closure_race_preserves_permit_constraint(db_path: Path) -> None:
    app = create_app(db_path)
    client = TestClient(app)
    tid = make_tournament(client)
    assert client.post(
        f"/tournaments/{tid}/reservations",
        json=reservation_body(tid),
        headers={"Idempotency-Key": "reserve-settle-close"},
    ).status_code == 201
    set_capacity(client, tid, 4)
    issue_codes(client, tid, ["R1"])
    claimed = claim_permit_http(client, tid, ["R1"], 1, key="claim-race-settle")
    pid = claimed.json()["permit"]["id"]

    from app import service as tournament_service

    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}

    def settle() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            outcomes["settle"] = admissions.settle_permit(
                conn, pid, {"gate_id": "G1", "records": []}, "race-settle-key"
            )
        except tournament_service.TournamentError as exc:
            outcomes["settle_error"] = exc
        finally:
            conn.close()

    def close_venue() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            outcomes["closure"] = scheduling.register_closure(
                conn,
                {
                    "venue_name": "V1",
                    "date": DATE,
                    "start_minute": 615,
                    "end_minute": 645,
                },
                "race-closure-key",
            )
        except tournament_service.TournamentError as exc:
            outcomes["closure_error"] = exc
        finally:
            conn.close()

    threads = [threading.Thread(target=settle), threading.Thread(target=close_venue)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert "settle" in outcomes
    assert outcomes["settle"][0] == 200
    # If closure went first it was rejected because permit was active.
    if "closure_error" in outcomes:
        assert outcomes["closure_error"].code in {
            "no_feasible_reschedule",
            "closure_conflicts_with_locked_slot",
        }
    else:
        assert outcomes["closure"][0] == 201

    permit = client.get(f"/offline-permits/{pid}").json()
    assert permit["status"] == "settled"
    schedule = client.get(f"/tournaments/{tid}/schedule").json()
    qf1 = next(s for s in schedule["slots"] if s["slot_id"] == "QF1")
    if "closure" in outcomes:
        assert qf1["venue_name"] == "V2"
    else:
        assert qf1["venue_name"] == "V1"


def test_restart_expiry_and_timeout_do_not_self_settle(
    client: TestClient,
    db_path: Path,
    reserved: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = reserved
    set_capacity(client, tid, 4)
    issue_codes(client, tid, ["T1", "T2"])
    claimed = claim_permit_http(client, tid, ["T1", "T2"], 2, key="claim-durability")
    pid = claimed.json()["permit"]["id"]

    # A fresh application/connection represents a service restart.
    restarted_service = TestClient(create_app(db_path))
    permit = restarted_service.get(f"/offline-permits/{pid}").json()
    assert permit["status"] == "active"

    monkeypatch.setenv("TOURNAMENT_NOW", "2026-06-01T12:00:00+00:00")
    expired = restarted_service.get(f"/offline-permits/{pid}").json()
    assert expired["expired"] is True
    assert expired["status"] == "active"
    assert expired["settlement_status"] == "unsettled"

    class FlakyTransport:
        def __init__(self) -> None:
            self.inner = transport_for(client)
            self.fail_next = True
            self.calls = 0

        def __call__(self, method, path, payload, headers):
            result = self.inner(method, path, payload, headers)
            self.calls += 1
            if self.fail_next:
                self.fail_next = False
                # The server may have committed before the response was lost.
                raise GateNetworkError("simulated_timeout", "simulated timeout after commit")
            return result

    flaky = FlakyTransport()
    gate = GateTerminal(tmp_path / "durability-gate.db", transport=flaky)
    gate.import_permit(claimed.json())
    gate.admit("T1", at=WINDOW)
    with pytest.raises(GateNetworkError):
        gate.settle(pid)

    # The gate durably stops use, but does not mark the report settled when
    # the response is lost.  The service may or may not have committed yet;
    # the stable retry resolves that ambiguity by replaying the receipt.
    unknown_local = gate.get_local_permit(pid)
    assert unknown_local["state"] == "terminated"
    assert unknown_local["settlement"] is None
    gate.close()

    # Restart the gate and retry with the same durable report key.
    retry_gate = GateTerminal(tmp_path / "durability-gate.db", transport=flaky)
    status, receipt = retry_gate.settle(pid)
    assert status == 200, receipt
    assert receipt["admitted_count"] == 1
    assert flaky.calls == 2

    settled = restarted_service.get(f"/offline-permits/{pid}").json()
    assert settled["status"] == "settled"
    assert settled["receipt"]["settlement_id"] == receipt["settlement_id"]
    view = summary(restarted_service, tid)
    assert view["confirmed_admissions"] == 1
    assert view["outstanding_reserved_capacity"] == 0
    assert view["remaining_allocatable_capacity"] == 3


def test_concurrent_claim_and_closure_never_issues_on_moved_schedule(
    db_path: Path,
) -> None:
    from app import service as tournament_service

    app = create_app(db_path)
    client = TestClient(app)
    tid = make_tournament(client)
    assert client.post(
        f"/tournaments/{tid}/reservations",
        json=reservation_body(tid),
        headers={"Idempotency-Key": "reserve-claim-closure-race"},
    ).status_code == 201
    set_capacity(client, tid, 8)
    issue_codes(client, tid, ["RACE-CLAIM"])

    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}

    def claim() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            outcomes["claim"] = admissions.claim_permit(
                conn,
                tid,
                "QF1",
                {
                    "gate_id": "RACE-CLAIM-GATE",
                    "ticket_codes": ["RACE-CLAIM"],
                    "release_limit": 1,
                },
                "claim-closure-race-key",
            )
        except tournament_service.TournamentError as exc:
            outcomes["claim_error"] = exc
        finally:
            conn.close()

    def close_venue() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            outcomes["closure"] = scheduling.register_closure(
                conn,
                {
                    "venue_name": "V1",
                    "date": DATE,
                    "start_minute": 615,
                    "end_minute": 645,
                },
                "claim-closure-race-closure-key",
            )
        except tournament_service.TournamentError as exc:
            outcomes["closure_error"] = exc
        finally:
            conn.close()

    threads = [threading.Thread(target=claim), threading.Thread(target=close_venue)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert "claim" in outcomes
    permit_status, permit_payload = outcomes["claim"]
    assert permit_status == 201
    permit = permit_payload["permit"]

    schedule = client.get(f"/tournaments/{tid}/schedule").json()
    qf1 = next(s for s in schedule["slots"] if s["slot_id"] == "QF1")
    assert permit["venue_name"] == qf1["venue_name"]
    assert permit["schedule_date"] == qf1.get("schedule_date", qf1.get("date"))
    assert permit["starts_at"] == qf1["starts_at"]
    assert permit["ends_at"] == qf1["ends_at"]

    if "closure" in outcomes:
        assert outcomes["closure"][0] == 201
        assert qf1["venue_name"] == "V2"
    else:
        assert outcomes["closure_error"].code in {
            "no_feasible_reschedule",
            "closure_conflicts_with_locked_slot",
        }
        assert qf1["venue_name"] == "V1"
