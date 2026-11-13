"""Playback commands, voice join/leave, voice-state handling and the 24/7 supervisor loop."""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from sources import SourceError
from spotify import SpotifyError, is_spotify
from utils import (Track, control_error, error_embed, format_duration, info_embed, is_admin, is_url,
                   join_user_channel, md, now_playing_embed, parse_time, queue_embed, truncate)

log = logging.getLogger(__name__)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._ac_cache: dict[str, tuple[float, list[app_commands.Choice[str]]]] = {}

    async def cog_load(self) -> None:
        self.supervisor.start()

    async def cog_unload(self) -> None:
        self.supervisor.cancel()

    # ---------------------------------------------------------------- checks
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Music commands only work in a server."), ephemeral=True)
            return False
        player = await self.bot.get_player(interaction.guild)
        if player.text_channel_id and interaction.channel_id != player.text_channel_id and not is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed(f"Please use music commands in <#{player.text_channel_id}>."), ephemeral=True)
            return False
        player.last_channel_id = interaction.channel_id
        return True

    async def _control(self, interaction: discord.Interaction) -> "GuildPlayer | None":  # noqa: F821
        """Get the player and verify the caller may control it; replies with an error otherwise."""
        player = await self.bot.get_player(interaction.guild)
        err = control_error(interaction.user, player)
        if err:
            await interaction.response.send_message(embed=error_embed(err), ephemeral=True)
            return None
        return player

    # --------------------------------------------------------- supervisor loop
    @tasks.loop(seconds=10)
    async def supervisor(self) -> None:
        for player in list(self.bot.players.values()):
            try:
                await player.tick()
            except Exception:
                log.exception("Supervisor error in guild %s", player.guild_id)

    @supervisor.before_loop
    async def _before_supervisor(self) -> None:
        await self.bot.wait_until_ready()

    # ----------------------------------------------------------- voice events
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.id != self.bot.user.id or self.bot.closing:
            return
        player = self.bot.players.get(member.guild.id)
        if not player:
            return
        if after.channel and (before.channel is None or before.channel.id != after.channel.id):
            if player.voice_channel_id != after.channel.id:
                log.info("Bot moved to channel %s in guild %s; saving it as the home channel", after.channel.id, member.guild.id)
                player.voice_channel_id = after.channel.id
                await player.save_setting(voice_channel_id=after.channel.id)
        elif after.channel is None and not player.intentional_leave:
            log.warning("Bot was disconnected from voice in guild %s; the supervisor will reconnect if needed", member.guild.id)
            player._next_attempt = time.monotonic() + 3

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload) -> None:
        guild = getattr(payload.player, "guild", None)
        player = self.bot.players.get(guild.id) if guild else None
        if not player:
            return
        reason = str(getattr(payload, "reason", "")).lower()
        if reason == "finished":
            await player.on_track_end(player._gen)
        elif reason == "loadfailed":
            await player.on_track_end(player._gen, RuntimeError("Lavalink failed to load the track"))

    # ------------------------------------------------------------ join / leave
    @app_commands.command(name="join", description="Join your voice channel (in 24/7 mode: move the home channel).")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        player = await self.bot.get_player(interaction.guild)
        if player.always_on and not is_admin(interaction.user):
            return await interaction.response.send_message(embed=error_embed("24/7 mode is on; only admins can move me."), ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(embed=error_embed("Join a voice channel first."), ephemeral=True)
        await interaction.response.defer()
        target = interaction.user.voice.channel
        try:
            await player.connect(target)
        except (asyncio.TimeoutError, discord.DiscordException) as exc:
            return await interaction.followup.send(embed=error_embed(f"Couldn't join {target.mention}: {exc or 'timed out'}"))
        player.kick()
        await interaction.followup.send(embed=info_embed(f"🔊 Joined {target.mention}."))

    @app_commands.command(name="leave", description="Leave the voice channel and clear the queue.")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        player = await self._control(interaction)
        if not player:
            return
        await interaction.response.defer()
        note = ""
        if player.always_on:
            player.always_on = False
            await player.save_setting(always_on=0)
            note = " 24/7 mode was turned off."
        await player.leave()
        await interaction.followup.send(embed=info_embed("👋 Left the voice channel." + note))

    # ------------------------------------------------------------------- play
    async def _resolve(self, query: str, member: discord.Member, *, interaction: discord.Interaction):
        """Returns (label, tracks_or_None, spotify_collection_or_None)."""
        query = query.strip()
        if is_spotify(query):
            coll = await self.bot.spotify.load(query)
            return coll.name, None, coll
        if is_url(query):
            name, tracks = await self.bot.source.resolve(query, limit=min(config.MAX_QUEUE, 200))
            return name, tracks, None
        tracks = await self.bot.source.search(query, limit=1)
        if not tracks:
            raise SourceError(f"No results for “{truncate(query, 80)}”.")
        return None, tracks[:1], None

    @staticmethod
    def _stamp(tracks: list[Track], member: discord.Member) -> None:
        for t in tracks:
            t.requester_id, t.requester_name = member.id, member.display_name

    async def _play_impl(self, interaction: discord.Interaction, query: str, front: bool) -> None:
        await interaction.response.defer()
        player = await self.bot.get_player(interaction.guild)
        err = await join_user_channel(interaction, player)
        if err:
            return await interaction.followup.send(embed=error_embed(err), ephemeral=True)
        member = interaction.user
        try:
            label, tracks, coll = await self._resolve(query, member, interaction=interaction)
            if coll is not None:
                return await self._play_spotify(interaction, player, coll, front)
        except (SourceError, SpotifyError) as exc:
            return await interaction.followup.send(embed=error_embed(str(exc)))

        self._stamp(tracks, member)
        index = 0 if front else None
        start, added = player.add(tracks, index)
        if not added:
            return await interaction.followup.send(embed=error_embed(f"The queue is full ({config.MAX_QUEUE} tracks)."))
        await interaction.followup.send(embed=self._queued_embed(player, tracks, added, start, label))

    def _queued_embed(self, player, tracks: list[Track], added: int, start: int, label: str | None) -> discord.Embed:
        first = tracks[0]
        if added == 1:
            link = f"[{md(truncate(first.title, 80))}]({first.link})" if first.link else md(first.title)
            if player.current is None and start == 0:
                text = f"▶️ Starting {link} — {md(first.artist)}"
            else:
                text = f"➕ Queued {link} — {md(first.artist)} (position {start + 1})"
        else:
            text = f"➕ Queued **{added}** tracks" + (f" from **{md(truncate(label, 80))}**" if label else "")
        e = info_embed(text)
        if first.thumbnail:
            e.set_thumbnail(url=first.thumbnail)
        return e

    async def _play_spotify(self, interaction: discord.Interaction, player, coll, front: bool) -> None:
        """Queue the first track at once; load/queue the rest in the background."""
        member = interaction.user
        batches = coll.batches
        try:
            first_batch = await anext(batches)
        except StopAsyncIteration:
            return await interaction.followup.send(embed=error_embed("No playable tracks found in that Spotify link."))
        self._stamp(first_batch, member)

        index = 0 if front else None
        start, added = player.add(first_batch[:1], index)
        if not added:
            return await interaction.followup.send(embed=error_embed(f"The queue is full ({config.MAX_QUEUE} tracks)."))
        if coll.kind == "track":
            return await interaction.followup.send(embed=self._queued_embed(player, first_batch, 1, start, None))

        embed = info_embed(f"🎧 Queued the first track of **{md(truncate(coll.name, 80))}** "
                           f"({coll.kind}) — loading the rest in the background…")
        if coll.thumbnail:
            embed.set_thumbnail(url=coll.thumbnail)
        message = await interaction.followup.send(embed=embed, wait=True)

        async def finish() -> None:
            total = 1
            nxt = (start + 1) if front else None
            try:
                rest = first_batch[1:]
                while True:
                    if rest:
                        self._stamp(rest, member)
                        s, n = player.add(rest, nxt)
                        total += n
                        if nxt is not None:
                            nxt = s + n
                        if n < len(rest):
                            break
                    try:
                        rest = await anext(batches)
                    except StopAsyncIteration:
                        break
                text = f"✅ Queued **{total}** tracks from **{md(truncate(coll.name, 80))}**."
                if total >= config.MAX_QUEUE:
                    text += " (queue limit reached)"
            except Exception as exc:
                log.exception("Background Spotify load failed")
                text = f"⚠️ Queued {total} tracks from **{md(truncate(coll.name, 80))}**, then loading failed: {exc}"
            try:
                await message.edit(embed=info_embed(text))
            except discord.HTTPException:
                pass

        self.bot.spawn(finish())

    @app_commands.command(name="play", description="Play a song name, YouTube/SoundCloud link, or Spotify track/album/playlist.")
    @app_commands.describe(query="Song name or link")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await self._play_impl(interaction, query, front=False)

    @app_commands.command(name="playnext", description="Queue a song to play right after the current one.")
    @app_commands.describe(query="Song name or link")
    @app_commands.guild_only()
    async def playnext(self, interaction: discord.Interaction, query: str) -> None:
        await self._play_impl(interaction, query, front=True)

    async def _autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        current = current.strip()
        if not config.AUTOCOMPLETE or len(current) < 3 or is_url(current) or is_spotify(current):
            return []
        key = current.lower()
        hit = self._ac_cache.get(key)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        try:
            tracks = await asyncio.wait_for(self.bot.source.search(current, limit=5, provider=config.SEARCH_PROVIDERS[0], timeout=2.4), 2.6)
        except Exception:
            return []
        choices = []
        for t in tracks:
            name = truncate(f"{t.title} — {t.artist}" + (f" [{format_duration(t.duration)}]" if t.duration else ""), 100)
            value = t.url if t.url and len(t.url) <= 100 else truncate(t.search_query, 100)
            choices.append(app_commands.Choice(name=name, value=value))
        if len(self._ac_cache) > 200:
            self._ac_cache.clear()
        self._ac_cache[key] = (time.monotonic(), choices)
        return choices

    @play.autocomplete("query")
    async def play_ac(self, interaction, current):
        return await self._autocomplete(interaction, current)

    @playnext.autocomplete("query")
    async def playnext_ac(self, interaction, current):
        return await self._autocomplete(interaction, current)

    # -------------------------------------------------------- simple controls
    @app_commands.command(name="pause", description="Pause playback.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        if player := await self._control(interaction):
            ok = await player.pause()
            await interaction.response.send_message(embed=info_embed("⏸️ Paused." if ok else "Nothing to pause."))

    @app_commands.command(name="resume", description="Resume playback.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        if player := await self._control(interaction):
            ok = await player.resume()
            await interaction.response.send_message(embed=info_embed("▶️ Resumed." if ok else "Nothing is paused."))

    @app_commands.command(name="skip", description="Skip the current track.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        if player := await self._control(interaction):
            skipped = player.skip()
            msg = f"⏭️ Skipped **{md(truncate(skipped.title, 80))}**." if skipped else "Nothing is playing."
            await interaction.response.send_message(embed=info_embed(msg))

    @app_commands.command(name="stop", description="Stop playback and clear the queue (stays in the channel).")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        if player := await self._control(interaction):
            await player.stop()
            await interaction.response.send_message(embed=info_embed("⏹️ Stopped and cleared the queue."))

    @app_commands.command(name="queue", description="Show the queue.")
    @app_commands.describe(page="Page number")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 500] = 1) -> None:
        player = await self.bot.get_player(interaction.guild)
        await interaction.response.send_message(embed=queue_embed(player, page))

    @app_commands.command(name="nowplaying", description="Show what's playing.")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = await self.bot.get_player(interaction.guild)
        await interaction.response.send_message(embed=now_playing_embed(player))

    @app_commands.command(name="volume", description="Set the volume (0-100).")
    @app_commands.describe(level="0-100")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
        if player := await self._control(interaction):
            await player.set_volume(level)
            await interaction.response.send_message(embed=info_embed(f"🔊 Volume set to **{level}%**."))

    @app_commands.command(name="loop", description="Set loop mode.")
    @app_commands.choices(mode=[app_commands.Choice(name="Off", value="off"),
                                app_commands.Choice(name="Track", value="track"),
                                app_commands.Choice(name="Queue", value="queue")])
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        if player := await self._control(interaction):
            await player.set_loop(mode.value)
            await interaction.response.send_message(embed=info_embed(f"🔁 Loop: **{mode.name}**."))

    @app_commands.command(name="shuffle", description="Shuffle the queue.")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if player := await self._control(interaction):
            if len(player.queue) < 2:
                return await interaction.response.send_message(embed=error_embed("Not enough tracks in the queue to shuffle."), ephemeral=True)
            player.shuffle()
            await interaction.response.send_message(embed=info_embed(f"🔀 Shuffled {len(player.queue)} tracks."))

    @app_commands.command(name="remove", description="Remove a track from the queue by position.")
    @app_commands.describe(position="Position shown in /queue")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 1000]) -> None:
        if player := await self._control(interaction):
            t = player.remove(position)
            if t:
                await interaction.response.send_message(embed=info_embed(f"🗑️ Removed **{md(truncate(t.title, 80))}**."))
            else:
                await interaction.response.send_message(embed=error_embed("There's no track at that position."), ephemeral=True)

    @app_commands.command(name="clear", description="Clear the queue (keeps the current track playing).")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        if player := await self._control(interaction):
            n = player.clear()
            await interaction.response.send_message(embed=info_embed(f"🧹 Cleared {n} track{'s' if n != 1 else ''}."))

    @app_commands.command(name="seek", description="Jump to a time in the current track (e.g. 90, 1:30, 1m30s).")
    @app_commands.describe(time="Target time")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, time: str) -> None:
        player = await self._control(interaction)
        if not player:
            return
        secs = parse_time(time)
        if secs is None:
            return await interaction.response.send_message(embed=error_embed("Use a time like `90`, `1:30` or `1m30s`."), ephemeral=True)
        await interaction.response.defer()
        try:
            await player.seek(secs)
        except SourceError as exc:
            return await interaction.followup.send(embed=error_embed(str(exc)))
        await interaction.followup.send(embed=info_embed(f"⏩ Seeked to **{format_duration(player.position)}**."))


async def setup(bot) -> None:
    await bot.add_cog(Music(bot))
