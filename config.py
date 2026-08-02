"""Configuration settings management for the application."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    """Application configuration container loaded from environment variables."""

    API_ID: int = field(default_factory=lambda: int(os.getenv("API_ID", "0")))
    API_HASH: str = field(default_factory=lambda: os.getenv("API_HASH", ""))
    BOT_TOKEN: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    SOCKS5_PROXY: str = field(default_factory=lambda: os.getenv("SOCKS5_PROXY", ""))
    DOWNLOAD_DIR: str = field(default_factory=lambda: os.getenv("DOWNLOAD_DIR", "downloads"))
    MAX_FILE_SIZE_MB: int = field(default_factory=lambda: int(os.getenv("MAX_FILE_SIZE_MB", "50")))
    DATABASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@postgres:5432/lukidown",
        )
    )
    REDIS_URL: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://redis:6379/0"))
    DOWNLOAD_WORKERS: int = field(default_factory=lambda: int(os.getenv("DOWNLOAD_WORKERS", "2")))
    CANCEL_TTL_SECONDS: int = field(default_factory=lambda: int(os.getenv("CANCEL_TTL_SECONDS", "60")))
    HISTORY_LIMIT: int = field(default_factory=lambda: int(os.getenv("HISTORY_LIMIT", "10")))
    REDDIT_COOKIE: str = field(default_factory=lambda: os.getenv('REDDIT_COOKIE', ''))

    def validate(self):
        """Validate that all required configuration variables are present.

        Raises:
            ValueError: If any required environment variable is missing.
        """
        missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN", "DATABASE_URL", "REDIS_URL") if not getattr(self, k)]
        if missing:
            raise ValueError(f"missing env vars: {', '.join(missing)}")


config = Config()

