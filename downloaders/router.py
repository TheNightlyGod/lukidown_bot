"""Media download routing module directing links and search requests to appropriate platform handlers."""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("mediabot.router")

from config import config
from downloaders.collections import download_ytdlp_playlist
from downloaders.core import (
    CancelCheck,
    DownloadCancelled,
    DownloadResult,
    FileTooLarge,
    ProgressCallback,
    _ensure_dir,
    _human_size,
    download_apple_music,
    download_deezer,
    download_jiosaavn,
    download_pinterest,
    download_reddit,
    download_snapchat,
    download_tenor,
    download_twitch,
    download_ytdlp,
)
from downloaders.kinopoisk import download_kinopoisk
from downloaders.spotify import download_spotify
from downloaders.transcoder import compress_video
from downloaders.vk_music import download_vk_music
from downloaders.yandex import download_yandex, download_yandex_playlist
from i18n import get_text
from platforms import Platform


async def download(
        url: str | None,
        platform: Platform,
        want_audio: bool,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        video_quality: str = "1080",
        artist_track_name: str | None = None,
        music_title: str | None = None,
        music_artist: str | None = None,
        should_cancel: CancelCheck | None = None,
        season: int | None = None,
        episode: int | None = None,
        translation_id: int | None = None,
        lang: str = "ru",
) -> DownloadResult:
    """Route media download request to the specific platform handler."""
    tmpdir = Path(tempfile.mkdtemp(dir=_ensure_dir(config.DOWNLOAD_DIR)))
    try:
        if url:
            if platform == Platform.KINOPOISK:
                result = await download_kinopoisk(
                    url=url,
                    tmpdir=tmpdir,
                    want_audio=want_audio,
                    on_progress=on_progress,
                    audio_format=audio_format,
                    video_quality=video_quality,
                    should_cancel=should_cancel,
                    season=season,
                    episode=episode,
                    translation_id=translation_id,
                    lang=lang,
                )
            elif platform == Platform.SPOTIFY:
                result = await download_spotify(
                    url,
                    tmpdir,
                    on_progress,
                    audio_format=audio_format,
                    should_cancel=should_cancel,
                )
            elif platform == Platform.PINTEREST:
                result = await download_pinterest(url, tmpdir, on_progress, should_cancel=should_cancel)
            elif platform == Platform.YANDEX:
                is_collection = ("/playlists/" in url or "/users/" in url) or ("/album/" in url and "/track/" not in url)
                if is_collection:
                    result = await download_yandex_playlist(
                        url, tmpdir,
                        on_progress=on_progress,
                        audio_format=audio_format,
                        should_cancel=should_cancel,
                    )
                else:
                    result = await download_yandex(
                        url, tmpdir=tmpdir,
                        on_progress=on_progress,
                        audio_format=audio_format,
                        should_cancel=should_cancel,
                    )
            elif platform == Platform.VK_MUSIC:
                result = await download_vk_music(
                    url,
                    tmpdir,
                    on_progress=on_progress,
                    audio_format=audio_format,
                    should_cancel=should_cancel,
                )
            elif platform == Platform.DEEZER:
                result = await download_deezer(
                    url, tmpdir, on_progress,
                    audio_format=audio_format, should_cancel=should_cancel,
                )
            elif platform == Platform.APPLE_MUSIC:
                result = await download_apple_music(
                    url, tmpdir, on_progress,
                    audio_format=audio_format, should_cancel=should_cancel,
                )
            elif platform == Platform.REDDIT:
                result = await download_reddit(url, tmpdir, on_progress, should_cancel=should_cancel)
            elif platform == Platform.TENOR:
                result = await download_tenor(url, tmpdir, on_progress, should_cancel=should_cancel)
            elif platform == Platform.JIOSAAVN:
                result = await download_jiosaavn(url, tmpdir, on_progress, should_cancel=should_cancel)
            elif platform == Platform.TWITCH:
                result = await download_twitch(url, tmpdir, on_progress, should_cancel=should_cancel)
            elif platform == Platform.SNAPCHAT:
                result = await download_snapchat(url, tmpdir, on_progress, should_cancel=should_cancel)
            else:
                if platform in (Platform.SHAZAM, Platform.SOUNDCLOUD):
                    want_audio = True
                is_playlist = False
                try:
                    import yt_dlp

                    from downloaders.core import _base_ydl_opts
                    loop = asyncio.get_event_loop()
                    ydl_opts_flat = {
                        **_base_ydl_opts(),
                        "extract_flat": True,
                        "noplaylist": False,
                    }
                    def _extract_flat(target_url):
                        with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
                            return ydl.extract_info(target_url, download=False)
                    flat_info = await loop.run_in_executor(None, _extract_flat, url)
                    while flat_info and flat_info.get("_type") in ("url", "url_transparent"):
                        resolved_url = flat_info.get("url")
                        if not resolved_url:
                            break
                        url = resolved_url
                        flat_info = await loop.run_in_executor(None, _extract_flat, url)
                    if flat_info and flat_info.get("_type") == "playlist":
                        is_playlist = True
                except Exception as e:  # noqa: BLE001
                    logger.debug("Flat extraction failed for %s: %s", url, e)
                if is_playlist:
                    result = await download_ytdlp_playlist(
                        url=url,
                        want_audio=want_audio,
                        tmpdir=tmpdir,
                        on_progress=on_progress,
                        audio_format=audio_format,
                        video_quality=video_quality,
                        should_cancel=should_cancel,
                    )
                else:
                    result = await download_ytdlp(
                        url,
                        want_audio,
                        tmpdir,
                        on_progress,
                        audio_format=audio_format,
                        video_quality=video_quality,
                        music_title=music_title,
                        music_artist=music_artist,
                        should_cancel=should_cancel,
                    )
        else:
            search_queries = [
                f"ytsearch1:{artist_track_name}",
                f"ytmusicsearch1:{artist_track_name}",
            ]
            last_err = None
            result = None
            for q in search_queries:
                try:
                    result = await download_ytdlp(
                        q,
                        want_audio=True,
                        tmpdir=tmpdir,
                        on_progress=on_progress,
                        audio_format=audio_format,
                        music_title=music_title,
                        music_artist=music_artist,
                        should_cancel=should_cancel,
                    )
                    break
                except (FileTooLarge, DownloadCancelled):
                    raise
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    for item in tmpdir.iterdir():
                        if item.is_file():
                            try:
                                item.unlink()
                            except Exception as e:  # noqa: BLE001
                                logger.debug(f"Failed to unlink: {e}", item)
            if not result:
                raise RuntimeError(f"Search failed: {last_err}") from last_err
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
    if result.filesize > max_bytes:
        if not result.is_audio and result.filepath and result.filepath.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".ts"):
            if on_progress:
                await on_progress(
                    get_text(lang, "file_exceeds_limit", size=_human_size(result.filesize), limit=config.MAX_FILE_SIZE_MB)
                )
            compressed_path = tmpdir / f"compressed_{result.filepath.name}"
            try:
                final_compressed = await compress_video(
                    input_path=result.filepath,
                    output_path=compressed_path,
                    max_size_bytes=int(max_bytes * 0.96),
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                    lang=lang,
                )
                if result.filepath.exists():
                    result.filepath.unlink()
                result.filepath = final_compressed
                result.filesize = final_compressed.stat().st_size
                result.converted = True
            except Exception as e:  # noqa: BLE001
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise FileTooLarge(
                    f"file too large: {_human_size(result.filesize)} (limit {config.MAX_FILE_SIZE_MB} MB), compression error: {e}"
                )
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise FileTooLarge(
                f"file too large: {_human_size(result.filesize)} (limit {config.MAX_FILE_SIZE_MB} MB)"
            )
    return result


def cleanup(target: DownloadResult | Path | str):
    """Remove temporary directory associated with download result or path.

    Args:
        target: DownloadResult instance, Path, or string path to clean up.
    """
    if hasattr(target, "filepath"):
        p = target.filepath
    else:
        p = Path(target)
    if p:
        shutil.rmtree(p.parent if p.is_file() else p, ignore_errors=True)

