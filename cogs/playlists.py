"""Per-user playlists and favourites (SQLite), plus the guild idle playlist for 24/7 mode."""
from __future__ import annotations

import json
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import Track, error_embed, format_duration, info_embed, is_admin, join_user_channel, md, truncate

MAX_PLAYLISTS = 25


class Playlists(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Use this in a server."), ephemeral=True)
            return False
        player = await self.bot.get_player(interaction.guild)
        if player.text_channel_id and interaction.channel_id != player.text_channel_id and not is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed(f"Please use music commands in <#{player.text_channel_id}>."), ephemeral=True)
            return False
        player.last_channel_id = interaction.channel_id
        return True

    playlist = app_commands.Group(name="playlist", description="Your saved playlists", guild_only=True)
    favourite = app_commands.Group(name="favourite", description="Your favourite tracks", guild_only=True)

    # --------------------------------------------------------- shared helpers
    async def _queue_tracks(self, interaction: discord.Interaction, tracks: list[dict], label: str, shuffle: bool = False) -> None:
        player = await self.bot.get_player(interaction.guild)
        err = await join_user_channel(interaction, player)
        if err:
            return await interaction.followup.send(embed=error_embed(err), ephemeral=True)
        items = [Track.from_dict(d) for d in tracks]
        if shuffle:
            random.shuffle(items)
        for t in items:
            t.requester_id, t.requester_name = interaction.user.id, interaction.user.display_name
        _, added = player.add(items)
        await interaction.followup.send(embed=info_embed(f"➕ Queued **{added}** track{'s' if added != 1 else ''} from {label}."))

    async def _names_ac(self, interaction: discord.Interaction, current: str):
        names = [n for n, _ in await self.bot.db.list_playlists(interaction.user.id)]
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]

    # -------------------------------------------------------------- playlists
    @playlist.command(name="save", description="Save the current track + queue as a playlist.")
    @app_commands.describe(name="Playlist name")
    async def pl_save(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]) -> None:
        player = await self.bot.get_player(interaction.guild)
        tracks = ([player.current] if player.current else []) + list(player.queue)
        if not tracks:
            return await interaction.response.send_message(embed=error_embed("Nothing to save - the queue is empty."), ephemeral=True)
        existing = [n for n, _ in await self.bot.db.list_playlists(interaction.user.id)]
        if name.lower() not in (e.lower() for e in existing) and len(existing) >= MAX_PLAYLISTS:
            return await interaction.response.send_message(embed=error_embed(f"You can keep up to {MAX_PLAYLISTS} playlists."), ephemeral=True)
        data = []
        for t in tracks[:config.MAX_QUEUE]:
            d = t.to_dict()
            d["requester_id"] = d["requester_name"] = None
            data.append(d)
        await self.bot.db.save_playlist(interaction.user.id, name.strip(), data)
        await interaction.response.send_message(embed=info_embed(f"💾 Saved **{md(name)}** ({len(data)} tracks)."), ephemeral=True)

    @playlist.command(name="load", description="Queue one of your saved playlists.")
    @app_commands.describe(name="Playlist name", shuffle="Shuffle before queueing")
    async def pl_load(self, interaction: discord.Interaction, name: str, shuffle: bool = False) -> None:
        await interaction.response.defer()
        tracks = await self.bot.db.get_playlist(interaction.user.id, name)
        if tracks is None:
            return await interaction.followup.send(embed=error_embed(f"You have no playlist called **{md(name)}**."), ephemeral=True)
        await self._queue_tracks(interaction, tracks, f"**{md(name)}**", shuffle)

    @playlist.command(name="list", description="List your saved playlists.")
    async def pl_list(self, interaction: discord.Interaction) -> None:
        items = await self.bot.db.list_playlists(interaction.user.id)
        text = "\n".join(f"• **{md(n)}** — {c} tracks" for n, c in items) or "You have no saved playlists. Use `/playlist save`."
        await interaction.response.send_message(embed=info_embed(text), ephemeral=True)

    @playlist.command(name="delete", description="Delete one of your playlists.")
    async def pl_delete(self, interaction: discord.Interaction, name: str) -> None:
        ok = await self.bot.db.delete_playlist(interaction.user.id, name)
        msg = info_embed(f"🗑️ Deleted **{md(name)}**.") if ok else error_embed(f"No playlist called **{md(name)}**.")
        await interaction.response.send_message(embed=msg, ephemeral=True)

    @playlist.command(name="idle", description="(Admin) Set one of YOUR playlists as the 24/7 idle playlist. Leave empty to clear.")
    @app_commands.describe(name="Playlist to play when the queue is empty in 24/7 mode")
    @app_commands.default_permissions(manage_guild=True)
    async def pl_idle(self, interaction: discord.Interaction, name: str | None = None) -> None:
        if not is_admin(interaction.user):
            return await interaction.response.send_message(embed=error_embed("Only admins can change the idle playlist."), ephemeral=True)
        player = await self.bot.get_player(interaction.guild)
        if name is None:
            player.idle_tracks = []
            await player.save_setting(idle_tracks=None)
            return await interaction.response.send_message(embed=info_embed("Idle playlist cleared."))
        tracks = await self.bot.db.get_playlist(interaction.user.id, name)
        if not tracks:
            return await interaction.response.send_message(embed=error_embed(f"You have no playlist called **{md(name)}**."), ephemeral=True)
        player.idle_tracks = [Track.from_dict(d) for d in tracks]
        await player.save_setting(idle_tracks=json.dumps(tracks))
        extra = "" if player.always_on else " (it only plays while 24/7 mode is on)"
        await interaction.response.send_message(embed=info_embed(f"📻 Idle playlist set to **{md(name)}** ({len(tracks)} tracks){extra}."))

    pl_load.autocomplete("name")(_names_ac)
    pl_delete.autocomplete("name")(_names_ac)
    pl_idle.autocomplete("name")(_names_ac)

    # ------------------------------------------------------------ favourites
    @favourite.command(name="add", description="Add the current track to your favourites.")
    async def fav_add(self, interaction: discord.Interaction) -> None:
        player = await self.bot.get_player(interaction.guild)
        t = player.current
        if not t:
            return await interaction.response.send_message(embed=error_embed("Nothing is playing."), ephemeral=True)
        d = t.to_dict()
        d["requester_id"] = d["requester_name"] = None
        new = await self.bot.db.add_favourite(interaction.user.id, t.key, d)
        msg = f"⭐ Added **{md(truncate(t.title, 80))}** to your favourites." if new else "That track is already in your favourites."
        await interaction.response.send_message(embed=info_embed(msg), ephemeral=True)

    @favourite.command(name="list", description="Show your favourites.")
    async def fav_list(self, interaction: discord.Interaction) -> None:
        favs = [Track.from_dict(d) for d in await self.bot.db.list_favourites(interaction.user.id)]
        if not favs:
            return await interaction.response.send_message(embed=info_embed("No favourites yet. Use `/favourite add` while a song plays."), ephemeral=True)
        lines = [f"`{i}.` {md(truncate(t.title, 55))} — {md(truncate(t.artist, 35))} `{format_duration(t.duration) if t.duration else '?:??'}`"
                 for i, t in enumerate(favs[:40], 1)]
        if len(favs) > 40:
            lines.append(f"…and {len(favs) - 40} more")
        await interaction.response.send_message(embed=info_embed("\n".join(lines)), ephemeral=True)

    @favourite.command(name="play", description="Queue your favourites (all of them, or just one by number).")
    @app_commands.describe(number="Number from /favourite list (leave empty for all)", shuffle="Shuffle when queueing all")
    async def fav_play(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 1000] | None = None, shuffle: bool = False) -> None:
        await interaction.response.defer()
        favs = await self.bot.db.list_favourites(interaction.user.id)
        if not favs:
            return await interaction.followup.send(embed=error_embed("You have no favourites yet."), ephemeral=True)
        if number is not None:
            if number > len(favs):
                return await interaction.followup.send(embed=error_embed(f"You only have {len(favs)} favourites."), ephemeral=True)
            favs, label = [favs[number - 1]], "your favourites"
        else:
            label = "your favourites"
        await self._queue_tracks(interaction, favs, label, shuffle)


async def setup(bot) -> None:
    await bot.add_cog(Playlists(bot))
