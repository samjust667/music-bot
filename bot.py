"""Discord music bot entry point (Railway worker: `python bot.py`)."""
from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from db import Database
from player import FFmpegBackend, GuildPlayer
from sources import YtdlpSource
from spotify import SpotifyClient
from utils import error_embed

log = logging.getLogger("musicbot")
EXTENSIONS = ("cogs.music", "cogs.player_ui", "cogs.playlists", "cogs.admin")


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()          # slash commands only: no privileged intents needed
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                         allowed_mentions=discord.AllowedMentions.none())
        self.db = Database(config.DB_PATH)
        self.players: dict[int, GuildPlayer] = {}
        self.closing = False
        self.http_session: aiohttp.ClientSession | None = None
        self.spotify: SpotifyClient | None = None
        self.source = None
        self.backend = None
        self._tasks: set[asyncio.Task] = set()
        self._restored = False
        self.tree.on_error = self.on_tree_error

    # -------------------------------------------------------------- helpers
    def spawn(self, coro) -> asyncio.Task:
        """create_task that keeps a reference and logs exceptions."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("Background task failed", exc_info=t.exception())

        task.add_done_callback(done)
        return task

    async def get_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if player is None:
            player = self.players[guild.id] = GuildPlayer(self, guild)
        await player.ensure_loaded()
        return player

    # ---------------------------------------------------------------- setup
    async def setup_hook(self) -> None:
        await self.db.connect()
        self.http_session = aiohttp.ClientSession(headers={"User-Agent": "discord-music-bot/1.0"})
        self.spotify = SpotifyClient(self.http_session, config.SPOTIFY_CLIENT_ID, config.SPOTIFY_CLIENT_SECRET)
        await self._init_audio()
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        from cogs.player_ui import PlayerView
        self.add_view(PlayerView())                   # persistent buttons survive restarts
        if config.DEV_GUILD_ID:
            guild = discord.Object(config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("Audio backend: %s | Spotify API credentials: %s", self.backend.name, "yes" if self.spotify.has_credentials else "no (embed fallback)")

    async def _init_audio(self) -> None:
        if config.USE_LAVALINK:
            from lavalink_backend import LavalinkBackend, LavalinkSource, connect_node
            if await connect_node(self, config.LAVALINK_URI, config.LAVALINK_PASSWORD):
                self.source = LavalinkSource()
                self.backend = LavalinkBackend(self.source)
                return
        cookies = config.prepare_cookies()
        self.source = YtdlpSource(cookies)
        self.backend = FFmpegBackend(self.source)
        if not discord.opus.is_loaded():
            for name in ("libopus.so.0", "libopus.so", "opus"):
                try:
                    discord.opus.load_opus(name)
                    break
                except OSError:
                    continue
        log.info("opus loaded: %s | yt-dlp cookies: %s", discord.opus.is_loaded(), "yes" if cookies else "no")

    # -------------------------------------------------------------- restore
    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d guild(s)", self.user, self.user.id, len(self.guilds))
        if self._restored:
            return
        self._restored = True
        await self.restore_players()

    async def restore_players(self) -> None:
        """After a restart/redeploy: rejoin saved channels and resume saved queues / idle playlists."""
        for row in await self.db.all_settings():
            guild = self.get_guild(row["guild_id"])
            if guild is None or not row["voice_channel_id"]:
                continue
            player = await self.get_player(guild)
            if not player.wants_connection():
                continue
            log.info("Restoring voice session in guild %s (24/7=%s, queued=%d)", guild.id, player.always_on, len(player.queue))
            try:
                await player.reconnect()
            except Exception as exc:
                log.warning("Initial restore failed for guild %s (%s); the supervisor will keep retrying", guild.id, exc)

    # ------------------------------------------------------------- shutdown
    async def close(self) -> None:
        self.closing = True                          # stop reacting to the disconnects we're about to cause
        for player in list(self.players.values()):
            try:
                await player._save_state()           # keeps current track + position for the next start
            except Exception:
                log.exception("Could not save state on shutdown")
        if config.USE_LAVALINK:
            try:
                import wavelink
                await wavelink.Pool.close()
            except Exception:
                pass
        await super().close()
        if self.http_session:
            await self.http_session.close()
        await self.db.close()

    # --------------------------------------------------------------- errors
    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure) and interaction.response.is_done():
            return
        if isinstance(error, app_commands.MissingPermissions):
            text = "You don't have permission to use that."
        elif isinstance(error, app_commands.CheckFailure):
            text = "You can't use that command here."
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"Slow down - try again in {error.retry_after:.0f}s."
        else:
            log.error("Unhandled command error in /%s", interaction.command.qualified_name if interaction.command else "?", exc_info=error)
            text = "Something went wrong. Check the bot logs."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed(text), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed(text), ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO), stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not config.DISCORD_TOKEN:
        sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env (or set it in Railway) and fill it in.")
    MusicBot().run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
