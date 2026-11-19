from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config import Settings
from cv_worker import CVWorker, EntryEvent
from dahua_client import DahuaClient
from door_access.actuators import ActuatorCall, TestActuator


ENV_NAMES = (
    "APP_ENV",
    "ACTUATOR_MODE",
    "BOT_TOKEN",
    "BOT_USERNAME",
    "ADMIN_IDS",
    "LOG_CHAT_ID",
    "DAHUA_IP",
    "DAHUA_USER",
    "DAHUA_PASS",
    "DAHUA_RTSP_PORT",
    "DAHUA_CHANNEL",
    "ACTUATOR_OPEN_TTL_SECONDS",
    "ACTUATOR_TIMEOUT_SECONDS",
    "DATABASE_PATH",
    "MEDIA_DIR",
    "FACE_TASKS_DIR",
    "TEMP_MEDIA_DIR",
    "RTSP_FPS",
    "PRE_ROLL_SECONDS",
    "POST_ROLL_SECONDS",
    "LIVE_CLIP_SECONDS",
    "FACE_ANALYSIS_INTERVAL",
    "FACE_SIMILARITY_THRESHOLD",
    "FACE_COOLDOWN_SECONDS",
    "DOOR_UNLOCK_COOLDOWN_SECONDS",
    "COURIER_FACE_TTL_SECONDS",
    "PUBLIC_BASE_URL",
    "HTTP_HOST",
    "HTTP_PORT",
)


class FakeDatabase:
    def __init__(self) -> None:
        self.unlock_timestamps: list[float] = []

    def record_unlock(self, timestamp: float) -> None:
        self.unlock_timestamps.append(timestamp)


def make_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CVWorker:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "synthetic-bot-token")
    monkeypatch.setenv("ADMIN_IDS", "123456")
    monkeypatch.setenv("DAHUA_IP", "192.0.2.10")
    monkeypatch.setenv("DAHUA_USER", "synthetic-user")
    monkeypatch.setenv("DAHUA_PASS", "synthetic-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "access.db"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("FACE_TASKS_DIR", str(tmp_path / "face_tasks"))
    monkeypatch.setenv("TEMP_MEDIA_DIR", str(tmp_path / "temp"))

    async def fail_dahua_open(self: DahuaClient, channel: int = 1) -> bool:
        raise AssertionError("Dahua must not be called in test mode")

    async def fail_network(self: DahuaClient) -> object:
        raise AssertionError("Network must not be used in test mode")

    monkeypatch.setattr(DahuaClient, "unlock_door", fail_dahua_open)
    monkeypatch.setattr(DahuaClient, "_get_client", fail_network)
    return CVWorker(Settings.from_env())


def install_fake_side_effects(worker: CVWorker) -> tuple[FakeDatabase, list[object]]:
    database = FakeDatabase()
    spawned: list[object] = []
    worker.db = database  # type: ignore[assignment]

    def fake_spawn(coroutine: object) -> None:
        coroutine.close()  # type: ignore[attr-defined]
        spawned.append(coroutine)

    worker._spawn = fake_spawn  # type: ignore[method-assign]
    return database, spawned


def test_worker_test_mode_never_calls_dahua(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = make_worker(monkeypatch, tmp_path)
    database, spawned = install_fake_side_effects(worker)

    assert isinstance(worker.actuator, TestActuator)
    opened = asyncio.run(
        worker.unlock_door(EntryEvent(method="button", user_id=42, details="synthetic"))
    )

    assert opened is True
    assert worker.actuator.calls == (ActuatorCall(user_id=42, ttl_seconds=5),)
    assert len(database.unlock_timestamps) == 1
    assert len(spawned) == 1


def test_unconfirmed_actuator_result_does_not_record_opening(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = make_worker(monkeypatch, tmp_path)
    database, spawned = install_fake_side_effects(worker)

    class UnconfirmedActuator:
        async def open(self, user_id: int, ttl_seconds: int = 5) -> bool:
            return False

    worker.actuator = UnconfirmedActuator()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="не подтвердил"):
        asyncio.run(
            worker.unlock_door(EntryEvent(method="button", user_id=42, details="synthetic"))
        )

    assert worker._last_unlock_ts == 0.0
    assert database.unlock_timestamps == []
    assert spawned == []
