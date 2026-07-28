"""
Discord TTS Bot
----------------
Joins a voice channel and reads aloud text messages sent in the text
channel where it was summoned, plus music playback via yt-dlp.

See README.md for setup, configuration, and the full command list.
"""

import array
import asyncio
import os
import json
import time
from collections import defaultdict, deque

import aiohttp
import discord
import yt_dlp
from discord.ext import commands
from dotenv import load_dotenv
from gtts import gTTS

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Loads variables from a .env file in this folder into the environment.
# See README.md / .env.example for how to set this up.
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Your Discord user ID. When set, the bot will auto-join your voice channel
# the moment you send a message (if it isn't already connected), instead of
# requiring !join. Leave blank/unset to disable this behavior.
# To get your ID: enable Developer Mode in Discord (Settings > Advanced),
# then right-click your name anywhere and choose "Copy User ID".
OWNER_USER_ID = os.getenv("OWNER_USER_ID")
OWNER_USER_ID = int(OWNER_USER_ID) if OWNER_USER_ID else None

COMMAND_PREFIX = "!"
MAX_MESSAGE_LENGTH = 200  # truncate very long messages before speaking them

# Music volume while idle vs. while a TTS clip is speaking over it.
MUSIC_NORMAL_VOLUME = 1.0
MUSIC_DUCK_VOLUME = 0.15

# For auto-read chat messages: how long (in seconds) someone can keep
# talking before the bot re-announces their name. Consecutive messages
# from the same person within this window are read without the name
# prefix, even when xsaid is enabled.
NAME_REPEAT_COOLDOWN = 10

# guild_id, user_id) -> monotonic timestamp of their last auto-read message
last_spoken_at: dict[tuple[int, int], float] = {}

# Which TTS engine to use for the owner's messages (see OWNER_USER_ID
# below): "gtts" (default, free/generic voice) or "elevenlabs" (cloud API,
# your own cloned voice or any ElevenLabs stock/library voice). Everyone
# else's messages are always read with gtts, regardless of this setting.
TTS_ENGINE = os.getenv("TTS_ENGINE", "gtts").lower()
TTS_LANGUAGE = "th"  # gTTS language code, e.g. "en", "es", "fr", "ja", "th"

# ElevenLabs settings (only used when TTS_ENGINE=elevenlabs)
# Get an API key from https://elevenlabs.io/app/settings/api-keys
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
# The voice to use. Find voice IDs at https://elevenlabs.io/app/voice-library
# or under your own cloned voices at https://elevenlabs.io/app/voice-lab
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
# Model to use for generation. eleven_v3 supports 70+ languages including
# Thai (Multilingual v2 does not support Thai). It's a research-preview
# model, so quality/behavior may shift over time, but it's the only
# current option that will actually speak Thai text correctly.
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_v3")

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # required to read message text
intents.voice_states = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# Per-guild state: which text channel is "bound" for TTS, the guild's
# audio mixer (see GuildMixer below), and a counter used to generate
# unique TTS filenames.
bound_text_channel: dict[int, int] = {}
guild_mixers: dict[int, "GuildMixer"] = {}
audio_file_counters: dict[int, int] = defaultdict(int)

# Saved volume settings
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
VOLUME_FILE = os.path.join(DATA_DIR, "setting.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "setting.json")


def load_volumes():
    if not os.path.exists(VOLUME_FILE):
        return {}

    with open(VOLUME_FILE, "r") as f:
        return json.load(f)


def save_volumes():
    with open(VOLUME_FILE, "w") as f:
        json.dump(guild_volumes, f, indent=4)

def load_guild_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return {}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_guild_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(guild_settings, f, indent=4, ensure_ascii=False)

guild_settings = load_guild_settings()
guild_volumes = load_volumes()

AUDIO_DIR = "tts_audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# 20ms of 48kHz, 16-bit, stereo PCM -- the frame size discord.py's voice
# pipeline expects from every AudioSource.read() call.
FRAME_BYTES = 3840
SILENCE_FRAME = b"\x00" * FRAME_BYTES

NAMES_FILE = os.path.join(DATA_DIR, "user_names.json")

def find_members(guild: discord.Guild, query: str) -> list[discord.Member]:
    """Find all members in the server matching display name, username, or nick."""
    query = query.lower().strip()
    
    matches = []
    # Match if username or display name starts with or contains the query
    for member in guild.members:
        if (
            query in member.name.lower()
            or query in member.display_name.lower()
        ):
            matches.append(member)

    return matches

async def prompt_member_selection(ctx: commands.Context, matches: list[discord.Member]) -> discord.Member | None:
    """Displays options if multiple users match, and waits for user choice."""
    if len(matches) == 1:
        return matches[0]

    # Display up to 5 matching options
    options = matches[:5]
    msg_content = "🔍 **Multiple users found. Please reply with the number:**\n"
    for idx, member in enumerate(options, start=1):
        msg_content += f"`{idx}.` **{member.display_name}** (`@{member.name}`)\n"

    prompt_msg = await ctx.send(msg_content)

    def check(m: discord.Message):
        return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit()

    try:
        reply = await bot.wait_for("message", check=check, timeout=15.0)
        choice = int(reply.content)
        if 1 <= choice <= len(options):
            return options[choice - 1]
        else:
            await ctx.send("Invalid option number.")
            return None
    except asyncio.TimeoutError:
        await ctx.send("Timed out waiting for selection.")
        return None

def load_user_names() -> dict[str, str]:
    if not os.path.exists(NAMES_FILE):
        return {}
    with open(NAMES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_user_names():
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(user_names, f, indent=4, ensure_ascii=False)

user_names: dict[str, str] = load_user_names()

def get_guild_setting(guild_id: int, key: str, default):
    """Retrieve a specific setting for a guild."""
    g_id = str(guild_id)
    if g_id not in guild_settings or not isinstance(guild_settings[g_id], dict):
        return default
    return guild_settings[g_id].get(key, default)


def set_guild_setting(guild_id: int, key: str, value):
    """Save a specific setting for a guild."""
    g_id = str(guild_id)
    if g_id not in guild_settings or not isinstance(guild_settings[g_id], dict):
        # Migrate old flat structure if it existed
        old_vol = (
            guild_settings[g_id] if isinstance(guild_settings.get(g_id), (int, float)) else 1.0
        )
        guild_settings[g_id] = {"volume": old_vol, "xsaid_enabled": True}

    guild_settings[g_id][key] = value
    save_guild_settings()

class GuildMixer(discord.AudioSource):
    """A persistent, always-on audio source for one guild's voice call.

    Discord only lets a voice client play a single AudioSource at a
    time, so to have TTS "duck" music instead of just queueing after
    it, this class owns two independent lanes -- a music queue and a
    speech queue -- and mixes their PCM output together on every
    20ms frame:

      - Music plays continuously, advancing to the next queued track
        whenever the current one ends.
      - Whenever a TTS clip is queued, it's layered on top of
        whatever music is playing, the music's volume is dropped to
        MUSIC_DUCK_VOLUME for the clip's duration, and restored to
        MUSIC_NORMAL_VOLUME the moment the clip finishes.
      - When both lanes are empty, silence is returned rather than
        ending the stream, so the voice connection stays open and
        !join / !play only need to call voice_client.play() once.
    """

    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.music_queue: deque = deque()
        self.speech_queue: deque = deque()

        self._music_source: discord.PCMVolumeTransformer | None = None
        self._music_item: dict | None = None
        self._speech_source: discord.FFmpegPCMAudio | None = None
        self._speech_item: dict | None = None

    # -- queueing -----------------------------------------------------

    def queue_music(self, item: dict):
        self.music_queue.append(item)

    def queue_speech(self, item: dict):
        self.speech_queue.append(item)

    @property
    def is_active(self) -> bool:
        return self._speech_source is not None or self._music_source is not None

    @property
    def now_playing_title(self):
        return self._music_item.get("title") if self._music_item else None

    def clear_music(self):
        self.music_queue.clear()
        self._finish_music()

    def clear_speech(self):
        self.speech_queue.clear()
        self._finish_speech()

    def skip_current(self):
        """Skip whichever lane is currently audible: a speaking TTS
        clip takes priority over the song underneath it."""
        if self._speech_source is not None:
            self._finish_speech()
        elif self._music_source is not None:
            self._finish_music()

    # -- internal lane management --------------------------------------

    def _advance_music(self):
        if not self.music_queue:
            self._music_item = None
            self._music_source = None
            return
        self._music_item = self.music_queue.popleft()
        ffmpeg_source = discord.FFmpegPCMAudio(
            self._music_item["source"],
            before_options=FFMPEG_STREAM_BEFORE_OPTIONS,
            options=FFMPEG_STREAM_OPTIONS,
        )
        normal_volume = float(
            guild_volumes.get(str(self.guild.id), MUSIC_NORMAL_VOLUME)
        )

        starting_volume = (
            MUSIC_DUCK_VOLUME
            if self._speech_source is not None
            else normal_volume
        )
        self._music_source = discord.PCMVolumeTransformer(ffmpeg_source, volume=starting_volume)

    def _finish_music(self):
        if self._music_source is not None:
            self._music_source.cleanup()
        self._music_item = None
        self._music_source = None

    def _advance_speech(self):
        if not self.speech_queue:
            self._speech_item = None
            self._speech_source = None
            self._restore_volume()
            return
        self._speech_item = self.speech_queue.popleft()
        self._speech_source = discord.FFmpegPCMAudio(self._speech_item["source"])
        self._duck_volume()

    def _finish_speech(self):
        item = self._speech_item
        if self._speech_source is not None:
            self._speech_source.cleanup()
        self._speech_source = None
        self._speech_item = None
        if item is not None and item.get("cleanup"):
            try:
                os.remove(item["source"])
            except OSError:
                pass
        self._restore_volume()

    def _duck_volume(self):
        if self._music_source is not None:
            self._music_source.volume = MUSIC_DUCK_VOLUME

    def _restore_volume(self):
        if self._music_source is not None:
            self._music_source.volume = MUSIC_NORMAL_VOLUME

    # -- discord.AudioSource interface ---------------------------------

    def read(self) -> bytes:
        if self._music_source is None and self.music_queue:
            self._advance_music()
        if self._speech_source is None and self.speech_queue:
            self._advance_speech()

        music_bytes = b""
        if self._music_source is not None:
            music_bytes = self._music_source.read()
            if not music_bytes:
                self._finish_music()
                self._advance_music()
                if self._music_source is not None:
                    music_bytes = self._music_source.read()

        speech_bytes = b""
        if self._speech_source is not None:
            speech_bytes = self._speech_source.read()
            if not speech_bytes:
                self._finish_speech()
                self._advance_speech()
                if self._speech_source is not None:
                    speech_bytes = self._speech_source.read()

        if not music_bytes and not speech_bytes:
            return SILENCE_FRAME

        return self._mix(music_bytes, speech_bytes)

    @staticmethod
    def _mix(a: bytes, b: bytes) -> bytes:
        if not a:
            return b
        if not b:
            return a
        if len(a) < len(b):
            a = a + b"\x00" * (len(b) - len(a))
        elif len(b) < len(a):
            b = b + b"\x00" * (len(a) - len(b))
        samples_a = array.array("h")
        samples_a.frombytes(a)
        samples_b = array.array("h")
        samples_b.frombytes(b)
        mixed = array.array(
            "h", (max(-32768, min(32767, x + y)) for x, y in zip(samples_a, samples_b))
        )
        return mixed.tobytes()

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        self._finish_music()
        self._finish_speech()
        self.music_queue.clear()
        self.speech_queue.clear()


def _make_audio_file_gtts(text: str, path: str) -> None:
    tts = gTTS(text=text, lang=TTS_LANGUAGE)
    tts.save(path)


async def _make_audio_file_elevenlabs(text: str, path: str) -> None:
    """Call the ElevenLabs API to generate audio using a chosen voice."""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not set in your .env file")
    if not ELEVENLABS_VOICE_ID:
        raise RuntimeError("ELEVENLABS_VOICE_ID is not set in your .env file")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }

    # Explicit timeout so a slow/overloaded API fails fast and falls back
    # to gTTS instead of blocking for minutes.
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"ElevenLabs API error {resp.status}: {body[:300]}"
                )
            data = await resp.read()

    with open(path, "wb") as f:
        f.write(data)


YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",  # bare queries -> take the first search hit
    "source_address": "0.0.0.0",
    # YouTube is rolling out SABR-only streaming for some player clients,
    # which withholds direct format URLs and causes "Requested format is
    # not available" even though the video plays fine in a browser.
    # android/ios clients still return direct URLs; web is kept as a
    # fallback. See https://github.com/yt-dlp/yt-dlp/issues/12482
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web"],
        }
    },
}

# YouTube sometimes demands sign-in verification, especially from
# datacenter/VPS IPs, regardless of the video. Exporting cookies from a
# logged-in browser session works around this. Set COOKIES_FILE to point
# at an exported cookies.txt (Netscape format) if you hit
# "Please sign in" errors. Left unset, yt-dlp just runs without cookies.
COOKIES_FILE = os.getenv("COOKIES_FILE")
if COOKIES_FILE and os.path.exists(COOKIES_FILE):
    YTDL_OPTIONS["cookiefile"] = COOKIES_FILE

# Reconnect flags recommended by discord.py for long-lived network streams,
# since a YouTube stream URL can drop mid-song.
FFMPEG_STREAM_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_STREAM_OPTIONS = "-vn"


def _extract_youtube_audio(query: str) -> dict:
    """Resolve a search query or URL to a direct, playable audio stream URL.

    Runs in a worker thread (it's blocking network + parsing work).
    """
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:  # search results come back as a playlist-like dict
            if not info["entries"]:
                raise RuntimeError("No results found.")
            info = info["entries"][0]
        return {
            "url": info["url"],
            "title": info.get("title", "Unknown title"),
            "webpage_url": info.get("webpage_url", query),
        }


async def make_audio_file(text: str, guild_id: int, use_elevenlabs: bool) -> str:
    """Generate an audio file for the given text and return its path.

    use_elevenlabs is only honored when TTS_ENGINE=elevenlabs; it's how
    callers restrict the ElevenLabs voice to the owner while everyone
    else gets gtts.
    """
    audio_file_counters[guild_id] += 1
    path = os.path.join(AUDIO_DIR, f"{guild_id}_{audio_file_counters[guild_id]}.mp3")

    if TTS_ENGINE == "elevenlabs" and use_elevenlabs:
        try:
            await _make_audio_file_elevenlabs(text, path)
        except Exception as e:
            print(f"ElevenLabs generation failed, falling back to gTTS: {e}")
            await asyncio.to_thread(_make_audio_file_gtts, text, path)
    else:
        await asyncio.to_thread(_make_audio_file_gtts, text, path)

    return path


def _ensure_mixer(guild: discord.Guild) -> GuildMixer:
    """Get (or lazily create) this guild's persistent audio mixer."""
    mixer = guild_mixers.get(guild.id)
    if mixer is None:
        mixer = GuildMixer(guild)
        guild_mixers[guild.id] = mixer
    return mixer


def _start_mixer_if_needed(guild: discord.Guild):
    """Attach the guild's mixer to its voice connection, if not already
    playing. The mixer is a persistent source (it emits silence when
    idle), so this only needs to run once per voice session."""
    mixer = guild_mixers.get(guild.id)
    voice_client = guild.voice_client
    if mixer is not None and voice_client is not None and not voice_client.is_playing():
        voice_client.play(mixer)


def format_speech_text(
    guild_id: int,
    author: discord.abc.User,
    text: str,
    check_recent: bool = False,
) -> str:
    """Apply the 'xsaid' name-prefix format if enabled for this guild,
    otherwise return the text unchanged.

    If check_recent is True, the name is only added when this author
    hasn't spoken (via auto-read) in the last NAME_REPEAT_COOLDOWN
    seconds in this guild -- so someone typing several messages in a
    row only gets announced once.
    """
    if not get_guild_setting(guild_id, "xsaid_enabled", True):
        return text

    if check_recent:
        key = (guild_id, author.id)
        now = time.monotonic()
        last = last_spoken_at.get(key)
        last_spoken_at[key] = now
        if last is not None and (now - last) < NAME_REPEAT_COOLDOWN:
            return text

    speaker_name = user_names.get(str(author.id), author.display_name)
    return f"{speaker_name} พูดว่า: {text}"


async def queue_speech(guild: discord.Guild, text: str, is_owner: bool = False):
    """Generate a TTS clip and layer it on top of the guild's mixer.

    If music is currently playing, its volume is automatically ducked
    for the duration of the clip and restored afterward (see
    GuildMixer).
    """
    text = text.strip()
    if not text:
        return
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH] + "..."

    mixer = guild_mixers.get(guild.id)
    if mixer is None:
        return  # not connected to voice in this guild

    path = await make_audio_file(text, guild.id, use_elevenlabs=is_owner)
    mixer.queue_speech({"source": path, "cleanup": True})
    _start_mixer_if_needed(guild)


async def queue_music(ctx: commands.Context, query: str):
    """Resolve a YouTube query/URL and add it to this guild's music lane."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is None:
        await ctx.send("I'm not in a voice channel.")
        return

    status = await ctx.send(f"\U0001F50E Looking up **{query}**...")
    try:
        info = await asyncio.to_thread(_extract_youtube_audio, query)
    except Exception as e:
        await status.edit(content=f"Couldn't find or play that: {e}")
        return

    mixer.queue_music({"source": info["url"], "title": info["title"]})
    current_volume = int(
        float(guild_volumes.get(str(ctx.guild.id), 1.0)) * 100
    )

    await status.edit(
        content=(
            f"🎵 Queued: **{info['title']}**\n"
            f"🔊 Volume: **{current_volume}%**\n"
            f"📃 Queue: **{len(mixer.music_queue)}**"
        )
    )
    _start_mixer_if_needed(ctx.guild)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.command()
async def join(ctx: commands.Context):
    """Join the caller's current voice channel and bind this text channel."""
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("You need to be in a voice channel first.")
        return

    channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect()
    else:
        await ctx.voice_client.move_to(channel)

    _ensure_mixer(ctx.guild)
    _start_mixer_if_needed(ctx.guild)

    bound_text_channel[ctx.guild.id] = ctx.channel.id
    await ctx.send(
        f"Joined **{channel.name}**. I'll read messages sent in "
        f"**#{ctx.channel.name}** out loud. Use `!leave` to stop."
    )


@bot.command()
async def leave(ctx: commands.Context):
    """Leave the voice channel and stop reading messages."""
    if ctx.voice_client is not None:
        await ctx.voice_client.disconnect()
        bound_text_channel.pop(ctx.guild.id, None)
        mixer = guild_mixers.pop(ctx.guild.id, None)
        if mixer is not None:
            mixer.cleanup()
        await ctx.send("Left the voice channel.")
    else:
        await ctx.send("I'm not in a voice channel.")


@bot.command()
async def tts(ctx: commands.Context, *, text: str):
    """Force-read a specific piece of text."""
    if ctx.voice_client is None:
        await ctx.send("I'm not in a voice channel. Use `!join` first.")
        return
    is_owner = OWNER_USER_ID is not None and ctx.author.id == OWNER_USER_ID
    full_text = format_speech_text(ctx.guild.id, ctx.author, text)
    await queue_speech(ctx.guild, full_text, is_owner=is_owner)


@bot.command(aliases=["p"])
async def play(ctx: commands.Context, *, query: str):
    """Search YouTube (or take a direct URL) and queue it for playback."""
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("You need to be in a voice channel first.")
        return

    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()
        # Bind this text channel too, so TTS also works without a separate !join.
        bound_text_channel.setdefault(ctx.guild.id, ctx.channel.id)

    _ensure_mixer(ctx.guild)
    await queue_music(ctx, query)


@bot.command(aliases=["sk"])
async def skip(ctx: commands.Context):
    """Skip the message or song currently playing."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is not None and mixer.is_active:
        mixer.skip_current()
        await ctx.send("Skipped.")
    else:
        await ctx.send("Nothing is playing.")


@bot.command(aliases=["st"])
async def stop(ctx: commands.Context):
    """Stop playback and clear the entire queue (TTS messages and songs)."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is not None:
        mixer.clear_music()
        mixer.clear_speech()
    await ctx.send("Stopped and cleared the queue.")

@bot.command(aliases=["q"])
async def queue(ctx: commands.Context):
    """Display the current music queue."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is None or (not mixer.now_playing_title and not mixer.music_queue):
        await ctx.send("The queue is currently empty.")
        return

    msg = ""
    if mixer.now_playing_title:
        msg += f"**Now Playing:** {mixer.now_playing_title}\n\n"

    if mixer.music_queue:
        msg += "**Up Next:**\n"
        for idx, item in enumerate(list(mixer.music_queue)[:10], start=1):
            msg += f"`{idx}.` {item['title']}\n"

        if len(mixer.music_queue) > 10:
            msg += f"\n*...and {len(mixer.music_queue) - 10} more track(s)*"
    else:
        msg += "*No remaining tracks in queue.*"

    await ctx.send(msg)


@bot.command(aliases=["np"])
async def nowplaying(ctx: commands.Context):
    """Display the title of the song currently playing."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer and mixer.now_playing_title:
        status = " (Paused)" if mixer.is_paused else ""
        await ctx.send(f"🎶 **Now Playing:** {mixer.now_playing_title}{status}")
    else:
        await ctx.send("Nothing is currently playing.")


@bot.command()
async def pause(ctx: commands.Context):
    """Pause playback."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is not None and not mixer.is_paused:
        mixer.pause()
        await ctx.send("⏸️ Paused playback.")
    elif mixer and mixer.is_paused:
        await ctx.send("Playback is already paused.")
    else:
        await ctx.send("Nothing is playing to pause.")


@bot.command()
async def resume(ctx: commands.Context):
    """Resume audio playback."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is not None and mixer.is_paused:
        mixer.resume()
        await ctx.send("▶️ Resumed playback.")
    elif mixer and not mixer.is_paused:
        await ctx.send("Playback is not paused.")
    else:
        await ctx.send("Nothing is paused.")

@bot.command(aliases=["v", "vol"])
async def volume(ctx: commands.Context, level: int):
    """Set music volume (0-100%)."""
    if level < 0 or level > 100:
        await ctx.send("Volume must be between 0 and 100.")
        return

    vol = level / 100
    set_guild_setting(ctx.guild.id, "volume", vol)

    mixer = guild_mixers.get(ctx.guild.id)
    if mixer and mixer._music_source:
        mixer._music_source.volume = vol

    await ctx.send(f"🔊 Volume set to **{level}%**")

@bot.command(aliases=["setname", "customname"])
async def name(ctx: commands.Context, target_input: str = None, *, custom_name: str = None):
    """
    Set or clear a custom TTS name. Shows options if multiple members match!
    """
    target_member = None

    if target_input is None:
        target_member = ctx.author
    else:
        # First try direct converter (Mention / ID)
        try:
            target_member = await commands.MemberConverter().convert(ctx, target_input)
        except commands.MemberNotFound:
            # Search for matching members
            matches = find_members(ctx.guild, target_input)

            if len(matches) > 1:
                target_member = await prompt_member_selection(ctx, matches)
                if target_member is None:
                    return  # Selection cancelled or timed out
            elif len(matches) == 1:
                target_member = matches[0]
            else:
                # If no members matched, treat target_input as the user setting THEIR OWN custom name
                custom_name = f"{target_input} {custom_name}" if custom_name else target_input
                target_member = ctx.author

    target_id_str = str(target_member.id)

    # Protection check: Only owner can edit owner's custom name
    if OWNER_USER_ID is not None and target_member.id == OWNER_USER_ID and ctx.author.id != OWNER_USER_ID:
        await ctx.send("🔒 You cannot change or clear the bot owner's custom name!")
        return

    # Clear custom name
    if not custom_name:
        if target_id_str in user_names:
            del user_names[target_id_str]
            save_user_names()
            await ctx.send(f"Cleared custom TTS name for **{target_member.display_name}**.")
        else:
            await ctx.send(f"**{target_member.display_name}** does not have a custom TTS name set.")
        return

    # Set custom name
    new_name = custom_name.strip()
    user_names[target_id_str] = new_name
    save_user_names()

    if target_member == ctx.author:
        await ctx.send(f"Your TTS name has been set to: **{new_name}**")
    else:
        await ctx.send(f"TTS name for **{target_member.display_name}** has been set to: **{new_name}**")

@bot.command(aliases=["myname"])
async def checkname(ctx: commands.Context):
    """Check your current custom TTS name."""
    user_id_str = str(ctx.author.id)
    saved = user_names.get(user_id_str)
    if saved:
        await ctx.send(f"Your custom TTS name is: **{saved}**")
    else:
        await ctx.send(f"You don't have a custom name set (using Discord display name: **{ctx.author.display_name}**).")

@bot.command(aliases=["xsaid", "say", "speak"])
async def said(ctx: commands.Context, *, text: str):
    """Speaks a message if xsaid is enabled."""
    is_enabled = get_guild_setting(ctx.guild.id, "xsaid_enabled", True)
    if not is_enabled:
        await ctx.send("`!xsaid` is currently turned off on this server. Enable it with `!xsaidtoggle on`.")
        return

    if ctx.voice_client is None:
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("You need to be in a voice channel first.")
            return

        await ctx.author.voice.channel.connect()
        bound_text_channel.setdefault(ctx.guild.id, ctx.channel.id)

    _ensure_mixer(ctx.guild)

    full_text = format_speech_text(ctx.guild.id, ctx.author, text)
    is_owner = OWNER_USER_ID is not None and ctx.author.id == OWNER_USER_ID
    await queue_speech(ctx.guild, full_text, is_owner=is_owner)

@bot.command(aliases=["togglexsaid", "xsaidmode"])
async def xsaidtoggle(ctx: commands.Context, mode: str = None):
    """
    Turn xsaid on or off for this server.
    Usage:
      !xsaidtoggle on
      !xsaidtoggle off
      !xsaidtoggle        (toggles current state)
    """
    current_state = get_guild_setting(ctx.guild.id, "xsaid_enabled", True)

    if mode is not None:
        mode_lower = mode.lower()
        if mode_lower in ["on", "enable", "true", "1"]:
            new_state = True
        elif mode_lower in ["off", "disable", "false", "0"]:
            new_state = False
        else:
            await ctx.send("Invalid option. Use `!xsaidtoggle on` or `!xsaidtoggle off`.")
            return
    else:
        new_state = not current_state

    set_guild_setting(ctx.guild.id, "xsaid_enabled", new_state)
    status_str = "**ENABLED** 🟢" if new_state else "**DISABLED** 🔴"
    await ctx.send(f"`!xsaid` feature is now {status_str} for this server.")

@bot.event
async def on_message(message: discord.Message):
    # Always let commands (like !join, !leave) get processed.
    await bot.process_commands(message)

    if message.author.bot or message.guild is None:
        return
    if message.content.startswith(COMMAND_PREFIX):
        return  # don't read out commands themselves

    voice_client = message.guild.voice_client

    # Auto-join: if this message is from the configured owner, the bot
    # isn't already in a voice channel in this guild, and the owner is
    # currently in one, join it and bind this text channel automatically.
    if (
        OWNER_USER_ID is not None
        and message.author.id == OWNER_USER_ID
        and voice_client is None
        and message.author.voice is not None
        and message.author.voice.channel is not None
    ):
        voice_client = await message.author.voice.channel.connect()
        bound_text_channel[message.guild.id] = message.channel.id
        _ensure_mixer(message.guild)
        await message.channel.send(
            f"Auto-joined **{message.author.voice.channel.name}** — "
            f"reading messages from **#{message.channel.name}**.",
            delete_after=10,
        )

    bound_channel_id = bound_text_channel.get(message.guild.id)
    if bound_channel_id is None or message.channel.id != bound_channel_id:
        return

    if voice_client is None:
        return

    is_owner = OWNER_USER_ID is not None and message.author.id == OWNER_USER_ID
    full_text = format_speech_text(
        message.guild.id, message.author, message.content, check_recent=True
    )
    await queue_speech(message.guild, full_text, is_owner=is_owner)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Automatically leaves the voice channel if no human members remain."""
    # We only care if someone left a voice channel
    if before.channel is None:
        return

    voice_client = member.guild.voice_client

    # Check if the bot is connected to the channel where someone just left
    if voice_client is not None and voice_client.channel.id == before.channel.id:
        # Filter out bots to count only human users
        human_members = [m for m in before.channel.members if not m.bot]

        # If no humans are left in the channel, disconnect
        if len(human_members) == 0:
            guild_id = member.guild.id
            
            # Disconnect and clean up state
            await voice_client.disconnect()
            bound_text_channel.pop(guild_id, None)
            mixer = guild_mixers.pop(guild_id, None)
            if mixer is not None:
                mixer.cleanup()

            print(f"Auto-disconnected from empty voice channel in guild {guild_id}.")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "No bot token found. Set the DISCORD_BOT_TOKEN environment "
            "variable before running the bot (see README.md)."
        )
    bot.run(TOKEN)