"""Audio source layer. Default implementation: yt-dlp (YouTube/SoundCloud/any yt-dlp site) + ffmpeg.

The bot only talks to the small interface below, so Lavalink (lavalink_backend.py) can replace it:
    search(query, limit) -> list[Track]            text search (first configured provider that returns results)
    resolve(url, limit)  -> (name | None, [Track]) a direct link: single track or playlist
    match(track)         -> None                   find audio for a metadata-only (Spotify) track; sets track.url
    prepare(track)       -> None                   warm-up before playback (used for the next track in queue)
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import yt_dlp

import config
from utils import Track

log = logging.getLogger(__name__)

PREFIX = {"youtube": "ytsearch", "soundcloud": "scsearch"}
BAD_WORDS = ("live", "cover", "karaoke", "remix", "reverse", "slowed", "sped up", "speed up", "8d",
             "nightcore", "instrumental", "mashup", "reaction", "tutorial", "parody")


class SourceError(Exception):
    """Raised with a message that is safe to show to users."""


def friendly_error(exc: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube is blocking this server (bot check). Add YouTube cookies or use Lavalink - see the README "
                "section \"YouTube blocking\".")
    if "private video" in low:
        return "That video is private."
    if "confirm your age" in low or "age-restricted" in low or "age restricted" in low:
        return "That video is age-restricted; the bot needs logged-in cookies to play it."
    if "not available in your country" in low or "blocked it in your country" in low or "geo" in low:
        return "That video is region-locked."
    if "video unavailable" in low or "has been removed" in low or "no longer available" in low:
        return "That video is unavailable."
    if "429" in low or "too many requests" in low:
        return "The audio site is rate-limiting this server. Try again in a minute."
    if "premieres in" in low or "live event will begin" in low:
        return "That live stream/premiere hasn't started yet."
    if "unsupported url" in low:
        return "I don't know how to play that link."
    if "timed out" in low or "timeout" in low:
        return "The audio site timed out. Try again."
    return f"Couldn't load that audio ({msg.splitlines()[0][:140] if msg else 'unknown error'})."


# ------------------------------------------------------------------ matching
def _tokens(s: str) -> set[str]:
    return set(re.findall(r"\w+", s.lower()))


def score_candidate(track: Track, c: Track) -> tuple[float, int | None]:
    """Higher is better. Returns (score, duration_diff_seconds)."""
    s, diff = 0.0, None
    if track.duration and c.duration:
        diff = abs(track.duration - c.duration)
        s += 30 - min(diff, 60)
        if diff <= 3:
            s += 10
    elif not c.duration:
        s -= 10                                   # probably a live stream
    tt, ct = _tokens(track.title), _tokens(c.title)
    if tt:
        s += 25 * len(tt & ct) / len(tt)
    at = _tokens(track.artist.split(",")[0])
    if at:
        s += 10 * len(at & (ct | _tokens(c.artist))) / len(at)
    low, tlow = c.title.lower(), track.title.lower()
    for w in BAD_WORDS:
        if w in low and w not in tlow:
            s -= 20
    if c.artist.endswith(" - Topic"):
        s += 8                                    # YouTube's auto-generated "official audio" channels
    return s, diff


def pick_best(track: Track, candidates: list[Track]) -> tuple[Track | None, int | None]:
    best, best_score, best_diff = None, float("-inf"), None
    for c in candidates:
        if not c.url:
            continue
        s, d = score_candidate(track, c)
        if s > best_score:
            best, best_score, best_diff = c, s, d
    return best, best_diff


def apply_match(track: Track, cand: Track) -> None:
    track.url = cand.url
    track.source = cand.source
    track.match_duration = cand.duration
    track.stream = None


# --------------------------------------------------------------------- yt-dlp
class YtdlpSource:
    name = "yt-dlp + ffmpeg"

    def __init__(self, cookies_file: str | None = None):
        self.cookies_file = cookies_file
        self._sem = asyncio.Semaphore(3)

    def _opts(self, **extra) -> dict:
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "format": "bestaudio[acodec=opus]/bestaudio/best", "socket_timeout": 15,
            "retries": 3, "extractor_retries": 2, "source_address": "0.0.0.0",   # force IPv4 (YouTube blocks many IPv6 ranges)
            "cachedir": False,
        }
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file
        if config.YTDLP_PROXY:
            opts["proxy"] = config.YTDLP_PROXY
        opts.update(extra)
        return opts

    async def _run(self, fn, *args, timeout: float = 60):
        async with self._sem:
            try:
                return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)
            except asyncio.TimeoutError as exc:
                raise SourceError("The audio site timed out. Try again.") from exc
            except yt_dlp.utils.DownloadError as exc:
                raise SourceError(friendly_error(exc)) from exc
            except SourceError:
                raise
            except Exception as exc:
                log.exception("yt-dlp failure")
                raise SourceError(friendly_error(exc)) from exc

    # ---- entry conversion
    @staticmethod
    def _entry_track(e: dict, source: str, clean: bool = True) -> Track | None:
        url = e.get("webpage_url") or e.get("url")
        if url and not url.startswith("http") and source == "youtube":
            url = f"https://www.youtube.com/watch?v={url}"
        if not url and source == "youtube" and e.get("id"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        if not url or not e.get("title"):
            return None
        artist = e.get("artist") or e.get("channel") or e.get("uploader") or "Unknown artist"
        if clean:
            artist = re.sub(r"\s*-\s*Topic$", "", artist)
        thumb = e.get("thumbnail")
        if not thumb and e.get("thumbnails"):
            thumb = e["thumbnails"][-1].get("url")
        if not thumb and source == "youtube" and e.get("id"):
            thumb = f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg"
        dur = e.get("duration")
        return Track(title=e["title"], artist=artist, duration=int(dur) if dur else None,
                     url=url, thumbnail=thumb, source=source)

    # ---- search
    def _search_sync(self, prefix: str, query: str, limit: int) -> list[dict]:
        with yt_dlp.YoutubeDL(self._opts(extract_flat=True)) as ydl:
            info = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
        return [e for e in (info or {}).get("entries") or [] if e]

    async def search(self, query: str, limit: int = 5, provider: str | None = None,
                     clean: bool = True, timeout: float = 45) -> list[Track]:
        providers = [provider] if provider else config.SEARCH_PROVIDERS
        last: SourceError | None = None
        for prov in providers:
            try:
                entries = await self._run(self._search_sync, PREFIX[prov], query, limit, timeout=timeout)
            except SourceError as exc:
                last = exc
                continue
            tracks = [t for t in (self._entry_track(e, prov, clean) for e in entries) if t]
            if tracks:
                return tracks
        if last:
            raise last
        return []

    # ---- direct links
    def _resolve_sync(self, url: str, limit: int, playlist_ok: bool) -> dict:
        with yt_dlp.YoutubeDL(self._opts(extract_flat="in_playlist", noplaylist=not playlist_ok, playlistend=limit)) as ydl:
            return ydl.extract_info(url, download=False)

    async def resolve(self, url: str, limit: int = 100) -> tuple[str | None, list[Track]]:
        yt_video = any(s in url for s in ("watch?v=", "youtu.be/", "/shorts/"))
        playlist_ok = (not yt_video) and bool(re.search(r"[?&]list=|/playlist|/sets/|/albums?/", url))
        info = await self._run(self._resolve_sync, url, limit, playlist_ok, timeout=90)
        if not info:
            raise SourceError("Nothing found at that link.")
        if info.get("entries"):
            src = "soundcloud" if "soundcloud" in (info.get("extractor_key") or "").lower() else "youtube"
            tracks = [t for t in (self._entry_track(e, src) for e in info["entries"] if e) if t]
            if not tracks:
                raise SourceError("That playlist has no playable tracks.")
            return info.get("title"), tracks
        ext = (info.get("extractor_key") or "").lower()
        src = "soundcloud" if "soundcloud" in ext else "youtube" if "youtube" in ext else "url"
        track = self._entry_track({**info, "url": None}, src) or Track(title=info.get("title") or "Unknown", url=url, source=src)
        track.url = info.get("webpage_url") or url
        if info.get("is_live"):
            track.duration = None
        if info.get("url"):                       # we already have a stream URL: cache it for ~20 minutes
            track.stream = (info["url"], info.get("http_headers") or {}, time.time() + 1200)
        return None, [track]

    # ---- matching (Spotify -> audio)
    async def match(self, track: Track) -> None:
        if track.url:
            return
        best_overall, best_diff = None, None
        for prov in config.SEARCH_PROVIDERS:
            try:
                cands = await self.search(track.search_query, limit=6, provider=prov, clean=False)
            except SourceError as exc:
                log.warning("Search on %s failed for %r: %s", prov, track.search_query, exc)
                continue
            best, diff = pick_best(track, cands)
            if best is None:
                continue
            if best_overall is None or (diff is not None and (best_diff is None or diff < best_diff)):
                best_overall, best_diff = best, diff
            if diff is not None and diff <= 8:
                break
        if best_overall is None:
            raise SourceError(f"No audio found for “{track.search_query}”.")
        apply_match(track, best_overall)

    # ---- streaming
    def _stream_sync(self, url: str) -> tuple[str, dict]:
        with yt_dlp.YoutubeDL(self._opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
        if not info:
            raise SourceError("Nothing playable at that link.")
        stream = info.get("url")
        if not stream:
            fmts = [f for f in info.get("formats") or [] if f.get("acodec") not in (None, "none") and f.get("url")]
            if not fmts:
                raise SourceError("No audio stream found for that link.")
            stream = fmts[-1]["url"]
        return stream, info.get("http_headers") or {}

    async def stream_info(self, track: Track) -> tuple[str, dict]:
        if track.stream and track.stream[2] > time.time():
            return track.stream[0], track.stream[1]
        if not track.url:
            await self.match(track)
        stream, headers = await self._run(self._stream_sync, track.url, timeout=90)
        track.stream = (stream, headers, time.time() + 1200)
        return stream, headers

    async def prepare(self, track: Track) -> None:
        await self.stream_info(track)
