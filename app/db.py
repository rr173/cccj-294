"""SQLite setup and small transaction helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    seed INTEGER NOT NULL CHECK (seed BETWEEN 1 AND 8),
    name TEXT NOT NULL,
    UNIQUE (tournament_id, seed)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    slot_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    participant_a_id INTEGER NOT NULL REFERENCES players(id),
    participant_b_id INTEGER NOT NULL REFERENCES players(id),
    status TEXT NOT NULL CHECK (status IN ('started', 'completed')),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    started_at TEXT NOT NULL,
    started_by TEXT NOT NULL,
    start_idempotency_key TEXT,
    completed_at TEXT,
    completed_by TEXT,
    superseded_at TEXT,
    superseded_reason TEXT,
    superseded_by_adjudication_id INTEGER,
    UNIQUE (tournament_id, slot_id, attempt),
    CHECK (participant_a_id <> participant_b_id)
);

-- A slot can only have one current match. Superseded matches stay in the
-- table (and therefore in history), but no longer occupy the slot.
CREATE UNIQUE INDEX IF NOT EXISTS ux_matches_one_current_per_slot
    ON matches (tournament_id, slot_id)
    WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS result_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    winner_player_id INTEGER NOT NULL REFERENCES players(id),
    loser_player_id INTEGER NOT NULL REFERENCES players(id),
    source TEXT NOT NULL CHECK (source IN ('reported', 'adjudicated')),
    reported_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    adjudication_id INTEGER,
    UNIQUE (match_id, version),
    CHECK (winner_player_id <> loser_player_id)
);

-- Staff may report at most one result per bout.  Subsequent effective
-- versions can only come from an adjudication.
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_reported_result_per_match
    ON result_versions (match_id)
    WHERE source = 'reported';

CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    match_id INTEGER NOT NULL REFERENCES matches(id),
    result_version_id INTEGER NOT NULL REFERENCES result_versions(id),
    description TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'upheld', 'overturned', 'stale')),
    stale_reason TEXT,
    decided_at TEXT,
    decided_by TEXT,
    outcome TEXT CHECK (outcome IN ('upheld', 'overturned')),
    adjudication_id INTEGER
);

CREATE TABLE IF NOT EXISTS adjudications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id INTEGER NOT NULL UNIQUE REFERENCES appeals(id),
    match_id INTEGER NOT NULL REFERENCES matches(id),
    based_on_result_version_id INTEGER NOT NULL REFERENCES result_versions(id),
    new_result_version_id INTEGER NOT NULL REFERENCES result_versions(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('upheld', 'overturned')),
    note TEXT,
    adjudicated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    response_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Keys are globally unique; a key reused elsewhere must never be silently
-- treated as the same operation.
CREATE UNIQUE INDEX IF NOT EXISTS ux_idempotency_scope ON idempotency_keys (key);

CREATE INDEX IF NOT EXISTS ix_matches_tournament ON matches (tournament_id);
CREATE INDEX IF NOT EXISTS ix_appeals_tournament ON appeals (tournament_id);
CREATE INDEX IF NOT EXISTS ix_appeals_version ON appeals (result_version_id);
CREATE INDEX IF NOT EXISTS ix_results_match ON result_versions (match_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection suitable for a short request-scoped unit of work."""
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,  # explicit BEGIN/COMMIT, autocommit otherwise
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 1")
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialize writers.  BEGIN IMMEDIATE avoids read-then-write races."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """Return one consistent snapshot of the database."""
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
