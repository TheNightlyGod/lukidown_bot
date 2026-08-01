"""Video transcoding and FFmpeg compression utilities."""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from downloaders.core import CancelCheck, DownloadCancelled, ProgressCallback, _human_size

log = logging.getLogger("mediabot.transcoder")


async def get_video_duration(filepath: Path) -> float:
    """Retrieve video duration in seconds using ffprobe.

    Args:
        filepath: Path to target video file.

    Returns:
        Duration in seconds as a float, or 0.0 on error.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(filepath),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and stdout:
            val = stdout.decode().strip()
            return float(val)
    except Exception as e:
        log.warning("ffprobe get_video_duration error for %s: %s", filepath, e)
    return 0.0


async def get_video_bitrate(filepath: Path) -> int:
    """Retrieve video stream bitrate in kbps using ffprobe.

    Args:
        filepath: Path to target video file.

    Returns:
        Video stream bitrate in kbps as an integer, or 0 on error.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=bit_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(filepath),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            val = stdout.decode().strip()
            if val.isdigit():
                return int(val) // 1000
    except Exception:
        pass
    return 0


async def compress_video(
    input_path: Path,
    output_path: Path,
    max_size_bytes: int,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> Path:
    """Compress video file to fit under specified maximum byte size using FFmpeg H.264.

    Args:
        input_path: Source video file path.
        output_path: Destination path for compressed video file.
        max_size_bytes: Target file size threshold in bytes.
        on_progress: Async progress callback function.
        should_cancel: Cancellation condition check function.

    Returns:
        Path to output compressed video file (or input_path if already within limits).

    Raises:
        DownloadCancelled: If compression was cancelled during processing.
        RuntimeError: If FFmpeg execution failed or produced an empty file.
    """
    input_size = input_path.stat().st_size if input_path.exists() else 0
    if input_size > 0 and input_size <= max_size_bytes:
        return input_path

    duration = await get_video_duration(input_path)
    if duration <= 0:
        duration = 5400.0

    safety_target_bytes = int(max_size_bytes * 0.95)
    target_total_bitrate = (safety_target_bytes * 8) / duration

    audio_bitrate_bps = 128_000
    video_bitrate_bps = int(target_total_bitrate - audio_bitrate_bps)

    if video_bitrate_bps < 250_000:
        video_bitrate_bps = 250_000

    v_bitrate_k = int(video_bitrate_bps / 1000)

    orig_v_bitrate_k = await get_video_bitrate(input_path)
    if orig_v_bitrate_k > 0 and v_bitrate_k > orig_v_bitrate_k:
        v_bitrate_k = int(orig_v_bitrate_k * 0.85)
    maxrate_k = int(v_bitrate_k * 1.25)
    bufsize_k = int(v_bitrate_k * 2)

    vf_scale = "scale='min(1920,iw)':-2"
    if v_bitrate_k < 800:
        vf_scale = "scale='min(854,iw)':-2"
    elif v_bitrate_k < 1600:
        vf_scale = "scale='min(1280,iw)':-2"

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "fast",
        "-b:v", f"{v_bitrate_k}k",
        "-maxrate", f"{maxrate_k}k",
        "-bufsize", f"{bufsize_k}k",
        "-vf", vf_scale,
        "-c:a", "aac",
        "-b:a", "128k",
        "-progress", "pipe:1",
        str(output_path),
    ]

    if on_progress:
        await on_progress(f"Запуск сжатия видео ({v_bitrate_k} kbps)...")

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

    try:
        while True:
            if should_cancel and should_cancel():
                proc.kill()
                await proc.wait()
                raise DownloadCancelled("Transcoding cancelled")

            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break

            line = line_bytes.decode(errors="ignore").strip()
            if line.startswith("out_time_us="):
                us_str = line.split("=")[1].strip()
                if us_str.isdigit():
                    out_time_sec = float(us_str) / 1_000_000.0
                    pct = min(100.0, (out_time_sec / duration) * 100.0) if duration > 0 else 0.0
                    now = time.time()
                    if on_progress and (now - last_update >= 2.0):
                        last_update = now
                        curr_size = output_path.stat().st_size if output_path.exists() else 0
                        await on_progress(
                            f"Сжатие видео: {pct:.1f}% ({_human_size(curr_size)})"
                        )
            elif line.startswith("out_time="):
                out_time_str = line.split("=")[1].strip().split(".")[0]
                now = time.time()
                if on_progress and (now - last_update >= 2.0):
                    last_update = now
                    curr_size = output_path.stat().st_size if output_path.exists() else 0
                    await on_progress(
                        f"Сжимаю видео... {out_time_str} ({_human_size(curr_size)})"
                    )
    finally:
        await stderr_task

    rc = await proc.wait()
    if rc != 0:
        err_msg = "\n".join(stderr_lines[-10:])
        raise RuntimeError(f"FFmpeg compression error (code {rc}): {err_msg}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Compressed video file not created or empty.")

    return output_path

