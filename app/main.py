"""HTTP layer for the tournament service."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import scheduling, service
from .db import connect, init_db


class CreateTournamentBody(BaseModel):
    name: str = Field(default="Eight-player cup", min_length=1)
    players: list[str] = Field(min_length=8, max_length=8)


class StartMatchBody(BaseModel):
    started_by: str = Field(min_length=1)


class ReportResultBody(BaseModel):
    winner_player_id: int
    reported_by: str = Field(min_length=1)


class FileAppealBody(BaseModel):
    result_version_id: int
    description: str = Field(min_length=1)
    submitted_by: str = Field(min_length=1)


class AdjudicateBody(BaseModel):
    outcome: Literal["upheld", "overturned"]
    expected_result_version_id: int
    adjudicated_by: str = Field(min_length=1)
    note: str | None = None


def _conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _ok(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload)


def create_app(db_path: str | Path) -> FastAPI:
    db_path = Path(db_path)
    init_db(db_path)

    app = FastAPI(
        title="Eight-player knockout tournament service",
        version="1.0.0",
    )
    app.state.db_path = db_path

    @app.exception_handler(service.TournamentError)
    async def tournament_error_handler(
        request: Request, exc: service.TournamentError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.payload()})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/tournaments", status_code=201)
    def create_tournament(
        body: CreateTournamentBody,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> JSONResponse:
        return _ok(
            service.create_tournament(conn, body.name, body.players), status_code=201
        )

    @app.get("/tournaments/{tournament_id}/bracket")
    def get_bracket(
        tournament_id: int,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return service.get_bracket(conn, tournament_id)

    # ------------------------------------------------------------------
    # Venue reservations and closure-aware rescheduling

    reservation_paths = [
        "/tournaments/{tournament_id}/schedule",
        "/tournaments/{tournament_id}/scheduling",
        "/tournaments/{tournament_id}/reservation",
        "/tournaments/{tournament_id}/reservations",
        "/tournaments/{tournament_id}/schedule/reservation",
        "/tournaments/{tournament_id}/schedule/reservations",
        "/tournaments/{tournament_id}/schedule/book",
        "/tournaments/{tournament_id}/schedule/confirm",
        "/tournaments/{tournament_id}/booking",
        "/tournaments/{tournament_id}/bookings",
        "/tournaments/{tournament_id}/venue-booking",
        "/tournaments/{tournament_id}/venue-bookings",
        "/tournaments/{tournament_id}/venue-reservation",
        "/tournaments/{tournament_id}/venue-reservations",
    ]

    def reserve_route(
        tournament_id: int,
        body: dict[str, Any],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        conn: sqlite3.Connection = Depends(_conn),
    ) -> JSONResponse:
        status, payload = scheduling.reserve_tournament(
            conn, tournament_id, body, idempotency_key
        )
        return _ok(payload, status_code=status)

    for path in reservation_paths:
        app.add_api_route(
            path,
            reserve_route,
            methods=["POST", "PUT"],
            status_code=201,
        )

    @app.post("/reservations", status_code=201)
    @app.post("/reserve", status_code=201)
    @app.post("/bookings", status_code=201)
    @app.post("/schedule/reserve", status_code=201)
    @app.post("/schedule/reservations", status_code=201)
    def reserve_without_tournament_prefix(
        body: dict[str, Any],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        conn: sqlite3.Connection = Depends(_conn),
    ) -> JSONResponse:
        tournament_id = (
            body.get("tournament_id")
            or body.get("tournamentId")
            or body.get("id")
            or (body.get("tournament") or {}).get("id")
            if isinstance(body.get("tournament"), dict)
            else body.get("tournament_id") or body.get("tournamentId") or body.get("id")
        )
        if tournament_id is None:
            raise service.TournamentError(
                422, "tournament_id_required", "tournament_id is required"
            )
        status, payload = scheduling.reserve_tournament(
            conn, int(tournament_id), body, idempotency_key
        )
        return _ok(payload, status_code=status)

    tournament_schedule_get_paths = [
        "/tournaments/{tournament_id}/schedule",
        "/tournaments/{tournament_id}/reservation",
        "/tournaments/{tournament_id}/reservations",
        "/tournaments/{tournament_id}/venue-schedule",
        "/tournaments/{tournament_id}/venue-reservation",
    ]

    def get_tournament_schedule_route(
        tournament_id: int,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return scheduling.get_tournament_schedule(conn, tournament_id)

    for path in tournament_schedule_get_paths:
        app.add_api_route(path, get_tournament_schedule_route, methods=["GET"])

    closure_post_paths = [
        "/closures",
        "/closure",
        "/venue-closures",
        "/venue-closure",
        "/schedule/closures",
        "/schedule/closure",
        "/venues/closures",
        "/api/closures",
    ]

    def create_closure_route(
        body: dict[str, Any],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        venue_hint: str | None = None,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> JSONResponse:
        status, payload = scheduling.register_closure(
            conn, body, idempotency_key, venue_hint=venue_hint
        )
        return _ok(payload, status_code=status)

    for path in closure_post_paths:
        app.add_api_route(path, create_closure_route, methods=["POST"], status_code=201)
    app.add_api_route(
        "/venues/{venue_hint}/closures",
        create_closure_route,
        methods=["POST", "PUT"],
        status_code=201,
    )
    app.add_api_route(
        "/venues/{venue_hint}/closure",
        create_closure_route,
        methods=["POST", "PUT"],
        status_code=201,
    )
    app.add_api_route(
        "/facilities/{venue_hint}/closures",
        create_closure_route,
        methods=["POST", "PUT"],
        status_code=201,
    )

    @app.get("/schedule")
    @app.get("/schedules")
    @app.get("/calendar")
    @app.get("/schedule/day")
    @app.get("/day-schedule")
    @app.get("/venue-schedule")
    @app.get("/venues/schedule")
    @app.get("/schedule/venue-day")
    def get_day_schedule(
        date: str | None = None,
        day: str | None = None,
        utc_date: str | None = None,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return scheduling.get_day_schedule(conn, date or day or utc_date)

    @app.get("/schedule/date/{day}")
    @app.get("/schedule/day/{day}")
    @app.get("/days/{day}/schedule")
    @app.get("/schedules/{day}")
    def get_day_schedule_by_path(
        day: str,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return scheduling.get_day_schedule(conn, day)

    @app.get("/closures")
    @app.get("/venue-closures")
    @app.get("/schedule/closures")
    def list_closures(
        date: str | None = None,
        day: str | None = None,
        venue: str | None = None,
        venue_name: str | None = None,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return scheduling.list_venue_closures(
            conn, venue or venue_name, date or day
        )

    @app.get("/venues/{venue}/closures")
    def list_venue_closures(
        venue: str,
        date: str | None = None,
        day: str | None = None,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return scheduling.list_venue_closures(conn, venue, date or day)

    @app.get("/venues/{venue}/schedule")
    def get_venue_day_schedule(
        venue: str,
        date: str | None = None,
        day: str | None = None,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        day_value = date or day
        result = scheduling.get_day_schedule(conn, day_value)
        result["venue"] = venue
        result["venue_name"] = venue
        result["assignments"] = [
            item for item in result["assignments"] if item["venue_name"] == venue
        ]
        result["closures"] = [
            item for item in result["closures"] if item["venue_name"] == venue
        ]
        return result

    @app.get("/idempotency/{key}")
    @app.get("/receipts/{key}")
    def get_receipt(
        key: str,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return scheduling.get_receipt(conn, key)

    @app.get("/tournaments/{tournament_id}/history")
    def get_history(
        tournament_id: int,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return service.get_history(conn, tournament_id)

    @app.get("/tournaments/{tournament_id}/appeals")
    def list_appeals(
        tournament_id: int,
        status: str | None = None,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return service.list_appeals(conn, tournament_id, status)

    @app.post(
        "/tournaments/{tournament_id}/slots/{slot_id}/start", status_code=201
    )
    def start_match(
        tournament_id: int,
        slot_id: str,
        body: StartMatchBody,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        conn: sqlite3.Connection = Depends(_conn),
    ) -> JSONResponse:
        status, payload = service.start_match(
            conn,
            tournament_id,
            slot_id,
            body.started_by,
            idempotency_key,
        )
        return _ok(payload, status_code=status)

    @app.post(
        "/tournaments/{tournament_id}/slots/{slot_id}/result", status_code=200
    )
    def report_result(
        tournament_id: int,
        slot_id: str,
        body: ReportResultBody,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        conn: sqlite3.Connection = Depends(_conn),
    ) -> JSONResponse:
        status, payload = service.report_result(
            conn,
            tournament_id,
            slot_id,
            body.winner_player_id,
            body.reported_by,
            idempotency_key,
        )
        return _ok(payload, status_code=status)

    @app.post(
        "/tournaments/{tournament_id}/slots/{slot_id}/appeals", status_code=201
    )
    def file_appeal(
        tournament_id: int,
        slot_id: str,
        body: FileAppealBody,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        conn: sqlite3.Connection = Depends(_conn),
    ) -> JSONResponse:
        status, payload = service.file_appeal(
            conn,
            tournament_id,
            slot_id,
            body.result_version_id,
            body.description,
            body.submitted_by,
            idempotency_key,
        )
        return _ok(payload, status_code=status)

    @app.post(
        "/tournaments/{tournament_id}/appeals/{appeal_id}/adjudicate", status_code=200
    )
    def adjudicate_appeal(
        tournament_id: int,
        appeal_id: int,
        body: AdjudicateBody,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        conn: sqlite3.Connection = Depends(_conn),
    ) -> JSONResponse:
        status, payload = service.adjudicate_appeal(
            conn,
            tournament_id,
            appeal_id,
            body.outcome,
            body.expected_result_version_id,
            body.adjudicated_by,
            body.note,
            idempotency_key,
        )
        return _ok(payload, status_code=status)

    @app.get("/tournaments/{tournament_id}/matches/{match_id}")
    def get_match(
        tournament_id: int,
        match_id: int,
        conn: sqlite3.Connection = Depends(_conn),
    ) -> dict[str, Any]:
        return service.get_match(conn, tournament_id, match_id)

    return app
