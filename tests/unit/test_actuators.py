from __future__ import annotations

import asyncio

import pytest

from door_access.actuators import ActuatorCall, DahuaActuator, TestActuator


class FakeDahuaClient:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[int] = []

    async def unlock_door(self, channel: int = 1) -> bool:
        self.calls.append(channel)
        return self.result


def test_test_actuator_records_successful_calls_without_delay() -> None:
    actuator = TestActuator()

    async def scenario() -> None:
        assert await actuator.open(42) is True
        assert await actuator.open(43, ttl_seconds=7) is True

    asyncio.run(scenario())

    assert actuator.calls == (
        ActuatorCall(user_id=42, ttl_seconds=5),
        ActuatorCall(user_id=43, ttl_seconds=7),
    )


@pytest.mark.parametrize("user_id", [0, -1, 1.5, "1", True, None])
def test_test_actuator_rejects_invalid_user_id(user_id: object) -> None:
    actuator = TestActuator()

    with pytest.raises(ValueError, match="user_id"):
        asyncio.run(actuator.open(user_id))  # type: ignore[arg-type]

    assert actuator.calls == ()


@pytest.mark.parametrize("ttl_seconds", [0, -1, 1.5, "5", True, None])
def test_test_actuator_rejects_invalid_ttl(ttl_seconds: object) -> None:
    actuator = TestActuator()

    with pytest.raises(ValueError, match="ttl_seconds"):
        asyncio.run(actuator.open(1, ttl_seconds))  # type: ignore[arg-type]

    assert actuator.calls == ()


def test_dahua_actuator_delegates_one_confirmed_call() -> None:
    client = FakeDahuaClient()
    actuator = DahuaActuator(client, channel=2, timeout_seconds=10)  # type: ignore[arg-type]

    assert asyncio.run(actuator.open(42, ttl_seconds=5)) is True
    assert client.calls == [2]


def test_dahua_actuator_does_not_retry_after_error() -> None:
    class FailingClient(FakeDahuaClient):
        async def unlock_door(self, channel: int = 1) -> bool:
            self.calls.append(channel)
            raise RuntimeError("test error")

    client = FailingClient()
    actuator = DahuaActuator(client)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="test error"):
        asyncio.run(actuator.open(42))

    assert client.calls == [1]


def test_dahua_actuator_applies_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeDahuaClient()
    actuator = DahuaActuator(client, timeout_seconds=3)  # type: ignore[arg-type]

    async def fake_wait_for(awaitable: object, timeout: int) -> bool:
        awaitable.close()  # type: ignore[attr-defined]
        assert timeout == 3
        raise TimeoutError

    monkeypatch.setattr("door_access.actuators.dahua.asyncio.wait_for", fake_wait_for)

    with pytest.raises(TimeoutError):
        asyncio.run(actuator.open(42))

    assert client.calls == []


@pytest.mark.parametrize(
    ("channel", "timeout_seconds"),
    [(0, 10), (-1, 10), (True, 10), (1, 0), (1, -1), (1, False)],
)
def test_dahua_actuator_validates_configuration(channel: object, timeout_seconds: object) -> None:
    with pytest.raises(ValueError):
        DahuaActuator(
            FakeDahuaClient(),  # type: ignore[arg-type]
            channel=channel,  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        )
