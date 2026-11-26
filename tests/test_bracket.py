"""Bracket fundamentals and idempotency of start/result."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import PLAYER_NAMES, Api


def test_creation_seeds_bracket_and_rejects_bad_player_lists(client: TestClient):
    response = client.post(
        "/tournaments",
        json={"name": "Cup", "players": PLAYER_NAMES},
    )
    assert response.status_code == 201
    body = response.json()
    slots = {s["slot_id"]: s for r in body["rounds"] for s in r["slots"]}

    assert [p["seed"] for p in body["players"]] == list(range(1, 9))
    assert slots["QF1"]["state"] == "ready"
    assert slots["QF1"]["participants"]["a"]["seed"] == 1
    assert slots["QF1"]["participants"]["b"]["seed"] == 8
    assert slots["SF1"]["state"] == "waiting"
    assert slots["F"]["state"] == "waiting"
    assert body["champion"] is None

    for bad in (PLAYER_NAMES[:7], PLAYER_NAMES + ["P9"], PLAYER_NAMES[:7] + ["P1"]):
        failed = client.post("/tournaments", json={"name": "Bad", "players": bad})
        assert failed.status_code in (422,)


def test_start_locks_participants_and_cannot_be_repeated(api: Api):
    first = api.start_ok("QF1")
    match = first["match"]
    assert match["participants"]["a"]["seed"] == 1
    assert match["participants"]["b"]["seed"] == 8
    assert match["status"] == "started"

    again = api.start("QF1", by="bob")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "match_already_in_progress"
    assert again.json()["error"]["started_by"] == "alice"

    # A started match may have its participants displayed but has no result.
    assert api.slot("QF1")["current_match"]["result"] is None
    assert api.slot("QF1")["state"] == "in_progress"


def test_result_requires_started_match_and_valid_winner(api: Api):
    not_started = api.result("QF1", api.player_id(1))
    assert not_started.status_code == 409
    assert not_started.json()["error"]["code"] == "match_not_started"

    api.start_ok("QF1")
    outsider = api.result("QF1", api.player_id(3))
    assert outsider.status_code == 422
    assert outsider.json()["error"]["code"] == "invalid_winner"

    good = api.result_ok("QF1", api.player_id(1))
    assert good["match"]["status"] == "completed"
    assert good["match"]["result"]["winner"]["seed"] == 1
    assert good["match"]["result"]["version"] == 1


def test_two_staff_cannot_create_two_effective_results(api: Api):
    api.start_ok("QF1")
    first = api.result_ok("QF1", api.player_id(1), by="alice")
    second = api.result(
        "QF1", api.player_id(8), by="bob", key="bob-result-attempt"
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "match_already_completed"

    history = api.history()
    matching = [m for m in history["matches"] if m["slot_id"] == "QF1"]
    assert len(matching) == 1
    assert len(matching[0]["result_versions"]) == 1
    assert matching[0]["result"]["id"] == first["result_version_id"]


def test_start_and_result_request_dedup(api: Api):
    key_start = "start-qf1-once"
    a = api.start("QF1", key=key_start)
    b = api.start("QF1", key=key_start)
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["match"]["id"] == b.json()["match"]["id"]
    assert len([m for m in api.history()["matches"] if m["slot_id"] == "QF1"]) == 1

    key_result = "result-qf1-once"
    r1 = api.result("QF1", api.player_id(1), key=key_result)
    r2 = api.result("QF1", api.player_id(1), key=key_result)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["result_version_id"] == r2.json()["result_version_id"]

    # A key cannot silently mean something else.
    conflict = api.result("QF1", api.player_id(8), key=key_result)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    # Missing key is a hard client error, not silently accepted.
    missing = api.client.post(
        f"/tournaments/{api.tournament_id}/slots/QF2/start",
        json={"started_by": "alice"},
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "idempotency_key_required"


def test_feeders_must_have_effective_winners(api: Api):
    response = api.start("SF1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "participants_not_ready"

    api.start_ok("QF1")
    api.result_ok("QF1", api.player_id(1))
    # Only one feeder done.
    response = api.start("SF1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "participants_not_ready"

    api.start_ok("QF2")
    api.result_ok("QF2", api.player_id(5))
    slot = api.slot("SF1")
    assert slot["state"] == "ready"
    assert slot["participants"]["a"]["seed"] == 1
    assert slot["participants"]["b"]["seed"] == 5


def test_completed_bout_cannot_be_restarted(api: Api):
    api.start_ok("QF1")
    api.result_ok("QF1", api.player_id(1))
    response = api.start("QF1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "match_already_completed"


def test_restart_keeps_state(db_path):
    from fastapi.testclient import TestClient as TC

    app = create_app(db_path)
    api = Api(TC(app))
    api.create()
    api.start_ok("QF1")
    api.result_ok("QF1", api.player_id(1))

    reopened = Api(TC(create_app(db_path)))
    reopened.tournament_id = api.tournament_id
    slot = reopened.slot("QF1")
    assert slot["state"] == "completed"
    assert slot["current_match"]["id"] == api.slot("QF1")["current_match"]["id"]
    assert len(reopened.history()["matches"]) == 1
