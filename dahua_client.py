from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

import httpx


logger = logging.getLogger(__name__)

DEFAULT_EVENT_CODES = ("BackKeyLight", "CallNoAnswered", "VideoMotion")


class DahuaError(RuntimeError):
    """Базовая ошибка обмена с панелью Dahua."""


class DahuaResponseError(DahuaError):
    """Панель вернула HTTP-ошибку или некорректный ответ."""


@dataclass(frozen=True, slots=True)
class DahuaEvent:
    code: str
    action: str
    index: int
    data: dict[str, Any] | None
    raw: str

    @property
    def is_start(self) -> bool:
        return self.action.lower() == "start"

    @property
    def is_call(self) -> bool:
        return self.code in {"BackKeyLight", "CallNoAnswered"}

    @classmethod
    def parse(cls, block: str) -> "DahuaEvent | None":
        raw = block.strip()
        if not raw or "Code=" not in raw:
            return None
        fields: dict[str, str] = {}
        for part in raw.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key:
                fields[key.lower()] = value.strip()
        code = fields.get("code")
        action = fields.get("action")
        if not code or not action:
            raise ValueError(f"Некорректное событие Dahua: {raw[:300]}")
        try:
            index = int(fields.get("index", "0"))
        except ValueError as exc:
            raise ValueError(f"Некорректный index события Dahua: {raw[:300]}") from exc
        data: dict[str, Any] | None = None
        raw_data = fields.get("data")
        if raw_data:
            try:
                decoded = json.loads(raw_data)
            except json.JSONDecodeError:
                data = {"raw": raw_data}
            else:
                data = decoded if isinstance(decoded, dict) else {"value": decoded}
        return cls(code=code, action=action, index=index, data=data, raw=raw)


class DahuaClient:
    def __init__(self, dahua_ip: str, dahua_user: str, dahua_password: str) -> None:
        host = dahua_ip.strip().rstrip("/")
        if not host or "://" in host:
            raise ValueError("dahua_ip должен содержать адрес без http://")
        if not dahua_user or not dahua_password:
            raise ValueError("Для Dahua необходимо указать логин и пароль")
        self.base_url = f"http://{host}"
        self._username = dahua_user
        self._password = dahua_password
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        auth=httpx.DigestAuth(self._username, self._password),
                        timeout=10.0,
                    )
        return self._client

    async def close(self) -> None:
        async with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> "DahuaClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def unlock_door(self, channel: int = 1) -> bool:
        if channel <= 0:
            raise ValueError("channel должен быть положительным")
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self.base_url}/cgi-bin/accessControl.cgi",
                params={
                    "action": "openDoor",
                    "channel": channel,
                    "UserID": 101,
                    "Type": "Remote",
                },
            )
        except httpx.HTTPError as exc:
            raise DahuaError(f"Не удалось выполнить открытие двери: {exc}") from exc
        if response.status_code != httpx.codes.OK:
            raise DahuaResponseError(
                f"Открытие двери отклонено: HTTP {response.status_code}, "
                f"ответ: {response.text[:300]!r}"
            )
        answer = response.text.strip()
        if not answer.upper().startswith("OK"):
            raise DahuaResponseError(f"Неожиданный ответ открытия двери: {answer[:300]!r}")
        logger.info("Дверь успешно открыта через Dahua, channel=%s", channel)
        return True

    async def snapshot(self, channel: int = 1) -> bytes:
        if channel <= 0:
            raise ValueError("channel должен быть положительным")
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self.base_url}/cgi-bin/snapshot.cgi", params={"channel": channel}
            )
        except httpx.HTTPError as exc:
            raise DahuaError(f"Не удалось получить snapshot: {exc}") from exc
        if response.status_code != httpx.codes.OK:
            raise DahuaResponseError(
                f"Snapshot отклонен: HTTP {response.status_code}, "
                f"ответ: {response.text[:300]!r}"
            )
        content = response.content
        if len(content) < 4 or not content.startswith(b"\xff\xd8"):
            raise DahuaResponseError("Dahua вернула ответ snapshot без JPEG-изображения")
        return content

    @staticmethod
    def _lines_for_event(lines: Iterable[str]) -> list[str]:
        event_lines: list[str] = []
        for source_line in lines:
            line = source_line.strip()
            if not line or line.startswith("--"):
                continue
            lower = line.lower()
            if lower.startswith("content-type:") or lower.startswith("content-length:"):
                continue
            event_lines.append(line)
        return event_lines

    @classmethod
    def _parse_lines(cls, lines: Iterable[str]) -> DahuaEvent | None:
        event_lines = cls._lines_for_event(lines)
        if not event_lines:
            return None
        return DahuaEvent.parse("\n".join(event_lines))

    @staticmethod
    async def _wait_before_reconnect(delay: float, stop_event: asyncio.Event | None) -> bool:
        if stop_event is None:
            await asyncio.sleep(delay)
            return True
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            return True
        return not stop_event.is_set()

    async def listen_events(
        self,
        codes: Iterable[str] = DEFAULT_EVENT_CODES,
        heartbeat: int = 5,
        reconnect: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[DahuaEvent]:
        event_codes = [code.strip() for code in codes if code.strip()]
        if not event_codes:
            raise ValueError("Необходимо указать хотя бы один код события Dahua")
        if heartbeat <= 0:
            raise ValueError("heartbeat должен быть положительным")
        client = await self._get_client()
        params = {
            "action": "attach",
            "codes": f"[{','.join(event_codes)}]",
            "heartbeat": heartbeat,
        }
        retry_delay = 1.0
        while stop_event is None or not stop_event.is_set():
            try:
                async with client.stream(
                    "GET", f"{self.base_url}/cgi-bin/eventManager.cgi", params=params
                ) as response:
                    if response.status_code != httpx.codes.OK:
                        body = await response.aread()
                        raise DahuaResponseError(
                            f"Event stream отклонен: HTTP {response.status_code}, "
                            f"ответ: {body[:300]!r}"
                        )
                    retry_delay = 1.0
                    logger.info("Подключен поток событий Dahua: %s", ",".join(event_codes))
                    lines: list[str] = []
                    async for line in response.aiter_lines():
                        if stop_event is not None and stop_event.is_set():
                            return
                        if line.strip():
                            lines.append(line)
                            continue
                        try:
                            event = self._parse_lines(lines)
                        except ValueError as exc:
                            logger.warning("Пропущено некорректное событие Dahua: %s", exc)
                        else:
                            if event is not None:
                                logger.debug(
                                    "Событие Dahua: code=%s action=%s index=%s",
                                    event.code,
                                    event.action,
                                    event.index,
                                )
                                yield event
                        lines = []
                    if lines:
                        try:
                            event = self._parse_lines(lines)
                        except ValueError as exc:
                            logger.warning("Пропущено некорректное событие Dahua: %s", exc)
                        else:
                            if event is not None:
                                yield event
                raise DahuaError("Поток событий Dahua завершен устройством")
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, DahuaError) as exc:
                if not reconnect:
                    raise
                logger.warning(
                    "Поток событий Dahua разорван: %s. Переподключение через %.1f с",
                    exc,
                    retry_delay,
                )
                if not await self._wait_before_reconnect(retry_delay, stop_event):
                    return
                retry_delay = min(retry_delay * 2.0, 30.0)
