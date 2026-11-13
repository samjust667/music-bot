"""SQLite (WAL) persistence via aiosqlite."""
from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id          INTEGER PRIMARY KEY,
    voice_channel_id  INTEGER,
    text_channel_id   INTEGER,
    always_on         INTEGER NOT NULL DEFAULT 0,
    volume            INTEGER NOT NULL DEFAULT 50,
    loop_mode         TEXT    NOT NULL DEFAULT 'off',
    dj_role_id        INTEGER,
    idle_tracks       TEXT,
    player_channel_id INTEGER,
    player_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS queue_state (
    guild_id   INTEGER PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS playlists (
    user_id    INTEGER NOT NULL,
    name       TEXT    NOT NULL COLLATE NOCASE,
    tracks     TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    PRIMARY KEY (user_id, name)
);
CREATE TABLE IF NOT EXISTS favourites (
    user_id  INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    data     TEXT    NOT NULL,
    added_at REAL    NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""

SETTINGS_COLS = {
    "voice_channel_id", "text_channel_id", "always_on", "volume", "loop_mode",
    "dj_role_id", "idle_tracks", "player_channel_id", "player_message_id",
}


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    # ------------------------------------------------------------ settings
    async def get_settings(self, guild_id: int) -> dict:
        cur = await self.conn.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        if row:
            return dict(row)
        return {"guild_id": guild_id, "voice_channel_id": None, "text_channel_id": None, "always_on": 0,
                "volume": 50, "loop_mode": "off", "dj_role_id": None, "idle_tracks": None,
                "player_channel_id": None, "player_message_id": None}

    async def update_settings(self, guild_id: int, **fields) -> None:
        if not fields:
            return
        bad = set(fields) - SETTINGS_COLS
        if bad:
            raise ValueError(f"Unknown settings columns: {bad}")
        await self.conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE guild_settings SET {sets} WHERE guild_id=?", (*fields.values(), guild_id))
        await self.conn.commit()

    async def all_settings(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM guild_settings")
        return [dict(r) for r in await cur.fetchall()]

    # --------------------------------------------------------------- queue
    async def save_queue(self, guild_id: int, data: dict | None) -> None:
        if data is None:
            await self.conn.execute("DELETE FROM queue_state WHERE guild_id=?", (guild_id,))
        else:
            await self.conn.execute(
                "INSERT INTO queue_state (guild_id, data, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (guild_id, json.dumps(data), time.time()),
            )
        await self.conn.commit()

    async def load_queue(self, guild_id: int) -> dict | None:
        cur = await self.conn.execute("SELECT data FROM queue_state WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        return json.loads(row["data"]) if row else None

    # ----------------------------------------------------------- playlists
    async def save_playlist(self, user_id: int, name: str, tracks: list[dict]) -> None:
        await self.conn.execute(
            "INSERT INTO playlists (user_id, name, tracks, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET tracks=excluded.tracks, created_at=excluded.created_at",
            (user_id, name, json.dumps(tracks), time.time()),
        )
        await self.conn.commit()

    async def get_playlist(self, user_id: int, name: str) -> list[dict] | None:
        cur = await self.conn.execute("SELECT tracks FROM playlists WHERE user_id=? AND name=?", (user_id, name))
        row = await cur.fetchone()
        return json.loads(row["tracks"]) if row else None

    async def list_playlists(self, user_id: int) -> list[tuple[str, int]]:
        cur = await self.conn.execute("SELECT name, tracks FROM playlists WHERE user_id=? ORDER BY name", (user_id,))
        return [(r["name"], len(json.loads(r["tracks"]))) for r in await cur.fetchall()]

    async def delete_playlist(self, user_id: int, name: str) -> bool:
        cur = await self.conn.execute("DELETE FROM playlists WHERE user_id=? AND name=?", (user_id, name))
        await self.conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------- favourites
    async def add_favourite(self, user_id: int, key: str, track: dict) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO favourites (user_id, key, data, added_at) VALUES (?,?,?,?)",
            (user_id, key, json.dumps(track), time.time()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_favourites(self, user_id: int) -> list[dict]:
        cur = await self.conn.execute("SELECT data FROM favourites WHERE user_id=? ORDER BY added_at", (user_id,))
        return [json.loads(r["data"]) for r in await cur.fetchall()]

    async def remove_favourite(self, user_id: int, key: str) -> bool:
        cur = await self.conn.execute("DELETE FROM favourites WHERE user_id=? AND key=?", (user_id, key))
        await self.conn.commit()
        return cur.rowcount > 0
