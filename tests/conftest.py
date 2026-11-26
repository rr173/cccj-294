"""Shared fixtures for the tournament service tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402

PLAYER_NAMES = [f"P{i}" for i in range(1, 9)]


class Api:
    """Thin, explicit HTTP helper so tests read like the documented flow."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.tournament_id: int | None = None
        # Keys include the tournament id so two Api helpers on one database
        # (two tournaments) never collide.
        self._keys = 0
        self._key_prefix = f"t{id(self)}"

    def fresh_key(self, prefix: str) -> str:
        self._keys += 1
        return f"{self._key_prefix}-{prefix}-{self._keys}"

    def create(self, names: list[str] | None = None) -> dict:
        response = self.client.post(
            "/tournaments",
            json={"name": "Club championship", "players": names or PLAYER_NAMES},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        self.tournament_id = body["tournament"]["id"]
        return body

    def bracket(self) -> dict:
        response = self.client.get(f"/tournaments/{self.tournament_id}/bracket")
        assert response.status_code == 200, response.text
        return response.json()

    def history(self) -> dict:
        response = self.client.get(f"/tournaments/{self.tournament_id}/history")
        assert response.status_code == 200, response.text
        return response.json()

    def slot(self, slot_id: str) -> dict:
        for round_ in self.bracket()["rounds"]:
            for slot in round_["slots"]:
                if slot["slot_id"] == slot_id:
                    return slot
        raise AssertionError(f"no slot {slot_id}")

    def start(self, slot_id: str, by: str = "alice", key: str | None = None):
        return self.client.post(
            f"/tournaments/{self.tournament_id}/slots/{slot_id}/start",
            json={"started_by": by},
            headers={"Idempotency-Key": key or self.fresh_key(f"start-{slot_id}")},
        )

    def start_ok(self, slot_id: str, by: str = "alice", key: str | None = None) -> dict:
        response = self.start(slot_id, by=by, key=key)
        assert response.status_code == 201, response.text
        return response.json()

    def result(
        self,
        slot_id: str,
        winner_player_id: int,
        by: str = "alice",
        key: str | None = None,
    ):
        return self.client.post(
            f"/tournaments/{self.tournament_id}/slots/{slot_id}/result",
            json={"winner_player_id": winner_player_id, "reported_by": by},
            headers={"Idempotency-Key": key or self.fresh_key(f"result-{slot_id}")},
        )

    def result_ok(
        self,
        slot_id: str,
        winner_player_id: int,
        by: str = "alice",
        key: str | None = None,
    ) -> dict:
        response = self.result(slot_id, winner_player_id, by=by, key=key)
        assert response.status_code == 200, response.text
        return response.json()

    def appeal(
        self,
        slot_id: str,
        result_version_id: int,
        description: str = "Score sheet dispute",
        by: str = "coach",
    ):
        return self.client.post(
            f"/tournaments/{self.tournament_id}/slots/{slot_id}/appeals",
            json={
                "result_version_id": result_version_id,
                "description": description,
                "submitted_by": by,
            },
        )

    def appeal_ok(self, slot_id: str, description: str = "Score sheet dispute") -> dict:
        slot = self.slot(slot_id)
        version_id = slot["current_match"]["result"]["id"]
        response = self.appeal(slot_id, version_id, description=description)
        assert response.status_code == 201, response.text
        return response.json()

    def adjudicate(
        self,
        appeal_id: int,
        outcome: str,
        expected_result_version_id: int,
        by: str = "judge",
        note: str | None = None,
        key: str | None = None,
    ):
        return self.client.post(
            f"/tournaments/{self.tournament_id}/appeals/{appeal_id}/adjudicate",
            json={
                "outcome": outcome,
                "expected_result_version_id": expected_result_version_id,
                "adjudicated_by": by,
                "note": note,
            },
            headers={
                "Idempotency-Key": key or self.fresh_key(f"adj-{appeal_id}")
            },
        )

    def adjudicate_ok(
        self,
        appeal_id: int,
        outcome: str,
        expected_result_version_id: int,
        key: str | None = None,
    ) -> dict:
        response = self.adjudicate(
            appeal_id,
            outcome,
            expected_result_version_id,
            key=key,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def player_id(self, seed: int) -> int:
        for player in self.bracket()["players"]:
            if player["seed"] == seed:
                return player["id"]
        raise AssertionError(f"no seed {seed}")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tournament.db"


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path))


@pytest.fixture
def api(client: TestClient) -> Api:
    instance = Api(client)
    instance.create()
    return instance


def play_quarterfinals(api: Api) -> dict[str, int]:
    """Finish all four quarterfinals: seeds 1, 5, 3, 2 advance."""
    winners = {
        "QF1": 1,  # seed 1 beats seed 8
        "QF2": 5,  # seed 5 beats seed 4
        "QF3": 3,  # seed 3 beats seed 6
        "QF4": 2,  # seed 2 beats seed 7
    }
    played: dict[str, int] = {}
    for slot_id, seed in winners.items():
        player_id = api.player_id(seed)
        api.start_ok(slot_id)
        api.result_ok(slot_id, player_id)
        played[slot_id] = player_id
    return played


def play_full_tournament(api: Api) -> dict[str, int]:
    """Finish a whole bracket; P1, P5, P3, P2 reach the semifinals.

    Returns a mapping of slot -> winning player id for convenience.
    """
    played = play_quarterfinals(api)

    # SF1: seed 1 vs seed 5 -> seed 1.  SF2: seed 3 vs seed 2 -> seed 3.
    api.start_ok("SF1")
    api.result_ok("SF1", api.player_id(1))
    played["SF1"] = api.player_id(1)
    api.start_ok("SF2")
    api.result_ok("SF2", api.player_id(3))
    played["SF2"] = api.player_id(3)

    # Final: seed 1 beats seed 3.
    api.start_ok("F")
    api.result_ok("F", api.player_id(1))
    played["F"] = api.player_id(1)
    return played
