"""Domain rules for the eight-player knockout tournament.

The service deliberately keeps two different ideas apart:

* a *bout* (row in ``matches``) is something that actually happened and is
  never deleted or renamed; and
* a *slot* (QF1 ... F) is a position in the bracket whose current occupant may
  be replaced after an successful appeal.

A match is created only when staff start it.  Its participants are snapshotted
at that moment.  If an appeal later changes an upstream result so that the
snapshot is no longer the path, the match is marked ``is_current = 0`` and the
slot waits for a new match.  The old bout, its result versions and its appeals
remain queryable as history.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal, Sequence

from .clock import now_iso as clock_now_iso
from .clock import utcnow
from .db import read_tx, write_tx

# ---------------------------------------------------------------------------
# Errors


class TournamentError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def now_iso() -> str:
    return clock_now_iso()


# ---------------------------------------------------------------------------
# Fixed bracket


@dataclass(frozen=True)
class SlotSpec:
    slot_id: str
    round_name: str
    round_index: int
    feeders: tuple[str, ...] = ()
    seeds: tuple[int, int] | None = None


SLOTS: tuple[SlotSpec, ...] = (
    SlotSpec("QF1", "quarterfinal", 0, seeds=(1, 8)),
    SlotSpec("QF2", "quarterfinal", 0, seeds=(4, 5)),
    SlotSpec("QF3", "quarterfinal", 0, seeds=(3, 6)),
    SlotSpec("QF4", "quarterfinal", 0, seeds=(2, 7)),
    SlotSpec("SF1", "semifinal", 1, feeders=("QF1", "QF2")),
    SlotSpec("SF2", "semifinal", 1, feeders=("QF3", "QF4")),
    SlotSpec("F", "final", 2, feeders=("SF1", "SF2")),
)
SLOT_BY_ID = {s.slot_id: s for s in SLOTS}
SLOT_ORDER = {s.slot_id: index for index, s in enumerate(SLOTS)}
ROUNDS = (("quarterfinal", 0), ("semifinal", 1), ("final", 2))


def _ancestors(slot_id: str) -> frozenset[str]:
    spec = SLOT_BY_ID[slot_id]
    result: set[str] = set()
    for feeder in spec.feeders:
        result.add(feeder)
        result.update(_ancestors(feeder))
    return frozenset(result)


def _descendants(slot_id: str) -> frozenset[str]:
    direct = {s.slot_id for s in SLOTS if slot_id in s.feeders}
    result: set[str] = set(direct)
    for child in direct:
        result.update(_descendants(child))
    return frozenset(result)


ANCESTORS = {s.slot_id: _ancestors(s.slot_id) for s in SLOTS}
DESCENDANTS = {s.slot_id: _descendants(s.slot_id) for s in SLOTS}


# ---------------------------------------------------------------------------
# Small database helpers


def _require_tournament(conn: sqlite3.Connection, tournament_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    if row is None:
        raise TournamentError(404, "tournament_not_found", "Tournament does not exist")
    return row


def _require_slot(slot_id: str) -> SlotSpec:
    spec = SLOT_BY_ID.get(slot_id)
    if spec is None:
        raise TournamentError(404, "slot_not_found", f"Unknown bracket slot {slot_id!r}")
    return spec


def _current_match(
    conn: sqlite3.Connection, tournament_id: int, slot_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM matches
        WHERE tournament_id = ? AND slot_id = ? AND is_current = 1
        """,
        (tournament_id, slot_id),
    ).fetchone()


def _match_by_id(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()


def _versions(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM result_versions WHERE match_id = ? ORDER BY version",
            (match_id,),
        ).fetchall()
    )


def _latest_version(
    conn: sqlite3.Connection, match_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM result_versions
        WHERE match_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        (match_id,),
    ).fetchone()


def _players(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM players WHERE tournament_id = ? ORDER BY seed",
            (tournament_id,),
        ).fetchall()
    )


def _pending_appeals(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT a.*, m.slot_id AS match_slot_id
            FROM appeals a
            JOIN matches m ON m.id = a.match_id
            WHERE a.tournament_id = ? AND a.status = 'pending'
            ORDER BY a.id
            """,
            (tournament_id,),
        ).fetchall()
    )


def _appeals_for_version(
    conn: sqlite3.Connection, result_version_id: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM appeals WHERE result_version_id = ? ORDER BY id",
            (result_version_id,),
        ).fetchall()
    )


def _player_view(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "seed": row["seed"], "name": row["name"]}


def _players_by_id(conn: sqlite3.Connection, tournament_id: int) -> dict[int, sqlite3.Row]:
    return {p["id"]: p for p in _players(conn, tournament_id)}


def _players_by_seed(conn: sqlite3.Connection, tournament_id: int) -> dict[int, sqlite3.Row]:
    return {p["seed"]: p for p in _players(conn, tournament_id)}


def _player_ref(players: dict[int, sqlite3.Row], player_id: int) -> dict[str, Any]:
    return _player_view(players[player_id])


# ---------------------------------------------------------------------------
# Current bracket derivation


def _feeder_winner(
    conn: sqlite3.Connection, tournament_id: int, feeder_slot: str
) -> int | None:
    """Return a feeder's current effective winner, or None if unavailable.

    Only the *current* match of the feeder is considered.  A superseded match
    is history and must never advance a player again.
    """
    match = _current_match(conn, tournament_id, feeder_slot)
    if match is None or match["status"] != "completed":
        return None
    version = _latest_version(conn, match["id"])
    return version["winner_player_id"] if version is not None else None


def _candidate_participants(
    conn: sqlite3.Connection,
    tournament_id: int,
    spec: SlotSpec,
    by_seed: dict[int, sqlite3.Row],
) -> tuple[int, int] | None:
    if spec.seeds is not None:
        first, second = spec.seeds
        return by_seed[first]["id"], by_seed[second]["id"]
    winners: list[int] = []
    for feeder in spec.feeders:
        winner = _feeder_winner(conn, tournament_id, feeder)
        if winner is None:
            return None
        winners.append(winner)
    # The tree structure guarantees distinct feeder branches; this is a
    # defensive invariant check rather than normal control flow.
    if len(winners) != 2 or winners[0] == winners[1]:
        return None
    return winners[0], winners[1]


def _slot_blockers(
    conn: sqlite3.Connection,
    tournament_id: int,
    slot_id: str,
    pending: Sequence[sqlite3.Row] | None = None,
) -> list[sqlite3.Row]:
    if pending is None:
        pending = _pending_appeals(conn, tournament_id)
    ancestors = ANCESTORS[slot_id]
    return [a for a in pending if a["match_slot_id"] in ancestors]


# ---------------------------------------------------------------------------
# Views


def _appeal_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "match_id": row["match_id"],
        "result_version_id": row["result_version_id"],
        "description": row["description"],
        "submitted_by": row["submitted_by"],
        "created_at": row["created_at"],
        "status": row["status"],
        "outcome": row["outcome"],
        "stale_reason": row["stale_reason"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
        "adjudication_id": row["adjudication_id"],
    }


def _version_view(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    players: dict[int, sqlite3.Row],
) -> dict[str, Any]:
    appeals = [_appeal_view(a) for a in _appeals_for_version(conn, row["id"])]
    return {
        "id": row["id"],
        "version": row["version"],
        "winner": _player_ref(players, row["winner_player_id"]),
        "loser": _player_ref(players, row["loser_player_id"]),
        "source": row["source"],
        "reported_by": row["reported_by"],
        "created_at": row["created_at"],
        "adjudication_id": row["adjudication_id"],
        "appeals": appeals,
        "pending_appeal_ids": [a["id"] for a in appeals if a["status"] == "pending"],
    }


def _assignment_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "candidate_id": row["candidate_id"],
        "tournament_id": row["tournament_id"],
        "slot_id": row["slot_id"],
        "venue": row["venue_name"],
        "venue_name": row["venue_name"],
        "court": row["venue_name"],
        "facility": row["venue_name"],
        "date": row["schedule_date"],
        "start_minute": row["start_minute"],
        "end_minute": row["end_minute"],
        "start_minutes": row["start_minute"],
        "end_minutes": row["end_minute"],
        "start": f"{row['start_minute'] // 60:02d}:{row['start_minute'] % 60:02d}",
        "end": f"{row['end_minute'] // 60:02d}:{row['end_minute'] % 60:02d}",
        "start_time": f"{row['start_minute'] // 60:02d}:{row['start_minute'] % 60:02d}",
        "end_time": f"{row['end_minute'] // 60:02d}:{row['end_minute'] % 60:02d}",
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "start_at": row["starts_at"],
        "end_at": row["ends_at"],
        "revision": row["revision"],
    }


def _venue_usage_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    scheduled = None
    if row["scheduled_starts_at"] is not None:
        scheduled = {
            "venue": row["venue_name"],
            "venue_name": row["venue_name"],
            "date": row["schedule_date"],
            "start_minute": row["start_minute"],
            "end_minute": row["end_minute"],
            "starts_at": row["scheduled_starts_at"],
            "ends_at": row["scheduled_ends_at"],
        }
    return {
        "id": row["id"],
        "match_id": row["match_id"],
        "tournament_id": row["tournament_id"],
        "slot_id": row["slot_id"],
        "assignment_id": row["assignment_id"],
        "venue": row["venue_name"],
        "venue_name": row["venue_name"],
        "court": row["venue_name"],
        "date": row["schedule_date"],
        "start_minute": row["start_minute"],
        "end_minute": row["end_minute"],
        "start_minutes": row["start_minute"],
        "end_minutes": row["end_minute"],
        "starts_at": row["scheduled_starts_at"],
        "ends_at": row["scheduled_ends_at"],
        "start_at": row["scheduled_starts_at"],
        "end_at": row["scheduled_ends_at"],
        "scheduled_slot": scheduled,
        "scheduled": scheduled,
        "actual_started_at": row["actual_started_at"],
        "within_scheduled_window": bool(row["within_scheduled_window"]),
        "schedule_controlled": bool(row["schedule_controlled"]),
    }


def _match_view(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    players: dict[int, sqlite3.Row],
) -> dict[str, Any]:
    versions = _versions(conn, row["id"])
    latest = versions[-1] if versions else None
    usage_row = conn.execute(
        "SELECT * FROM match_venue_usages WHERE match_id = ?", (row["id"],)
    ).fetchone()
    venue_usage = _venue_usage_view(usage_row)
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "attempt": row["attempt"],
        "status": row["status"],
        "is_current": bool(row["is_current"]),
        "participants": {
            "a": _player_ref(players, row["participant_a_id"]),
            "b": _player_ref(players, row["participant_b_id"]),
        },
        "started_at": row["started_at"],
        "started_by": row["started_by"],
        "venue_usage": venue_usage,
        "actual_schedule": venue_usage,
        "venue": venue_usage["venue_name"] if venue_usage else None,
        "venue_name": venue_usage["venue_name"] if venue_usage else None,
        "schedule": venue_usage["scheduled"] if venue_usage else None,
        "completed_at": row["completed_at"],
        "completed_by": row["completed_by"],
        "result": _version_view(conn, latest, players) if latest else None,
        "result_versions": [
            _version_view(conn, v, players) for v in versions
        ],
        "superseded": (
            {
                "at": row["superseded_at"],
                "reason": row["superseded_reason"],
                "by_adjudication_id": row["superseded_by_adjudication_id"],
            }
            if not row["is_current"]
            else None
        ),
    }


def _suspension_view(
    blockers: Iterable[sqlite3.Row],
) -> dict[str, Any]:
    reasons = [
        {
            "appeal_id": a["id"],
            "appealed_slot_id": a["match_slot_id"],
            "match_id": a["match_id"],
            "result_version_id": a["result_version_id"],
            "submitted_by": a["submitted_by"],
            "description": a["description"],
            "created_at": a["created_at"],
        }
        for a in blockers
    ]
    return {
        "suspended": bool(reasons),
        "reasons": reasons,
        "appeal_ids": [r["appeal_id"] for r in reasons],
    }


def _slot_view(
    conn: sqlite3.Connection,
    tournament_id: int,
    spec: SlotSpec,
    players: dict[int, sqlite3.Row],
    by_seed: dict[int, sqlite3.Row],
    pending: Sequence[sqlite3.Row],
) -> dict[str, Any]:
    current = _current_match(conn, tournament_id, spec.slot_id)
    assignment_row = conn.execute(
        """
        SELECT * FROM schedule_assignments
        WHERE tournament_id = ? AND slot_id = ?
        """,
        (tournament_id, spec.slot_id),
    ).fetchone()
    assignment = _assignment_view(assignment_row)
    history_rows = list(
        conn.execute(
            """
            SELECT * FROM matches
            WHERE tournament_id = ? AND slot_id = ? AND is_current = 0
            ORDER BY attempt
            """,
            (tournament_id, spec.slot_id),
        ).fetchall()
    )
    candidates = _candidate_participants(conn, tournament_id, spec, by_seed)
    blockers = _slot_blockers(conn, tournament_id, spec.slot_id, pending)

    feeder_winners = {
        feeder: (
            _player_ref(players, winner)
            if (winner := _feeder_winner(conn, tournament_id, feeder)) is not None
            else None
        )
        for feeder in spec.feeders
    }

    current_winner = None
    if current is not None and current["status"] == "completed":
        latest = _latest_version(conn, current["id"])
        if latest is not None:
            current_winner = _player_ref(players, latest["winner_player_id"])

    if current is not None:
        state = "completed" if current["status"] == "completed" else "in_progress"
        participant_ids = (
            current["participant_a_id"],
            current["participant_b_id"],
        )
    elif blockers:
        # No live bout may begin while an ancestor result is under appeal.
        state = "suspended"
        participant_ids = candidates
    elif candidates is not None:
        state = "ready"
        participant_ids = candidates
    else:
        state = "waiting"
        participant_ids = None

    result_under_appeal = False
    if current is not None and current["status"] == "completed":
        latest = _latest_version(conn, current["id"])
        if latest is not None:
            result_under_appeal = bool(
                conn.execute(
                    """
                    SELECT 1 FROM appeals
                    WHERE result_version_id = ? AND status = 'pending'
                    LIMIT 1
                    """,
                    (latest["id"],),
                ).fetchone()
            )

    history_views = [_match_view(conn, m, players) for m in history_rows]
    bypassed = [
        {
            "match_id": view["id"],
            "attempt": view["attempt"],
            "status": view["status"],
            "participants": view["participants"],
            "result": view["result"],
            "superseded": view["superseded"],
            "note": "This bout actually happened but is no longer on the "
            "effective path and never advances a player again.",
        }
        for view in history_views
    ]

    return {
        "slot_id": spec.slot_id,
        "round": spec.round_name,
        "round_index": spec.round_index,
        "schedule": assignment,
        "venue_schedule": assignment,
        "feeders": list(spec.feeders),
        "fixed_seeds": list(spec.seeds) if spec.seeds else None,
        "feeder_winners": feeder_winners,
        "state": state,
        "playable": state == "ready",
        "participants": (
            {
                "a": _player_ref(players, participant_ids[0]),
                "b": _player_ref(players, participant_ids[1]),
            }
            if participant_ids is not None
            else None
        ),
        # "current_match" means the pair was locked at start; "effective_path"
        # means these are the players a new start would lock right now.
        "participants_source": (
            "current_match"
            if current is not None
            else ("effective_path" if participant_ids is not None else None)
        ),
        "candidate_participants": (
            {
                "a": _player_ref(players, candidates[0]),
                "b": _player_ref(players, candidates[1]),
            }
            if candidates is not None
            else None
        ),
        "current_match": (
            _match_view(conn, current, players) if current is not None else None
        ),
        "current_winner": current_winner,
        "result_under_appeal": result_under_appeal,
        "suspension": _suspension_view(blockers),
        "history": history_views,
        "bypassed_bouts": bypassed,
    }


def _champion_view(
    conn: sqlite3.Connection,
    tournament_id: int,
    players: dict[int, sqlite3.Row],
    pending: Sequence[sqlite3.Row],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    final = _current_match(conn, tournament_id, "F")
    if final is None:
        return None, {
            "state": "pending",
            "reason": "final_not_started",
            "blocking_appeal_ids": [],
        }
    if final["status"] != "completed":
        return None, {
            "state": "pending",
            "reason": "final_in_progress",
            "blocking_appeal_ids": [],
        }
    if pending:
        # Every pending appeal can still change the path to the final.  A
        # pending appeal in the other half is enough to make the title
        # provisional: the final's participants could change.
        latest = _latest_version(conn, final["id"])
        return None, {
            "state": "provisional",
            "reason": "pending_appeal_blocks_champion",
            "blocking_appeal_ids": [a["id"] for a in pending],
            # Shown for operational awareness only, never as the champion.
            "provisional_winner_player_id": (
                latest["winner_player_id"] if latest is not None else None
            ),
        }
    latest = _latest_version(conn, final["id"])
    if latest is None:
        return None, {
            "state": "pending",
            "reason": "final_result_missing",
            "blocking_appeal_ids": [],
        }
    return (
        {
            "player": _player_ref(players, latest["winner_player_id"]),
            "match_id": final["id"],
            "slot_id": "F",
            "result_version_id": latest["id"],
            "result_version": latest["version"],
        },
        {
            "state": "decided",
            "reason": None,
            "blocking_appeal_ids": [],
        },
    )


def get_bracket(conn: sqlite3.Connection, tournament_id: int) -> dict[str, Any]:
    with read_tx(conn):
        _require_tournament(conn, tournament_id)
        players = _players_by_id(conn, tournament_id)
        by_seed = _players_by_seed(conn, tournament_id)
        pending = _pending_appeals(conn, tournament_id)
        champion, champion_status = _champion_view(
            conn, tournament_id, players, pending
        )

        rounds = []
        for round_name, round_index in ROUNDS:
            rounds.append(
                {
                    "round": round_name,
                    "round_index": round_index,
                    "slots": [
                        _slot_view(
                            conn,
                            tournament_id,
                            spec,
                            players,
                            by_seed,
                            pending,
                        )
                        for spec in SLOTS
                        if spec.round_name == round_name
                    ],
                }
            )

        superseded = list(
            conn.execute(
                """
                SELECT * FROM matches
                WHERE tournament_id = ? AND is_current = 0
                ORDER BY id
                """,
                (tournament_id,),
            ).fetchall()
        )

        standings = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM matches WHERE tournament_id = ?) AS bouts,
                (SELECT COUNT(*) FROM matches
                  WHERE tournament_id = ? AND is_current = 0) AS superseded,
                (SELECT COUNT(*) FROM result_versions rv
                   JOIN matches m ON m.id = rv.match_id
                  WHERE m.tournament_id = ?) AS versions,
                (SELECT COUNT(*) FROM appeals
                  WHERE tournament_id = ? AND status = 'pending') AS pending,
                (SELECT COUNT(*) FROM adjudications ad
                   JOIN matches m ON m.id = ad.match_id
                  WHERE m.tournament_id = ?) AS adjudications
            """,
            (tournament_id,) * 5,
        ).fetchone()

        return {
            "tournament": {
                "id": tournament_id,
                "name": conn.execute(
                    "SELECT name FROM tournaments WHERE id = ?", (tournament_id,)
                ).fetchone()["name"],
                "created_at": conn.execute(
                    "SELECT created_at FROM tournaments WHERE id = ?",
                    (tournament_id,),
                ).fetchone()["created_at"],
                "complete": bool(champion is not None),
                "counts": {
                    "bouts_ever_played": int(standings["bouts"]),
                    "superseded_bouts": int(standings["superseded"]),
                    "result_versions": int(standings["versions"]),
                    "pending_appeals": int(standings["pending"]),
                    "adjudications": int(standings["adjudications"]),
                },
            },
            "players": [_player_view(p) for p in players.values()],
            "rounds": rounds,
            "champion": champion,
            "champion_status": champion_status,
            "pending_appeals": [
                {
                    **_appeal_view(a),
                    "slot_id": a["match_slot_id"],
                    "affected_slot_ids": sorted(DESCENDANTS[a["match_slot_id"]]),
                }
                for a in pending
            ],
            "superseded_matches": [
                _match_view(conn, m, players) for m in superseded
            ],
        }


def get_match(
    conn: sqlite3.Connection, tournament_id: int, match_id: int
) -> dict[str, Any]:
    with read_tx(conn):
        _require_tournament(conn, tournament_id)
        row = conn.execute(
            "SELECT * FROM matches WHERE id = ? AND tournament_id = ?",
            (match_id, tournament_id),
        ).fetchone()
        if row is None:
            raise TournamentError(404, "match_not_found", "Match does not exist")
        players = _players_by_id(conn, tournament_id)
        view = _match_view(conn, row, players)
        view["is_on_effective_path"] = bool(row["is_current"])
        return view


def get_history(conn: sqlite3.Connection, tournament_id: int) -> dict[str, Any]:
    with read_tx(conn):
        _require_tournament(conn, tournament_id)
        players = _players_by_id(conn, tournament_id)
        matches = list(
            conn.execute(
                "SELECT * FROM matches WHERE tournament_id = ? ORDER BY id",
                (tournament_id,),
            ).fetchall()
        )
        appeals = list(
            conn.execute(
                "SELECT * FROM appeals WHERE tournament_id = ? ORDER BY id",
                (tournament_id,),
            ).fetchall()
        )
        return {
            "matches": [_match_view(conn, m, players) for m in matches],
            "appeals": [_appeal_view(a) for a in appeals],
        }


def list_appeals(
    conn: sqlite3.Connection, tournament_id: int, status: str | None = None
) -> dict[str, Any]:
    with read_tx(conn):
        _require_tournament(conn, tournament_id)
        sql = """
            SELECT a.*, m.slot_id AS match_slot_id
            FROM appeals a JOIN matches m ON m.id = a.match_id
            WHERE a.tournament_id = ?
        """
        params: list[Any] = [tournament_id]
        if status:
            sql += " AND a.status = ?"
            params.append(status)
        sql += " ORDER BY a.id"
        rows = list(conn.execute(sql, params).fetchall())
        return {
            "appeals": [
                {
                    **_appeal_view(a),
                    "slot_id": a["match_slot_id"],
                    "affected_slot_ids": sorted(DESCENDANTS[a["match_slot_id"]]),
                }
                for a in rows
            ]
        }


# ---------------------------------------------------------------------------
# Idempotency


def _request_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_idempotency(
    conn: sqlite3.Connection, key: str, scope: str, request_hash: str
) -> tuple[int, dict[str, Any]] | None:
    row = conn.execute(
        "SELECT * FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    if row["scope"] != scope or row["request_hash"] != request_hash:
        raise TournamentError(
            409,
            "idempotency_conflict",
            "Idempotency-Key was already used for a different request",
            existing_scope=row["scope"],
        )
    return int(row["status_code"]), json.loads(row["response_json"])


def _store_idempotency(
    conn: sqlite3.Connection,
    key: str,
    scope: str,
    request_hash: str,
    status_code: int,
    response: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO idempotency_keys
            (key, scope, request_hash, status_code, response_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            scope,
            request_hash,
            status_code,
            json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            now_iso(),
        ),
    )


def _require_key(key: str | None) -> str:
    if not key or not key.strip():
        raise TournamentError(
            400,
            "idempotency_key_required",
            "An Idempotency-Key header is required for this operation",
        )
    return key.strip()


# ---------------------------------------------------------------------------
# Tournament creation


def create_tournament(
    conn: sqlite3.Connection, name: str, player_names: Sequence[str]
) -> dict[str, Any]:
    if len(player_names) != 8:
        raise TournamentError(
            422, "players_invalid", "Exactly eight player names are required"
        )
    cleaned = [p.strip() for p in player_names]
    if any(not p for p in cleaned):
        raise TournamentError(422, "players_invalid", "Player names must not be empty")
    if len(set(cleaned)) != 8:
        raise TournamentError(422, "players_invalid", "Player names must be unique")

    created_at = now_iso()
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO tournaments (name, created_at) VALUES (?, ?)",
            (name.strip(), created_at),
        )
        tournament_id = int(cur.lastrowid)
        for seed, player_name in enumerate(cleaned, start=1):
            conn.execute(
                "INSERT INTO players (tournament_id, seed, name) VALUES (?, ?, ?)",
                (tournament_id, seed, player_name),
            )
    return get_bracket(conn, tournament_id)


# ---------------------------------------------------------------------------
# Starting a match


def start_match(
    conn: sqlite3.Connection,
    tournament_id: int,
    slot_id: str,
    started_by: str,
    idempotency_key: str | None,
) -> tuple[int, dict[str, Any]]:
    key = _require_key(idempotency_key)
    spec = _require_slot(slot_id)
    scope = f"start:{tournament_id}:{slot_id}"
    request_hash = _request_hash({"started_by": started_by})

    with write_tx(conn):
        _require_tournament(conn, tournament_id)
        cached = _check_idempotency(conn, key, scope, request_hash)
        if cached is not None:
            return cached

        current = _current_match(conn, tournament_id, slot_id)
        if current is not None:
            if current["status"] == "started":
                raise TournamentError(
                    409,
                    "match_already_in_progress",
                    "A staff member has already started a live match in this slot",
                    match_id=current["id"],
                    started_by=current["started_by"],
                    started_at=current["started_at"],
                    state=current["status"],
                )
            if current["status"] == "completed":
                raise TournamentError(
                    409,
                    "match_already_completed",
                    "This slot already has a completed bout; it cannot be restarted "
                    "without a successful appeal that replaces it",
                    match_id=current["id"],
                    state=current["status"],
                )

        pending = _pending_appeals(conn, tournament_id)
        blockers = _slot_blockers(conn, tournament_id, slot_id, pending)
        if blockers:
            raise TournamentError(
                409,
                "slot_suspended",
                "This slot is suspended by a pending appeal and cannot be started",
                appeal_ids=[a["id"] for a in blockers],
                reasons=_suspension_view(blockers)["reasons"],
            )

        by_seed = _players_by_seed(conn, tournament_id)
        candidates = _candidate_participants(conn, tournament_id, spec, by_seed)
        if candidates is None:
            raise TournamentError(
                409,
                "participants_not_ready",
                "Both participants are not yet determined by current effective feeder results",
                feeders=list(spec.feeders),
                feeder_winners={
                    f: _feeder_winner(conn, tournament_id, f)
                    for f in spec.feeders
                },
            )

        row = conn.execute(
            """
            SELECT COALESCE(MAX(attempt), 0) AS max_attempt
            FROM matches WHERE tournament_id = ? AND slot_id = ?
            """,
            (tournament_id, slot_id),
        ).fetchone()
        attempt = int(row["max_attempt"]) + 1
        timestamp = now_iso()

        # Only the first start of a bracket slot is required to use the
        # confirmed venue slot.  A later attempt is an appeal replay that
        # staff arrange outside this first-booking workflow.
        scheduled_assignment = None
        if attempt == 1:
            scheduled_assignment = conn.execute(
                """
                SELECT * FROM schedule_assignments
                WHERE tournament_id = ? AND slot_id = ?
                """,
                (tournament_id, slot_id),
            ).fetchone()
            if scheduled_assignment is not None:
                window_start = datetime.fromisoformat(
                    scheduled_assignment["starts_at"]
                )
                window_end = datetime.fromisoformat(
                    scheduled_assignment["ends_at"]
                )
                current_time = utcnow()
                if not (window_start <= current_time < window_end):
                    raise TournamentError(
                        409,
                        "outside_scheduled_slot",
                        "The first bout in this reserved slot must start "
                        "during its confirmed [start, end) interval",
                        current_time=current_time.isoformat(),
                        scheduled_slot={
                            "venue": scheduled_assignment["venue_name"],
                            "venue_name": scheduled_assignment["venue_name"],
                            "date": scheduled_assignment["schedule_date"],
                            "start_minute": scheduled_assignment["start_minute"],
                            "end_minute": scheduled_assignment["end_minute"],
                            "starts_at": scheduled_assignment["starts_at"],
                            "ends_at": scheduled_assignment["ends_at"],
                        },
                    )

        cur = conn.execute(
            """
            INSERT INTO matches
                (tournament_id, slot_id, attempt,
                 participant_a_id, participant_b_id,
                 status, is_current, started_at, started_by,
                 start_idempotency_key)
            VALUES (?, ?, ?, ?, ?, 'started', 1, ?, ?, ?)
            """,
            (
                tournament_id,
                slot_id,
                attempt,
                candidates[0],
                candidates[1],
                timestamp,
                started_by,
                key,
            ),
        )
        match_id = int(cur.lastrowid)
        if scheduled_assignment is not None:
            conn.execute(
                """
                INSERT INTO match_venue_usages
                    (match_id, tournament_id, slot_id, assignment_id,
                     venue_name, schedule_date, start_minute, end_minute,
                     scheduled_starts_at, scheduled_ends_at,
                     actual_started_at, within_scheduled_window,
                     schedule_controlled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
                """,
                (
                    match_id,
                    tournament_id,
                    slot_id,
                    scheduled_assignment["id"],
                    scheduled_assignment["venue_name"],
                    scheduled_assignment["schedule_date"],
                    scheduled_assignment["start_minute"],
                    scheduled_assignment["end_minute"],
                    scheduled_assignment["starts_at"],
                    scheduled_assignment["ends_at"],
                    timestamp,
                    timestamp,
                ),
            )
        match = _match_by_id(conn, match_id)
        players = _players_by_id(conn, tournament_id)
        assert match is not None
        response = {
            "match": _match_view(conn, match, players),
            "slot_state": "in_progress",
        }
        _store_idempotency(conn, key, scope, request_hash, 201, response)
        return 201, response


# ---------------------------------------------------------------------------
# Reporting a result


def report_result(
    conn: sqlite3.Connection,
    tournament_id: int,
    slot_id: str,
    winner_player_id: int,
    reported_by: str,
    idempotency_key: str | None,
) -> tuple[int, dict[str, Any]]:
    key = _require_key(idempotency_key)
    _require_slot(slot_id)
    scope = f"result:{tournament_id}:{slot_id}"
    request_hash = _request_hash(
        {"winner_player_id": winner_player_id, "reported_by": reported_by}
    )

    with write_tx(conn):
        _require_tournament(conn, tournament_id)
        cached = _check_idempotency(conn, key, scope, request_hash)
        if cached is not None:
            return cached

        match = _current_match(conn, tournament_id, slot_id)
        if match is None:
            raise TournamentError(
                409,
                "match_not_started",
                "There is no current match in this slot to report a result for",
            )
        if match["status"] == "completed":
            raise TournamentError(
                409,
                "match_already_completed",
                "This match already has an effective result; a second result is not allowed",
                match_id=match["id"],
            )
        if match["status"] != "started":
            raise TournamentError(
                409, "match_not_started", "Only a started match can be completed"
            )

        participants = {match["participant_a_id"], match["participant_b_id"]}
        if winner_player_id not in participants:
            raise TournamentError(
                422,
                "invalid_winner",
                "The winner must be one of the two participants locked at match start",
                participant_ids=sorted(participants),
            )

        loser_id = (
            match["participant_b_id"]
            if winner_player_id == match["participant_a_id"]
            else match["participant_a_id"]
        )
        timestamp = now_iso()
        try:
            cur = conn.execute(
                """
                INSERT INTO result_versions
                    (match_id, version, winner_player_id, loser_player_id,
                     source, reported_by, created_at, adjudication_id)
                VALUES (?, 1, ?, ?, 'reported', ?, ?, NULL)
                """,
                (match["id"], winner_player_id, loser_id, reported_by, timestamp),
            )
        except sqlite3.IntegrityError:
            # Defensive: the serialized transaction and status check above
            # already prevent this.  Never let two effective results exist.
            raise TournamentError(
                409,
                "match_already_completed",
                "This match already has an effective result",
                match_id=match["id"],
            )
        version_id = int(cur.lastrowid)
        conn.execute(
            """
            UPDATE matches
            SET status = 'completed', completed_at = ?, completed_by = ?
            WHERE id = ?
            """,
            (timestamp, reported_by, match["id"]),
        )
        match = _match_by_id(conn, match["id"])
        players = _players_by_id(conn, tournament_id)
        assert match is not None
        response = {
            "match": _match_view(conn, match, players),
            "result_version_id": version_id,
        }
        _store_idempotency(conn, key, scope, request_hash, 200, response)
        return 200, response


# ---------------------------------------------------------------------------
# Appeals


def file_appeal(
    conn: sqlite3.Connection,
    tournament_id: int,
    slot_id: str,
    result_version_id: int,
    description: str,
    submitted_by: str,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    _require_slot(slot_id)
    key = (idempotency_key or "").strip() or None
    scope = f"appeal:{tournament_id}:{slot_id}"
    request_hash = _request_hash(
        {
            "result_version_id": result_version_id,
            "description": description,
            "submitted_by": submitted_by,
        }
    )

    with write_tx(conn):
        _require_tournament(conn, tournament_id)
        if key:
            cached = _check_idempotency(conn, key, scope, request_hash)
            if cached is not None:
                return cached

        match = _current_match(conn, tournament_id, slot_id)
        if match is None:
            raise TournamentError(
                409,
                "no_current_match",
                "There is no current match in this slot to appeal",
            )
        if match["status"] != "completed":
            raise TournamentError(
                409,
                "no_result_to_appeal",
                "Only a completed current match can be appealed",
                match_id=match["id"],
                state=match["status"],
            )
        latest = _latest_version(conn, match["id"])
        if latest is None or latest["id"] != result_version_id:
            raise TournamentError(
                409,
                "result_version_changed",
                "The appeal must target the current effective result version",
                current_result_version_id=latest["id"] if latest else None,
            )

        timestamp = now_iso()
        cur = conn.execute(
            """
            INSERT INTO appeals
                (tournament_id, match_id, result_version_id, description,
                 submitted_by, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                tournament_id,
                match["id"],
                result_version_id,
                description,
                submitted_by,
                timestamp,
            ),
        )
        appeal_id = int(cur.lastrowid)
        appeal = conn.execute(
            "SELECT * FROM appeals WHERE id = ?", (appeal_id,)
        ).fetchone()
        assert appeal is not None
        response = {
            "appeal": _appeal_view(appeal),
            "slot_id": slot_id,
            "affected_slot_ids": sorted(DESCENDANTS[slot_id]),
            "slot_now_suspended": True,
        }
        if key:
            _store_idempotency(conn, key, scope, request_hash, 201, response)
        return 201, response


def _supersede_descendants(
    conn: sqlite3.Connection,
    tournament_id: int,
    slot_id: str,
    adjudication_id: int,
    timestamp: str,
) -> list[int]:
    """Retire affected downstream bouts whose pair is no longer on the path.

    A completed bout is a historical fact and is never renamed or deleted.
    A started-but-unfinished bout is likewise kept as replaced history.  Both
    are marked ``is_current = 0`` and the slot waits for a fresh match.  A
    descendant whose snapshotted pair still equals the pair produced by the
    new effective path is left alone, so the untouched half of the bracket
    keeps its state.
    """
    superseded_ids: list[int] = []
    by_seed = _players_by_seed(conn, tournament_id)
    ordered = sorted(
        DESCENDANTS[slot_id], key=lambda sid: (SLOT_BY_ID[sid].round_index, SLOT_ORDER[sid])
    )
    for descendant in ordered:
        match = _current_match(conn, tournament_id, descendant)
        if match is None:
            continue
        candidates = _candidate_participants(
            conn, tournament_id, SLOT_BY_ID[descendant], by_seed
        )
        if candidates is not None and candidates == (
            match["participant_a_id"],
            match["participant_b_id"],
        ):
            # The actual pair is still exactly what the current path gives.
            continue
        conn.execute(
            """
            UPDATE matches
            SET is_current = 0,
                superseded_at = ?,
                superseded_reason = 'upstream_result_overturned',
                superseded_by_adjudication_id = ?
            WHERE id = ?
            """,
            (timestamp, adjudication_id, match["id"]),
        )
        superseded_ids.append(match["id"])
    return superseded_ids


def adjudicate_appeal(
    conn: sqlite3.Connection,
    tournament_id: int,
    appeal_id: int,
    outcome: Literal["upheld", "overturned"],
    expected_result_version_id: int,
    adjudicated_by: str,
    note: str | None,
    idempotency_key: str | None,
) -> tuple[int, dict[str, Any]]:
    key = _require_key(idempotency_key)
    scope = f"adjudicate:{tournament_id}:{appeal_id}"
    request_hash = _request_hash(
        {
            "outcome": outcome,
            "expected_result_version_id": expected_result_version_id,
            "adjudicated_by": adjudicated_by,
            "note": note,
        }
    )

    with write_tx(conn):
        _require_tournament(conn, tournament_id)
        cached = _check_idempotency(conn, key, scope, request_hash)
        if cached is not None:
            return cached

        appeal = conn.execute(
            "SELECT * FROM appeals WHERE id = ? AND tournament_id = ?",
            (appeal_id, tournament_id),
        ).fetchone()
        if appeal is None:
            raise TournamentError(404, "appeal_not_found", "Appeal does not exist")

        if appeal["status"] != "pending":
            if appeal["status"] in ("upheld", "overturned"):
                existing = conn.execute(
                    "SELECT * FROM adjudications WHERE appeal_id = ?", (appeal_id,)
                ).fetchone()
                if (
                    existing is not None
                    and existing["outcome"] == outcome
                    and existing["based_on_result_version_id"]
                    == expected_result_version_id
                ):
                    # Repeating the same adjudication is a replay, not a new
                    # decision.  Return the exact original response.
                    return 200, json.loads(existing["response_json"])
                raise TournamentError(
                    409,
                    "appeal_already_decided",
                    "This appeal has already been decided; a later decision cannot overwrite it",
                    status=appeal["status"],
                    outcome=appeal["outcome"],
                    adjudication_id=appeal["adjudication_id"],
                )
            raise TournamentError(
                409,
                "appeal_stale",
                "This appeal is stale because its result version is no longer on the effective path",
                status=appeal["status"],
                stale_reason=appeal["stale_reason"],
            )

        # The client may only decide the version it actually saw.  Checking
        # inside BEGIN IMMEDIATE makes concurrent opposite decisions serial:
        # the loser sees the changed version and must not overwrite it.
        if (
            appeal["result_version_id"] != expected_result_version_id
            or appeal["status"] != "pending"
        ):
            raise TournamentError(
                409,
                "result_version_changed",
                "The adjudication does not target the appeal's effective result version",
                current_result_version_id=appeal["result_version_id"],
            )

        match = _match_by_id(conn, appeal["match_id"])
        if match is None or not match["is_current"]:
            raise TournamentError(
                409,
                "result_version_changed",
                "The appealed match is no longer the current match in its slot",
            )
        latest = _latest_version(conn, match["id"])
        if (
            latest is None
            or latest["id"] != expected_result_version_id
            or latest["id"] != appeal["result_version_id"]
            or match["status"] != "completed"
        ):
            raise TournamentError(
                409,
                "result_version_changed",
                "The current effective result version differs from the one the adjudicator saw",
                current_result_version_id=latest["id"] if latest else None,
            )

        timestamp = now_iso()
        old_winner = int(latest["winner_player_id"])
        old_loser = int(latest["loser_player_id"])
        new_winner = old_loser if outcome == "overturned" else old_winner
        new_loser = old_winner if outcome == "overturned" else old_loser

        cur = conn.execute(
            """
            INSERT INTO result_versions
                (match_id, version, winner_player_id, loser_player_id,
                 source, reported_by, created_at, adjudication_id)
            VALUES (?, ?, ?, ?, 'adjudicated', ?, ?, NULL)
            """,
            (
                match["id"],
                latest["version"] + 1,
                new_winner,
                new_loser,
                adjudicated_by,
                timestamp,
            ),
        )
        new_version_id = int(cur.lastrowid)

        cur = conn.execute(
            """
            INSERT INTO adjudications
                (appeal_id, match_id, based_on_result_version_id,
                 new_result_version_id, outcome, note, adjudicated_by,
                 created_at, response_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}')
            """,
            (
                appeal_id,
                match["id"],
                latest["id"],
                new_version_id,
                outcome,
                note,
                adjudicated_by,
                timestamp,
            ),
        )
        adjudication_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE result_versions SET adjudication_id = ? WHERE id = ?",
            (adjudication_id, new_version_id),
        )

        appeal_status = "upheld" if outcome == "upheld" else "overturned"
        conn.execute(
            """
            UPDATE appeals
            SET status = ?, outcome = ?, decided_at = ?, decided_by = ?,
                adjudication_id = ?
            WHERE id = ?
            """,
            (
                appeal_status,
                outcome,
                timestamp,
                adjudicated_by,
                adjudication_id,
                appeal_id,
            ),
        )

        # Any other undecided appeal on the *same old version* is obsolete:
        # that version has just been replaced by this adjudication.
        stale_rows = list(
            conn.execute(
                """
                SELECT id FROM appeals
                WHERE result_version_id = ? AND status = 'pending' AND id <> ?
                """,
                (latest["id"], appeal_id),
            ).fetchall()
        )
        conn.execute(
            """
            UPDATE appeals
            SET status = 'stale',
                stale_reason = 'another_appeal_adjudicated',
                decided_at = ?, adjudication_id = ?
            WHERE result_version_id = ? AND status = 'pending' AND id <> ?
            """,
            (timestamp, adjudication_id, latest["id"], appeal_id),
        )

        superseded_ids: list[int] = []
        cascade_stale_ids: list[int] = []
        if outcome == "overturned":
            # A started/completed downstream bout was a real event.  Never
            # rename or delete it; retire it and let the slot restart with
            # the new participants once they are known.
            before = {
                r["id"]
                for r in conn.execute(
                    """
                    SELECT a.id FROM appeals a
                    JOIN matches m ON m.id = a.match_id
                    WHERE a.status = 'pending' AND m.tournament_id = ?
                    """,
                    (tournament_id,),
                ).fetchall()
            }
            superseded_ids = _supersede_descendants(
                conn, tournament_id, match["slot_id"], adjudication_id, timestamp
            )
            # A pending appeal whose match just left the effective path is
            # obsolete: it must neither freeze anything nor be decidable.
            conn.execute(
                """
                UPDATE appeals
                SET status = 'stale',
                    stale_reason = 'result_superseded_by_upstream_adjudication',
                    decided_at = ?, adjudication_id = ?
                WHERE id IN (
                    SELECT a.id FROM appeals a
                    JOIN matches m ON m.id = a.match_id
                    WHERE a.status = 'pending' AND m.is_current = 0
                      AND m.tournament_id = ?
                )
                """,
                (timestamp, adjudication_id, tournament_id),
            )
            after = {
                r["id"]
                for r in conn.execute(
                    """
                    SELECT a.id FROM appeals a
                    JOIN matches m ON m.id = a.match_id
                    WHERE a.status = 'pending' AND m.tournament_id = ?
                    """,
                    (tournament_id,),
                ).fetchall()
            }
            cascade_stale_ids = sorted(before - after)

        players = _players_by_id(conn, tournament_id)
        match = _match_by_id(conn, match["id"])
        assert match is not None
        response = {
            "adjudication": {
                "id": adjudication_id,
                "appeal_id": appeal_id,
                "outcome": outcome,
                "based_on_result_version_id": latest["id"],
                "new_result_version_id": new_version_id,
                "note": note,
                "adjudicated_by": adjudicated_by,
                "created_at": timestamp,
                "other_appeals_marked_stale": [
                    r["id"] for r in stale_rows
                ],
                "superseded_match_ids": superseded_ids,
                "cascade_stale_appeal_ids": cascade_stale_ids,
            },
            "match": _match_view(conn, match, players),
        }
        conn.execute(
            "UPDATE adjudications SET response_json = ? WHERE id = ?",
            (
                json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                adjudication_id,
            ),
        )
        _store_idempotency(conn, key, scope, request_hash, 200, response)
        return 200, response
