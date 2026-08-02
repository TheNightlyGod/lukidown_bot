"""Kinopoisk movie and series metadata fetcher and stream extractor."""

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Browser, Playwright, async_playwright

from config import config
from downloaders.core import (
    CancelCheck,
    DownloadCancelled,
    DownloadResult,
    ProgressCallback,
    _human_size,
    _safe_filename,
)
from downloaders.http import get_http_client

_playwright_instance: Playwright | None = None
_browser_instance: Browser | None = None
_browser_lock = asyncio.Lock()


async def _get_browser() -> Browser:
    """Get or initialize a shared Playwright browser instance."""
    global _playwright_instance, _browser_instance
    if _browser_instance is not None and _browser_instance.is_connected():
        return _browser_instance
    async with _browser_lock:
        if _browser_instance is not None and _browser_instance.is_connected():
            return _browser_instance
        if _playwright_instance is None:
            _playwright_instance = await async_playwright().start()
        _browser_instance = await _playwright_instance.chromium.launch(headless=True)
        return _browser_instance


def _is_prime(n: int) -> bool:
    """Check if number n is a prime integer."""
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    c = 3
    while c * c <= n:
        if n % c == 0:
            return False
        c += 2
    return True


def _next_prime_above(n: int) -> int:
    """Find smallest prime integer strictly greater than n."""
    c = max(2, n + 1)
    while not _is_prime(c):
        c += 1
    return c


def _bit_length(i: int) -> int:
    """Return number of bits required to represent integer i."""
    count = 0
    while i > 0:
        count += 1
        i >>= 1
    return count


def _log2_ceil(n: int) -> int:
    """Compute ceiling of base 2 logarithm of integer n."""
    aU = 0
    while (1 << aU) < n:
        aU += 1
    return aU


def a5(s: str) -> str:
    """Permute characters in string s using prime step modular arithmetic."""
    ah = len(s)
    if ah <= 0:
        return s
    ax = _next_prime_above(ah)
    ac = []
    seen = [False] * ah
    pos = 0
    while len(ac) < ah:
        pos = (pos + 2) % ax
        if pos < ah and not seen[pos]:
            ac.append(pos)
            seen[pos] = True
    result = [''] * ah
    for i in range(ah):
        result[ac[i]] = s[i]
    return ''.join(result)


def a6(s: str) -> str:
    """Permute characters in string s using trailing zero bit count buckets."""
    ah = len(s)
    if ah <= 0:
        return s
    aU = _log2_ceil(ah)
    def ax(i: int) -> int:
        if i == 0:
            return aU
        count = 0
        while not (i & 1):
            count += 1
            i >>= 1
        return count
    bucket_sizes = [0] * (aU + 1)
    for i in range(ah):
        bucket_sizes[ax(i)] += 1
    pieces = []
    pos = 0
    for b in range(aU + 1):
        pieces.append(s[pos: pos + bucket_sizes[b]])
        pos += bucket_sizes[b]
    ptrs = [0] * (aU + 1)
    result = [''] * ah
    for i in range(ah):
        b = ax(i)
        result[i] = pieces[b][ptrs[b]]
        ptrs[b] += 1
    return ''.join(result)


def a7(s: str) -> str:
    """Permute characters in string s using bit length bucket indexing."""
    ah = len(s)
    if ah <= 0:
        return s
    aU = _log2_ceil(ah)
    def ax(i: int) -> int:
        if i == 0:
            return 0
        return _bit_length(i)
    bucket_sizes = [0] * (aU + 1)
    for i in range(ah):
        bucket_sizes[ax(i)] += 1
    pieces = [None] * (aU + 1)
    pos = 0
    for b in range(aU, -1, -1):
        pieces[b] = s[pos: pos + bucket_sizes[b]]
        pos += bucket_sizes[b]
    ptrs = [0] * (aU + 1)
    result = [''] * ah
    for i in range(ah):
        b = ax(i)
        result[i] = pieces[b][ptrs[b]]
        ptrs[b] += 1
    return ''.join(result)


def sha256_hex(data: str) -> str:
    """Compute SHA-256 hexadecimal hash string for input data."""
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def build_wv(fingerprint_parts: list[str]) -> str:
    """Compute fingerprint hash from list of browser fingerprint elements."""
    joined = '||'.join(str(p) for p in fingerprint_parts)
    return sha256_hex(joined)


FIXED_WV = "4d8dbfa5cd49094e5cf3a6b75f003280425dd2392667f53bd65435cc8c7e4e2f"

def make_borth(wl: str, wv: str = FIXED_WV) -> str:
    """Generate Borth token header for player API."""
    permuted = a5(a6(a7(wl)))
    return f"{wv}|{permuted}"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
KINOPOISK_API_KEY = "c20595a1-3d8c-4cbd-92eb-fd7b0fa75c67"

async def get_kinopoisk_meta(kp_id: str) -> dict:
    """Fetch movie or series metadata from Kinopoisk Unofficial API."""
    url = f"https://kinopoiskapiunofficial.tech/api/v2.2/films/{kp_id}"
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "x-api-key": KINOPOISK_API_KEY,
        "User-Agent": UA,
    }
    try:
        client = await get_http_client(enable_proxy=False)
        r = await client.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def get_kinopoisk_id(url: str) -> str:
    """Extract Kinopoisk item ID from URL."""
    m = re.search(r'/(?:series|film|movie)/(\d+)', url)
    if not m:
        raise ValueError(f"Could not extract ID from URL: {url}")
    return m.group(1)

async def get_players(kinopoisk_id: str, retries: int = 3, delay: float = 2.0) -> list:
    """Get player iframe endpoints for Kinopoisk content."""
    url = f"https://p.linkpp.ink/api/players?kinopoisk={kinopoisk_id}"
    last_err = None
    client = await get_http_client(enable_proxy=False)
    for attempt in range(retries):
        try:
            r = await client.get(url, timeout=15, headers={"User-Agent": UA})
            r.raise_for_status()
            data = r.json()
            return data.get("data", [])
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(delay)
    raise RuntimeError(f"Player API request failed: {last_err}")

def get_alloha_player(players: list) -> dict:
    """Find Alloha player in players list."""
    for p in players:
        if p.get("type") == "Alloha":
            return p
    raise ValueError("Alloha player not found")

def extract_file_list(html: str) -> dict:
    """Extract file list JSON structure from iframe HTML."""
    m = re.search(r"const fileList\s*=\s*JSON\.parse\('((?:[^'\\]|\\.)*)'\)", html)
    if not m:
        raise ValueError("fileList not found in HTML")
    raw = m.group(1).replace("\\'", "'")
    return json.loads(raw)

def extract_token_from_url(iframe_url: str) -> str:
    """Extract query token from iframe URL."""
    parsed = urlparse(iframe_url)
    params = parse_qs(parsed.query)
    token = params.get("token", [None])[0]
    if not token:
        raise ValueError(f"token parameter not found in URL: {iframe_url}")
    return token

def find_all_entries(data) -> list[dict]:
    """Recursively extract video entries from file list."""
    entries = []
    if isinstance(data, dict):
        if "id_translation" in data and "id" in data:
            entries.append(data)
        else:
            for v in data.values():
                entries.extend(find_all_entries(v))
    elif isinstance(data, list):
        for item in data:
            entries.extend(find_all_entries(item))
    return entries

def list_translations(file_list: dict) -> list[dict]:
    """List available audio translations."""
    seen_ids = set()
    result = []
    all_entries = find_all_entries(file_list.get("all", {}))
    non_sub_entries = [
        e for e in all_entries
        if not re.search(r"субтитр|subtitles", str(e.get("translation", "")), re.IGNORECASE)
    ]
    entries_to_use = non_sub_entries if non_sub_entries else all_entries
    for entry in entries_to_use:
        tid = entry.get("id_translation")
        if tid is not None and tid not in seen_ids:
            seen_ids.add(tid)
            result.append({
                "id": tid,
                "name": entry.get("translation", "?"),
            })
    return sorted(result, key=lambda x: x["id"])

def list_seasons(file_list: dict) -> list[int]:
    """List available season numbers."""
    return sorted(int(s) for s in file_list.get("all", {}).keys() if str(s).isdigit())

def list_episodes(file_list: dict, season: int) -> list[int]:
    """List available episode numbers for a season."""
    season_data = file_list.get("all", {}).get(str(season), {})
    return sorted(int(e) for e in season_data.keys() if str(e).isdigit())

def get_movie_entry(file_list: dict, translation_id: int | None = None) -> dict:
    """Get movie video entry matching translation."""
    all_entries = find_all_entries(file_list.get("all", {}))
    if translation_id is not None:
        for entry in all_entries:
            if entry.get("id_translation") == translation_id:
                return entry
        raise ValueError(f"Translation ID {translation_id} not found.")
    active = file_list.get("active")
    if active and "id" in active:
        return active
    if all_entries:
        return all_entries[0]
    raise ValueError("Could not find movie entries in fileList")

def get_episode_entry(
    file_list: dict,
    season: int,
    episode: int,
    translation_id: int | None = None,
) -> dict:
    """Get series episode entry matching season, episode and translation."""
    season_data = file_list.get("all", {}).get(str(season), {})
    ep_data = season_data.get(str(episode), {})
    if not ep_data:
        raise ValueError(f"Episode S{season:02d}E{episode:02d} not found in fileList")
    if translation_id is not None:
        key = f"t{translation_id}"
        entry = ep_data.get(key)
        if not entry:
            raise ValueError(f"Translation {translation_id} for S{season:02d}E{episode:02d} not found.")
        return entry
    return next(iter(ep_data.values()))

async def fetch_kinopoisk_info(kp_url: str) -> dict:
    """Fetch content metadata, season list, and translation list for Kinopoisk URL."""
    kp_id = get_kinopoisk_id(kp_url)
    meta = await get_kinopoisk_meta(kp_id)
    players = await get_players(kp_id)
    alloha = get_alloha_player(players)
    target_iframe_url = alloha["iframeUrl"]
    browser = await _get_browser()
    context = await browser.new_context(user_agent=UA)
    try:
        page = await context.new_page()
        await page.route("**/*app.1216f2e9.js*", lambda route: route.abort())
        await page.goto("https://linkpp.ink/", wait_until="domcontentloaded")
        await page.evaluate(f"""() => {{
            const iframe = document.createElement('iframe');
            iframe.src = '{target_iframe_url}';
            iframe.id = 'player_iframe';
            document.body.appendChild(iframe);
        }}""")
        await page.wait_for_timeout(2000)
        frame = None
        for f in page.frames:
            if "theatre.stravers.live" in f.url:
                frame = f
                break
        if not frame:
            raise RuntimeError("Failed to load player iframe")
        html = await frame.content()
    finally:
        await context.close()

    file_list = extract_file_list(html)
    content_type = file_list.get("type", "movie")
    translations = list_translations(file_list)
    seasons = list_seasons(file_list) if content_type == "serial" else []
    return {
        "kp_id": kp_id,
        "content_type": content_type,
        "file_list": file_list,
        "translations": translations,
        "seasons": seasons,
        "meta": meta,
    }

async def extract_kinopoisk_stream(
    kp_url: str,
    season: int | None = None,
    episode: int | None = None,
    translation_id: int | None = None,
) -> dict:
    """Extract stream links, quality options, and auth tokens for Kinopoisk item."""
    kp_id = get_kinopoisk_id(kp_url)
    meta = await get_kinopoisk_meta(kp_id)
    players = await get_players(kp_id)
    alloha = get_alloha_player(players)
    base_iframe_url = alloha["iframeUrl"]
    target_iframe_url = base_iframe_url
    if translation_id is not None:
        for t in alloha.get("translations", []):
            if t.get("id") == translation_id:
                target_iframe_url = t["iframeUrl"]
                break
    browser = await _get_browser()
    context = await browser.new_context(user_agent=UA)
    try:
        page = await context.new_page()
        await page.route("**/*app.1216f2e9.js*", lambda route: route.abort())
        await page.goto("https://linkpp.ink/", wait_until="domcontentloaded")
        await page.evaluate(f"""() => {{
            const iframe = document.createElement('iframe');
            iframe.src = '{target_iframe_url}';
            iframe.id = 'player_iframe';
            document.body.appendChild(iframe);
        }}""")
        await page.wait_for_timeout(2000)
        frame = None
        for f in page.frames:
            if "theatre.stravers.live" in f.url:
                frame = f
                break
        if not frame:
            raise RuntimeError("Failed to load player iframe")
        meta_wl = await frame.evaluate("() => document.querySelector('meta[name=\"viewporti\"]')?.content")
        if not meta_wl:
            html_content = await frame.content()
            meta_wl = re.search(r'<meta\s+name=["\']viewporti["\']\s+content=["\']([^"\']+)["\']', html_content).group(1)
        html = await frame.content()
        file_list = extract_file_list(html)
        token = extract_token_from_url(target_iframe_url)
        content_type = file_list.get("type", "movie")
        raw_name = meta.get("nameRu") or meta.get("nameOriginal") or meta.get("nameEn")
        year = meta.get("year")
        year_str = f" ({year})" if year else ""
        if content_type == "serial":
            if season is None or episode is None:
                seasons = list_seasons(file_list)
                season = season or (seasons[0] if seasons else 1)
                episodes = list_episodes(file_list, season)
                episode = episode or (episodes[0] if episodes else 1)
            entry = get_episode_entry(file_list, season, episode, translation_id)
            if raw_name:
                title = f"{raw_name} - S{season:02d}E{episode:02d}"
            else:
                title = f"Kinopoisk_S{season:02d}E{episode:02d}"
        else:
            entry = get_movie_entry(file_list, translation_id)
            if raw_name:
                title = f"{raw_name}{year_str}"
            else:
                title = f"Kinopoisk_Film_{kp_id}"
        episode_id = entry["id"]
        borth_value = make_borth(meta_wl)
        fetch_js = """async (args) => {
            const res = await fetch('https://theatre.stravers.live/bnsi/movies/' + args.ep_id, {
                method: 'POST',
                headers: {
                    'accept': '*/*',
                    'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'x-requested-with': 'XMLHttpRequest',
                    'borth': args.borth
                },
                body: 'token=' + args.token + '&av1=true&autoplay=0&audio=&subtitle='
            });
            return { status: res.status, data: await res.json() };
        }"""
        res = await frame.evaluate(fetch_js, {"ep_id": episode_id, "borth": borth_value, "token": token})
    finally:
        await context.close()

    if res["status"] != 200:
        raise RuntimeError(f"HTTP {res['status']}: {res['data']}")
    result = res["data"]
    guard_token = (
        result.get("guard")
        or result.get("edge_hash")
        or result.get("token")
        or "pXzvbyDGLYyB6VkwsWZDv3iMKZtsXNzpzRyxZUcsKHXxsSeaYakbo3hw9mBFRc5VQTpqAX6BW8aDEqyLaHYcXSQiV6KHYTVTK6MYRphNAy5sBjtrevqkDzKmLqNdfMZGEU9NELjmtKfZy3RNGzCd767sNh1mXEj4tCcvqndHtzmwAbZNkhm4ghDEasodotMBewypNQ56uotJAQGX11csfeRfBAPk8DcUWWkkqzxca8vbnEw12vUFbBzT6hz8ZB3F3dzUhUXoL2cr1WM1bXQArRCS1MUNMz3X5WDMMQoZKxj2AMTRqp7QQX4dDB9B7VzEZTmyFULhm1AcHHMkoMvSVvKYoBoAKLycYAgMHeD4ECJcGEAGpnkJhrV57zQ7"
    )
    hls_sources = result.get("hlsSource", [])
    quality_map = hls_sources[0].get("quality", {}) if hls_sources else {}
    return {
        "title": title,
        "guard_token": guard_token,
        "quality_map": quality_map,
        "entry": entry,
        "result_data": result,
        "target_iframe_url": target_iframe_url,
        "meta": meta,
    }

from i18n import get_text


async def download_kinopoisk(
    url: str,
    tmpdir: Path,
    want_audio: bool = False,
    on_progress: ProgressCallback | None = None,
    audio_format: str = "mp3_192",
    video_quality: str = "1080",
    should_cancel: CancelCheck | None = None,
    season: int | None = None,
    episode: int | None = None,
    translation_id: int | None = None,
    lang: str = "ru",
) -> DownloadResult:
    """Download Kinopoisk stream via FFmpeg."""
    if on_progress:
        await on_progress(get_text(lang, "dl_kinopoisk_links"))
    stream_info = await extract_kinopoisk_stream(
        url,
        season,
        episode,
        translation_id,
    )
    quality_map = stream_info.get("quality_map", {})
    if not quality_map:
        raise RuntimeError("Could not find available video streams.")
    m3u8_url = quality_map.get(video_quality)
    if not m3u8_url:
        best_q = next(iter(quality_map.keys()))
        m3u8_url = quality_map[best_q]
    guard_token = stream_info.get("guard_token", "")
    title = stream_info.get("title", "Kinopoisk_Video")
    target_iframe_url = stream_info.get("target_iframe_url", "https://theatre.stravers.live/")
    ext = ".mp3" if want_audio else ".mp4"
    out_path = tmpdir / f"{_safe_filename(title)}{ext}"
    headers_str = (
        f"Accept: */*\r\n"
        f"Accept-Language: ru,en-US;q=0.9,en;q=0.8\r\n"
        f"Accepts-Controls: 4d8dbfa5cd49094e5cf3a6b75f003280425dd2392667f53bd65435cc8c7e4e2f\r\n"
        f"Authorizations: Bearer {guard_token}\r\n"
        f"Origin: https://theatre.stravers.live\r\n"
        f"Referer: {target_iframe_url}\r\n"
        f"User-Agent: {UA}\r\n"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-headers", headers_str,
        "-i", m3u8_url,
    ]
    if want_audio:
        cmd.extend(["-vn", "-c:a", "libmp3lame", "-b:a", "192k"])
    else:
        cmd.extend(["-c", "copy"])
    cmd.extend(["-progress", "pipe:1", str(out_path)])
    if on_progress:
        await on_progress(get_text(lang, "dl_downloading_audio") if want_audio else get_text(lang, "dl_downloading_video"))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_lines: list[str] = []

    async def _drain_stderr():
        while True:
            line_bytes = await proc.stderr.readline()
            if not line_bytes:
                break
            line_str = line_bytes.decode(errors="ignore").strip()
            if line_str:
                stderr_lines.append(line_str)
                if len(stderr_lines) > 50:
                    stderr_lines.pop(0)

    stderr_task = asyncio.create_task(_drain_stderr())
    last_update = 0.0
    last_bytes = 0
    last_time = time.time()
    last_speed = 0.0
    current_size = 0
    current_out_time = ""
    current_speed_str = ""
    try:
        while True:
            if should_cancel and should_cancel():
                proc.kill()
                await proc.wait()
                raise DownloadCancelled("Download cancelled")
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode(errors="ignore").strip()
            if "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if k == "total_size" and v.isdigit():
                    current_size = int(v)
                elif k == "out_time":
                    current_out_time = v.split(".")[0]
                elif k == "speed":
                    current_speed_str = v
            now = time.time()
            if on_progress and (now - last_update >= 2.0):
                size = current_size or (out_path.stat().st_size if out_path.exists() else 0)
                if size > 0:
                    dt = now - last_time
                    if dt > 0 and size > last_bytes:
                        last_speed = (size - last_bytes) / dt
                        last_bytes = size
                        last_time = now
                    last_update = now
                    max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
                    size_str = _human_size(size)
                    if size > max_bytes:
                        size_str += get_text(lang, "will_be_compressed")
                    parts = [size_str]
                    if last_speed > 0:
                        parts.append(f"{_human_size(last_speed)}/s")
                    elif current_speed_str and current_speed_str != "N/A":
                        parts.append(current_speed_str)
                    if current_out_time and current_out_time not in ("N/A", "00:00:00"):
                        parts.append(current_out_time)
                    await on_progress(" / ".join(parts))
    finally:
        await stderr_task

    rc = await proc.wait()
    if rc != 0:
        err_msg = "\n".join(stderr_lines[-10:])
        raise RuntimeError(f"FFmpeg error (code {rc}): {err_msg}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("Final file not created or empty.")
    return DownloadResult(
        filepath=out_path,
        title=title,
        uploader=None,
        is_audio=want_audio,
        filesize=out_path.stat().st_size,
    )
