"""Health monitoring server for checking system components status."""

import asyncio
import logging

from aiohttp import web

log = logging.getLogger("mediabot.health")

_app_ref = None
_service_ref = None


def init(app, service):
    """Initialize references to the Pyrogram Client app and ServiceContext.

    Args:
        app: Pyrogram Client instance.
        service: ServiceContext instance containing database and queue handles.
    """
    global _app_ref, _service_ref
    _app_ref = app
    _service_ref = service


async def _check_telegram() -> tuple[str, dict]:
    """Check connectivity to Telegram API by calling get_me().

    Returns:
        A tuple containing (status_str, details_dict).
    """
    try:
        me = await asyncio.wait_for(_app_ref.get_me(), timeout=5)
        return "OK", {"username": f"@{me.username}", "id": me.id}
    except asyncio.TimeoutError:
        return "TIMEOUT", {}
    except Exception as e:  # noqa: BLE001
        return "ERROR", {"error": str(e)}


async def _check_database() -> tuple[str, dict]:
    """Check connectivity to Redis and retrieve queue length status.

    Returns:
        A tuple containing (status_str, details_dict).
    """
    try:
        redis = _service_ref.redis
        await asyncio.wait_for(redis.ping(), timeout=3)
        queue_len = await redis.llen("download_queue")
        processing = await _service_ref.queue.get_processing_count()
        return "OK", {"redis": "OK", "queue_length": queue_len, "processing": processing}
    except asyncio.TimeoutError:
        return "TIMEOUT", {"error": "Redis ping timed out"}
    except Exception as e:  # noqa: BLE001
        return "ERROR", {"error": str(e)}


async def _check_downloader() -> tuple[str, dict]:
    """Check availability and version of yt-dlp binary or Python package.

    Returns:
        A tuple containing (status_str, details_dict).
    """
    try:
        import importlib.util
        import shutil
        import subprocess

        ytdlp = shutil.which("yt-dlp")
        if ytdlp is None:
            if importlib.util.find_spec("yt_dlp") is not None:
                return "OK", {"backend": "yt_dlp (python package)"}
            return "ERROR", {"error": "yt-dlp not found"}

        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                ytdlp, "--version",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ),
            timeout=5,
        )
        stdout, _ = await proc.communicate()
        version = stdout.decode().strip()
        return "OK", {"backend": "yt-dlp", "version": version}
    except asyncio.TimeoutError:
        return "TIMEOUT", {}
    except ImportError:
        return "ERROR", {"error": "yt-dlp not found"}
    except Exception as e:  # noqa: BLE001
        return "ERROR", {"error": str(e)}


async def handle_health(request: web.Request) -> web.Response:
    """HTTP request handler returning JSON representation of system health.

    Args:
        request: Incoming aiohttp web Request.

    Returns:
        An aiohttp web Response with JSON body and HTTP status 200 or 503.
    """
    tg_status, tg_detail = await _check_telegram()
    db_status, db_detail = await _check_database()
    dl_status, dl_detail = await _check_downloader()

    try:
        from bot import get_reconnect_status
        reconnect_detail = get_reconnect_status()
    except Exception as e:  # noqa: BLE001
        reconnect_detail = {"error": f"reconnect status unavailable: {e}"}

    all_ok = all(s == "OK" for s in (tg_status, db_status, dl_status))
    overall = "OK" if all_ok else "DEGRADED"

    body = {
        "status": overall,
        "components": {
            "telegram": tg_status,
            "database": db_status,
            "downloader": dl_status,
        },
        "details": {
            "telegram": tg_detail,
            "database": db_detail,
            "downloader": dl_detail,
            "reconnect": reconnect_detail,
        },
    }

    status_code = 200 if all_ok else 503
    return web.json_response(body, status=status_code)


def create_app() -> web.Application:
    """Create and configure the aiohttp Application for health endpoints.

    Returns:
        Configured aiohttp web Application.
    """
    http_app = web.Application()
    http_app.router.add_get("/", handle_health)
    http_app.router.add_get("/health", handle_health)
    return http_app


async def start_health_server(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Start the HTTP server on specified host and port for health checks.

    Args:
        host: Host IP address to bind to.
        port: Port number for health server.

    Returns:
        The running aiohttp AppRunner instance.
    """
    http_app = create_app()
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Health server listening on %s:%d", host, port)
    return runner