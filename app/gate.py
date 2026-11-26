"""Durable, trusted offline gate terminal.

The server never treats an expired or timed-out permit as settled.  This small
terminal therefore keeps its own durable SQLite state:

1. import the permit issued by the HTTP service;
2. admit one permit code at a time while offline;
3. terminate the permit locally before reconnecting;
4. submit the complete record (an empty list is valid);
5. retry the same stable report/idempotency key after a crash or timeout.

A new :class:`GateTerminal` on the same state file represents a process restart
and sees every previously admitted code.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .clock import utcnow

Transport = Callable[[str, str, dict[str, Any] | None, dict[str, str] | None], tuple[int, dict[str, Any]]]


class GateError(Exception):
    """A local gate rule rejected an admission or settlement action."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


class GateNetworkError(GateError):
    pass


class HttpTransport:
    """Minimal JSON HTTP transport using the standard library."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def __call__(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = None
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return exc.code, parsed
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GateNetworkError("gate_network_unavailable", str(exc)) from exc


class GateTerminal:
    def __init__(
        self,
        state_path: str | Path,
        gate_id: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.gate_id = gate_id
        self.transport = transport
        self.conn = sqlite3.connect(str(self.state_path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._init_db()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "GateTerminal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS gate_permits (
                permit_id INTEGER PRIMARY KEY,
                permit_code TEXT,
                gate_id TEXT NOT NULL,
                venue_name TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                release_limit INTEGER NOT NULL,
                codes_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open', 'terminated', 'settled')),
                report_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                permit_json TEXT NOT NULL,
                terminated_at TEXT,
                settled_at TEXT,
                settlement_status INTEGER,
                settlement_json TEXT
            );

            CREATE TABLE IF NOT EXISTS gate_admissions (
                permit_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                admitted_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (permit_id, code),
                FOREIGN KEY (permit_id) REFERENCES gate_permits(permit_id)
            );
            """
        )
        self.conn.commit()

    @staticmethod
    def _permit_from_response(data: dict[str, Any]) -> dict[str, Any]:
        permit = data.get("permit") or data.get("offline_permit") or data
        if not isinstance(permit, dict):
            raise GateError("invalid_permit", "Permit response must be an object")
        return permit

    def import_permit(self, permit_response: dict[str, Any]) -> dict[str, Any]:
        permit = self._permit_from_response(permit_response)
        pid = permit.get("id") or permit.get("permit_id")
        gate_id = permit.get("gate_id") or permit.get("gate")
        codes = permit.get("ticket_codes") or permit.get("codes") or []
        if pid is None or not gate_id or not isinstance(codes, list) or not codes:
            raise GateError("invalid_permit", "Permit is missing id, gate_id or codes")
        if self.gate_id and gate_id != self.gate_id:
            raise GateError(
                "wrong_gate",
                "Permit was issued to another gate",
                permit_gate_id=gate_id,
                gate_id=self.gate_id,
            )
        release_limit = int(
            permit.get("release_limit")
            or permit.get("allowed_count")
            or permit.get("admission_limit")
            or len(codes)
        )
        starts_at = permit.get("starts_at") or permit.get("time_window", {}).get("starts_at")
        ends_at = permit.get("ends_at") or permit.get("time_window", {}).get("ends_at")
        venue = permit.get("venue_name") or permit.get("venue")
        if not starts_at or not ends_at or not venue:
            raise GateError("invalid_permit", "Permit is missing venue/time window")

        pid = int(pid)
        existing = self.conn.execute(
            "SELECT * FROM gate_permits WHERE permit_id=?", (pid,)
        ).fetchone()
        if existing is None:
            report_id = f"gate-report-{pid}-{uuid.uuid4().hex}"
            idem_key = f"settle-permit-{pid}-{uuid.uuid4().hex}"
            self.conn.execute(
                """
                INSERT INTO gate_permits
                    (permit_id, permit_code, gate_id, venue_name, starts_at, ends_at,
                     release_limit, codes_json, state, report_id, idempotency_key,
                     permit_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    pid,
                    permit.get("permit_code") or permit.get("code"),
                    gate_id,
                    venue,
                    starts_at,
                    ends_at,
                    release_limit,
                    json.dumps([str(c) for c in codes]),
                    report_id,
                    idem_key,
                    json.dumps(permit, ensure_ascii=False),
                ),
            )
            self.conn.commit()
        return self.get_local_permit(pid)

    def get_local_permit(self, permit_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM gate_permits WHERE permit_id=?", (permit_id,)
        ).fetchone()
        if row is None:
            raise GateError("local_permit_not_found", "Gate has not imported this permit")
        return self._permit_state(row)

    def _permit_state(self, row: sqlite3.Row) -> dict[str, Any]:
        codes = json.loads(row["codes_json"])
        used_rows = self.conn.execute(
            "SELECT code, admitted_at FROM gate_admissions WHERE permit_id=? ORDER BY rowid",
            (row["permit_id"],),
        ).fetchall()
        used = [r["code"] for r in used_rows]
        return {
            "permit_id": row["permit_id"],
            "permit_code": row["permit_code"],
            "gate_id": row["gate_id"],
            "venue_name": row["venue_name"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "release_limit": row["release_limit"],
            "codes": codes,
            "state": row["state"],
            "report_id": row["report_id"],
            "idempotency_key": row["idempotency_key"],
            "used_codes": used,
            "admitted_count": len(used),
            "remaining_local_admissions": max(0, row["release_limit"] - len(used)),
            "terminated_at": row["terminated_at"],
            "settled_at": row["settled_at"],
            "settlement": json.loads(row["settlement_json"]) if row["settlement_json"] else None,
        }

    def list_permits(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT permit_id FROM gate_permits ORDER BY permit_id").fetchall()
        return [self.get_local_permit(int(r["permit_id"])) for r in rows]

    def _find_open_permit_for_code(self, code: str) -> sqlite3.Row:
        rows = self.conn.execute(
            "SELECT * FROM gate_permits WHERE state='open' ORDER BY permit_id"
        ).fetchall()
        for row in rows:
            if code in json.loads(row["codes_json"]):
                return row
        # Distinguish a code on a terminated/settled permit for diagnostics.
        all_rows = self.conn.execute("SELECT * FROM gate_permits ORDER BY permit_id").fetchall()
        for row in all_rows:
            if code in json.loads(row["codes_json"]):
                raise GateError(
                    "permit_terminated",
                    "This offline permit has already been terminated locally",
                    permit_id=row["permit_id"],
                    state=row["state"],
                )
        raise GateError(
            "offline_code_not_allowed",
            "Gate is offline and this code is not on an imported permit",
            ticket_code=code,
        )

    def admit(
        self,
        code: str,
        *,
        at: str | datetime | None = None,
        permit_id: int | None = None,
    ) -> dict[str, Any]:
        code = str(code).strip()
        if not code:
            raise GateError("code_required", "A ticket code is required")

        if permit_id is not None:
            row = self.conn.execute(
                "SELECT * FROM gate_permits WHERE permit_id=?", (int(permit_id),)
            ).fetchone()
            if row is None:
                raise GateError("local_permit_not_found", "Permit is not imported")
            if row["state"] != "open":
                raise GateError("permit_terminated", "Permit is no longer accepting admissions")
            if code not in json.loads(row["codes_json"]):
                raise GateError("offline_code_not_allowed", "Code is not on this permit")
        else:
            row = self._find_open_permit_for_code(code)

        pid = int(row["permit_id"])
        duplicate = self.conn.execute(
            "SELECT admitted_at FROM gate_admissions WHERE permit_id=? AND code=?",
            (pid, code),
        ).fetchone()
        if duplicate is not None:
            raise GateError(
                "ticket_already_admitted_locally",
                "This code has already been admitted by this gate",
                ticket_code=code,
                admitted_at=duplicate["admitted_at"],
            )

        count = int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM gate_admissions WHERE permit_id=?", (pid,)
            ).fetchone()["n"]
        )
        if count >= int(row["release_limit"]):
            raise GateError(
                "local_permit_limit_exceeded",
                "The permit's releasable person limit has already been used",
                release_limit=row["release_limit"],
                admitted_count=count,
            )

        if at is None:
            when = utcnow()
        elif isinstance(at, datetime):
            when = at
        else:
            when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc)

        starts = datetime.fromisoformat(row["starts_at"])
        ends = datetime.fromisoformat(row["ends_at"])
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=timezone.utc)
            ends = ends.replace(tzinfo=timezone.utc)
        if not (starts <= when < ends):
            raise GateError(
                "outside_permit_window",
                "The gate is outside this permit's date/time window",
                at=when.isoformat(),
                starts_at=row["starts_at"],
                ends_at=row["ends_at"],
                venue_name=row["venue_name"],
            )

        timestamp = utcnow().isoformat()
        admitted_at = when.isoformat()
        try:
            self.conn.execute(
                """
                INSERT INTO gate_admissions (permit_id, code, admitted_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (pid, code, admitted_at, timestamp),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise GateError("ticket_already_admitted_locally", "Code was already used") from exc

        return {
            "admitted": True,
            "offline": True,
            "code": code,
            "permit_id": pid,
            "gate_id": row["gate_id"],
            "venue_name": row["venue_name"],
            "admitted_at": admitted_at,
            "permit_state": self.get_local_permit(pid),
        }

    def terminate_locally(self, permit_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM gate_permits WHERE permit_id=?", (int(permit_id),)
        ).fetchone()
        if row is None:
            raise GateError("local_permit_not_found", "Permit is not imported")
        if row["state"] == "open":
            self.conn.execute(
                "UPDATE gate_permits SET state='terminated', terminated_at=? WHERE permit_id=?",
                (utcnow().isoformat(), permit_id),
            )
            self.conn.commit()
        return self.get_local_permit(int(permit_id))

    def complete_records(self, permit_id: int) -> list[dict[str, str]]:
        rows = self.conn.execute(
            "SELECT code, admitted_at FROM gate_admissions WHERE permit_id=? ORDER BY rowid",
            (int(permit_id),),
        ).fetchall()
        permit = self.get_local_permit(int(permit_id))
        return [
            {
                "code": r["code"],
                "admitted_at": r["admitted_at"],
                "gate_id": permit["gate_id"],
                "venue_name": permit["venue_name"],
            }
            for r in rows
        ]

    def settle(
        self,
        permit_id: int | None = None,
        *,
        transport: Transport | None = None,
    ) -> tuple[int, dict[str, Any]]:
        transport = transport or self.transport
        if transport is None:
            raise GateError("transport_required", "An HTTP transport is required to settle")

        if permit_id is None:
            open_rows = self.conn.execute(
                "SELECT permit_id FROM gate_permits WHERE state != 'settled' ORDER BY permit_id"
            ).fetchall()
            if len(open_rows) != 1:
                raise GateError(
                    "permit_id_required",
                    "Specify which permit to settle",
                    permit_ids=[r["permit_id"] for r in open_rows],
                )
            permit_id = int(open_rows[0]["permit_id"])

        pid = int(permit_id)
        row = self.conn.execute("SELECT * FROM gate_permits WHERE permit_id=?", (pid,)).fetchone()
        if row is None:
            raise GateError("local_permit_not_found", "Permit is not imported")

        if row["state"] == "settled" and row["settlement_status"] is not None:
            return int(row["settlement_status"]), json.loads(row["settlement_json"] or "{}")

        # Durably stop local use before any network attempt.
        if row["state"] == "open":
            self.terminate_locally(pid)
            row = self.conn.execute("SELECT * FROM gate_permits WHERE permit_id=?", (pid,)).fetchone()

        records = self.complete_records(pid)
        payload = {
            "gate_id": row["gate_id"],
            "report_id": row["report_id"],
            "records": records,
        }
        headers = {"Idempotency-Key": row["idempotency_key"]}
        status, response = transport(
            "POST", f"/offline-permits/{pid}/settle", payload, headers
        )
        if 200 <= status < 300:
            self.conn.execute(
                """
                UPDATE gate_permits
                SET state='settled', settled_at=?, settlement_status=?, settlement_json=?
                WHERE permit_id=?
                """,
                (
                    utcnow().isoformat(),
                    status,
                    json.dumps(response, ensure_ascii=False),
                    pid,
                ),
            )
            self.conn.commit()
        return status, response


# Descriptive aliases for gate-side callers/tests.
GateTerminal.verify_and_admit = GateTerminal.admit  # type: ignore[attr-defined]
GateTerminal.check_in = GateTerminal.admit  # type: ignore[attr-defined]
GateTerminal.redeem = GateTerminal.admit  # type: ignore[attr-defined]
GateTerminal.submit_settlement = GateTerminal.settle  # type: ignore[attr-defined]
OfflineGate = GateTerminal
DurableGateTerminal = GateTerminal


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(description="Durable offline gate terminal")
    parser.add_argument("--state", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import")
    imp.add_argument("--permit-file", required=True)

    admit = sub.add_parser("admit")
    admit.add_argument("--code", required=True)
    admit.add_argument("--at")
    admit.add_argument("--permit-id", type=int)

    terminate = sub.add_parser("terminate")
    terminate.add_argument("--permit-id", type=int, required=True)

    settle = sub.add_parser("settle")
    settle.add_argument("--permit-id", type=int)
    settle.add_argument("--base-url", required=True)

    sub.add_parser("show")
    args = parser.parse_args(argv)

    terminal = GateTerminal(args.state)
    try:
        if args.command == "import":
            data = json.loads(Path(args.permit_file).read_text())
            print(json.dumps(terminal.import_permit(data), ensure_ascii=False, indent=2))
        elif args.command == "admit":
            print(
                json.dumps(
                    terminal.admit(args.code, at=args.at, permit_id=args.permit_id),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "terminate":
            print(json.dumps(terminal.terminate_locally(args.permit_id), indent=2))
        elif args.command == "settle":
            status, payload = terminal.settle(
                args.permit_id, transport=HttpTransport(args.base_url)
            )
            print(json.dumps({"status_code": status, "response": payload}, ensure_ascii=False, indent=2))
            return 0 if 200 <= status < 300 else 1
        else:
            print(json.dumps(terminal.list_permits(), ensure_ascii=False, indent=2))
        return 0
    except GateError as exc:
        print(json.dumps({"error": exc.payload()}, ensure_ascii=False, indent=2))
        return 2
    finally:
        terminal.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
