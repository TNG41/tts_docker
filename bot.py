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
import threading
import time
import uuid
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

# Chatbot (used by !chat/!ask and @mentioning the bot). Uses any
# OpenAI-compatible chat completions API. Defaults to Groq, which has a
# free tier -- get a key at https://console.groq.com/keys
#
# To use a different provider instead, set CHATBOT_API_BASE / _MODEL /
# _API_KEY accordingly, e.g.:
#   OpenAI:      https://api.openai.com/v1        gpt-4o-mini
#   OpenRouter:  https://openrouter.ai/api/v1      meta-llama/llama-3.1-8b-instruct:free
#   Ollama:      http://localhost:11434/v1         llama3.1 (no key needed)
CHATBOT_API_BASE = os.getenv("CHATBOT_API_BASE", "https://api.groq.com/openai/v1")
CHATBOT_MODEL = os.getenv("CHATBOT_MODEL", "llama-3.3-70b-versatile")
CHATBOT_API_KEY = os.getenv("CHATBOT_API_KEY", os.getenv("GROQ_API_KEY", ""))
CHATBOT_SYSTEM_PROMPT = os.getenv(
    "CHATBOT_SYSTEM_PROMPT",
    "You're chatting in a Discord voice channel like one of the group's "
    "friends, not a customer service bot. Talk casual -- short, "
    "conversational replies, no corporate politeness, no over-explaining. "
    "Swearing is fine if it fits naturally, don't force it. Give real "
    "opinions and push back if someone's wrong instead of hedging. "
    "Replies are also read aloud with text-to-speech, so keep them short.",
)
# How many past user/assistant exchanges to remember per channel for context.
CHAT_HISTORY_TURNS = int(os.getenv("CHAT_HISTORY_TURNS", "6"))

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

# Saved per-guild settings (volume, xsaid on/off, etc.)
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(DATA_DIR, "setting.json")


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

    await ctx.send(msg_content)

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
        self._paused = False

        # music_queue is touched from three different threads: the event
        # loop (commands adding tracks), the voice player thread (popping
        # the next track in _advance_music), and the prefetch task's
        # worker threads (downloading tracks in the background). This
        # lock guards every read/mutation of the deque itself.
        self._lock = threading.Lock()
        self.active = True  # flips to False in cleanup() to stop prefetching
        self.prefetch_task: asyncio.Task | None = None

    # -- queueing -----------------------------------------------------

    def queue_music(self, item: dict):
        with self._lock:
            self.music_queue.append(item)

    def insert_next(self, item: dict):
        with self._lock:
            self.music_queue.appendleft(item)

    def queue_speech(self, item: dict):
        self.speech_queue.append(item)

    @property
    def is_active(self) -> bool:
        return self._speech_source is not None or self._music_source is not None

    @property
    def now_playing_title(self):
        return self._music_item.get("title") if self._music_item else None

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self):
        """Freeze both lanes: read() will emit silence without consuming
        from either source, so playback resumes exactly where it left off."""
        self._paused = True

    def resume(self):
        self._paused = False

    def set_volume(self, level: float):
        """Apply a new normal-playback volume to whatever's currently
        playing. Only takes effect immediately if music isn't currently
        ducked for TTS -- the ducked level always wins while a clip is
        speaking, and the new normal level is picked up next time
        _advance_music or _restore_volume runs (both read the saved
        per-guild setting rather than a value passed in)."""
        if self._music_source is not None and self._speech_source is None:
            self._music_source.volume = level

    @staticmethod
    def _discard_music_item(item: dict):
        """Delete a queued (not-yet-played) track's downloaded file, if any."""
        if item.get("cleanup") and item.get("source"):
            try:
                os.remove(item["source"])
            except OSError:
                pass

    def clear_music(self):
        with self._lock:
            while self.music_queue:
                self._discard_music_item(self.music_queue.popleft())
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

    def skip_music(self):
        """End the current track regardless of whether TTS is speaking
        over it. Used by !skipto, which needs to jump within the music
        queue without being blocked by an unrelated TTS clip."""
        self._finish_music()

    def discard_upcoming(self, count: int) -> str | None:
        """Drop the next `count` queued (not-yet-playing) tracks, cleaning
        up any already-downloaded files. Used by !skipto. Returns the
        title of whatever's now at the front of the queue, if any."""
        with self._lock:
            for _ in range(count):
                if not self.music_queue:
                    break
                self._discard_music_item(self.music_queue.popleft())
            return self.music_queue[0]["title"] if self.music_queue else None

    def snapshot_queue(self) -> list[dict]:
        """A thread-safe copy of the queue's current contents, for display
        (e.g. !queue) without racing the player/prefetch threads."""
        with self._lock:
            return list(self.music_queue)

    # -- internal lane management --------------------------------------

    def _advance_music(self):
        with self._lock:
            if not self.music_queue:
                self._music_item = None
                self._music_source = None
                return
            item = self.music_queue.popleft()

        if item.get("source") == UNPLAYABLE:
            # The background prefetch task already tried and failed to
            # download this one -- don't retry, just move on.
            print(f"Skipping unplayable queued track '{item.get('title')}'")
            self._advance_music()
            return

        if item.get("source") is None:
            # Deferred playlist entry the prefetch task hasn't reached yet
            # (e.g. it's further out than PREFETCH_AHEAD, or the bot just
            # started). Fall back to downloading it right now. This runs
            # on the voice player thread, so it's fine to block here; it
            # just means a short pause for this one track instead of it
            # already being ready.
            try:
                resolved = _download_youtube_audio(item["webpage_url"])
                item["source"] = resolved["path"]
                item["title"] = resolved.get("title", item["title"])
                item["cleanup"] = True
            except Exception as e:
                print(f"Skipping unplayable queued track '{item.get('title')}': {e}")
                self._advance_music()  # try the next track instead of stalling
                return

        self._music_item = item
        if item.get("is_stream"):
            # Live/unbounded streams (see !stream) still play directly from
            # their URL, so keep the reconnect flags for network hiccups.
            ffmpeg_source = discord.FFmpegPCMAudio(
                self._music_item["source"],
                before_options=FFMPEG_STREAM_BEFORE_OPTIONS,
                options=FFMPEG_STREAM_OPTIONS,
            )
        else:
            # Downloaded local file -- no network involved during playback.
            ffmpeg_source = discord.FFmpegPCMAudio(self._music_item["source"])
        normal_volume = float(
            get_guild_setting(self.guild.id, "volume", MUSIC_NORMAL_VOLUME)
        )

        starting_volume = (
            MUSIC_DUCK_VOLUME
            if self._speech_source is not None
            else normal_volume
        )
        self._music_source = discord.PCMVolumeTransformer(ffmpeg_source, volume=starting_volume)
        self._announce_now_playing(item)

    def _announce_now_playing(self, item: dict):
        """Post a "Now Playing" message for a track that just started,
        whether it's the first one queued or an automatic transition
        after the previous song ended. Runs on the voice player thread,
        so the send is scheduled onto the bot's event loop instead of
        awaited directly."""
        channel_id = bound_text_channel.get(self.guild.id)
        if channel_id is None:
            return
        channel = self.guild.get_channel(channel_id)
        if channel is None:
            return
        title = item.get("title", "Unknown title")
        asyncio.run_coroutine_threadsafe(
            channel.send(f"🎶 **Now Playing:** {title}", silent=True), bot.loop
        )

    def _finish_music(self):
        item = self._music_item
        if self._music_source is not None:
            self._music_source.cleanup()
        self._music_item = None
        self._music_source = None
        if item is not None and item.get("cleanup"):
            try:
                os.remove(item["source"])
            except OSError:
                pass

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
            self._music_source.volume = float(
                get_guild_setting(self.guild.id, "volume", MUSIC_NORMAL_VOLUME)
            )

    # -- discord.AudioSource interface ---------------------------------

    def read(self) -> bytes:
        if self._paused:
            return SILENCE_FRAME

        if self._music_source is None and self.music_queue:
            self._advance_music()
        if self._speech_source is None and self.speech_queue:
            self._advance_speech()

        music_bytes = b""
        if self._music_source is not None:
            with self._lock:
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
        self.active = False
        if self.prefetch_task is not None:
            self.prefetch_task.cancel()
        self._finish_speech()
        self.speech_queue.clear()
        self.clear_music()


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


# -- Chatbot (see !chat/!ask and @mentions) ------------------------------

CHAT_TIMEOUT = aiohttp.ClientTimeout(total=30)

# channel_id -> deque of {"role": "user"|"assistant", "content": str},
# capped at CHAT_HISTORY_TURNS exchanges (2 entries per exchange), so each
# channel keeps its own short-term chatbot context.
chat_histories: dict[int, deque] = defaultdict(
    lambda: deque(maxlen=CHAT_HISTORY_TURNS * 2)
)

# Preset ("canned") answers. If the user's message matches one of these,
# the bot replies instantly with the saved text instead of calling the
# chat API -- handy for FAQs and saves free-tier quota. Managed with
# !preset add / !preset remove / !preset list. Keys are stored lowercase.
PRESET_ANSWERS_FILE = os.path.join(DATA_DIR, "preset_answers.json")


def load_preset_answers() -> dict[str, str]:
    if not os.path.exists(PRESET_ANSWERS_FILE):
        return {}
    with open(PRESET_ANSWERS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_preset_answers():
    with open(PRESET_ANSWERS_FILE, "w", encoding="utf-8") as f:
        json.dump(preset_answers, f, indent=4, ensure_ascii=False)


preset_answers: dict[str, str] = load_preset_answers()


def find_preset_answer(prompt: str) -> str | None:
    """Return a saved canned answer if `prompt` matches a trigger, else None.
    Tries an exact match first, then falls back to the trigger appearing
    anywhere in the message (so short triggers like "hours" still fire)."""
    prompt_lower = prompt.lower().strip()

    if prompt_lower in preset_answers:
        return preset_answers[prompt_lower]

    for trigger, answer in preset_answers.items():
        if trigger in prompt_lower:
            return answer

    return None


async def _get_chat_reply(channel_id: int, prompt: str) -> str:
    """Send `prompt` (plus this channel's recent history) to OpenAI's chat
    completions API, return the reply text, and update the history."""
    history = chat_histories[channel_id]
    messages = [{"role": "system", "content": CHATBOT_SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {CHATBOT_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": CHATBOT_MODEL,
        "messages": messages,
        "max_tokens": 300,
    }

    async with aiohttp.ClientSession(timeout=CHAT_TIMEOUT) as session:
        async with session.post(
            f"{CHATBOT_API_BASE}/chat/completions", headers=headers, json=payload
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Chatbot API error {resp.status}: {body[:300]}")
            data = await resp.json()

    reply = data["choices"][0]["message"]["content"].strip()

    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": reply})

    return reply


async def respond_with_chat(
    channel: discord.abc.Messageable,
    guild: discord.Guild,
    author: discord.abc.User,
    prompt: str,
):
    """Get an AI reply for `prompt`, send it as text, and also speak it
    aloud if the bot is currently connected to voice in this guild.
    Checks preset/canned answers first, before touching the API."""
    preset_reply = find_preset_answer(prompt)
    if preset_reply is not None:
        reply = preset_reply
        await channel.send(reply)
    else:
        needs_key = "localhost" not in CHATBOT_API_BASE and "127.0.0.1" not in CHATBOT_API_BASE
        if needs_key and not CHATBOT_API_KEY:
            await channel.send(
                "❌ Chatbot isn't configured. Set `CHATBOT_API_KEY` in your `.env`."
            )
            return

        async with channel.typing():
            try:
                reply = await _get_chat_reply(channel.id, prompt)
            except Exception as e:
                await channel.send(f"❌ Chatbot request failed: {e}")
                return

        await channel.send(reply)

    voice_client = guild.voice_client
    if voice_client is not None:
        try:
            _ensure_mixer(guild)
            is_owner = OWNER_USER_ID is not None and author.id == OWNER_USER_ID

            spoken_prompt = format_speech_text(guild.id, author, prompt)
            await queue_speech(guild, spoken_prompt, is_owner=is_owner)

            spoken_reply = format_speech_text(guild.id, bot.user, reply)
            await queue_speech(guild, spoken_reply, is_owner=is_owner)

            _start_mixer_if_needed(guild)
        except Exception as e:
            print(f"[chat TTS] failed to speak reply: {e!r}")
            await channel.send(f"⚠️ Got a reply, but couldn't speak it: {e}")


YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "retries": 1,  # <--- Stop searching/retrying after 5 failed attempts
    "fragment_retries": 1,  # <--- Max 5 retries for audio fragments
    "extractor_args": {
        "youtube": {
            "player_client": ["mweb", "tv", "web"],
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
    """Resolve a single search query or URL to a playable audio stream.

    Runs in a worker thread (it's blocking network + parsing work).
    """
    # Ensure noplaylist is True so !play only ever picks a single video
    options = dict(YTDL_OPTIONS)
    options["noplaylist"] = True

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:  # If a search term was used, grab the first hit
            if not info["entries"]:
                raise RuntimeError("No results found.")
            info = info["entries"][0]
        return {
            "url": info["url"],
            "title": info.get("title", "Unknown title"),
            "webpage_url": info.get("webpage_url", query),
        }


def _resolve_stream_source(url: str) -> dict:
    """Resolve a link for !stream to a directly playable source.

    Tries yt-dlp first, which handles YouTube videos, YouTube livestreams,
    and many other sites (Twitch, SoundCloud, etc.). If yt-dlp doesn't
    recognize the link at all -- e.g. a plain internet-radio/Icecast
    stream -- falls back to handing the URL straight to ffmpeg, which can
    play those formats directly without any resolving.
    """
    try:
        return _extract_youtube_audio(url)
    except Exception:
        return {"url": url, "title": url, "webpage_url": url}


# Where downloaded tracks are cached before playback (see
# _download_youtube_audio). Separate from AUDIO_DIR (TTS clips) since
# these files can be much larger.
MUSIC_CACHE_DIR = "music_audio"
os.makedirs(MUSIC_CACHE_DIR, exist_ok=True)


def _download_youtube_audio(query: str) -> dict:
    """Download a track's audio to a local file and return its path.

    !play/!insert/!playlist queue tracks with this instead of streaming
    the raw googlevideo URL live through ffmpeg. Streaming that URL
    directly is prone to YouTube throttling or dropping the connection
    mid-song ("Stream ends prematurely" / reconnect errors in the
    ffmpeg log), which causes audible stutter/lag even once ffmpeg
    reconnects. Downloading first means the actual voice playback reads
    from a stable local file with no network involved. !stream is the
    exception -- it's for live/unbounded streams, which can't be
    downloaded, so it keeps playing the live URL directly.

    Runs in a worker thread (it's blocking network + disk I/O).
    """
    options = dict(YTDL_OPTIONS)
    options["noplaylist"] = True
    options["outtmpl"] = os.path.join(
        MUSIC_CACHE_DIR, f"{uuid.uuid4().hex}_%(id)s.%(ext)s"
    )

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=True)
        if "entries" in info:  # If a search term was used, grab the first hit
            if not info["entries"]:
                raise RuntimeError("No results found.")
            info = info["entries"][0]
        path = ydl.prepare_filename(info)
        return {
            "path": path,
            "title": info.get("title", "Unknown title"),
            "webpage_url": info.get("webpage_url", query),
        }


# Max number of tracks !playlist will queue from a single playlist. Set to
# None to remove the cap (not recommended -- large playlists can take a
# long time to extract and will flood the queue).
MAX_PLAYLIST_TRACKS = int(os.getenv("MAX_PLAYLIST_TRACKS", "30"))

# How many upcoming deferred (playlist) tracks to keep downloaded ahead of
# time, so playback doesn't pause between songs waiting on a download.
PREFETCH_AHEAD = int(os.getenv("PREFETCH_AHEAD", "5"))

# Marks a queued track the prefetch task already tried and failed to
# download, so _advance_music skips it instead of retrying forever.
UNPLAYABLE = "__unplayable__"


async def _prefetch_loop(mixer: "GuildMixer"):
    """Background task: keep the next PREFETCH_AHEAD queued tracks that
    don't have a local file yet (i.e. deferred playlist entries) downloaded
    ahead of time. Runs for as long as the guild's mixer is active; started
    in _ensure_mixer and cancelled in GuildMixer.cleanup().
    """
    while mixer.active:
        try:
            pending = [
                item
                for item in mixer.snapshot_queue()[:PREFETCH_AHEAD]
                if item.get("source") is None
            ]
            for item in pending:
                if not mixer.active:
                    break
                try:
                    resolved = await asyncio.to_thread(
                        _download_youtube_audio, item["webpage_url"]
                    )
                    item["source"] = resolved["path"]
                    item["title"] = resolved.get("title", item["title"])
                    item["cleanup"] = True
                except Exception as e:
                    print(f"Prefetch failed for '{item.get('title')}': {e}")
                    item["source"] = UNPLAYABLE
        except Exception as e:
            print(f"Prefetch loop error: {e}")

        await asyncio.sleep(2)


def _extract_youtube_playlist(query: str) -> list[dict]:
    """Resolve a YouTube playlist URL (or playlist search) into multiple tracks.

    Stops after MAX_PLAYLIST_TRACKS entries.
    """
    options = dict(YTDL_OPTIONS)
    options["noplaylist"] = False
    options["extract_flat"] = "in_playlist"  # Fast-extract track list info
    if MAX_PLAYLIST_TRACKS:
        # Tells yt-dlp to stop paging once it has this many entries, so we
        # don't waste time/requests fetching tracks we'll just discard.
        options["playlistend"] = MAX_PLAYLIST_TRACKS

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=False)

        # Extract entries from a playlist dictionary
        entries = info.get("entries") if "entries" in info else [info]
        if not entries:
            raise RuntimeError("No playlist entries found.")

        tracks = []
        for entry in entries:
            if not entry:
                continue
            # extract_flat only gives lightweight metadata, not a playable
            # audio stream URL -- entry["url"] here is just the video ID or
            # webpage link, which ffmpeg can't play directly. Build a real
            # watch-page URL and leave "source" unset; GuildMixer resolves
            # it to an actual stream URL just before the track plays (see
            # _advance_music), which also avoids stream URLs expiring for
            # tracks that don't play until much later in a long playlist.
            video_id = entry.get("id")
            webpage_url = (
                entry.get("webpage_url")
                or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
                or entry.get("url")
            )
            if webpage_url:
                tracks.append({
                    "source": None,
                    "webpage_url": webpage_url,
                    "title": entry.get("title", "Unknown title"),
                })
            if MAX_PLAYLIST_TRACKS and len(tracks) >= MAX_PLAYLIST_TRACKS:
                # Defensive: playlistend isn't always honored exactly by
                # every YouTube extraction path, so also cap here.
                break
        return tracks

async def queue_playlist(ctx: commands.Context, query: str):
    """Resolve a playlist URL and add all its tracks to this guild's queue."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is None:
        await ctx.send("I'm not in a voice channel.", silent=True)
        return

    status = await ctx.send(f"🔎 Extracting playlist **{query}**...", silent=True)
    try:
        tracks = await asyncio.to_thread(_extract_youtube_playlist, query)
    except Exception as e:
        error_msg = str(e) if str(e) else "Failed to load playlist."
        await status.edit(content=f"❌ **Playlist search failed:**\nReason: {error_msg}")
        return

    for track in tracks:
        mixer.queue_music(track)

    vol = get_guild_setting(ctx.guild.id, "volume", MUSIC_NORMAL_VOLUME)
    current_volume = int(float(vol) * 100)

    capped_note = ""
    if MAX_PLAYLIST_TRACKS and len(tracks) >= MAX_PLAYLIST_TRACKS:
        capped_note = f"\n⚠️ Limited to the first **{MAX_PLAYLIST_TRACKS}** tracks."

    await status.edit(
        content=(
            f"🎶 **Queued Playlist!** Added **{len(tracks)}** tracks.\n"
            f"🔊 Volume: **{current_volume}%**\n"
            f"📃 Total Queue: **{len(mixer.music_queue)}**"
            f"{capped_note}"
        )
    )
    _start_mixer_if_needed(ctx.guild)


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
        mixer.prefetch_task = asyncio.create_task(_prefetch_loop(mixer))
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
    """Resolve a YouTube query/URL, download its audio, and add it to
    this guild's music lane. Downloads happen before the track is
    queued/played so playback always reads from a stable local file --
    streaming the raw googlevideo URL live is prone to YouTube
    throttling/dropping the connection mid-song, which causes audible
    stutter even once ffmpeg reconnects."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is None:
        await ctx.send("I'm not in a voice channel.", silent=True)
        return

    status = await ctx.send(f"🔎 Looking up **{query}**...", silent=True)

    try:
        info = await asyncio.to_thread(_download_youtube_audio, query)
    except Exception as e:
        await status.edit(content=f"❌ **Search failed for:** **{query}**\nReason: {e}")
        return

    mixer.queue_music({"source": info["path"], "title": info["title"], "cleanup": True})

    vol = get_guild_setting(ctx.guild.id, "volume", MUSIC_NORMAL_VOLUME)
    current_volume = int(float(vol) * 100)

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


async def _ensure_voice(ctx: commands.Context) -> bool:
    """Make sure the bot is connected to voice in this guild -- joining
    the caller's current channel if it isn't already -- and that this
    text channel is bound for TTS/now-playing messages. Sends an error
    and returns False if the caller isn't in a voice channel to join.

    Shared by !play, !stream, !playlist, and !said.
    """
    if ctx.voice_client is None:
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("You need to be in a voice channel first.", silent=True)
            return False
        await ctx.author.voice.channel.connect(self_deaf=True)

    bound_text_channel.setdefault(ctx.guild.id, ctx.channel.id)
    _ensure_mixer(ctx.guild)
    return True


@bot.command()
async def join(ctx: commands.Context):
    """Join the caller's current voice channel and bind this text channel."""
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("You need to be in a voice channel first.")
        return

    channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect(self_deaf=True)
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
        await ctx.send("I'm not in a voice channel.", silent=True)


@bot.command()
async def tts(ctx: commands.Context, *, text: str):
    """Force-read a specific piece of text."""
    if ctx.voice_client is None:
        await ctx.send("I'm not in a voice channel. Use `!join` first.", silent=True)
        return
    is_owner = OWNER_USER_ID is not None and ctx.author.id == OWNER_USER_ID
    full_text = format_speech_text(ctx.guild.id, ctx.author, text)
    await queue_speech(ctx.guild, full_text, is_owner=is_owner)


@bot.command(aliases=["ask", "gpt", "c"])
async def chat(ctx: commands.Context, *, message: str):
    """Ask the AI chatbot something. Replies in chat, and speaks the
    answer aloud too -- joining your voice channel first if I'm not
    already connected here.

    You can also just @mention me instead of using this command."""
    if ctx.voice_client is None and ctx.author.voice is not None and ctx.author.voice.channel is not None:
        await ctx.author.voice.channel.connect(self_deaf=True)
        bound_text_channel.setdefault(ctx.guild.id, ctx.channel.id)
        _ensure_mixer(ctx.guild)

    await respond_with_chat(ctx.channel, ctx.guild, ctx.author, message)


@bot.command(aliases=["clearchat", "forget"])
async def chatreset(ctx: commands.Context):
    """Clear the chatbot's conversation memory for this channel."""
    chat_histories.pop(ctx.channel.id, None)
    await ctx.send("🧹 Chatbot memory cleared for this channel.")


@bot.command(aliases=["presets", "canned"])
async def preset(ctx: commands.Context, action: str = None, *, rest: str = None):
    """
    Manage preset (canned) chatbot answers.
    Usage:
      !preset add <trigger> | <answer>   -- save a canned answer
      !preset remove <trigger>           -- delete one
      !preset list                       -- show all triggers
    A trigger fires if it appears anywhere in the user's message.
    """
    if action is None:
        await ctx.send(
            "Usage: `!preset add <trigger> | <answer>`, `!preset remove <trigger>`, "
            "or `!preset list`."
        )
        return

    action = action.lower()

    if action == "list":
        if not preset_answers:
            await ctx.send("No preset answers saved yet.")
            return
        lines = [f"`{trigger}`" for trigger in preset_answers]
        await ctx.send("**Preset triggers:**\n" + "\n".join(lines))
        return

    if action == "remove":
        if not rest:
            await ctx.send("Usage: `!preset remove <trigger>`")
            return
        trigger = rest.strip().lower()
        if trigger in preset_answers:
            del preset_answers[trigger]
            save_preset_answers()
            await ctx.send(f"🗑️ Removed preset for `{trigger}`.")
        else:
            await ctx.send(f"No preset found for `{trigger}`.")
        return

    if action == "add":
        if not rest or "|" not in rest:
            await ctx.send("Usage: `!preset add <trigger> | <answer>`")
            return
        trigger_part, answer_part = rest.split("|", 1)
        trigger = trigger_part.strip().lower()
        answer = answer_part.strip()
        if not trigger or not answer:
            await ctx.send("Usage: `!preset add <trigger> | <answer>`")
            return
        preset_answers[trigger] = answer
        save_preset_answers()
        await ctx.send(f"✅ Saved preset answer for `{trigger}`.")
        return

    await ctx.send("Unknown action. Use `add`, `remove`, or `list`.")


@bot.command(aliases=["p"])
async def play(ctx: commands.Context, *, query: str):
    """Search YouTube (or take a direct URL) and queue it for playback."""
    if not await _ensure_voice(ctx):
        return
    await queue_music(ctx, query)


@bot.command(aliases=["str"])
async def stream(ctx: commands.Context, *, url: str):
    """Stream a single link directly, replacing anything currently playing.

    Unlike !play/!playlist, this always plays just one source at a time --
    running !stream again drops whatever was playing/queued instead of
    adding to it. Good for internet radio or live streams with no fixed end.
    """
    if not await _ensure_voice(ctx):
        return
    mixer = _ensure_mixer(ctx.guild)

    status = await ctx.send(f"🔎 Resolving stream **{url}**...", silent=True)
    try:
        info = await asyncio.to_thread(_resolve_stream_source, url)
    except Exception as e:
        await status.edit(content=f"❌ **Could not stream:** **{url}**\nReason: {e}")
        return

    # Only ever stream one thing at a time: drop anything queued/playing.
    mixer.clear_music()
    mixer.queue_music({"source": info["url"], "title": info["title"], "is_stream": True})

    await status.edit(content=f"📡 **Now streaming:** {info['title']}")
    _start_mixer_if_needed(ctx.guild)


@bot.command(aliases=["pl"])
async def playlist(ctx: commands.Context, *, query: str):
    """Queue an entire YouTube playlist."""
    if not await _ensure_voice(ctx):
        return
    await queue_playlist(ctx, query)


@bot.command(aliases=["sk"])
async def skip(ctx: commands.Context):
    """Skip the message or song currently playing."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is not None and mixer.is_active:
        mixer.skip_current()
        await ctx.send("Skipped.", silent=True)
    else:
        await ctx.send("Nothing is playing.", silent=True)


@bot.command(aliases=["jumpto", "goto"])
async def skipto(ctx: commands.Context, position: int):
    """Jump straight to track <position> in the queue (see !queue for numbers)."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is None or not mixer.music_queue:
        await ctx.send("There's nothing queued to skip to.", silent=True)
        return

    if position < 1 or position > len(mixer.music_queue):
        await ctx.send(
            f"Pick a number between 1 and {len(mixer.music_queue)} (see `!queue`).",
            silent=True,
        )
        return

    # Discard every track before the target one.
    target_title = mixer.discard_upcoming(position - 1)
    if target_title is None:
        await ctx.send("The queue changed before I could skip there — try again.", silent=True)
        return

    mixer.skip_music()  # ends the current track; the mixer auto-advances to the new front
    await ctx.send(f"⏭️ Skipping to **{target_title}**.", silent=True)


@bot.command(aliases=["ins", "playnext"])
async def insert(ctx: commands.Context, *, query: str):
    """Look up a track and put it at the front of the queue to play next."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is None:
        await ctx.send("I'm not in a voice channel. Use `!join` or `!play` first.", silent=True)
        return

    status = await ctx.send(f"🔎 Looking up **{query}**...", silent=True)
    try:
        info = await asyncio.to_thread(_download_youtube_audio, query)
    except Exception as e:
        await status.edit(content=f"❌ **Search failed for:** **{query}**\nReason: {e}")
        return

    mixer.insert_next({"source": info["path"], "title": info["title"], "cleanup": True})
    await status.edit(content=f"⏩ **{info['title']}** will play next.")
    _start_mixer_if_needed(ctx.guild)


@bot.command(aliases=["st"])
async def stop(ctx: commands.Context):
    """Stop playback and clear the entire queue (TTS messages and songs)."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is not None:
        mixer.clear_music()
        mixer.clear_speech()
    await ctx.send("Stopped and cleared the queue.", silent=True)

@bot.command(aliases=["q"])
async def queue(ctx: commands.Context):
    """Display the current music queue."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is None:
        await ctx.send("The queue is currently empty.", silent=True)
        return

    upcoming = mixer.snapshot_queue()
    if not mixer.now_playing_title and not upcoming:
        await ctx.send("The queue is currently empty.", silent=True)
        return

    msg = ""
    if mixer.now_playing_title:
        msg += f"**Now Playing:** {mixer.now_playing_title}\n\n"

    if upcoming:
        msg += "**Up Next:**\n"
        for idx, item in enumerate(upcoming[:10], start=1):
            msg += f"`{idx}.` {item['title']}\n"

        if len(upcoming) > 10:
            msg += f"\n*...and {len(upcoming) - 10} more track(s)*"
    else:
        msg += "*No remaining tracks in queue.*"

    await ctx.send(msg, silent=True)


@bot.command(aliases=["np"])
async def nowplaying(ctx: commands.Context):
    """Display the title of the song currently playing."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer and mixer.now_playing_title:
        status = " (Paused)" if mixer.is_paused else ""
        await ctx.send(f"🎶 **Now Playing:** {mixer.now_playing_title}{status}", silent=True)
    else:
        await ctx.send("Nothing is currently playing.", silent=True)


@bot.command()
async def pause(ctx: commands.Context):
    """Pause playback."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is not None and not mixer.is_paused:
        mixer.pause()
        await ctx.send("⏸️ Paused playback.", silent=True)
    elif mixer and mixer.is_paused:
        await ctx.send("Playback is already paused.", silent=True)
    else:
        await ctx.send("Nothing is playing to pause.", silent=True)


@bot.command()
async def resume(ctx: commands.Context):
    """Resume audio playback."""
    mixer = guild_mixers.get(ctx.guild.id)
    if mixer is not None and mixer.is_paused:
        mixer.resume()
        await ctx.send("▶️ Resumed playback.", silent=True)
    elif mixer and not mixer.is_paused:
        await ctx.send("Playback is not paused.", silent=True)
    else:
        await ctx.send("Nothing is paused.", silent=True)

@bot.command(aliases=["v", "vol"])
async def volume(ctx: commands.Context, level: int):
    """Set music volume (0-100%)."""
    if level < 0 or level > 100:
        await ctx.send("Volume must be between 0 and 100.", silent=True)
        return

    vol = level / 100
    set_guild_setting(ctx.guild.id, "volume", vol)

    mixer = guild_mixers.get(ctx.guild.id)
    if mixer:
        mixer.set_volume(vol)

    await ctx.send(f"🔊 Volume set to **{level}%**", silent=True)

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

    if not await _ensure_voice(ctx):
        return

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

def _find_voice_channel(guild: discord.Guild, channel_input: str) -> discord.VoiceChannel | None:
    """Find a voice channel by ID, exact name (case-insensitive), or partial name match."""
    # Try by ID first
    if channel_input.isdigit():
        channel = guild.get_channel(int(channel_input))
        if isinstance(channel, discord.VoiceChannel):
            return channel

    query = channel_input.lower().strip()

    # Exact name match, case-insensitive
    for vc in guild.voice_channels:
        if vc.name.lower() == query:
            return vc

    # Partial/substring match, case-insensitive
    partial_matches = [vc for vc in guild.voice_channels if query in vc.name.lower()]
    if len(partial_matches) == 1:
        return partial_matches[0]

    return None


def _is_owner_or_can_manage_channels(ctx: commands.Context) -> bool:
    if OWNER_USER_ID is not None and ctx.author.id == OWNER_USER_ID:
        return True
    return ctx.author.guild_permissions.manage_channels


@bot.command(aliases=["fj"])
@commands.check(_is_owner_or_can_manage_channels)
async def forcejoin(ctx: commands.Context, *, channel_input: str = None):
    """Make the bot join a voice channel by ID or Name.
    If no channel is given, joins whichever voice channel you're currently in.
    """
    if channel_input is None:
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("❌ You're not in a voice channel — specify one: `!fj <channel name or ID>`.")
            return
        target_channel = ctx.author.voice.channel
    else:
        target_channel = _find_voice_channel(ctx.guild, channel_input)

    if target_channel is None:
        await ctx.send(f"❌ Voice channel `{channel_input}` not found.")
        return

    if ctx.voice_client is None:
        await target_channel.connect(self_deaf=True)
    else:
        await ctx.voice_client.move_to(target_channel)

    _ensure_mixer(ctx.guild)
    _start_mixer_if_needed(ctx.guild)

    bound_text_channel[ctx.guild.id] = ctx.channel.id
    await ctx.send(f"Joined **{target_channel.name}** and bound to **#{ctx.channel.name}**.")

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Global fallback so bad input doesn't crash with a raw traceback."""
    if isinstance(error, commands.CommandNotFound):
        return  # silently ignore unknown commands

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        return

    if isinstance(error, (commands.CheckFailure, commands.NotOwner)):
        await ctx.send("🔒 You don't have permission to use this command.")
        return

    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ That command is on cooldown. Try again in {error.retry_after:.1f}s.")
        return

    # Anything else: log it and let the user know something went wrong,
    # without leaking a stack trace into the channel.
    print(f"Unhandled error in command {ctx.command}: {error!r}")
    await ctx.send("⚠️ Something went wrong running that command.")

@bot.event
async def on_message(message: discord.Message):
    # Always let commands (like !join, !leave) get processed.
    await bot.process_commands(message)

    if message.author.bot or message.guild is None:
        return
    if message.content.startswith(COMMAND_PREFIX):
        return  # don't read out commands themselves

    # Chatbot: respond whenever the bot is directly @mentioned, in any
    # channel (not just the one bound for auto-read TTS).
    if bot.user in message.mentions:
        query = message.content
        for mention in message.mentions:
            query = query.replace(f"<@{mention.id}>", "").replace(
                f"<@!{mention.id}>", ""
            )
        query = query.strip()
        if query:
            await respond_with_chat(
                message.channel, message.guild, message.author, query
            )
        return

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
        voice_client = await message.author.voice.channel.connect(self_deaf=True)
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