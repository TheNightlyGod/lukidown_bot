"""VK Music downloader module interacting with VK API method endpoints."""

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

VK_API_URL = "https://api.vk.com/method"
VK_CLIENT_ID = "6287487"
VK_CLIENT_SECRET = "QbYic1K3lEV5kTGiqlq2"
VK_API_VERSION = "5.282"
VK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


async def _vk_api_request(url: str, params: dict) -> dict:
    """Perform HTTP POST request to VK API and return parsed JSON."""
    client = await get_http_client()
    headers = {"User-Agent": VK_USER_AGENT}
    try:
        resp = await client.post(url, headers=headers, data=params, timeout=30.0)
    except httpx.HTTPError as e:
        raise RuntimeError(f"VK API request failed: {e}") from e
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"VK API returned invalid JSON: {e}\nBody: {resp.text[:300]}")


async def _vk_get_anon_token() -> str:
    """Fetch anonymous VK API access token."""
    data = await _vk_api_request(
        "https://login.vk.com/?act=get_anonym_token",
        {
            "client_secret": VK_CLIENT_SECRET,
            "client_id": VK_CLIENT_ID,
            "scopes": "audio_anonymous,video_anonymous,photos_anonymous,profile_anonymous",
            "isApiOauthAnonymEnabled": "false",
            "version": "1",
            "app_id": VK_CLIENT_ID,
        },
    )
    if data.get("type") != "okay":
        raise RuntimeError(f"VK anon token error: {data}")
    return data["data"]["access_token"]


def _vk_parse_url(url: str) -> tuple[str, dict]:
    """Extract VK entity type (track, album, playlist) and identifiers from URL."""
    m = re.search(r"/audio(-?\d+)_(\d+)(?:_([0-9a-f]+))?", url)
    if m:
        return "track", {
            "owner_id": m.group(1),
            "audio_id": m.group(2),
            "access_key": m.group(3) or "",
        }
    m = re.search(r"/music/album/(-?\d+)_(\d+)(?:_([0-9a-f]+))?", url)
    if m:
        return "album", {
            "owner_id": m.group(1),
            "playlist_id": m.group(2),
            "access_key": m.group(3) or "",
        }
    m = re.search(r"/music/playlist/(-?\d+)_(\d+)(?:_([0-9a-f]+))?", url)
    if m:
        return "playlist", {
            "owner_id": m.group(1),
            "playlist_id": m.group(2),
            "access_key": m.group(3) or "",
        }
    raise RuntimeError(f"Cannot parse VK Music URL: {url}")


async def _vk_get_playlist_info(token: str, owner_id: str, playlist_id: str, access_key: str) -> dict:
    """Fetch playlist object from VK audio.getPlaylistById endpoint."""
    data = await _vk_api_request(
        f"{VK_API_URL}/audio.getPlaylistById",
        {
            "v": VK_API_VERSION,
            "client_id": VK_CLIENT_ID,
            "owner_id": owner_id,
            "playlist_id": playlist_id,
            "access_key": access_key,
            "access_token": token,
            "extra_fields": "owner,duration",
        },
    )
    if "error" in data:
        raise RuntimeError(f"VK audio.getPlaylistById error: {data['error']}")
    return data["response"]["playlist"]


async def _vk_get_audio_ids(token: str, owner_id: str, playlist_id: str, access_key: str) -> list[str]:
    """Fetch list of audio item IDs for a VK playlist."""
    entity_id = f"{owner_id}_{playlist_id}"
    if access_key:
        entity_id += f"_{access_key}"
    data = await _vk_api_request(
        f"{VK_API_URL}/audio.getIdsBySource",
        {
            "v": VK_API_VERSION,
            "client_id": VK_CLIENT_ID,
            "source": "playlist",
            "entity_id": entity_id,
            "access_token": token,
        },
    )
    if "error" in data:
        raise RuntimeError(f"VK audio.getIdsBySource error: {data['error']}")
    return [item["audio_id"] for item in data["response"]["audios"]]


async def _vk_get_audios_by_id(token: str, audio_ids: list[str]) -> list[dict]:
    """Fetch track metadata objects from VK audio.getById endpoint in batches."""
    results = []
    chunk_size = 50
    for i in range(0, len(audio_ids), chunk_size):
        chunk = audio_ids[i: i + chunk_size]
        data = await _vk_api_request(
            f"{VK_API_URL}/audio.getById",
            {
                "v": VK_API_VERSION,
                "client_id": VK_CLIENT_ID,
                "audios": ",".join(chunk),
                "client_secret": VK_CLIENT_SECRET,
                "access_token": token,
            },
        )
        if "error" in data:
            raise RuntimeError(f"VK audio.getById error: {data['error']}")
        results.extend(data.get("response", []))
    return results


async def download_vk_music(
        url: str,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download VK Music track, album, or playlist.

    Args:
        url: VK Music track, album, or playlist URL.
        tmpdir: Directory path for temporary outputs.
        on_progress: Async progress updates callback function.
        audio_format: Codec/bitrate selection key.
        should_cancel: Cancellation condition check function.

    Returns:
        DownloadResult instance containing output media or ZIP path.
    """
    if on_progress:
        await on_progress("Getting VK token...")
    token = await _vk_get_anon_token()
    entity_type, params = _vk_parse_url(url)
    if entity_type == "track":
        audio_id = f"{params['owner_id']}_{params['audio_id']}"
        if params.get("access_key"):
            audio_id += f"_{params['access_key']}"
        if on_progress:
            await on_progress("Getting track info...")
        tracks = await _vk_get_audios_by_id(token, [audio_id])
        if not tracks:
            raise RuntimeError("VK API returned no track info")
        track = tracks[0]
        artist = track.get("artist", "")
        title = track.get("title", "")
        thumb_url = (track.get("thumb") or {}).get("photo_600") or \
                    (track.get("album", {}).get("thumb") or {}).get("photo_600")
        if on_progress:
            await on_progress(f"Searching '{artist} - {title}' on YouTube Music...")
        return await _download_track_search(
            artist, title, tmpdir,
            on_progress=on_progress,
            audio_format=audio_format,
            thumb_url=thumb_url or None,
            should_cancel=should_cancel,
        )
    owner_id = params["owner_id"]
    playlist_id = params["playlist_id"]
    access_key = params.get("access_key", "")
    if on_progress:
        await on_progress("Getting playlist info...")
    playlist_info = await _vk_get_playlist_info(token, owner_id, playlist_id, access_key)
    album_title = playlist_info.get("title", "VK Playlist")
    main_artists = playlist_info.get("main_artists", [])
    album_artist = main_artists[0]["name"] if main_artists else ""
    cover_url: str = (playlist_info.get("photo") or {}).get("photo_600", "")
    album_artist_final = album_artist or "VK Music"
    if on_progress:
        await on_progress(f"Downloading tracks '{album_title}'...")
    audio_ids = await _vk_get_audio_ids(token, owner_id, playlist_id, access_key)
    if not audio_ids:
        raise RuntimeError("VK playlist contains no tracks")
    tracks = await _vk_get_audios_by_id(token, audio_ids)
    async def _vk_download_one(track: dict, idx: int, total: int, track_tmpdir: Path) -> DownloadResult:
        artist = track.get("artist", album_artist or "Unknown")
        title = track.get("title", f"Track {idx}")
        track_thumb = (track.get("thumb") or {}).get("photo_600") or cover_url or None
        if on_progress:
            await on_progress(f"Downloading {idx}/{total}: {artist} - {title}")
        return await _download_track_search(
            artist, title, track_tmpdir,
            audio_format=audio_format,
            thumb_url=track_thumb,
            should_cancel=should_cancel,
        )
    return await _process_collection_tracks(
        tracks, album_title, cover_url, tmpdir,
        on_progress=on_progress,
        should_cancel=should_cancel,
        download_one=_vk_download_one,
        uploader=album_artist_final,
    )

