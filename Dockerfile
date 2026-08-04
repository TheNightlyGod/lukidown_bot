FROM python:3.11-slim

COPY --from=denoland/deno:bin-2.7.11 /deno /usr/local/bin/deno

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PUPPETEER_CACHE_DIR=/app/.cache/puppeteer
RUN deno run -A npm:puppeteer browsers install chrome

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p downloads

CMD ["python", "bot.py"]