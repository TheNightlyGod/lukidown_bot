"""Core downloader logic, yt-dlp wrappers, and metadata embedding functions."""

import asyncio
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from config import config
from downloaders.http import get_http_client


@dataclass
class DownloadResult:
    """Container for the output metadata of a finished download operation."""

    filepath: Path
    title: str
    thumbnail: Path | None = None
    uploader: str = ""
    is_audio: bool = False
    width: int = 0
    height: int = 0
    duration: int = 0
    filesize: int = 0
    converted: bool = False
    extra: dict = field(default_factory=dict)


ProgressCallback = Callable[[str], Awaitable[None]]
CancelCheck = Callable[[], bool]

AUDIO_FORMATS = {
    "flac": {"codec": "flac", "quality": "0", "ext": ".flac", "label": "FLAC (lossless)", "emoji": "5458806371250105072"},
    "m4a": {"codec": "m4a", "quality": "0", "ext": ".m4a", "label": "M4A / AAC", "emoji": "5364257510951762757"},
    "mp3_320": {"codec": "mp3", "quality": "320", "ext": ".mp3", "label": "MP3 / 320 kbps", "emoji": "5194988065222831801"},
    "mp3_192": {"codec": "mp3", "quality": "192", "ext": ".mp3", "label": "MP3 / 192 kbps", "emoji": "5373193336439990453"},
}

VIDEO_QUALITIES = {
    "2160": {"label": "4K / 2160p", "emoji": "5287435501102445539"},
    "1080": {"label": "Full HD / 1080p", "emoji": "5287237979851467801"},
    "720": {"label": "HD / 720p", "emoji": "5287544765070454088"},
    "480": {"label": "SD / 480p", "emoji": "5287316036587106782"},
    "360": {"label": "Low / 360p", "emoji": "5287715971056806685"},
}


class DownloadCancelled(RuntimeError):
    """Raised when a download operation is cancelled by the user."""

    pass


class FileTooLarge(RuntimeError):
    """Raised when the requested media exceeds the maximum configured size limit."""

    pass


def _safe_filename(name: str) -> str:
    """Sanitize filename by replacing invalid characters with underscores."""
    return re.sub(r"[\\/\:*?\"<>|]", "_", name).strip()


def _ensure_dir(path: str | Path) -> Path:
    """Ensure directory exists at specified path, creating parent dirs if needed."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _human_size(n: int) -> str:
    """Format byte count as human readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _is_format_unavailable(error: Exception) -> bool:
    """Check if exception indicates requested media format was missing."""
    return "Requested format is not available" in str(error)


def _base_ydl_opts() -> dict:
    """Construct baseline YoutubeDL options dictionary."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "remote_components": ["ejs:github"],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }
    cookie_path = Path("cookies.txt")
    if cookie_path.exists() and cookie_path.stat().st_size > 0:
        opts["cookiefile"] = str(cookie_path)
    return opts


def _progress_hook(
        cb: Callable[[str], None],
        should_cancel: CancelCheck | None = None,
        max_bytes: int | None = None,
):
    """Build a yt-dlp progress hook callback."""
    last = [0.0]
    def hook(d: dict):
        if should_cancel and should_cancel():
            raise DownloadCancelled("download cancelled")
        if d["status"] != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        if max_bytes and total and total > max_bytes:
            raise FileTooLarge(
                f"file too large: ~{_human_size(total)} (limit {_human_size(max_bytes)})"
            )
        now = time.time()
        if now - last[0] < 2:
            return
        last[0] = now
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed") or 0
        eta = d.get("eta") or 0
        pct = (downloaded / total * 100) if total else 0
        parts = [f"{pct:.0f}%"]
        if total:
            parts.append(f"{_human_size(downloaded)} / {_human_size(total)}")
        if speed:
            parts.append(f"{_human_size(speed)}/s")
        if eta:
            parts.append(f"eta {eta}s")
        cb(" / ".join(parts))
    return hook


def _embed_cover_flac(media_file: Path, thumb_file: Path) -> None:
    """Embed image cover art into FLAC audio file metadata."""
    try:
        audio = FLAC(media_file)
        pic = Picture()
        pic.type = 3
        pic.mime = "image/png" if thumb_file.suffix.lower() == ".png" else "image/jpeg"
        pic.desc = "Cover"
        pic.data = thumb_file.read_bytes()
        audio.add_picture(pic)
        audio.save()
    except Exception:
        pass


def _embed_cover_m4a(media_file: Path, thumb_file: Path) -> None:
    """Embed image cover art into M4A audio file metadata."""
    try:
        audio = MP4(media_file)
        fmt = MP4Cover.FORMAT_PNG if thumb_file.suffix.lower() == ".png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(thumb_file.read_bytes(), imageformat=fmt)]
        audio.save()
    except Exception:
        pass


def _embed_cover_mp3(media_file: Path, thumb_file: Path) -> None:
    """Embed image cover art into MP3 ID3 tags."""
    try:
        audio = MP3(media_file, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        mime = "image/png" if thumb_file.suffix.lower() == ".png" else "image/jpeg"
        audio.tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=thumb_file.read_bytes(),
            )
        )
        audio.save()
    except Exception:
        pass


async def _clean_deezer_title(raw: str) -> tuple[str, str]:
    """Parse artist and title from Deezer page title string."""
    left = raw.split("|")[0].strip()
    if "-" in left:
        artist, title = [p.strip() for p in left.split("-", 1)]
        return artist, title
    return "", left


async def _clean_apple_music_title(raw: str) -> tuple[str, str]:
    """Parse artist and title from Apple Music page title string."""
    clean = raw.rsplit("-", 1)[0].strip()
    if clean.lower().startswith("song by"):
        clean = clean[8:].strip()
    if "-" in clean:
        artist, title = [p.strip() for p in clean.split("-", 1)]
        return artist, title
    return "", clean


async def get_available_video_heights(url: str) -> list[int]:
    """Extract list of available video heights for a URL using yt-dlp.

    Args:
        url: Media link.

    Returns:
        Sorted list of integer video heights in descending order.
    """
    loop = asyncio.get_event_loop()
    def _run():
        opts = {**_base_ydl_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
            return sorted(
                {
                    int(f["height"])
                    for f in formats
                    if f.get("height") and f.get("vcodec") not in (None, "none")
                },
                reverse=True,
            )
    try:
        return await loop.run_in_executor(None, _run)
    except Exception:
        return []


async def get_available_audio_codecs(url: str) -> set[str]:
    """Extract set of available audio codecs for a URL using yt-dlp.

    Args:
        url: Media link.

    Returns:
        Set of audio codec name strings.
    """
    loop = asyncio.get_event_loop()
    def _run():
        opts = {**_base_ydl_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                f["acodec"].split(".")[0].lower()
                for f in info.get("formats", [])
                if f.get("acodec") and f["acodec"] != "none"
            }
    try:
        return await loop.run_in_executor(None, _run)
    except Exception:
        return set()


async def download_ytdlp(
        url: str,
        want_audio: bool,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        video_quality: str | None = None,
        music_title: str | None = None,
        music_artist: str | None = None,
        thumb_url: str | None = None,
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download media using yt-dlp with support for audio/video format options.

    Args:
        url: Target media link.
        want_audio: True for audio extraction, False for video download.
        tmpdir: Path to directory for temporary outputs.
        on_progress: Async progress updates callback function.
        audio_format: Codec/bitrate key from AUDIO_FORMATS.
        video_quality: Video resolution height string or None.
        music_title: Optional title metadata override.
        music_artist: Optional artist metadata override.
        thumb_url: Optional remote thumbnail URL to fetch and embed.
        should_cancel: Cancellation condition check function.

    Returns:
        DownloadResult containing output media path and metadata.
    """
    loop = asyncio.get_event_loop()
    def sync_cb(msg: str):
        if on_progress:
            asyncio.run_coroutine_threadsafe(on_progress(msg), loop)
    max_mb = config.MAX_FILE_SIZE_MB
    afmt = AUDIO_FORMATS.get(audio_format, AUDIO_FORMATS["mp3_192"])
    common_meta = [
        *(["-metadata", f"title={music_title}"] if music_title else []),
        *(["-metadata", f"artist={music_artist}"] if music_artist else []),
    ]
    if want_audio:
        codec = afmt["codec"]
        quality = afmt["quality"]
        if codec == "m4a":
            fmt = "bestaudio[ext=m4a]/bestaudio/best"
            postprocessors = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"},
                {"key": "FFmpegMetadata"},
            ]
            postprocessor_args = [
                *common_meta,
                "-metadata", "comment=Downloaded via Telegram bot",
                "-metadata", "description=",
                "-metadata", "ldes=",
                "-metadata", "purl=",
            ]
        else:
            fmt = "bestaudio/best"
            postprocessors = [
                {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality},
                {"key": "FFmpegMetadata"},
            ]
            postprocessor_args = [
                *common_meta,
                "-metadata", "comment=Downloaded via Telegram bot",
                "-metadata", "TXXX:description=",
                "-metadata", "purl=",
            ]
    else:
        if video_quality and video_quality != "0":
            h = video_quality
            fmt = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]"
                f"/b[height<={h}]"
                f"/bestvideo+bestaudio"
                f"/best"
                f"/b"
            )
        else:
            fmt = (
                f"bestvideo[ext=mp4][filesize<{max_mb}M]+bestaudio[ext=m4a]"
                f"/bestvideo[ext=mp4][filesize_approx<{max_mb}M]+bestaudio[ext=m4a]"
                f"/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo+bestaudio"
                f"/best"
                f"/b"
            )
        postprocessors = [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
            {"key": "FFmpegMetadata"},
        ]
        postprocessor_args = [
            *common_meta,
            "-metadata", "comment=Downloaded via Telegram bot",
            "-metadata", "description=",
            "-metadata", "ldes=",
            "-metadata", "purl=",
        ]
    max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
    common_opts = {
        **_base_ydl_opts(),
        "outtmpl": str(tmpdir / "%(id)s.%(ext)s"),
        "writethumbnail": True,
        "postprocessors": postprocessors,
        "progress_hooks": [_progress_hook(sync_cb, should_cancel=should_cancel, max_bytes=max_bytes)],
        "postprocessor_args": postprocessor_args,
    }
    ydl_opts = {**common_opts, "format": fmt}
    def _get_info(opts: dict):
        preflight = {**_base_ydl_opts(), "skip_download": True, "format": opts.get("format", "best")}
        with yt_dlp.YoutubeDL(preflight) as ydl:
            return ydl.extract_info(url, download=False)
    try:
        preflight_info = await loop.run_in_executor(None, _get_info, ydl_opts)
        if preflight_info:
            est = preflight_info.get("filesize") or preflight_info.get("filesize_approx") or 0
            if est and est > max_bytes:
                raise FileTooLarge(
                    f"file too large: ~{_human_size(est)} (limit {config.MAX_FILE_SIZE_MB} MB)"
                )
    except (FileTooLarge, DownloadCancelled):
        raise
    except Exception:
        pass
    converted = False
    def _run(opts: dict):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    try:
        info = await loop.run_in_executor(None, _run, ydl_opts)
    except (DownloadCancelled, FileTooLarge):
        raise
    except yt_dlp.utils.DownloadError as e:
        if not _is_format_unavailable(e):
            raise RuntimeError(str(e)) from e
        converted = True
        fallback_fmt = "bestaudio/best" if want_audio else "best/b"
        fallback_opts = {**common_opts, "format": fallback_fmt}
        fallback_opts.pop("cookiefile", None)
        if on_progress:
            await on_progress("Required format not available, converting from best available...")
        try:
            info = await loop.run_in_executor(None, _run, fallback_opts)
        except (DownloadCancelled, FileTooLarge):
            raise
        except yt_dlp.utils.DownloadError as e2:
            try:
                ultimate_opts = {**common_opts, "format": "b/best"}
                ultimate_opts.pop("cookiefile", None)
                info = await loop.run_in_executor(None, _run, ultimate_opts)
            except Exception:
                raise RuntimeError(str(e2)) from e2
    if not info:
        raise RuntimeError("yt-dlp returned no info")
    media_exts = {".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".ogg", ".flac", ".wav"}
    img_exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted(tmpdir.iterdir(), key=lambda f: f.stat().st_size, reverse=True)
    media_file = next((f for f in files if f.suffix.lower() in media_exts), None)
    if thumb_url:
        thumb_path = tmpdir / "thumbnail.jpg"
        client = await get_http_client()
        resp = await client.get(thumb_url, timeout=30.0, follow_redirects=True)
        thumb_path.write_bytes(resp.content)
        thumb_file = thumb_path
    else:
        thumb_file = next((f for f in files if f.suffix.lower() in img_exts), None)
    if not media_file:
        raise RuntimeError("no media file after download")
    title = info.get("title", "")
    uploader = info.get("uploader", "") or info.get("channel", "") or ""
    if want_audio and media_file.suffix.lower() == ".mp3":
        try:
            audio = MP3(media_file, ID3=ID3)
            if audio.tags:
                if "TIT2" in audio.tags:
                    title = str(audio.tags["TIT2"])
                if "TPE1" in audio.tags:
                    uploader = str(audio.tags["TPE1"])
        except Exception:
            pass
    if want_audio and media_file.suffix.lower() == ".m4a":
        try:
            audio = MP4(media_file)
            if audio.tags:
                if "©nam" in audio.tags:
                    title = str(audio.tags["©nam"][0])
                if "©ART" in audio.tags:
                    uploader = str(audio.tags["©ART"][0])
        except Exception:
            pass
    final_title = music_title if music_title is not None else title
    final_artist = music_artist if music_artist is not None else uploader
    if final_title:
        if final_artist:
            new_stem = f"{final_artist} - {final_title}"
        else:
            new_stem = final_title
    else:
        new_stem = media_file.stem
    safe_stem = _safe_filename(new_stem)
    if not safe_stem:
        safe_stem = media_file.stem
    safe_stem = safe_stem[:150].strip()
    new_filepath = media_file.with_name(safe_stem + media_file.suffix)
    if media_file != new_filepath:
        try:
            if new_filepath.exists():
                new_filepath.unlink()
            media_file.rename(new_filepath)
            media_file = new_filepath
        except Exception:
            pass
    if want_audio and thumb_file and thumb_file.exists():
        sfx = media_file.suffix.lower()
        if sfx == ".mp3":
            _embed_cover_mp3(media_file, thumb_file)
        elif sfx == ".flac":
            _embed_cover_flac(media_file, thumb_file)
        elif sfx == ".m4a":
            _embed_cover_m4a(media_file, thumb_file)
    return DownloadResult(
        filepath=media_file,
        title=music_title if music_title is not None else title,
        thumbnail=thumb_file,
        uploader=music_artist if music_artist is not None else uploader,
        is_audio=want_audio,
        width=int(info.get("width", 0) or 0),
        height=int(info.get("height", 0) or 0),
        duration=int(info.get("duration", 0) or 0),
        filesize=media_file.stat().st_size,
        converted=converted,
    )


async def _download_track_search(
        artist: str, title: str, tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        thumb_url: str | None = None,
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download audio track by querying YouTube search with artist and title."""
    return await download_ytdlp(
        f"ytsearch1:{artist} - {title}",
        want_audio=True,
        tmpdir=tmpdir,
        on_progress=on_progress,
        audio_format=audio_format,
        music_artist=artist,
        music_title=title,
        thumb_url=thumb_url,
        should_cancel=should_cancel,
    )


async def download_simple(url: str, tmpdir: Path, on_progress: ProgressCallback | None = None,
                           should_cancel: CancelCheck | None = None) -> DownloadResult:
    """Generic download helper for platforms requiring standard video extraction."""
    return await download_ytdlp(url, want_audio=False, tmpdir=tmpdir,
                                on_progress=on_progress, should_cancel=should_cancel)

download_tenor = download_jiosaavn = download_twitch = download_snapchat = download_simple


async def download_reddit(url: str, tmpdir: Path, on_progress: ProgressCallback | None = None,
                           should_cancel: CancelCheck | None = None) -> DownloadResult:
    """Download Reddit media using configured session cookies."""
    ydl_opts = {**_base_ydl_opts(), "http_headers": {"Cookie": config.REDDIT_COOKIE}}
    loop = asyncio.get_event_loop()
    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    info = await loop.run_in_executor(None, _run)
    return await download_ytdlp(
        info.get("url") or url,
        want_audio=False,
        tmpdir=tmpdir,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )


async def download_deezer(
        url: str,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download audio for Deezer link by resolving metadata and searching YouTube."""
    if on_progress:
        await on_progress("Searching track in Deezer...")
    info = await download_ytdlp(url, want_audio=False, tmpdir=tmpdir, on_progress=on_progress)
    artist, title = await _clean_deezer_title(info.title)
    return await _download_track_search(
        artist, title, tmpdir,
        on_progress=on_progress,
        audio_format=audio_format,
        should_cancel=should_cancel,
    )


async def download_apple_music(
        url: str,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download audio for Apple Music link by resolving metadata and searching YouTube."""
    if on_progress:
        await on_progress("Searching track in Apple Music...")
    info = await download_ytdlp(url, want_audio=False, tmpdir=tmpdir, on_progress=on_progress)
    artist, title = await _clean_apple_music_title(info.title)
    return await _download_track_search(
        artist, title, tmpdir,
        on_progress=on_progress,
        audio_format=audio_format,
        should_cancel=should_cancel,
    )


async def download_pinterest(
        url: str,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download video or image content from Pinterest links."""
    try:
        return await download_ytdlp(
            url,
            want_audio=False,
            tmpdir=tmpdir,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
    except Exception:
        pass
    img_url = None
    client = await get_http_client()
    resp = await client.get(url, timeout=30.0, follow_redirects=True)
    html_text = resp.text
    for pattern in [
        r'<meta property="og:image"\s+content="([^"]+)"',
        r'"images":\{"orig":\{"url":"([^"]+)"',
        r'"url":"(https://i\.pinimg\.com/originals/[^"]+)"',
        r'src="(https://i\.pinimg\.com/[^"]+\.(jpg|png|webp))"',
    ]:
        m = re.search(pattern, html_text)
        if m:
            img_url = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
            break
    if not img_url:
        raise RuntimeError("image not found on Pinterest page")
    img_path = tmpdir / f"pinterest{Path(img_url).suffix.split('?')[0] or '.jpg'}"
    resp_img = await client.get(img_url, timeout=30.0, follow_redirects=True)
    img_path.write_bytes(resp_img.content)
    return DownloadResult(
        filepath=img_path,
        title="Pinterest",
        is_audio=False,
        filesize=img_path.stat().st_size,
    )

