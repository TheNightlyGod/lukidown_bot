"""HTTP client management for async network requests with optional proxy support."""

import asyncio

import httpx

from config import config

_http_client_with_proxy: httpx.AsyncClient | None = None
_http_client_ru_proxy: httpx.AsyncClient | None = None
_http_client_no_proxy: httpx.AsyncClient | None = None
_http_client_lock = asyncio.Lock()


def _format_proxy_url(raw: str) -> str | None:
    """Format and validate proxy URL."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if "://" not in raw:
        return f"socks5://{raw}"
    return raw


async def get_http_client(enable_proxy: bool = True, is_ru: bool = False) -> httpx.AsyncClient:
    """Get or initialize a shared HTTP client instance.

    Args:
        enable_proxy: Whether to use configured proxy if available.
        is_ru: Whether the request is targeting a Russian service (prefers RU_PROXY).

    Returns:
        An active AsyncClient instance.
    """
    global _http_client_with_proxy, _http_client_ru_proxy, _http_client_no_proxy

    if not enable_proxy:
        if _http_client_no_proxy is not None:
            return _http_client_no_proxy
        async with _http_client_lock:
            if _http_client_no_proxy is None:
                limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
                timeout = httpx.Timeout(20.0, connect=5.0)
                _http_client_no_proxy = httpx.AsyncClient(
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                )
            return _http_client_no_proxy

    proxy_str = config.RU_PROXY if is_ru else config.SOCKS5_PROXY
    formatted_proxy = _format_proxy_url(proxy_str)

    if not formatted_proxy:
        return await get_http_client(enable_proxy=False)

    if is_ru:
        if _http_client_ru_proxy is not None:
            return _http_client_ru_proxy
        async with _http_client_lock:
            if _http_client_ru_proxy is None:
                limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
                timeout = httpx.Timeout(20.0, connect=5.0)
                _http_client_ru_proxy = httpx.AsyncClient(
                    proxy=formatted_proxy,
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                )
            return _http_client_ru_proxy
    else:
        if _http_client_with_proxy is not None:
            return _http_client_with_proxy
        async with _http_client_lock:
            if _http_client_with_proxy is None:
                limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
                timeout = httpx.Timeout(20.0, connect=5.0)
                _http_client_with_proxy = httpx.AsyncClient(
                    proxy=formatted_proxy,
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                )
            return _http_client_with_proxy


async def aclose_http_clients() -> None:
    """Close all open HTTP client sessions and clean up resources."""
    global _http_client_with_proxy, _http_client_ru_proxy, _http_client_no_proxy
    for client in (_http_client_with_proxy, _http_client_ru_proxy, _http_client_no_proxy):
        if client is not None:
            await client.aclose()
    _http_client_with_proxy = None
    _http_client_ru_proxy = None
    _http_client_no_proxy = None

