from __future__ import annotations

import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import aiosqlite
import numpy as np


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    role TEXT NOT NULL CHECK (role IN ('admin', 'resident', 'blocked')),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS face_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    person_name TEXT NOT NULL,
    embedding BLOB NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS face_tasks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    person_name TEXT NOT NULL,
    image_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'error')),
    result_msg TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS qr_tokens (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    creator_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    qr_type TEXT NOT NULL CHECK (qr_type IN ('permanent', 'temporary', 'courier')),
    max_uses INTEGER NOT NULL DEFAULT 0,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS courier_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    unload_mode_until REAL NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ipc_commands (
    id INTEGER PRIMARY KEY,
    cmd_type TEXT NOT NULL CHECK (
        cmd_type IN ('unlock_door', 'capture_live_clip', 'enable_unload_mode', 'reload_faces')
    ),
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processed')),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_outbox (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('video', 'photo', 'text')),
    file_path TEXT,
    caption TEXT,
    keyboard_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent')),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS door_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_motion_ts REAL NOT NULL DEFAULT 0,
    last_person_ts REAL NOT NULL DEFAULT 0,
    people_count INTEGER NOT NULL DEFAULT 0,
    last_unlock_ts REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS access_logs (
    id INTEGER PRIMARY KEY,
    method TEXT NOT NULL CHECK (
        method IN ('face', 'qr_camera', 'qr_deeplink', 'button', 'courier_auto', 'courier_barcode')
    ),
    user_id INTEGER,
    details TEXT,
    people_entered INTEGER NOT NULL DEFAULT 1,
    tailgating_alert INTEGER NOT NULL DEFAULT 0,
    video_path TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_face_embeddings_active
    ON face_embeddings(is_active, user_id);
CREATE INDEX IF NOT EXISTS idx_face_tasks_status
    ON face_tasks(status, id);
CREATE INDEX IF NOT EXISTS idx_qr_tokens_creator
    ON qr_tokens(creator_id, created_at);
CREATE INDEX IF NOT EXISTS idx_courier_sessions_active
    ON courier_sessions(is_active, expires_at);
CREATE INDEX IF NOT EXISTS idx_ipc_commands_status
    ON ipc_commands(status, id);
CREATE INDEX IF NOT EXISTS idx_bot_outbox_status
    ON bot_outbox(status, id);
CREATE INDEX IF NOT EXISTS idx_access_logs_created
    ON access_logs(created_at DESC);

INSERT INTO door_state (id, last_motion_ts, last_person_ts, people_count, last_unlock_ts)
VALUES (1, 0, 0, 0, 0)
ON CONFLICT(id) DO NOTHING;
"""

USER_ROLES = {"admin", "resident", "blocked"}
MEDIA_TYPES = {"video", "photo", "text"}
COMMAND_TYPES = {"unlock_door", "capture_live_clip", "enable_unload_mode", "reload_faces"}
QR_TYPES = {"permanent", "temporary", "courier"}
ACCESS_METHODS = {
    "face",
    "qr_camera",
    "qr_deeplink",
    "button",
    "courier_auto",
    "courier_barcode",
}
DOOR_STATE_FIELDS = {"last_motion_ts", "last_person_ts", "people_count", "last_unlock_ts"}


@dataclass(frozen=True, slots=True)
class QRConsumeResult:
    allowed: bool
    reason: str
    token: dict[str, Any] | None


def _serialize_embedding(embedding: np.ndarray | bytes) -> bytes:
    if isinstance(embedding, bytes):
        array = np.frombuffer(embedding, dtype=np.float32)
    else:
        array = np.asarray(embedding, dtype=np.float32)
    if array.size != 512:
        raise ValueError("Эмбеддинг лица должен содержать 512 значений float32")
    array = array.reshape(512)
    if not np.isfinite(array).all():
        raise ValueError("Эмбеддинг лица содержит NaN или бесконечность")
    if float(np.linalg.norm(array)) == 0.0:
        raise ValueError("Эмбеддинг лица не может быть нулевым вектором")
    return array.tobytes()


def _row_dict(row: sqlite3.Row | aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows_dict(rows: list[sqlite3.Row] | list[aiosqlite.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _qr_rejection_reason(row: sqlite3.Row | aiosqlite.Row | None, now: float) -> str | None:
    if row is None:
        return "not_found"
    if not row["is_active"]:
        return "inactive"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    if row["max_uses"] > 0 and row["used_count"] >= row["max_uses"]:
        return "exhausted"
    return None


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit должен быть в диапазоне от 1 до 500")


class _DatabaseBase:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)


class Database(_DatabaseBase):
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path, timeout=5.0)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA journal_mode=WAL;")
            await connection.execute("PRAGMA synchronous=NORMAL;")
            await connection.execute("PRAGMA busy_timeout=5000;")
            yield connection
        finally:
            await connection.close()

    async def init(self) -> None:
        async with self.connection() as connection:
            await connection.executescript(SCHEMA)
            await connection.commit()

    async def upsert_user(
        self,
        telegram_id: int,
        username: str | None,
        full_name: str,
        role: str = "resident",
    ) -> None:
        if role not in USER_ROLES:
            raise ValueError(f"Недопустимая роль пользователя: {role}")
        async with self.connection() as connection:
            await connection.execute(
                """
                INSERT INTO users (telegram_id, username, full_name, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name
                """,
                (telegram_id, username, full_name, role, time.time()),
            )
            await connection.commit()

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            return _row_dict(await cursor.fetchone())

    async def list_users(self, limit: int = 100) -> list[dict[str, Any]]:
        _validate_limit(limit)
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            return _rows_dict(await cursor.fetchall())

    async def set_user_role(self, telegram_id: int, role: str) -> bool:
        if role not in USER_ROLES:
            raise ValueError(f"Недопустимая роль пользователя: {role}")
        async with self.connection() as connection:
            cursor = await connection.execute(
                "UPDATE users SET role = ? WHERE telegram_id = ?", (role, telegram_id)
            )
            await connection.commit()
            return cursor.rowcount > 0

    async def get_notification_chat_ids(self) -> list[int]:
        async with self.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT telegram_id FROM users
                WHERE role IN ('admin', 'resident')
                ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at
                """
            )
            rows = await cursor.fetchall()
            return [int(row["telegram_id"]) for row in rows]

    async def list_face_embeddings(
        self, user_id: int, only_active: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM face_embeddings WHERE user_id = ?"
        parameters: tuple[Any] = (user_id,)
        if only_active:
            query += " AND is_active = 1"
        query += " ORDER BY created_at DESC"
        async with self.connection() as connection:
            cursor = await connection.execute(query, parameters)
            return _rows_dict(await cursor.fetchall())

    async def set_face_embedding_active(
        self, face_id: int, user_id: int, is_active: bool
    ) -> bool:
        async with self.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE face_embeddings SET is_active = ?
                WHERE id = ? AND user_id = ?
                """,
                (int(is_active), face_id, user_id),
            )
            await connection.commit()
            return cursor.rowcount > 0

    async def create_face_task(
        self, user_id: int, person_name: str, image_path: str
    ) -> int:
        async with self.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO face_tasks
                    (user_id, person_name, image_path, status, result_msg, created_at)
                VALUES (?, ?, ?, 'pending', NULL, ?)
                """,
                (user_id, person_name, image_path, time.time()),
            )
            await connection.commit()
            return int(cursor.lastrowid)

    async def get_face_task(self, task_id: int) -> dict[str, Any] | None:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM face_tasks WHERE id = ?", (task_id,)
            )
            return _row_dict(await cursor.fetchone())

    async def create_qr_token(
        self,
        token: str,
        creator_id: int,
        label: str,
        qr_type: str,
        max_uses: int = 0,
        expires_at: float | None = None,
    ) -> int:
        if qr_type not in QR_TYPES:
            raise ValueError(f"Недопустимый тип QR: {qr_type}")
        if max_uses < 0:
            raise ValueError("max_uses не может быть отрицательным")
        async with self.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO qr_tokens
                    (token, creator_id, label, qr_type, max_uses, used_count,
                     expires_at, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, 1, ?)
                """,
                (token, creator_id, label, qr_type, max_uses, expires_at, time.time()),
            )
            await connection.commit()
            return int(cursor.lastrowid)

    async def get_qr_token(self, token: str) -> dict[str, Any] | None:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM qr_tokens WHERE token = ?", (token,)
            )
            return _row_dict(await cursor.fetchone())

    async def list_qr_tokens(
        self, creator_id: int | None = None, include_inactive: bool = True
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if creator_id is not None:
            conditions.append("creator_id = ?")
            parameters.append(creator_id)
        if not include_inactive:
            conditions.append("is_active = 1")
        query = "SELECT * FROM qr_tokens"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        async with self.connection() as connection:
            cursor = await connection.execute(query, tuple(parameters))
            return _rows_dict(await cursor.fetchall())

    async def set_qr_token_active(
        self, qr_id: int, is_active: bool, creator_id: int | None = None
    ) -> bool:
        query = "UPDATE qr_tokens SET is_active = ? WHERE id = ?"
        parameters: list[Any] = [int(is_active), qr_id]
        if creator_id is not None:
            query += " AND creator_id = ?"
            parameters.append(creator_id)
        async with self.connection() as connection:
            cursor = await connection.execute(query, tuple(parameters))
            await connection.commit()
            return cursor.rowcount > 0

    async def consume_qr_token(self, token: str) -> QRConsumeResult:
        now = time.time()
        async with self.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE;")
            cursor = await connection.execute(
                "SELECT * FROM qr_tokens WHERE token = ?", (token,)
            )
            row = await cursor.fetchone()
            reason = _qr_rejection_reason(row, now)
            if reason is not None:
                await connection.commit()
                return QRConsumeResult(False, reason, _row_dict(row))
            await connection.execute(
                "UPDATE qr_tokens SET used_count = used_count + 1 WHERE id = ?",
                (row["id"],),
            )
            result = dict(row)
            result["used_count"] += 1
            await connection.commit()
            return QRConsumeResult(True, "ok", result)

    async def create_courier_session(
        self, user_id: int, duration_seconds: int
    ) -> int:
        if duration_seconds <= 0:
            raise ValueError("Длительность курьерской сессии должна быть положительной")
        now = time.time()
        async with self.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE;")
            await connection.execute(
                "UPDATE courier_sessions SET is_active = 0, unload_mode_until = 0 WHERE user_id = ?",
                (user_id,),
            )
            cursor = await connection.execute(
                """
                INSERT INTO courier_sessions
                    (user_id, expires_at, unload_mode_until, is_active, created_at)
                VALUES (?, ?, 0, 1, ?)
                """,
                (user_id, now + duration_seconds, now),
            )
            session_id = int(cursor.lastrowid)
            await connection.commit()
            return session_id

    async def get_active_courier_session(self, user_id: int) -> dict[str, Any] | None:
        async with self.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM courier_sessions
                WHERE user_id = ? AND is_active = 1 AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, time.time()),
            )
            return _row_dict(await cursor.fetchone())

    async def set_courier_session_active(self, session_id: int, is_active: bool) -> bool:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "UPDATE courier_sessions SET is_active = ? WHERE id = ?",
                (int(is_active), session_id),
            )
            await connection.commit()
            return cursor.rowcount > 0

    async def enable_unload_mode(self, user_id: int, duration_seconds: int = 600) -> float:
        if duration_seconds <= 0:
            raise ValueError("Длительность режима разгрузки должна быть положительной")
        now = time.time()
        until = now + duration_seconds
        async with self.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE;")
            cursor = await connection.execute(
                """
                SELECT id FROM courier_sessions
                WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.execute(
                    """
                    INSERT INTO courier_sessions
                        (user_id, expires_at, unload_mode_until, is_active, created_at)
                    VALUES (?, ?, ?, 0, ?)
                    """,
                    (user_id, now, until, now),
                )
            else:
                await connection.execute(
                    "UPDATE courier_sessions SET unload_mode_until = ? WHERE id = ?",
                    (until, row["id"]),
                )
            await connection.commit()
            return until

    async def enqueue_ipc_command(self, cmd_type: str, payload: str) -> int:
        if cmd_type not in COMMAND_TYPES:
            raise ValueError(f"Недопустимый тип IPC-команды: {cmd_type}")
        async with self.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO ipc_commands (cmd_type, payload, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (cmd_type, payload, time.time()),
            )
            await connection.commit()
            return int(cursor.lastrowid)

    async def get_pending_outbox(self, limit: int = 20) -> list[dict[str, Any]]:
        _validate_limit(limit)
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM bot_outbox WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            )
            return _rows_dict(await cursor.fetchall())

    async def mark_outbox_sent(self, outbox_id: int) -> bool:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "UPDATE bot_outbox SET status = 'sent' WHERE id = ? AND status = 'pending'",
                (outbox_id,),
            )
            await connection.commit()
            return cursor.rowcount > 0

    async def get_door_state(self) -> dict[str, Any]:
        async with self.connection() as connection:
            cursor = await connection.execute("SELECT * FROM door_state WHERE id = 1")
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Таблица door_state не инициализирована")
            return dict(row)

    async def update_door_state(self, **values: float | int) -> None:
        if not values or any(key not in DOOR_STATE_FIELDS for key in values):
            raise ValueError("Переданы пустые или недопустимые поля door_state")
        assignments = ", ".join(f"{key} = ?" for key in values)
        parameters = tuple(values.values())
        async with self.connection() as connection:
            await connection.execute(
                f"UPDATE door_state SET {assignments} WHERE id = 1", parameters
            )
            await connection.commit()

    async def add_access_log(
        self,
        method: str,
        user_id: int | None,
        details: str,
        people_entered: int = 1,
        tailgating_alert: bool = False,
        video_path: str | None = None,
    ) -> int:
        if method not in ACCESS_METHODS:
            raise ValueError(f"Недопустимый метод доступа: {method}")
        async with self.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO access_logs
                    (method, user_id, details, people_entered, tailgating_alert,
                     video_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    method,
                    user_id,
                    details,
                    people_entered,
                    int(tailgating_alert),
                    video_path,
                    time.time(),
                ),
            )
            await connection.commit()
            return int(cursor.lastrowid)

    async def get_access_log(self, log_id: int) -> dict[str, Any] | None:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM access_logs WHERE id = ?", (log_id,)
            )
            return _row_dict(await cursor.fetchone())

    async def get_recent_access_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        _validate_limit(limit)
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM access_logs ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )
            return _rows_dict(await cursor.fetchall())


class SyncDatabase(_DatabaseBase):
    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute("PRAGMA synchronous=NORMAL;")
            connection.execute("PRAGMA busy_timeout=5000;")
            yield connection
        finally:
            connection.close()

    def init(self) -> None:
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def upsert_user(
        self,
        telegram_id: int,
        username: str | None,
        full_name: str,
        role: str = "resident",
    ) -> None:
        if role not in USER_ROLES:
            raise ValueError(f"Недопустимая роль пользователя: {role}")
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO users (telegram_id, username, full_name, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name
                """,
                (telegram_id, username, full_name, role, time.time()),
            )
            connection.commit()

    def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return _row_dict(row)

    def list_users(self, limit: int = 100) -> list[dict[str, Any]]:
        _validate_limit(limit)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return _rows_dict(rows)

    def set_user_role(self, telegram_id: int, role: str) -> bool:
        if role not in USER_ROLES:
            raise ValueError(f"Недопустимая роль пользователя: {role}")
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE users SET role = ? WHERE telegram_id = ?", (role, telegram_id)
            )
            connection.commit()
            return cursor.rowcount > 0

    def get_notification_chat_ids(self) -> list[int]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT telegram_id FROM users
                WHERE role IN ('admin', 'resident')
                ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at
                """
            ).fetchall()
            return [int(row["telegram_id"]) for row in rows]

    def get_active_face_embeddings(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, person_name, embedding, is_active, created_at
                FROM face_embeddings WHERE is_active = 1 ORDER BY id
                """
            ).fetchall()
            return _rows_dict(rows)

    def list_face_embeddings(
        self, user_id: int, only_active: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM face_embeddings WHERE user_id = ?"
        if only_active:
            query += " AND is_active = 1"
        query += " ORDER BY created_at DESC"
        with self.connection() as connection:
            rows = connection.execute(query, (user_id,)).fetchall()
            return _rows_dict(rows)

    def set_face_embedding_active(
        self, face_id: int, user_id: int, is_active: bool
    ) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE face_embeddings SET is_active = ?
                WHERE id = ? AND user_id = ?
                """,
                (int(is_active), face_id, user_id),
            )
            connection.commit()
            return cursor.rowcount > 0

    def get_pending_face_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        _validate_limit(limit)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM face_tasks WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return _rows_dict(rows)

    def get_face_task(self, task_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM face_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return _row_dict(row)

    def complete_face_task(
        self, task_id: int, embedding: np.ndarray | bytes, result_msg: str
    ) -> int:
        serialized = _serialize_embedding(embedding)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE;")
            task = connection.execute(
                "SELECT * FROM face_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                connection.rollback()
                raise ValueError(f"Задача регистрации лица {task_id} не найдена")
            if task["status"] != "pending":
                connection.rollback()
                raise ValueError(f"Задача регистрации лица {task_id} уже обработана")
            cursor = connection.execute(
                """
                INSERT INTO face_embeddings
                    (user_id, person_name, embedding, is_active, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (task["user_id"], task["person_name"], serialized, time.time()),
            )
            connection.execute(
                """
                UPDATE face_tasks SET status = 'done', result_msg = ?
                WHERE id = ? AND status = 'pending'
                """,
                (result_msg, task_id),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def fail_face_task(self, task_id: int, result_msg: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE face_tasks SET status = 'error', result_msg = ?
                WHERE id = ? AND status = 'pending'
                """,
                (result_msg, task_id),
            )
            connection.commit()
            return cursor.rowcount > 0

    def create_qr_token(
        self,
        token: str,
        creator_id: int,
        label: str,
        qr_type: str,
        max_uses: int = 0,
        expires_at: float | None = None,
    ) -> int:
        if qr_type not in QR_TYPES:
            raise ValueError(f"Недопустимый тип QR: {qr_type}")
        if max_uses < 0:
            raise ValueError("max_uses не может быть отрицательным")
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO qr_tokens
                    (token, creator_id, label, qr_type, max_uses, used_count,
                     expires_at, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, 1, ?)
                """,
                (token, creator_id, label, qr_type, max_uses, expires_at, time.time()),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get_qr_token(self, token: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM qr_tokens WHERE token = ?", (token,)
            ).fetchone()
            return _row_dict(row)

    def list_qr_tokens(
        self, creator_id: int | None = None, include_inactive: bool = True
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if creator_id is not None:
            conditions.append("creator_id = ?")
            parameters.append(creator_id)
        if not include_inactive:
            conditions.append("is_active = 1")
        query = "SELECT * FROM qr_tokens"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        with self.connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
            return _rows_dict(rows)

    def set_qr_token_active(
        self, qr_id: int, is_active: bool, creator_id: int | None = None
    ) -> bool:
        query = "UPDATE qr_tokens SET is_active = ? WHERE id = ?"
        parameters: list[Any] = [int(is_active), qr_id]
        if creator_id is not None:
            query += " AND creator_id = ?"
            parameters.append(creator_id)
        with self.connection() as connection:
            cursor = connection.execute(query, tuple(parameters))
            connection.commit()
            return cursor.rowcount > 0

    def consume_qr_token(self, token: str) -> QRConsumeResult:
        now = time.time()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE;")
            row = connection.execute(
                "SELECT * FROM qr_tokens WHERE token = ?", (token,)
            ).fetchone()
            reason = _qr_rejection_reason(row, now)
            if reason is not None:
                connection.commit()
                return QRConsumeResult(False, reason, _row_dict(row))
            connection.execute(
                "UPDATE qr_tokens SET used_count = used_count + 1 WHERE id = ?",
                (row["id"],),
            )
            result = dict(row)
            result["used_count"] += 1
            connection.commit()
            return QRConsumeResult(True, "ok", result)

    def create_courier_session(self, user_id: int, duration_seconds: int) -> int:
        if duration_seconds <= 0:
            raise ValueError("Длительность курьерской сессии должна быть положительной")
        now = time.time()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE;")
            connection.execute(
                "UPDATE courier_sessions SET is_active = 0, unload_mode_until = 0 WHERE user_id = ?",
                (user_id,),
            )
            cursor = connection.execute(
                """
                INSERT INTO courier_sessions
                    (user_id, expires_at, unload_mode_until, is_active, created_at)
                VALUES (?, ?, 0, 1, ?)
                """,
                (user_id, now + duration_seconds, now),
            )
            session_id = int(cursor.lastrowid)
            connection.commit()
            return session_id

    def get_active_courier_session(self, user_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM courier_sessions
                WHERE user_id = ? AND is_active = 1 AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, time.time()),
            ).fetchone()
            return _row_dict(row)

    def consume_active_courier_session(self) -> dict[str, Any] | None:
        now = time.time()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE;")
            row = connection.execute(
                """
                SELECT * FROM courier_sessions
                WHERE is_active = 1 AND expires_at > ?
                ORDER BY created_at LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                """
                UPDATE courier_sessions SET is_active = 0
                WHERE id = ? AND is_active = 1 AND expires_at > ?
                """,
                (row["id"], now),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError("Не удалось атомарно закрыть курьерскую сессию")
            connection.commit()
            return dict(row)

    def set_courier_session_active(self, session_id: int, is_active: bool) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE courier_sessions SET is_active = ? WHERE id = ?",
                (int(is_active), session_id),
            )
            connection.commit()
            return cursor.rowcount > 0

    def enable_unload_mode(self, user_id: int, duration_seconds: int = 600) -> float:
        if duration_seconds <= 0:
            raise ValueError("Длительность режима разгрузки должна быть положительной")
        now = time.time()
        until = now + duration_seconds
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE;")
            row = connection.execute(
                """
                SELECT id FROM courier_sessions
                WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO courier_sessions
                        (user_id, expires_at, unload_mode_until, is_active, created_at)
                    VALUES (?, ?, ?, 0, ?)
                    """,
                    (user_id, now, until, now),
                )
            else:
                connection.execute(
                    "UPDATE courier_sessions SET unload_mode_until = ? WHERE id = ?",
                    (until, row["id"]),
                )
            connection.commit()
            return until

    def get_active_unload_session(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM courier_sessions
                WHERE unload_mode_until > ?
                ORDER BY unload_mode_until DESC LIMIT 1
                """,
                (time.time(),),
            ).fetchone()
            return _row_dict(row)

    def enqueue_ipc_command(self, cmd_type: str, payload: str) -> int:
        if cmd_type not in COMMAND_TYPES:
            raise ValueError(f"Недопустимый тип IPC-команды: {cmd_type}")
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ipc_commands (cmd_type, payload, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (cmd_type, payload, time.time()),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get_pending_ipc_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        _validate_limit(limit)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ipc_commands WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return _rows_dict(rows)

    def mark_ipc_command_processed(self, command_id: int) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ipc_commands SET status = 'processed'
                WHERE id = ? AND status = 'pending'
                """,
                (command_id,),
            )
            connection.commit()
            return cursor.rowcount > 0

    def enqueue_outbox(
        self,
        chat_id: int,
        media_type: str,
        caption: str,
        file_path: str | None = None,
        keyboard_json: str | None = None,
    ) -> int:
        if media_type not in MEDIA_TYPES:
            raise ValueError(f"Недопустимый тип сообщения outbox: {media_type}")
        if media_type != "text" and not file_path:
            raise ValueError("Для фото и видео необходимо указать file_path")
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO bot_outbox
                    (chat_id, media_type, file_path, caption, keyboard_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (chat_id, media_type, file_path, caption, keyboard_json, time.time()),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get_pending_outbox(self, limit: int = 20) -> list[dict[str, Any]]:
        _validate_limit(limit)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM bot_outbox WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return _rows_dict(rows)

    def mark_outbox_sent(self, outbox_id: int) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE bot_outbox SET status = 'sent' WHERE id = ? AND status = 'pending'",
                (outbox_id,),
            )
            connection.commit()
            return cursor.rowcount > 0

    def get_door_state(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM door_state WHERE id = 1").fetchone()
            if row is None:
                raise RuntimeError("Таблица door_state не инициализирована")
            return dict(row)

    def update_door_state(self, **values: float | int) -> None:
        if not values or any(key not in DOOR_STATE_FIELDS for key in values):
            raise ValueError("Переданы пустые или недопустимые поля door_state")
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE door_state SET {assignments} WHERE id = 1", tuple(values.values())
            )
            connection.commit()

    def record_motion(self, timestamp: float | None = None) -> None:
        self.update_door_state(last_motion_ts=time.time() if timestamp is None else timestamp)

    def record_person(
        self, people_count: int = 1, timestamp: float | None = None
    ) -> None:
        if people_count < 0:
            raise ValueError("people_count не может быть отрицательным")
        self.update_door_state(
            last_person_ts=time.time() if timestamp is None else timestamp,
            people_count=people_count,
        )

    def record_unlock(self, timestamp: float | None = None) -> None:
        self.update_door_state(last_unlock_ts=time.time() if timestamp is None else timestamp)

    def add_access_log(
        self,
        method: str,
        user_id: int | None,
        details: str,
        people_entered: int = 1,
        tailgating_alert: bool = False,
        video_path: str | None = None,
    ) -> int:
        if method not in ACCESS_METHODS:
            raise ValueError(f"Недопустимый метод доступа: {method}")
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO access_logs
                    (method, user_id, details, people_entered, tailgating_alert,
                     video_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    method,
                    user_id,
                    details,
                    people_entered,
                    int(tailgating_alert),
                    video_path,
                    time.time(),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get_access_log(self, log_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_logs WHERE id = ?", (log_id,)
            ).fetchone()
            return _row_dict(row)

    def get_recent_access_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        _validate_limit(limit)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM access_logs ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return _rows_dict(rows)
