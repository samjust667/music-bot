"""Shared helpers: Track model, time formatting, embeds, permission checks."""
from __future__ import annotations

import asyncio
import dataclasses
import math
import re
from dataclasses import dataclass, field
from typing import Any

import discord

COLOUR = discord.Colour.from_rgb(88, 101, 242)
ERR_COLOUR = discord.Colour.from_rgb(237, 66, 69)
LOOP_LABEL = {"off": "Off", "track": "Track 🔂", "queue": "Queue 🔁"}


@dataclass
class Track:
    title: str
    artist: str = "Unknown artist"
    duration: int | None = None          # seconds; None = unknown / live
    url: str | None = None               # audio page URL; None until a Spotify track has been matched
    spotify_url: str | None = None
    thumbnail: str | None = None
    source: str = "youtube"              # youtube | soundcloud | url | spotify (unmatched)
    requester_id: int | None = None
    requester_name: str | None = None
    match_duration: int | None = None    # duration of the audio we matched (may differ from Spotify's)
    stream: Any = field(default=None, repr=False, compare=False)   # cached (url, headers, expiry)
    failed: bool = field(default=False, repr=False, compare=False)

    _SAVED = ("title", "artist", "duration", "url", "spotify_url", "thumbnail", "source",
              "requester_id", "requester_name", "match_duration")

    @property
    def link(self) -> str | None:
        return self.spotify_url or self.url

    @property
    def search_query(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist and self.artist != "Unknown artist" else self.title

    @property
    def key(self) -> str:
        return (self.spotify_url or self.url or f"{self.artist}-{self.title}").lower()[:300]

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._SAVED}

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        data = {k: d[k] for k in cls._SAVED if k in d}
        data.setdefault("title", "Unknown")
        return cls(**data)

    def clone(self, **changes) -> "Track":
        return dataclasses.replace(self, stream=None, failed=False, **changes)


# ---------------------------------------------------------------- formatting
def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "LIVE"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> int | None:
    """'90', '1:30', '1:02:03', '1m30s', '2h' -> seconds."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", text)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    return None


def progress_bar(position: float, duration: int | None, width: int = 16) -> str:
    if not duration:
        return "🔴 live / unknown length"
    ratio = min(max(position / duration, 0.0), 1.0)
    i = min(int(ratio * width), width - 1)
    return "▬" * i + "🔘" + "▬" * (width - i - 1)


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def md(text: str) -> str:
    """Escape markdown and link-breaking brackets."""
    return discord.utils.escape_markdown(text).replace("[", "(").replace("]", ")")


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://\S+$", text.strip()))


# --------------------------------------------------------------- permissions
def is_admin(member: discord.abc.User) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


def control_error(member: discord.Member, player) -> str | None:
    """Return a user-facing reason why `member` may not control playback, or None if allowed."""
    if is_admin(member):
        return None
    if player.dj_role_id and not any(r.id == player.dj_role_id for r in member.roles):
        return f"You need the <@&{player.dj_role_id}> role to control the music."
    channel = player.voice_channel()
    if channel is None:
        return "I'm not in a voice channel."
    if not member.voice or not member.voice.channel or member.voice.channel.id != channel.id:
        return f"Join {channel.mention} to control the music."
    return None


async def join_user_channel(interaction: discord.Interaction, player) -> str | None:
    """Make sure the bot is in the caller's voice channel. Returns an error string or None."""
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        return "Join a voice channel first."
    target = member.voice.channel
    perms = target.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.connect and perms.speak):
        return f"I need **View Channel, Connect and Speak** permissions in {target.mention}."
    current = player.voice_channel() if player.backend.connected(player) else None
    if current and current.id == target.id:
        return None
    if current:
        if player.always_on:
            return f"I'm in 24/7 mode in {current.mention}. Join it, or have an admin use `/join`."
        busy = (player.current or player.queue) and any(not m.bot for m in current.members)
        if busy:
            return f"I'm already playing for people in {current.mention}."
    try:
        await player.connect(target)
    except asyncio.TimeoutError:
        return "Timed out connecting to the voice channel. Try again in a moment."
    except discord.DiscordException as exc:
        return f"Couldn't join the voice channel: {exc}"
    return None


# -------------------------------------------------------------------- embeds
def now_playing_embed(player) -> discord.Embed:
    t: Track | None = player.current
    embed = discord.Embed(colour=COLOUR)
    if t is None:
        embed.title = "Nothing playing"
        embed.description = "Use `/play` with a song name, YouTube/SoundCloud link or Spotify link."
    else:
        embed.title = "⏸️ Paused" if player.paused else "🎶 Now playing"
        title = md(truncate(t.title, 150))
        embed.description = f"**[{title}]({t.link})**" if t.link else f"**{title}**"
        embed.description += f"\nby {md(truncate(t.artist, 100))}"
        who = f"<@{t.requester_id}>" if t.requester_id else (t.requester_name or "—")
        embed.add_field(name="Requested by", value=who, inline=True)
        embed.add_field(name="Loop", value=LOOP_LABEL.get(player.loop_mode, "Off"), inline=True)
        embed.add_field(name="Volume", value=f"{player.volume}%", inline=True)
        pos = player.position
        dur = t.match_duration or t.duration
        embed.add_field(
            name="Progress",
            value=f"{progress_bar(pos, dur)}\n`{format_duration(pos)} / {format_duration(dur) if dur else 'LIVE'}`",
            inline=False,
        )
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
    foot = f"Queue: {len(player.queue)} track{'s' if len(player.queue) != 1 else ''}"
    if player.always_on:
        foot += " • 24/7"
    embed.set_footer(text=f"{foot} • {player.backend.name}")
    return embed


def queue_embed(player, page: int = 1, per_page: int = 10) -> discord.Embed:
    q = player.queue
    pages = max(1, math.ceil(len(q) / per_page))
    page = min(max(page, 1), pages)
    embed = discord.Embed(title="Queue", colour=COLOUR)
    lines = []
    if player.current:
        c = player.current
        lines.append(f"**Now:** [{md(truncate(c.title, 60))}]({c.link or 'https://discord.com'}) — {md(truncate(c.artist, 40))}")
    start = (page - 1) * per_page
    for i, t in enumerate(q[start:start + per_page], start + 1):
        label = f"[{md(truncate(t.title, 55))}]({t.link})" if t.link else md(truncate(t.title, 55))
        lines.append(f"`{i}.` {label} — {md(truncate(t.artist, 35))} `{format_duration(t.duration) if t.duration else '?:??'}`")
    if not q:
        lines.append("\nThe queue is empty.")
    embed.description = "\n".join(lines)[:4000]
    total = sum(t.duration or 0 for t in q)
    embed.set_footer(text=f"Page {page}/{pages} • {len(q)} queued • {format_duration(total)} total • Loop: {player.loop_mode}")
    return embed


def error_embed(text: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {text}", colour=ERR_COLOUR)


def info_embed(text: str) -> discord.Embed:
    return discord.Embed(description=text, colour=COLOUR)
