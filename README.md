<div style="height:140px"></div>
<img src="assets/banner.png" alt="lukidown banner" style="width:100%; max-height:140px; object-fit:cover" />

# lukidown

lukidown is a Telegram bot (built on [Kurigram](https://kurigram.icu/)) that downloads media from many popular platforms - YouTube, TikTok, Instagram, Pinterest, Rutube, VK, Spotify, Shazam, Yandex Music, SoundCloud, VK Music, Deezer, Apple Music, Tenor, JioSaavn, Twitch (clips only), Snapchat, Reddit, and Kinopoisk - directly inside a Telegram chat.

## Features
- Download video, audio, and thumbnails from all supported platforms via [yt-dlp](https://github.com/yt-dlp/yt-dlp).
- Full Telegram bot UX: inline keyboards for format/quality selection, cancel buttons, and per-user download history.
- **Inline mode** - trigger downloads from any chat via `@your_bot <link or search query>`.
- Music search fallback: Deezer/Apple Music links resolve metadata, then fetch the matching track via YouTube search.
- Kinopoisk support for browsing seasons, episodes, and voiceover/translation options.
- Result caching (Postgres + Redis) so repeat requests are served instantly instead of re-downloading.
- Configurable concurrent download workers and download queue with cancellation support.
- Audio tag/cover-art embedding (ID3, FLAC, MP4) via `mutagen`.
- Localization support (Russian/English).
- Built-in health check HTTP server for container orchestration.
- Docker-ready for easy deployment.

## Stack
- **Language:** Python 3.9+
- **Bot framework:** [Kurigram](https://kurigram.icu/)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- **Storage:** PostgreSQL (via `asyncpg`) for persistent history/cache, Redis for queueing/caching
- **Other notable libraries:** `mutagen` (audio metadata/cover art), `ffmpeg` (system dependency for audio/video processing)

## Quickstart - Local (development)
1. Clone:
   ```bash
   git clone https://github.com/TheNightlyGod/lukidown_bot.git
   cd lukidown_bot
   ```

2. Create and activate a venv:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # macOS / Linux
   .venv\Scripts\activate      # Windows
   ```

3. Install:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure:
   ```bash
   cp .env.example .env
   # then edit .env with your values
   ```

5. Make sure PostgreSQL and Redis are running and reachable at the URLs set in `.env`.

6. Run:
   ```bash
   python bot.py
   ```

## Quickstart - Docker
Build and run (this also starts the Postgres and Redis services the bot depends on):
```bash
docker compose up -d --build
```

## Configuration (environment variables)
```bash
API_ID=YOUR_API_ID
API_HASH=YOUR_API_HASH
BOT_TOKEN=YOUR_BOT_TOKEN

# RU_PROXY=socks5://user:password@ru-proxy-ip:port  # Proxy for Russian platforms (Rutube, VK, etc.)
# SOCKS5_PROXY=socks5://user:password@ip:port        # General proxy

DOWNLOAD_DIR=downloads
MAX_FILE_SIZE_MB=2000

DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/lukidown
REDIS_URL=redis://redis:6379/0

DOWNLOAD_WORKERS=2
CANCEL_TTL_SECONDS=60
HISTORY_LIMIT=10

# REDDIT_COOKIE=if_you_need_this_uncomment
```

`API_ID` and `API_HASH` come from [my.telegram.org](https://my.telegram.org), and `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).

## Troubleshooting
- **ffmpeg errors:** install ffmpeg and ensure it's on `PATH`.
- **Permission errors:** verify host → container mount permissions, or local file permissions.
- **Large files:** ensure sufficient disk space and container memory; adjust `MAX_FILE_SIZE_MB` as needed.
- **Bot won't start:** double-check `API_ID`, `API_HASH`, and `BOT_TOKEN`, and confirm Postgres/Redis are reachable at the configured URLs.

## Contributing
- Fork, branch, and open a PR with a clear description.
- Follow PEP 8 and include tests for new downloaders or major bug fixes.
- Use small, focused commits.

## License
This project is licensed under the AGPL-3.0. See the `LICENSE` file for the full text.

## Acknowledgements
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) for the download engine.
- [Kurigram](https://kurigram.icu/) for the Telegram bot framework.
- ffmpeg for audio/video processing.