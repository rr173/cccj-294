"""Venue reservation and closure-aware rescheduling.

The solver is intentionally dependency free.  The supported problem envelope
is small (at most four tournaments sharing a UTC day and six candidates per
slot in the expected usage range), so an exact lexically ordered DFS is enough
to guarantee both the minimum number of moved slots and the specified
tie-break.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import service
from .clock import utcnow
from .db import read_tx, write_tx

UTC = timezone.utc
SLOT_ORDER = service.SLOT_ORDER
SLOTS = service.SLOTS
SLOT_IDS = tuple(s.slot_id for s in SLOTS)
FEEDERS = {s.slot_id: tuple(s.feeders) for s in SLOTS}
DAILY_TOURNAMENT_LIMIT = 4
MAX_CANDIDATES_PER_SLOT = 6


# ---------------------------------------------------------------------------
# Errors and primitive parsing


def _error(status: int, code: str, message: str, **extra: Any) -> service.TournamentError:
    return service.TournamentError(status, code, message, **extra)


def _first(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _clean_name(value: Any, what: str = "venue") -> str:
    if isinstance(value, dict):
        value = _first(value, "name", "venue_name", "label", "id")
    if value is None:
        raise _error(422, f"{what}_required", f"A non-empty {what} is required")
    text = str(value).strip()
    if not text:
        raise _error(422, f"{what}_required", f"A non-empty {what} is required")
    return text


def parse_date(value: Any) -> str:
    if value is None:
        raise _error(422, "date_required", "A UTC date (YYYY-MM-DD) is required")
    if isinstance(value, datetime):
        return value.astimezone(UTC).date().isoformat()
    if isinstance(value, date_cls):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        raise _error(422, "date_required", "A UTC date (YYYY-MM-DD) is required")
    try:
        if "T" in text or " " in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).date().isoformat()
        return date_cls.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise _error(
            422,
            "invalid_date",
            "Date must be a valid UTC calendar date (YYYY-MM-DD)",
            value=text,
        ) from exc


def _parse_minute_value(value: Any) -> tuple[int | None, str | None]:
    """Return minute-of-day and an optional UTC date inferred from ISO input."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        raise _error(422, "invalid_minute", "Minutes must be an integer or HH:MM")
    if isinstance(value, (int, float)):
        minute = int(value)
        if isinstance(value, float) and not float(value).is_integer():
            raise _error(422, "invalid_minute", "Minute values must be whole minutes")
        return minute, None
    text = str(value).strip()
    if not text:
        return None, None
    if text.isdigit():
        return int(text), None
    if "T" in text or "-" in text and ":" in text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            dt = dt.astimezone(UTC)
            return dt.hour * 60 + dt.minute, dt.date().isoformat()
    if ":" in text:
        parts = text.split(":")
        if len(parts) not in (2, 3):
            raise _error(422, "invalid_minute", "Time must be HH:MM")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError as exc:
            raise _error(422, "invalid_minute", "Time must be HH:MM") from exc
        return hour * 60 + minute, None
    raise _error(422, "invalid_minute", f"Unrecognized minute/time value: {text!r}")


@dataclass(frozen=True)
class Interval:
    venue: str
    day: str
    start_minute: int
    end_minute: int
    starts_at: datetime
    ends_at: datetime


def normalize_interval(raw: dict[str, Any], *, require_venue: bool = True) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _error(422, "invalid_schedule_interval", "Schedule interval must be an object")

    venue = None
    if require_venue:
        venue = _clean_name(
            _first(raw, "venue_name", "venueName", "venue", "court", "facility", "venue_id", "venueId"),
            "venue",
        )

    explicit_day = _first(raw, "date", "day", "schedule_date", "scheduleDate", "utc_date", "utcDate")
    day = parse_date(explicit_day) if explicit_day is not None else None

    start_raw = _first(
        raw,
        "start_minute",
        "startMinute",
        "start_minutes",
        "startMinutes",
        "start_time",
        "startTime",
        "starts_at",
        "startsAt",
        "start_at",
        "startAt",
        "start",
        "from",
    )
    end_raw = _first(
        raw,
        "end_minute",
        "endMinute",
        "end_minutes",
        "endMinutes",
        "end_time",
        "endTime",
        "ends_at",
        "endsAt",
        "end_at",
        "endAt",
        "end",
        "to",
    )
    start_minute, start_day = _parse_minute_value(start_raw)
    end_minute, end_day = _parse_minute_value(end_raw)

    if day is None:
        day = start_day or end_day
    if day is None:
        raise _error(422, "date_required", "A UTC date or full UTC timestamp is required")
    if start_day is not None and explicit_day is not None and start_day != day:
        raise _error(422, "date_timezone_mismatch", "Start timestamp is not on the stated UTC date")
    if end_day is not None and end_day != day:
        next_day = (date_cls.fromisoformat(day) + timedelta(days=1)).isoformat()
        if end_minute == 0 and end_day == next_day:
            # 24:00 is represented as the next date's midnight in ISO form.
            end_minute = 1440
        else:
            raise _error(
                422,
                "interval_crosses_utc_date",
                "Each candidate must stay on one UTC date",
            )

    if start_minute is None:
        raise _error(422, "start_required", "A start minute/time is required")
    if end_minute is None:
        duration = _first(raw, "duration_minutes", "durationMinutes", "duration", "length_minutes")
        if duration is not None:
            try:
                end_minute = start_minute + int(duration)
            except (TypeError, ValueError) as exc:
                raise _error(422, "invalid_duration", "duration must be in whole minutes") from exc
    if end_minute is None:
        raise _error(422, "end_required", "An end minute/time or duration is required")

    if isinstance(start_raw, bool) or not (0 <= start_minute < 1440):
        raise _error(
            422,
            "invalid_start_minute",
            "start_minute must satisfy 0 <= start_minute < 1440",
            start_minute=start_minute,
        )
    if not (start_minute < end_minute <= 1440):
        raise _error(
            422,
            "invalid_end_minute",
            "end_minute must satisfy start_minute < end_minute <= 1440",
            start_minute=start_minute,
            end_minute=end_minute,
        )

    day_date = date_cls.fromisoformat(day)
    starts_at = datetime(day_date.year, day_date.month, day_date.day, tzinfo=UTC) + timedelta(
        minutes=start_minute
    )
    ends_at = datetime(day_date.year, day_date.month, day_date.day, tzinfo=UTC) + timedelta(
        minutes=end_minute
    )
    return {
        "venue": venue,
        "date": day,
        "start_minute": start_minute,
        "end_minute": end_minute,
        "starts_at": starts_at,
        "ends_at": ends_at,
    }


def intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


# ---------------------------------------------------------------------------
# Request extraction


def _unwrap_body(body: dict[str, Any]) -> dict[str, Any]:
    for key in ("reservation", "booking", "schedule_request", "data"):
        inner = body.get(key)
        if isinstance(inner, dict):
            merged = {**inner}
            for k, v in body.items():
                if k not in merged:
                    merged[k] = v
            return merged
    return body


def extract_candidate_groups(body: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(body, dict):
        raise _error(422, "invalid_request", "JSON request body must be an object")
    body = _unwrap_body(body)

    raw: Any = None
    for key in ("candidates", "slots", "options", "schedule", "items", "choices"):
        if key in body:
            raw = body[key]
            break
    if raw is None and any(slot in body for slot in SLOT_IDS):
        raw = body
    if raw is None:
        raise _error(
            422,
            "candidates_required",
            "Provide candidates for all seven slots",
            slots=list(SLOT_IDS),
        )

    groups: dict[str, list[dict[str, Any]]] = {slot: [] for slot in SLOT_IDS}

    def add(slot: Any, candidate: Any) -> None:
        if slot is None:
            raise _error(422, "slot_id_required", "Each candidate group needs a slot_id")
        slot_text = str(slot)
        if slot_text not in service.SLOT_BY_ID:
            raise _error(404, "slot_not_found", f"Unknown bracket slot {slot_text!r}")
        if not isinstance(candidate, dict):
            raise _error(422, "invalid_candidate", "Each candidate must be an object")
        groups[slot_text].append(candidate)

    if isinstance(raw, dict):
        for key, value in raw.items():
            slot = key
            if isinstance(value, dict) and _first(value, "slot_id", "slot", "position", "id"):
                slot = _first(value, "slot_id", "slot", "position", "id")
            if isinstance(value, dict) and isinstance(
                _first(value, "candidates", "options", "choices"), list
            ):
                opts = _first(value, "candidates", "options", "choices")
                group_slot = (
                    _first(value, "slot_id", "slot", "position", "id") or slot
                )
                for opt in opts:
                    add(group_slot, opt)
            elif isinstance(value, list):
                for opt in value:
                    add(slot, opt)
            else:
                add(slot, value)
    elif isinstance(raw, list):
        flat_without_slot = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise _error(422, "invalid_candidate_group", "Candidate groups must be objects")
            nested = _first(item, "candidates", "options", "choices")
            slot = _first(item, "slot_id", "slot", "position", "bracket_slot")
            if isinstance(nested, list):
                for opt in nested:
                    add(slot, opt)
            elif slot is not None:
                add(slot, item)
            else:
                flat_without_slot.append(item)
        if flat_without_slot:
            if len(raw) == 7 and len(flat_without_slot) == 7:
                for slot, item in zip(SLOT_IDS, flat_without_slot):
                    add(slot, item)
            else:
                raise _error(
                    422,
                    "slot_id_required",
                    "Flat candidate lists must include slot_id on every item",
                )
    else:
        raise _error(422, "invalid_candidates", "candidates must be a list or object")

    missing = [slot for slot in SLOT_IDS if not groups[slot]]
    if missing:
        raise _error(
            422,
            "all_seven_slots_required",
            "At least one candidate is required for every bracket slot",
            missing_slots=missing,
        )
    too_many = {slot: len(opts) for slot, opts in groups.items() if len(opts) > MAX_CANDIDATES_PER_SLOT}
    if too_many:
        raise _error(
            422,
            "too_many_candidates",
            f"At most {MAX_CANDIDATES_PER_SLOT} candidates are allowed per slot",
            counts=too_many,
            limit=MAX_CANDIDATES_PER_SLOT,
        )

    normalized: dict[str, list[dict[str, Any]]] = {}
    for slot in SLOT_IDS:
        normalized[slot] = [normalize_interval(item) for item in groups[slot]]
    return normalized


def extract_closure(body: dict[str, Any], venue_hint: str | None = None) -> dict[str, Any]:
    body = _unwrap_body(body if isinstance(body, dict) else {})
    nested = body.get("closure") if isinstance(body.get("closure"), dict) else {}
    merged = {**nested, **body} if nested else body
    venue = _first(merged, "venue_name", "venueName", "venue", "court", "facility", "venue_id", "venueId")
    if venue is None:
        venue = venue_hint
    interval_body = dict(merged)
    if venue is not None:
        interval_body["venue"] = venue
    interval = normalize_interval(interval_body, require_venue=True)
    interval["reason"] = _first(merged, "reason", "note", "description")
    interval["requested_by"] = _first(
        merged, "requested_by", "created_by", "closed_by", "submitted_by", "staff"
    )
    return interval


# ---------------------------------------------------------------------------
# Views


def interval_view(row: sqlite3.Row | dict[str, Any], *, venue_key: str = "venue_name") -> dict[str, Any]:
    def get(key: str) -> Any:
        if isinstance(row, dict):
            return row.get(key)
        return row[key]

    venue = get(venue_key)
    if venue is None and isinstance(row, dict):
        venue = row.get("venue")
    start_minute = int(get("start_minute"))
    end_minute = int(get("end_minute"))
    day = get("schedule_date") if get("schedule_date") is not None else get("date")
    return {
        "venue": venue,
        "venue_name": venue,
        "court": venue,
        "facility": venue,
        "date": day,
        "start_minute": start_minute,
        "end_minute": end_minute,
        "start_minutes": start_minute,
        "end_minutes": end_minute,
        "start": f"{start_minute // 60:02d}:{start_minute % 60:02d}",
        "end": f"{end_minute // 60:02d}:{end_minute % 60:02d}",
        "start_time": f"{start_minute // 60:02d}:{start_minute % 60:02d}",
        "end_time": f"{end_minute // 60:02d}:{end_minute % 60:02d}",
        "starts_at": get("starts_at"),
        "ends_at": get("ends_at"),
        "start_at": get("starts_at"),
        "end_at": get("ends_at"),
        "utc_start": get("starts_at"),
        "utc_end": get("ends_at"),
    }


def assignment_view(row: sqlite3.Row) -> dict[str, Any]:
    base = interval_view(row)
    base.update(
        {
            "candidate_id": row["candidate_id"],
            "assignment_id": row["id"],
            "tournament_id": row["tournament_id"],
            "slot_id": row["slot_id"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
        }
    )
    return base


def closure_view(row: sqlite3.Row) -> dict[str, Any]:
    base = interval_view(row)
    base.update(
        {
            "id": row["id"],
            "reason": row["reason"],
            "requested_by": row["requested_by"],
            "created_at": row["created_at"],
        }
    )
    return base


# ---------------------------------------------------------------------------
# Solver data structures


@dataclass(frozen=True)
class Choice:
    tournament_id: int
    slot_id: str
    candidate_id: int
    option_index: int
    venue: str
    day: str
    start_minute: int
    end_minute: int
    starts_at: datetime
    ends_at: datetime
    cost: int

    @property
    def lex_key(self) -> tuple[datetime, datetime, str, int]:
        return (self.starts_at, self.ends_at, self.venue, self.option_index)

    @property
    def schedule_tuple(self) -> tuple[str, int, int, str]:
        return (self.day, self.start_minute, self.end_minute, self.venue)


@dataclass
class Variable:
    tournament_id: int
    slot_id: str
    choices: list[Choice]
    locked: bool
    lock_reason: str | None = None

    @property
    def key(self) -> tuple[int, str]:
        return (self.tournament_id, self.slot_id)

    @property
    def order(self) -> tuple[int, int]:
        return (self.tournament_id, SLOT_ORDER[self.slot_id])


def _choice_from_candidate_row(
    row: sqlite3.Row, old_tuple: tuple[str, int, int, str] | None
) -> Choice:
    starts = datetime.fromisoformat(row["starts_at"])
    ends = datetime.fromisoformat(row["ends_at"])
    schedule_tuple = (
        row["schedule_date"],
        int(row["start_minute"]),
        int(row["end_minute"]),
        row["venue_name"],
    )
    return Choice(
        tournament_id=int(row["tournament_id"]),
        slot_id=row["slot_id"],
        candidate_id=int(row["id"]),
        option_index=int(row["option_index"]),
        venue=row["venue_name"],
        day=row["schedule_date"],
        start_minute=int(row["start_minute"]),
        end_minute=int(row["end_minute"]),
        starts_at=starts,
        ends_at=ends,
        cost=0 if old_tuple == schedule_tuple else 1,
    )


def _closure_intervals(conn: sqlite3.Connection) -> list[Interval]:
    rows = conn.execute("SELECT * FROM venue_closures").fetchall()
    return [
        Interval(
            venue=r["venue_name"],
            day=r["schedule_date"],
            start_minute=r["start_minute"],
            end_minute=r["end_minute"],
            starts_at=datetime.fromisoformat(r["starts_at"]),
            ends_at=datetime.fromisoformat(r["ends_at"]),
        )
        for r in rows
    ]


def _choice_hard_ok(
    choice: Choice,
    assigned: dict[tuple[int, str], Choice],
    closures: Iterable[Interval],
    day_tournaments: dict[str, set[int]],
    *,
    daily_limit: int | None,
) -> bool:
    for closure in closures:
        if closure.venue != choice.venue:
            continue
        if intervals_overlap(
            choice.starts_at, choice.ends_at, closure.starts_at, closure.ends_at
        ):
            return False

    for other in assigned.values():
        if other.venue != choice.venue:
            continue
        if intervals_overlap(choice.starts_at, choice.ends_at, other.starts_at, other.ends_at):
            return False

    # Direct feeder rounds must end no later than this slot starts.
    for feeder in FEEDERS[choice.slot_id]:
        feeder_choice = assigned.get((choice.tournament_id, feeder))
        if feeder_choice is not None and choice.starts_at < feeder_choice.ends_at:
            return False

    if daily_limit is not None:
        owners = day_tournaments.get(choice.day, set())
        if choice.tournament_id not in owners and len(owners) >= daily_limit:
            return False
    return True


def _choices_compatible(
    var_a: Variable,
    choice_a: Choice,
    var_b: Variable,
    choice_b: Choice,
) -> bool:
    if (
        choice_a.venue == choice_b.venue
        and intervals_overlap(
            choice_a.starts_at,
            choice_a.ends_at,
            choice_b.starts_at,
            choice_b.ends_at,
        )
    ):
        return False

    if choice_a.tournament_id == choice_b.tournament_id:
        # var_b is a direct feeder of var_a: var_a may not start first.
        if var_b.slot_id in FEEDERS[var_a.slot_id]:
            if choice_a.starts_at < choice_b.ends_at:
                return False
        # var_a is a direct feeder of var_b.
        if var_a.slot_id in FEEDERS[var_b.slot_id]:
            if choice_b.starts_at < choice_a.ends_at:
                return False
    return True


def _arc_consistency(
    variables: list[Variable], domains: dict[tuple[int, str], list[Choice]]
) -> bool:
    """Remove values with no support in another variable's current domain."""
    changed = True
    while changed:
        changed = False
        for var in variables:
            current = domains[var.key]
            supported: list[Choice] = []
            for choice in current:
                has_support = True
                for other in variables:
                    if other.key == var.key:
                        continue
                    if not any(
                        _choices_compatible(var, choice, other, other_choice)
                        for other_choice in domains[other.key]
                    ):
                        has_support = False
                        break
                if has_support:
                    supported.append(choice)
            if not supported:
                return False
            if len(supported) < len(current):
                domains[var.key] = supported
                changed = True
    return True


def _remaining_min_cost(
    variables: list[Variable],
    assigned: dict[tuple[int, str], Choice],
    domains: dict[tuple[int, str], list[Choice]],
) -> int:
    return sum(
        min(choice.cost for choice in domains[var.key])
        for var in variables
        if var.key not in assigned
    )


def _find_feasible(
    variables: list[Variable],
    budget: int,
    initial_assigned: dict[tuple[int, str], Choice] | None = None,
    *,
    daily_limit: int | None = DAILY_TOURNAMENT_LIMIT,
    failed_states: set[frozenset[tuple[tuple[int, str], int]]] | None = None,
) -> dict[tuple[int, str], Choice] | None:
    domains: dict[tuple[int, str], list[Choice]] = {
        var.key: list(var.choices) for var in variables
    }
    assigned: dict[tuple[int, str], Choice] = dict(initial_assigned or {})
    for key, choice in assigned.items():
        domains[key] = [choice]

    if not _arc_consistency(variables, domains):
        return None

    spent = sum(choice.cost for choice in assigned.values())
    if spent + _remaining_min_cost(variables, assigned, domains) > budget:
        return None

    initial_days: dict[str, set[int]] = {}
    for choice in assigned.values():
        initial_days.setdefault(choice.day, set()).add(choice.tournament_id)

    failed = failed_states if failed_states is not None else set()

    def recurse(
        spent: int,
        assigned: dict[tuple[int, str], Choice],
        domains: dict[tuple[int, str], list[Choice]],
        days: dict[str, set[int]],
    ) -> dict[tuple[int, str], Choice] | None:
        state_key = frozenset(
            (key, choice.candidate_id) for key, choice in assigned.items()
        )
        if state_key in failed:
            return None
        if spent + _remaining_min_cost(variables, assigned, domains) > budget:
            failed.add(state_key)
            return None
        unassigned = [var for var in variables if var.key not in assigned]
        if not unassigned:
            return dict(assigned)

        def degree(var: Variable) -> int:
            children = sum(var.slot_id in feeders for feeders in FEEDERS.values())
            return len(FEEDERS[var.slot_id]) + children

        # MRV is a feasibility-search heuristic only.  The public lexical
        # guarantee is constructed separately after the minimum cost is known.
        var = min(
            unassigned,
            key=lambda item: (
                len(domains[item.key]),
                -degree(item),
                item.order,
            ),
        )
        values = sorted(
            domains[var.key],
            key=lambda choice: (choice.cost, choice.lex_key),
        )
        for choice in values:
            next_spent = spent + choice.cost
            next_days = {day: set(owners) for day, owners in days.items()}
            owners = next_days.setdefault(choice.day, set())
            if (
                daily_limit is not None
                and choice.tournament_id not in owners
                and len(owners) >= daily_limit
            ):
                continue

            next_domains = {key: list(values_) for key, values_ in domains.items()}
            next_domains[var.key] = [choice]
            next_assigned = dict(assigned)
            next_assigned[var.key] = choice
            owners.add(choice.tournament_id)

            if not _arc_consistency(variables, next_domains):
                continue
            if (
                next_spent
                + _remaining_min_cost(variables, next_assigned, next_domains)
                > budget
            ):
                continue

            result = recurse(next_spent, next_assigned, next_domains, next_days)
            if result is not None:
                return result
        failed.add(state_key)
        return None

    return recurse(spent, assigned, domains, initial_days)


def _prepare_variables(
    variables: list[Variable], closures: list[Interval]
) -> list[Variable]:
    variables = sorted(variables, key=lambda var: var.order)
    for var in variables:
        if not var.choices:
            raise _error(
                409,
                "no_feasible_arrangement",
                "A slot has no candidate that is selectable at this operation time",
                tournament_id=var.tournament_id,
                slot_id=var.slot_id,
            )
        var.choices.sort(
            key=lambda choice: (
                choice.starts_at,
                choice.ends_at,
                choice.venue,
                # Equal lexical tuples prefer the incumbent/lower option.
                choice.cost,
                choice.option_index,
                choice.candidate_id,
            )
        )

    # Locked choices produce a precise 409 before generic CSP failure.
    for var in variables:
        if var.locked and len(var.choices) == 1:
            choice = var.choices[0]
            for closure in closures:
                if closure.venue == choice.venue and intervals_overlap(
                    choice.starts_at,
                    choice.ends_at,
                    closure.starts_at,
                    closure.ends_at,
                ):
                    raise _error(
                        409,
                        "closure_conflicts_with_locked_slot",
                        (
                            "A closure overlaps a slot pinned by an unsettled "
                            "offline permit"
                            if var.lock_reason == "offline_permit"
                            else "A closure overlaps a slot that has started or reached its start time"
                        ),
                        tournament_id=choice.tournament_id,
                        slot_id=choice.slot_id,
                        lock_reason=var.lock_reason,
                        pinned_by_offline_permit=var.lock_reason == "offline_permit",
                        closure={
                            "venue": closure.venue,
                            "date": closure.day,
                            "start_minute": closure.start_minute,
                            "end_minute": closure.end_minute,
                        },
                        schedule={
                            "venue": choice.venue,
                            "date": choice.day,
                            "start_minute": choice.start_minute,
                            "end_minute": choice.end_minute,
                        },
                    )

    # Closures are unary constraints for every variable domain.
    for var in variables:
        var.choices = [
            choice
            for choice in var.choices
            if not any(
                closure.venue == choice.venue
                and intervals_overlap(
                    choice.starts_at,
                    choice.ends_at,
                    closure.starts_at,
                    closure.ends_at,
                )
                for closure in closures
            )
        ]
    return variables


class BitmaskCsp:
    """Small binary CSP with AC-3 propagation and MRV search.

    Domains contain at most six candidates, so a domain is a six-bit mask.
    The expensive lexical guarantee is constructed by the caller; this class
    only answers whether a cost budget is feasible.
    """

    def __init__(
        self,
        variables: list[Variable],
        closures: list[Interval],
        daily_limit: int | None,
    ) -> None:
        self.variables = _prepare_variables(variables, closures)
        self.n = len(self.variables)
        self.daily_limit = daily_limit
        self.full_mask = [(1 << len(var.choices)) - 1 for var in self.variables]
        self.support: list[list[list[int] | None]] = [
            [None] * self.n for _ in range(self.n)
        ]
        for i, var_i in enumerate(self.variables):
            for j, var_j in enumerate(self.variables):
                if i == j:
                    continue
                supports: list[int] = []
                for choice_i in var_i.choices:
                    mask = 0
                    for index_j, choice_j in enumerate(var_j.choices):
                        if _choices_compatible(var_i, choice_i, var_j, choice_j):
                            mask |= 1 << index_j
                    supports.append(mask)
                self.support[i][j] = supports

    def _ac3(self, masks: list[int], changed_index: int | None = None) -> bool:
        from collections import deque

        if changed_index is None:
            queue = deque(
                (i, j)
                for i in range(self.n)
                for j in range(self.n)
                if i != j
            )
        else:
            queue = deque(
                (k, changed_index) for k in range(self.n) if k != changed_index
            )
        while queue:
            i, j = queue.popleft()
            supports = self.support[i][j]
            assert supports is not None
            old = masks[i]
            revised = 0
            bits = old
            while bits:
                bit = bits & -bits
                index = bit.bit_length() - 1
                if supports[index] & masks[j]:
                    revised |= bit
                bits -= bit
            if revised == 0:
                return False
            if revised != old:
                masks[i] = revised
                for k in range(self.n):
                    if k != i:
                        queue.append((k, i))
        return True

    def _min_cost(self, index: int, mask: int) -> int:
        best = self.n + 1
        choices = self.variables[index].choices
        bits = mask
        while bits:
            bit = bits & -bits
            choice_index = bit.bit_length() - 1
            best = min(best, choices[choice_index].cost)
            bits -= bit
        return best

    def feasible(
        self,
        budget: int,
        initial: dict[tuple[int, str], Choice] | None = None,
        failed: set[tuple[tuple[int, ...], frozenset[tuple[int, int]]]] | None = None,
    ) -> dict[tuple[int, str], Choice] | None:
        if any(not var.choices for var in self.variables):
            return None
        masks = list(self.full_mask)
        assigned: dict[int, int] = {}
        spent = 0
        days: dict[str, set[int]] = {}
        index_by_key = {var.key: i for i, var in enumerate(self.variables)}

        for key, choice in (initial or {}).items():
            i = index_by_key[key]
            candidate_index = self.variables[i].choices.index(choice)
            masks[i] = 1 << candidate_index
            assigned[i] = candidate_index
            spent += choice.cost
            days.setdefault(choice.day, set()).add(choice.tournament_id)

        if not self._ac3(masks):
            return None

        failed_states = failed if failed is not None else set()

        def lower_bound(
            current_spent: int,
            current_masks: list[int],
            chosen: dict[int, int],
        ) -> int:
            total = current_spent
            for i, mask in enumerate(current_masks):
                if i not in chosen and mask:
                    total += self._min_cost(i, mask)
            return total

        def degree(i: int) -> int:
            slot = self.variables[i].slot_id
            children = sum(slot in feeders for feeders in FEEDERS.values())
            return len(FEEDERS[slot]) + children

        def recurse(
            current_spent: int,
            current_masks: list[int],
            chosen: dict[int, int],
            schedule_days: dict[str, set[int]],
        ) -> dict[int, int] | None:
            state = (tuple(current_masks), frozenset(chosen.items()))
            if state in failed_states:
                return None
            if lower_bound(current_spent, current_masks, chosen) > budget:
                failed_states.add(state)
                return None
            if len(chosen) == self.n:
                return dict(chosen)

            unassigned = [i for i in range(self.n) if i not in chosen]
            i = min(
                unassigned,
                key=lambda item: (
                    current_masks[item].bit_count(),
                    -degree(item),
                    item,
                ),
            )
            # Choices are already lexical; try low-cost values first for a
            # quick feasible answer while retaining deterministic ordering.
            bit_indices = [
                idx
                for idx in range(len(self.variables[i].choices))
                if current_masks[i] & (1 << idx)
            ]
            bit_indices.sort(
                key=lambda idx: (self.variables[i].choices[idx].cost, idx)
            )
            for candidate_index in bit_indices:
                choice = self.variables[i].choices[candidate_index]
                next_spent = current_spent + choice.cost
                if next_spent + sum(
                    self._min_cost(k, current_masks[k])
                    for k in unassigned
                    if k != i
                ) > budget:
                    continue

                next_days = {day: set(owners) for day, owners in schedule_days.items()}
                owners = next_days.setdefault(choice.day, set())
                if (
                    self.daily_limit is not None
                    and choice.tournament_id not in owners
                    and len(owners) >= self.daily_limit
                ):
                    continue

                next_masks = current_masks.copy()
                next_masks[i] = 1 << candidate_index
                if not self._ac3(next_masks, changed_index=i):
                    continue

                next_chosen = dict(chosen)
                next_chosen[i] = candidate_index
                owners.add(choice.tournament_id)
                result = recurse(
                    next_spent, next_masks, next_chosen, next_days
                )
                if result is not None:
                    return result
            failed_states.add(state)
            return None

        indices = recurse(spent, masks, assigned, days)
        if indices is None:
            return None
        return {
            self.variables[i].key: self.variables[i].choices[candidate_index]
            for i, candidate_index in indices.items()
        }

    def minimum_lex(self) -> tuple[dict[tuple[int, str], Choice], int]:
        maximum = self.n
        if self.feasible(maximum) is None:
            raise _error(
                409,
                "no_feasible_arrangement",
                "No complete arrangement satisfies all scheduling constraints",
            )

        low = sum(min(choice.cost for choice in var.choices) for var in self.variables)
        high = maximum
        while low < high:
            middle = (low + high) // 2
            if self.feasible(middle) is not None:
                high = middle
            else:
                low = middle + 1
        best = low

        assigned: dict[tuple[int, str], Choice] = {}
        cache: set[tuple[tuple[int, ...], frozenset[tuple[int, int]]]] = set()
        for var in self.variables:
            committed = False
            for choice in var.choices:
                trial = dict(assigned)
                trial[var.key] = choice
                if self.feasible(best, initial=trial, failed=cache) is not None:
                    assigned = trial
                    committed = True
                    break
            if not committed:
                raise _error(
                    409,
                    "no_feasible_arrangement",
                    "No lexical completion exists for the minimum-cost schedule",
                )
        return assigned, best


def solve_minimum_lex(
    variables: list[Variable],
    closures: list[Interval],
    *,
    daily_limit: int | None = DAILY_TOURNAMENT_LIMIT,
) -> tuple[dict[tuple[int, str], Choice] | None, int | None]:
    solver = BitmaskCsp(variables, closures, daily_limit)
    try:
        return solver.minimum_lex()
    except service.TournamentError as exc:
        if exc.code == "closure_conflicts_with_locked_slot":
            raise
        if exc.code == "no_feasible_arrangement":
            return None, None
        raise


def _legacy_solve_minimum_lex(
    variables: list[Variable],
    closures: list[Interval],
    *,
    daily_limit: int | None = DAILY_TOURNAMENT_LIMIT,
) -> tuple[dict[tuple[int, str], Choice] | None, int | None]:
    variables = _prepare_variables(variables, closures)
    if any(not var.choices for var in variables):
        return None, None

    maximum_budget = len(variables)
    if _find_feasible(variables, maximum_budget, daily_limit=daily_limit) is None:
        return None, None

    low = sum(min(choice.cost for choice in var.choices) for var in variables)
    high = maximum_budget
    while low < high:
        middle = (low + high) // 2
        if _find_feasible(variables, middle, daily_limit=daily_limit) is not None:
            high = middle
        else:
            low = middle + 1
    best_budget = low

    # Build the globally lexicographically smallest prefix that still admits a
    # minimum-cost completion.  This is the required tie-break.
    assigned: dict[tuple[int, str], Choice] = {}
    lexical_failed: set[frozenset[tuple[tuple[int, str], int]]] = set()
    for var in variables:
        committed = False
        for choice in var.choices:
            trial = dict(assigned)
            trial[var.key] = choice
            completion = _find_feasible(
                variables,
                best_budget,
                initial_assigned=trial,
                daily_limit=daily_limit,
                failed_states=lexical_failed,
            )
            if completion is not None:
                assigned = trial
                committed = True
                break
        if not committed:
            return None, None
    return assigned, best_budget


def _solve_ordered_variables(
    variables: list[Variable],
    closures: list[Interval],
    budget: int,
    *,
    daily_limit: int | None = DAILY_TOURNAMENT_LIMIT,
) -> dict[tuple[int, str], Choice] | None:
    """Lexicographic solution with cost at most ``budget``.

    Retained for callers that do not need the binary-search optimizer.
    """
    variables = _prepare_variables(variables, closures)
    if any(not var.choices for var in variables):
        return None
    return _find_feasible(
        variables,
        budget,
        daily_limit=daily_limit,
    )


def solve_initial_reservation(
    conn: sqlite3.Connection, tournament_id: int, now: datetime
) -> dict[tuple[int, str], Choice]:
    candidate_rows = conn.execute(
        """
        SELECT * FROM schedule_candidates
        WHERE tournament_id = ?
        ORDER BY slot_id, option_index
        """,
        (tournament_id,),
    ).fetchall()
    by_slot: dict[str, list[sqlite3.Row]] = {slot: [] for slot in SLOT_IDS}
    for row in candidate_rows:
        by_slot[row["slot_id"]].append(row)

    variables: list[Variable] = []
    for slot in SLOT_IDS:
        choices = [
            _choice_from_candidate_row(r, None)
            for r in by_slot[slot]
            if datetime.fromisoformat(r["starts_at"]) >= now
        ]
        # New choices are all untried; cost zero makes the first lexical
        # feasible solution the answer.
        for c in choices:
            object.__setattr__(c, "cost", 0)
        if not choices:
            raise _error(
                422,
                "schedule_start_in_past",
                "Every slot needs at least one candidate starting at or after server time",
                slot_id=slot,
                server_time=now.isoformat(),
            )
        variables.append(Variable(tournament_id, slot, choices, locked=False))

    closures = _closure_intervals(conn)

    # Other tournaments' confirmed assignments are immutable fixed occupancy
    # for a first reservation.
    fixed_rows = conn.execute(
        "SELECT * FROM schedule_assignments WHERE tournament_id <> ?",
        (tournament_id,),
    ).fetchall()
    fixed_variables: list[Variable] = []
    for row in fixed_rows:
        candidate_row = conn.execute(
            "SELECT * FROM schedule_candidates WHERE id = ?", (row["candidate_id"],)
        ).fetchone()
        if candidate_row is None:
            continue
        choice = _choice_from_candidate_row(candidate_row, None)
        object.__setattr__(choice, "cost", 0)
        fixed_variables.append(
            Variable(int(row["tournament_id"]), row["slot_id"], [choice], locked=True)
        )

    # Put fixed assignments into the base state by representing them as leading
    # locked variables.  They sort by ids naturally; the new tournament may
    # interleave, so use one solver call with all variables.
    all_variables = fixed_variables + variables
    solution, _minimum_cost = solve_minimum_lex(
        all_variables,
        closures,
        daily_limit=DAILY_TOURNAMENT_LIMIT,
    )
    if solution is None:
        raise _error(
            409,
            "no_feasible_reservation",
            "No complete seven-slot arrangement satisfies closures, venue overlaps, "
            "bracket order, time and daily tournament limits",
        )
    return {key: choice for key, choice in solution.items() if key[0] == tournament_id}


def build_repair_variables(
    conn: sqlite3.Connection, now: datetime
) -> list[Variable]:
    reservation_rows = conn.execute("SELECT tournament_id FROM schedule_reservations").fetchall()
    variables: list[Variable] = []
    # Every unsettled offline permit pins its exact claimed venue/interval.
    # The closure solver therefore cannot repair a closure by moving that slot;
    # if the new closure overlaps the pinned interval, solving fails and the
    # closure transaction rolls back.
    permitted_rows = conn.execute(
        """
        SELECT DISTINCT tournament_id, slot_id
        FROM offline_permits
        WHERE status = 'active'
        """
    ).fetchall()
    pinned_slots = {(int(r["tournament_id"]), r["slot_id"]) for r in permitted_rows}
    for reservation in reservation_rows:
        tid = int(reservation["tournament_id"])
        used_slots = {
            r["slot_id"]
            for r in conn.execute(
                "SELECT DISTINCT slot_id FROM matches WHERE tournament_id = ?", (tid,)
            ).fetchall()
        }
        assignment_rows = conn.execute(
            "SELECT * FROM schedule_assignments WHERE tournament_id = ?", (tid,)
        ).fetchall()
        assignments = {r["slot_id"]: r for r in assignment_rows}
        candidate_rows = conn.execute(
            "SELECT * FROM schedule_candidates WHERE tournament_id = ? ORDER BY option_index",
            (tid,),
        ).fetchall()
        candidates_by_slot: dict[str, list[sqlite3.Row]] = {s: [] for s in SLOT_IDS}
        for row in candidate_rows:
            candidates_by_slot[row["slot_id"]].append(row)

        for slot in SLOT_IDS:
            old_row = assignments.get(slot)
            if old_row is None:
                # Should be impossible because confirmation writes all seven.
                raise _error(500, "incomplete_reservation", "A confirmed reservation is missing a slot")
            old_tuple = (
                old_row["schedule_date"],
                int(old_row["start_minute"]),
                int(old_row["end_minute"]),
                old_row["venue_name"],
            )
            old_start = datetime.fromisoformat(old_row["starts_at"])
            pinned_by_permit = (tid, slot) in pinned_slots
            time_locked = old_start <= now
            locked = slot in used_slots or time_locked or pinned_by_permit
            if pinned_by_permit:
                lock_reason = "offline_permit"
            elif slot in used_slots:
                lock_reason = "match_started"
            elif time_locked:
                lock_reason = "start_time_reached"
            else:
                lock_reason = None
            old_candidate = conn.execute(
                "SELECT * FROM schedule_candidates WHERE id = ?",
                (old_row["candidate_id"],),
            ).fetchone()
            if locked:
                if old_candidate is None:
                    raise _error(500, "candidate_missing", "Selected candidate no longer exists")
                choice = _choice_from_candidate_row(old_candidate, old_tuple)
                variables.append(
                    Variable(tid, slot, [choice], locked=True, lock_reason=lock_reason)
                )
                continue

            choices: list[Choice] = []
            for candidate in candidates_by_slot[slot]:
                starts = datetime.fromisoformat(candidate["starts_at"])
                if starts < now:
                    continue
                choices.append(_choice_from_candidate_row(candidate, old_tuple))
            if not choices and old_candidate is not None:
                # Defensive: an unlocked old row is necessarily future, but
                # include it rather than making state unusable.
                choices.append(_choice_from_candidate_row(old_candidate, old_tuple))
            variables.append(Variable(tid, slot, choices, locked=False))
    return variables


def solve_closure_repair(
    conn: sqlite3.Connection, now: datetime
) -> tuple[dict[tuple[int, str], Choice], int]:
    closures = _closure_intervals(conn)
    variables = build_repair_variables(conn, now)
    if not variables:
        return {}, 0

    # The optimizer binary-searches the minimum changed-slot count and then
    # constructs the globally lexicographically smallest minimum-cost answer.
    try:
        solution, _minimum_cost = solve_minimum_lex(
            variables,
            closures,
            daily_limit=DAILY_TOURNAMENT_LIMIT,
        )
    except service.TournamentError:
        raise
    if solution is None:
        raise _error(
            409,
            "no_feasible_reschedule",
            "Closure rejected: no complete arrangement can satisfy all brackets and venue conflicts",
        )

    changed = 0
    for var in variables:
        choice = solution[var.key]
        old = conn.execute(
            """
            SELECT schedule_date, start_minute, end_minute, venue_name
            FROM schedule_assignments
            WHERE tournament_id = ? AND slot_id = ?
            """,
            var.key,
        ).fetchone()
        old_tuple = (
            old["schedule_date"],
            int(old["start_minute"]),
            int(old["end_minute"]),
            old["venue_name"],
        )
        if old_tuple != choice.schedule_tuple:
            changed += 1
    return solution, changed


# ---------------------------------------------------------------------------
# Persistence / HTTP service operations


def _idempotent_replay(
    conn: sqlite3.Connection,
    key: str | None,
    scope: str,
    request_payload: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    key = service._require_key(key)
    request_hash = service._request_hash(request_payload)
    return service._check_idempotency(conn, key, scope, request_hash)


def reserve_tournament(
    conn: sqlite3.Connection,
    tournament_id: int,
    body: dict[str, Any],
    idempotency_key: str | None,
) -> tuple[int, dict[str, Any]]:
    raw_key = idempotency_key or body.get("idempotency_key")
    key = service._require_key(raw_key)
    groups = extract_candidate_groups(body)
    scope = f"schedule-reservation:{tournament_id}"
    request_payload = {"body": body, "kind": "reserve"}
    request_hash = service._request_hash(request_payload)

    with write_tx(conn):
        service._require_tournament(conn, tournament_id)
        cached = service._check_idempotency(conn, key, scope, request_hash)
        if cached is not None:
            return cached

        existing = conn.execute(
            "SELECT * FROM schedule_reservations WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()
        if existing is not None:
            current = build_tournament_schedule_view(conn, tournament_id)
            raise _error(
                409,
                "reservation_already_confirmed",
                "This tournament already has its one confirmed venue reservation",
                reservation=current,
            )

        started = conn.execute(
            "SELECT 1 FROM matches WHERE tournament_id = ? LIMIT 1", (tournament_id,)
        ).fetchone()
        if started is not None:
            raise _error(
                409,
                "tournament_already_started",
                "Candidates can only be submitted before the tournament's first bout",
            )

        timestamp = service.now_iso()
        conn.execute(
            """
            INSERT INTO schedule_reservations
                (tournament_id, status, created_at, updated_at)
            VALUES (?, 'confirmed', ?, ?)
            """,
            (tournament_id, timestamp, timestamp),
        )
        for slot in SLOT_IDS:
            for index, candidate in enumerate(groups[slot]):
                conn.execute(
                    """
                    INSERT INTO schedule_candidates
                        (tournament_id, slot_id, option_index, venue_name,
                         schedule_date, start_minute, end_minute,
                         starts_at, ends_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tournament_id,
                        slot,
                        index,
                        candidate["venue"],
                        candidate["date"],
                        candidate["start_minute"],
                        candidate["end_minute"],
                        candidate["starts_at"].isoformat(),
                        candidate["ends_at"].isoformat(),
                        timestamp,
                    ),
                )

        now = utcnow()
        solution = solve_initial_reservation(conn, tournament_id, now)

        # Reject candidates whose start is in the past.  The solver did not
        # need now for ordering, so enforce this explicitly on chosen values.
        for (tid, slot), choice in solution.items():
            if choice.starts_at < now:
                raise _error(
                    422,
                    "schedule_start_in_past",
                    "A newly selected schedule cannot start before server time",
                    slot_id=slot,
                    server_time=now.isoformat(),
                    starts_at=choice.starts_at.isoformat(),
                )

        for slot in SLOT_IDS:
            choice = solution[(tournament_id, slot)]
            conn.execute(
                """
                INSERT INTO schedule_assignments
                    (tournament_id, slot_id, candidate_id, venue_name,
                     schedule_date, start_minute, end_minute, starts_at, ends_at,
                     revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    tournament_id,
                    slot,
                    choice.candidate_id,
                    choice.venue,
                    choice.day,
                    choice.start_minute,
                    choice.end_minute,
                    choice.starts_at.isoformat(),
                    choice.ends_at.isoformat(),
                    timestamp,
                    timestamp,
                ),
            )

        schedule = build_tournament_schedule_view(conn, tournament_id)
        confirmation = {
            "tournament_id": tournament_id,
            "id": tournament_id,
            "status": "confirmed",
            "confirmed": True,
            "created_at": timestamp,
            "confirmed_at": timestamp,
            "candidate_count": sum(len(v) for v in groups.values()),
            "slots": schedule["slots"],
            "schedule": schedule["slots"],
        }
        response = {
            "reservation": confirmation,
            "booking": confirmation,
            "confirmation": confirmation,
            "tournament_id": tournament_id,
            "status": "confirmed",
            "schedule": schedule["slots"],
            "slots": schedule["slots"],
            "assignments": schedule["slots"],
            "reservation_view": schedule,
        }
        if key:
            service._store_idempotency(conn, key, scope, request_hash, 201, response)
        return 201, response


def register_closure(
    conn: sqlite3.Connection,
    body: dict[str, Any],
    idempotency_key: str | None,
    venue_hint: str | None = None,
) -> tuple[int, dict[str, Any]]:
    raw_key = idempotency_key or body.get("idempotency_key")
    key = service._require_key(raw_key)
    interval = extract_closure(body, venue_hint)
    scope = "venue-closure"
    request_payload = {"body": body, "venue_hint": venue_hint, "kind": "closure"}
    request_hash = service._request_hash(request_payload)

    with write_tx(conn):
        cached = service._check_idempotency(conn, key, scope, request_hash)
        if cached is not None:
            return cached

        now = utcnow()
        if interval["starts_at"] <= now:
            raise _error(
                409,
                "closure_already_started",
                "Only closure periods that have not started yet can be registered",
                server_time=now.isoformat(),
                closure_start=interval["starts_at"].isoformat(),
            )

        timestamp = service.now_iso()
        cur = conn.execute(
            """
            INSERT INTO venue_closures
                (venue_name, schedule_date, start_minute, end_minute,
                 starts_at, ends_at, reason, requested_by,
                 idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interval["venue"],
                interval["date"],
                interval["start_minute"],
                interval["end_minute"],
                interval["starts_at"].isoformat(),
                interval["ends_at"].isoformat(),
                interval.get("reason"),
                interval.get("requested_by"),
                key,
                timestamp,
            ),
        )
        closure_id = int(cur.lastrowid)

        solution, changed_count = solve_closure_repair(conn, now)

        changes: list[dict[str, Any]] = []
        changed_keys: list[dict[str, Any]] = []
        for (tid, slot), choice in sorted(
            solution.items(),
            key=lambda item: (
                item[1].tournament_id,
                SLOT_ORDER[item[1].slot_id],
            ),
        ):
            old = conn.execute(
                "SELECT * FROM schedule_assignments WHERE tournament_id=? AND slot_id=?",
                (tid, slot),
            ).fetchone()
            if old is None:
                continue
            old_tuple = (
                old["schedule_date"],
                int(old["start_minute"]),
                int(old["end_minute"]),
                old["venue_name"],
            )
            new_tuple = choice.schedule_tuple
            change_record = {
                "tournament_id": tid,
                "slot_id": slot,
                "changed": old_tuple != new_tuple,
                "from": interval_view(old),
                "to": {
                    "candidate_id": choice.candidate_id,
                    "tournament_id": tid,
                    "slot_id": slot,
                    "venue": choice.venue,
                    "venue_name": choice.venue,
                    "date": choice.day,
                    "start_minute": choice.start_minute,
                    "end_minute": choice.end_minute,
                    "start": f"{choice.start_minute // 60:02d}:{choice.start_minute % 60:02d}",
                    "end": f"{choice.end_minute // 60:02d}:{choice.end_minute % 60:02d}",
                    "starts_at": choice.starts_at.isoformat(),
                    "ends_at": choice.ends_at.isoformat(),
                },
            }
            changes.append(change_record)
            if change_record["changed"]:
                changed_keys.append({"tournament_id": tid, "slot_id": slot})
                conn.execute(
                    """
                    UPDATE schedule_assignments
                    SET candidate_id=?, venue_name=?, schedule_date=?,
                        start_minute=?, end_minute=?, starts_at=?, ends_at=?,
                        revision=revision+1, updated_at=?
                    WHERE tournament_id=? AND slot_id=?
                    """,
                    (
                        choice.candidate_id,
                        choice.venue,
                        choice.day,
                        choice.start_minute,
                        choice.end_minute,
                        choice.starts_at.isoformat(),
                        choice.ends_at.isoformat(),
                        timestamp,
                        tid,
                        slot,
                    ),
                )

        closure_row = conn.execute("SELECT * FROM venue_closures WHERE id=?", (closure_id,)).fetchone()
        day_schedule = build_day_schedule_view(conn, interval["date"])
        response = {
            "closure": closure_view(closure_row),
            "closed": True,
            "changed": bool(changed_count),
            "moved": bool(changed_count),
            "changed_slot_count": changed_count,
            "minimum_changed_slots": changed_count,
            "moved_slot_count": changed_count,
            "changed_slots": changed_keys,
            "moved_slots": changed_keys,
            "changes": [c for c in changes if c["changed"]],
            "moves": [c for c in changes if c["changed"]],
            "slot_changes": changes,
            "all_slot_changes": changes,
            "schedule": day_schedule,
            "day_schedule": day_schedule,
        }
        service._store_idempotency(conn, key, scope, request_hash, 201, response)
        return 201, response


# ---------------------------------------------------------------------------
# Queries


def build_tournament_schedule_view(
    conn: sqlite3.Connection, tournament_id: int
) -> dict[str, Any]:
    service._require_tournament(conn, tournament_id)
    reservation = conn.execute(
        "SELECT * FROM schedule_reservations WHERE tournament_id=?", (tournament_id,)
    ).fetchone()
    rows = list(
        conn.execute(
            "SELECT * FROM schedule_assignments WHERE tournament_id=?", (tournament_id,)
        ).fetchall()
    )
    by_slot = {r["slot_id"]: assignment_view(r) for r in rows}
    slots = [by_slot[s] for s in SLOT_IDS if s in by_slot]
    candidates: dict[str, list[dict[str, Any]]] = {s: [] for s in SLOT_IDS}
    for row in conn.execute(
        "SELECT * FROM schedule_candidates WHERE tournament_id=? ORDER BY slot_id, option_index",
        (tournament_id,),
    ).fetchall():
        view = interval_view(row)
        view["candidate_id"] = row["id"]
        view["option_index"] = row["option_index"]
        candidates[row["slot_id"]].append(view)
    tournament = conn.execute(
        "SELECT name FROM tournaments WHERE id=?", (tournament_id,)
    ).fetchone()
    return {
        "tournament_id": tournament_id,
        "id": tournament_id,
        "tournament_name": tournament["name"],
        "reserved": reservation is not None,
        "status": reservation["status"] if reservation else None,
        "confirmed_at": reservation["created_at"] if reservation else None,
        "slots": slots,
        "schedule": slots,
        "assignments": by_slot,
        "candidates": candidates,
    }


def get_tournament_schedule(
    conn: sqlite3.Connection, tournament_id: int
) -> dict[str, Any]:
    with read_tx(conn):
        return build_tournament_schedule_view(conn, tournament_id)


def build_day_schedule_view(conn: sqlite3.Connection, day: str) -> dict[str, Any]:
    closure_rows = list(
        conn.execute(
            "SELECT * FROM venue_closures WHERE schedule_date=? "
            "ORDER BY start_minute, end_minute, venue_name, id",
            (day,),
        ).fetchall()
    )
    assignment_rows = list(
        conn.execute(
            "SELECT * FROM schedule_assignments WHERE schedule_date=?",
            (day,),
        ).fetchall()
    )
    tournament_ids = sorted({int(r["tournament_id"]) for r in assignment_rows})
    names = {
        int(r["id"]): r["name"]
        for r in conn.execute("SELECT id, name FROM tournaments").fetchall()
    }

    flat = []
    grouped: dict[int, list[dict[str, Any]]] = {}
    ordered_rows = sorted(
        assignment_rows,
        key=lambda r: (int(r["tournament_id"]), SLOT_ORDER[r["slot_id"]]),
    )
    for row in ordered_rows:
        view = assignment_view(row)
        flat.append(view)
        grouped.setdefault(int(row["tournament_id"]), []).append(view)

    tournaments = []
    for tid in tournament_ids:
        tournaments.append(
            {
                "tournament_id": tid,
                "id": tid,
                "name": names.get(tid),
                "tournament_name": names.get(tid),
                "date": day,
                "slots": grouped.get(tid, []),
                "schedule": grouped.get(tid, []),
            }
        )

    by_venue: dict[str, dict[str, Any]] = {}
    for closure in closure_rows:
        venue = closure["venue_name"]
        entry = by_venue.setdefault(
            venue,
            {"venue": venue, "venue_name": venue, "closures": [], "assignments": []},
        )
        entry["closures"].append(closure_view(closure))
    for assignment in flat:
        venue = assignment["venue_name"]
        entry = by_venue.setdefault(
            venue,
            {"venue": venue, "venue_name": venue, "closures": [], "assignments": []},
        )
        entry["assignments"].append(assignment)

    return {
        "date": day,
        "day": day,
        "closures": [closure_view(r) for r in closure_rows],
        "assignments": flat,
        "arrangements": flat,
        "current_arrangements": flat,
        "slots": flat,
        "schedules": flat,
        "bookings": tournaments,
        "tournaments": tournaments,
        "venues": sorted(by_venue.values(), key=lambda x: x["venue"]),
    }


def get_day_schedule(conn: sqlite3.Connection, day: str | None = None) -> dict[str, Any]:
    if day is None:
        day = utcnow().date().isoformat()
    else:
        day = parse_date(day)

    with read_tx(conn):
        return build_day_schedule_view(conn, day)


def list_venue_closures(
    conn: sqlite3.Connection, venue: str | None = None, day: str | None = None
) -> dict[str, Any]:
    sql = "SELECT * FROM venue_closures WHERE 1=1"
    params: list[Any] = []
    if venue is not None:
        venue = _clean_name(venue, "venue")
        sql += " AND venue_name=?"
        params.append(venue)
    if day is not None:
        day = parse_date(day)
        sql += " AND schedule_date=?"
        params.append(day)
    sql += " ORDER BY schedule_date, start_minute, venue_name, id"
    with read_tx(conn):
        rows = list(conn.execute(sql, params).fetchall())
        return {"closures": [closure_view(r) for r in rows]}


def get_receipt(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    with read_tx(conn):
        row = conn.execute("SELECT * FROM idempotency_keys WHERE key=?", (key,)).fetchone()
        if row is None:
            raise _error(404, "receipt_not_found", "No successful request receipt exists for that key")
        return {
            "key": row["key"],
            "scope": row["scope"],
            "status_code": row["status_code"],
            "created_at": row["created_at"],
            "response": json.loads(row["response_json"]),
        }
