"""Robustness: replaced live bouts, races, pending reports, restart durability."""

from __future__ import annotations

import threading

from .conftest import Api, play_full_tournament, play_quarterfinals


def _slot(body: dict, slot_id: str) -> dict:
    for round_ in body["rounds"]:
        for slot in round_["slots"]:
            if slot["slot_id"] == slot_id:
                return slot
    raise AssertionError(slot_id)


def test_started_but_unfinished_descendant_is_preserved_as_history(api: Api):
    play_quarterfinals(api)
    sf1 = api.start_ok("SF1")["match"]
    assert sf1["status"] == "started"

    # Appeal the already-finished QF2 while SF1 is live.
    qf2_version = api.slot("QF2")["current_match"]["result"]["id"]
    appeal = api.appeal_ok("QF2")
    api.adjudicate_ok(appeal["appeal"]["id"], "overturned", qf2_version)

    bracket = api.bracket()
    slot = _slot(bracket, "SF1")
    # The live bout is not renamed, not deleted, and no longer current.
    assert slot["current_match"] is None
    assert slot["state"] == "ready"
    assert slot["participants"]["b"]["seed"] == 4
    assert len(slot["history"]) == 1
    old = slot["history"][0]
    assert old["id"] == sf1["id"]
    assert old["status"] == "started"
    assert old["participants"]["b"]["seed"] == 5
    assert old["result"] is None
    assert old["superseded"]["reason"] == "upstream_result_overturned"

    # A new bout with the corrected pair starts as attempt 2.
    new = api.start_ok("SF1")["match"]
    assert new["attempt"] == 2 and new["id"] != sf1["id"]

    # The historical bout is still directly retrievable by id.
    response = api.client.get(
        f"/tournaments/{api.tournament_id}/matches/{sf1['id']}"
    )
    assert response.status_code == 200
    assert response.json()["is_current"] is False


def test_result_can_be_reported_for_bout_already_live_during_appeal(api: Api):
    play_quarterfinals(api)
    api.start_ok("SF1")  # starts while QF2 still has its original result

    appeal = api.appeal_ok("QF2")
    # SF1 is already under way, so it is not paused; its result is a fact.
    reported = api.result_ok("SF1", api.player_id(1))
    assert reported["match"]["status"] == "completed"
    assert _slot(api.bracket(), "F")["suspension"]["appeal_ids"] == [
        appeal["appeal"]["id"]
    ]

    # Upholding keeps that semifinal as the current one.
    api.adjudicate_ok(
        appeal["appeal"]["id"],
        "upheld",
        api.slot("QF2")["current_match"]["result"]["id"],
    )
    bracket = api.bracket()
    assert _slot(bracket, "SF1")["current_match"]["id"] == reported["match"]["id"]
    assert _slot(bracket, "F")["suspension"]["appeal_ids"] == []


def test_concurrent_starts_create_only_one_live_bout(api: Api):
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def start(by: str, key: str) -> None:
        worker = Api(api.client)
        worker.tournament_id = api.tournament_id
        barrier.wait()
        response = worker.start("QF1", by=by, key=key)
        with lock:
            outcomes.append((response.status_code, response.json()))

    threads = [
        threading.Thread(target=start, args=("alice", "race-start-a")),
        threading.Thread(target=start, args=("bob", "race-start-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    codes = sorted(code for code, _ in outcomes)
    assert codes == [201, 409], outcomes
    qf1_matches = [
        m for m in api.history()["matches"] if m["slot_id"] == "QF1"
    ]
    assert len(qf1_matches) == 1


def test_concurrent_results_create_only_one_version(api: Api):
    api.start_ok("QF1")
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def report(seed: int, key: str) -> None:
        worker = Api(api.client)
        worker.tournament_id = api.tournament_id
        barrier.wait()
        response = worker.result(
            "QF1", worker.player_id(seed), by=f"staff-{seed}", key=key
        )
        with lock:
            outcomes.append((response.status_code, response.json()))

    threads = [
        threading.Thread(target=report, args=(1, "race-result-a")),
        threading.Thread(target=report, args=(8, "race-result-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    codes = sorted(code for code, _ in outcomes)
    assert codes == [200, 409], outcomes
    versions = api.slot("QF1")["current_match"]["result_versions"]
    assert len(versions) == 1


def test_stale_appeal_does_not_freeze_path(api: Api):
    play_full_tournament(api)
    sf1_version = api.slot("SF1")["current_match"]["result"]["id"]
    sf1_appeal = api.appeal_ok("SF1")

    qf2_version = api.slot("QF2")["current_match"]["result"]["id"]
    qf2_appeal = api.appeal_ok("QF2")
    api.adjudicate_ok(qf2_appeal["appeal"]["id"], "overturned", qf2_version)

    bracket = api.bracket()
    # SF1's appeal is stale, so it no longer blocks anything.
    assert bracket["pending_appeals"] == []
    assert _slot(bracket, "SF1")["suspension"]["appeal_ids"] == []
    assert _slot(bracket, "F")["suspension"]["appeal_ids"] == []

    stale = [
        a
        for a in api.history()["appeals"]
        if a["id"] == sf1_appeal["appeal"]["id"]
    ][0]
    assert stale["status"] == "stale"
    assert stale["result_version_id"] == sf1_version


def test_overturn_supersedes_new_downstream_chain_repeatedly(api: Api):
    """Nested overturns: v1 -> v2 -> v3, with downstream replays in between."""
    api.start_ok("QF1")
    api.result_ok("QF1", api.player_id(1))
    api.start_ok("QF2")
    api.result_ok("QF2", api.player_id(5))

    # First appeal: uphold, so SF1 is a normal current semifinal.
    v1 = api.slot("QF2")["current_match"]["result"]["id"]
    a1 = api.appeal_ok("QF2")
    api.adjudicate_ok(a1["appeal"]["id"], "upheld", v1)

    api.start_ok("SF1")
    api.result_ok("SF1", api.player_id(1))
    v2 = api.slot("QF2")["current_match"]["result"]["id"]

    # Second appeal: overturn; the completed SF1 is preserved as history.
    a2 = api.appeal_ok("QF2")
    api.adjudicate_ok(a2["appeal"]["id"], "overturned", v2)
    sf1_old = _slot(api.bracket(), "SF1")["history"][0]
    assert sf1_old["result"]["winner"]["seed"] == 1

    api.start_ok("SF1")
    api.result_ok("SF1", api.player_id(4))
    v3 = api.slot("QF2")["current_match"]["result"]["id"]

    # Third appeal: overturn once more (back to P5).  Both earlier SF1 bouts
    # are history; no winner from either is allowed to advance.
    a3 = api.appeal_ok("QF2")
    api.adjudicate_ok(a3["appeal"]["id"], "overturned", v3)
    bracket = api.bracket()
    sf1 = _slot(bracket, "SF1")
    assert [h["id"] for h in sf1["history"]] == [
        sf1_old["id"],
        _slot(api.bracket(), "SF1")["history"][1]["id"],
    ]
    assert sf1["participants"]["b"]["seed"] == 5
    assert _slot(bracket, "F")["state"] == "waiting"
    assert bracket["champion"] is None


def test_matches_endpoint_404s_for_wrong_tournament(api: Api):
    api.start_ok("QF1")
    other = Api(api.client)
    other.create()
    missing = api.client.get(
        f"/tournaments/{other.tournament_id}/matches/1"
    )
    assert missing.status_code == 404
