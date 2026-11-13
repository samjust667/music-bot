# Discord Music Bot (24/7, Spotify links, button player)

A private, single-server Discord music bot for Railway. Slash commands, a persistent button player,
24/7 voice presence with automatic reconnect, and queue/idle-playlist resume after redeploys.

> **Private use only.** Downloading or streaming audio from YouTube may violate YouTube's Terms of
> Service, and Spotify's terms forbid streaming its audio. This project is intended for private use by
> you and a couple of friends on one server. Don't run it as a public bot.

## How Spotify links work (read this first)

Spotify audio cannot be streamed to Discord (DRM + terms). The bot reads Spotify **metadata only**
(title, artist, duration, cover art) and then finds the matching audio on YouTube (or SoundCloud) by
searching `"artist - title"` and **preferring the result whose duration is closest** to Spotify's.
It also penalises live/cover/remix/slowed versions and prefers YouTube's auto-generated "- Topic" channels.

Metadata sources, tried in order:

1. **Spotify Web API** (client credentials; set `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`).
   Uses the `/playlists/{id}/items` endpoint (the old `/tracks` path is tried as a fallback).
2. **Public embed pages** (no credentials). Used automatically if the API fails or isn't configured.
   Returns up to ~100 tracks per playlist/album. Best-effort: Spotify can change that page at any time.

Spotify tightened its API in 2026 (Development Mode apps need a Premium owner, a small user allowlist, and
some playlist/metadata endpoints are restricted), so expect the embed fallback to be used for some
playlists. Large playlists: the **first track plays immediately** and the rest load in the background.

## Features

| Area | What you get |
|---|---|
| Voice | `/join`, `/leave`, `/247 on\|off`; auto-reconnect with backoff; rejoin + resume after redeploy |
| Playback | `/play`, `/playnext`, `/pause`, `/resume`, `/skip`, `/stop`, `/queue [page]`, `/nowplaying`, `/volume`, `/loop`, `/shuffle`, `/remove`, `/clear`, `/seek` |
| Playlists | `/playlist save\|load\|list\|delete\|idle`, `/favourite add\|list\|play` (per user) |
| Player card | One "Now playing" embed (cover, title, artist, requester, progress bar, queue length, loop) with buttons: Pause/Resume, Skip, Stop, Loop, Shuffle, Queue, Vol-, Vol+ |
| Admin | `/setup <text channel>`, `/dj_role <role>`, `/247` (all require **Manage Server**) |

`/play` accepts a song name, YouTube/SoundCloud link (tracks or playlists), or a Spotify track/album/playlist link.
It autocompletes song names as you type.

### Behaviour details

- **24/7 mode**: the bot stays in the saved channel when idle or alone. While alone it **pauses** (saves
  bandwidth) and resumes when someone joins (`PAUSE_WHEN_EMPTY=false` to keep playing). If a 24/7 guild has an
  idle playlist (`/playlist idle <name>`), it plays shuffled whenever the queue runs out. `/stop` suppresses the
  idle playlist until something new is queued. `/leave` ends the session **and turns 24/7 off**.
- **Not 24/7**: the bot leaves after `IDLE_TIMEOUT` (5 min) idle or `EMPTY_TIMEOUT` (2 min) alone.
- **Moved by an admin**: the new channel becomes the saved home channel.
- **Restart/redeploy**: on shutdown the current track + position + queue are saved. On start the bot rejoins and
  resumes from that position (24/7 guilds, or any guild that still had a queue).
- **DJ role**: when set, *control* commands and the player buttons (pause, skip, stop, volume, loop, shuffle,
  remove, clear, seek, leave) require the role. Admins always pass. `/play`, `/queue`, `/nowplaying` and the
  Queue button stay open. With no DJ role, anyone **in the bot's voice channel** can use the buttons.
- **`/setup`**: music commands only work in that channel (admins exempt) and the player card is posted there.
  Without `/setup`, the card goes to the channel of the last command.

## Create the Discord application

1. <https://discord.com/developers/applications> → **New Application** → **Bot** → copy the token.
2. No privileged intents are needed.
3. Invite with scopes `bot` + `applications.commands` and permissions: View Channels, Send Messages,
   Embed Links, Connect, Speak.
4. Voice needs **discord.py ≥ 2.7** (DAVE end-to-end encryption, mandatory on Discord since 2026-03-01). That is
   pinned in `requirements.txt` together with `davey`. Older versions fail to join voice.

## Run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in DISCORD_TOKEN
# system packages: ffmpeg, libopus (e.g. apt install ffmpeg libopus0), and deno for yt-dlp (https://deno.com)
python bot.py
```

Set `GUILD_ID` while developing so slash commands appear instantly (global sync can take a while).

## Deploy on Railway

The bot is a **worker** (no port, no web server). It needs **ffmpeg + libopus** (and **deno** for yt-dlp).
Pick one of two builds:

### Option A - Dockerfile (recommended for yt-dlp mode)

`Dockerfile.fallback` installs ffmpeg, libopus and deno, so nothing depends on builder behaviour.

```bash
cp railway.docker.json railway.json    # uses "builder": "DOCKERFILE", dockerfilePath "Dockerfile.fallback"
```

### Option B - Railpack (`railway.json` as shipped)

`railway.json` selects Railpack and runs `python bot.py`. Add these **service variables**:

```
RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg libopus0
```

yt-dlp also wants a JS runtime for YouTube; Railpack has no obvious way to add one, so with Option B either use
Lavalink or accept that some YouTube formats may fail. (Verify the variable names against Railway's current
Railpack docs, as this builder is evolving; if ffmpeg/opus errors appear in the logs, use Option A.)

### Both options

1. Push to a **private** GitHub repo → Railway → *New Project → Deploy from GitHub*.
2. **Add a Volume** (service → Settings → Volumes) mounted at `/data`, then set `DATA_DIR=/data`.
   Without it SQLite is wiped on every deploy and the bot forgets 24/7 settings, playlists and the saved queue.
3. Set variables: `DISCORD_TOKEN`, (optional) `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, cookies, Lavalink.
4. Keep **replicas = 1** (two copies would fight over the same voice channel).

## Choosing the audio source: yt-dlp vs Lavalink

| | yt-dlp + ffmpeg (default) | Lavalink (optional) |
|---|---|---|
| Setup | Nothing extra | Run a Lavalink server (separate Railway service, VPS, or hosted) |
| YouTube from Railway IP | Frequently blocked ("Sign in to confirm you're not a bot") → needs cookies/proxy | Same IP problem, but the YouTube plugin has OAuth/poToken options and is maintained for exactly this |
| CPU on the bot host | ffmpeg transcodes every track | Offloaded to the Lavalink server |
| Voice encryption (DAVE) | Handled by discord.py ≥ 2.7 | Handled by Lavalink: use a build that supports DAVE |
| Best for | Trying it out; works well from a home server/VPS | A datacenter-hosted bot that must be reliable |

**Recommendation:** start with yt-dlp. If YouTube blocks Railway's IP and cookies don't hold up, move to Lavalink
(or run the bot from a residential connection). Enable Lavalink with:

```
LAVALINK_URI=http://your-lavalink-host:2333      # https:// if TLS
LAVALINK_PASSWORD=your-password
```

If the node can't be reached at startup the bot logs it and falls back to yt-dlp + ffmpeg.

### YouTube blocking (yt-dlp mode)

1. Export cookies from a **throw-away** Google account (browser extension "Get cookies.txt LOCALLY", Netscape format).
2. Provide them as `YTDLP_COOKIES_B64` (`base64 -w0 cookies.txt`) or mount a file and set `YTDLP_COOKIES_FILE`.
3. Cookies expire/rotate; refresh them if errors return. Using a main account risks it being flagged.
4. Optional: `YTDLP_PROXY=http://user:pass@host:port` (residential proxy).
5. Always redeploy to pick up the latest yt-dlp (unpinned in `requirements.txt`).

Playback errors are reported in chat with a friendly message (private video, age-restricted, region-locked, bot check, ...).
After 5 consecutive failures the bot stops and notifies the channel instead of looping forever.

## Configuration

See `.env.example` for every variable. Highlights: `DATA_DIR`, `SEARCH_PROVIDERS` (`youtube,soundcloud`),
`DEFAULT_VOLUME`, `MAX_QUEUE`, `IDLE_TIMEOUT`, `EMPTY_TIMEOUT`, `PAUSE_WHEN_EMPTY`, `AUTOCOMPLETE`, `LOG_LEVEL`.

## Project layout

```
bot.py               entry point, startup restore, graceful shutdown
config.py            environment variables
db.py                aiosqlite (WAL): settings, saved queue, playlists, favourites
utils.py             Track model, formatting, embeds, permission helpers
sources.py           yt-dlp search/resolve/match/stream (the swappable audio-source layer)
spotify.py           Spotify metadata (Web API + embed fallback)
player.py            per-guild engine: queue, loop, 24/7 supervisor, FFmpeg backend
lavalink_backend.py  optional wavelink backend/source
cogs/music.py        playback commands + voice events + supervisor loop
cogs/player_ui.py    persistent button view + now-playing embed
cogs/playlists.py    playlists, favourites, idle playlist
cogs/admin.py        /setup, /dj_role, /247
```

## Troubleshooting

- **Bot joins then drops / "4017"-style close codes**: discord.py or `davey` is too old → `pip install -U -r requirements.txt`.
- **`OpusNotLoaded` / `ffmpeg was not found`**: system packages are missing; use Option A (Dockerfile).
- **"YouTube is blocking this server"**: see *YouTube blocking* above or use Lavalink.
- **Slash commands missing**: wait for global sync, re-invite with `applications.commands`, or set `GUILD_ID`.
- **Settings vanish after deploy**: no Volume / `DATA_DIR` not pointing at it.
- **Spotify playlist only loads 100 tracks**: that's the embed fallback; check API credentials/the app's access mode.

## Known limitations

- Written and syntax-checked offline; it has **not** been run against live Discord/Spotify/YouTube/Lavalink
  from this environment. Expect to do a first-deploy shakedown.
- The Spotify embed fallback and yt-dlp extraction depend on third-party pages that change without notice.
- Playlists/favourites are per user across your server (not shared between users).

License: MIT.
