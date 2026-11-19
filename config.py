from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent


class ConfigError(ValueError):
    """Ошибка конфигурации приложения."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана обязательная переменная окружения {name}")
    return value


def _path(name: str, default: str) -> Path:
    raw_value = os.getenv(name, default).strip()
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def _integer(name: str, default: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        if default is None:
            raise ConfigError(f"Не задана обязательная переменная окружения {name}")
        return default
    try:
        return int(raw_value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом") from exc


def _positive_integer(name: str, default: int) -> int:
    value = _integer(name, default)
    if value <= 0:
        raise ConfigError(f"{name} должен быть больше нуля")
    return value


def _positive_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть числом") from exc
    if value <= 0:
        raise ConfigError(f"{name} должен быть больше нуля")
    return value


def _parse_admin_ids() -> frozenset[int]:
    raw_value = _required("ADMIN_IDS")
    admin_ids: set[int] = set()
    for item in raw_value.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            admin_ids.add(int(item))
        except ValueError as exc:
            raise ConfigError(f"Некорректный Telegram ID в ADMIN_IDS: {item}") from exc
    if not admin_ids:
        raise ConfigError("ADMIN_IDS должен содержать хотя бы один Telegram ID")
    return frozenset(admin_ids)


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    actuator_mode: str
    bot_token: str
    bot_username: str
    admin_ids: frozenset[int]
    log_chat_id: int | None
    dahua_ip: str
    dahua_user: str
    dahua_password: str
    dahua_rtsp_port: int
    dahua_channel: int
    actuator_open_ttl_seconds: int
    actuator_timeout_seconds: int
    database_path: Path
    media_dir: Path
    face_tasks_dir: Path
    temp_media_dir: Path
    rtsp_fps: int
    pre_roll_seconds: int
    post_roll_seconds: int
    live_clip_seconds: int
    face_analysis_interval: float
    face_similarity_threshold: float
    face_cooldown_seconds: float
    door_unlock_cooldown_seconds: float
    courier_face_ttl_seconds: int
    public_base_url: str
    http_host: str
    http_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        app_env = os.getenv("APP_ENV", "test").strip().lower()
        if app_env not in {"test", "pilot", "production"}:
            raise ConfigError("APP_ENV должен быть test, pilot или production")
        actuator_mode = os.getenv("ACTUATOR_MODE", "test").strip().lower()
        if actuator_mode not in {"test", "dahua"}:
            raise ConfigError("ACTUATOR_MODE должен быть test или dahua")
        if app_env == "test" and actuator_mode == "dahua":
            raise ConfigError("ACTUATOR_MODE=dahua запрещен при APP_ENV=test")

        dahua_ip = os.getenv("DAHUA_IP", "").strip()
        if "://" in dahua_ip or "/" in dahua_ip:
            raise ConfigError("DAHUA_IP должен содержать только адрес устройства, без схемы и /")
        if actuator_mode == "dahua":
            if not dahua_ip:
                raise ConfigError("Не задана обязательная переменная окружения DAHUA_IP")
            dahua_user = _required("DAHUA_USER")
            dahua_password = _required("DAHUA_PASS")
        else:
            dahua_user = os.getenv("DAHUA_USER", "").strip()
            dahua_password = os.getenv("DAHUA_PASS", "").strip()

        log_chat_id_raw = os.getenv("LOG_CHAT_ID", "").strip()
        log_chat_id = _integer("LOG_CHAT_ID") if log_chat_id_raw else None
        similarity = _positive_float("FACE_SIMILARITY_THRESHOLD", 0.48)
        if similarity > 1.0:
            raise ConfigError("FACE_SIMILARITY_THRESHOLD не может быть больше 1.0")
        public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if public_base_url:
            parsed_url = urlsplit(public_base_url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.netloc
                or parsed_url.username is not None
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ConfigError(
                    "PUBLIC_BASE_URL должен быть HTTP(S) URL без учетных данных, query и fragment"
                )
        http_host = os.getenv("HTTP_HOST", "0.0.0.0").strip()
        if not http_host:
            raise ConfigError("HTTP_HOST не может быть пустым")
        return cls(
            app_env=app_env,
            actuator_mode=actuator_mode,
            bot_token=_required("BOT_TOKEN"),
            bot_username=os.getenv("BOT_USERNAME", "").strip().lstrip("@"),
            admin_ids=_parse_admin_ids(),
            log_chat_id=log_chat_id,
            dahua_ip=dahua_ip,
            dahua_user=dahua_user,
            dahua_password=dahua_password,
            dahua_rtsp_port=_positive_integer("DAHUA_RTSP_PORT", 554),
            dahua_channel=_positive_integer("DAHUA_CHANNEL", 1),
            actuator_open_ttl_seconds=_positive_integer("ACTUATOR_OPEN_TTL_SECONDS", 5),
            actuator_timeout_seconds=_positive_integer("ACTUATOR_TIMEOUT_SECONDS", 10),
            database_path=_path("DATABASE_PATH", "data/access.db"),
            media_dir=_path("MEDIA_DIR", "data/media"),
            face_tasks_dir=_path("FACE_TASKS_DIR", "data/face_tasks"),
            temp_media_dir=_path("TEMP_MEDIA_DIR", tempfile.gettempdir()),
            rtsp_fps=_positive_integer("RTSP_FPS", 10),
            pre_roll_seconds=_positive_integer("PRE_ROLL_SECONDS", 3),
            post_roll_seconds=_positive_integer("POST_ROLL_SECONDS", 5),
            live_clip_seconds=_positive_integer("LIVE_CLIP_SECONDS", 4),
            face_analysis_interval=_positive_float("FACE_ANALYSIS_INTERVAL", 0.2),
            face_similarity_threshold=similarity,
            face_cooldown_seconds=_positive_float("FACE_COOLDOWN_SECONDS", 20.0),
            door_unlock_cooldown_seconds=_positive_float("DOOR_UNLOCK_COOLDOWN_SECONDS", 8.0),
            courier_face_ttl_seconds=_positive_integer("COURIER_FACE_TTL_SECONDS", 600),
            public_base_url=public_base_url,
            http_host=http_host,
            http_port=_positive_integer("HTTP_PORT", 8080),
        )

    @property
    def dahua_base_url(self) -> str:
        return f"http://{self.dahua_ip}"

    @property
    def rtsp_url(self) -> str:
        user = quote(self.dahua_user, safe="")
        password = quote(self.dahua_password, safe="")
        return (
            f"rtsp://{user}:{password}@{self.dahua_ip}:{self.dahua_rtsp_port}"
            f"/cam/realmonitor?channel={self.dahua_channel}&subtype=0"
        )

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.face_tasks_dir.mkdir(parents=True, exist_ok=True)
        self.temp_media_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    settings = Settings.from_env()
    settings.ensure_directories()
    return settings
