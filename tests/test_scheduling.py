"""Tests for venue reservations, closure repair and scheduled first starts."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import scheduling, service
from app.db import connect
from app.main import create_app

DATE = "2026-06-01"
BEFORE = "2026-05-31T12:00:00+00:00"
SLOT_IDS = ["QF1", "QF2", "QF3", "QF4", "SF1", "SF2", "F"]


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOURNAMENT_NOW", BEFORE)


def make_tournament(client: TestClient, name: str = "Cup") -> int:
    response = client.post(
        "/tournaments",
        json={"name": name, "players": [f"{name}-P{i}" for i in range(1, 9)]},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["tournament"]["id"])


def interval(
    venue: str,
    start: int,
    end: int,
    date: str = DATE,
) -> dict[str, Any]:
    return {
        "venue_name": venue,
        "date": date,
        "start_minute": start,
        "end_minute": end,
    }


def candidate_body(
    tournament_id: int,
    qf1: list[dict[str, Any]] | None = None,
    *,
    shared_other_venues: bool = False,
) -> dict[str, Any]:
    """A feasible seven-slot body.

    Quarter-finals run in parallel on different venues, semis start after
    QF ends, and the final starts after both semis end.
    """

    def venue(slot: str) -> str:
        return slot if shared_other_venues else f"T{tournament_id}-{slot}"

    candidates: dict[str, list[dict[str, Any]]] = {}
    for slot in SLOT_IDS:
        if slot == "QF1" and qf1 is not None:
            candidates[slot] = qf1
        elif slot.startswith("QF"):
            candidates[slot] = [interval(venue(slot), 600, 630)]
        elif slot == "SF1":
            candidates[slot] = [interval(venue(slot), 640, 670)]
        elif slot == "SF2":
            candidates[slot] = [interval(venue(slot), 640, 670)]
        else:
            candidates[slot] = [interval(venue(slot), 680, 710)]
    return {"candidates": candidates}


def reserve(
    client: TestClient,
    tournament_id: int,
    body: dict[str, Any] | None = None,
    key: str = "reserve-key",
):
    return client.post(
        f"/tournaments/{tournament_id}/reservations",
        json=body or candidate_body(tournament_id),
        headers={"Idempotency-Key": key},
    )


def selected_venues(response_json: dict[str, Any]) -> dict[str, str]:
    return {slot["slot_id"]: slot["venue_name"] for slot in response_json["schedule"]}


def test_reservation_requires_all_seven_and_at_most_six_candidates(
    client: TestClient,
) -> None:
    tid = make_tournament(client)

    incomplete = candidate_body(tid)
    del incomplete["candidates"]["F"]
    response = reserve(client, tid, incomplete, key="incomplete")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "all_seven_slots_required"

    too_many = candidate_body(tid)
    too_many["candidates"]["QF1"] = [
        interval(f"V{i}", 600 + i, 610 + i) for i in range(7)
    ]
    response = reserve(client, tid, too_many, key="too-many")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "too_many_candidates"


def test_reservation_requires_idempotency_key(client: TestClient) -> None:
    tid = make_tournament(client)
    response = client.post(
        f"/tournaments/{tid}/reservations", json=candidate_body(tid)
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_reservation_selects_lexicographically_smallest_feasible_schedule(
    client: TestClient,
) -> None:
    tid = make_tournament(client)
    body = candidate_body(tid)
    # Every slot has a lexically larger same-time venue and a smaller one.
    for slot in SLOT_IDS:
        current = body["candidates"][slot][0]
        body["candidates"][slot] = [
            {**current, "venue_name": f"Z-{slot}"},
            {**current, "venue_name": f"A-{slot}"},
        ]
    response = reserve(client, tid, body, key="lex")
    assert response.status_code == 201, response.text
    assert all(v == f"A-{slot}" for slot, v in selected_venues(response.json()).items())


def test_bracket_order_and_half_open_overlap_are_enforced(client: TestClient) -> None:
    tid = make_tournament(client)
    body = candidate_body(tid)
    # Equality with feeder end is allowed by the half-open interval rule.
    body["candidates"]["SF1"] = [interval("SF1", 630, 660)]
    response = reserve(client, tid, body, key="equal-ok")
    assert response.status_code == 201, response.text

    tid2 = make_tournament(client, "Second")
    bad = candidate_body(tid2)
    bad["candidates"]["SF1"] = [interval(f"T{tid2}-SF1", 629, 660)]
    response = reserve(client, tid2, bad, key="bad-order")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_feasible_reservation"


def test_venue_overlap_with_other_tournament_blocks_booking_and_rolls_back(
    client: TestClient,
) -> None:
    t1 = make_tournament(client, "One")
    t2 = make_tournament(client, "Two")
    first = candidate_body(t1, [interval("Shared", 600, 630)])
    second = candidate_body(t2, [interval("Shared", 600, 630)])

    assert reserve(client, t1, first, key="r1").status_code == 201
    response = reserve(client, t2, second, key="r2")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_feasible_reservation"

    schedule = client.get(f"/tournaments/{t2}/schedule").json()
    assert schedule["reserved"] is False
    assert schedule["slots"] == []

    day = client.get("/schedule", params={"date": DATE}).json()
    shared = [a for a in day["assignments"] if a["venue_name"] == "Shared"]
    assert len(shared) == 1
    assert shared[0]["tournament_id"] == t1


def test_reservation_idempotent_replay_and_same_key_conflict(
    client: TestClient,
) -> None:
    tid = make_tournament(client)
    body = candidate_body(tid)
    first = reserve(client, tid, body, key="same-key")
    assert first.status_code == 201
    first_id = first.json()["reservation"]["tournament_id"]

    replay = reserve(client, tid, body, key="same-key")
    assert replay.status_code == 201
    assert replay.json() == first.json()

    changed = candidate_body(tid)
    changed["candidates"]["QF1"] = [interval("Changed", 600, 630)]
    conflict = reserve(client, tid, changed, key="same-key")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    again = reserve(client, tid, body, key="another-key")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "reservation_already_confirmed"
    assert first_id == tid


def test_cross_tournament_closure_uses_exact_two_slot_minimum(
    client: TestClient, db_path: Path
) -> None:
    t1 = make_tournament(client, "One")
    t2 = make_tournament(client, "Two")
    b1 = candidate_body(
        t1,
        [interval("A", 600, 630), interval("B", 600, 630)],
    )
    b2 = candidate_body(
        t2,
        [interval("B", 600, 630), interval("C", 600, 630)],
    )
    assert reserve(client, t1, b1, key="r1").status_code == 201
    assert reserve(client, t2, b2, key="r2").status_code == 201

    closure_body = {
        "venue_name": "A",
        "date": DATE,
        "start_minute": 605,
        "end_minute": 620,
        "reason": "floor repair",
    }
    response = client.post(
        "/closures", json=closure_body, headers={"Idempotency-Key": "closure-1"}
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["changed_slot_count"] == 2
    assert [
        (c["tournament_id"], c["slot_id"], c["from"]["venue"], c["to"]["venue"])
        for c in payload["changes"]
    ] == [(t1, "QF1", "A", "B"), (t2, "QF1", "B", "C")]

    day = client.get("/schedule", params={"date": DATE}).json()
    qf1 = {
        a["tournament_id"]: a["venue_name"]
        for a in day["assignments"]
        if a["slot_id"] == "QF1"
    }
    assert qf1 == {t1: "B", t2: "C"}
    assert day["closures"][0]["venue_name"] == "A"

    # Replay after "restart": same receipt/result, no second closure/solve.
    restarted = TestClient(create_app(db_path))
    replay = restarted.post(
        "/closures", json=closure_body, headers={"Idempotency-Key": "closure-1"}
    )
    assert replay.status_code == 201
    assert replay.json() == payload
    receipt = restarted.get("/idempotency/closure-1")
    assert receipt.status_code == 200
    assert receipt.json()["response"]["closure"]["id"] == payload["closure"]["id"]

    conflict = restarted.post(
        "/closures",
        json={**closure_body, "end_minute": 621},
        headers={"Idempotency-Key": "closure-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    # Closing B as well makes QF1 impossible for T1; reject atomically.
    impossible = restarted.post(
        "/closures",
        json={"venue_name": "B", "date": DATE, "start_minute": 600, "end_minute": 630},
        headers={"Idempotency-Key": "closure-2"},
    )
    assert impossible.status_code == 409
    assert impossible.json()["error"]["code"] == "no_feasible_reschedule"
    day_after = restarted.get("/schedule", params={"date": DATE}).json()
    assert len(day_after["closures"]) == 1
    qf1_after = {
        a["tournament_id"]: a["venue_name"]
        for a in day_after["assignments"]
        if a["slot_id"] == "QF1"
    }
    assert qf1_after == {t1: "B", t2: "C"}


def test_arrived_slot_is_locked_against_closure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = make_tournament(client)
    body = candidate_body(
        tid, [interval("A", 600, 630), interval("B", 600, 630)]
    )
    assert reserve(client, tid, body, key="locked-reserve").status_code == 201

    monkeypatch.setenv("TOURNAMENT_NOW", f"{DATE}T10:00:00+00:00")
    response = client.post(
        "/closures",
        json={
            "venue_name": "A",
            "date": DATE,
            "start_minute": 605,
            "end_minute": 620,
        },
        headers={"Idempotency-Key": "locked-closure"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "closure_conflicts_with_locked_slot"

    day = client.get("/schedule", params={"date": DATE}).json()
    assert day["closures"] == []
    qf1 = next(a for a in day["assignments"] if a["slot_id"] == "QF1")
    assert qf1["venue_name"] == "A"


def test_closure_requires_idempotency_key(client: TestClient) -> None:
    response = client.post(
        "/closures",
        json={"venue_name": "A", "date": DATE, "start_minute": 600, "end_minute": 630},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_past_closure_period_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/closures",
        json={
            "venue_name": "A",
            "date": "2026-05-31",
            "start_minute": 300,
            "end_minute": 330,
        },
        headers={"Idempotency-Key": "past-closure"},
    )
    # Frozen clock is 2026-05-31 12:00 UTC, so 05:00 has already started.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "closure_already_started"


def test_first_start_uses_schedule_and_usage_survives_appeal(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = make_tournament(client, "Scheduled")
    assert reserve(client, tid, key="start-reserve").status_code == 201

    monkeypatch.setenv("TOURNAMENT_NOW", f"{DATE}T09:55:00+00:00")
    early = client.post(
        f"/tournaments/{tid}/slots/QF1/start",
        json={"started_by": "alice"},
        headers={"Idempotency-Key": "early-start"},
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "outside_scheduled_slot"

    monkeypatch.setenv("TOURNAMENT_NOW", f"{DATE}T10:05:00+00:00")
    started = client.post(
        f"/tournaments/{tid}/slots/QF1/start",
        json={"started_by": "alice"},
        headers={"Idempotency-Key": "qf1-start"},
    )
    assert started.status_code == 201, started.text
    usage = started.json()["match"]["venue_usage"]
    assert usage["venue_name"] == f"T{tid}-QF1"
    assert usage["within_scheduled_window"] is True
    usage_id = usage["id"]

    players = client.get(f"/tournaments/{tid}/bracket").json()["players"]
    seed1 = next(p["id"] for p in players if p["seed"] == 1)
    reported = client.post(
        f"/tournaments/{tid}/slots/QF1/result",
        json={"winner_player_id": seed1, "reported_by": "alice"},
        headers={"Idempotency-Key": "qf1-result"},
    )
    assert reported.status_code == 200, reported.text
    version_id = reported.json()["result_version_id"]

    appeal = client.post(
        f"/tournaments/{tid}/slots/QF1/appeals",
        json={
            "result_version_id": version_id,
            "description": "review",
            "submitted_by": "coach",
        },
    )
    assert appeal.status_code == 201, appeal.text
    appeal_id = appeal.json()["appeal"]["id"]
    adjudicated = client.post(
        f"/tournaments/{tid}/appeals/{appeal_id}/adjudicate",
        json={
            "outcome": "overturned",
            "expected_result_version_id": version_id,
            "adjudicated_by": "judge",
        },
        headers={"Idempotency-Key": "adjudicate"},
    )
    assert adjudicated.status_code == 200, adjudicated.text

    history = client.get(f"/tournaments/{tid}/history").json()
    qf1_matches = [m for m in history["matches"] if m["slot_id"] == "QF1"]
    assert len(qf1_matches) == 1
    after_usage = qf1_matches[0]["venue_usage"]
    assert after_usage["id"] == usage_id
    assert after_usage["venue_name"] == f"T{tid}-QF1"
    assert after_usage["actual_started_at"] == usage["actual_started_at"]

    restarted = TestClient(create_app(db_path))
    restarted_history = restarted.get(f"/tournaments/{tid}/history").json()
    restarted_qf1 = [
        m for m in restarted_history["matches"] if m["slot_id"] == "QF1"
    ][0]
    assert restarted_qf1["venue_usage"]["id"] == usage_id
    assert restarted_qf1["venue_usage"]["venue_name"] == f"T{tid}-QF1"


def test_appeal_replay_start_is_not_window_controlled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = make_tournament(client, "Replay")
    assert reserve(client, tid, key="replay-reserve").status_code == 201
    players = client.get(f"/tournaments/{tid}/bracket").json()["players"]
    pid = {p["seed"]: p["id"] for p in players}

    monkeypatch.setenv("TOURNAMENT_NOW", f"{DATE}T10:05:00+00:00")
    for slot, winner_seed in (("QF1", 1), ("QF2", 4)):
        assert client.post(
            f"/tournaments/{tid}/slots/{slot}/start",
            json={"started_by": "alice"},
            headers={"Idempotency-Key": f"{slot}-start"},
        ).status_code == 201
        assert client.post(
            f"/tournaments/{tid}/slots/{slot}/result",
            json={"winner_player_id": pid[winner_seed], "reported_by": "alice"},
            headers={"Idempotency-Key": f"{slot}-result"},
        ).status_code == 200

    monkeypatch.setenv("TOURNAMENT_NOW", f"{DATE}T11:05:00+00:00")
    sf_start = client.post(
        f"/tournaments/{tid}/slots/SF1/start",
        json={"started_by": "alice"},
        headers={"Idempotency-Key": "sf1-start"},
    )
    assert sf_start.status_code == 201, sf_start.text
    sf_match_id = sf_start.json()["match"]["id"]
    assert sf_start.json()["match"]["venue_usage"]["id"] is not None

    sf_result = client.post(
        f"/tournaments/{tid}/slots/SF1/result",
        json={"winner_player_id": pid[1], "reported_by": "alice"},
        headers={"Idempotency-Key": "sf1-result"},
    )
    assert sf_result.status_code == 200

    bracket = client.get(f"/tournaments/{tid}/bracket").json()
    qf2 = next(
        slot
        for round_ in bracket["rounds"]
        for slot in round_["slots"]
        if slot["slot_id"] == "QF2"
    )
    qf2_version = qf2["current_match"]["result"]["id"]
    appeal = client.post(
        f"/tournaments/{tid}/slots/QF2/appeals",
        json={
            "result_version_id": qf2_version,
            "description": "wrong winner",
            "submitted_by": "coach",
        },
    ).json()
    adjudicate = client.post(
        f"/tournaments/{tid}/appeals/{appeal['appeal']['id']}/adjudicate",
        json={
            "outcome": "overturned",
            "expected_result_version_id": qf2_version,
            "adjudicated_by": "judge",
        },
        headers={"Idempotency-Key": "qf2-overturn"},
    )
    assert adjudicate.status_code == 200, adjudicate.text
    assert sf_match_id in adjudicate.json()["adjudication"]["superseded_match_ids"]

    # Far after the original window: this is attempt 2 and is arranged outside
    # the first-start scheduling scope.
    monkeypatch.setenv("TOURNAMENT_NOW", f"{DATE}T20:00:00+00:00")
    replay_start = client.post(
        f"/tournaments/{tid}/slots/SF1/start",
        json={"started_by": "bob"},
        headers={"Idempotency-Key": "sf1-replay-start"},
    )
    assert replay_start.status_code == 201, replay_start.text
    assert replay_start.json()["match"]["attempt"] == 2

    history = client.get(f"/tournaments/{tid}/history").json()
    sf_matches = [m for m in history["matches"] if m["slot_id"] == "SF1"]
    assert [m["attempt"] for m in sf_matches] == [1, 2]
    assert sf_matches[0]["venue_usage"] is not None
    assert sf_matches[0]["venue_usage"]["match_id"] == sf_match_id


def test_concurrent_reservations_cannot_double_book_venue(db_path: Path) -> None:
    init_app = create_app(db_path)
    client = TestClient(init_app)
    t1 = make_tournament(client, "Concurrent One")
    t2 = make_tournament(client, "Concurrent Two")
    b1 = candidate_body(t1, [interval("Race-Court", 600, 630)])
    b2 = candidate_body(t2, [interval("Race-Court", 600, 630)])

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, Any]] = []
    lock = threading.Lock()

    def worker(tid: int, body: dict[str, Any], key: str) -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            try:
                status, payload = scheduling.reserve_tournament(conn, tid, body, key)
                with lock:
                    outcomes.append(("ok", status))
            except service.TournamentError as exc:
                with lock:
                    outcomes.append(("error", exc.code))
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(t1, b1, "concurrent-r1")),
        threading.Thread(target=worker, args=(t2, b2, "concurrent-r2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    oks = [x for x in outcomes if x[0] == "ok"]
    errors = [x for x in outcomes if x[0] == "error"]
    assert len(oks) == 1, outcomes
    assert len(errors) == 1, outcomes
    assert errors[0][1] == "no_feasible_reservation"

    conn = connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM schedule_assignments WHERE venue_name='Race-Court'"
        ).fetchone()["n"]
        assert count == 1
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM schedule_reservations"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_concurrent_same_closure_key_inserts_one_closure(db_path: Path) -> None:
    app = create_app(db_path)
    client = TestClient(app)
    tid = make_tournament(client)
    assert reserve(client, tid, key="closure-race-reserve").status_code == 201
    body = {
        "venue_name": "Unused-Race-Court",
        "date": DATE,
        "start_minute": 605,
        "end_minute": 615,
    }
    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            status, payload = scheduling.register_closure(
                conn, body, "shared-closure-key"
            )
            with lock:
                results.append(status)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [201, 201]
    conn = connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM venue_closures WHERE idempotency_key='shared-closure-key'"
        ).fetchone()["n"] == 1
    finally:
        conn.close()
