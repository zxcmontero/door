from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import Settings, get_settings
from database import Database
from qr_generator import build_deep_link, generate_token, render_qr_png


logger = logging.getLogger(__name__)
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
QR_REASON_MESSAGES = {
    "not_found": "QR-код не найден",
    "inactive": "QR-код отозван",
    "expired": "Срок действия QR-кода истек",
    "exhausted": "Лимит использований QR-кода исчерпан",
}


class QRStates(StatesGroup):
    name = State()
    kind = State()
    max_uses = State()
    ttl = State()


class FaceStates(StatesGroup):
    name = State()
    photo = State()


class AdminStates(StatesGroup):
    add_user = State()
    block_user = State()


class DoorBot:
    def __init__(self, settings: Settings) -> None:
        if not settings.bot_username:
            raise ValueError("Для QR и курьерских deep links необходимо задать BOT_USERNAME")
        self.settings = settings
        self.db = Database(settings.database_path)
        self.bot = Bot(token=settings.bot_token)
        self.router = Router()
        self._retry_after: dict[int, float] = {}
        self._retry_attempts: dict[int, int] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        router = self.router
        router.message.register(self.on_start, CommandStart())
        router.message.register(self.on_cancel, Command("cancel"))
        router.callback_query.register(self.on_fsm_cancel, F.data == "fsm_cancel")

        router.callback_query.register(self.on_inline_unlock, F.data == "unlock_door")
        router.callback_query.register(self.on_inline_unlock_unload, F.data == "unlock_unload:10")
        router.callback_query.register(self.on_courier_unload, F.data == "courier_unload:10")
        router.callback_query.register(self.on_deeplink_retry, F.data.startswith("deeplink_retry:"))
        router.callback_query.register(self.on_courier_start, F.data.startswith("courier_start:"))

        router.callback_query.register(self.on_qr_create, F.data == "qr_create")
        router.callback_query.register(self.on_qr_kind, F.data.startswith("qr_kind:"), QRStates.kind)
        router.callback_query.register(self.on_qr_view, F.data.startswith("qr_view:"))
        router.callback_query.register(self.on_qr_revoke, F.data.startswith("qr_revoke:"))
        router.callback_query.register(self.on_face_add, F.data == "face_add")
        router.callback_query.register(self.on_face_toggle, F.data.startswith("face_toggle:"))

        router.callback_query.register(self.on_admin_add, F.data == "admin_add")
        router.callback_query.register(self.on_admin_block, F.data == "admin_block")
        router.callback_query.register(self.on_admin_logs, F.data == "admin_logs")
        router.callback_query.register(self.on_admin_qrs, F.data == "admin_qrs")
        router.callback_query.register(self.on_admin_qr_revoke, F.data.startswith("admin_qr_revoke:"))
        router.callback_query.register(self.on_log_video, F.data.startswith("log_video:"))

        router.message.register(self.on_qr_name, QRStates.name, F.text)
        router.message.register(self.on_qr_max_uses, QRStates.max_uses, F.text)
        router.message.register(self.on_qr_ttl, QRStates.ttl, F.text)
        router.message.register(self.on_face_name, FaceStates.name, F.text)
        router.message.register(self.on_face_photo, FaceStates.photo, F.photo)
        router.message.register(self.on_face_photo_invalid, FaceStates.photo)
        router.message.register(self.on_admin_add_id, AdminStates.add_user, F.text)
        router.message.register(self.on_admin_block_id, AdminStates.block_user, F.text)

        router.message.register(self.on_unlock_text, F.text == "🔓 Открыть дверь")
        router.message.register(self.on_courier_menu, F.text == "🍕 Жду курьера")
        router.message.register(self.on_live_text, F.text == "📹 Что у двери?")
        router.message.register(self.on_qr_menu, F.text == "🎟 Мои QR-коды")
        router.message.register(self.on_face_menu, F.text == "👤 Мои лица")
        router.message.register(self.on_admin_menu, F.text == "⚙️ Админ-панель")
        router.message.register(self.on_fallback, F.text)

    async def _get_role(self, telegram_id: int) -> str | None:
        user = await self.db.get_user(telegram_id)
        if telegram_id in self.settings.admin_ids:
            if user is None:
                await self.db.upsert_user(telegram_id, None, f"Администратор {telegram_id}", "admin")
            elif user["role"] != "admin":
                await self.db.set_user_role(telegram_id, "admin")
            return "admin"
        return str(user["role"]) if user is not None else None

    async def _touch_user(self, telegram_id: int, username: str | None, full_name: str) -> str | None:
        role = await self._get_role(telegram_id)
        if role is not None:
            await self.db.upsert_user(telegram_id, username, full_name, role)
        return role

    async def _require_message(self, message: Message, admin: bool = False) -> bool:
        if message.from_user is None:
            return False
        role = await self._get_role(message.from_user.id)
        allowed = role == "admin" or (role == "resident" and not admin)
        if not allowed:
            await message.answer("⛔ Доступ запрещен. Обратитесь к администратору.")
        return allowed

    async def _require_callback(self, callback: CallbackQuery, admin: bool = False) -> bool:
        role = await self._get_role(callback.from_user.id)
        allowed = role == "admin" or (role == "resident" and not admin)
        if not allowed:
            await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return allowed

    @staticmethod
    def _callback_message(callback: CallbackQuery) -> Message | None:
        return callback.message if isinstance(callback.message, Message) else None

    @staticmethod
    def _main_keyboard(role: str) -> Any:
        builder = ReplyKeyboardBuilder()
        builder.button(text="🔓 Открыть дверь")
        builder.button(text="🍕 Жду курьера")
        builder.button(text="📹 Что у двери?")
        builder.button(text="🎟 Мои QR-коды")
        builder.button(text="👤 Мои лица")
        if role == "admin":
            builder.button(text="⚙️ Админ-панель")
        builder.adjust(1, 2, 2, 1)
        return builder.as_markup(resize_keyboard=True)

    @staticmethod
    def _cancel_keyboard() -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Отмена", callback_data="fsm_cancel")
        return builder.as_markup()

    async def on_start(self, message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
        if message.from_user is None:
            return
        role = await self._touch_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
        )
        payload = (command.args or "").strip()
        if payload:
            match = re.fullmatch(r"(qr|cour)_([A-Za-z0-9]{8})", payload)
            if match:
                await self._handle_deep_link(message, match.group(2), match.group(1), message.from_user.id)
                return
            await message.answer("Некорректная ссылка доступа.")
            return
        if role not in {"admin", "resident"}:
            await message.answer("Ваш Telegram ID не зарегистрирован. Передайте его администратору.")
            return
        await message.answer("Управление домофоном", reply_markup=self._main_keyboard(role))

    async def on_cancel(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user is None:
            return
        role = await self._get_role(message.from_user.id)
        if role in {"admin", "resident"}:
            await message.answer("Действие отменено.", reply_markup=self._main_keyboard(role))

    async def on_fsm_cancel(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        message = self._callback_message(callback)
        if message is not None:
            await message.answer("Действие отменено.")

    async def on_unlock_text(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        await self._enqueue_unlock(message.from_user.id, message.chat.id, "button", "Команда из Telegram")
        await message.answer("Команда открытия отправлена.")

    async def on_inline_unlock(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        await self._enqueue_unlock(callback.from_user.id, callback.from_user.id, "button", "Inline-кнопка Telegram")
        await callback.answer("Команда отправлена")

    async def on_inline_unlock_unload(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        try:
            await self._enqueue_unlock(
                callback.from_user.id,
                callback.from_user.id,
                "button",
                "Открытие и разгрузка из Telegram",
            )
            await self._enqueue_unload(callback.from_user.id)
        except Exception as exc:
            logger.exception("Не удалось поставить команды открытия и разгрузки: %s", exc)
            await callback.answer("Ошибка создания команды", show_alert=True)
            return
        await callback.answer("Команды отправлены")

    async def on_courier_unload(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        await self._enqueue_unload(callback.from_user.id)
        await callback.answer("Режим разгрузки запрошен")

    async def _enqueue_unlock(self, user_id: int, chat_id: int, method: str, details: str) -> int:
        payload = {
            "user_id": user_id,
            "chat_id": chat_id,
            "method": method,
            "details": details,
        }
        return await self.db.enqueue_ipc_command("unlock_door", json.dumps(payload, ensure_ascii=False))

    async def _enqueue_unload(self, user_id: int) -> int:
        payload = {"user_id": user_id, "chat_id": user_id}
        return await self.db.enqueue_ipc_command(
            "enable_unload_mode", json.dumps(payload, ensure_ascii=False)
        )

    async def on_live_text(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        payload = {"chat_id": message.chat.id}
        await self.db.enqueue_ipc_command("capture_live_clip", json.dumps(payload))
        await message.answer("Записываю свежий 4-секундный клип.")

    async def on_courier_menu(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        active = await self.db.get_active_courier_session(message.from_user.id)
        if active is not None:
            remaining = max(1, int((float(active["expires_at"]) - time.time()) / 60))
            await message.answer(f"Режим ожидания курьера уже активен, осталось около {remaining} мин.")
            return
        builder = InlineKeyboardBuilder()
        for minutes in (15, 30, 60):
            builder.button(text=f"{minutes} минут", callback_data=f"courier_start:{minutes}")
        builder.adjust(3)
        await message.answer("На какое время включить ожидание курьера?", reply_markup=builder.as_markup())

    async def on_courier_start(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        try:
            minutes = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректный таймер", show_alert=True)
            return
        if minutes not in {15, 30, 60}:
            await callback.answer("Некорректный таймер", show_alert=True)
            return
        user_id = callback.from_user.id
        if await self.db.get_active_courier_session(user_id) is not None:
            await callback.answer("Ожидание курьера уже активно", show_alert=True)
            return
        await self.db.create_courier_session(user_id, minutes * 60)
        try:
            _, token = await self._create_unique_qr(
                creator_id=user_id,
                label=f"Курьер {time.strftime('%d.%m.%Y %H:%M')}",
                qr_type="courier",
                max_uses=1,
                expires_at=time.time() + minutes * 60,
            )
        except Exception as exc:
            logger.exception("Не удалось создать курьерский QR: %s", exc)
            await callback.answer("Сессия создана, но ссылка недоступна", show_alert=True)
            return
        link = build_deep_link(self.settings.bot_username, token, "cour")
        comment = (
            "Домофон откроется сам при нажатии кнопки вызова на панели, "
            f"либо нажмите у двери: {link}"
        )
        await message.answer_photo(
            BufferedInputFile(render_qr_png(token), filename=f"courier_{token}.png"),
            caption=f"🍕 Ожидание курьера включено на {minutes} минут.",
        )
        await message.answer(comment)
        await callback.answer("Режим включен")

    async def _handle_deep_link(
        self, message: Message, token: str, kind: str, initiator_id: int
    ) -> None:
        if await self._get_role(initiator_id) == "blocked":
            await message.answer("⛔ Доступ запрещен.")
            return
        if not TOKEN_PATTERN.fullmatch(token) or kind not in {"qr", "cour"}:
            await message.answer("Некорректная ссылка доступа.")
            return
        qr = await self.db.get_qr_token(token)
        reason = self._qr_rejection_reason(qr)
        if reason is not None:
            await message.answer(f"⚠️ {QR_REASON_MESSAGES[reason]}.")
            return
        if qr is None:
            return
        courier_session: dict[str, Any] | None = None
        if kind == "cour":
            if qr["qr_type"] != "courier":
                await message.answer("⚠️ Эта ссылка не является курьерской.")
                return
            courier_session = await self.db.get_active_courier_session(int(qr["creator_id"]))
            if courier_session is None:
                await message.answer("⚠️ Ожидание курьера уже завершено.")
                return
        state = await self.db.get_door_state()
        now = time.time()
        present = (
            now - float(state["last_person_ts"]) <= 5.0
            or now - float(state["last_motion_ts"]) <= 4.0
        )
        if not present:
            builder = InlineKeyboardBuilder()
            builder.button(
                text="🔄 Я у камеры, повторить",
                callback_data=f"deeplink_retry:{kind}:{token}",
            )
            await message.answer(
                "⚠️ Подойдите вплотную к камере домофона и нажмите кнопку ниже",
                reply_markup=builder.as_markup(),
            )
            return
        consumed = await self.db.consume_qr_token(token)
        if not consumed.allowed or consumed.token is None:
            await message.answer(
                f"⚠️ {QR_REASON_MESSAGES.get(consumed.reason, 'QR-код недействителен')}."
            )
            return
        qr = consumed.token
        payload: dict[str, Any] = {
            "user_id": int(qr["creator_id"]),
            "chat_id": message.chat.id,
            "method": "qr_deeplink",
            "details": f"QR «{qr['label']}», тип {qr['qr_type']}",
            "qr_id": int(qr["id"]),
            "expected_used_count": int(qr["used_count"]),
        }
        if courier_session is not None:
            payload["courier_session_id"] = int(courier_session["id"])
        try:
            await self.db.enqueue_ipc_command(
                "unlock_door", json.dumps(payload, ensure_ascii=False)
            )
        except Exception as exc:
            await self.db.refund_qr_use(int(qr["id"]), int(qr["used_count"]))
            logger.exception("Не удалось создать IPC для QR deep link: %s", exc)
            await message.answer("⚠️ Не удалось передать команду. Использование QR возвращено.")
            return
        await message.answer("✅ Проверка пройдена. Команда открытия отправлена.")

    async def on_deeplink_retry(self, callback: CallbackQuery) -> None:
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        try:
            _, kind, token = callback.data.split(":", 2)
        except ValueError:
            await callback.answer("Некорректная ссылка", show_alert=True)
            return
        await self._handle_deep_link(message, token, kind, callback.from_user.id)
        await callback.answer()

    @staticmethod
    def _qr_rejection_reason(qr: dict[str, Any] | None) -> str | None:
        if qr is None:
            return "not_found"
        if not qr["is_active"]:
            return "inactive"
        if qr["expires_at"] is not None and float(qr["expires_at"]) <= time.time():
            return "expired"
        if int(qr["max_uses"]) > 0 and int(qr["used_count"]) >= int(qr["max_uses"]):
            return "exhausted"
        return None

    async def _create_unique_qr(
        self,
        creator_id: int,
        label: str,
        qr_type: str,
        max_uses: int,
        expires_at: float | None,
    ) -> tuple[int, str]:
        for _ in range(10):
            token = generate_token()
            try:
                qr_id = await self.db.create_qr_token(
                    token, creator_id, label, qr_type, max_uses, expires_at
                )
            except sqlite3.IntegrityError:
                continue
            return qr_id, token
        raise RuntimeError("Не удалось создать уникальный QR-токен")

    async def on_qr_menu(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        tokens = await self.db.list_qr_tokens(message.from_user.id)
        builder = InlineKeyboardBuilder()
        for qr in tokens[:20]:
            status = "✅" if qr["is_active"] else "🚫"
            builder.button(
                text=f"{status} {qr['label']}",
                callback_data=f"qr_view:{qr['id']}",
            )
        builder.button(text="➕ Создать QR", callback_data="qr_create")
        builder.adjust(1)
        await message.answer(
            "Ваши QR-коды:" if tokens else "У вас пока нет QR-кодов.",
            reply_markup=builder.as_markup(),
        )

    async def on_qr_create(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        await state.clear()
        await state.set_state(QRStates.name)
        message = self._callback_message(callback)
        if message is not None:
            await message.answer("Введите название пропуска (1–64 символа):", reply_markup=self._cancel_keyboard())
        await callback.answer()

    async def on_qr_name(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message):
            return
        name = (message.text or "").strip()
        if not 1 <= len(name) <= 64:
            await message.answer("Название должно содержать от 1 до 64 символов.")
            return
        await state.update_data(name=name)
        await state.set_state(QRStates.kind)
        builder = InlineKeyboardBuilder()
        builder.button(text="Постоянный", callback_data="qr_kind:permanent")
        builder.button(text="Временный", callback_data="qr_kind:temporary")
        builder.button(text="Отмена", callback_data="fsm_cancel")
        builder.adjust(2, 1)
        await message.answer("Выберите тип QR:", reply_markup=builder.as_markup())

    async def on_qr_kind(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        kind = callback.data.rsplit(":", 1)[1]
        if kind not in {"permanent", "temporary"}:
            await callback.answer("Некорректный тип", show_alert=True)
            return
        await state.update_data(kind=kind)
        await state.set_state(QRStates.max_uses)
        message = self._callback_message(callback)
        if message is not None:
            await message.answer(
                "Введите максимальное число использований: 0 — безлимит, иначе целое число от 1 до 10000.",
                reply_markup=self._cancel_keyboard(),
            )
        await callback.answer()

    async def on_qr_max_uses(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message):
            return
        try:
            max_uses = int((message.text or "").strip())
        except ValueError:
            max_uses = -1
        if not 0 <= max_uses <= 10000:
            await message.answer("Введите 0 или целое число от 1 до 10000.")
            return
        await state.update_data(max_uses=max_uses)
        data = await state.get_data()
        if data.get("kind") == "temporary":
            await state.set_state(QRStates.ttl)
            await message.answer(
                "Введите срок действия в минутах (1–10080):",
                reply_markup=self._cancel_keyboard(),
            )
            return
        await self._finish_qr_creation(message, state)

    async def on_qr_ttl(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message):
            return
        try:
            minutes = int((message.text or "").strip())
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= 10080:
            await message.answer("Введите целое число минут от 1 до 10080.")
            return
        await state.update_data(expires_at=time.time() + minutes * 60)
        await self._finish_qr_creation(message, state)

    async def _finish_qr_creation(self, message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        data = await state.get_data()
        kind = str(data.get("kind", ""))
        name = str(data.get("name", ""))
        if kind not in {"permanent", "temporary"} or not name or "max_uses" not in data:
            await state.clear()
            await message.answer("Данные мастера устарели. Начните создание QR заново.")
            return
        qr_id, token = await self._create_unique_qr(
            creator_id=message.from_user.id,
            label=name,
            qr_type=kind,
            max_uses=int(data["max_uses"]),
            expires_at=float(data["expires_at"]) if kind == "temporary" else None,
        )
        await state.clear()
        await self._send_qr(message, qr_id, token)

    async def _send_qr(self, message: Message, qr_id: int, token: str) -> None:
        qr = await self.db.get_qr_token(token)
        if qr is None or int(qr["id"]) != qr_id:
            raise RuntimeError("Созданный QR не найден")
        link = build_deep_link(self.settings.bot_username, token)
        uses = "безлимит" if int(qr["max_uses"]) == 0 else str(qr["max_uses"])
        expires = (
            time.strftime("%d.%m.%Y %H:%M", time.localtime(float(qr["expires_at"])))
            if qr["expires_at"] is not None
            else "бессрочный"
        )
        caption = (
            f"🎟 {qr['label']}\nТип: {qr['qr_type']}\n"
            f"Использований: {uses}\nСрок: {expires}\n\nСсылка у двери: {link}"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="🚫 Отозвать", callback_data=f"qr_revoke:{qr_id}")
        await message.answer_photo(
            BufferedInputFile(render_qr_png(token), filename=f"door_{token}.png"),
            caption=caption,
            reply_markup=builder.as_markup(),
        )

    async def on_qr_view(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        qr = await self._find_user_qr(callback.from_user.id, callback.data, "qr_view:")
        if qr is None:
            await callback.answer("QR не найден", show_alert=True)
            return
        await self._send_qr(message, int(qr["id"]), str(qr["token"]))
        await callback.answer()

    async def on_qr_revoke(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        qr = await self._find_user_qr(callback.from_user.id, callback.data, "qr_revoke:")
        if qr is None:
            await callback.answer("QR не найден", show_alert=True)
            return
        await self.db.set_qr_token_active(int(qr["id"]), False, callback.from_user.id)
        await callback.answer("QR отозван")

    async def _find_user_qr(
        self, user_id: int, callback_data: str, prefix: str
    ) -> dict[str, Any] | None:
        try:
            qr_id = int(callback_data.removeprefix(prefix))
        except ValueError:
            return None
        tokens = await self.db.list_qr_tokens(user_id)
        return next((qr for qr in tokens if int(qr["id"]) == qr_id), None)

    async def on_face_menu(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        faces = await self.db.list_face_embeddings(message.from_user.id)
        builder = InlineKeyboardBuilder()
        for face in faces[:20]:
            status = "✅" if face["is_active"] else "🚫"
            builder.button(
                text=f"{status} {face['person_name']}",
                callback_data=f"face_toggle:{face['id']}",
            )
        builder.button(text="➕ Добавить лицо", callback_data="face_add")
        builder.adjust(1)
        await message.answer(
            "Нажмите на лицо, чтобы включить или отключить его."
            if faces
            else "У вас пока нет зарегистрированных лиц.",
            reply_markup=builder.as_markup(),
        )

    async def on_face_add(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        await state.clear()
        await state.set_state(FaceStates.name)
        message = self._callback_message(callback)
        if message is not None:
            await message.answer("Введите имя человека (1–64 символа):", reply_markup=self._cancel_keyboard())
        await callback.answer()

    async def on_face_name(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message):
            return
        name = (message.text or "").strip()
        if not 1 <= len(name) <= 64:
            await message.answer("Имя должно содержать от 1 до 64 символов.")
            return
        await state.update_data(name=name)
        await state.set_state(FaceStates.photo)
        await message.answer(
            "Отправьте одну четкую фотографию лица. На ней должен быть только один человек.",
            reply_markup=self._cancel_keyboard(),
        )

    async def on_face_photo(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        data = await state.get_data()
        name = str(data.get("name", "")).strip()
        if not name:
            await state.clear()
            await message.answer("Данные мастера устарели. Начните регистрацию заново.")
            return
        path = self.settings.face_tasks_dir / f"{message.from_user.id}_{uuid.uuid4().hex}.jpg"
        try:
            await message.bot.download(message.photo[-1], destination=path)
            task_id = await self.db.create_face_task(message.from_user.id, name, str(path))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        await state.clear()
        await message.answer(f"Фотография принята. Задача регистрации №{task_id} передана воркеру.")

    async def on_face_photo_invalid(self, message: Message) -> None:
        if await self._require_message(message):
            await message.answer("Нужно отправить фотографию, а не текст или другой файл.")

    async def on_face_toggle(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        try:
            face_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный ID лица", show_alert=True)
            return
        faces = await self.db.list_face_embeddings(callback.from_user.id)
        face = next((item for item in faces if int(item["id"]) == face_id), None)
        if face is None:
            await callback.answer("Лицо не найдено", show_alert=True)
            return
        active = not bool(face["is_active"])
        await self.db.set_face_embedding_active(face_id, callback.from_user.id, active)
        await self.db.enqueue_ipc_command("reload_faces", "{}")
        await callback.answer("Лицо включено" if active else "Лицо отключено")

    async def on_admin_menu(self, message: Message) -> None:
        if not await self._require_message(message, admin=True):
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Добавить/разблокировать", callback_data="admin_add")
        builder.button(text="🚫 Заблокировать", callback_data="admin_block")
        builder.button(text="📜 Последние 10 входов", callback_data="admin_logs")
        builder.button(text="🎟 Все QR", callback_data="admin_qrs")
        builder.adjust(1)
        await message.answer("Админ-панель", reply_markup=builder.as_markup())

    async def on_admin_add(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        await state.clear()
        await state.set_state(AdminStates.add_user)
        message = self._callback_message(callback)
        if message is not None:
            await message.answer("Введите Telegram ID пользователя:", reply_markup=self._cancel_keyboard())
        await callback.answer()

    async def on_admin_add_id(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message, admin=True):
            return
        telegram_id = self._parse_telegram_id(message.text or "")
        if telegram_id is None:
            await message.answer("Telegram ID должен быть положительным целым числом.")
            return
        existing = await self.db.get_user(telegram_id)
        role = "admin" if existing is not None and existing["role"] == "admin" else "resident"
        await self.db.upsert_user(
            telegram_id,
            existing["username"] if existing is not None else None,
            existing["full_name"] if existing is not None else f"Пользователь {telegram_id}",
            role,
        )
        if existing is not None and existing["role"] == "blocked":
            await self.db.set_user_role(telegram_id, "resident")
        await state.clear()
        await message.answer(f"Пользователь {telegram_id} добавлен или разблокирован как resident.")

    async def on_admin_block(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        await state.clear()
        await state.set_state(AdminStates.block_user)
        message = self._callback_message(callback)
        if message is not None:
            await message.answer("Введите Telegram ID для блокировки:", reply_markup=self._cancel_keyboard())
        await callback.answer()

    async def on_admin_block_id(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message, admin=True) or message.from_user is None:
            return
        telegram_id = self._parse_telegram_id(message.text or "")
        if telegram_id is None:
            await message.answer("Telegram ID должен быть положительным целым числом.")
            return
        if telegram_id == message.from_user.id:
            await message.answer("Нельзя заблокировать собственную учетную запись.")
            return
        if telegram_id in self.settings.admin_ids:
            await message.answer("Нельзя заблокировать администратора из ADMIN_IDS.")
            return
        async with self.db.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "UPDATE users SET role = 'blocked' WHERE telegram_id = ?", (telegram_id,)
            )
            if cursor.rowcount == 0:
                await connection.rollback()
                await message.answer("Пользователь не найден.")
                return
            await connection.execute(
                "UPDATE face_embeddings SET is_active = 0 WHERE user_id = ?", (telegram_id,)
            )
            await connection.execute(
                "UPDATE qr_tokens SET is_active = 0 WHERE creator_id = ?", (telegram_id,)
            )
            await connection.execute(
                "UPDATE courier_sessions SET is_active = 0, unload_mode_until = 0 WHERE user_id = ?",
                (telegram_id,),
            )
            await connection.commit()
        await self.db.enqueue_ipc_command("reload_faces", "{}")
        await state.clear()
        await message.answer(
            f"Пользователь {telegram_id} заблокирован; его лица, QR и курьерские сессии отключены."
        )

    @staticmethod
    def _parse_telegram_id(value: str) -> int | None:
        try:
            telegram_id = int(value.strip())
        except ValueError:
            return None
        return telegram_id if telegram_id > 0 else None

    async def on_admin_logs(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        logs = await self.db.get_recent_access_logs(10)
        if not logs:
            await message.answer("Журнал входов пуст.")
            await callback.answer()
            return
        lines = []
        keyboard = InlineKeyboardBuilder()
        for log in logs:
            created = time.strftime("%d.%m %H:%M:%S", time.localtime(float(log["created_at"])))
            warning = " ⚠️" if log["tailgating_alert"] else ""
            lines.append(
                f"#{log['id']} {created} — {log['method']}, user={log['user_id']}, "
                f"people={log['people_entered']}{warning}\n{log['details'] or ''}"
            )
            if log["video_path"]:
                keyboard.button(text=f"Видео #{log['id']}", callback_data=f"log_video:{log['id']}")
        keyboard.adjust(2)
        await message.answer("\n\n".join(lines), reply_markup=keyboard.as_markup())
        await callback.answer()

    async def on_log_video(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        try:
            log_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный ID записи", show_alert=True)
            return
        log = await self.db.get_access_log(log_id)
        if log is None or not log["video_path"]:
            await callback.answer("Видео не найдено", show_alert=True)
            return
        path = Path(str(log["video_path"]))
        if not path.is_file():
            await callback.answer("Файл видео был удален", show_alert=True)
            return
        await message.answer_video(
            FSInputFile(path),
            caption=f"Вход #{log_id}: {log['details'] or log['method']}",
        )
        await callback.answer()

    async def on_admin_qrs(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        tokens = await self.db.list_qr_tokens()
        builder = InlineKeyboardBuilder()
        active_count = 0
        for qr in tokens:
            if not qr["is_active"]:
                continue
            active_count += 1
            builder.button(
                text=f"🚫 #{qr['id']} {qr['label']} ({qr['creator_id']})",
                callback_data=f"admin_qr_revoke:{qr['id']}",
            )
            if active_count >= 20:
                break
        if active_count == 0:
            await message.answer("Активных QR-кодов нет.")
        else:
            builder.adjust(1)
            await message.answer(
                "Нажмите на QR, чтобы отозвать его:", reply_markup=builder.as_markup()
            )
        await callback.answer()

    async def on_admin_qr_revoke(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        try:
            qr_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный ID QR", show_alert=True)
            return
        changed = await self.db.set_qr_token_active(qr_id, False)
        await callback.answer("QR отозван" if changed else "QR не найден", show_alert=not changed)

    async def on_fallback(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        role = await self._get_role(message.from_user.id)
        if role in {"admin", "resident"}:
            await message.answer("Используйте кнопки меню.", reply_markup=self._main_keyboard(role))

    async def _send_outbox_item(self, item: dict[str, Any]) -> None:
        keyboard: InlineKeyboardMarkup | None = None
        if item["keyboard_json"]:
            keyboard_data = json.loads(item["keyboard_json"])
            keyboard = InlineKeyboardMarkup.model_validate(keyboard_data)
        media_type = str(item["media_type"])
        caption = item["caption"]
        chat_id = int(item["chat_id"])
        if media_type == "text":
            if not caption:
                raise ValueError("Текстовое сообщение outbox не может быть пустым")
            await self.bot.send_message(chat_id, str(caption), reply_markup=keyboard)
            return
        file_path = Path(str(item["file_path"] or ""))
        if not file_path.is_file():
            raise FileNotFoundError(f"Медиафайл outbox не найден: {file_path}")
        media = FSInputFile(file_path)
        if media_type == "photo":
            await self.bot.send_photo(chat_id, media, caption=caption, reply_markup=keyboard)
        elif media_type == "video":
            await self.bot.send_video(chat_id, media, caption=caption, reply_markup=keyboard)
        else:
            raise ValueError(f"Неизвестный тип outbox: {media_type}")

    async def outbox_dispatcher(self) -> None:
        while True:
            try:
                items = await self.db.get_pending_outbox(20)
                now = time.monotonic()
                for item in items:
                    outbox_id = int(item["id"])
                    if now < self._retry_after.get(outbox_id, 0.0):
                        continue
                    try:
                        await self._send_outbox_item(item)
                        if await self.db.mark_outbox_sent(outbox_id):
                            await self._cleanup_media_if_ready(item["file_path"])
                        self._retry_after.pop(outbox_id, None)
                        self._retry_attempts.pop(outbox_id, None)
                    except TelegramRetryAfter as exc:
                        self._schedule_retry(outbox_id, float(exc.retry_after))
                    except (TelegramForbiddenError, TelegramBadRequest, FileNotFoundError, ValueError) as exc:
                        logger.warning("Постоянная ошибка outbox id=%s: %s", outbox_id, exc)
                        self._schedule_retry(outbox_id)
                    except Exception as exc:
                        logger.warning("Временная ошибка outbox id=%s: %s", outbox_id, exc)
                        self._schedule_retry(outbox_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Ошибка цикла outbox dispatcher: %s", exc)
            await asyncio.sleep(0.5)

    def _schedule_retry(self, outbox_id: int, delay: float | None = None) -> None:
        attempts = self._retry_attempts.get(outbox_id, 0) + 1
        self._retry_attempts[outbox_id] = attempts
        if delay is None:
            delay = min(300.0, 2.0 ** min(attempts, 8))
        self._retry_after[outbox_id] = time.monotonic() + delay

    async def _cleanup_media_if_ready(self, file_path: str | None) -> None:
        if not file_path:
            return
        async with self.db.connection() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM bot_outbox WHERE file_path = ? AND status = 'pending'",
                (file_path,),
            )
            row = await cursor.fetchone()
        if row is not None and int(row["count"]) > 0:
            return
        candidate = Path(file_path).resolve()
        temporary_root = self.settings.temp_media_dir.resolve()
        if not candidate.is_relative_to(temporary_root):
            logger.warning("Отказано в удалении файла вне TEMP_MEDIA_DIR: %s", candidate)
            return
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Не удалось удалить временный файл %s: %s", candidate, exc)

    async def _initialize_admins(self) -> None:
        for admin_id in self.settings.admin_ids:
            existing = await self.db.get_user(admin_id)
            await self.db.upsert_user(
                admin_id,
                existing["username"] if existing is not None else None,
                existing["full_name"] if existing is not None else f"Администратор {admin_id}",
                "admin",
            )
            if existing is not None and existing["role"] != "admin":
                await self.db.set_user_role(admin_id, "admin")

    async def run(self) -> None:
        await self.db.init()
        await self._initialize_admins()
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(self.router)
        outbox_task = asyncio.create_task(self.outbox_dispatcher(), name="outbox-dispatcher")
        try:
            await dispatcher.start_polling(self.bot)
        finally:
            outbox_task.cancel()
            await asyncio.gather(outbox_task, return_exceptions=True)
            await self.bot.session.close()


async def main() -> None:
    settings = get_settings()
    await DoorBot(settings).run()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Остановка по Ctrl+C")
