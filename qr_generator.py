from __future__ import annotations

import re
import secrets
from io import BytesIO
from urllib.parse import urlsplit

import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_M


TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
BOT_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")
OUTER_BORDER_PX = 40
OUTER_BORDER_COLOR = "#222222"


def generate_token() -> str:
    return secrets.token_hex(4)


def render_qr_png(token: str, payload: str | None = None) -> bytes:
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("QR-токен должен состоять из 8 латинских букв или цифр")
    if payload is not None and not payload.strip():
        raise ValueError("Содержимое QR не может быть пустым")
    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_M,
        box_size=12,
        border=4,
    )
    qr.add_data(payload or f"DOOR:{token}")
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    framed = Image.new(
        "RGB",
        (qr_image.width + OUTER_BORDER_PX * 2, qr_image.height + OUTER_BORDER_PX * 2),
        OUTER_BORDER_COLOR,
    )
    framed.paste(qr_image, (OUTER_BORDER_PX, OUTER_BORDER_PX))
    output = BytesIO()
    framed.save(output, format="PNG", optimize=True)
    return output.getvalue()


def build_deep_link(bot_username: str, token: str, kind: str = "qr") -> str:
    username = bot_username.strip().lstrip("@")
    if not BOT_USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Некорректный username Telegram-бота")
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("QR-токен должен состоять из 8 латинских букв или цифр")
    if kind not in {"qr", "cour"}:
        raise ValueError("Тип deep link должен быть qr или cour")
    return f"https://t.me/{username}?start={kind}_{token}"


def build_http_link(base_url: str, token: str, kind: str = "qr") -> str:
    base_url = base_url.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_BASE_URL должен быть HTTP(S) URL без учетных данных, query и fragment")
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("QR-токен должен состоять из 8 латинских букв или цифр")
    if kind not in {"qr", "cour"}:
        raise ValueError("Тип ссылки должен быть qr или cour")
    return f"{base_url}/open/{kind}/{token}"


def extract_token(payload: str) -> str | None:
    text = payload.strip()
    if text.startswith("DOOR:"):
        token = text[5:].strip()
        return token if TOKEN_PATTERN.fullmatch(token) else None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 3 or parts[-3] != "open" or parts[-2] not in {"qr", "cour"}:
        return None
    token = parts[-1]
    return token if TOKEN_PATTERN.fullmatch(token) else None
