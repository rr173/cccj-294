"""Concurrency at the SQLite level, using independent connections.

TestClient serializes requests through one portal, so these tests call the
service functions directly with one connection per thread.  That is exactly
the situation in production (each request gets its own connection).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app import service
from app.db import connect, init_db


PLAYERS = [f"P{i}" for i in range(1, 9)]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "concurrent.db"
    init_db(path)
    conn = connect(path)
    try:
        service.create_tournament(conn, "Cup", PLAYERS)
    finally:
        conn.close()
    return path


def _player_id(db: Path, seed: int) -> int:
    conn = connect(db)
    try:
        return service._players_by_seed(conn, 1)[seed]["id"]
    finally:
        conn.close()


def _worker(db: Path, fn):
    conn = connect(db)
    try:
        return fn(conn)
    finally:
        conn.close()


def test_only_one_reported_result_survives_many_writers(db: Path):
    _worker(
        db,
        lambda conn: service.start_match(
            conn, 1, "QF1", "alice", "start-shared"
        ),
    )
    seeds = [1, 8, 1, 8, 1, 8, 1, 8]
    barrier = threading.Barrier(len(seeds))
    outcomes: list[tuple[str, object]] = []
    lock = threading.Lock()

    def attempt(index: int, seed: int) -> None:
        barrier.wait()
        try:
            result = _worker(
                db,
                lambda conn: service.report_result(
                    conn,
                    1,
                    "QF1",
                    _player_id(db, seed),
                    f"staff-{index}",
                    f"key-{index}",
                ),
            )
            with lock:
                outcomes.append(("ok", result[1]["result_version_id"]))
        except service.TournamentError as exc:
            with lock:
                outcomes.append(("error", exc.code))

    threads = [
        threading.Thread(target=attempt, args=(i, seed))
        for i, seed in enumerate(seeds)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    oks = [o for o in outcomes if o[0] == "ok"]
    assert len(oks) == 1, outcomes
    assert all(o[0] == "error" for o in outcomes if o[0] != "ok")

    version = _worker(
        db,
        lambda conn: service._versions(conn, 1),
    )
    assert len(version) == 1


def test_opposite_concurrent_adjudications_cannot_both_win(db: Path):
    def prepare(conn):
        service.start_match(conn, 1, "QF1", "alice", "s1")
        service.report_result(conn, 1, "QF1", _player_id(db, 1), "alice", "r1")
        service.start_match(conn, 1, "QF2", "alice", "s2")
        service.report_result(conn, 1, "QF2", _player_id(db, 5), "alice", "r2")
        version = service._latest_version(conn, 2)
        appeal_status, body = service.file_appeal(
            conn, 1, "QF2", version["id"], "dispute", "coach"
        )
        return version["id"], body["appeal"]["id"]

    version_id, appeal_id = _worker(db, prepare)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def decide(outcome: str, key: str) -> None:
        barrier.wait()
        try:
            _worker(
                db,
                lambda conn: service.adjudicate_appeal(
                    conn,
                    1,
                    appeal_id,
                    outcome,
                    version_id,
                    "judge",
                    None,
                    key,
                ),
            )
            with lock:
                outcomes.append(("ok", outcome))
        except service.TournamentError as exc:
            with lock:
                outcomes.append(("error", exc.code))

    threads = [
        threading.Thread(target=decide, args=("upheld", "adj-a")),
        threading.Thread(target=decide, args=("overturned", "adj-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    oks = [o for o in outcomes if o[0] == "ok"]
    errors = [o for o in outcomes if o[0] == "error"]
    assert len(oks) == 1, outcomes
    assert len(errors) == 1
    assert errors[0][1] == "appeal_already_decided"

    def inspect(conn):
        return service._versions(conn, 2)

    versions = _worker(db, inspect)
    assert len(versions) == 2
    adjudications = _worker(
        db,
        lambda conn: conn.execute("SELECT * FROM adjudications").fetchall(),
    )
    assert len(adjudications) == 1


def test_two_appeals_same_version_opposite_decisions(db: Path):
    def prepare(conn):
        for slot, seed in (("QF1", 1), ("QF2", 5)):
            service.start_match(conn, 1, slot, "alice", f"s-{slot}")
            service.report_result(
                conn, 1, slot, _player_id(db, seed), "alice", f"r-{slot}"
            )
        version = service._latest_version(conn, 2)
        _, first = service.file_appeal(
            conn, 1, "QF2", version["id"], "first", "coach"
        )
        _, second = service.file_appeal(
            conn, 1, "QF2", version["id"], "second", "coach"
        )
        return version["id"], first["appeal"]["id"], second["appeal"]["id"]

    version_id, appeal_a, appeal_b = _worker(db, prepare)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def decide(appeal_id: int, outcome: str, key: str) -> None:
        barrier.wait()
        try:
            _worker(
                db,
                lambda conn: service.adjudicate_appeal(
                    conn,
                    1,
                    appeal_id,
                    outcome,
                    version_id,
                    "judge",
                    None,
                    key,
                ),
            )
            with lock:
                outcomes.append(("ok", str(appeal_id)))
        except service.TournamentError as exc:
            with lock:
                outcomes.append(("error", exc.code))

    threads = [
        threading.Thread(target=decide, args=(appeal_a, "upheld", "k-a")),
        threading.Thread(target=decide, args=(appeal_b, "overturned", "k-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([o for o in outcomes if o[0] == "ok"]) == 1, outcomes
    stale_or_decided = [o for o in outcomes if o[0] == "error"]
    assert len(stale_or_decided) == 1
    assert stale_or_decided[0][1] in ("appeal_stale", "appeal_already_decided")

    def inspect(conn):
        rows = conn.execute(
            "SELECT status FROM appeals ORDER BY id"
        ).fetchall()
        versions = service._versions(conn, 2)
        return [r["status"] for r in rows], len(versions)

    statuses, version_count = _worker(db, inspect)
    assert statuses.count("pending") == 0
    assert len(statuses) == 2
    assert version_count == 2
