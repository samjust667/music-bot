"""Spotify METADATA only (title / artist / duration / cover). Audio is matched elsewhere.

Strategy (Spotify tightened its Web API in Feb 2026, so we layer two methods):
  1. Web API with client-credentials (needs SPOTIFY_CLIENT_ID/SECRET). Uses the /items playlist endpoint.
  2. Fallback: the public embed page (open.spotify.com/embed/...), which needs no credentials and
     returns up to ~100 tracks for playlists/albums. Best-effort: Spotify can change that page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import AsyncIterator

import aiohttp

from utils import Track

log = logging.getLogger(__name__)

SPOTIFY_URL_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}(?:-[A-Za-z]+)?/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]{22})"
)
SHORT_RE = re.compile(r"https?://(?:spotify\.link|spoti\.fi|spotify\.app\.link)/\S+")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def is_spotify(text: str) -> bool:
    return bool(SPOTIFY_URL_RE.search(text) or SHORT_RE.search(text))


class SpotifyError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Collection:
    kind: str                         # track | album | playlist
    name: str
    url: str
    thumbnail: str | None
    batches: AsyncIterator[list[Track]]   # first batch is already fetched; the rest load lazily


def _artists(items) -> str:
    return ", ".join(a.get("name", "") for a in (items or []) if a.get("name")) or "Unknown artist"


def _api_track(t: dict, fallback_thumb: str | None = None) -> Track | None:
    if not t or not t.get("id") or t.get("is_local") or t.get("type") not in (None, "track"):
        return None
    imgs = (t.get("album") or {}).get("images") or []
    ms = t.get("duration_ms")
    return Track(
        title=t.get("name") or "Unknown",
        artist=_artists(t.get("artists")),
        duration=round(ms / 1000) if ms else None,
        spotify_url=(t.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/track/{t['id']}",
        thumbnail=imgs[0]["url"] if imgs else fallback_thumb,
        source="spotify",
    )


def _embed_image(entity: dict) -> str | None:
    for path in (("coverArt", "sources"), ("visualIdentity", "image"), ("images",)):
        node = entity
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, list) and node:
            best = max((i for i in node if isinstance(i, dict) and i.get("url")), key=lambda i: i.get("maxWidth") or i.get("width") or 0, default=None)
            if best:
                return best["url"]
    return None


class SpotifyClient:
    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str):
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()
        self._api_disabled_until = 0.0

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ---------------------------------------------------------------- API
    async def _get_token(self, force: bool = False) -> str:
        async with self._lock:
            if self._token and not force and time.time() < self._expires - 30:
                return self._token
            async with self.session.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=aiohttp.BasicAuth(self.client_id, self.client_secret),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    raise SpotifyError(f"Spotify auth failed ({r.status})", r.status)
                data = await r.json()
            self._token = data["access_token"]
            self._expires = time.time() + int(data.get("expires_in", 3600))
            return self._token

    async def _api(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            token = await self._get_token(force=attempt == 1)
            try:
                async with self.session.get(
                    f"https://api.spotify.com/v1/{path}", params=params,
                    headers={"Authorization": f"Bearer {token}"}, timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 401:
                        continue
                    if r.status == 429:
                        wait = int(r.headers.get("Retry-After", "2"))
                        if wait > 10:
                            raise SpotifyError("Spotify rate limit hit", 429)
                        await asyncio.sleep(wait + 0.5)
                        continue
                    if r.status != 200:
                        raise SpotifyError(f"Spotify API returned {r.status}", r.status)
                    return await r.json()
            except aiohttp.ClientError as exc:
                raise SpotifyError(f"Spotify network error: {exc}") from exc
        raise SpotifyError("Spotify API kept failing")

    # ------------------------------------------------------------ parsing
    async def parse(self, text: str) -> tuple[str, str]:
        m = SPOTIFY_URL_RE.search(text)
        if not m:
            short = SHORT_RE.search(text)
            if short:
                try:
                    async with self.session.get(short.group(0), allow_redirects=True, headers={"User-Agent": UA},
                                                timeout=aiohttp.ClientTimeout(total=15)) as r:
                        m = SPOTIFY_URL_RE.search(str(r.url)) or SPOTIFY_URL_RE.search(await r.text())
                except aiohttp.ClientError:
                    m = None
        if not m:
            raise SpotifyError("That doesn't look like a Spotify track, album or playlist link.")
        return m.group(1), m.group(2)

    # --------------------------------------------------------------- load
    async def load(self, text: str) -> Collection:
        kind, sid = await self.parse(text)
        url = f"https://open.spotify.com/{kind}/{sid}"
        if self.has_credentials and time.time() >= self._api_disabled_until:
            try:
                return await self._load_api(kind, sid, url)
            except SpotifyError as exc:
                log.warning("Spotify API failed for %s %s (%s) - trying embed fallback", kind, sid, exc)
                if exc.status in (401, 403):   # credentials rejected / restricted app: stop hammering the API
                    self._api_disabled_until = time.time() + 600
        return await self._load_embed(kind, sid, url)

    async def _load_api(self, kind: str, sid: str, url: str) -> Collection:
        if kind == "track":
            t = _api_track(await self._api(f"tracks/{sid}"))
            if not t:
                raise SpotifyError("Track unavailable", 404)
            return Collection("track", t.title, url, t.thumbnail, self._one(t))

        if kind == "album":
            album = await self._api(f"albums/{sid}")
            thumb = (album.get("images") or [{}])[0].get("url")
            first = album.get("tracks") or {}

            async def gen():
                page, offset = first, 0
                while True:
                    items = page.get("items") or []
                    tracks = [t for t in (_api_track(i, thumb) for i in items) if t]
                    for t in tracks:
                        t.thumbnail = t.thumbnail or thumb
                    if tracks:
                        yield tracks
                    if not page.get("next"):
                        return
                    offset += len(items)
                    page = await self._api(f"albums/{sid}/tracks", {"limit": 50, "offset": offset})

            return Collection("album", album.get("name") or "Album", url, thumb, gen())

        meta = await self._api(f"playlists/{sid}", {"fields": "name,images"})
        thumb = (meta.get("images") or [{}])[0].get("url")
        base = f"playlists/{sid}/items"
        try:
            first = await self._api(base, {"limit": 100, "offset": 0})
        except SpotifyError as exc:
            if exc.status != 404:
                raise
            base = f"playlists/{sid}/tracks"      # pre-2026 endpoint name
            first = await self._api(base, {"limit": 100, "offset": 0})

        async def pgen():
            page, offset = first, 0
            while True:
                items = page.get("items") or []
                tracks = [t for t in (_api_track(i.get("item") or i.get("track"), thumb) for i in items) if t]
                if tracks:
                    yield tracks
                if not page.get("next") or not items:
                    return
                offset += len(items)
                page = await self._api(base, {"limit": 100, "offset": offset})

        return Collection("playlist", meta.get("name") or "Playlist", url, thumb, pgen())

    @staticmethod
    async def _one(track: Track):
        yield [track]

    # -------------------------------------------------------------- embed
    async def _load_embed(self, kind: str, sid: str, url: str) -> Collection:
        try:
            async with self.session.get(f"https://open.spotify.com/embed/{kind}/{sid}", headers={"User-Agent": UA},
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    raise SpotifyError(f"Spotify embed returned {r.status}", r.status)
                html = await r.text()
        except aiohttp.ClientError as exc:
            raise SpotifyError(f"Couldn't reach Spotify: {exc}") from exc
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise SpotifyError("Couldn't read that Spotify link (no API credentials and the embed page changed).")
        try:
            entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SpotifyError("Couldn't read that Spotify link (embed format changed).") from exc

        thumb = _embed_image(entity)
        name = entity.get("name") or entity.get("title") or kind.title()
        if kind == "track":
            ms = entity.get("duration")
            t = Track(title=name, artist=_artists(entity.get("artists")) if entity.get("artists") else (entity.get("subtitle") or "Unknown artist"),
                      duration=round(ms / 1000) if ms else None, spotify_url=url, thumbnail=thumb, source="spotify")
            return Collection("track", name, url, thumb, self._one(t))

        tracks: list[Track] = []
        for item in entity.get("trackList") or []:
            uri = item.get("uri", "")
            if not uri.startswith("spotify:track:"):
                continue
            ms = item.get("duration")
            tracks.append(Track(
                title=item.get("title") or "Unknown",
                artist=(item.get("subtitle") or "Unknown artist").replace("\xa0", " "),
                duration=round(ms / 1000) if ms else None,
                spotify_url=f"https://open.spotify.com/track/{uri.rsplit(':', 1)[-1]}",
                thumbnail=thumb, source="spotify",
            ))
        if not tracks:
            raise SpotifyError("No playable tracks found (the playlist may be private or empty).")

        async def gen():
            yield tracks

        return Collection(kind, name, url, thumb, gen())
