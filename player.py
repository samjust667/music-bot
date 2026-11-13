"""Per-guild playback engine: queue, loop modes, 24/7 reconnect supervision, state persistence.

Backends are swappable: FFmpegBackend (discord.py voice + yt-dlp) lives here, LavalinkBackend in
lavalink_backend.py. GuildPlayer never touches either library directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import shlex
import time
from abc import ABC, abstractmethod

import discord

import config
from sources import SourceError
from utils import Track, error_embed

log = logging.getLogger(__name__)
LOOP_MODES = ("off", "track", "queue")


class NotConnected(Exception):
    pass


# ------------------------------------------------------------------ backends
class Backend(ABC):
    name = "backend"

    @abstractmethod
    async def connect(self, player: "GuildPlayer", channel: discord.VoiceChannel) -> None: ...
    @abstractmethod
    async def disconnect(self, player: "GuildPlayer") -> None: ...
    @abstractmethod
    def connected(self, player: "GuildPlayer") -> bool: ...
    @abstractmethod
    def channel(self, player: "GuildPlayer") -> discord.abc.Connectable | None: ...
    @abstractmethod
    async def play(self, player: "GuildPlayer", track: Track, start: float, gen: int) -> None: ...
    @abstractmethod
    async def pause(self, player: "GuildPlayer") -> None: ...
    @abstractmethod
    async def resume(self, player: "GuildPlayer") -> None: ...
    @abstractmethod
    async def stop(self, player: "GuildPlayer") -> None: ...
    @abstractmethod
    async def set_volume(self, player: "GuildPlayer", volume: int) -> None: ...


class FFmpegBackend(Backend):
    name = "yt-dlp + ffmpeg"

    def __init__(self, source):
        self.source = source

    async def connect(self, player, channel):
        vc = player.guild.voice_client
        if vc is not None and not isinstance(vc, discord.VoiceClient):
            await vc.disconnect(force=True)       # leftover Lavalink player from a previous backend
            vc = None
        if vc is not None and not vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None
        if vc is None:
            await channel.connect(timeout=30.0, reconnect=True, self_deaf=True)
        elif vc.channel.id != channel.id:
            await vc.move_to(channel)

    async def disconnect(self, player):
        vc = player.guild.voice_client
        if vc:
            await vc.disconnect(force=True)

    def connected(self, player):
        vc = player.guild.voice_client
        return bool(vc and vc.is_connected())

    def channel(self, player):
        vc = player.guild.voice_client
        return vc.channel if vc and vc.is_connected() else None

    async def play(self, player, track, start, gen):
        vc = player.guild.voice_client
        if not vc or not vc.is_connected():
            raise NotConnected()
        url, headers = await self.source.stream_info(track)
        before = ["-nostdin", "-reconnect", "1", "-reconnect_streaming", "1", "-reconnect_delay_max", "5"]
        # Only forward headers ffmpeg should send (Accept-Encoding etc. would make servers gzip the stream).
        keep = {"user-agent", "referer", "origin", "cookie", "authorization"}
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items() if k.lower() in keep)
        if hdr:
            before += ["-headers", hdr]
        if start and start > 0:
            before += ["-ss", str(int(start))]
        audio = discord.FFmpegPCMAudio(url, executable=config.FFMPEG_PATH,
                                       before_options=" ".join(shlex.quote(p) for p in before), options="-vn")
        audio = discord.PCMVolumeTransformer(audio, volume=player.volume / 100)
        vc = player.guild.voice_client                      # re-check after the (slow) extraction
        if not vc or not vc.is_connected():
            raise NotConnected()
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            asyncio.run_coroutine_threadsafe(player.on_track_end(gen, error), loop)

        vc.play(audio, after=after)

    async def pause(self, player):
        vc = player.guild.voice_client
        if vc:
            vc.pause()

    async def resume(self, player):
        vc = player.guild.voice_client
        if vc:
            vc.resume()

    async def stop(self, player):
        vc = player.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    async def set_volume(self, player, volume):
        vc = player.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume / 100


# -------------------------------------------------------------------- player
class GuildPlayer:
    def __init__(self, bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.guild_id = guild.id
        # persisted settings
        self.voice_channel_id: int | None = None
        self.text_channel_id: int | None = None       # channel chosen with /setup
        self.always_on = False
        self.volume = config.DEFAULT_VOLUME
        self.loop_mode = "off"
        self.dj_role_id: int | None = None
        self.idle_tracks: list[Track] = []
        self.player_channel_id: int | None = None
        self.player_message_id: int | None = None
        # runtime
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.paused = False
        self.last_channel_id: int | None = None       # channel of the last command (used when /setup wasn't run)
        self.intentional_leave = False
        self._offset = 0.0
        self._t0: float | None = None
        self._gen = 0
        self._lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()
        self._loaded = False
        self._advancing = False
        self._interrupted = False
        self._auto_paused = False
        self._idle_suppressed = False
        self._idle_blocked_until = 0.0
        self._retries = 0
        self._fail_streak = 0
        self._attempts = 0
        self._next_attempt = 0.0
        self._empty_since: float | None = None
        self._idle_since = time.monotonic()
        self._last_save = 0.0

    # ------------------------------------------------------------ properties
    @property
    def backend(self) -> Backend:
        return self.bot.backend

    @property
    def target_channel_id(self) -> int | None:
        return self.text_channel_id or self.last_channel_id or self.player_channel_id

    @property
    def position(self) -> float:
        if self._t0 is None:
            return self._offset
        return self._offset + (time.monotonic() - self._t0)

    @property
    def is_playing(self) -> bool:
        return self.current is not None and not self.paused and self._t0 is not None

    def voice_channel(self):
        return self.backend.channel(self)

    def humans(self) -> int:
        ch = self.voice_channel()
        return sum(1 for m in ch.members if not m.bot) if ch else 0

    # --------------------------------------------------------------- loading
    async def ensure_loaded(self) -> None:
        async with self._load_lock:
            if self._loaded:
                return
            s = await self.bot.db.get_settings(self.guild_id)
            self.voice_channel_id = s["voice_channel_id"]
            self.text_channel_id = s["text_channel_id"]
            self.always_on = bool(s["always_on"])
            self.volume = int(s["volume"])
            self.loop_mode = s["loop_mode"] if s["loop_mode"] in LOOP_MODES else "off"
            self.dj_role_id = s["dj_role_id"]
            self.player_channel_id = s["player_channel_id"]
            self.player_message_id = s["player_message_id"]
            if s["idle_tracks"]:
                try:
                    self.idle_tracks = [Track.from_dict(d) for d in json.loads(s["idle_tracks"])]
                except (ValueError, TypeError):
                    log.warning("Bad idle playlist JSON for guild %s", self.guild_id)
            raw = await self.bot.db.load_queue(self.guild_id)
            if raw:
                try:
                    self.queue = [Track.from_dict(d) for d in raw.get("queue", [])]
                    cur = raw.get("current")
                    self.current = Track.from_dict(cur) if cur else None
                    self._offset = float(raw.get("position") or 0)
                    self._interrupted = self.current is not None
                except (ValueError, TypeError):
                    log.warning("Bad saved queue for guild %s", self.guild_id)
            self._loaded = True

    def save_state(self) -> None:
        self.bot.spawn(self._save_state())

    async def _save_state(self) -> None:
        try:
            if self.current is None and not self.queue:
                await self.bot.db.save_queue(self.guild_id, None)
            else:
                await self.bot.db.save_queue(self.guild_id, {
                    "current": self.current.to_dict() if self.current else None,
                    "position": round(self.position, 1),
                    "queue": [t.to_dict() for t in self.queue],
                })
            self._last_save = time.monotonic()
        except Exception:
            log.exception("Failed to save queue for guild %s", self.guild_id)

    async def save_setting(self, **fields) -> None:
        await self.bot.db.update_settings(self.guild_id, **fields)

    def notify_update(self) -> None:
        self.bot.dispatch("music_update", self.guild_id)

    async def notify(self, text: str) -> None:
        ch = self.guild.get_channel(self.target_channel_id) if self.target_channel_id else None
        if ch:
            try:
                await ch.send(embed=error_embed(text))
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------ connection
    async def connect(self, channel: discord.VoiceChannel) -> None:
        self.intentional_leave = False
        await self.backend.connect(self, channel)
        self.voice_channel_id = channel.id
        self._attempts = 0
        self._idle_since = time.monotonic()
        await self.save_setting(voice_channel_id=channel.id)

    async def leave(self) -> None:
        """User-requested (or idle/give-up) disconnect: forget the session."""
        self.intentional_leave = True
        self._gen += 1
        self.queue.clear()
        self.current = None
        self.paused = False
        self._offset, self._t0 = 0.0, None
        try:
            await self.backend.disconnect(self)
        except Exception:
            log.exception("Error while disconnecting")
        self.voice_channel_id = None
        await self.save_setting(voice_channel_id=None)
        await self._save_state()
        self.notify_update()

    def wants_connection(self) -> bool:
        return bool(self.voice_channel_id) and not self.intentional_leave and (
            self.always_on or self.current is not None or bool(self.queue))

    async def reconnect(self) -> None:
        ch = self.guild.get_channel(self.voice_channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(self.voice_channel_id)
            except discord.NotFound:
                log.warning("Saved voice channel %s no longer exists (guild %s)", self.voice_channel_id, self.guild_id)
                self.voice_channel_id, self.always_on = None, False
                await self.save_setting(voice_channel_id=None, always_on=0)
                return
        await self.backend.connect(self, ch)
        self._attempts = 0
        log.info("Reconnected to voice channel %s (guild %s)", ch.id, self.guild_id)
        if self.current:
            await self._resume_current()
        else:
            self.kick()

    async def _resume_current(self) -> None:
        cur, was_paused = self.current, self.paused
        if cur is None:
            return
        async with self._lock:
            try:
                await self._start(cur, start=self._offset)
            except NotConnected:
                return
            except SourceError as exc:
                cur.failed = True
                await self.notify(f"Couldn't resume **{cur.title}**: {exc}")
                self.current = None
        if self.current is None:
            await self.advance()
        elif was_paused:
            await self.pause()

    # ----------------------------------------------------------------- queue
    def add(self, tracks: list[Track], index: int | None = None) -> tuple[int, int]:
        """Insert tracks. Returns (start index in queue, number actually added)."""
        room = max(config.MAX_QUEUE - len(self.queue), 0)
        tracks = tracks[:room]
        if index is None:
            start = len(self.queue)
            self.queue.extend(tracks)
        else:
            start = min(index, len(self.queue))
            self.queue[start:start] = tracks
        self._idle_suppressed = False
        if tracks:
            self.save_state()
            self.notify_update()
            self.kick()
        return start, len(tracks)

    def kick(self) -> None:
        """Start playback in the background if nothing is playing."""
        if self.current is None and not self._advancing and not self.bot.closing and self.backend.connected(self):
            self._advancing = True
            self.bot.spawn(self._advance_task())

    async def _advance_task(self) -> None:
        try:
            await self.advance()
        finally:
            self._advancing = False

    def _pick_next(self, manual: bool) -> Track | None:
        cur = self.current
        if cur and self.loop_mode == "track" and not manual and not cur.failed:
            return cur
        if cur and self.loop_mode == "queue" and not cur.failed:
            self.queue.append(cur)
        return self.queue.pop(0) if self.queue else None

    async def advance(self, *, manual: bool = False, natural_gen: int | None = None) -> None:
        async with self._lock:
            if self.bot.closing:
                return
            if natural_gen is not None and natural_gen != self._gen:
                return                                    # a skip/seek already moved on
            self._retries = 0
            first = True
            while True:
                nxt = self._pick_next(manual and first)
                first = False
                if nxt is None:
                    if self._refill_idle():
                        continue
                    await self._go_idle()
                    return
                try:
                    await self._start(nxt)
                    return
                except NotConnected:
                    self.current, self._interrupted = nxt, True
                    self._offset, self._t0 = 0.0, None
                    self.save_state()
                    return
                except SourceError as exc:
                    nxt.failed = True
                    self.current = None
                    self._fail_streak += 1
                    await self.notify(f"Skipped **{nxt.title}**: {exc}")
                    if self._fail_streak >= 5:
                        await self.notify("Too many tracks failed in a row, so I stopped. Check the logs / README (YouTube blocking).")
                        self.queue.clear()
                        self._idle_blocked_until = time.monotonic() + 300
                        self._fail_streak = 0
                        await self._go_idle()
                        return

    def _refill_idle(self) -> bool:
        if (self.always_on and self.idle_tracks and not self._idle_suppressed
                and time.monotonic() >= self._idle_blocked_until):
            tracks = [t.clone() for t in self.idle_tracks]
            random.shuffle(tracks)
            self.queue = tracks
            return True
        return False

    async def _go_idle(self) -> None:
        self._gen += 1
        try:
            await self.backend.stop(self)
        except Exception:
            log.exception("stop failed")
        self.current = None
        self.paused = False
        self._offset, self._t0 = 0.0, None
        self._idle_since = time.monotonic()
        self.save_state()
        self.notify_update()

    async def _start(self, track: Track, start: float = 0.0) -> None:
        self._gen += 1
        gen = self._gen
        self.current = track
        try:
            await self.backend.play(self, track, start, gen)
        except (NotConnected, SourceError):
            raise
        except Exception as exc:
            log.exception("Playback failed")
            raise SourceError(f"Playback error ({type(exc).__name__}).") from exc
        self._offset, self._t0 = float(start), time.monotonic()
        self.paused = self._auto_paused = self._interrupted = False
        self._fail_streak = 0
        self.save_state()
        self.notify_update()
        if self.queue and not self.queue[0].stream and self.bot.source:
            self.bot.spawn(self._preresolve(self.queue[0]))

    async def _preresolve(self, track: Track) -> None:
        try:
            await self.bot.source.prepare(track)
        except Exception as exc:
            log.debug("Pre-resolve failed for %s: %s", track.title, exc)

    async def on_track_end(self, gen: int, error: Exception | None = None) -> None:
        """Called by the backend when the current track finished (or died)."""
        if gen != self._gen or self.bot.closing:
            return
        if error:
            log.warning("Playback error in guild %s: %s", self.guild_id, error)
        if not self.backend.connected(self):
            self._offset, self._t0 = self.position, None          # voice dropped mid-track; resume after reconnect
            self._interrupted = True
            self.save_state()
            return
        cur, pos = self.current, self.position
        expected = (cur.match_duration or cur.duration) if cur else None
        if cur and expected and pos < expected - 10 and self._retries < 2 and not self.paused:
            self._retries += 1
            cur.stream = None                                     # stream URL probably expired; fetch a fresh one
            log.info("Track ended early (%.0fs of %ss); retry %d", pos, expected, self._retries)
            async with self._lock:
                if gen == self._gen:
                    try:
                        await self._start(cur, start=pos)
                        return
                    except (SourceError, NotConnected):
                        cur.failed = True
        await self.advance(natural_gen=gen if not (cur and cur.failed) else None)

    # -------------------------------------------------------------- controls
    async def pause(self, auto: bool = False) -> bool:
        if not self.current or self.paused or self._t0 is None:
            return False
        await self.backend.pause(self)
        self._offset, self._t0 = self.position, None
        self.paused, self._auto_paused = True, auto
        self.save_state()
        self.notify_update()
        return True

    async def resume(self) -> bool:
        if not self.current or not self.paused:
            return False
        await self.backend.resume(self)
        self._t0 = time.monotonic()
        self.paused = self._auto_paused = False
        self.notify_update()
        return True

    def skip(self) -> Track | None:
        """Skip immediately; the next track starts in the background."""
        skipped = self.current
        if skipped is None:
            return None
        self._gen += 1
        self.bot.spawn(self.backend.stop(self))
        self.bot.spawn(self.advance(manual=True))
        return skipped

    async def stop(self) -> None:
        self.queue.clear()
        self._idle_suppressed = True
        await self._go_idle()

    async def seek(self, seconds: int) -> None:
        cur = self.current
        if cur is None:
            raise SourceError("Nothing is playing.")
        dur = cur.match_duration or cur.duration
        if not dur:
            raise SourceError("I can't seek in a live stream.")
        seconds = max(0, min(seconds, dur - 1))
        was_paused = self.paused
        async with self._lock:
            await self._start(cur, start=seconds)
        if was_paused:
            await self.pause()

    async def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, volume))
        await self.backend.set_volume(self, self.volume)
        await self.save_setting(volume=self.volume)
        self.notify_update()

    async def set_loop(self, mode: str) -> None:
        self.loop_mode = mode
        await self.save_setting(loop_mode=mode)
        self.notify_update()

    def shuffle(self) -> None:
        random.shuffle(self.queue)
        self.save_state()
        self.notify_update()

    def remove(self, position: int) -> Track | None:
        if 1 <= position <= len(self.queue):
            t = self.queue.pop(position - 1)
            self.save_state()
            self.notify_update()
            return t
        return None

    def clear(self) -> int:
        n = len(self.queue)
        self.queue.clear()
        self.save_state()
        self.notify_update()
        return n

    # ------------------------------------------------------------ supervisor
    async def tick(self) -> None:
        """Called every ~10 s by the music cog: reconnect, auto-pause, idle timeout, periodic save."""
        if self.bot.closing or not self._loaded:
            return
        now = time.monotonic()
        if not self.backend.connected(self):
            if not self.wants_connection() or now < self._next_attempt:
                return
            try:
                await self.reconnect()
            except Exception as exc:
                self._attempts += 1
                delay = min(5 * 2 ** min(self._attempts - 1, 5), 120)      # 5,10,20,40,80,120 s
                self._next_attempt = now + delay
                log.warning("Reconnect attempt %d failed for guild %s (%s); retrying in %ds",
                            self._attempts, self.guild_id, exc, delay)
                if not self.always_on and self._attempts >= 6:
                    log.warning("Giving up on guild %s", self.guild_id)
                    await self.leave()
            return

        self._attempts = 0
        if self.current and self._interrupted and not self._lock.locked():
            await self._resume_current()

        humans = self.humans()
        if humans == 0:
            self._empty_since = self._empty_since or now
            if self.always_on:
                if config.PAUSE_WHEN_EMPTY and self.is_playing:
                    await self.pause(auto=True)
            elif now - self._empty_since > config.EMPTY_TIMEOUT:
                log.info("Leaving empty channel in guild %s", self.guild_id)
                await self.leave()
                return
        else:
            self._empty_since = None
            if self._auto_paused:
                await self.resume()

        if self.current is None and not self.queue:
            if not self.always_on and now - self._idle_since > config.IDLE_TIMEOUT:
                log.info("Idle timeout in guild %s", self.guild_id)
                await self.leave()
                return
            if self.always_on and self.idle_tracks and not self._idle_suppressed and now >= self._idle_blocked_until:
                self.kick()
        else:
            self._idle_since = now

        if self.is_playing and now - self._last_save > 30:
            self.save_state()
