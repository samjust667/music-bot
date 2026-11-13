"""Admin commands: /setup, /dj_role, /247."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import error_embed, info_embed, is_admin, join_user_channel


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not is_admin(interaction.user):
            await interaction.response.send_message(embed=error_embed("You need the **Manage Server** permission for that."), ephemeral=True)
            return False
        return True

    @app_commands.command(name="setup", description="Choose the text channel for music commands and the player card.")
    @app_commands.describe(channel="Text channel (leave empty to remove the restriction)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        player = await self.bot.get_player(interaction.guild)
        if channel is None:
            player.text_channel_id = None
            await player.save_setting(text_channel_id=None)
            return await interaction.response.send_message(embed=info_embed("Music commands now work in any channel."))
        me = interaction.guild.me
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                embed=error_embed(f"I need **View Channel, Send Messages and Embed Links** in {channel.mention}."), ephemeral=True)
        player.text_channel_id = channel.id
        await player.save_setting(text_channel_id=channel.id)
        await interaction.response.send_message(embed=info_embed(f"✅ Music commands and the player card now live in {channel.mention}."))
        player.notify_update()

    @app_commands.command(name="dj_role", description="Restrict playback controls to a role (admins always allowed).")
    @app_commands.describe(role="DJ role (leave empty to allow everyone)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def dj_role(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        player = await self.bot.get_player(interaction.guild)
        player.dj_role_id = role.id if role else None
        await player.save_setting(dj_role_id=player.dj_role_id)
        msg = f"🎚️ Only {role.mention} (and admins) can control playback now." if role else "🎚️ DJ role removed - anyone in the voice channel can control playback."
        await interaction.response.send_message(embed=info_embed(msg), allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="247", description="Keep the bot in its voice channel 24/7.")
    @app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def always_on(self, interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
        player = await self.bot.get_player(interaction.guild)
        if state.value == "off":
            player.always_on = False
            await player.save_setting(always_on=0)
            return await interaction.response.send_message(embed=info_embed("24/7 mode is **off**. I'll leave when idle or alone."))
        await interaction.response.defer()
        if not player.backend.connected(player):
            err = await join_user_channel(interaction, player)
            if err:
                return await interaction.followup.send(embed=error_embed(err + " Join a voice channel and run `/247 on` again."))
        player.always_on = True
        await player.save_setting(always_on=1)
        ch = player.voice_channel()
        extra = "" if player.idle_tracks else " Tip: `/playlist idle` sets music to play when the queue is empty."
        await interaction.followup.send(embed=info_embed(
            f"♾️ 24/7 mode is **on** — I'll stay in {ch.mention if ch else 'the channel'}, reconnect after drops and resume after restarts.{extra}"))
        player.kick()


async def setup(bot) -> None:
    await bot.add_cog(Admin(bot))
