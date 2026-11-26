"""Tests for prize funds, payment commitments, bank results and recoveries."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import service, settlement
from app.db import SCHEMA, connect, init_db
from app.main import create_app
from tests.conftest import Api, play_full_tournament


CHAMPION_PRIZE = 10_000
RUNNER_UP_PRIZE = 5_000


@pytest.fixture
def settled_api(api: Api) -> Api:
    play_full_tournament(api)
    return api


def _set_prizes(api: Api, *, champion: int = CHAMPION_PRIZE, runner_up: int = RUNNER_UP_PRIZE):
    response = api.client.post(
        f"/tournaments/{api.tournament_id}/prize-settings",
        json={
            "champion_amount_cents": champion,
            "runner_up_amount_cents": runner_up,
            "set_by": "treasurer",
        },
        headers={"Idempotency-Key": "prize-settings-1"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _add_funds(api: Api, amount: int, key: str | None = None):
    response = api.client.post(
        f"/tournaments/{api.tournament_id}/funds",
        json={"amount_cents": amount, "funded_by": "finance"},
        headers={"Idempotency-Key": key or api.fresh_key("funds")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _settlement(api: Api) -> dict:
    response = api.client.get(f"/tournaments/{api.tournament_id}/settlement")
    assert response.status_code == 200, response.text
    return response.json()


def _player_row(settlement_body: dict, player_id: int) -> dict:
    for row in settlement_body["players"]:
        if row["player_id"] == player_id:
            return row
    raise AssertionError(f"no settlement row for player {player_id}")


def _issue_payment(
    api: Api,
    player_id: int,
    amount: int,
    version_id: int | None = None,
    key: str | None = None,
    *,
    expected_status: int = 201,
):
    body = {"player_id": player_id, "amount_cents": amount}
    if version_id is not None:
        body["result_version_id"] = version_id
    response = api.client.post(
        f"/tournaments/{api.tournament_id}/payments",
        json=body,
        headers={"Idempotency-Key": key or api.fresh_key("payment")},
    )
    assert response.status_code == expected_status, response.text
    return response


def _confirm(
    api_or_client,
    payment_id: int,
    body: dict,
    key: str | None = None,
    tournament_id: int | None = None,
):
    client = api_or_client.client if isinstance(api_or_client, Api) else api_or_client
    tid = tournament_id or (
        api_or_client.tournament_id if isinstance(api_or_client, Api) else None
    )
    path = f"/payments/{payment_id}/confirm"
    return client.post(
        path,
        json=body,
        headers={"Idempotency-Key": key or "confirm-key"},
    )


def _overturn_final(api: Api) -> tuple[int, int]:
    """Appeal and overturn the final; return old/new version ids."""
    final_slot = api.slot("F")
    old_version = final_slot["current_match"]["result"]["id"]
    appeal = api.appeal_ok("F")["appeal"]["id"]
    api.adjudicate_ok(appeal, "overturned", old_version)
    settlement_body = _settlement(api)
    new_version = settlement_body["effective_final_result_version_id"]
    assert new_version and new_version != old_version
    return old_version, new_version


def test_prize_settings_are_one_shot_and_funds_are_tournament_scoped(
    settled_api: Api, client: TestClient
):
    api = settled_api
    first = _set_prizes(api)
    assert first["prize_settings"]["champion_amount_cents"] == CHAMPION_PRIZE

    duplicate = api.client.post(
        f"/tournaments/{api.tournament_id}/prize-settings",
        json={"champion_amount_cents": 1, "runner_up_amount_cents": 2},
        headers={"Idempotency-Key": "prize-settings-2"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "prize_settings_already_set"

    # The same key/body replays the original creation.
    replay = api.client.post(
        f"/tournaments/{api.tournament_id}/prize-settings",
        json={
            "champion_amount_cents": CHAMPION_PRIZE,
            "runner_up_amount_cents": RUNNER_UP_PRIZE,
            "set_by": "treasurer",
        },
        headers={"Idempotency-Key": "prize-settings-1"},
    )
    assert replay.status_code == 201
    assert replay.json()["prize_settings"]["created_at"] == first["prize_settings"]["created_at"]

    _add_funds(api, 12_000, "fund-1")
    _add_funds(api, 3_000, "fund-2")
    body = _settlement(api)
    assert body["prize_pool"]["funding_total_cents"] == 15_000
    assert body["available_funds_cents"] == 15_000

    # Another tournament cannot see or spend this tournament's money.
    other = Api(client)
    other.create(names=[f"Q{i}" for i in range(1, 9)])
    other_settlement = _settlement(other)
    assert other_settlement["available_funds_cents"] == 0
    assert other_settlement["payments"] == []


def test_payment_limit_uses_prize_net_paid_and_pending_and_funds(settled_api: Api):
    api = settled_api
    _set_prizes(api)
    _add_funds(api, 100_000)
    champion_id = api.player_id(1)
    version = _settlement(api)["effective_final_result_version_id"]

    _issue_payment(api, champion_id, 6_000, version, "pay-6000")
    body = _settlement(api)
    row = _player_row(body, champion_id)
    assert row["unconfirmed_payment_cents"] == 6_000
    assert row["issuable_balance_cents"] == 4_000

    over_limit = _issue_payment(
        api, champion_id, 4_001, version, "pay-too-much", expected_status=409
    )
    assert over_limit.json()["error"]["code"] == "payment_exceeds_award_balance"

    # A second, distinct payment can use the remaining capacity.
    _issue_payment(api, champion_id, 4_000, version, "pay-4000")
    body = _settlement(api)
    row = _player_row(body, champion_id)
    assert row["unconfirmed_payment_cents"] == 10_000
    assert row["issuable_balance_cents"] == 0

    # Failed confirmation releases the commitment.
    payment_id = body["payments"][0]["id"]
    failed = _confirm(
        api,
        payment_id,
        {"status": "failed", "failure_reason": "bank rejected account"},
        "fail-1",
    )
    assert failed.status_code == 200, failed.text
    body = _settlement(api)
    row = _player_row(body, champion_id)
    assert row["unconfirmed_payment_cents"] == 4_000
    assert row["issuable_balance_cents"] == 6_000
    assert body["available_funds_cents"] == 100_000 - 4_000


def test_payment_cannot_promise_same_funds_twice(settled_api: Api):
    api = settled_api
    _set_prizes(api, champion=600, runner_up=400)
    _add_funds(api, 600, "only-600")
    champion = api.player_id(1)
    runner_up = api.player_id(3)
    version = _settlement(api)["effective_final_result_version_id"]

    response = _issue_payment(api, champion, 600, version, "champ-pay")
    payment_id = response.json()["id"]

    blocked = _issue_payment(
        api, runner_up, 400, version, "runner-pay", expected_status=409
    )
    assert blocked.json()["error"]["code"] == "insufficient_available_funds"

    # Failure releases the cash and the runner-up can now be paid.
    confirmed = _confirm(
        api, payment_id, {"status": "failed", "reason": "bad account"}, "fail-champ"
    )
    assert confirmed.status_code == 200
    _issue_payment(api, runner_up, 400, version, "runner-pay-201")
    settlement_body = _settlement(api)
    assert settlement_body["available_funds_cents"] == 200


def test_core_story_confirm_after_readjudication_recover_and_pay_new_winner(
    settled_api: Api,
):
    api = settled_api
    champion_seed1 = api.player_id(1)
    new_champion_seed3 = api.player_id(3)

    _set_prizes(api)
    _add_funds(api, 12_000, "fund-12000")
    _add_funds(api, 3_000, "fund-3000")
    old_version = _settlement(api)["effective_final_result_version_id"]

    payment_response = _issue_payment(
        api, champion_seed1, 10_000, old_version, "old-champion-payment"
    )
    old_payment = payment_response.json()["payment"]
    assert old_payment["recipient_player_id"] == champion_seed1
    assert old_payment["result_version_id"] == old_version
    assert old_payment["status"] == "pending"

    # While the final result is appealed, new payments/recoveries pause, but
    # the existing order remains a pending commitment.
    api.appeal_ok("F")
    paused = _settlement(api)
    assert paused["settlement_status"] == "suspended"
    assert paused["paused"] is True
    old_row = _player_row(paused, champion_seed1)
    assert old_row["prize_amount_cents"] is None
    assert old_row["recoverable_amount_cents"] is None
    assert old_row["unconfirmed_payment_cents"] == 10_000
    assert len(paused["payments"]) == 1

    blocked_payment = _issue_payment(
        api, champion_seed1, 1, old_version, "blocked-pay", expected_status=409
    )
    assert blocked_payment.json()["error"]["code"] == "settlement_suspended"
    blocked_recovery = api.client.post(
        f"/tournaments/{api.tournament_id}/recoveries",
        json={"player_id": champion_seed1, "amount_cents": 1},
        headers={"Idempotency-Key": "blocked-recovery"},
    )
    assert blocked_recovery.status_code == 409
    assert blocked_recovery.json()["error"]["code"] == "settlement_suspended"

    # Overturn the final: seed 3 is now champion and seed 1 runner-up.
    appeal_id = api.slot("F")["current_match"]["result"]["appeals"][-1]["id"]
    api.adjudicate_ok(appeal_id, "overturned", old_version, key="overturn-final")
    readjudicated = _settlement(api)
    new_version = readjudicated["effective_final_result_version_id"]
    assert readjudicated["settlement_status"] == "decided"

    # The pending order was not rewritten by the new result version.
    assert readjudicated["payments"][0]["recipient_player_id"] == champion_seed1
    assert readjudicated["payments"][0]["result_version_id"] == old_version
    assert readjudicated["payments"][0]["amount_cents"] == 10_000

    # Bank confirms only after readjudication.  It still pays the original
    # recipient and creates the overpayment/receivable.
    confirm_response = _confirm(
        api,
        old_payment["id"],
        {"status": "success", "bank_receipt_number": "BANK-RCPT-1"},
        "confirm-old",
    )
    assert confirm_response.status_code == 200, confirm_response.text
    assert confirm_response.json()["bank_receipt_number"] == "BANK-RCPT-1"

    after_bank = _settlement(api)
    old_row = _player_row(after_bank, champion_seed1)
    new_row = _player_row(after_bank, new_champion_seed3)
    assert old_row["paid_cents"] == 10_000
    assert old_row["net_paid_cents"] == 10_000
    assert old_row["current_prize_amount_cents"] == 5_000
    assert old_row["recoverable_amount_cents"] == 5_000
    assert new_row["current_prize_amount_cents"] == 10_000
    assert after_bank["available_funds_cents"] == 5_000

    # Actual partial/full recovery of the currently recoverable difference.
    recovery_response = api.client.post(
        f"/tournaments/{api.tournament_id}/recoveries",
        json={
            "player_id": champion_seed1,
            "amount_cents": 5_000,
            "result_version_id": new_version,
            "received_by": "cashier",
        },
        headers={"Idempotency-Key": "recover-5000"},
    )
    assert recovery_response.status_code == 201, recovery_response.text
    recovery = recovery_response.json()["recovery"]
    assert recovery["result_version_id"] == new_version

    after_recovery = _settlement(api)
    assert after_recovery["available_funds_cents"] == 10_000
    old_row = _player_row(after_recovery, champion_seed1)
    assert old_row["net_paid_cents"] == 5_000
    assert old_row["recovered_cents"] == 5_000
    assert old_row["recoverable_amount_cents"] == 0

    # Recovered money is available for payment to the new champion.
    new_payment = _issue_payment(
        api, new_champion_seed3, 10_000, new_version, "new-champion-payment"
    ).json()
    confirm_new = _confirm(
        api,
        new_payment["id"],
        {"status": "success", "bank_receipt_number": "BANK-RCPT-2"},
        "confirm-new",
    )
    assert confirm_new.status_code == 200

    final_body = _settlement(api)
    old_row = _player_row(final_body, champion_seed1)
    new_row = _player_row(final_body, new_champion_seed3)
    assert old_row["net_paid_cents"] == 5_000
    assert old_row["recoverable_amount_cents"] == 0
    assert new_row["paid_cents"] == 10_000
    assert new_row["net_paid_cents"] == 10_000
    assert new_row["recoverable_amount_cents"] == 0
    assert final_body["available_funds_cents"] == 0

    # Readjudication differences are non-cash records; real cash events remain
    # separately identifiable in the ledger.
    diff = final_body["award_differences"][-1]
    deltas = {c["player_id"]: c["delta_cents"] for c in diff["changes"]}
    assert deltas[champion_seed1] == -5_000
    assert deltas[new_champion_seed3] == 5_000
    cash_types = {e["type"] for e in final_body["cash_ledger"]}
    assert {"funding_added", "payment_succeeded", "recovery_received"} <= cash_types
    assert all(e["is_actual_cash_movement"] for e in final_body["cash_ledger"])


def test_repeated_success_returns_receipt_and_same_key_conflicts(settled_api: Api):
    api = settled_api
    _set_prizes(api)
    _add_funds(api, 100_000)
    champion = api.player_id(1)
    version = _settlement(api)["effective_final_result_version_id"]
    payment_id = _issue_payment(api, champion, 1_000, version, "pay-idem").json()["id"]

    first = _confirm(
        api,
        payment_id,
        {"status": "success", "bank_receipt_number": "R-OK"},
        "confirm-key-1",
    )
    second = _confirm(
        api,
        payment_id,
        {"status": "success", "bank_receipt_number": "R-OK"},
        "confirm-key-1",
    )
    assert first.status_code == second.status_code == 200
    assert second.json()["bank_receipt_number"] == "R-OK"

    conflict = _confirm(
        api,
        payment_id,
        {"status": "failed", "failure_reason": "different request"},
        "confirm-key-1",
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    body = _settlement(api)
    assert body["payments"][0]["status"] == "success"
    assert body["payments"][0]["bank_receipt_number"] == "R-OK"


def test_successful_bank_receipt_cannot_be_used_on_another_payment(settled_api: Api):
    api = settled_api
    _set_prizes(api, champion=100_000, runner_up=100_000)
    _add_funds(api, 1_000_000)
    champion = api.player_id(1)
    runner_up = api.player_id(3)
    version = _settlement(api)["effective_final_result_version_id"]
    first_id = _issue_payment(api, champion, 100, version, "receipt-a").json()["id"]
    second_id = _issue_payment(api, runner_up, 100, version, "receipt-b").json()["id"]

    ok = _confirm(
        api, first_id, {"status": "success", "bank_receipt_number": "UNIQUE-R"}, "k-a"
    )
    assert ok.status_code == 200
    duplicate = _confirm(
        api, second_id, {"status": "success", "bank_receipt_number": "UNIQUE-R"}, "k-b"
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "bank_receipt_already_used"

    # A different receipt can confirm the second order.
    ok2 = _confirm(
        api, second_id, {"status": "success", "bank_receipt_number": "UNIQUE-R-2"}, "k-c"
    )
    assert ok2.status_code == 200


def test_superseded_final_pauses_but_existing_payment_can_confirm(settled_api: Api):
    api = settled_api
    _set_prizes(api)
    _add_funds(api, 100_000)
    champion = api.player_id(1)
    version = _settlement(api)["effective_final_result_version_id"]
    payment_id = _issue_payment(api, champion, 1_000, version, "pay-before-invalid").json()["id"]

    # Overturn SF2's result.  Its winner seed 3 is replaced by seed 2; the
    # completed final pair no longer matches the effective path and is retired.
    sf2 = api.slot("SF2")
    sf2_version = sf2["current_match"]["result"]["id"]
    appeal_id = api.appeal_ok("SF2")["appeal"]["id"]
    api.adjudicate_ok(appeal_id, "overturned", sf2_version, key="overturn-sf2")

    body = _settlement(api)
    assert body["settlement_status"] == "suspended"
    assert body["reason"] == "final_superseded"
    assert body["effective_final"] is None
    champion_row = _player_row(body, champion)
    assert champion_row["prize_amount_cents"] is None
    assert champion_row["recoverable_amount_cents"] is None

    blocked = _issue_payment(
        api, champion, 1, expected_status=409
    )
    assert blocked.json()["error"]["code"] == "settlement_suspended"

    # Bank confirmation is not blocked by suspension.
    confirmed = _confirm(
        api,
        payment_id,
        {"status": "success", "bank_receipt_number": "LATE-RCPT"},
        "late-confirm",
    )
    assert confirmed.status_code == 200, confirmed.text
    body = _settlement(api)
    assert body["settlement_status"] == "suspended"
    assert _player_row(body, champion)["paid_cents"] == 1_000
    assert body["payments"][0]["bank_receipt_number"] == "LATE-RCPT"


def test_player_removed_from_new_final_has_zero_award_and_full_receivable(
    settled_api: Api,
):
    api = settled_api
    _set_prizes(api)
    _add_funds(api, 100_000)
    old_champion = api.player_id(1)
    version = _settlement(api)["effective_final_result_version_id"]
    payment_id = _issue_payment(
        api, old_champion, 10_000, version, "old-champ-full"
    ).json()["id"]
    assert _confirm(
        api,
        payment_id,
        {"status": "success", "bank_receipt_number": "OLD-CHAMP-R"},
        "confirm-old-champ",
    ).status_code == 200

    # Overturn SF1: seed 5 replaces seed 1 and the old final is invalidated.
    sf1_version = api.slot("SF1")["current_match"]["result"]["id"]
    appeal_id = api.appeal_ok("SF1")["appeal"]["id"]
    api.adjudicate_ok(appeal_id, "overturned", sf1_version, key="overturn-sf1-full")
    assert _settlement(api)["reason"] == "final_superseded"

    # Play the replacement final.  Old champion seed 1 is not a participant.
    api.start_ok("F")
    api.result_ok("F", api.player_id(3), key="new-final-result")
    body = _settlement(api)
    assert body["settlement_status"] == "decided"
    old_row = _player_row(body, old_champion)
    assert old_row["role"] is None
    assert old_row["prize_amount_cents"] == 0
    assert old_row["net_paid_cents"] == 10_000
    assert old_row["recoverable_amount_cents"] == 10_000

    # Partial recovery leaves a correct remaining receivable.
    partial = api.client.post(
        f"/tournaments/{api.tournament_id}/recoveries",
        json={"player_id": old_champion, "amount_cents": 4_000},
        headers={"Idempotency-Key": "partial-recovery"},
    )
    assert partial.status_code == 201, partial.text
    body = _settlement(api)
    old_row = _player_row(body, old_champion)
    assert old_row["recovered_cents"] == 4_000
    assert old_row["recoverable_amount_cents"] == 6_000

    final_recovery = api.client.post(
        f"/tournaments/{api.tournament_id}/recoveries",
        json={"player_id": old_champion, "amount_cents": 6_000},
        headers={"Idempotency-Key": "final-recovery"},
    )
    assert final_recovery.status_code == 201
    body = _settlement(api)
    assert _player_row(body, old_champion)["recoverable_amount_cents"] == 0
    assert body["available_funds_cents"] == 100_000


def test_restart_keeps_settlement_and_allows_confirmation(
    db_path: Path, client: TestClient, api: Api
):
    play_full_tournament(api)
    _set_prizes(api)
    _add_funds(api, 50_000)
    champion = api.player_id(1)
    version = _settlement(api)["effective_final_result_version_id"]
    payment_id = _issue_payment(api, champion, 2_000, version, "restart-pay").json()["id"]

    restarted = TestClient(create_app(db_path))
    response = restarted.get(f"/tournaments/{api.tournament_id}/settlement")
    assert response.status_code == 200
    assert response.json()["payments"][0]["id"] == payment_id

    confirmed = _confirm(
        restarted,
        payment_id,
        {"status": "success", "bank_receipt_number": "AFTER-RESTART"},
        "restart-confirm",
        tournament_id=api.tournament_id,
    )
    assert confirmed.status_code == 200, confirmed.text
    again = restarted.get(f"/payments/{payment_id}")
    assert again.json()["status"] == "success"
    assert again.json()["bank_receipt_number"] == "AFTER-RESTART"


def test_pre_settlement_database_is_migrated_and_keeps_data(db_path: Path):
    old_schema = SCHEMA.split("CREATE TABLE IF NOT EXISTS prize_settings", 1)[0]
    conn = connect(db_path)
    try:
        conn.executescript(old_schema)
        service.create_tournament(conn, "Legacy Cup", [f"L{i}" for i in range(1, 9)])
        service.start_match(conn, 1, "QF1", "legacy-staff", "legacy-start")
    finally:
        conn.close()

    # Creating the app runs the additive migration/initialization.
    client = TestClient(create_app(db_path))
    bracket = client.get("/tournaments/1/bracket")
    assert bracket.status_code == 200
    assert bracket.json()["tournament"]["name"] == "Legacy Cup"

    settlement_body = client.get("/tournaments/1/settlement")
    assert settlement_body.status_code == 200
    assert settlement_body.json()["settlement_status"] == "awaiting_final"
    assert settlement_body.json()["payments"] == []

    prize = client.post(
        "/tournaments/1/prize-settings",
        json={"champion_amount_cents": 100, "runner_up_amount_cents": 50},
        headers={"Idempotency-Key": "legacy-prize"},
    )
    assert prize.status_code == 201, prize.text
    qf1 = next(
        slot
        for round_ in bracket.json()["rounds"]
        for slot in round_["slots"]
        if slot["slot_id"] == "QF1"
    )
    assert qf1["state"] == "in_progress"


# ---------------------------------------------------------------------------
# SQLite-level concurrency


def _direct_api_setup(db_path: Path) -> int:
    init_db(db_path)
    conn = connect(db_path)
    try:
        service.create_tournament(conn, "Concurrent Cup", [f"C{i}" for i in range(1, 9)])
    finally:
        conn.close()

    client = TestClient(create_app(db_path))
    api = Api(client)
    # The direct service call above created tournament 1.
    api.tournament_id = 1
    play_full_tournament(api)
    client.post(
        "/tournaments/1/prize-settings",
        json={"champion_amount_cents": 1_000, "runner_up_amount_cents": 500},
        headers={"Idempotency-Key": "concurrent-prizes"},
    )
    client.post(
        "/tournaments/1/funds",
        json={"amount_cents": 10_000_000},
        headers={"Idempotency-Key": "concurrent-funds"},
    )
    return api.player_id(1)


def test_concurrent_payments_cannot_exceed_player_award_balance(db_path: Path):
    champion_id = _direct_api_setup(db_path)
    barrier = threading.Barrier(6)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            try:
                settlement.create_payment(
                    conn,
                    1,
                    {"player_id": champion_id, "amount_cents": 300},
                    f"race-pay-{index}",
                )
                result = ("ok", "")
            except service.TournamentError as exc:
                result = ("error", exc.code)
        finally:
            conn.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [o for o in outcomes if o[0] == "ok"]
    errors = [o for o in outcomes if o[0] == "error"]
    assert len(oks) == 3, outcomes
    assert all(code == "payment_exceeds_award_balance" for _, code in errors), outcomes

    conn = connect(db_path)
    try:
        body = settlement.get_settlement(conn, 1)
    finally:
        conn.close()
    row = _player_row(body, champion_id)
    assert row["unconfirmed_payment_cents"] == 900
    assert row["issuable_balance_cents"] == 100


def test_concurrent_recoveries_cannot_exceed_receivable(db_path: Path):
    champion_id = _direct_api_setup(db_path)

    # Pay 1000 and overturn final so champion becomes runner-up with prize 500.
    conn = connect(db_path)
    try:
        body = settlement.get_settlement(conn, 1)
        version = body["effective_final_result_version_id"]
        status, payment_body = settlement.create_payment(
            conn,
            1,
            {"player_id": champion_id, "amount_cents": 1_000, "result_version_id": version},
            "overpay-payment",
        )
        payment_id = payment_body["id"]
        settlement.confirm_payment(
            conn,
            payment_id,
            {"status": "success", "bank_receipt_number": "OVERPAY-R"},
            "overpay-confirm",
        )
        version_row = service._latest_version(
            conn, service._current_match(conn, 1, "F")["id"]
        )
        service.file_appeal(conn, 1, "F", version_row["id"], "dispute", "coach")
        appeal = conn.execute("SELECT id FROM appeals ORDER BY id DESC LIMIT 1").fetchone()["id"]
        service.adjudicate_appeal(
            conn, 1, appeal, "overturned", version_row["id"], "judge", None, "adj-overturn"
        )
    finally:
        conn.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def recover(index: int) -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            try:
                settlement.register_recovery(
                    conn,
                    1,
                    {"player_id": champion_id, "amount_cents": 500},
                    f"race-recovery-{index}",
                )
                code = "ok"
            except service.TournamentError as exc:
                code = exc.code
        finally:
            conn.close()
        with lock:
            outcomes.append(code)

    threads = [threading.Thread(target=recover, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1, outcomes
    assert "recovery_exceeds_recoverable_amount" in outcomes

    conn = connect(db_path)
    try:
        body = settlement.get_settlement(conn, 1)
    finally:
        conn.close()
    row = _player_row(body, champion_id)
    assert row["recovered_cents"] == 500
    assert row["recoverable_amount_cents"] == 0


def test_concurrent_same_receipt_only_one_payment_succeeds(db_path: Path):
    champion_id = _direct_api_setup(db_path)
    runner_id = None
    conn = connect(db_path)
    try:
        players = service._players_by_seed(conn, 1)
        runner_id = players[3]["id"]
        version = settlement.get_settlement(conn, 1)["effective_final_result_version_id"]
        p1 = settlement.create_payment(
            conn, 1, {"player_id": champion_id, "amount_cents": 10}, "receipt-race-p1"
        )[1]["id"]
        p2 = settlement.create_payment(
            conn, 1, {"player_id": runner_id, "amount_cents": 10}, "receipt-race-p2"
        )[1]["id"]
    finally:
        conn.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def confirm(payment_id: int, key: str) -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            try:
                settlement.confirm_payment(
                    conn,
                    payment_id,
                    {"status": "success", "bank_receipt_number": "RACE-RECEIPT"},
                    key,
                )
                code = "ok"
            except service.TournamentError as exc:
                code = exc.code
        finally:
            conn.close()
        with lock:
            outcomes.append(code)

    t1 = threading.Thread(target=confirm, args=(p1, "confirm-race-1"))
    t2 = threading.Thread(target=confirm, args=(p2, "confirm-race-2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert outcomes.count("ok") == 1, outcomes
    assert "bank_receipt_already_used" in outcomes
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id,status,bank_receipt_number FROM payment_orders "
            "WHERE bank_receipt_number = 'RACE-RECEIPT'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "success"


def test_concurrent_appeal_and_payment_do_not_create_mixed_snapshot(db_path: Path):
    champion_id = _direct_api_setup(db_path)
    conn = connect(db_path)
    try:
        version = settlement.get_settlement(conn, 1)["effective_final_result_version_id"]
    finally:
        conn.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def pay() -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            try:
                settlement.create_payment(
                    conn,
                    1,
                    {"player_id": champion_id, "amount_cents": 100, "result_version_id": version},
                    "appeal-race-pay",
                )
                code = "paid"
            except service.TournamentError as exc:
                code = exc.code
        finally:
            conn.close()
        with lock:
            outcomes.append(code)

    def appeal() -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            try:
                service.file_appeal(conn, 1, "F", version, "race dispute", "coach", "appeal-race-key")
                code = "appealed"
            except service.TournamentError as exc:
                code = exc.code
        finally:
            conn.close()
        with lock:
            outcomes.append(code)

    t1 = threading.Thread(target=pay)
    t2 = threading.Thread(target=appeal)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert "appealed" in outcomes
    conn = connect(db_path)
    try:
        body = settlement.get_settlement(conn, 1)
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM appeals WHERE tournament_id=1 AND status='pending'"
        ).fetchone()["c"]
    finally:
        conn.close()

    assert pending == 1
    assert body["settlement_status"] == "suspended"
    if "paid" in outcomes:
        assert len(body["payments"]) == 1
        assert body["payments"][0]["status"] == "pending"
    else:
        assert "settlement_suspended" in outcomes
        assert body["payments"] == []
