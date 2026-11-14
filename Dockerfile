# Fallback / recommended image for yt-dlp mode: guarantees ffmpeg, libopus and a JS runtime (deno).
# Use it by copying railway.docker.json over railway.json (see README -> Deploy on Railway).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libopus0 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to solve YouTube challenges.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "bot.py"]
