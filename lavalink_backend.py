"""Optional Lavalink audio layer (via wavelink 3.x). Enabled when LAVALINK_URI + LAVALINK_PASSWORD are set.

Lavalink does the audio fetching/encoding/voice transport on its own server, so YouTube blocking and
Discord's voice encryption (DAVE) are handled there - use a Lavalink build that supports DAVE and a
YouTube plugin that works from your server's IP (see README).
"""
from __future__ import annotations

import logging

import discord

from player import Backend, NotConnected
from sources import PREFIX, SourceError, apply_match, pick_best
from utils import Track

try:
    import wavelink
except ImportError:          # pragma: no cover
    wavelink = None

log = logging.getLogger(__name__)


def _to_track(p, source: str) -> Track:
    return Track(title=p.title, artist=p.author or "Unknown artist",
                 duration=int(p.length // 1000) if p.length and not p.is_stream else None,
                 url=p.uri, thumbnail=getattr(p, "artwork", None), source=source)


class LavalinkSource:
    name = "Lavalink"

    async def _search(self, query: str, provider: str):
        src = wavelink.TrackSource.SoundCloud if provider == "soundcloud" else wavelink.TrackSource.YouTube
        try:
            res = await wavelink.Playable.search(query, source=src)
        except wavelink.WavelinkException as exc:
            raise SourceError(f"Lavalink search failed: {exc}") from exc
        return res.tracks if isinstance(res, wavelink.Playlist) else list(res)

    async def search(self, query: str, limit: int = 5, provider: str | None = None, clean: bool = True,
                     timeout: float = 20) -> list[Track]:
        import config
        last = None
        for prov in ([provider] if provider else config.SEARCH_PROVIDERS):
            try:
                found = await self._search(query, prov)
            except SourceError as exc:
                last = exc
                continue
            if found:
                return [_to_track(p, prov) for p in found[:limit]]
        if last:
            raise last
        return []

    async def resolve(self, url: str, limit: int = 100) -> tuple[str | None, list[Track]]:
        try:
            res = await wavelink.Playable.search(url)
        except wavelink.WavelinkException as exc:
            raise SourceError(f"Lavalink couldn't load that link: {exc}") from exc
        if isinstance(res, wavelink.Playlist):
            return res.name, [_to_track(p, "youtube") for p in res.tracks[:limit]]
        if not res:
            raise SourceError("Nothing found at that link.")
        t = _to_track(res[0], "url")
        t.url = t.url or url
        return None, [t]

    async def match(self, track: Track) -> None:
        import config
        if track.url:
            return
        best_overall, best_diff = None, None
        for prov in config.SEARCH_PROVIDERS:
            try:
                cands = await self.search(track.search_query, limit=6, provider=prov, clean=False)
            except SourceError:
                continue
            best, diff = pick_best(track, cands)
            if best and (best_overall is None or (diff is not None and (best_diff is None or diff < best_diff))):
                best_overall, best_diff = best, diff
            if diff is not None and diff <= 8:
                break
        if best_overall is None:
            raise SourceError(f"No audio found for “{track.search_query}”.")
        apply_match(track, best_overall)

    async def prepare(self, track: Track) -> None:
        await self.match(track)


class LavalinkBackend(Backend):
    name = "Lavalink"

    def __init__(self, source: LavalinkSource):
        self.source = source

    @staticmethod
    def _vc(player):
        vc = player.guild.voice_client
        return vc if wavelink and isinstance(vc, wavelink.Player) else None

    async def connect(self, player, channel):
        vc = self._vc(player)
        if vc is None:
            old = player.guild.voice_client
            if old:
                await old.disconnect(force=True)
            vc = await channel.connect(cls=wavelink.Player, self_deaf=True, timeout=30.0)
        elif vc.channel and vc.channel.id != channel.id:
            await vc.move_to(channel)
        vc.autoplay = wavelink.AutoPlayMode.disabled
        await vc.set_volume(player.volume)

    async def disconnect(self, player):
        vc = self._vc(player)
        if vc:
            await vc.disconnect()

    def connected(self, player):
        vc = self._vc(player)
        return bool(vc and vc.connected)

    def channel(self, player):
        vc = self._vc(player)
        return vc.channel if vc and vc.connected else None

    async def play(self, player, track, start, gen):
        vc = self._vc(player)
        if not vc or not vc.connected:
            raise NotConnected()
        if not track.url:
            await self.source.match(track)
        try:
            res = await wavelink.Playable.search(track.url)
        except wavelink.WavelinkException as exc:
            raise SourceError(f"Lavalink couldn't load this track: {exc}") from exc
        found = res.tracks if isinstance(res, wavelink.Playlist) else list(res)
        if not found:
            raise SourceError("Lavalink found nothing for this track.")
        await vc.play(found[0], start=int(start * 1000), volume=player.volume, paused=False)

    async def pause(self, player):
        vc = self._vc(player)
        if vc:
            await vc.pause(True)

    async def resume(self, player):
        vc = self._vc(player)
        if vc:
            await vc.pause(False)

    async def stop(self, player):
        vc = self._vc(player)
        if vc and vc.playing:
            await vc.stop()

    async def set_volume(self, player, volume):
        vc = self._vc(player)
        if vc:
            await vc.set_volume(volume)


async def connect_node(bot, uri: str, password: str) -> bool:
    """Connect to Lavalink. Returns False (and logs) if wavelink is missing or the node is unreachable."""
    if wavelink is None:
        log.error("LAVALINK_URI is set but wavelink is not installed.")
        return False
    try:
        node = wavelink.Node(uri=uri, password=password, inactive_player_timeout=None)
        await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=100)
        return True
    except Exception as exc:
        log.error("Could not connect to Lavalink (%s): %s - falling back to yt-dlp + ffmpeg", uri, exc)
        return False
