FROM python:3.11-slim AS base

# ffmpeg: required by discord.py's audio pipeline and yt-dlp playback.
# libsodium: required by PyNaCl (voice encryption) in case no prebuilt wheel matches.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       libsodium23 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Persisted at runtime via a volume (see docker-compose.yml):
#   data/setting.json, data/user_names.json, tts_audio/
RUN mkdir -p /app/tts_audio /app/data

# Run as a non-root user
RUN useradd --create-home --uid 1000 botuser \
    && chown -R botuser:botuser /app
USER botuser

CMD ["python", "bot.py"]
