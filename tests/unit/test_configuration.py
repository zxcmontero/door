from __future__ import annotations

from unittest.mock import Mock

import pytest

import config
from config import ConfigError, Settings


ENV_NAMES = (
    "APP_ENV",
    "ACTUATOR_MODE",
    "BOT_TOKEN",
    "BOT_USERNAME",
    "ADMIN_IDS",
    "LOG_CHAT_ID",
    "DAHUA_IP",
    "DAHUA_USER",
    "DAHUA_PASS",
    "DAHUA_RTSP_PORT",
    "DAHUA_CHANNEL",
    "ACTUATOR_OPEN_TTL_SECONDS",
    "ACTUATOR_TIMEOUT_SECONDS",
    "DATABASE_PATH",
    "MEDIA_DIR",
    "FACE_TASKS_DIR",
    "TEMP_MEDIA_DIR",
    "RTSP_FPS",
    "PRE_ROLL_SECONDS",
    "POST_ROLL_SECONDS",
    "LIVE_CLIP_SECONDS",
    "FACE_ANALYSIS_INTERVAL",
    "FACE_SIMILARITY_THRESHOLD",
    "FACE_COOLDOWN_SECONDS",
    "DOOR_UNLOCK_COOLDOWN_SECONDS",
    "COURIER_FACE_TTL_SECONDS",
    "PUBLIC_BASE_URL",
    "HTTP_HOST",
    "HTTP_PORT",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "synthetic-bot-token")
    monkeypatch.setenv("ADMIN_IDS", "123456")


def enable_dahua(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "pilot")
    monkeypatch.setenv("ACTUATOR_MODE", "dahua")
    monkeypatch.setenv("DAHUA_IP", "192.0.2.10")
    monkeypatch.setenv("DAHUA_USER", "synthetic-user")
    monkeypatch.setenv("DAHUA_PASS", "synthetic-password")


def test_safe_defaults_do_not_require_dahua_credentials() -> None:
    settings = Settings.from_env()

    assert settings.app_env == "test"
    assert settings.actuator_mode == "test"
    assert settings.actuator_open_ttl_seconds == 5
    assert settings.actuator_timeout_seconds == 10
    assert settings.dahua_channel == 1
    assert settings.dahua_ip == ""
    assert settings.dahua_user == ""
    assert settings.dahua_password == ""


def test_explicit_dahua_configuration_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_dahua(monkeypatch)
    monkeypatch.setenv("DAHUA_CHANNEL", "2")
    monkeypatch.setenv("ACTUATOR_OPEN_TTL_SECONDS", "6")
    monkeypatch.setenv("ACTUATOR_TIMEOUT_SECONDS", "4")

    settings = Settings.from_env()

    assert settings.app_env == "pilot"
    assert settings.actuator_mode == "dahua"
    assert settings.dahua_channel == 2
    assert settings.actuator_open_ttl_seconds == 6
    assert settings.actuator_timeout_seconds == 4
    assert "channel=2" in settings.rtsp_url


def test_dahua_is_forbidden_in_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_dahua(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")

    with pytest.raises(ConfigError, match="APP_ENV=test"):
        Settings.from_env()


@pytest.mark.parametrize("missing_name", ["DAHUA_IP", "DAHUA_USER", "DAHUA_PASS"])
def test_dahua_mode_requires_connection_secrets(
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
) -> None:
    enable_dahua(monkeypatch)
    monkeypatch.delenv(missing_name)

    with pytest.raises(ConfigError, match=missing_name) as error:
        Settings.from_env()

    assert "synthetic-password" not in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_ENV", "unknown"),
        ("ACTUATOR_MODE", "unknown"),
        ("ACTUATOR_OPEN_TTL_SECONDS", "0"),
        ("ACTUATOR_OPEN_TTL_SECONDS", "-1"),
        ("ACTUATOR_OPEN_TTL_SECONDS", "1.5"),
        ("ACTUATOR_OPEN_TTL_SECONDS", "invalid"),
        ("ACTUATOR_TIMEOUT_SECONDS", "0"),
        ("ACTUATOR_TIMEOUT_SECONDS", "invalid"),
        ("DAHUA_CHANNEL", "0"),
        ("DAHUA_CHANNEL", "invalid"),
    ],
)
def test_invalid_actuator_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("dahua_ip", ["http://192.0.2.10", "192.0.2.10/"])
def test_dahua_ip_rejects_scheme_and_slashes(
    monkeypatch: pytest.MonkeyPatch,
    dahua_ip: str,
) -> None:
    monkeypatch.setenv("DAHUA_IP", dahua_ip)

    with pytest.raises(ConfigError, match="DAHUA_IP"):
        Settings.from_env()


def test_from_env_does_not_load_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dotenv = Mock(side_effect=AssertionError(".env must not be read"))
    monkeypatch.setattr(config, "load_dotenv", load_dotenv)

    Settings.from_env()

    load_dotenv.assert_not_called()
