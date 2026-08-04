"""Platform detection and URL parsing utilities."""

import re
from enum import Enum, auto


class Platform(Enum):
    """Supported media platforms for link extraction and download processing."""

    YOUTUBE = auto()
    TIKTOK = auto()
    INSTAGRAM = auto()
    PINTEREST = auto()
    RUTUBE = auto()
    VK = auto()
    SPOTIFY = auto()
    SHAZAM = auto()
    SOUNDCLOUD = auto()
    YANDEX = auto()
    VK_MUSIC = auto()
    DEEZER = auto()
    APPLE_MUSIC = auto()
    TENOR = auto()
    JIOSAAVN = auto()
    TWITCH = auto()
    SNAPCHAT = auto()
    REDDIT = auto()
    KINOPOISK = auto()
    UNKNOWN = auto()


PLATFORM_PATTERNS: list[tuple[Platform, list[str]]] = [
    (Platform.KINOPOISK, [
        r"(?:https?://)?(?:www\.)?kinopoisk\.ru/(?:film|series|movie)/\d+",
    ]),
    (Platform.YOUTUBE, [
        r"(?:https?://)?(?:www\.)?youtube\.com/(?:watch|shorts|live|playlist)",
        r"(?:https?://)?(?:www\.)?youtu\.be/",
        r"(?:https?://)?(?:m\.)?youtube\.com/",
        r"(?:https?://)?(?:music\.)?youtube\.com/",
    ]),
    (Platform.TIKTOK, [
        r"(?:https?://)?(?:www\.)?tiktok\.com/",
        r"(?:https?://)?vm\.tiktok\.com/",
        r"(?:https?://)?vt\.tiktok\.com/",
    ]),
    (Platform.INSTAGRAM, [
        r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/",
        r"(?:https?://)?(?:www\.)?instagram\.com/stories/",
    ]),
    (Platform.PINTEREST, [
        r"(?:https?://)?(?:www\.)?pinterest\.\w+/pin/",
        r"(?:https?://)?(?:www\.)?pinterest\.\w+/",
        r"(?:https?://)?pin\.it/",
    ]),
    (Platform.RUTUBE, [
        r"(?:https?://)?(?:www\.)?rutube\.ru/video/",
        r"(?:https?://)?(?:www\.)?rutube\.ru/play/",
    ]),
    (Platform.VK_MUSIC, [
        r"(?:https?://)?(?:www\.)?vk\.ru/audio-?\d+_\d+",
        r"(?:https?://)?(?:www\.)?vk\.ru/music/album/",
        r"(?:https?://)?(?:www\.)?vk\.ru/music/playlist/",
    ]),
    (Platform.VK, [
        r"(?:https?://)?(?:www\.)?vk\.ru/video",
        r"(?:https?://)?(?:www\.)?vk\.ru/clip",
        r"(?:https?://)?(?:www\.)?vk\.ru/wall.*video",
        r"(?:https?://)?vkvideo\.ru/",
    ]),
    (Platform.SPOTIFY, [
        r"(?:https?://)?open\.spotify\.com/(?:track|album|playlist|artist)/",
    ]),
    (Platform.SHAZAM, [
        r"(?:https?://)?(?:www\.)?shazam\.com/track/",
        r"(?:https?://)?(?:www\.)?shazam\.com/song/",
    ]),
    (Platform.SOUNDCLOUD, [
        r"(?:https?://)?(?:[a-zA-Z0-9-]+\.)?soundcloud\.com/",
        r"(?:https?://)?snd\.sc/",
    ]),
    (Platform.YANDEX, [
        r"(?:https?://)?music\.yandex\.\w+/album/(\d+)",
        r"(?:https?://)?music\.yandex\.\w+/album/(\d+)/track/(\d+)",
        r"(?:https?://)?music\.yandex\.\w+/users/[^/]+/playlists/[^/?\s]+",
        r"(?:https?://)?music\.yandex\.\w+/playlists/[^/?\s]+",
    ]),

    (Platform.DEEZER, [
        r"(?:https?://)?link\.deezer\.com/s/",
    ]),
    (Platform.APPLE_MUSIC, [
        r"(?:https?://)?music\.apple\.com/.+/song/",
    ]),
    (Platform.TENOR, [
        r"(?:https?://)?tenor\.com/",
    ]),
    (Platform.JIOSAAVN, [
        r"(?:https?://)?www\.jiosaavn\.com/song/",
    ]),
    (Platform.TWITCH, [
        r"(?:https?://)?www\.twitch\.tv/.+/clip/",
        r"(?:https?://)?www\.twitch\.tv/videos/"
    ]),
    (Platform.SNAPCHAT, [
        r"(?:https?://)?www\.snapchat\.com/",
    ]),
    (Platform.REDDIT, [
        r"(?:https?://)?www\.reddit\.com/",
    ]),
]

URL_REGEX = re.compile(
    r"https?://[^\s<>\"{}|\\^`\[\]]+"
    r"|(?:www\.|youtu\.be|vk\.ru|tiktok\.com|pin\.it)[^\s<>\"{}|\\^`\[\]]+"
)


def extract_url(text: str) -> str | None:
    """Extract a single URL candidate from input text.

    Args:
        text: Input string that may contain a media URL.

    Returns:
        Extracted URL prefixed with https:// if necessary, or None if no match is found.
    """
    match = URL_REGEX.search(text)
    if match:
        url = match.group(0).rstrip(".,;!?)")
        if not url.startswith("http"):
            url = "https://" + url
        return url
    return None


def detect_platform(url: str) -> Platform:
    """Identify the target media platform based on regex pattern matching.

    Args:
        url: Direct link to media item or web page.

    Returns:
        The matching Platform enum member, or Platform.UNKNOWN if unrecognized.
    """
    for platform, patterns in PLATFORM_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, url, re.IGNORECASE):
                return platform
    return Platform.UNKNOWN
