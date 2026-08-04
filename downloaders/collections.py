"""Collection and playlist batch download routines."""

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import yt_dlp

logger = logging.getLogger("mediabot.collections")

from downloaders.core import (
    CancelCheck,
    DownloadCancelled,
    DownloadResult,
    ProgressCallback,
    _base_ydl_opts,
    _safe_filename,
    download_ytdlp,
)
from downloaders.http import get_http_client


async def _download_cover(cover_url: str, dest: Path) -> None:
    """Fetch image cover art from remote URL and save to destination path."""
    try:
        client = await get_http_client()
        resp = await client.get(cover_url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    except httpx.HTTPError:
        pass


async def _process_collection_tracks(
        tracks: list[dict],
        collection_name: str,
        cover_url: str | None,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        should_cancel: CancelCheck | None = None,
        download_one: Callable[[dict, int, int, Path], Awaitable[DownloadResult]] | None = None,
        uploader: str = "",
) -> DownloadResult:
    """Download list of tracks concurrently and archive them into a single ZIP file.

    Args:
        tracks: List of track dictionaries with metadata.
        collection_name: Title of playlist or album.
        cover_url: Optional remote URL for playlist/album cover.
        tmpdir: Directory for temporary storage.
        on_progress: Async callback for progress updates.
        should_cancel: Function to check cancellation state.
        download_one: Async callback function to download an individual track item.
        uploader: Uploader or artist name string.

    Returns:
        DownloadResult instance pointing to the generated ZIP archive.
    """
    album_name = _safe_filename(collection_name)
    album_dir = tmpdir / album_name
    album_dir.mkdir(parents=True, exist_ok=True)
    if cover_url:
        cover_path = album_dir / "cover.jpg"
        await _download_cover(cover_url, cover_path)
    total = len(tracks)
    sem = asyncio.Semaphore(5)
    done_count = 0
    async def _dl_one(idx: int, track: dict):
        nonlocal done_count
        if should_cancel and should_cancel():
            raise DownloadCancelled("download cancelled by user")
        track_tmpdir = tmpdir / f"_track_{idx:03d}"
        track_tmpdir.mkdir(parents=True, exist_ok=True)
        async with sem:
            try:
                result = await download_one(track, idx, total, track_tmpdir)
            except DownloadCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to download collection track: %s", e)
                if on_progress:
                    await on_progress(f"Skipping {track.get('title', 'Unknown')}: error occurred.")
                shutil.rmtree(track_tmpdir, ignore_errors=True)
                return
        dest_path = album_dir / f"{idx:02d} - {_safe_filename(result.title)}{result.filepath.suffix}"
        shutil.move(str(result.filepath), str(dest_path))
        shutil.rmtree(track_tmpdir, ignore_errors=True)
        done_count += 1
    results = await asyncio.gather(
        *[_dl_one(idx, track) for idx, track in enumerate(tracks, start=1)],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, DownloadCancelled):
            raise r
    zip_path = tmpdir / f"{album_name}.zip"
    shutil.make_archive(base_name=str(zip_path.with_suffix("")), format="zip", root_dir=str(album_dir))
    return DownloadResult(
        filepath=zip_path,
        title=collection_name,
        thumbnail=None,
        uploader=uploader or collection_name,
        is_audio=False,
        filesize=zip_path.stat().st_size,
    )


async def download_ytdlp_playlist(
        url: str,
        want_audio: bool,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        video_quality: str = "1080",
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download generic yt-dlp supported playlist and pack tracks into a ZIP file.

    Args:
        url: Playlist URL.
        want_audio: True for audio, False for video.
        tmpdir: Temporary output directory.
        on_progress: Progress reporting callback.
        audio_format: Selected audio format key.
        video_quality: Selected max video height.
        should_cancel: Cancellation condition predicate.

    Returns:
        DownloadResult referencing the output ZIP archive.
    """
    loop = asyncio.get_event_loop()
    ydl_opts_flat = {
        **_base_ydl_opts(),
        "extract_flat": True,
        "noplaylist": False,
    }
    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
            return ydl.extract_info(url, download=False)
    if on_progress:
        await on_progress("Getting playlist info...")
    info = await loop.run_in_executor(None, _extract)
    if not info or info.get("_type") != "playlist":
        raise RuntimeError("URL is not a playlist.")
    collection_name = info.get("title") or "Playlist"
    entries = info.get("entries") or []
    if not entries:
        raise RuntimeError("Playlist is empty.")
    if on_progress:
        await on_progress(f"Found {len(entries)} tracks in playlist '{collection_name}'...")
    tracks = []
    for entry in entries:
        if not entry:
            continue
        track_url = entry.get("url") or entry.get("webpage_url")
        if not track_url and entry.get("id"):
            track_url = f"https://www.youtube.com/watch?v={entry['id']}"
        elif track_url and not track_url.startswith("http"):
            if track_url.startswith("/"):
                track_url = f"https://www.youtube.com{track_url}"
            else:
                track_url = f"https://www.youtube.com/watch?v={track_url}"

        if track_url:
            tracks.append({
                "url": track_url,
                "title": entry.get("title") or entry.get("id") or "Track",
            })
    async def _dl_one(track: dict, idx: int, total: int, track_tmpdir: Path) -> DownloadResult:
        track_url = track["url"]
        if on_progress:
            await on_progress(f"Downloading {idx}/{total}: {track.get('title') or 'Unknown'}")
        return await download_ytdlp(
            url=track_url,
            want_audio=want_audio,
            tmpdir=track_tmpdir,
            on_progress=None,
            audio_format=audio_format,
            video_quality=video_quality,
            should_cancel=should_cancel,
        )
    return await _process_collection_tracks(
        tracks=tracks,
        collection_name=collection_name,
        cover_url=None,
        tmpdir=tmpdir,
        on_progress=on_progress,
        should_cancel=should_cancel,
        download_one=_dl_one,
        uploader="Downloader",
    )

