"""Database storage models, Redis queue management, and service orchestration for media processing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    desc,
    select,
    text,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import config
from platforms import Platform

log = logging.getLogger("mediabot.services")

QUEUE_KEY = "download_queue"
PROCESSING_COUNT_KEY = "processing_count"
USER_QUEUE_KEY = "user_queue:{user_id}"
ACTIVE_TASK_KEY = "active_task:{user_id}"
CANCEL_FLAG_KEY = "cancel_flag:{user_id}"
TASK_DATA_KEY = "task_data:{task_id}"
TASK_STATUS_KEY = "task_status:{task_id}"
PENDING_KEY = "pending:{key}"
PENDING_TTL_SECONDS = 30 * 60


class Base(DeclarativeBase):
    """Base class for SQLAlchemy declarative ORM models."""



class MediaCache(Base):
    """Database table for cached Telegram file IDs and media metadata."""

    __tablename__ = "media_cache"

    media_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(10), nullable=False)
    media_format: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(Text)
    performer: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, nullable=False)


class UserHistory(Base):
    """Database table tracking media downloads per user."""

    __tablename__ = "user_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    media_key: Mapped[str] = mapped_column(String(64), ForeignKey("media_cache.media_key"), nullable=False, index=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, nullable=False)


class UserSettings(Base):
    """Database table storing user settings preferences."""

    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    language: Mapped[str] = mapped_column(String(10), default="ru", nullable=False)



@dataclass(slots=True)
class CacheEntry:
    """Read-only container representation of a cached media item."""

    media_key: str
    file_id: str
    media_type: str
    media_format: str | None
    title: str | None
    performer: str | None
    source_url: str | None = None


@dataclass(slots=True)
class HistoryEntry:
    """Read-only container representation of a user download history item."""

    media_key: str
    file_id: str
    media_type: str
    media_format: str | None
    title: str | None
    performer: str | None
    downloaded_at: datetime
    source_url: str | None = None


@dataclass(slots=True)
class QueueTask:
    """Task payload representation stored in Redis download queue."""

    task_id: str
    user_id: int
    chat_id: int
    status_chat_id: int
    status_message_id: int
    url: str | None
    platform: str
    want_audio: bool
    audio_format: str
    video_quality: str
    search_query: str | None = None
    music_title: str | None = None
    music_artist: str | None = None
    media_key: str | None = None
    created_at: str | None = None
    season: int | None = None
    episode: int | None = None
    translation_id: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """Convert task object attributes to a JSON-serializable dictionary."""
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "status_chat_id": self.status_chat_id,
            "status_message_id": self.status_message_id,
            "url": self.url,
            "platform": self.platform,
            "want_audio": self.want_audio,
            "audio_format": self.audio_format,
            "video_quality": self.video_quality,
            "search_query": self.search_query,
            "music_title": self.music_title,
            "music_artist": self.music_artist,
            "media_key": self.media_key,
            "created_at": self.created_at or datetime.now(timezone.utc).isoformat(),
            "season": self.season,
            "episode": self.episode,
            "translation_id": self.translation_id,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> QueueTask:
        """Instantiate QueueTask object from dictionary payload."""
        return cls(**payload)


class Storage:
    """Async database operations manager for PostgreSQL / SQLite."""

    def __init__(self) -> None:
        self.engine = create_async_engine(config.DATABASE_URL, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create database tables and perform schema migrations."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("ALTER TABLE media_cache ADD COLUMN IF NOT EXISTS media_format VARCHAR(32)"))
            await conn.execute(text("ALTER TABLE media_cache ADD COLUMN IF NOT EXISTS source_url TEXT"))

    async def close(self) -> None:
        """Close database connection engine."""
        await self.engine.dispose()

    async def get_cache(self, media_key: str) -> CacheEntry | None:
        """Fetch cached media entry by hash key.

        Args:
            media_key: blake2b hash key identifying media content.

        Returns:
            CacheEntry instance or None if key is not found.
        """
        async with self.session_factory() as session:
            row = await session.get(MediaCache, media_key)
            if row is None:
                return None
            return CacheEntry(
                media_key=row.media_key,
                file_id=row.file_id,
                media_type=row.media_type,
                media_format=row.media_format,
                title=row.title,
                performer=row.performer,
                source_url=row.source_url,
            )

    async def save_cache(
        self,
        media_key: str,
        file_id: str,
        media_type: str,
        media_format: str | None,
        title: str | None,
        performer: str | None,
        source_url: str | None = None,
    ) -> CacheEntry:
        """Create or update cached media entry in database.

        Args:
            media_key: Unique blake2b hash key.
            file_id: Telegram file ID string.
            media_type: Type descriptor ('audio', 'video', 'document').
            media_format: Requested audio/video format quality string.
            title: Media title name.
            performer: Media artist or channel uploader.
            source_url: Source link URL.

        Returns:
            Saved CacheEntry instance.
        """
        async with self.session_factory() as session:
            existing = await session.get(MediaCache, media_key)
            if existing is None:
                existing = MediaCache(media_key=media_key)
                session.add(existing)
            existing.file_id = file_id
            existing.media_type = media_type
            existing.media_format = media_format
            existing.title = title
            existing.performer = performer
            existing.source_url = source_url
            await session.commit()
            return CacheEntry(
                media_key=existing.media_key,
                file_id=existing.file_id,
                media_type=existing.media_type,
                media_format=existing.media_format,
                title=existing.title,
                performer=existing.performer,
                source_url=existing.source_url,
            )

    async def add_history(self, user_id: int, media_key: str) -> None:
        """Add record of user media download to user_history table."""
        async with self.session_factory() as session:
            session.add(UserHistory(user_id=user_id, media_key=media_key))
            await session.commit()

    async def recent_history(self, user_id: int, limit: int | None = None) -> list[HistoryEntry]:
        """Retrieve list of recent downloads for specified user.

        Args:
            user_id: Telegram user ID integer.
            limit: Maximum items to fetch (defaults to config limit).

        Returns:
            List of HistoryEntry items sorted descending by download timestamp.
        """
        effective_limit = limit or config.HISTORY_LIMIT
        async with self.session_factory() as session:
            stmt = (
                select(UserHistory, MediaCache)
                .join(MediaCache, MediaCache.media_key == UserHistory.media_key)
                .where(UserHistory.user_id == user_id)
                .order_by(desc(UserHistory.downloaded_at), desc(UserHistory.id))
                .limit(effective_limit)
            )
            rows = (await session.execute(stmt)).all()
            return [
                HistoryEntry(
                    media_key=cache.media_key,
                    file_id=cache.file_id,
                    media_type=cache.media_type,
                    media_format=cache.media_format,
                    title=cache.title,
                    performer=cache.performer,
                    downloaded_at=history.downloaded_at,
                    source_url=cache.source_url,
                )
                for history, cache in rows
            ]

    async def get_user_language(self, user_id: int) -> str | None:
        """Fetch user's preferred language code.

        Args:
            user_id: Telegram user ID.

        Returns:
            Language code string ('ru' or 'en') or None if user has not set a language yet.
        """
        async with self.session_factory() as session:
            row = await session.get(UserSettings, user_id)
            if row is None:
                return None
            return row.language

    async def set_user_language(self, user_id: int, language: str) -> None:
        """Save user's preferred language code.

        Args:
            user_id: Telegram user ID.
            language: Language code ('ru' or 'en').
        """
        async with self.session_factory() as session:
            row = await session.get(UserSettings, user_id)
            if row is None:
                row = UserSettings(user_id=user_id, language=language)
                session.add(row)
            else:
                row.language = language
            await session.commit()



class QueueManager:
    """Redis-backed queue manager handling download task queuing and cancellations."""

    def __init__(self, redis_client: Redis) -> None:
        self.redis = redis_client
        self._cancel_set: set[int] = set()

    async def enqueue(self, task: QueueTask) -> None:
        """Push download task into Redis queue and set metadata.

        Args:
            task: QueueTask instance to queue.
        """
        payload = json.dumps(task.to_payload())
        async with self.redis.pipeline(transaction=True) as pipe:
            await (
                pipe.set(TASK_DATA_KEY.format(task_id=task.task_id), payload)
                .set(TASK_STATUS_KEY.format(task_id=task.task_id), "queued")
                .rpush(QUEUE_KEY, payload)
                .rpush(USER_QUEUE_KEY.format(user_id=task.user_id), task.task_id)
                .execute()
            )

    async def claim_next(self, timeout: int = 2) -> QueueTask | None:
        """Pop next queued task from Redis with blocking timeout.

        Args:
            timeout: Timeout in seconds for blocking left pop.

        Returns:
            Claimed QueueTask or None if queue is empty.
        """
        item = await self.redis.blpop(QUEUE_KEY, timeout=timeout)
        if not item:
            return None
        _, payload = item
        task = QueueTask.from_payload(json.loads(payload))
        await self.redis.set(TASK_STATUS_KEY.format(task_id=task.task_id), "processing")
        return task

    async def set_active(self, user_id: int, message_id: int) -> None:
        """Mark user as actively processing a task in Redis."""
        async with self.redis.pipeline(transaction=True) as pipe:
            await (
                pipe.set(ACTIVE_TASK_KEY.format(user_id=user_id), message_id)
                .incr(PROCESSING_COUNT_KEY)
                .execute()
            )

    async def clear_active(self, user_id: int, task_id: str) -> None:
        """Clear active status and cleanup Redis task data after completion."""
        async with self.redis.pipeline(transaction=True) as pipe:
            await (
                pipe.delete(ACTIVE_TASK_KEY.format(user_id=user_id))
                .delete(CANCEL_FLAG_KEY.format(user_id=user_id))
                .lrem(USER_QUEUE_KEY.format(user_id=user_id), 0, task_id)
                .delete(TASK_DATA_KEY.format(task_id=task_id))
                .delete(TASK_STATUS_KEY.format(task_id=task_id))
                .execute()
            )
        self._cancel_set.discard(user_id)
        current = int(await self.redis.get(PROCESSING_COUNT_KEY) or 0)
        if current > 0:
            await self.redis.decr(PROCESSING_COUNT_KEY)

    async def finish_queued_without_processing(self, user_id: int, task_id: str, status: str) -> None:
        """Mark queued task as finished without running worker processing."""
        async with self.redis.pipeline(transaction=True) as pipe:
            await (
                pipe.lrem(USER_QUEUE_KEY.format(user_id=user_id), 0, task_id)
                .set(TASK_STATUS_KEY.format(task_id=task_id), status, ex=60)
                .delete(TASK_DATA_KEY.format(task_id=task_id))
                .execute()
            )

    async def list_user_tasks(self, user_id: int) -> list[dict[str, Any]]:
        """List all pending/queued tasks for specific user."""
        task_ids = await self.redis.lrange(USER_QUEUE_KEY.format(user_id=user_id), 0, -1)
        tasks: list[dict[str, Any]] = []
        for task_id in task_ids:
            raw = await self.redis.get(TASK_DATA_KEY.format(task_id=task_id))
            if raw is None:
                continue
            payload = json.loads(raw)
            status = await self.redis.get(TASK_STATUS_KEY.format(task_id=task_id)) or "queued"
            payload["status"] = status
            tasks.append(payload)
        return tasks

    async def get_processing_count(self) -> int:
        """Get number of tasks currently being processed by workers."""
        raw = await self.redis.get(PROCESSING_COUNT_KEY)
        return int(raw or 0)

    async def request_cancel(self, user_id: int) -> bool:
        """Request cancellation of active download task for user.

        Args:
            user_id: Telegram user ID.

        Returns:
            True if user had an active task, False otherwise.
        """
        key = ACTIVE_TASK_KEY.format(user_id=user_id)
        if not await self.redis.exists(key):
            return False
        self._cancel_set.add(user_id)
        await self.redis.set(CANCEL_FLAG_KEY.format(user_id=user_id), 1, ex=config.CANCEL_TTL_SECONDS)
        return True

    def is_cancel_requested_sync(self, user_id: int) -> bool:
        """Synchronously check local memory set for cancellation signal."""
        return user_id in self._cancel_set

    async def is_cancel_requested(self, user_id: int) -> bool:
        """Asynchronously check if user requested task cancellation."""
        return user_id in self._cancel_set or bool(await self.redis.get(CANCEL_FLAG_KEY.format(user_id=user_id)))

    async def clear_cancel(self, user_id: int) -> None:
        """Clear cancellation state for specified user."""
        self._cancel_set.discard(user_id)
        await self.redis.delete(CANCEL_FLAG_KEY.format(user_id=user_id))


class PendingStore:
    """Redis-backed store for user format selection state."""

    def __init__(self, redis_client: Redis) -> None:
        self.redis = redis_client

    async def set(self, key: str, state: dict[str, Any]) -> None:
        """Save pending selection state dictionary under key with TTL."""
        await self.redis.set(PENDING_KEY.format(key=key), json.dumps(state), ex=PENDING_TTL_SECONDS)

    async def get(self, key: str) -> dict[str, Any] | None:
        """Retrieve pending selection state dictionary by key."""
        raw = await self.redis.get(PENDING_KEY.format(key=key))
        if raw is None:
            return None
        return json.loads(raw)

    async def pop(self, key: str) -> dict[str, Any] | None:
        """Retrieve and delete pending selection state dictionary atomically."""
        redis_key = PENDING_KEY.format(key=key)
        async with self.redis.pipeline(transaction=True) as pipe:
            raw, _ = await pipe.get(redis_key).delete(redis_key).execute()
        if raw is None:
            return None
        return json.loads(raw)


class MediaService:
    """Central media processing orchestrator managing database, queue, and background workers."""

    def __init__(self) -> None:
        self.storage = Storage()
        self.redis = Redis.from_url(
            config.REDIS_URL,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=10,
            socket_keepalive=True,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        self.queue = QueueManager(self.redis)
        self.pending = PendingStore(self.redis)
        self.worker_tasks: list[asyncio.Task[Any]] = []
        self.worker_handler: Callable[[QueueTask], Awaitable[None]] | None = None
        self.stop_event = asyncio.Event()

    async def init(self) -> None:
        """Initialize database storage schema and verify Redis connection."""
        await self.storage.init()
        await self.redis.ping()

    async def close(self) -> None:
        """Shut down background worker tasks and close Redis and DB connections."""
        self.stop_event.set()
        for task in self.worker_tasks:
            task.cancel()
        if self.worker_tasks:
            await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        await self.redis.aclose()
        await self.storage.close()

    def start_workers(self, handler: Callable[[QueueTask], Awaitable[None]]) -> None:
        """Spawn background queue worker tasks.

        Args:
            handler: Async function accepting QueueTask objects for processing.
        """
        self.worker_handler = handler
        if self.worker_tasks:
            return
        for index in range(max(1, config.DOWNLOAD_WORKERS)):
            self.worker_tasks.append(asyncio.create_task(self._worker_loop(index + 1)))

    async def _worker_loop(self, worker_no: int) -> None:
        """Background loop continuously claiming and processing queued tasks."""
        while not self.stop_event.is_set():
            try:
                try:
                    task = await self.queue.claim_next(timeout=2)
                except RedisTimeoutError:
                    continue
                if task is None:
                    continue
                if self.worker_handler is None:
                    log.error("worker %s has no handler", worker_no)
                    await self.queue.finish_queued_without_processing(task.user_id, task.task_id, "failed")
                    continue
                await self.worker_handler(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker %s crashed while processing queue", worker_no)
                await asyncio.sleep(1)

    async def compute_media_key(
        self,
        *,
        url: str | None,
        platform: Platform,
        want_audio: bool,
        audio_format: str,
        video_quality: str,
        search_query: str | None,
        music_title: str | None,
        music_artist: str | None,
        season: int | None = None,
        episode: int | None = None,
        translation_id: int | None = None,
    ) -> str:
        """Generate unique blake2b cache key string for media parameters.

        Returns:
            32-character hexadecimal blake2b digest string.
        """
        if want_audio:
            artist = (music_artist or "").strip().lower()
            title = (music_title or search_query or url or "").strip().lower()
            source = f"audio|{artist}|{title}|{audio_format}|s{season}e{episode}t{translation_id}"
        else:
            video_id = url or f"{platform.value}:{search_query or ''}"
            source = f"video|{video_id.strip().lower()}|{video_quality}|s{season}e{episode}t{translation_id}"
        return hashlib.blake2b(source.encode("utf-8"), digest_size=4).hexdigest()


media_service = MediaService()