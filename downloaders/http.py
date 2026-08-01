"""HTTP client management for async network requests with optional SOCKS5 proxy support."""

import asyncio
import httpx
from config import config

_http_client_with_proxy: httpx.AsyncClient | None = None
_http_client_no_proxy: httpx.AsyncClient | None = None
_http_client_lock = asyncio.Lock()


async def get_http_client(enable_proxy: bool = True) -> httpx.AsyncClient:
    """Get or initialize a shared HTTP client instance.

    Args:
        enable_proxy: Whether to use configured SOCKS5 proxy if available.

    Returns:
        An active AsyncClient instance.
    """
    global _http_client_with_proxy, _http_client_no_proxy
    if enable_proxy and _http_client_with_proxy is not None:
        return _http_client_with_proxy
    if not enable_proxy and _http_client_no_proxy is not None:
        return _http_client_no_proxy
    async with _http_client_lock:
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
        timeout = httpx.Timeout(20.0, connect=5.0)
        if enable_proxy:
            if _http_client_with_proxy is None:
                _http_client_with_proxy = httpx.AsyncClient(
                    proxy=f"socks5://{config.SOCKS5_PROXY}" if config.SOCKS5_PROXY else None,
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                )
            return _http_client_with_proxy
        else:
            if _http_client_no_proxy is None:
                _http_client_no_proxy = httpx.AsyncClient(
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                )
            return _http_client_no_proxy


async def aclose_http_clients() -> None:
    """Close all open HTTP client sessions and clean up resources."""
    global _http_client_with_proxy, _http_client_no_proxy
    if _http_client_with_proxy is not None:
        await _http_client_with_proxy.aclose()
        _http_client_with_proxy = None
    if _http_client_no_proxy is not None:
        await _http_client_no_proxy.aclose()
        _http_client_no_proxy = None

