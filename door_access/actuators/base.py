from __future__ import annotations

from abc import abstractmethod
from typing import Protocol


class Actuator(Protocol):
    @abstractmethod
    async def open(self, user_id: int, ttl_seconds: int = 5) -> bool:
        raise NotImplementedError


def validate_positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} должен быть положительным целым числом")


def validate_open_request(user_id: int, ttl_seconds: int) -> None:
    validate_positive_integer(user_id, "user_id")
    validate_positive_integer(ttl_seconds, "ttl_seconds")
