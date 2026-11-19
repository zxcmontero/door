from __future__ import annotations

import asyncio

from dahua_client import DahuaClient

from .base import validate_open_request, validate_positive_integer


class DahuaActuator:
    def __init__(
        self,
        client: DahuaClient,
        channel: int = 1,
        timeout_seconds: int = 10,
    ) -> None:
        validate_positive_integer(channel, "channel")
        validate_positive_integer(timeout_seconds, "timeout_seconds")
        self._client = client
        self._channel = channel
        self._timeout_seconds = timeout_seconds

    async def open(self, user_id: int, ttl_seconds: int = 5) -> bool:
        validate_open_request(user_id, ttl_seconds)
        return await asyncio.wait_for(
            self._client.unlock_door(channel=self._channel),
            timeout=self._timeout_seconds,
        )
