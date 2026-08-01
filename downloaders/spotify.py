"""Spotify media downloader using Web GraphQL partner API endpoints."""

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import httpx
from downloaders.http import get_http_client
from downloaders.core import (
    DownloadResult,
    ProgressCallback,
    CancelCheck,
    _download_track_search,
)
from downloaders.collections import _process_collection_tracks

log = logging.getLogger("mediabot.spotify")


class _SpotifyTokenCache:
    """Cache for Spotify access and client tokens."""

    _instance: "_SpotifyTokenCache | None" = None
    _class_lock = threading.Lock()

    def __new__(cls):
        with cls._class_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._access_token: str = ""
                inst._client_token: str = ""
                inst._expires_at: float = 0.0
                inst._refresh_lock = asyncio.Lock()
                cls._instance = inst
            return cls._instance

    @property
    def access_token(self) -> str:
        """Get currently cached Spotify access token string."""
        return self._access_token

    @property
    def client_token(self) -> str:
        """Get currently cached Spotify client token string."""
        return self._client_token

    def invalidate(self):
        """Invalidate token cache to force refresh on next call."""
        self._expires_at = 0.0

    async def ensure_fresh_async(self):
        """Ensure active tokens are fresh, refreshing via Playwright if expired."""
        if time.time() < self._expires_at - 300 and self._access_token and self._client_token:
            return
        async with self._refresh_lock:
            if time.time() < self._expires_at - 300 and self._access_token and self._client_token:
                return
            await self._refresh_async()

    async def _refresh_async(self):
        """Launch headless Chromium browser via Playwright to intercept new tokens."""
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")
                await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})

                access_token_data = None
                client_token_data = None
                pending = {}

                def on_response(event):
                    resp = event.get("response", {})
                    url = resp.get("url", "")
                    status = resp.get("status")
                    req_id = event.get("requestId")
                    if status == 200:
                        if "/api/token" in url and not access_token_data:
                            pending[req_id] = "access"
                        elif "clienttoken.spotify.com" in url and not client_token_data:
                            pending[req_id] = "client"

                async def on_loading_finished(event):
                    nonlocal access_token_data, client_token_data
                    req_id = event.get("requestId")
                    kind = pending.pop(req_id, None)
                    if not kind:
                        return
                    try:
                        body_res = await cdp.send("Network.getResponseBody", {"requestId": req_id})
                        body = body_res.get("body", "")
                        data = json.loads(body)
                        if kind == "access":
                            access_token_data = data.get("accessToken")
                        elif kind == "client":
                            client_token_data = data.get("granted_token", {}).get("token")
                    except Exception:
                        pass

                cdp.on("Network.responseReceived", on_response)
                cdp.on("Network.loadingFinished", lambda ev: asyncio.create_task(on_loading_finished(ev)))

                await page.goto("https://open.spotify.com/", wait_until="domcontentloaded")
                for _ in range(50):
                    if access_token_data and client_token_data:
                        break
                    await asyncio.sleep(0.2)
            finally:
                await browser.close()

        if not access_token_data or not client_token_data:
            raise RuntimeError("Could not retrieve Spotify tokens via Playwright")

        self._access_token = access_token_data
        self._client_token = client_token_data
        self._expires_at = time.time() + 55 * 60

_spotify_tokens = _SpotifyTokenCache()

_SPOTIFY_PARTNER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
_SPOTIFY_HASHES = {
    "getTrack": "612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294",
    "getAlbum": "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10",
    "fetchPlaylistContents": "a65e12194ed5fc443a1cdebed5fabe33ca5b07b987185d63c72483867ad13cb4",
}
_SPOTIFY_APP_VERSION = "896000000"


async def _spotify_partner(operation: str, variables: dict, retry_on_401: bool = True) -> dict:
    """Call Spotify partner GraphQL endpoint."""
    payload = {
        "variables": variables,
        "operationName": operation,
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": _SPOTIFY_HASHES[operation],
            }
        },
    }
    await _spotify_tokens.ensure_fresh_async()
    headers = {
        "Authorization": f"Bearer {_spotify_tokens.access_token}",
        "client-token": _spotify_tokens.client_token,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "Accept-Language": "ru",
        "app-platform": "WebPlayer",
        "spotify-app-version": _SPOTIFY_APP_VERSION,
        "Origin": "https://open.spotify.com",
        "Referer": "https://open.spotify.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    }
    client = await get_http_client(enable_proxy=False)
    try:
        resp = await client.post(_SPOTIFY_PARTNER_URL, headers=headers, content=json.dumps(payload), timeout=30.0)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Spotify partner API request failed: {e}") from e
    if not resp.text or not resp.text.strip():
        raise RuntimeError("Spotify partner API returned an empty response.")
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        snippet = resp.text[:500]
        raise RuntimeError(
            f"Spotify partner API returned non-JSON data: {e}.\n"
            f"Response snippet: {snippet}"
        ) from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Spotify API returned unexpected format: {data}")
    is_401 = resp.status_code == 401
    err_obj = data.get("error", {})
    if isinstance(err_obj, dict):
        if err_obj.get("status") == 401 or "expired" in str(err_obj).lower() or "token" in str(err_obj).lower():
            is_401 = True
    if is_401:
        if retry_on_401:
            log.warning("Spotify API 401 error. Refreshing token cache and retrying...")
            _spotify_tokens.invalidate()
            await _spotify_tokens.ensure_fresh_async()
            return await _spotify_partner(operation, variables, retry_on_401=False)
        else:
            raise RuntimeError("Spotify API access token is invalid or expired after refresh.")
    if "errors" in data:
        raise RuntimeError(f"Spotify partner API error: {data['errors']}")
    if "data" not in data or not isinstance(data["data"], dict):
        raise RuntimeError(f"Spotify API missing 'data' object: {data}")
    return data


def _parse_spotify_url(url: str) -> tuple[str, str]:
    """Parse Spotify entity type and ID from URL."""
    m = re.search(r"/(track|album|playlist)/([A-Za-z0-9]+)", urlparse(url).path)
    if not m:
        raise RuntimeError(f"Could not parse Spotify URL: {url}")
    return m.group(1), m.group(2)


def _best_image(sources: list) -> str:
    """Find highest resolution image URL."""
    if not sources:
        return ""
    return max(sources, key=lambda s: s.get("maxHeight", 0) or s.get("height", 0)).get("url", "")


async def _spotify_get_collection_tracks(
    entity_type: str, entity_id: str
) -> tuple[str, str, list[dict]]:
    """Fetch track list for Spotify album or playlist."""
    if entity_type == "album":
        data = await _spotify_partner("getAlbum", {
            "uri": f"spotify:album:{entity_id}",
            "locale": "",
            "offset": 0,
            "limit": 50,
        })
        album = data["data"]["albumUnion"]
        name = album.get("name", "Spotify Album")
        cover = _best_image(
            album.get("coverArt", {}).get("sources", [])
        )
        album_artist = ", ".join(
            a["profile"]["name"]
            for a in album.get("artists", {}).get("items", [])
        )
        tracks = []
        disc_union = album.get("tracksV2", {})
        items = disc_union.get("items", [])
        offset = 50
        total = disc_union.get("totalCount", 0)
        while len(tracks) + len(items) < total and offset < total:
            page = await _spotify_partner("getAlbum", {
                "uri": f"spotify:album:{entity_id}",
                "locale": "",
                "offset": offset,
                "limit": 50,
            })
            items += page["data"]["albumUnion"]["tracksV2"]["items"]
            offset += 50
        for item in items:
            t = item.get("track", {})
            if not t:
                continue
            artists = ", ".join(
                a["profile"]["name"] for a in t.get("artists", {}).get("items", [])
            ) or album_artist
            tracks.append({
                "artist": artists,
                "title": t.get("name", ""),
                "thumb": cover,
            })
        return name, cover, tracks
    else:
        data = await _spotify_partner("fetchPlaylistContents", {
            "uri": f"spotify:playlist:{entity_id}",
            "offset": 0,
            "limit": 50,
            "includeEpisodeContentRatingsV2": True,
        })
        playlist = data["data"]["playlistV2"]
        name = playlist.get("name", "Spotify Playlist")
        cover_sources = playlist.get("images", {}).get("items", [{}])[0].get("sources", []) if playlist.get("images", {}).get("items") else []
        cover = _best_image(cover_sources)
        content = playlist.get("content", {})
        total = content.get("totalCount", 0)
        items = content.get("items", [])
        offset = 50
        while len(items) < total:
            page = await _spotify_partner("fetchPlaylistContents", {
                "uri": f"spotify:playlist:{entity_id}",
                "offset": offset,
                "limit": 50,
                "includeEpisodeContentRatingsV2": True,
            })
            items += page["data"]["playlistV2"]["content"]["items"]
            offset += 50
        tracks = []
        for item in items:
            item_v2 = item.get("itemV2", {})
            if item_v2.get("__typename") != "TrackResponseWrapper":
                continue
            t = item_v2.get("data", {})
            if not t:
                continue
            artists = ", ".join(
                a["profile"]["name"] for a in t.get("artists", {}).get("items", [])
            )
            cover_sources = t.get("albumOfTrack", {}).get("coverArt", {}).get("sources", [])
            thumb = _best_image(cover_sources) or cover
            tracks.append({
                "artist": artists,
                "title": t.get("name", ""),
                "thumb": thumb,
            })
        return name, cover, tracks


async def download_spotify(
    url: str,
    tmpdir: Path,
    on_progress: ProgressCallback | None = None,
    audio_format: str = "mp3_192",
    should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download Spotify track, album, or playlist.

    Args:
        url: Spotify track/album/playlist link.
        tmpdir: Directory for temporary file output.
        on_progress: Async callback for status updates.
        audio_format: Codec/bitrate selection key.
        should_cancel: Cancellation condition check function.

    Returns:
        DownloadResult containing media file path or ZIP archive path.
    """
    entity_type, entity_id = _parse_spotify_url(url)
    if entity_type == "track":
        if on_progress:
            await on_progress("Getting Spotify track info...")
        data = await _spotify_partner("getTrack", {
            "uri": f"spotify:track:{entity_id}",
        })
        t = data["data"]["trackUnion"]
        track_name = t.get("name", "")
        artists_list = []
        for src in ("firstArtist", "otherArtists"):
            for a in t.get(src, {}).get("items", []):
                if a.get("profile", {}).get("name"):
                    artists_list.append(a["profile"]["name"])
        artist = ", ".join(artists_list)
        cover_sources = t.get("albumOfTrack", {}).get("coverArt", {}).get("sources", [])
        thumb_url = _best_image(cover_sources) or None
        if not track_name or not artist:
            raise RuntimeError(
                f"Could not get track info: artist={artist!r} title={track_name!r}"
            )
        if on_progress:
            await on_progress(f"Searching '{artist} - {track_name}' on YouTube Music...")
        return await _download_track_search(
            artist, track_name, tmpdir,
            on_progress=on_progress,
            audio_format=audio_format,
            thumb_url=thumb_url,
            should_cancel=should_cancel,
        )
    if on_progress:
        await on_progress("Getting Spotify track list...")
    collection_name, cover_url, tracks = await _spotify_get_collection_tracks(entity_type, entity_id)
    if not tracks:
        raise RuntimeError(f"Spotify {entity_type} contains no tracks")
    if on_progress:
        await on_progress(f"Downloading '{collection_name}' ({len(tracks)} tracks)...")

    async def _spotify_download_one(track: dict, idx: int, total: int, track_tmpdir: Path) -> DownloadResult:
        artist = track["artist"] or "Unknown"
        title = track["title"] or f"Track {idx}"
        thumb = track.get("thumb") or None
        if on_progress:
            await on_progress(f"Downloading {idx}/{total}: {artist} - {title}")
        return await _download_track_search(
            artist, title, track_tmpdir,
            audio_format=audio_format,
            thumb_url=thumb,
            should_cancel=should_cancel,
        )

    return await _process_collection_tracks(
        tracks, collection_name, cover_url, tmpdir,
        on_progress=on_progress,
        should_cancel=should_cancel,
        download_one=_spotify_download_one,
        uploader="Spotify",
    )

