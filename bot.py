"""Telegram bot interface, command handlers, and inline query callbacks."""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import re
from pathlib import Path
from health import init as health_init, start_health_server

from pyrogram import Client, filters, idle
from pyrogram.enums import ButtonStyle, ParseMode
from pyrogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputMediaAudio,
    InputMediaVideo,
    InputTextMessageContent,
    Message,
)

from config import config
from downloaders import (
    AUDIO_FORMATS, VIDEO_QUALITIES, DownloadCancelled, FileTooLarge,
    cleanup, download, get_available_audio_codecs, get_available_video_heights,
    fetch_kinopoisk_info, list_episodes,
)
from i18n import EMOJI_RU, EMOJI_US, get_text
from platforms import Platform, detect_platform, extract_url
from services import CacheEntry, QueueTask, media_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mediabot")

config.validate()
MAIN_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(MAIN_LOOP)

app = Client(
    "mediabot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)

_YOUTUBE_CODECS: set[str] = {"opus", "mp4a"}

EMOJI_CANCEL = "5240241223632954241"
EMOJI_MOVIE = "5375464961822695044"
EMOJI_HISTORY = "6264850799315127348"
EMOJI_LOADING = "5215327832040811010"
EMOJI_POPCORN = "5449418822576520200"
EMOJI_GEAR = "5341785795382522635"
EMOJI_BACK = "5212944206795920744"
EMOJI_AUDIO = "5402595016101078333"
EMOJI_VIDEO = "5337301488748211009"
EMOJI_MIC = "5382013970905309819"


def _platform_to_json(platform: Platform) -> str:
    """Serialize Platform enum value to string name."""
    return platform.name


def _platform_from_json(name: str) -> Platform:
    """Deserialize string name to Platform enum value."""
    return Platform[name]


def _keyboard_language() -> InlineKeyboardMarkup:
    """Build inline keyboard for language selection."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Русский", callback_data="set_lang:ru", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_RU),
            InlineKeyboardButton("English", callback_data="set_lang:en", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_US),
        ]
    ])


def _keyboard_start(lang: str = "ru") -> InlineKeyboardMarkup:
    """Build start command inline keyboard containing Help and Settings buttons."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text(lang, "btn_help"), callback_data="help", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_HISTORY),
            InlineKeyboardButton(get_text(lang, "btn_settings"), callback_data="settings", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_GEAR),
        ]
    ])


def _keyboard_cancel(lang: str = "ru") -> InlineKeyboardMarkup:
    """Build inline keyboard containing a cancel download button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(get_text(lang, "btn_cancel"), callback_data="cancel_download", style=ButtonStyle.DANGER, icon_custom_emoji_id=EMOJI_CANCEL)]
    ])


def _keyboard_fmt(platform: Platform, lang: str = "ru") -> InlineKeyboardMarkup:
    """Build initial format selection keyboard (Video / Audio)."""
    video_supported = platform not in (Platform.SPOTIFY, Platform.SHAZAM, Platform.PINTEREST, Platform.SOUNDCLOUD)
    buttons = []
    if video_supported:
        buttons.append(InlineKeyboardButton(get_text(lang, "btn_video"), callback_data="fmt:video", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_VIDEO))
    buttons.append(InlineKeyboardButton(get_text(lang, "btn_audio"), callback_data="fmt:audio", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_AUDIO))
    return InlineKeyboardMarkup([buttons])


def _keyboard_video_quality(available_heights: list[int] | None = None, lang: str = "ru") -> InlineKeyboardMarkup:
    """Build video quality resolution options keyboard."""
    rows = []
    for key, info in VIDEO_QUALITIES.items():
        if available_heights and int(key) > max(available_heights):
            continue
        style = ButtonStyle.SUCCESS if key == "2160" else ButtonStyle.PRIMARY
        rows.append([InlineKeyboardButton(info["label"], callback_data=f"vq:{key}", style=style, icon_custom_emoji_id=int(info["emoji"]))])
    rows.append([InlineKeyboardButton(get_text(lang, "btn_back"), callback_data="back:fmt", style=ButtonStyle.DANGER, icon_custom_emoji_id=EMOJI_BACK)])
    return InlineKeyboardMarkup(rows)


def _keyboard_audio_format(codecs: set[str] | None = None, show_back: bool = True, lang: str = "ru") -> InlineKeyboardMarkup:
    """Build audio format options keyboard."""
    lossless_native = codecs is None or bool(codecs & {"flac", "alac"})

    rows = []
    for key, info in AUDIO_FORMATS.items():
        if key == "flac" and not lossless_native:
            continue
        style = ButtonStyle.SUCCESS if key == "flac" else ButtonStyle.PRIMARY
        rows.append([InlineKeyboardButton(info["label"], callback_data=f"af:{key}", style=style, icon_custom_emoji_id=int(info["emoji"]))])

    if show_back:
        rows.append([InlineKeyboardButton(get_text(lang, "btn_back"), callback_data="back:fmt", style=ButtonStyle.DANGER, icon_custom_emoji_id=EMOJI_BACK)])
    return InlineKeyboardMarkup(rows)



IGNORED_PERFORMERS = {"Неизвестно", "Unknown", "Кинопоиск", "Kinopoisk"}


def _keyboard_kp_seasons(seasons: list[int], lang: str = "ru") -> InlineKeyboardMarkup:
    """Build season selection inline keyboard for Kinopoisk series."""
    rows = []
    row = []
    for s in seasons:
        row.append(InlineKeyboardButton(get_text(lang, "kp_season_btn", season=s), callback_data=f"kp_s:{s}", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_MOVIE))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _keyboard_kp_episodes(season: int, episodes: list[int], lang: str = "ru") -> InlineKeyboardMarkup:
    """Build episode selection inline keyboard for a Kinopoisk season."""
    rows = []
    row = []
    for e in episodes:
        row.append(InlineKeyboardButton(get_text(lang, "kp_episode_btn", episode=e), callback_data=f"kp_e:{e}", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_POPCORN))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _keyboard_kp_translations(translations: list[dict]) -> InlineKeyboardMarkup:
    """Build voiceover translation selection inline keyboard for Kinopoisk content."""
    rows = []
    for t in translations:
        rows.append([InlineKeyboardButton(f"{t['name']}", callback_data=f"kp_tr:{t['id']}", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_MIC)])
    return InlineKeyboardMarkup(rows)


def _history_keyboard(entries: list, lang: str = "ru") -> InlineKeyboardMarkup:
    """Build inline keyboard presenting recent user downloads history."""
    rows = []
    for entry in entries:
        no_title = get_text(lang, "untitled")
        label = entry.title or no_title
        if entry.performer and entry.performer not in IGNORED_PERFORMERS:
            label = f"{entry.performer} - {label}"
        media_type = getattr(entry, "media_type", None)
        if media_type == "audio":
            emoji_id = EMOJI_AUDIO
            style = ButtonStyle.SUCCESS
        elif media_type == "video":
            emoji_id = EMOJI_VIDEO
            style = ButtonStyle.PRIMARY
        else:
            emoji_id = EMOJI_HISTORY
            style = ButtonStyle.PRIMARY

        media_kind_map = {
            "audio": get_text(lang, "media_audio"),
            "video": get_text(lang, "media_video"),
            "photo": get_text(lang, "media_photo"),
            "document": get_text(lang, "media_file"),
        }
        media_kind = media_kind_map.get(media_type, media_type or get_text(lang, "media_file"))
        format_label = getattr(entry, "media_format", None) or get_text(lang, "unknown_format")
        label = f"[{media_kind} | {format_label}] {label}"
        if len(label) > 55:
            label = f"{label[:52]}..."
        rows.append([InlineKeyboardButton(label, callback_data=f"save:{entry.media_key}", style=style, icon_custom_emoji_id=emoji_id)])
    return InlineKeyboardMarkup(rows)


_MD_SPECIAL_CHARS = r"_*`[]"

def _md_escape(text: str) -> str:
    return re.sub(f"([{re.escape(_MD_SPECIAL_CHARS)}])", r"\\\1", text)

def _title_with_link(title: str, url: str | None, lang: str = "ru") -> str:
    safe_title = _md_escape(title) if title else get_text(lang, "untitled")
    if not url:
        return safe_title

    safe_url = url.replace(")", "%29")
    return f"[{safe_title}]({safe_url})"

def _caption(result, url: str | None = None, lang: str = "ru") -> str:
    title_part = _title_with_link(result.title, url, lang=lang)
    performer = result.uploader
    if performer and performer not in IGNORED_PERFORMERS:
        lines = [f"**{_md_escape(performer)}** - {title_part}"]
    else:
        lines = [title_part]
    if getattr(result, "converted", False):
        lines.append(get_text(lang, "format_converted"))
    lines.append("@lukidown_bot")
    return "\n".join(lines)

def _cache_caption(entry: CacheEntry, lang: str = "ru") -> str:
    title_part = _title_with_link(entry.title or "", entry.source_url, lang=lang)
    performer = entry.performer
    if performer and performer not in IGNORED_PERFORMERS:
        return f"**{_md_escape(performer)}** - {title_part}\n@lukidown_bot"
    return f"{title_part}\n@lukidown_bot"

async def _probe_video_metadata(filepath: Path) -> tuple[int, int, int]:
    """Retrieve (width, height, duration) using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-show_entries", "format=duration",
        "-of", "json",
        str(filepath),
    ]
    width, height, duration = 0, 0, 0
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode())
            streams = data.get("streams", [])
            if streams:
                vstream = streams[0]
                width = int(vstream.get("width") or 0)
                height = int(vstream.get("height") or 0)
                if "duration" in vstream:
                    try:
                        duration = int(float(vstream["duration"]))
                    except (ValueError, TypeError):
                        pass
            if not duration and "format" in data:
                try:
                    duration = int(float(data["format"].get("duration", 0)))
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        log.warning("ffprobe metadata extraction failed for %s: %s", filepath, e)
    return width, height, duration


async def _ensure_video_info(filepath: Path, result) -> tuple[int, int, int, Path | None]:
    """Ensure video resolution, duration, and thumbnail preview exist for video sending."""
    width = getattr(result, "width", 0) or 0
    height = getattr(result, "height", 0) or 0
    duration = getattr(result, "duration", 0) or 0
    thumb = getattr(result, "thumbnail", None)
    thumb_path = thumb if (thumb and thumb.exists()) else None

    if not (width > 0 and height > 0 and duration > 0):
        p_width, p_height, p_duration = await _probe_video_metadata(filepath)
        if width <= 0 and p_width > 0:
            width = p_width
            if hasattr(result, "width"):
                result.width = width
        if height <= 0 and p_height > 0:
            height = p_height
            if hasattr(result, "height"):
                result.height = height
        if duration <= 0 and p_duration > 0:
            duration = p_duration
            if hasattr(result, "duration"):
                result.duration = duration

    if not thumb_path:
        gen_thumb = filepath.with_suffix(".thumb.jpg")
        if gen_thumb.exists() and gen_thumb.stat().st_size > 0:
            thumb_path = gen_thumb
        else:
            seek_sec = 3.0
            if duration > 0:
                seek_sec = min(5.0, max(1.0, duration * 0.1))
            cmd = [
                "ffmpeg",
                "-y",
                "-ss", str(seek_sec),
                "-i", str(filepath),
                "-vframes", "1",
                "-q:v", "2",
                str(gen_thumb),
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                if proc.returncode == 0 and gen_thumb.exists() and gen_thumb.stat().st_size > 0:
                    thumb_path = gen_thumb
                    if hasattr(result, "thumbnail"):
                        result.thumbnail = gen_thumb
            except Exception as e:
                log.warning("Failed to generate video thumbnail for %s: %s", filepath, e)

    return width, height, duration, thumb_path

def _requested_format_label(*, want_audio: bool, audio_format: str, video_quality: str) -> str:
    if want_audio:
        return AUDIO_FORMATS.get(audio_format, {}).get("label", audio_format)
    return VIDEO_QUALITIES.get(video_quality, f"{video_quality}p")

def _cached_inline_media(entry: CacheEntry, lang: str = "ru") -> InputMediaAudio | InputMediaVideo:
    if entry.media_type == "video":
        return InputMediaVideo(media=entry.file_id, caption=_cache_caption(entry, lang=lang))
    return InputMediaAudio(media=entry.file_id, caption=_cache_caption(entry, lang=lang))

async def _store_inline_cache(user_id: int, media_key: str, result, want_audio: bool, audio_format: str,
                              video_quality: str, url: str | None = None):
    shadow_message = None
    user_lang = (await media_service.storage.get_user_language(user_id)) or "ru"
    try:
        if want_audio:
            shadow_message = await app.send_audio(
                chat_id=user_id,
                audio=str(result.filepath),
                caption=_caption(result, url, lang=user_lang),
                performer=result.uploader,
                title=result.title,
                thumb=str(result.thumbnail) if result.thumbnail and result.thumbnail.exists() else None,
                disable_notification=True,
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            v_width, v_height, v_duration, v_thumb = await _ensure_video_info(result.filepath, result)
            kwargs = {
                "chat_id": user_id,
                "video": str(result.filepath),
                "caption": _caption(result, url, lang=user_lang),
                "supports_streaming": True,
                "disable_notification": True,
                "parse_mode": ParseMode.MARKDOWN,
            }
            if v_width > 0:
                kwargs["width"] = v_width
            if v_height > 0:
                kwargs["height"] = v_height
            if v_duration > 0:
                kwargs["duration"] = v_duration
            if v_thumb:
                kwargs["thumb"] = str(v_thumb)
            shadow_message = await app.send_video(**kwargs)

        file_id = None
        media_type = None
        if shadow_message.audio:
            file_id = shadow_message.audio.file_id
            media_type = "audio"
        elif shadow_message.video:
            file_id = shadow_message.video.file_id
            media_type = "video"
        elif shadow_message.document:
            file_id = shadow_message.document.file_id
            media_type = "document"

        if file_id:
            await media_service.storage.save_cache(
                media_key=media_key,
                file_id=file_id,
                media_type=media_type,
                media_format=_requested_format_label(
                    want_audio=want_audio,
                    audio_format=audio_format,
                    video_quality=video_quality,
                ),
                title=result.title,
                performer=result.uploader,
                source_url=url,
            )
            await media_service.storage.add_history(user_id, media_key)
    except Exception as e:
        log.warning("Inline cache persistence failed: %s", e)
    finally:
        if shadow_message is not None:
            try:
                await shadow_message.delete()
            except Exception:
                pass

async def _safe_edit(msg: Message, text: str, reply_markup=None):
    """Edit message text safely ignoring exceptions."""
    try:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    except Exception:
        pass


async def _safe_delete(msg: Message):
    """Delete message safely ignoring exceptions."""
    try:
        await msg.delete()
    except Exception:
        pass


def _parse_search_query(text: str):
    """Parse title and artist search terms from user input text."""
    text = text.strip()

    if "\n" in text:
        parts = [p.strip() for p in text.splitlines() if p.strip()]
        title = parts[0] if len(parts) > 0 else None
        artist = parts[1] if len(parts) > 1 else None
        return title, artist, text

    if " - " in text:
        left, _, right = text.partition(" - ")
        return right.strip(), left.strip(), text

    return None, None, None


def get_stable_id(query: str, fmt: str) -> str:
    """Generate short stable identifier for inline query results."""
    hash_obj = hashlib.md5(query.encode())
    return f"{fmt}_{hash_obj.hexdigest()[:8]}"


async def _send_cached_media(chat_id: int, entry: CacheEntry, lang: str = "ru"):
    """Send media file using cached Telegram file_id."""
    caption = _cache_caption(entry, lang=lang)
    try:
        if entry.media_type == "audio":
            return await app.send_audio(chat_id, audio=entry.file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
        if entry.media_type == "video":
            return await app.send_video(chat_id, video=entry.file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
        if entry.media_type == "photo":
            return await app.send_photo(chat_id, photo=entry.file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
        return await app.send_document(chat_id, document=entry.file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
    except ValueError as e:

        log.warning("cached media_type=%s mismatched real file type, falling back to document: %s",
                    entry.media_type, e)
        return await app.send_document(chat_id, document=entry.file_id, caption=caption, parse_mode=ParseMode.MARKDOWN)


async def send_result(chat_id: int, result, status_msg: Message, url: str | None = None, lang: str = "ru"):
    """Send downloaded media result file to specified Telegram chat."""
    cap = _caption(result, url, lang=lang)
    fp = result.filepath
    thumb = result.thumbnail
    thumb_path = str(thumb) if thumb and thumb.exists() else None

    await _safe_edit(status_msg, get_text(lang, "sending_file"))

    sent_message = None
    media_type = "document"
    try:
        suffix = fp.suffix.lower()
        if result.is_audio or suffix in (".mp3", ".m4a", ".flac", ".ogg", ".wav"):
            kwargs = {
                "chat_id": chat_id,
                "audio": str(fp),
                "caption": cap,
                "parse_mode": ParseMode.MARKDOWN,
            }
            if result.uploader:
                kwargs["performer"] = result.uploader
            if result.title:
                kwargs["title"] = result.title
            if thumb_path:
                kwargs["thumb"] = thumb_path
            sent_message = await app.send_audio(**kwargs)
            media_type = "audio"
        elif suffix in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
            v_width, v_height, v_duration, v_thumb = await _ensure_video_info(fp, result)
            kwargs = {
                "chat_id": chat_id,
                "video": str(fp),
                "caption": cap,
                "supports_streaming": True,
                "parse_mode": ParseMode.MARKDOWN,
            }
            if v_thumb:
                kwargs["thumb"] = str(v_thumb)
            if v_width > 0:
                kwargs["width"] = v_width
            if v_height > 0:
                kwargs["height"] = v_height
            if v_duration > 0:
                kwargs["duration"] = v_duration
            sent_message = await app.send_video(**kwargs)
            media_type = "video"
        elif suffix in (".jpg", ".jpeg", ".png", ".webp"):
            sent_message = await app.send_photo(
                chat_id,
                photo=str(fp),
                caption=cap,
                parse_mode=ParseMode.MARKDOWN,
            )
            media_type = "photo"
        else:
            kwargs = {
                "chat_id": chat_id,
                "document": str(fp),
                "caption": cap,
                "parse_mode": ParseMode.MARKDOWN,
            }
            if thumb_path:
                kwargs["thumb"] = thumb_path
            sent_message = await app.send_document(**kwargs)
            media_type = "document"
    except Exception as e:
        log.warning("send as media failed, fallback to document: %s", e)
        sent_message = await app.send_document(
            chat_id,
            document=str(fp),
            caption=cap,
            parse_mode=ParseMode.MARKDOWN,
        )
        media_type = "document"

    await _safe_delete(status_msg)
    return sent_message, media_type


async def _try_send_cached(chat_id: int, user_id: int, media_key: str, status_msg: Message | None = None, lang: str = "ru") -> bool:
    """Attempt sending cached media if matching key is found in storage."""
    cached = await media_service.storage.get_cache(media_key)
    if cached is None:
        return False

    if status_msg is not None:
        await _safe_edit(status_msg, get_text(lang, "found_in_cache"))
    await _send_cached_media(chat_id, cached, lang=lang)
    await media_service.storage.add_history(user_id, media_key)
    if status_msg is not None:
        await _safe_delete(status_msg)
    return True


async def _enqueue_download(
        *,
        chat_id: int,
        user_id: int,
        url: str | None,
        platform: Platform,
        want_audio: bool,
        status_msg: Message,
        audio_format: str = "mp3_192",
        video_quality: str = "1080",
        search_query: str | None = None,
        music_title: str | None = None,
        music_artist: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        translation_id: int | None = None,
):
    """Create and push a new download QueueTask into Redis queue."""
    media_key = await media_service.compute_media_key(
        url=url,
        platform=platform,
        want_audio=want_audio,
        audio_format=audio_format,
        video_quality=video_quality,
        search_query=search_query,
        music_title=music_title,
        music_artist=music_artist,
        season=season,
        episode=episode,
        translation_id=translation_id,
    )

    if await _try_send_cached(chat_id, user_id, media_key, status_msg=status_msg):
        return

    task = QueueTask(
        task_id=f"{user_id}:{status_msg.id}:{media_key[:8]}",
        user_id=user_id,
        chat_id=chat_id,
        status_chat_id=status_msg.chat.id,
        status_message_id=status_msg.id,
        url=url,
        platform=platform.value,
        want_audio=want_audio,
        audio_format=audio_format,
        video_quality=video_quality,
        search_query=search_query,
        music_title=music_title,
        music_artist=music_artist,
        media_key=media_key,
        season=season,
        episode=episode,
        translation_id=translation_id,
    )
    await media_service.queue.enqueue(task)


async def _process_queued_download(task: QueueTask):
    """Execute queued download task and handle file sending or error reporting."""
    await media_service.queue.set_active(task.user_id, task.status_message_id)
    status_msg = await app.get_messages(task.status_chat_id, task.status_message_id)
    user_lang = (await media_service.storage.get_user_language(task.user_id)) or "ru"

    if task.media_key and await _try_send_cached(task.chat_id, task.user_id, task.media_key, status_msg=status_msg, lang=user_lang):
        await media_service.queue.clear_active(task.user_id, task.task_id)
        return

    last_progress = [""]
    loop = asyncio.get_running_loop()

    async def on_progress(text: str):
        if text != last_progress[0]:
            last_progress[0] = text
            await _safe_edit(status_msg, text, reply_markup=_keyboard_cancel(user_lang))

    def should_cancel() -> bool:
        return media_service.queue.is_cancel_requested_sync(task.user_id)

    result = None
    try:
        result = await download(
            url=task.url,
            platform=Platform(task.platform),
            want_audio=task.want_audio,
            on_progress=on_progress,
            audio_format=task.audio_format,
            video_quality=task.video_quality,
            artist_track_name=task.search_query,
            music_title=task.music_title,
            music_artist=task.music_artist,
            should_cancel=should_cancel,
            season=task.season,
            episode=task.episode,
            translation_id=task.translation_id,
            lang=user_lang,
        )
        sent_message, media_type = await send_result(task.chat_id, result, status_msg, url=task.url, lang=user_lang)

        if sent_message and task.media_key:
            file_id = None
            if media_type == "audio" and sent_message.audio:
                file_id = sent_message.audio.file_id
            elif media_type == "video" and sent_message.video:
                file_id = sent_message.video.file_id
            elif media_type == "photo" and sent_message.photo:
                file_id = sent_message.photo.file_id
            elif sent_message.document:
                file_id = sent_message.document.file_id

            if file_id:
                performer = result.uploader or task.music_artist
                title = result.title or task.music_title
                media_format = task.audio_format if task.want_audio else task.video_quality
                await media_service.storage.save_cache(
                    media_key=task.media_key,
                    file_id=file_id,
                    media_type=media_type,
                    media_format=media_format,
                    title=title,
                    performer=performer,
                    source_url=task.url,
                )
                await media_service.storage.add_history(task.user_id, task.media_key)
    except DownloadCancelled:
        await media_service.queue.clear_cancel(task.user_id)
        await _safe_edit(status_msg, get_text(user_lang, "cancel_done"))
    except FileTooLarge as e:
        await _safe_edit(status_msg, get_text(user_lang, "file_too_large", limit=config.MAX_FILE_SIZE_MB))
    except Exception as e:
        log.exception("download failed for task %s: %s", task.task_id, e)
        await _safe_edit(status_msg, get_text(user_lang, "download_error"))

    finally:
        await media_service.queue.clear_active(task.user_id, task.task_id)
        if result and result.filepath and result.filepath.exists():
            cleanup(result.filepath)


async def _run_worker(worker_no: int):
    """Worker background task processing queue items."""
    log.info("worker %s started", worker_no)
    while True:
        try:
            task = await media_service.queue.claim_next(timeout=2)
            if task is None:
                await asyncio.sleep(0.5)
                continue
            log.info("worker %s processing task %s", worker_no, task.task_id)
            await _process_queued_download(task)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("worker %s error", worker_no)
            await asyncio.sleep(1)


async def _ensure_user_lang(union: Message | CallbackQuery) -> str | None:
    """Ensure user has selected a language. If not, send language prompt and return None."""
    user_id = union.from_user.id
    lang = await media_service.storage.get_user_language(user_id)
    if lang is None:
        text = get_text(None, "select_language_prompt")
        markup = _keyboard_language()
        if isinstance(union, CallbackQuery):
            await union.answer()
            await union.message.reply_text(text, reply_markup=markup)
        else:
            await union.reply_text(text, reply_markup=markup)
        return None
    return lang


@app.on_message(filters.command("start"))
async def cmd_start(client: Client, msg: Message):
    """Handle /start command by checking user language preference."""
    lang = await media_service.storage.get_user_language(msg.from_user.id)
    if lang is None:
        await msg.reply_text(
            get_text(None, "select_language_prompt"),
            reply_markup=_keyboard_language(),
        )
        return

    await msg.reply_text(
        get_text(lang, "start_text"),
        reply_markup=_keyboard_start(lang),
    )


@app.on_message(filters.command("settings"))
@app.on_callback_query(filters.regex(r"^settings$"))
async def handle_settings(client: Client, union: Message | CallbackQuery):
    """Handle /settings command and settings callback by displaying language selection."""
    user_id = union.from_user.id
    lang = (await media_service.storage.get_user_language(user_id)) or "ru"
    text = get_text(lang, "settings_text")
    reply_markup = _keyboard_language()

    if isinstance(union, CallbackQuery):
        await union.answer()
        await union.message.edit_text(text, reply_markup=reply_markup)
    else:
        await union.reply_text(text, reply_markup=reply_markup)


@app.on_callback_query(filters.regex(r"^set_lang:(ru|en)$"))
async def cb_set_lang(client: Client, cq: CallbackQuery):
    """Handle callback for user language selection."""
    lang_code = cq.data.split(":")[1]
    await media_service.storage.set_user_language(cq.from_user.id, lang_code)
    await cq.answer(get_text(lang_code, "language_saved"), show_alert=True)
    await cq.message.edit_text(
        get_text(lang_code, "start_text"),
        reply_markup=_keyboard_start(lang_code),
    )


@app.on_message(filters.command("help"))
@app.on_callback_query(filters.regex(r"^help$"))
async def handle_help(client: Client, union: Message | CallbackQuery):
    """Handle /help command and help callback by displaying instructions."""
    lang = await _ensure_user_lang(union)
    if not lang:
        return
    help_text = get_text(lang, "help_text")
    if isinstance(union, CallbackQuery):
        await union.answer()
        await union.message.reply_text(help_text)
    else:
        await union.reply_text(help_text)


@app.on_message(filters.command("queue"))
async def cmd_queue(client: Client, msg: Message):
    """Handle /queue command showing user's queued downloads."""
    lang = await _ensure_user_lang(msg)
    if not lang:
        return
    tasks = await media_service.queue.list_user_tasks(msg.from_user.id)
    if not tasks:
        await msg.reply_text(get_text(lang, "queue_empty"))
        return

    lines = [get_text(lang, "queue_header")]
    for index, task in enumerate(tasks, start=1):
        kind = get_text(lang, "btn_audio") if task.get("want_audio") else get_text(lang, "btn_video")
        status_key = f"status_{task['status']}"
        status = get_text(lang, status_key)
        no_title = "Untitled" if lang == "en" else "Без названия"
        label = task.get("music_title") or task.get("search_query") or task.get("url") or no_title
        lines.append(f"{index}. [{status}] {kind} - {label}")
    await msg.reply_text("\n".join(lines))


@app.on_message(filters.command("cancel"))
async def cmd_cancel(client: Client, msg: Message):
    """Handle /cancel command requesting cancellation of user's active task."""
    lang = await _ensure_user_lang(msg)
    if not lang:
        return
    cancelled = await media_service.queue.request_cancel(msg.from_user.id)
    if cancelled:
        await msg.reply_text(get_text(lang, "cancel_requested"))
    else:
        await msg.reply_text(get_text(lang, "cancel_no_active"))


@app.on_callback_query(filters.regex(r"^cancel_download$"))
async def cb_cancel_download(client: Client, cq: CallbackQuery):
    """Handle cancel download inline button callback."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    cancelled = await media_service.queue.request_cancel(cq.from_user.id)
    if cancelled:
        await cq.answer(get_text(lang, "cancel_requested"), show_alert=True)
        await _safe_edit(cq.message, get_text(lang, "cancel_downloading"), reply_markup=None)
    else:
        await cq.answer(get_text(lang, "cancel_no_active"), show_alert=True)


@app.on_message(filters.command("saves"))
async def cmd_saves(client: Client, msg: Message):
    """Handle /saves command showing recent download history."""
    lang = await _ensure_user_lang(msg)
    if not lang:
        return
    history = await media_service.storage.recent_history(msg.from_user.id, limit=10)
    if not history:
        await msg.reply_text(get_text(lang, "saves_empty"))
        return

    await msg.reply_text(
        get_text(lang, "saves_header", limit=10),
        reply_markup=_history_keyboard(history, lang),
    )


@app.on_message(filters.command("search"))
async def cmd_search(client: Client, msg: Message):
    """Handle /search command to search audio tracks by query text."""
    lang = await _ensure_user_lang(msg)
    if not lang:
        return
    args = msg.text.partition(" ")[2].strip()

    if not args:
        await msg.reply_text(
            get_text(lang, "search_usage"),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    title, artist, query = _parse_search_query(args)
    if query is None:
        await msg.reply_text(
            get_text(lang, "search_usage"),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    reply = await msg.reply_text(
        get_text(lang, "select_audio_format"),
        reply_markup=_keyboard_audio_format(_YOUTUBE_CODECS, show_back=False, lang=lang),
    )
    await media_service.pending.set(f"{msg.chat.id}:{reply.id}", {
        "url": None,
        "platform": _platform_to_json(Platform.UNKNOWN),
        "want_audio": True,
        "available_codecs": list(_YOUTUBE_CODECS),
        "search_query": query,
        "music_title": title,
        "music_artist": artist,
        "user_id": msg.from_user.id,
    })


@app.on_message(filters.text & ~filters.command(["start", "settings", "help", "queue", "cancel", "saves", "search"]))
async def handle_url(client: Client, msg: Message):
    """Handle incoming text messages containing media URLs."""
    lang = await _ensure_user_lang(msg)
    if not lang:
        return
    url = extract_url(msg.text)
    if not url:
        return

    platform = detect_platform(url)
    if platform == Platform.UNKNOWN:
        await msg.reply_text(get_text(lang, "unsupported_platform"))
        return

    if platform == Platform.PINTEREST:
        reply = await msg.reply_text(get_text(lang, "pinterest_added"))
        await _enqueue_download(
            chat_id=msg.chat.id,
            user_id=msg.from_user.id,
            url=url,
            platform=platform,
            want_audio=False,
            status_msg=reply,
        )
        return

    if platform == Platform.KINOPOISK:
        status_msg = await msg.reply_text(get_text(lang, "kinopoisk_fetching"))
        try:
            info = await fetch_kinopoisk_info(url)
        except Exception as e:
            log.warning("Kinopoisk fetch_kinopoisk_info error: %s", e)
            await status_msg.edit_text(get_text(lang, "kinopoisk_error"))
            return

        key = f"{status_msg.chat.id}:{status_msg.id}"
        state = {
            "url": url,
            "platform": _platform_to_json(platform),
            "user_id": msg.from_user.id,
            "content_type": info["content_type"],
            "file_list": info["file_list"],
            "translations": info["translations"],
            "seasons": info["seasons"],
            "season": None,
            "episode": None,
            "translation_id": None,
        }
        await media_service.pending.set(key, state)

        meta = info.get("meta", {})
        serial_fallback = get_text(lang, "serial_label")
        movie_fallback = get_text(lang, "movie_label")
        title_name = meta.get("nameRu") or meta.get("nameOriginal") or (serial_fallback if info["content_type"] == "serial" else movie_fallback)
        year_str = f" ({meta['year']})" if meta.get("year") else ""

        if info["content_type"] == "serial":
            await status_msg.edit_text(
                get_text(lang, "kp_season_select", title=title_name, year=year_str),
                reply_markup=_keyboard_kp_seasons(info["seasons"], lang=lang),
            )
        else:
            await status_msg.edit_text(
                get_text(lang, "kp_movie_tr"),
                reply_markup=_keyboard_kp_translations(info["translations"]),
            )
        return

    if platform in (Platform.SPOTIFY, Platform.SHAZAM, Platform.YANDEX, Platform.SOUNDCLOUD, Platform.VK_MUSIC):
        reply = await msg.reply_text(
            get_text(lang, "select_audio_format"),
            reply_markup=_keyboard_audio_format(_YOUTUBE_CODECS, show_back=False, lang=lang),
        )
        await media_service.pending.set(f"{msg.chat.id}:{reply.id}", {
            "url": url,
            "platform": _platform_to_json(platform),
            "want_audio": True,
            "available_codecs": list(_YOUTUBE_CODECS),
            "user_id": msg.from_user.id,
        })
        return

    reply = await msg.reply_text(get_text(lang, "select_format"), reply_markup=_keyboard_fmt(platform, lang=lang))
    await media_service.pending.set(f"{msg.chat.id}:{reply.id}", {
        "url": url,
        "platform": _platform_to_json(platform),
        "want_audio": None,
        "user_id": msg.from_user.id,
    })


@app.on_callback_query(filters.regex(r"^fmt:(video|audio)$"))
async def cb_fmt(client: Client, cq: CallbackQuery):
    """Handle callback for media type selection (Video or Audio)."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    key = f"{cq.message.chat.id}:{cq.message.id}"
    state = await media_service.pending.get(key)
    if not state:
        await cq.answer(get_text(lang, "query_expired"), show_alert=True)
        return

    await cq.answer()
    platform = _platform_from_json(state["platform"])

    if cq.data == "fmt:video":
        state["want_audio"] = False
        getting_qualities = get_text(lang, "getting_qualities")
        select_quality = get_text(lang, "select_quality")
        await cq.message.edit_text(getting_qualities)
        if platform == Platform.KINOPOISK:
            heights = [1080, 720, 480, 360]
        else:
            heights = await get_available_video_heights(state["url"])
        state["available_heights"] = heights
        await media_service.pending.set(key, state)
        await cq.message.edit_text(
            select_quality,
            reply_markup=_keyboard_video_quality(heights or None, lang=lang),
        )
    else:
        state["want_audio"] = True
        getting_formats = get_text(lang, "getting_formats")
        await cq.message.edit_text(getting_formats)
        if platform == Platform.KINOPOISK:
            codecs = {"mp3_192"}
        else:
            codecs = await get_available_audio_codecs(state["url"])
        state["available_codecs"] = list(codecs)
        await media_service.pending.set(key, state)
        await cq.message.edit_text(
            get_text(lang, "select_audio_format"),
            reply_markup=_keyboard_audio_format(codecs or None, lang=lang),
        )


@app.on_callback_query(filters.regex(r"^vq:\d+$"))
async def cb_video_quality(client: Client, cq: CallbackQuery):
    """Handle callback for video quality selection."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    key = f"{cq.message.chat.id}:{cq.message.id}"
    state = await media_service.pending.pop(key)
    if not state:
        await cq.answer(get_text(lang, "query_expired"), show_alert=True)
        return

    quality = cq.data.split(":")[1]
    await cq.answer()
    adding_msg = get_text(lang, "adding_video_queue", quality=VIDEO_QUALITIES[quality])
    await cq.message.edit_text(adding_msg)

    await _enqueue_download(
        chat_id=cq.message.chat.id,
        user_id=state["user_id"],
        url=state.get("url"),
        platform=_platform_from_json(state["platform"]),
        want_audio=False,
        status_msg=cq.message,
        video_quality=quality,
        search_query=state.get("search_query"),
        music_title=state.get("music_title"),
        music_artist=state.get("music_artist"),
        season=state.get("season"),
        episode=state.get("episode"),
        translation_id=state.get("translation_id"),
    )


@app.on_callback_query(filters.regex(r"^af:.+$"))
async def cb_audio_format(client: Client, cq: CallbackQuery):
    """Handle callback for audio format/bitrate selection."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    key = f"{cq.message.chat.id}:{cq.message.id}"
    state = await media_service.pending.pop(key)
    if not state:
        await cq.answer(get_text(lang, "query_expired"), show_alert=True)
        return

    afmt = cq.data.split(":", 1)[1]
    label = AUDIO_FORMATS.get(afmt, {}).get("label", afmt)
    await cq.answer()
    adding_msg = get_text(lang, "adding_audio_queue", label=label)
    await cq.message.edit_text(adding_msg)

    await _enqueue_download(
        chat_id=cq.message.chat.id,
        user_id=state["user_id"],
        url=state.get("url"),
        platform=_platform_from_json(state["platform"]),
        want_audio=True,
        status_msg=cq.message,
        audio_format=afmt,
        search_query=state.get("search_query"),
        music_title=state.get("music_title"),
        music_artist=state.get("music_artist"),
        season=state.get("season"),
        episode=state.get("episode"),
        translation_id=state.get("translation_id"),
    )


@app.on_callback_query(filters.regex(r"^kp_s:\d+$"))
async def cb_kp_season(client: Client, cq: CallbackQuery):
    """Handle callback for Kinopoisk season selection."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    key = f"{cq.message.chat.id}:{cq.message.id}"
    state = await media_service.pending.get(key)
    if not state:
        await cq.answer(get_text(lang, "query_expired"), show_alert=True)
        return

    season = int(cq.data.split(":")[1])
    state["season"] = season
    await media_service.pending.set(key, state)
    await cq.answer()

    episodes = list_episodes(state["file_list"], season)
    await cq.message.edit_text(
        get_text(lang, "kp_serial_season", season=season),
        reply_markup=_keyboard_kp_episodes(season, episodes, lang=lang),
    )


@app.on_callback_query(filters.regex(r"^kp_e:\d+$"))
async def cb_kp_episode(client: Client, cq: CallbackQuery):
    """Handle callback for Kinopoisk episode selection."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    key = f"{cq.message.chat.id}:{cq.message.id}"
    state = await media_service.pending.get(key)
    if not state:
        await cq.answer(get_text(lang, "query_expired"), show_alert=True)
        return

    episode = int(cq.data.split(":")[1])
    state["episode"] = episode
    await media_service.pending.set(key, state)
    await cq.answer()

    translations = state.get("translations", [])
    await cq.message.edit_text(
        get_text(lang, "kp_serial_episode", season=state['season'], episode=episode),
        reply_markup=_keyboard_kp_translations(translations),
    )


@app.on_callback_query(filters.regex(r"^kp_tr:\d+$"))
async def cb_kp_translation(client: Client, cq: CallbackQuery):
    """Handle callback for Kinopoisk translation/voiceover selection."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    key = f"{cq.message.chat.id}:{cq.message.id}"
    state = await media_service.pending.get(key)
    if not state:
        await cq.answer(get_text(lang, "query_expired"), show_alert=True)
        return

    translation_id = int(cq.data.split(":")[1])
    state["translation_id"] = translation_id
    await media_service.pending.set(key, state)
    await cq.answer()

    await cq.message.edit_text(
        get_text(lang, "select_format"),
        reply_markup=_keyboard_fmt(Platform.KINOPOISK, lang=lang),
    )


@app.on_callback_query(filters.regex(r"^back:fmt$"))
async def cb_back(client: Client, cq: CallbackQuery):
    """Handle back button callback to return to format selection step."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    key = f"{cq.message.chat.id}:{cq.message.id}"
    state = await media_service.pending.get(key)
    if not state:
        await cq.answer(get_text(lang, "query_expired"), show_alert=True)
        return

    await cq.answer()
    platform = _platform_from_json(state["platform"])

    if platform in (Platform.SPOTIFY, Platform.SHAZAM, Platform.YANDEX, Platform.SOUNDCLOUD, Platform.VK_MUSIC):
        await cq.answer(get_text(lang, "cannot_go_back"), show_alert=True)
        return

    state["want_audio"] = None
    await media_service.pending.set(key, state)
    await cq.message.edit_text(get_text(lang, "select_format"), reply_markup=_keyboard_fmt(platform, lang=lang))



@app.on_callback_query(filters.regex(r"^save:.+$"))
async def cb_saved_media(client: Client, cq: CallbackQuery):
    """Handle callback to resend a cached media item from history."""
    lang = (await media_service.storage.get_user_language(cq.from_user.id)) or "ru"
    media_key = cq.data.split(":", 1)[1]
    entry = await media_service.storage.get_cache(media_key)
    if entry is None:
        await cq.answer(get_text(lang, "file_unavailable"), show_alert=True)
        return

    await cq.answer(get_text(lang, "sending_saved"))
    await _send_cached_media(cq.message.chat.id, entry, lang=lang)
    await media_service.storage.add_history(cq.from_user.id, media_key)


@app.on_inline_query()
async def on_inline_query(client, iq):
    """Handle inline queries (@bot_username search query)."""
    user_lang = (await media_service.storage.get_user_language(iq.from_user.id)) or "ru"
    query = iq.query.strip()
    if not query:
        await iq.answer([], cache_time=0)
        return

    url = extract_url(query)
    title, artist, search_q = None, None, None

    if url:
        platform = detect_platform(url)
        if platform not in (Platform.SPOTIFY, Platform.SHAZAM, Platform.YANDEX, Platform.SOUNDCLOUD, Platform.VK_MUSIC):
            pass
    else:
        title, artist, search_q = _parse_search_query(query)
        if search_q is None:
            await iq.answer([], cache_time=0)
            return
        platform = Platform.UNKNOWN

    results = []

    for key, info in AUDIO_FORMATS.items():
        if key == "flac":
            continue
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(get_text(user_lang, "download_inline"), callback_data="ignore", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_LOADING)]])
        display_text = get_text(user_lang, "download_as_fmt", label=info['label'])
        if title and artist:
            display_text = f"{artist} - {title} {info['label']}"
        results.append(
            InlineQueryResultArticle(
                id=get_stable_id(query, f"af_{key}"),
                title=info["label"],
                description=display_text,
                input_message_content=InputTextMessageContent(
                    get_text(user_lang, "download_inline_status", query=query, fmt=info['label'])
                ),
                reply_markup=keyboard,
            )
        )

    if not search_q:
        if platform not in (Platform.SPOTIFY, Platform.SHAZAM, Platform.YANDEX, Platform.SOUNDCLOUD, Platform.VK_MUSIC):
            for q, label in VIDEO_QUALITIES.items():
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(get_text(user_lang, "download_inline"), callback_data="ignore", style=ButtonStyle.PRIMARY, icon_custom_emoji_id=EMOJI_LOADING)]])
                display_text = get_text(user_lang, "download_video_fmt", label=label)
                results.append(
                    InlineQueryResultArticle(
                        id=get_stable_id(query, f"vf_{q}"),
                        title=label,
                        description=display_text,
                        input_message_content=InputTextMessageContent(
                            get_text(user_lang, "download_inline_status", query=query, fmt=f"Video {label}")
                        ),
                        reply_markup=keyboard,
                    )
                )

    await iq.answer(results, cache_time=0, is_personal=True)


@app.on_chosen_inline_result()
async def on_chosen_inline_result(client: Client, chosen: ChosenInlineResult):
    """Handle chosen inline result to download and serve inline query selection."""
    if not chosen.inline_message_id or not (chosen.result_id.startswith("af_") or chosen.result_id.startswith("vf_")):
        return

    try:
        prefix, fmt, _ = chosen.result_id.split("_", 2)
    except ValueError:
        return

    is_video = (prefix == "vf")
    want_audio = not is_video

    afmt = fmt if want_audio else "mp3_192"
    vfmt = fmt if is_video else "1080"
    user_lang = (await media_service.storage.get_user_language(chosen.from_user.id)) or "ru"

    await client.edit_inline_text(
        inline_message_id=chosen.inline_message_id,
        text=get_text(user_lang, "starting_download"),
    )

    original_query = chosen.query.strip()
    url = extract_url(original_query)

    title, artist, search_q = None, None, None
    platform = Platform.UNKNOWN

    if url:
        platform = detect_platform(url)

        if is_video and platform in (Platform.SPOTIFY, Platform.SHAZAM, Platform.YANDEX, Platform.SOUNDCLOUD, Platform.VK_MUSIC):
            return
    else:
        title, artist, search_q = _parse_search_query(original_query)

    media_key = await media_service.compute_media_key(
        url=url,
        platform=platform,
        want_audio=want_audio,
        audio_format=afmt,
        video_quality=vfmt,
        search_query=search_q,
        music_title=title,
        music_artist=artist,
    )

    cached = await media_service.storage.get_cache(media_key)
    if cached:
        if (want_audio and cached.media_type == "audio") or (is_video and cached.media_type == "video"):
            try:
                await client.edit_inline_media(
                    inline_message_id=chosen.inline_message_id,
                    media=_cached_inline_media(cached, lang=user_lang),
                )
                await media_service.storage.add_history(chosen.from_user.id, media_key)
                return
            except Exception as e:
                log.warning("Inline cache send failed, fallback to download: %s", e)

    try:
        result = await download(
            url=url,
            platform=platform,
            want_audio=want_audio,
            audio_format=afmt,
            video_quality=vfmt,
            artist_track_name=search_q,
            music_title=title,
            music_artist=artist,
            lang=user_lang,
        )

        thumb_path = str(result.thumbnail) if result.thumbnail and result.thumbnail.exists() else None

        if want_audio:
            media_obj = InputMediaAudio(
                media=str(result.filepath),
                caption=_caption(result, url, lang=user_lang),
                performer=result.uploader,
                title=result.title,
                thumb=thumb_path,
            )
        else:
            media_obj = InputMediaVideo(
                media=str(result.filepath),
                caption=_caption(result, url, lang=user_lang),
                width=result.width,
                height=result.height,
                thumb=thumb_path,
            )

        await client.edit_inline_media(
            inline_message_id=chosen.inline_message_id,
            media=media_obj,
        )

        await _store_inline_cache(chosen.from_user.id, media_key, result, want_audio, afmt, vfmt, url=url)
        cleanup(result)

    except Exception as e:
        log.error("Inline error %s", e)
        await client.edit_inline_text(
            inline_message_id=chosen.inline_message_id,
            text=get_text(user_lang, "download_error"),
        )


async def main():
    """Main application entry point for service startup and bot client execution."""
    log.info("starting mediabot")
    await media_service.init()
    media_service.start_workers(_process_queued_download)
    await app.start()

    health_init(app, media_service)
    health_runner = await start_health_server(host="0.0.0.0", port=8080)

    try:
        await idle()
    finally:
        await health_runner.cleanup()
        await app.stop()
        await media_service.close()


if __name__ == "__main__":
    try:
        MAIN_LOOP.run_until_complete(main())
    finally:
        MAIN_LOOP.close()
