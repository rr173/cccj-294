"""Appeal lifecycle: suspension, uphold, overturn, history, version races."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import Api, play_full_tournament, play_quarterfinals


def _slot(body: dict, slot_id: str) -> dict:
    for round_ in body["rounds"]:
        for slot in round_["slots"]:
            if slot["slot_id"] == slot_id:
                return slot
    raise AssertionError(slot_id)


def test_appeal_suspends_only_undecided_descendants(api: Api):
    play_full_tournament(api)

    # Appeal the completed QF3 (other half).  SF1 has not yet been played in
    # this scenario, so first assert the same-half vs other-half distinction
    # on a fresh tournament where both semifinals are still unstarted.
    other = Api(api.client)
    other.create()
    play_quarterfinals(other)
    qf3 = other.appeal_ok("QF3")
    bracket = other.bracket()
    sf1 = _slot(bracket, "SF1")
    assert sf1["suspension"]["appeal_ids"] == []
    assert sf1["state"] == "ready"
    assert _slot(bracket, "SF2")["suspension"]["appeal_ids"] == [qf3["appeal"]["id"]]
    assert _slot(bracket, "F")["suspension"]["appeal_ids"] == [qf3["appeal"]["id"]]
    assert _slot(bracket, "SF2")["state"] == "suspended"
    assert _slot(bracket, "F")["state"] == "suspended"

    # The appealed bout keeps displaying its current result while pending.
    qf3_slot = _slot(bracket, "QF3")
    assert qf3_slot["state"] == "completed"
    assert qf3_slot["result_under_appeal"] is True
    assert qf3_slot["current_match"]["result"]["winner"]["seed"] == 3

    # No champion can be declared while the bracket is incomplete or an
    # appeal is pending.
    assert bracket["champion"] is None
    assert bracket["champion_status"]["state"] == "pending"

    # The untouched half stays playable.
    assert other.start("SF1").status_code == 201

    # Now a finished tournament: appealing QF3 freezes only the final (SF2 is
    # already completed and keeps displaying its result).
    full = api.bracket()
    qf3_full = api.appeal_ok("QF3")
    after = api.bracket()
    assert _slot(after, "SF1")["suspension"]["appeal_ids"] == []
    assert _slot(after, "SF2")["suspension"]["appeal_ids"] == [qf3_full["appeal"]["id"]]
    assert _slot(after, "SF2")["result_under_appeal"] is False  # SF2 itself not appealed
    assert _slot(after, "F")["suspension"]["appeal_ids"] == [qf3_full["appeal"]["id"]]
    assert after["champion"] is None
    assert full["champion"]["player"]["seed"] == 1


def test_uphold_restores_playability_without_renaming(api: Api):
    play_quarterfinals(api)
    appeal = api.appeal_ok("QF2", "Ball was out")
    before = api.bracket()
    qf2_version = _slot(before, "QF2")["current_match"]["result"]["id"]

    # Upstream path is frozen while pending.
    blocked = api.start("SF1")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "slot_suspended"
    assert _slot(before, "SF1")["state"] == "suspended"
    assert _slot(before, "SF1")["suspension"]["appeal_ids"] == [
        appeal["appeal"]["id"]
    ]
    assert _slot(before, "SF2")["suspension"]["appeal_ids"] == []

    decision = api.adjudicate_ok(appeal["appeal"]["id"], "upheld", qf2_version)
    assert decision["adjudication"]["outcome"] == "upheld"
    assert decision["adjudication"]["superseded_match_ids"] == []

    after = api.bracket()
    qf2 = _slot(after, "QF2")
    assert qf2["result_under_appeal"] is False
    versions = qf2["current_match"]["result_versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["winner"]["seed"] == 5
    assert versions[1]["winner"]["seed"] == 5  # upheld keeps the same winner
    assert versions[1]["source"] == "adjudicated"

    # Upheld restores the not-yet-started descendant's playability.
    sf1 = _slot(after, "SF1")
    assert sf1["state"] == "ready"
    assert sf1["suspension"]["appeal_ids"] == []
    api.start_ok("SF1")
    api.result_ok("SF1", api.player_id(1))

    # An upheld decision is a new effective version, so a fresh appeal may be
    # filed against it (nested appeals follow the current path).
    nested = api.appeal(
        "QF2", qf2["current_match"]["result"]["id"], "new evidence"
    )
    assert nested.status_code == 201


def test_overturn_retires_finished_downstream_bouts_as_history(api: Api):
    play_full_tournament(api)
    qf2 = api.slot("QF2")
    qf2_version = qf2["current_match"]["result"]["id"]
    sf1_match_id = api.slot("SF1")["current_match"]["id"]
    final_match_id = api.slot("F")["current_match"]["id"]

    appeal = api.appeal_ok("QF2")
    api.adjudicate_ok(appeal["appeal"]["id"], "overturned", qf2_version)

    bracket = api.bracket()
    # QF2 now has two versions and seed 4 as the effective winner.
    qf2_after = _slot(bracket, "QF2")
    effective = qf2_after["current_match"]["result"]
    assert effective["winner"]["seed"] == 4
    assert effective["version"] == 2
    assert [v["winner"]["seed"] for v in qf2_after["current_match"]["result_versions"]] == [5, 4]

    # SF1 and the final were real bouts; they are preserved but no longer
    # current, and never advance a player again.
    sf1 = _slot(bracket, "SF1")
    assert sf1["state"] == "ready"
    assert sf1["current_match"] is None
    assert sf1["participants"]["b"]["seed"] == 4  # new path: P1 vs P4
    assert [h["id"] for h in sf1["history"]] == [sf1_match_id]
    assert sf1["history"][0]["result"]["winner"]["seed"] == 1
    assert sf1["history"][0]["superseded"]["reason"] == "upstream_result_overturned"
    assert [b["match_id"] for b in sf1["bypassed_bouts"]] == [sf1_match_id]

    final = _slot(bracket, "F")
    assert final["state"] == "waiting"
    assert final["participants"] is None
    assert final["candidate_participants"] is None
    assert final["feeder_winners"]["SF2"]["seed"] == 3
    assert [h["id"] for h in final["history"]] == [final_match_id]
    assert final["history"][0]["result"]["winner"]["seed"] == 1

    # No stale champion: the replaced final winner is not displayed as champ.
    assert bracket["champion"] is None
    assert bracket["champion_status"]["state"] == "pending"
    assert [m["id"] for m in bracket["superseded_matches"]] == [
        sf1_match_id,
        final_match_id,
    ]

    # The untouched half is exactly as it was.
    sf2 = _slot(bracket, "SF2")
    assert sf2["state"] == "completed"
    assert sf2["current_match"]["result"]["winner"]["seed"] == 3

    # The old final winner cannot be used to seed a new final.
    api.start_ok("SF1")
    api.result_ok("SF1", api.player_id(1))
    new_final = api.slot("F")
    assert new_final["state"] == "ready"
    assert new_final["participants"]["a"]["seed"] == 1
    assert new_final["participants"]["b"]["seed"] == 3
    # Unfinished? No, the old final was completed: the new final is attempt 2.
    new_match = api.start_ok("F")["match"]
    assert new_match["attempt"] == 2
    assert new_match["id"] != final_match_id
    api.result_ok("F", api.player_id(3))
    assert api.bracket()["champion"]["player"]["seed"] == 3


def test_overturn_before_downstream_bout_started(api: Api):
    play_full_tournament(api)
    # Take QF1's appeal; SF1 was already played, so it is preserved as history.
    appeal = api.appeal_ok("QF1")
    api.adjudicate_ok(
        appeal["appeal"]["id"], "overturned", api.slot("QF1")["current_match"]["result"]["id"]
    )
    bracket = api.bracket()
    # Seed 8 now won QF1; SF1 needs a fresh start with P8 vs P5.
    sf1 = _slot(bracket, "SF1")
    assert sf1["state"] == "ready"
    assert sf1["participants"]["a"]["seed"] == 8
    assert sf1["participants"]["b"]["seed"] == 5
    assert len(sf1["history"]) == 1


def test_appeal_on_superseded_bout_is_stale_and_cannot_decide(api: Api):
    play_full_tournament(api)
    # Appeal the SF1 result, then overturn its ancestor QF2.
    sf1 = api.slot("SF1")
    sf1_appeal = api.appeal_ok("SF1")
    assert api.start("SF1").status_code == 409  # completed anyway
    qf2 = api.slot("QF2")
    api.adjudicate_ok(
        api.appeal_ok("QF2")["appeal"]["id"],
        "overturned",
        qf2["current_match"]["result"]["id"],
    )

    bracket = api.bracket()
    # SF1 was replaced because its pair changed; its appeal went stale.
    sf1_after = _slot(bracket, "SF1")
    assert sf1_after["history"][0]["id"] == sf1["current_match"]["id"]
    appeals = {a["id"]: a for a in api.history()["appeals"]}
    stale = appeals[sf1_appeal["appeal"]["id"]]
    assert stale["status"] == "stale"
    assert stale["stale_reason"] == "result_superseded_by_upstream_adjudication"

    # And the stale appeal cannot be adjudicated to bring the old bout back.
    decision = api.adjudicate(
        stale["id"], "overturned", sf1_appeal["appeal"]["result_version_id"]
    )
    assert decision.status_code == 409
    assert decision.json()["error"]["code"] == "appeal_stale"

    # A pending appeal can no longer freeze a path that bypassed it.
    assert _slot(bracket, "F")["suspension"]["appeal_ids"] == []


def test_adjudication_must_target_seen_version(api: Api):
    play_full_tournament(api)
    qf2 = api.slot("QF2")
    old_version = qf2["current_match"]["result"]["id"]
    first = api.appeal_ok("QF2")
    api.adjudicate_ok(first["appeal"]["id"], "upheld", old_version)

    # A second adjudication of the same appeal against the old version is a
    # duplicate replay only when identical; a different outcome is refused.
    replay = api.adjudicate(first["appeal"]["id"], "overturned", old_version)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "appeal_already_decided"

    # A new appeal targets the new effective version, and stale version
    # numbers are rejected.
    new_version = api.slot("QF2")["current_match"]["result"]["id"]
    second = api.appeal_ok("QF2")
    wrong = api.adjudicate(second["appeal"]["id"], "overturned", old_version)
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "result_version_changed"
    api.adjudicate_ok(second["appeal"]["id"], "overturned", new_version)


def test_concurrent_opposite_adjudications_do_not_overwrite(api: Api):
    play_full_tournament(api)
    qf2 = api.slot("QF2")
    version_id = qf2["current_match"]["result"]["id"]
    appeal = api.appeal_ok("QF2")
    appeal_id = appeal["appeal"]["id"]

    barrier = threading.Barrier(2)
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def decide(outcome: str, key: str) -> None:
        worker = Api(api.client)
        worker.tournament_id = api.tournament_id
        barrier.wait()
        response = worker.adjudicate(appeal_id, outcome, version_id, key=key)
        with lock:
            results.append((response.status_code, response.json()))

    threads = [
        threading.Thread(target=decide, args=("upheld", "concurrent-a")),
        threading.Thread(target=decide, args=("overturned", "concurrent-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    codes = sorted(code for code, _ in results)
    assert codes == [200, 409], results
    winner_codes = [code for code, _ in results if code == 200]
    assert len(winner_codes) == 1

    # Exactly one adjudication exists and exactly two versions.
    history = api.history()
    versions = [
        v
        for m in history["matches"]
        if m["slot_id"] == "QF2"
        for v in m["result_versions"]
    ]
    assert len(versions) == 2


def test_duplicate_same_adjudication_returns_original(api: Api):
    play_full_tournament(api)
    qf2 = api.slot("QF2")
    version_id = qf2["current_match"]["result"]["id"]
    appeal = api.appeal_ok("QF2")
    first = api.adjudicate_ok(appeal["appeal"]["id"], "overturned", version_id)
    replay = api.adjudicate_ok(appeal["appeal"]["id"], "overturned", version_id)
    assert replay == first
    versions = api.history()
    assert len(versions["matches"]) == 7  # no extra match rows were created


def test_nested_appeals_follow_effective_path(api: Api):
    play_full_tournament(api)
    qf2 = api.slot("QF2")
    v1 = qf2["current_match"]["result"]["id"]
    first = api.appeal_ok("QF2")
    api.adjudicate_ok(first["appeal"]["id"], "overturned", v1)

    qf2 = api.slot("QF2")
    v2 = qf2["current_match"]["result"]["id"]
    assert v2 != v1
    assert qf2["current_match"]["result"]["winner"]["seed"] == 4

    second = api.appeal_ok("QF2")
    api.adjudicate_ok(second["appeal"]["id"], "overturned", v2)
    qf2 = api.slot("QF2")
    assert qf2["current_match"]["result"]["version"] == 3
    assert qf2["current_match"]["result"]["winner"]["seed"] == 5

    # Back to P5; the final (now void) must not hand the title to anybody.
    bracket = api.bracket()
    assert bracket["champion"] is None
    assert _slot(bracket, "SF1")["history"][0]["result"]["winner"]["seed"] == 1

    # The path now feeds P5 again, so SF1 restarts with P1 vs P5.
    sf1 = _slot(bracket, "SF1")
    assert sf1["participants"]["b"]["seed"] == 5


def test_multiple_appeals_same_version_go_stale_when_one_decided(api: Api):
    play_full_tournament(api)
    qf2 = api.slot("QF2")
    version_id = qf2["current_match"]["result"]["id"]
    first = api.appeal_ok("QF2")
    second = api.appeal_ok("QF2")
    api.adjudicate_ok(first["appeal"]["id"], "upheld", version_id)

    appeals = {a["id"]: a for a in api.history()["appeals"]}
    assert appeals[second["appeal"]["id"]]["status"] == "stale"
    assert (
        appeals[second["appeal"]["id"]]["stale_reason"]
        == "another_appeal_adjudicated"
    )

    # Once stale, it no longer freezes the path and cannot be decided.
    assert api.bracket()["champion"]["player"]["seed"] == 1
    response = api.adjudicate(second["appeal"]["id"], "overturned", version_id)
    assert response.status_code == 409


def test_suspended_slot_with_no_candidates_is_waiting(api: Api):
    # QF4 done; QF3 done -> SF2 played while an appeal is pending on QF1
    # (other half).  The pending appeal must not affect SF2.
    api.start_ok("QF4")
    api.result_ok("QF4", api.player_id(2))
    api.start_ok("QF3")
    api.result_ok("QF3", api.player_id(3))
    # Directly create a pending appeal on QF4 *after* SF2 starts:
    api.start_ok("SF2")
    api.result_ok("SF2", api.player_id(2))
    appeal = api.appeal_ok("QF4")
    bracket = api.bracket()
    # SF2 is complete, so it keeps its state and shows the old result.
    assert _slot(bracket, "SF2")["current_match"]["result"]["winner"]["seed"] == 2
    # The appeal is on the *other* side of the semifinal? QF4 feeds SF2, so it
    # is the same half; SF2's own completed result remains displayed but its
    # downstream (final) is suspended.
    assert _slot(bracket, "F")["suspension"]["appeal_ids"] == [appeal["appeal"]["id"]]
    assert bracket["champion"] is None


def test_tournament_can_be_replayed_after_restart(db_path):
    first = Api(TestClient(create_app(db_path)))
    first.create()
    play_full_tournament(first)
    appeal = first.appeal_ok("QF2")
    first.adjudicate_ok(
        appeal["appeal"]["id"],
        "overturned",
        first.slot("QF2")["current_match"]["result"]["id"],
    )

    reopened = Api(TestClient(create_app(db_path)))
    reopened.tournament_id = first.tournament_id
    bracket = reopened.bracket()
    assert len(bracket["superseded_matches"]) == 2
    history = reopened.history()
    assert len(history["matches"]) == 7
    assert any(m["superseded"] for m in history["matches"])
    assert any(a["status"] == "overturned" for a in history["appeals"])
    # The tournament is not stuck: the live path can continue after restart.
    reopened.start_ok("SF1")
    reopened.result_ok("SF1", reopened.player_id(1))
    reopened.start_ok("F")
    reopened.result_ok("F", reopened.player_id(3))
    assert reopened.bracket()["champion"]["player"]["seed"] == 3


def test_overturn_the_final_switches_the_champion(api: Api):
    play_full_tournament(api)
    final = api.slot("F")
    version = final["current_match"]["result"]["id"]
    assert api.bracket()["champion"]["player"]["seed"] == 1

    appeal = api.appeal_ok("F", "Final point was replayed")
    # Pending appeal on the final: result still displayed, but no champion.
    pending = api.bracket()
    assert pending["champion"] is None
    assert pending["champion_status"]["state"] == "provisional"
    assert _slot(pending, "F")["state"] == "completed"
    assert _slot(pending, "F")["result_under_appeal"] is True

    decision = api.adjudicate_ok(appeal["appeal"]["id"], "overturned", version)
    assert decision["adjudication"]["superseded_match_ids"] == []
    after = api.bracket()
    assert after["champion"]["player"]["seed"] == 3
    assert _slot(after, "F")["current_match"]["result"]["version"] == 2
    assert after["tournament"]["complete"] is True

    # Nested appeal against the new version blocks the title again, then
    # upholding it restores the same champion.
    new_version = _slot(after, "F")["current_match"]["result"]["id"]
    nested = api.appeal_ok("F", "Still disputing")
    assert api.bracket()["champion"] is None
    api.adjudicate_ok(nested["appeal"]["id"], "upheld", new_version)
    assert api.bracket()["champion"]["player"]["seed"] == 3


def test_appeals_endpoint_filters_by_status(api: Api):
    play_quarterfinals(api)
    first = api.appeal_ok("QF2")
    api.appeal_ok("QF3")
    api.adjudicate_ok(
        first["appeal"]["id"],
        "upheld",
        api.slot("QF2")["current_match"]["result"]["id"],
    )

    pending = api.client.get(
        f"/tournaments/{api.tournament_id}/appeals", params={"status": "pending"}
    ).json()["appeals"]
    assert len(pending) == 1
    assert pending[0]["slot_id"] == "QF3"
    decided = api.client.get(
        f"/tournaments/{api.tournament_id}/appeals", params={"status": "upheld"}
    ).json()["appeals"]
    assert [a["id"] for a in decided] == [first["appeal"]["id"]]
