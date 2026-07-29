# Discord TTS Bot

Joins a voice channel and reads chat messages aloud as text-to-speech,
with optional YouTube music playback mixed in on the same voice
connection.

## Features

- Reads messages typed in a bound text channel aloud in voice chat.
- Two TTS engines: free Google TTS (gTTS) or ElevenLabs for
  higher-quality / cloned voices.
- Music playback via YouTube search, direct link, or full playlist
  (`yt-dlp`), sharing the same voice connection — music automatically
  ducks in volume whenever a TTS clip plays over it.
- Queue management: skip, jump to a specific track, or insert a track
  to play next.
- Per-user custom TTS names, and an optional "announce who's talking"
  mode (`xsaid`).
- Auto-joins a configured owner's voice channel, and auto-leaves an
  empty channel.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
   (discord.py, yt-dlp, aiohttp, python-dotenv, gTTS — plus `ffmpeg`, e.g.
   installed and on your PATH.)

2. Create a `.env` file in this folder with at least:
   ```
   DISCORD_BOT_TOKEN=your-bot-token-here
   ```

3. Optional `.env` settings:

   | Variable | Purpose |
   |---|---|
   | `OWNER_USER_ID` | Your Discord user ID. When set, the bot auto-joins your voice channel the moment you send a message, instead of requiring `!join`. Enable Developer Mode in Discord (Settings → Advanced), then right-click your name and "Copy User ID". |
   | `TTS_ENGINE` | `gtts` (default, free/generic voice) or `elevenlabs` (cloud API, higher quality). Only affects the owner's voice — everyone else always uses gTTS. |
   | `ELEVENLABS_API_KEY` | Required if `TTS_ENGINE=elevenlabs`. Get one at https://elevenlabs.io/app/settings/api-keys |
   | `ELEVENLABS_VOICE_ID` | Required if `TTS_ENGINE=elevenlabs`. Find voice IDs at https://elevenlabs.io/app/voice-library or your own cloned voices at https://elevenlabs.io/app/voice-lab |
   | `ELEVENLABS_MODEL_ID` | Defaults to `eleven_v3`, needed for Thai support (the bot's default TTS language). |
   | `COOKIES_FILE` | Path to a Netscape-format `cookies.txt` exported from a logged-in browser session. Only needed if YouTube starts demanding sign-in verification (common from datacenter/VPS IPs). Left unset, `yt-dlp` just runs without cookies. |
   | `MAX_PLAYLIST_TRACKS` | Max number of tracks `!playlist` will queue from one playlist. Defaults to `30`. Set to a very large number to effectively remove the cap (not recommended — large playlists take a long time to extract and flood the queue). |
   | `DATA_DIR` | Folder where custom names and per-server settings are saved. Defaults to `data`. |

4. Run the bot:
   ```
   python bot.py
   ```

## Commands

All commands use the `!` prefix.

### Voice & TTS

| Command | Aliases | Description |
|---|---|---|
| `!join` | | Joins your current voice channel and starts reading messages from this text channel. |
| `!leave` | | Leaves the voice channel and stops reading. |
| `!tts <text>` | | Force-reads a specific piece of text (bot must already be in voice). |
| `!xsaid <text>` | `!say`, `!speak` | Speaks `<text>` with your name announced (e.g. "Alex said: hello"), joining your voice channel first if needed. Only works if `xsaid` is enabled for the server. |
| `!xsaidtoggle [on\|off]` | `!togglexsaid`, `!xsaidmode` | Turns the `xsaid` name-announcing behavior on/off for this server. With no argument, flips the current state. |

**About `xsaid`:** when enabled, the bot announces the speaker's name before reading their message — both for `!xsaid`/`!tts` and for normal auto-read chat messages. For auto-read chat, the name is only re-announced if that person hasn't spoken in the last 10 seconds, so someone sending several messages in a row isn't announced every time.

### Names

| Command | Aliases | Description |
|---|---|---|
| `!name [user] [custom name]` | `!setname`, `!customname` | Sets a custom name used when announcing that person via `xsaid`. With no arguments, sets your own name. Supports mentions, IDs, or partial name search (prompts you to pick if multiple members match). The bot owner's name can only be changed by the owner. |
| `!checkname` | `!myname` | Shows your current custom TTS name. |

### Music

| Command | Aliases | Description |
|---|---|---|
| `!play <query or URL>` | `!p` | Searches YouTube (or plays a direct link) and queues it. Shares the same queue/mixer as TTS. |
| `!stream <url>` | `!str` | Plays a single link directly (YouTube, Twitch, SoundCloud, internet radio/Icecast, etc.), replacing anything currently playing or queued. Good for live streams with no fixed end. |
| `!playlist <URL>` | `!pl` | Queues an entire YouTube playlist (up to `MAX_PLAYLIST_TRACKS` tracks). Each track's playable stream is resolved just before it plays, so stream URLs don't expire while sitting in a long queue. |
| `!skip` | `!sk` | Skips whatever's currently playing (a TTS clip takes priority over the song under it). |
| `!skipto <number>` | `!jumpto`, `!goto` | Jumps straight to a specific track in the queue (see `!queue` for numbers), discarding everything before it. |
| `!insert <query or URL>` | `!ins`, `!playnext` | Looks up a track and puts it at the front of the queue, so it plays right after the current one. |
| `!stop` | `!st` | Stops playback and clears the entire queue (TTS + music). |
| `!queue` | `!q` | Shows the current music queue. |
| `!nowplaying` | `!np` | Shows the title of the current song. |
| `!pause` | | Pauses playback. |
| `!resume` | | Resumes playback. |
| `!volume <0-100>` | `!v`, `!vol` | Sets music volume as a percentage. |

## Notes

- Messages starting with `!` are treated as commands and are not read aloud.
- The bot automatically leaves a voice channel once no human members remain in it.
- Custom names and per-server settings (volume, `xsaid_enabled`) are saved to local JSON files (`user_names.json`, `setting.json`) and persist across restarts.
