"""Yandex Music downloader module using web API endpoints."""

import json
import re
from pathlib import Path

import httpx

from downloaders.collections import _process_collection_tracks
from downloaders.core import (
    CancelCheck,
    DownloadResult,
    ProgressCallback,
    _download_track_search,
)
from downloaders.http import get_http_client


async def _yandex_api_request(url: str, params: dict = None) -> str:
    """Send HTTP request to Yandex Music API and return raw response string."""
    client = await get_http_client()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://music.yandex.ru/",
    }
    try:
        if params:
            resp = await client.post(url, headers=headers, data=params, timeout=30.0)
        else:
            resp = await client.get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError as e:
        raise RuntimeError(f"HTTP request failed: {e}") from e
    return resp.text


async def _parse_yandex_api(entity_type: str, entity_id: str) -> dict:
    """Fetch and parse JSON payload for Yandex Music entity type and ID."""
    if entity_type == "users" and re.search(r"[a-f0-9\-]{10,}", entity_id):
        url = f"https://api.music.yandex.net/playlist/{entity_id}"
    else:
        url = f"https://api.music.yandex.net/{entity_type}/{entity_id}"
        if entity_type == "albums":
            url += "/with-tracks"
    json_data = await _yandex_api_request(url)
    try:
        data = json.loads(json_data)
        return data.get("result", {})
    except Exception as e:
        raise RuntimeError(f"Failed to parse Yandex API response: {e}")


async def download_yandex(
        url: str,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download single Yandex Music track.

    Args:
        url: Yandex Music track or album link.
        tmpdir: Temporary output folder path.
        on_progress: Async callback function for progress updates.
        audio_format: Selected audio codec/format string.
        should_cancel: Cancellation condition predicate.

    Returns:
        DownloadResult containing media file path and metadata.
    """
    if on_progress:
        await on_progress("Searching track on Yandex.Music...")
    match = re.search(r"/(track|album)/(\d+)", url)
    if not match:
        raise RuntimeError("Could not parse Yandex URL")
    entity_type = match.group(1)
    entity_id = match.group(2)
    if entity_type == "track":
        data = await _parse_yandex_api("tracks", entity_id)
        if isinstance(data, list): data = data[0]
        track_name = data.get("title", "Unknown")
        artists = data.get("artists", [])
        artist_name = artists[0]["name"] if artists else "Unknown"
        cover_uri = ""
        if data.get("albums"):
            cover_uri = data["albums"][0].get("ogImage", "").replace("%%", "400x400")
    else:
        track_id_match = re.search(r"/track/(\d+)", url)
        if track_id_match:
            data = await _parse_yandex_api("tracks", track_id_match.group(1))
            if isinstance(data, list): data = data[0]
            track_name = data.get("title", "Unknown")
            artists = data.get("artists", [])
            artist_name = artists[0]["name"] if artists else "Unknown"
            cover_uri = ""
            if data.get("albums"):
                cover_uri = data["albums"][0].get("ogImage", "").replace("%%", "400x400")
        else:
            data = await _parse_yandex_api("albums", entity_id)
            tracks = []
            for vol in data.get("volumes", []):
                for t in vol:
                    tracks.append(t)
            if tracks:
                track_data = await _parse_yandex_api("tracks", tracks[0]["id"])
                if isinstance(track_data, list): track_data = track_data[0]
                track_name = track_data.get("title", "Unknown")
                artists = track_data.get("artists", [])
                artist_name = artists[0]["name"] if artists else "Unknown"
                cover_uri = data.get("ogImage", "").replace("%%", "400x400")
            else:
                raise RuntimeError("Could not find tracks in album")
    if not track_name:
        raise RuntimeError("could not parse Yandex Music track info")
    if on_progress:
            await on_progress(f"Searching '{artist_name} - {track_name}' on YouTube Music...")
    return await _download_track_search(
        artist_name, track_name, tmpdir,
        on_progress=on_progress,
        audio_format=audio_format,
        thumb_url=f"https://{cover_uri}" if cover_uri else None,
        should_cancel=should_cancel,
    )


async def download_yandex_playlist(
        album_url: str,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download Yandex Music album or playlist as a ZIP archive.

    Args:
        album_url: Yandex Music playlist or album URL.
        tmpdir: Temporary output folder path.
        on_progress: Async callback function for progress updates.
        audio_format: Selected audio codec/format string.
        should_cancel: Cancellation condition predicate.

    Returns:
        DownloadResult containing ZIP archive file path.
    """
    album_match = re.search(r"/album/(\d+)", album_url)
    playlist_match = re.search(r"/playlists/([^/?#\s]+)", album_url)
    user_match = re.search(r"/users/([^/]+)/playlists/([^/?#\s]+)", album_url)
    if album_match:
        album_id = album_match.group(1)
        data = await _parse_yandex_api("albums", album_id)
        tracks = [t for vol in data.get("volumes", []) for t in vol]
    elif user_match:
        user_login = user_match.group(1)
        kind_or_uuid = user_match.group(2)
        data = await _parse_yandex_api(f"users/{user_login}/playlists", kind_or_uuid)
        tracks = [item.get("track", item) for item in data.get("tracks", [])]
    elif playlist_match:
        uuid = playlist_match.group(1)
        data = await _parse_yandex_api("users", uuid)
        tracks = [item.get("track", item) for item in data.get("tracks", [])]
    else:
        raise RuntimeError("Could not parse Yandex playlist/album URL")
    collection_name = data.get("title", "Yandex Playlist")
    cover_uri = data.get("ogImage", data.get("cover", {}).get("uri", "")).replace("%%", "400x400")
    cover_url = f"https://{cover_uri}" if cover_uri and not cover_uri.startswith("http") else (cover_uri or None)
    async def _yandex_download_one(track: dict, idx: int, total: int, track_tmpdir: Path) -> DownloadResult:
        track_albums = track.get("albums", [])
        track_album_id = track_albums[0]["id"] if track_albums else "track"
        track_url = f"https://music.yandex.ru/album/{track_album_id}/track/{track['id']}"
        title = track.get("title", "Unknown")
        if on_progress:
            await on_progress(f"Downloading {idx}/{total}: {title}")
        return await download_yandex(
            track_url, track_tmpdir,
            on_progress=None,
            audio_format=audio_format,
            should_cancel=should_cancel,
        )
    return await _process_collection_tracks(
        tracks, collection_name, cover_url, tmpdir,
        on_progress=on_progress,
        should_cancel=should_cancel,
        download_one=_yandex_download_one,
        uploader="Yandex Music",
    )

