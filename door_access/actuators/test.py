from __future__ import annotations

from dataclasses import dataclass

from .base import validate_open_request


@dataclass(frozen=True, slots=True)
class ActuatorCall:
    user_id: int
    ttl_seconds: int


class TestActuator:
    __test__ = False

    def __init__(self) -> None:
        self._calls: list[ActuatorCall] = []

    @property
    def calls(self) -> tuple[ActuatorCall, ...]:
        return tuple(self._calls)

    async def open(self, user_id: int, ttl_seconds: int = 5) -> bool:
        validate_open_request(user_id, ttl_seconds)
        self._calls.append(ActuatorCall(user_id=user_id, ttl_seconds=ttl_seconds))
        return True
