"""Загрузка настроек: секреты из .env / переменных окружения, остальное из config.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

SUMMARY_DEFAULTS: dict = {
    "language": "русский",
    "timezone": "Europe/Moscow",
    "max_messages": 3000,
    "max_words_per_day": 200,
    "chunk_chars": 60000,
    "max_message_chars": 1500,
    "extra_instructions": "",
    "include_links": True,           # ссылки из переписки попадают в выжимку
    "include_message_links": True,   # ссылки на сообщения чата (t.me/...) у важных тем
    "max_media_per_day": 3,          # сколько вложений приложить к выжимке одного дня (0 — отключить)
    "max_media_mb": 20,              # вложения крупнее пропускаются
    "min_messages_per_day": 100,     # день, в котором сообщений меньше, объединяется со следующим (0 — не объединять)
}


@dataclass
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    allowed_user_ids: set[int]
    llm: dict
    summary: dict
    sessions_dir: Path
    login_method: str = "qr"  # "qr" | "phone"
    mark_as_read: bool = True  # после выжимки непрочитанных отметить их прочитанными


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


def load_settings() -> Settings:
    load_dotenv()

    config_path = Path(os.environ.get("CONFIG_PATH", "config.toml"))
    if not config_path.exists():
        raise SystemExit(f"Не найден файл настроек {config_path}")
    with config_path.open("rb") as f:
        cfg = tomllib.load(f)

    try:
        api_id = int(_require("TELEGRAM_API_ID"))
        allowed = {
            int(x) for x in _require("ALLOWED_USER_IDS").replace(" ", "").split(",") if x
        }
    except ValueError:
        raise SystemExit("TELEGRAM_API_ID и ALLOWED_USER_IDS должны быть числами") from None

    sessions_dir = Path(os.environ.get("SESSIONS_DIR", "sessions"))
    sessions_dir.mkdir(parents=True, exist_ok=True)

    login_method = str(cfg.get("telegram", {}).get("login", "qr")).lower()
    if login_method not in ("qr", "phone"):
        raise SystemExit('[telegram].login в config.toml должен быть "qr" или "phone"')

    mark_as_read = cfg.get("telegram", {}).get("mark_as_read", True)
    if not isinstance(mark_as_read, bool):
        raise SystemExit("[telegram].mark_as_read в config.toml должен быть true или false")

    min_per_day = SUMMARY_DEFAULTS["min_messages_per_day"]
    min_per_day = cfg.get("summary", {}).get("min_messages_per_day", min_per_day)
    if isinstance(min_per_day, bool) or not isinstance(min_per_day, int) or min_per_day < 0:
        raise SystemExit("[summary].min_messages_per_day в config.toml должен быть целым числом >= 0")

    return Settings(
        api_id=api_id,
        api_hash=_require("TELEGRAM_API_HASH"),
        bot_token=_require("BOT_TOKEN"),
        allowed_user_ids=allowed,
        llm=cfg.get("llm", {}),
        summary={**SUMMARY_DEFAULTS, **cfg.get("summary", {})},
        sessions_dir=sessions_dir,
        login_method=login_method,
        mark_as_read=mark_as_read,
    )
