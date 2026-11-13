"""Configuration, read from environment variables (and a local .env file)."""
from __future__ import annotations

import base64
import logging
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    return default if not raw else raw.lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: list[str]) -> list[str]:
    raw = _str(name)
    return [p.strip().lower() for p in raw.split(",") if p.strip()] if raw else default


DISCORD_TOKEN = _str("DISCORD_TOKEN")
DEV_GUILD_ID = _int("GUILD_ID", 0) or None

DATA_DIR = Path(_str("DATA_DIR", "data"))
DB_PATH = Path(_str("DB_PATH") or DATA_DIR / "musicbot.db")

SPOTIFY_CLIENT_ID = _str("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = _str("SPOTIFY_CLIENT_SECRET")

SEARCH_PROVIDERS = [p for p in _list("SEARCH_PROVIDERS", ["youtube", "soundcloud"]) if p in {"youtube", "soundcloud"}] or ["youtube"]
YTDLP_COOKIES_FILE = _str("YTDLP_COOKIES_FILE")
YTDLP_COOKIES_B64 = _str("YTDLP_COOKIES_B64")
YTDLP_PROXY = _str("YTDLP_PROXY")
FFMPEG_PATH = _str("FFMPEG_PATH", "ffmpeg")

LAVALINK_URI = _str("LAVALINK_URI")
LAVALINK_PASSWORD = _str("LAVALINK_PASSWORD")
USE_LAVALINK = bool(LAVALINK_URI and LAVALINK_PASSWORD)

DEFAULT_VOLUME = max(0, min(100, _int("DEFAULT_VOLUME", 50)))
MAX_QUEUE = max(10, _int("MAX_QUEUE", 500))
IDLE_TIMEOUT = _int("IDLE_TIMEOUT", 300)      # seconds idle before leaving (non-24/7 only)
EMPTY_TIMEOUT = _int("EMPTY_TIMEOUT", 120)    # seconds alone in channel before leaving (non-24/7 only)
PAUSE_WHEN_EMPTY = _bool("PAUSE_WHEN_EMPTY", True)
AUTOCOMPLETE = _bool("AUTOCOMPLETE", True)
LOG_LEVEL = _str("LOG_LEVEL", "INFO").upper()


def prepare_cookies() -> str | None:
    """Return a writable cookies.txt path for yt-dlp (it rewrites the file), or None."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / "cookies.txt"
    try:
        if YTDLP_COOKIES_B64:
            target.write_bytes(base64.b64decode(YTDLP_COOKIES_B64))
            return str(target)
        if YTDLP_COOKIES_FILE:
            src = Path(YTDLP_COOKIES_FILE)
            if src.is_file():
                if src.resolve() != target.resolve():
                    shutil.copyfile(src, target)
                return str(target)
            log.warning("YTDLP_COOKIES_FILE=%s does not exist; ignoring.", src)
    except Exception:
        log.exception("Could not prepare yt-dlp cookies")
    return None
