from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from html import escape
from pathlib import Path
from typing import Any

from aiohttp import web
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
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import Settings, get_settings
from database import Database
from qr_generator import build_deep_link, build_http_link, generate_token, render_qr_png


logger = logging.getLogger(__name__)
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
QR_REASON_MESSAGES = {
    "not_found": "QR-код не найден",
    "inactive": "QR-код отозван",
    "expired": "Срок действия QR-кода истек",
    "exhausted": "Лимит использований QR-кода исчерпан",
}
BUTTON_UNLOCK = "🔓 Открыть дверь"
BUTTON_CAMERA = "📹 Камера"
BUTTON_COURIER = "🍕 Курьер"
BUTTON_QR = "🎟 Пропуска"
BUTTON_FACES = "👥 Лица"
BUTTON_ADMIN = "🛡 Админ-панель"
HOME_TEXT = (
    "🏠 Домофон\n\n"
    "Управление дверью, камерой и доступом.\n"
    "Выберите действие на клавиатуре ниже."
)
MAX_QR_TTL_MINUTES = 365 * 1440
QR_TYPE_NAMES = {
    "permanent": "♾️ Бессрочный",
    "temporary": "⏳ Временный",
    "courier": "📦 Курьерский",
}
ACCESS_METHOD_NAMES = {
    "face": "👤 Распознавание лица",
    "qr_camera": "🎟 QR перед камерой",
    "qr_deeplink": "🔗 QR по ссылке",
    "button": "🔘 Кнопка в Telegram",
    "courier_auto": "📦 Автоматический вход курьера",
    "courier_barcode": "📊 Штрихкод доставки",
}


class QRStates(StatesGroup):
    name = State()
    kind = State()
    max_uses = State()
    ttl = State()
    ttl_value = State()


class FaceStates(StatesGroup):
    name = State()
    photo = State()


class AdminStates(StatesGroup):
    add_user = State()
    add_role = State()
    block_user = State()


class DoorBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot_username = settings.bot_username
        self.db = Database(settings.database_path)
        self.bot = Bot(token=settings.bot_token)
        self.router = Router()
        self._retry_after: dict[int, float] = {}
        self._retry_attempts: dict[int, int] = {}
        self._http_runner: web.AppRunner | None = None
        self._register_handlers()

    def _register_handlers(self) -> None:
        router = self.router
        router.message.register(self.on_start, CommandStart())
        router.message.register(self.on_cancel, Command("cancel"))
        router.message.register(self.on_menu, Command("menu"))
        router.callback_query.register(self.on_fsm_cancel, F.data == "fsm_cancel")
        router.callback_query.register(self.on_nav_main, F.data == "nav:main")
        router.callback_query.register(self.on_nav_qr, F.data == "nav:qr")
        router.callback_query.register(self.on_nav_faces, F.data == "nav:faces")
        router.callback_query.register(self.on_nav_admin, F.data == "nav:admin")

        router.callback_query.register(self.on_inline_unlock, F.data == "unlock_door")
        router.callback_query.register(self.on_inline_unlock_unload, F.data == "unlock_unload:10")
        router.callback_query.register(self.on_courier_unload, F.data == "courier_unload:10")
        router.callback_query.register(self.on_deeplink_retry, F.data.startswith("deeplink_retry:"))
        router.callback_query.register(self.on_courier_start, F.data.startswith("courier_start:"))
        router.callback_query.register(self.on_courier_resend, F.data == "courier_resend")

        router.callback_query.register(self.on_qr_create, F.data == "qr_create")
        router.callback_query.register(self.on_qr_kind, F.data.startswith("qr_kind:"), QRStates.kind)
        router.callback_query.register(self.on_qr_limit, F.data.startswith("qr_limit:"), QRStates.max_uses)
        router.callback_query.register(self.on_qr_ttl_preset, F.data.startswith("qr_ttl:"), QRStates.ttl)
        router.callback_query.register(self.on_qr_ttl_unit, F.data.startswith("qr_ttl_unit:"), QRStates.ttl)
        router.callback_query.register(self.on_qr_ttl_finish, F.data == "qr_ttl_finish", QRStates.ttl)
        router.callback_query.register(self.on_qr_ttl_reset, F.data == "qr_ttl_reset", QRStates.ttl)
        router.callback_query.register(self.on_qr_ttl_back, F.data == "qr_ttl_back", QRStates.ttl_value)
        router.callback_query.register(self.on_qr_view, F.data.startswith("qr_view:"))
        router.callback_query.register(self.on_qr_revoke, F.data.startswith("qr_revoke:"))
        router.callback_query.register(self.on_face_add, F.data == "face_add")
        router.callback_query.register(self.on_face_toggle, F.data.startswith("face_toggle:"))

        router.callback_query.register(self.on_admin_add, F.data == "admin_add")
        router.callback_query.register(self.on_admin_add_role, F.data.startswith("admin_add_role:"), AdminStates.add_role)
        router.callback_query.register(self.on_admin_block, F.data == "admin_block")
        router.callback_query.register(self.on_admin_logs, F.data == "admin_logs")
        router.callback_query.register(self.on_admin_qrs, F.data == "admin_qrs")
        router.callback_query.register(self.on_admin_users, F.data == "admin_users")
        router.callback_query.register(self.on_user_view, F.data.startswith("user_view:"))
        router.callback_query.register(self.on_admin_user_role, F.data.startswith("admin_user_role:"))
        router.callback_query.register(self.on_admin_user_block, F.data.startswith("admin_user_block:"))
        router.callback_query.register(self.on_admin_user_restore, F.data.startswith("admin_user_restore:"))
        router.callback_query.register(self.on_admin_faces, F.data.startswith("admin_faces:"))
        router.callback_query.register(self.on_admin_face_add, F.data.startswith("admin_face_add:"))
        router.callback_query.register(self.on_admin_face_toggle, F.data.startswith("admin_face_toggle:"))
        router.callback_query.register(self.on_admin_qr_revoke, F.data.startswith("admin_qr_revoke:"))
        router.callback_query.register(self.on_log_video, F.data.startswith("log_video:"))

        router.message.register(self.on_qr_name, QRStates.name, F.text)
        router.message.register(self.on_qr_max_uses, QRStates.max_uses, F.text)
        router.message.register(self.on_qr_ttl, QRStates.ttl, F.text)
        router.message.register(self.on_qr_ttl_value, QRStates.ttl_value, F.text)
        router.message.register(self.on_face_name, FaceStates.name, F.text)
        router.message.register(self.on_face_photo, FaceStates.photo, F.photo)
        router.message.register(self.on_face_photo_invalid, FaceStates.photo)
        router.message.register(self.on_admin_add_id, AdminStates.add_user, F.text)
        router.message.register(self.on_admin_block_id, AdminStates.block_user, F.text)

        router.message.register(self.on_unlock_text, F.text == BUTTON_UNLOCK)
        router.message.register(self.on_courier_menu, F.text == BUTTON_COURIER)
        router.message.register(self.on_live_text, F.text == BUTTON_CAMERA)
        router.message.register(self.on_qr_menu, F.text == BUTTON_QR)
        router.message.register(self.on_face_menu, F.text == BUTTON_FACES)
        router.message.register(self.on_admin_menu, F.text == BUTTON_ADMIN)
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

    async def _edit_message(
        self,
        message: Message,
        text: str,
        reply_markup: InlineKeyboardMarkup,
        **kwargs: Any,
    ) -> None:
        try:
            await message.edit_text(text, reply_markup=reply_markup, **kwargs)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            try:
                await message.delete()
            except TelegramBadRequest as delete_error:
                logger.debug("Не удалось удалить старое сообщение меню: %s", delete_error)
            await message.answer(text, reply_markup=reply_markup, **kwargs)

    @staticmethod
    def _main_keyboard(role: str) -> Any:
        builder = ReplyKeyboardBuilder()
        builder.button(text=BUTTON_UNLOCK)
        builder.button(text=BUTTON_CAMERA)
        builder.button(text=BUTTON_COURIER)
        builder.button(text=BUTTON_QR)
        builder.button(text=BUTTON_FACES)
        if role == "admin":
            builder.button(text=BUTTON_ADMIN)
        builder.adjust(1, 2, 2, 1)
        return builder.as_markup(
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Выберите действие…",
        )

    @staticmethod
    def _cancel_keyboard(back: str = "nav:main") -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data=back)
        builder.button(text="✖️ Отмена", callback_data="fsm_cancel")
        builder.adjust(2)
        return builder.as_markup()

    async def _send_home(self, message: Message, role: str, replace: bool = False) -> None:
        if replace:
            try:
                await message.delete()
            except TelegramBadRequest as exc:
                logger.debug("Не удалось удалить предыдущее меню: %s", exc)
        await message.answer(HOME_TEXT, reply_markup=self._main_keyboard(role))

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
            await message.answer(
                "⛔ Доступ не настроен.\n\n"
                f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
                "Отправьте его администратору для регистрации.",
                parse_mode="HTML",
            )
            return
        await self._send_home(message, role)

    async def on_menu(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        if not await self._require_message(message) or message.from_user is None:
            return
        role = await self._get_role(message.from_user.id)
        if role in {"admin", "resident"}:
            await self._send_home(message, role)

    async def on_cancel(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user is None:
            return
        role = await self._get_role(message.from_user.id)
        if role in {"admin", "resident"}:
            await message.answer("✖️ Действие отменено.", reply_markup=self._main_keyboard(role))

    async def on_fsm_cancel(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Действие отменено")
        message = self._callback_message(callback)
        if message is not None:
            role = await self._get_role(callback.from_user.id)
            if role in {"admin", "resident"}:
                await self._send_home(message, role, replace=True)

    async def on_nav_main(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await self._require_callback(callback):
            return
        message = self._callback_message(callback)
        if message is not None:
            role = await self._get_role(callback.from_user.id)
            if role in {"admin", "resident"}:
                await self._send_home(message, role, replace=True)
        await callback.answer()

    async def on_nav_qr(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await self._require_callback(callback):
            return
        message = self._callback_message(callback)
        if message is not None:
            await self._send_qr_menu(message, callback.from_user.id, edit=True)
        await callback.answer()

    async def on_nav_faces(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await self._require_callback(callback):
            return
        message = self._callback_message(callback)
        if message is not None:
            await self._send_face_menu(message, callback.from_user.id, edit=True)
        await callback.answer()

    async def on_nav_admin(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await self._require_callback(callback, admin=True):
            return
        message = self._callback_message(callback)
        if message is not None:
            await self._send_admin_menu(message, edit=True)
        await callback.answer()

    async def on_unlock_text(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        await self._enqueue_unlock(message.from_user.id, message.chat.id, "button", "Команда из Telegram")
        await message.answer(
            "⏳ Команда открытия отправлена.\n"
            "Результат придет отдельным сообщением; после успешного входа появится видео."
        )

    async def on_inline_unlock(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        await self._enqueue_unlock(callback.from_user.id, callback.from_user.id, "button", "Inline-кнопка Telegram")
        await callback.answer("Команда отправлена. Ожидайте результат.")

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
        await message.answer(
            f"📹 Записываю свежий клип: {self.settings.live_clip_seconds} с.\n"
            "Видео появится здесь после обработки."
        )

    async def on_courier_menu(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        await self._close_expired_courier_qrs(message.from_user.id)
        active = await self.db.get_active_courier_session(message.from_user.id)
        if active is not None:
            remaining_seconds = max(0, float(active["expires_at"]) - time.time())
            remaining = max(1, int((remaining_seconds + 59) // 60))
            builder = InlineKeyboardBuilder()
            builder.button(text="🎟 Показать QR и ссылку", callback_data="courier_resend")
            builder.button(text="🏠 Главное меню", callback_data="nav:main")
            builder.adjust(1)
            await message.answer(
                "🟢 Ожидание курьера активно\n\n"
                f"Осталось около {remaining} мин.\n"
                "Дверь откроется после звонка на панели или по ссылке из комментария к заказу.\n\n"
                "Можно повторно получить QR и готовый текст для курьера.",
                reply_markup=builder.as_markup(),
            )
            return
        builder = InlineKeyboardBuilder()
        for minutes in (15, 30, 60):
            builder.button(text=f"⏱ {minutes} мин", callback_data=f"courier_start:{minutes}")
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(3, 1)
        await message.answer(
            "🍕 Ожидание курьера\n\n"
            "Выберите длительность режима. После включения вы получите QR и текст для комментария к заказу.\n\n"
            "Дверь откроется один раз после звонка на панели или по курьерской ссылке.",
            reply_markup=builder.as_markup(),
        )

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
        await self._send_courier_pass(message, token, minutes)
        await callback.answer("Режим включен")

    async def _get_courier_token(
        self, user_id: int, session: dict[str, Any]
    ) -> str | None:
        tokens = await self.db.list_qr_tokens(user_id)
        for qr in tokens:
            if (
                qr["qr_type"] == "courier"
                and self._qr_rejection_reason(qr) is None
                and float(qr["created_at"]) >= float(session["created_at"]) - 1.0
            ):
                return str(qr["token"])
        return None

    async def _send_courier_pass(self, message: Message, token: str, minutes: int) -> None:
        link = self._access_link(token, "cour")
        comment = (
            "Домофон откроется сам при нажатии кнопки вызова на панели, "
            f"либо нажмите у двери: {link}"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="🎟 Показать QR еще раз", callback_data="courier_resend")
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(1)
        await message.answer_photo(
            BufferedInputFile(
                render_qr_png(token, self._qr_payload(token, "cour")),
                filename=f"courier_{token}.png",
            ),
            caption=f"📦 Курьерский пропуск\n\nОсталось около {minutes} мин.",
            reply_markup=builder.as_markup(),
        )
        await message.answer(comment)

    async def on_courier_resend(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback):
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        user_id = callback.from_user.id
        await self._close_expired_courier_qrs(user_id)
        session = await self.db.get_active_courier_session(user_id)
        if session is None:
            await callback.answer("Курьерский режим не активен", show_alert=True)
            return
        token = await self._get_courier_token(user_id, session)
        remaining_seconds = max(1, int(float(session["expires_at"]) - time.time()))
        if token is None:
            _, token = await self._create_unique_qr(
                creator_id=user_id,
                label=f"Курьер {time.strftime('%d.%m.%Y %H:%M')}",
                qr_type="courier",
                max_uses=1,
                expires_at=time.time() + remaining_seconds,
            )
        await self._send_courier_pass(message, token, max(1, remaining_seconds // 60))
        await callback.answer("QR и текст отправлены")

    def _access_link(self, token: str, kind: str = "qr") -> str:
        if self.settings.public_base_url:
            return build_http_link(self.settings.public_base_url, token, kind)
        return build_deep_link(self.bot_username, token, kind)

    def _qr_payload(self, token: str, kind: str = "qr") -> str | None:
        if self.settings.public_base_url:
            return build_http_link(self.settings.public_base_url, token, kind)
        return None

    async def _request_deep_link_unlock(
        self,
        token: str,
        kind: str,
        initiator_id: int | None,
        chat_id: int | None,
    ) -> tuple[int, str, bool]:
        if initiator_id is not None and await self._get_role(initiator_id) == "blocked":
            return 403, "⛔ Доступ запрещен.", False
        if not TOKEN_PATTERN.fullmatch(token) or kind not in {"qr", "cour"}:
            return 400, "Некорректная ссылка доступа.", False
        qr = await self.db.get_qr_token(token)
        reason = self._qr_rejection_reason(qr)
        if reason is not None:
            status = 404 if reason == "not_found" else 410
            return status, f"⚠️ {QR_REASON_MESSAGES[reason]}.", False
        if qr is None:
            return 404, "⚠️ QR-код не найден.", False
        if kind == "cour" and qr["qr_type"] != "courier":
            return 400, "⚠️ Эта ссылка не является курьерской.", False
        if kind == "qr" and qr["qr_type"] == "courier":
            return 400, "⚠️ Для курьерского пропуска используйте курьерскую ссылку.", False
        courier_session: dict[str, Any] | None = None
        if kind == "cour":
            courier_session = await self.db.get_active_courier_session(int(qr["creator_id"]))
            if courier_session is None:
                return 410, "⚠️ Ожидание курьера уже завершено.", False
        state = await self.db.get_door_state()
        now = time.time()
        present = (
            now - float(state["last_person_ts"]) <= 5.0
            or now - float(state["last_motion_ts"]) <= 4.0
        )
        if not present:
            return 409, "⚠️ Подойдите вплотную к камере домофона и повторите попытку.", True
        consumed = await self.db.consume_qr_token(token)
        if not consumed.allowed or consumed.token is None:
            return 410, f"⚠️ {QR_REASON_MESSAGES.get(consumed.reason, 'QR-код недействителен')}.", False
        qr = consumed.token
        payload: dict[str, Any] = {
            "user_id": int(qr["creator_id"]),
            "chat_id": chat_id if chat_id is not None else int(qr["creator_id"]),
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
            return 500, "⚠️ Не удалось передать команду. Использование QR возвращено.", False
        return 202, "✅ Проверка пройдена. Команда открытия отправлена.", False

    async def _handle_deep_link(
        self, message: Message, token: str, kind: str, initiator_id: int
    ) -> None:
        _, text, retry = await self._request_deep_link_unlock(
            token, kind, initiator_id, message.chat.id
        )
        keyboard = None
        if retry:
            builder = InlineKeyboardBuilder()
            builder.button(
                text="🔄 Я у камеры, повторить",
                callback_data=f"deeplink_retry:{kind}:{token}",
            )
            keyboard = builder.as_markup()
        await message.answer(text, reply_markup=keyboard)

    async def _http_open(self, request: web.Request) -> web.Response:
        status, text, _ = await self._request_deep_link_unlock(
            request.match_info["token"],
            request.match_info["kind"],
            None,
            None,
        )
        return web.Response(status=status, text=text, charset="utf-8")

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
        if qr["expires_at"] is not None and float(qr["expires_at"]) <= time.time():
            return "expired"
        if not qr["is_active"]:
            return "inactive"
        if int(qr["max_uses"]) > 0 and int(qr["used_count"]) >= int(qr["max_uses"]):
            return "exhausted"
        return None

    async def _close_expired_courier_qrs(self, user_id: int | None = None) -> int:
        tokens = await self.db.list_qr_tokens(user_id)
        closed = 0
        now = time.time()
        for qr in tokens:
            if (
                qr["qr_type"] == "courier"
                and qr["is_active"]
                and qr["expires_at"] is not None
                and float(qr["expires_at"]) <= now
            ):
                changed = await self.db.set_qr_token_active(
                    int(qr["id"]), False, user_id
                )
                closed += int(changed)
        return closed

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
        await self._send_qr_menu(message, message.from_user.id)

    async def _send_qr_menu(self, message: Message, user_id: int, edit: bool = False) -> None:
        await self._close_expired_courier_qrs(user_id)
        tokens = await self.db.list_qr_tokens(user_id)
        builder = InlineKeyboardBuilder()
        for qr in tokens[:20]:
            status = "🟢" if self._qr_rejection_reason(qr) is None else "⚪"
            type_icon = QR_TYPE_NAMES.get(str(qr["qr_type"]), "🎟").split()[0]
            max_uses = int(qr["max_uses"])
            limit = "∞" if max_uses == 0 else str(max_uses)
            builder.button(
                text=(
                    f"{status} {type_icon} {str(qr['label'])[:28]} · "
                    f"{qr['used_count']}/{limit}"
                ),
                callback_data=f"qr_view:{qr['id']}",
            )
        builder.button(text="➕ Создать пропуск", callback_data="qr_create")
        builder.button(text="👥 Перейти к лицам", callback_data="nav:faces")
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(1)
        if tokens:
            active_count = sum(self._qr_rejection_reason(qr) is None for qr in tokens)
            text = (
                "🎟 Пропуска\n\n"
                f"Всего: {len(tokens)} · активных: {active_count}\n"
                "🟢 действует · ⚪ недействителен\n\n"
                "Нажмите на пропуск, чтобы увидеть QR, срок и ссылку."
            )
            if len(tokens) > 20:
                text += "\n\nПоказаны 20 последних пропусков."
        else:
            text = (
                "🎟 Пропуска\n\n"
                "Здесь появятся ваши QR-коды.\n"
                "Создайте постоянный или временный пропуск для гостей."
            )
        if edit:
            await self._edit_message(message, text, builder.as_markup())
        else:
            await message.answer(text, reply_markup=builder.as_markup())

    async def on_qr_create(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        await state.clear()
        await state.set_state(QRStates.name)
        message = self._callback_message(callback)
        if message is not None:
            await self._edit_message(
                message,
                "🎟 Новый пропуск\n\nВведите понятное название, например «Гость» или «Сантехник»:",
                self._cancel_keyboard("nav:qr"),
            )
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
        builder.button(text="♾️ Бессрочный", callback_data="qr_kind:permanent")
        builder.button(text="⏳ По сроку", callback_data="qr_kind:temporary")
        builder.button(text="⬅️ Назад", callback_data="nav:qr")
        builder.button(text="✖️ Отмена", callback_data="fsm_cancel")
        builder.adjust(2, 2)
        await message.answer(
            f"🎟 {name}\n\n♾️ Бессрочный не имеет срока действия.\n⏳ По сроку можно настроить месяцы, дни, часы и минуты.",
            reply_markup=builder.as_markup(),
        )

    @staticmethod
    def _qr_limit_keyboard() -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="1 вход", callback_data="qr_limit:1")
        builder.button(text="5 входов", callback_data="qr_limit:5")
        builder.button(text="∞ Безлимит", callback_data="qr_limit:0")
        builder.button(text="⬅️ Назад", callback_data="nav:qr")
        builder.button(text="✖️ Отмена", callback_data="fsm_cancel")
        builder.adjust(3, 2)
        return builder.as_markup()

    @staticmethod
    def _qr_ttl_keyboard() -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="30 мин", callback_data="qr_ttl:30")
        builder.button(text="2 часа", callback_data="qr_ttl:120")
        builder.button(text="6 часов", callback_data="qr_ttl:360")
        builder.button(text="24 часа", callback_data="qr_ttl:1440")
        builder.button(text="3 дня", callback_data="qr_ttl:4320")
        builder.button(text="7 дней", callback_data="qr_ttl:10080")
        builder.button(text="📆 Месяцы", callback_data="qr_ttl_unit:months")
        builder.button(text="📅 Дни", callback_data="qr_ttl_unit:days")
        builder.button(text="🕐 Часы", callback_data="qr_ttl_unit:hours")
        builder.button(text="⏱ Минуты", callback_data="qr_ttl_unit:minutes")
        builder.button(text="✅ Создать пропуск", callback_data="qr_ttl_finish")
        builder.button(text="🧹 Сбросить", callback_data="qr_ttl_reset")
        builder.button(text="⬅️ Назад", callback_data="nav:qr")
        builder.button(text="✖️ Отмена", callback_data="fsm_cancel")
        builder.adjust(3, 3, 4, 1, 3)
        return builder.as_markup()

    @staticmethod
    def _qr_ttl_text(data: dict[str, Any]) -> str:
        months = int(data.get("ttl_months", 0))
        days = int(data.get("ttl_days", 0))
        hours = int(data.get("ttl_hours", 0))
        minutes = int(data.get("ttl_minutes", 0))
        parts = []
        if months:
            parts.append(f"{months} мес.")
        if days:
            parts.append(f"{days} дн.")
        if hours:
            parts.append(f"{hours} ч.")
        if minutes:
            parts.append(f"{minutes} мин.")
        total = " + ".join(parts) if parts else "не выбран"
        return (
            "⏳ Срок действия\n\n"
            f"📆 Месяцы: {months} (по 30 дней)\n📅 Дни: {days}\n"
            f"🕐 Часы: {hours}\n⏱ Минуты: {minutes}\n\n"
            f"Итого: {total}\n\n"
            "Выберите пресет, настройте единицы отдельно или введите общее число минут."
        )

    @staticmethod
    def _qr_total_minutes(data: dict[str, Any]) -> int:
        return (
            int(data.get("ttl_months", 0)) * 43200
            + int(data.get("ttl_days", 0)) * 1440
            + int(data.get("ttl_hours", 0)) * 60
            + int(data.get("ttl_minutes", 0))
        )

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
            await self._edit_message(
                message,
                f"{QR_TYPE_NAMES[kind]}\n\nВыберите лимит или введите свое число от 0 до 10000.",
                self._qr_limit_keyboard(),
            )
        await callback.answer()

    async def on_qr_limit(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        try:
            max_uses = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный лимит", show_alert=True)
            return
        if max_uses not in {0, 1, 5}:
            await callback.answer("Некорректный лимит", show_alert=True)
            return
        await state.update_data(max_uses=max_uses)
        data = await state.get_data()
        message = self._callback_message(callback)
        if data.get("kind") == "temporary":
            await state.set_state(QRStates.ttl)
            if message is not None:
                await self._edit_message(message, self._qr_ttl_text(data), self._qr_ttl_keyboard())
        elif message is not None:
            await self._finish_qr_creation(message, state, callback.from_user.id)
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
            await message.answer(self._qr_ttl_text(data), reply_markup=self._qr_ttl_keyboard())
            return
        await self._finish_qr_creation(message, state)

    async def on_qr_ttl_preset(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        try:
            minutes = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный срок", show_alert=True)
            return
        if minutes not in {30, 120, 360, 1440, 4320, 10080}:
            await callback.answer("Некорректный срок", show_alert=True)
            return
        await state.update_data(expires_at=time.time() + minutes * 60)
        message = self._callback_message(callback)
        if message is not None:
            await self._finish_qr_creation(message, state, callback.from_user.id)
        await callback.answer()

    async def on_qr_ttl_unit(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        unit = callback.data.rsplit(":", 1)[1]
        prompts = {
            "months": ("ttl_months", "📆 Введите количество месяцев (1 месяц = 30 дней):"),
            "days": ("ttl_days", "📅 Введите количество дней:"),
            "hours": ("ttl_hours", "🕐 Введите количество часов:"),
            "minutes": ("ttl_minutes", "⏱ Введите количество минут:"),
        }
        if unit not in prompts:
            await callback.answer("Некорректная единица", show_alert=True)
            return
        field, prompt = prompts[unit]
        await state.update_data(ttl_unit=field)
        await state.set_state(QRStates.ttl_value)
        message = self._callback_message(callback)
        if message is not None:
            await self._edit_message(message, prompt, self._cancel_keyboard("qr_ttl_back"))
        await callback.answer()

    async def on_qr_ttl_back(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        await state.set_state(QRStates.ttl)
        data = await state.get_data()
        message = self._callback_message(callback)
        if message is not None:
            await self._edit_message(message, self._qr_ttl_text(data), self._qr_ttl_keyboard())
        await callback.answer()

    async def on_qr_ttl_value(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message):
            return
        try:
            value = int((message.text or "").strip())
        except ValueError:
            value = -1
        if value < 0:
            await message.answer("Введите ноль или положительное целое число.")
            return
        data = await state.get_data()
        field = str(data.get("ttl_unit", ""))
        if field not in {"ttl_months", "ttl_days", "ttl_hours", "ttl_minutes"}:
            await state.set_state(QRStates.ttl)
            await message.answer(self._qr_ttl_text(data), reply_markup=self._qr_ttl_keyboard())
            return
        candidate = dict(data)
        candidate[field] = value
        if self._qr_total_minutes(candidate) > MAX_QR_TTL_MINUTES:
            await message.answer("Общий срок не может превышать 365 дней.")
            return
        await state.update_data(**{field: value})
        await state.set_state(QRStates.ttl)
        await message.answer(
            self._qr_ttl_text(await state.get_data()),
            reply_markup=self._qr_ttl_keyboard(),
        )

    async def on_qr_ttl_reset(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        await state.update_data(ttl_months=0, ttl_days=0, ttl_hours=0, ttl_minutes=0)
        message = self._callback_message(callback)
        if message is not None:
            data = await state.get_data()
            await self._edit_message(message, self._qr_ttl_text(data), self._qr_ttl_keyboard())
        await callback.answer("Срок сброшен")

    async def on_qr_ttl_finish(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback):
            return
        data = await state.get_data()
        total = self._qr_total_minutes(data)
        if total <= 0:
            await callback.answer("Сначала настройте срок действия", show_alert=True)
            return
        if total > MAX_QR_TTL_MINUTES:
            await callback.answer("Срок не может превышать 365 дней", show_alert=True)
            return
        await state.update_data(expires_at=time.time() + total * 60)
        message = self._callback_message(callback)
        if message is not None:
            await self._finish_qr_creation(message, state, callback.from_user.id)
        await callback.answer()

    async def on_qr_ttl(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message):
            return
        try:
            minutes = int((message.text or "").strip())
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= MAX_QR_TTL_MINUTES:
            await message.answer("Введите положительное число минут не более 365 дней.")
            return
        await state.update_data(expires_at=time.time() + minutes * 60)
        await self._finish_qr_creation(message, state)

    async def _finish_qr_creation(
        self, message: Message, state: FSMContext, user_id: int | None = None
    ) -> None:
        if user_id is None:
            if message.from_user is None:
                return
            user_id = message.from_user.id
        data = await state.get_data()
        kind = str(data.get("kind", ""))
        name = str(data.get("name", ""))
        if kind not in {"permanent", "temporary"} or not name or "max_uses" not in data:
            await state.clear()
            await message.answer("Данные мастера устарели. Начните создание QR заново.")
            return
        qr_id, token = await self._create_unique_qr(
            creator_id=user_id,
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
        link = self._access_link(token)
        max_uses = int(qr["max_uses"])
        limit = "∞" if max_uses == 0 else str(max_uses)
        expires = (
            time.strftime("%d.%m.%Y %H:%M", time.localtime(float(qr["expires_at"])))
            if qr["expires_at"] is not None
            else "без ограничения"
        )
        reason = self._qr_rejection_reason(qr)
        status = "🟢 Активен" if reason is None else f"⚪ {QR_REASON_MESSAGES[reason]}"
        qr_type = QR_TYPE_NAMES.get(str(qr["qr_type"]), str(qr["qr_type"]))
        caption = (
            f"🎟 {qr['label']}\n\n"
            f"Тип: {qr_type}\n"
            f"Статус: {status}\n"
            f"Использовано: {qr['used_count']} / {limit}\n"
            f"Действует до: {expires}\n\n"
            f"🔗 Открытие у двери:\n{link}"
        )
        builder = InlineKeyboardBuilder()
        if reason is None:
            builder.button(text="🚫 Отозвать пропуск", callback_data=f"qr_revoke:{qr_id}")
        builder.button(text="⬅️ К пропускам", callback_data="nav:qr")
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(1)
        await message.answer_photo(
            BufferedInputFile(
                render_qr_png(token, self._qr_payload(token)),
                filename=f"door_{token}.png",
            ),
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
        await callback.answer("Пропуск отозван")
        message = self._callback_message(callback)
        if message is not None:
            await self._send_qr_menu(message, callback.from_user.id, edit=True)

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
        await self._send_face_menu(message, message.from_user.id)

    async def _send_face_menu(self, message: Message, user_id: int, edit: bool = False) -> None:
        role = await self._get_role(user_id)
        faces = await self.db.list_face_embeddings(user_id)
        builder = InlineKeyboardBuilder()
        for face in faces[:20]:
            active = bool(face["is_active"])
            status = "🟢" if active else "⚪"
            action = "включено" if active else "отключено"
            builder.button(
                text=f"{status} {str(face['person_name'])[:34]} · {action}",
                callback_data=f"face_toggle:{face['id']}",
            )
        if role == "admin":
            builder.button(text="➕ Добавить лицо", callback_data="face_add")
        builder.button(text="🎟 Перейти к пропускам", callback_data="nav:qr")
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(1)
        if faces:
            active_count = sum(bool(face["is_active"]) for face in faces)
            text = (
                "👥 Лица\n\n"
                f"Всего: {len(faces)} · активных: {active_count}\n"
                "🟢 распознается · ⚪ отключено\n\n"
                "Нажмите на имя, чтобы изменить доступ."
            )
            if len(faces) > 20:
                text += "\n\nПоказаны 20 последних записей."
        else:
            text = "👥 Лица\n\nЗарегистрированных лиц пока нет."
        if role == "admin":
            text += "\n\nДобавьте фотографию с одним хорошо видимым лицом."
        else:
            text += "\n\nНовые лица регистрирует администратор."
        if edit:
            await self._edit_message(message, text, builder.as_markup())
        else:
            await message.answer(text, reply_markup=builder.as_markup())

    async def on_face_add(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        await state.clear()
        await state.update_data(target_user_id=callback.from_user.id, back_callback="nav:faces")
        await state.set_state(FaceStates.name)
        message = self._callback_message(callback)
        if message is not None:
            await self._edit_message(
                message,
                "👥 Новое лицо\n\nВведите имя человека:",
                self._cancel_keyboard("nav:faces"),
            )
        await callback.answer()

    async def on_face_name(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message, admin=True) or message.from_user is None:
            return
        data = await state.get_data()
        name = (message.text or "").strip()
        if not 1 <= len(name) <= 64:
            await message.answer("Имя должно содержать от 1 до 64 символов.")
            return
        await state.update_data(name=name)
        await state.set_state(FaceStates.photo)
        back_callback = str(data.get("back_callback", "nav:faces"))
        await message.answer(
            f"👤 {name}\n\nОтправьте четкую фотографию. На снимке должно быть только одно лицо.",
            reply_markup=self._cancel_keyboard(back_callback),
        )

    async def on_face_photo(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message, admin=True) or message.from_user is None:
            return
        data = await state.get_data()
        name = str(data.get("name", "")).strip()
        target_user_id = int(data.get("target_user_id", message.from_user.id))
        target_user = await self.db.get_user(target_user_id)
        if not name or target_user is None or target_user["role"] == "blocked":
            await state.clear()
            await message.answer("Данные устарели или пользователь недоступен. Начните заново.")
            return
        path = self.settings.face_tasks_dir / f"{target_user_id}_{uuid.uuid4().hex}.jpg"
        try:
            await message.bot.download(message.photo[-1], destination=path)
            await self.db.create_face_task(target_user_id, name, str(path))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        await state.clear()
        builder = InlineKeyboardBuilder()
        back_callback = str(data.get("back_callback", "nav:faces"))
        builder.button(text="⬅️ К списку лиц", callback_data=back_callback)
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(1)
        await message.answer(
            f"✅ Фотография «{name}» принята.\n\n"
            "Результат появится в списке лиц; пользователь получит отдельное уведомление.",
            reply_markup=builder.as_markup(),
        )

    async def on_face_photo_invalid(self, message: Message) -> None:
        if await self._require_message(message, admin=True):
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
        await callback.answer("Распознавание включено" if active else "Распознавание отключено")
        message = self._callback_message(callback)
        if message is not None:
            await self._send_face_menu(message, callback.from_user.id, edit=True)

    async def _send_admin_faces(
        self, message: Message, user_id: int, edit: bool = True
    ) -> bool:
        user = await self.db.get_user(user_id)
        if user is None:
            return False
        faces = await self.db.list_face_embeddings(user_id)
        builder = InlineKeyboardBuilder()
        for face in faces[:20]:
            status = "🟢" if face["is_active"] else "⚪"
            builder.button(
                text=f"{status} {str(face['person_name'])[:40]}",
                callback_data=f"admin_face_toggle:{face['id']}:{user_id}",
            )
        if user["role"] != "blocked":
            builder.button(text="➕ Добавить лицо", callback_data=f"admin_face_add:{user_id}")
        builder.button(text="⬅️ К карточке пользователя", callback_data=f"user_view:{user_id}")
        builder.button(text="🛡 Админ-панель", callback_data="nav:admin")
        builder.adjust(1)
        active_count = sum(bool(face["is_active"]) for face in faces)
        text = (
            f"👥 Лица: {user['full_name']}\n\n"
            f"Всего: {len(faces)} · активных: {active_count}\n"
            "Нажмите на лицо, чтобы включить или отключить доступ."
        )
        if len(faces) > 20:
            text += "\n\nПоказаны 20 последних записей."
        if user["role"] == "blocked":
            text += "\n\nПользователь заблокирован: включить лица нельзя."
        if edit:
            await self._edit_message(message, text, builder.as_markup())
        else:
            await message.answer(text, reply_markup=builder.as_markup())
        return True

    async def on_admin_faces(self, callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await self._require_callback(callback, admin=True):
            return
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        if not await self._send_admin_faces(message, user_id):
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        await callback.answer()

    async def on_admin_face_add(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        user = await self.db.get_user(user_id)
        if user is None or user["role"] == "blocked":
            await callback.answer("Пользователь не найден или заблокирован", show_alert=True)
            return
        await state.clear()
        await state.update_data(
            target_user_id=user_id,
            back_callback=f"admin_faces:{user_id}",
        )
        await state.set_state(FaceStates.name)
        message = self._callback_message(callback)
        if message is not None:
            await self._edit_message(
                message,
                f"👥 Новое лицо: {user['full_name']}\n\nВведите имя человека:",
                self._cancel_keyboard(f"admin_faces:{user_id}"),
            )
        await callback.answer()

    async def on_admin_face_toggle(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        try:
            _, raw_face_id, raw_user_id = callback.data.split(":", 2)
            face_id = int(raw_face_id)
            user_id = int(raw_user_id)
        except ValueError:
            await callback.answer("Некорректные данные лица", show_alert=True)
            return
        user = await self.db.get_user(user_id)
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        faces = await self.db.list_face_embeddings(user_id)
        face = next((item for item in faces if int(item["id"]) == face_id), None)
        if face is None:
            await callback.answer("Лицо не найдено", show_alert=True)
            return
        active = not bool(face["is_active"])
        if active and user["role"] == "blocked":
            await callback.answer("Сначала разблокируйте пользователя", show_alert=True)
            return
        await self.db.set_face_embedding_active(face_id, user_id, active)
        await self.db.enqueue_ipc_command("reload_faces", "{}")
        await callback.answer("Распознавание включено" if active else "Распознавание отключено")
        message = self._callback_message(callback)
        if message is not None:
            await self._send_admin_faces(message, user_id)

    async def on_admin_menu(self, message: Message) -> None:
        if not await self._require_message(message, admin=True):
            return
        await self._send_admin_menu(message)

    async def _send_admin_menu(self, message: Message, edit: bool = False) -> None:
        builder = InlineKeyboardBuilder()
        builder.button(text="👥 Пользователи и доступ", callback_data="admin_users")
        builder.button(text="➕ Добавить пользователя или администратора", callback_data="admin_add")
        builder.button(text="🚫 Заблокировать по Telegram ID", callback_data="admin_block")
        builder.button(text="📜 Журнал входов", callback_data="admin_logs")
        builder.button(text="🎟 Активные пропуска", callback_data="admin_qrs")
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(1)
        text = (
            "🛡 Админ-панель\n\n"
            "Управление ролями, лицами и пропусками.\n"
            "Откройте карточку пользователя, чтобы изменить его доступ."
        )
        if edit:
            await self._edit_message(message, text, builder.as_markup())
        else:
            await message.answer(text, reply_markup=builder.as_markup())

    async def on_admin_add(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        await state.clear()
        await state.set_state(AdminStates.add_user)
        message = self._callback_message(callback)
        if message is not None:
            await self._edit_message(
                message,
                "👤 Новый пользователь\n\nОтправьте его Telegram ID, затем выберите роль:",
                self._cancel_keyboard("nav:admin"),
            )
        await callback.answer()

    async def on_admin_add_id(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message, admin=True):
            return
        telegram_id = self._parse_telegram_id(message.text or "")
        if telegram_id is None:
            await message.answer("Telegram ID должен быть положительным целым числом.")
            return
        await state.update_data(new_user_id=telegram_id)
        await state.set_state(AdminStates.add_role)
        builder = InlineKeyboardBuilder()
        builder.button(text="👤 Постоялец", callback_data="admin_add_role:resident")
        builder.button(text="🛡 Администратор", callback_data="admin_add_role:admin")
        builder.button(text="⬅️ Назад", callback_data="nav:admin")
        builder.button(text="✖️ Отмена", callback_data="fsm_cancel")
        builder.adjust(2)
        await message.answer(
            f"Пользователь: <code>{telegram_id}</code>\n\n"
            "👤 Постоялец управляет своей дверью и пропусками.\n"
            "🛡 Администратор дополнительно управляет пользователями и всеми лицами.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )

    async def on_admin_add_role(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        role = callback.data.rsplit(":", 1)[1]
        data = await state.get_data()
        telegram_id = int(data.get("new_user_id", 0))
        if role not in {"admin", "resident"} or telegram_id <= 0:
            await state.clear()
            await callback.answer("Данные устарели. Начните заново.", show_alert=True)
            return
        existing = await self.db.get_user(telegram_id)
        await self.db.upsert_user(
            telegram_id,
            existing["username"] if existing is not None else None,
            existing["full_name"] if existing is not None else f"Пользователь {telegram_id}",
            role,
        )
        await state.clear()
        role_name = "администратор" if role == "admin" else "постоялец"
        await callback.answer(f"Роль назначена: {role_name}")
        message = self._callback_message(callback)
        if message is not None:
            await message.answer(f"✅ Пользователь {telegram_id} сохранен как {role_name}.")
            await self._send_admin_menu(message)

    async def on_admin_block(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        await state.clear()
        await state.set_state(AdminStates.block_user)
        message = self._callback_message(callback)
        if message is not None:
            await self._edit_message(
                message,
                "🚫 Блокировка\n\nОтправьте Telegram ID пользователя:",
                self._cancel_keyboard("nav:admin"),
            )
        await callback.answer()

    async def _block_user(self, actor_id: int, telegram_id: int) -> tuple[bool, str]:
        if telegram_id == actor_id:
            return False, "Нельзя заблокировать собственную учетную запись."
        if telegram_id in self.settings.admin_ids:
            return False, "Нельзя заблокировать администратора из ADMIN_IDS."
        async with self.db.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "UPDATE users SET role = 'blocked' WHERE telegram_id = ?", (telegram_id,)
            )
            if cursor.rowcount == 0:
                await connection.rollback()
                return False, "Пользователь не найден."
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
        return True, (
            f"🚫 Пользователь {telegram_id} заблокирован.\n"
            "Его лица, пропуска и курьерские сессии отключены."
        )

    async def on_admin_block_id(self, message: Message, state: FSMContext) -> None:
        if not await self._require_message(message, admin=True) or message.from_user is None:
            return
        telegram_id = self._parse_telegram_id(message.text or "")
        if telegram_id is None:
            await message.answer("Telegram ID должен быть положительным целым числом.")
            return
        changed, text = await self._block_user(message.from_user.id, telegram_id)
        if not changed:
            await message.answer(text)
            return
        await state.clear()
        await message.answer(text)
        await self._send_admin_menu(message)

    def _available_user_qrs(self, tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [qr for qr in tokens if self._qr_rejection_reason(qr) is None]

    @staticmethod
    def _user_expiry_label(tokens: list[dict[str, Any]]) -> str:
        expirations = [
            float(qr["expires_at"])
            for qr in tokens
            if qr["expires_at"] is not None
        ]
        if expirations:
            return time.strftime("%d.%m %H:%M", time.localtime(min(expirations)))
        if tokens:
            return "бессрочно"
        return "нет пропусков"

    async def on_admin_users(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        users = await self.db.list_users(100)
        builder = InlineKeyboardBuilder()
        for user in users[:20]:
            tokens = self._available_user_qrs(
                await self.db.list_qr_tokens(int(user["telegram_id"]))
            )
            role_icon = {"admin": "🛡", "resident": "👤", "blocked": "⛔"}.get(
                str(user["role"]), "👤"
            )
            expiry = self._user_expiry_label(tokens)
            builder.button(
                text=f"{role_icon} {str(user['full_name'])[:24]} · {expiry}",
                callback_data=f"user_view:{user['telegram_id']}",
            )
        builder.button(text="⬅️ В админ-панель", callback_data="nav:admin")
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(1)
        await self._edit_message(
            message,
            "👥 Пользователи\n\nВыберите пользователя. В списке указан ближайший срок активного пропуска.",
            builder.as_markup(),
        )
        await callback.answer()

    async def _send_admin_user_card(
        self,
        message: Message,
        actor_id: int,
        user_id: int,
        edit: bool = True,
    ) -> bool:
        user = await self.db.get_user(user_id)
        if user is None:
            return False
        tokens = self._available_user_qrs(await self.db.list_qr_tokens(user_id))
        faces = await self.db.list_face_embeddings(user_id)
        active_faces = sum(1 for face in faces if face["is_active"])
        role_names = {"admin": "Администратор", "resident": "Постоялец", "blocked": "Заблокирован"}
        username = f"@{user['username']}" if user["username"] else "не указан"
        created = time.strftime("%d.%m.%Y", time.localtime(float(user["created_at"])))
        qr_lines = []
        for qr in tokens[:5]:
            label = escape(str(qr["label"]))
            if qr["expires_at"] is None:
                qr_lines.append(f"♾️ {label} — без срока")
            else:
                expires = time.strftime(
                    "%d.%m.%Y %H:%M", time.localtime(float(qr["expires_at"]))
                )
                qr_lines.append(f"⏳ {label} — до {expires}")
        if len(tokens) > 5:
            qr_lines.append(f"… и еще {len(tokens) - 5}")
        courier = await self.db.get_active_courier_session(user_id)
        courier_text = "нет"
        if courier is not None:
            courier_text = time.strftime(
                "%d.%m.%Y %H:%M", time.localtime(float(courier["expires_at"]))
            )
        text = (
            f"👤 {escape(str(user['full_name']))}\n\n"
            f"ID: <code>{user_id}</code>\nUsername: {escape(username)}\n"
            f"Роль: {role_names.get(str(user['role']), user['role'])}\n"
            f"Добавлен: {created}\n"
            f"Лица: {active_faces} активных из {len(faces)}\n"
            f"Курьерский режим до: {courier_text}\n\n"
            f"🎟 Действующие пропуска: {len(tokens)}\n"
            + ("\n".join(qr_lines) if qr_lines else "Нет действующих пропусков.")
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="👥 Управление лицами", callback_data=f"admin_faces:{user_id}")
        role = str(user["role"])
        if role == "resident":
            builder.button(
                text="🛡 Сделать администратором",
                callback_data=f"admin_user_role:{user_id}:admin",
            )
        elif role == "admin" and user_id != actor_id and user_id not in self.settings.admin_ids:
            builder.button(
                text="👤 Сделать постояльцем",
                callback_data=f"admin_user_role:{user_id}:resident",
            )
        if role == "blocked":
            builder.button(text="✅ Разблокировать", callback_data=f"admin_user_restore:{user_id}")
        elif user_id != actor_id and user_id not in self.settings.admin_ids:
            builder.button(text="🚫 Заблокировать", callback_data=f"admin_user_block:{user_id}")
        builder.button(text="⬅️ К пользователям", callback_data="admin_users")
        builder.button(text="🛡 Админ-панель", callback_data="nav:admin")
        builder.adjust(1)
        if edit:
            await self._edit_message(message, text, builder.as_markup(), parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        return True

    async def on_user_view(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        message = self._callback_message(callback)
        if message is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return
        if not await self._send_admin_user_card(message, callback.from_user.id, user_id):
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        await callback.answer()

    async def on_admin_user_role(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        try:
            _, raw_user_id, role = callback.data.split(":", 2)
            user_id = int(raw_user_id)
        except ValueError:
            await callback.answer("Некорректные данные роли", show_alert=True)
            return
        if role not in {"admin", "resident"}:
            await callback.answer("Некорректная роль", show_alert=True)
            return
        if role == "resident" and user_id == callback.from_user.id:
            await callback.answer("Нельзя снять роль администратора с себя", show_alert=True)
            return
        if role == "resident" and user_id in self.settings.admin_ids:
            await callback.answer("Этот администратор задан через ADMIN_IDS", show_alert=True)
            return
        if not await self.db.set_user_role(user_id, role):
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        await callback.answer("Роль обновлена")
        message = self._callback_message(callback)
        if message is not None:
            await self._send_admin_user_card(message, callback.from_user.id, user_id)

    async def on_admin_user_block(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        changed, text = await self._block_user(callback.from_user.id, user_id)
        await callback.answer(text, show_alert=True)
        message = self._callback_message(callback)
        if changed and message is not None:
            await self._send_admin_user_card(message, callback.from_user.id, user_id)

    async def on_admin_user_restore(self, callback: CallbackQuery) -> None:
        if not await self._require_callback(callback, admin=True):
            return
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        user = await self.db.get_user(user_id)
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        if user["role"] != "blocked":
            await callback.answer("Пользователь не заблокирован", show_alert=True)
            return
        await self.db.set_user_role(user_id, "resident")
        await callback.answer("Пользователь разблокирован как постоялец")
        message = self._callback_message(callback)
        if message is not None:
            await self._send_admin_user_card(message, callback.from_user.id, user_id)

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
        keyboard = InlineKeyboardBuilder()
        if not logs:
            keyboard.button(text="⬅️ В админ-панель", callback_data="nav:admin")
            keyboard.button(text="🏠 Главное меню", callback_data="nav:main")
            keyboard.adjust(1)
            await message.answer(
                "📜 Журнал входов\n\nЗаписей пока нет. Новые входы появятся здесь автоматически.",
                reply_markup=keyboard.as_markup(),
            )
            await callback.answer()
            return
        lines = []
        for log in logs:
            created = time.strftime("%d.%m %H:%M:%S", time.localtime(float(log["created_at"])))
            warning = "\n⚠️ Возможен проход следом" if log["tailgating_alert"] else ""
            method = ACCESS_METHOD_NAMES.get(str(log["method"]), str(log["method"]))
            lines.append(
                f"#{log['id']} · {created}\n{log['details'] or method}\n"
                f"{method} · Людей: {log['people_entered']}{warning}"
            )
            if log["video_path"]:
                keyboard.button(text=f"📹 Видео входа #{log['id']}", callback_data=f"log_video:{log['id']}")
        keyboard.button(text="⬅️ В админ-панель", callback_data="nav:admin")
        keyboard.button(text="🏠 Главное меню", callback_data="nav:main")
        keyboard.adjust(1)
        await message.answer(
            "📜 Последние 10 входов\n\n" + "\n\n".join(lines),
            reply_markup=keyboard.as_markup(),
        )
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
        await self._close_expired_courier_qrs()
        tokens = await self.db.list_qr_tokens()
        builder = InlineKeyboardBuilder()
        active_count = 0
        for qr in tokens:
            if self._qr_rejection_reason(qr) is not None:
                continue
            active_count += 1
            builder.button(
                text=f"🎟 #{qr['id']} · {str(qr['label'])[:30]} · {qr['creator_id']}",
                callback_data=f"admin_qr_revoke:{qr['id']}",
            )
            if active_count >= 20:
                break
        builder.button(text="⬅️ В админ-панель", callback_data="nav:admin")
        builder.button(text="🏠 Главное меню", callback_data="nav:main")
        builder.adjust(1)
        text = (
            "🎟 Активные пропуска\n\nНажмите на пропуск, чтобы отозвать его."
            if active_count
            else "🎟 Активные пропуска\n\nСейчас активных пропусков нет."
        )
        await message.answer(text, reply_markup=builder.as_markup())
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
        await callback.answer("Пропуск отозван" if changed else "Пропуск не найден", show_alert=not changed)

    async def on_fallback(self, message: Message) -> None:
        if not await self._require_message(message) or message.from_user is None:
            return
        role = await self._get_role(message.from_user.id)
        if role in {"admin", "resident"}:
            await message.answer(HOME_TEXT, reply_markup=self._main_keyboard(role))

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
        last_courier_cleanup = 0.0
        while True:
            try:
                now = time.monotonic()
                if now - last_courier_cleanup >= 30.0:
                    await self._close_expired_courier_qrs()
                    last_courier_cleanup = now
                items = await self.db.get_pending_outbox(20)
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

    async def _start_http_server(self) -> None:
        if not self.settings.public_base_url:
            return
        application = web.Application()
        application.router.add_get("/open/{kind}/{token}", self._http_open)
        runner = web.AppRunner(application)
        await runner.setup()
        try:
            site = web.TCPSite(
                runner,
                host=self.settings.http_host,
                port=self.settings.http_port,
            )
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._http_runner = runner
        logger.info(
            "HTTP-ссылки доступа включены: %s, listen=%s:%d",
            self.settings.public_base_url,
            self.settings.http_host,
            self.settings.http_port,
        )

    async def run(self) -> None:
        outbox_task: asyncio.Task[None] | None = None
        try:
            await self.db.init()
            await self._close_expired_courier_qrs()
            await self._initialize_admins()
            telegram_user = await self.bot.get_me()
            if telegram_user.username:
                self.bot_username = telegram_user.username
            await self.bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начать работу"),
                    BotCommand(command="menu", description="Главное меню"),
                    BotCommand(command="cancel", description="Отменить текущее действие"),
                ]
            )
            dispatcher = Dispatcher(storage=MemoryStorage())
            dispatcher.include_router(self.router)
            await self._start_http_server()
            outbox_task = asyncio.create_task(self.outbox_dispatcher(), name="outbox-dispatcher")
            await dispatcher.start_polling(self.bot)
        finally:
            if outbox_task is not None:
                outbox_task.cancel()
                await asyncio.gather(outbox_task, return_exceptions=True)
            if self._http_runner is not None:
                await self._http_runner.cleanup()
                self._http_runner = None
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
