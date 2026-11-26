"""HTTP layer for the tournament service."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import service
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
