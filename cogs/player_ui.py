"""The persistent "Now playing" embed + button controller."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

import discord
from discord.ext import commands, tasks

from sources import SourceError
from utils import control_error, error_embed, now_playing_embed, queue_embed

log = logging.getLogger(__name__)
LOOP_NEXT = {"off": "track", "track": "queue", "queue": "off"}
LOOP_BTN = {"off": "Loop: Off", "track": "Loop: Track", "queue": "Loop: Queue"}


async def handle_button(interaction: discord.Interaction, action: str) -> None:
    bot = interaction.client
    if interaction.guild is None:
        return
    player = await bot.get_player(interaction.guild)

    if action == "queue":
        return await interaction.response.send_message(embed=queue_embed(player, 1), ephemeral=True)

    err = control_error(interaction.user, player)
    if err:
        return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)
    if player.current is None and action != "stop":
        return await interaction.response.send_message(embed=error_embed("Nothing is playing."), ephemeral=True)

    await interaction.response.defer()          # acknowledge fast; some actions take a moment
    try:
        if action == "pause":
            await (player.resume() if player.paused else player.pause())
        elif action == "skip":
            player.skip()
        elif action == "stop":
            await player.stop()
        elif action == "loop":
            await player.set_loop(LOOP_NEXT[player.loop_mode])
        elif action == "shuffle":
            player.shuffle()
        elif action == "vol_down":
            await player.set_volume(player.volume - 10)
        elif action == "vol_up":
            await player.set_volume(player.volume + 10)
    except SourceError as exc:
        return await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
    try:
        await interaction.edit_original_response(embed=now_playing_embed(player), view=PlayerView(paused=player.paused, loop=player.loop_mode))
    except discord.HTTPException:
        pass


class PlayerView(discord.ui.View):
    """Persistent: timeout=None and a fixed custom_id on every item (registered via bot.add_view)."""

    def __init__(self, *, paused: bool = False, loop: str = "off"):
        super().__init__(timeout=None)
        self.pause_btn.label = "Resume" if paused else "Pause"
        self.pause_btn.emoji = "▶️" if paused else "⏸️"
        self.loop_btn.label = LOOP_BTN.get(loop, "Loop: Off")
        self.loop_btn.style = discord.ButtonStyle.secondary if loop == "off" else discord.ButtonStyle.success

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="musicbot:pause", row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_button(interaction, "pause")

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="musicbot:skip", row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_button(interaction, "skip")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="musicbot:stop", row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_button(interaction, "stop")

    @discord.ui.button(label="Loop: Off", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="musicbot:loop", row=0)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_button(interaction, "loop")

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="musicbot:shuffle", row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_button(interaction, "shuffle")

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="musicbot:queue", row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_button(interaction, "queue")

    @discord.ui.button(label="Vol-", emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="musicbot:vol_down", row=1)
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_button(interaction, "vol_down")

    @discord.ui.button(label="Vol+", emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="musicbot:vol_up", row=1)
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_button(interaction, "vol_up")


class PlayerUI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: set[int] = set()

    async def cog_load(self) -> None:
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    @commands.Cog.listener()
    async def on_music_update(self, guild_id: int) -> None:
        """Coalesce bursts of changes into one embed edit."""
        if guild_id in self._pending or self.bot.closing:
            return
        self._pending.add(guild_id)
        await asyncio.sleep(0.8)
        self._pending.discard(guild_id)
        player = self.bot.players.get(guild_id)
        if player:
            await self.refresh(player)

    @tasks.loop(seconds=15)
    async def ticker(self) -> None:
        for player in list(self.bot.players.values()):
            if player.is_playing and not self.bot.closing:
                await self.refresh(player)

    @ticker.before_loop
    async def _before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    async def refresh(self, player) -> None:
        async with self._locks[player.guild_id]:
            channel = player.guild.get_channel(player.target_channel_id) if player.target_channel_id else None
            if channel is None:
                return
            have_message = bool(player.player_message_id and player.player_channel_id)
            if not have_message and player.current is None:
                return                                   # don't post an "idle" card out of nowhere
            embed = now_playing_embed(player)
            view = PlayerView(paused=player.paused, loop=player.loop_mode)
            try:
                if have_message and player.player_channel_id == channel.id:
                    try:
                        await channel.get_partial_message(player.player_message_id).edit(embed=embed, view=view)
                        return
                    except discord.NotFound:
                        pass
                elif have_message:                        # player channel changed via /setup: remove the old card
                    old = player.guild.get_channel(player.player_channel_id)
                    if old:
                        try:
                            await old.get_partial_message(player.player_message_id).delete()
                        except discord.HTTPException:
                            pass
                msg = await channel.send(embed=embed, view=view)
                player.player_channel_id, player.player_message_id = channel.id, msg.id
                await player.save_setting(player_channel_id=channel.id, player_message_id=msg.id)
            except discord.Forbidden:
                log.warning("Missing permission to post the player in #%s (guild %s)", channel, player.guild_id)
            except discord.HTTPException as exc:
                log.warning("Player embed update failed in guild %s: %s", player.guild_id, exc)


async def setup(bot) -> None:
    await bot.add_cog(PlayerUI(bot))
