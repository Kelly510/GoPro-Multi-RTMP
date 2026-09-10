"""Hardware-free multi-publisher end-to-end recording test."""

from __future__ import annotations

import math
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, public_config
from .manifest import Manifest
from .mediamtx import MediaMTXRunner, find_mediamtx, inspect_recordings


class SyntheticTestError(RuntimeError):
    """The local RTMP recording path failed its integration test."""


def _publisher_command(ffmpeg: str, url: str, duration: float, frequency: int) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=30",
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:sample_rate=48000",
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "30",
        "-c:a",
        "aac",
        "-f",
        "flv",
        url,
    ]


def run_synthetic_test(config: AppConfig, duration: float = 5.0) -> Path:
    """Publish one generated A/V stream per configured camera and verify every MP4."""
    if not math.isfinite(duration) or duration < 2:
        raise SyntheticTestError("selftest duration 必须是至少 2 秒的有限数值")
    binary = find_mediamtx(config.server.mediamtx_binary)
    if binary is None:
        raise SyntheticTestError("找不到 mediamtx；请先执行 brew install mediamtx")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SyntheticTestError("找不到 ffmpeg；请先执行 brew install ffmpeg")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_id = f"{stamp}_selftest_{uuid.uuid4().hex[:6]}"
    session_dir = config.server.output_root / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    manifest = Manifest(
        session_dir / "manifest.json",
        session_id,
        public_config(config, "127.0.0.1"),
    )
    manifest.data["mode"] = "synthetic_selftest"
    manifest.write()
    server = MediaMTXRunner(
        binary,
        session_dir,
        config.network.rtmp_port,
        config.server.api_port,
        config.cameras,
        config.server,
        config.preview,
    )
    publishers: list[subprocess.Popen[Any]] = []
    logs: list[Any] = []
    failure: str | None = None
    try:
        server.start()
        manifest.event("system", "mediamtx_ready")
        for index, camera in enumerate(config.cameras):
            log_path = session_dir / "logs" / f"ffmpeg_{camera.alias}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = log_path.open("w", encoding="utf-8")
            logs.append(handle)
            url = f"rtmp://127.0.0.1:{config.network.rtmp_port}/live/{camera.stream_key}"
            publishers.append(
                subprocess.Popen(
                    _publisher_command(ffmpeg, url, duration, 440 + index * 220),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
            manifest.event(camera.alias, "synthetic_publisher_started", rtmp_url=url)

        names = {f"live/{camera.stream_key}" for camera in config.cameras}
        if not server.wait_paths(
            names,
            online=True,
            timeout=config.timeouts.publisher_ready_seconds,
        ):
            raise SyntheticTestError("MediaMTX 未同时看到全部合成发布流")
        manifest.status("recording")
        deadline = time.monotonic() + duration + 20
        for publisher in publishers:
            remaining = max(1.0, deadline - time.monotonic())
            code = publisher.wait(timeout=remaining)
            if code != 0:
                raise SyntheticTestError(f"FFmpeg 合成发布失败，退出码 {code}")
        server.wait_paths(names, online=False, timeout=5)
    except BaseException as exc:
        failure = str(exc)
    finally:
        for publisher in publishers:
            if publisher.poll() is None:
                publisher.terminate()
                try:
                    publisher.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    publisher.kill()
                    publisher.wait(timeout=3)
        for handle in logs:
            handle.close()
        server.stop(timeout=config.timeouts.shutdown_seconds)

    recordings = inspect_recordings(
        session_dir,
        config.cameras,
        sha256=config.server.sha256,
    )
    manifest.recordings(recordings)

    def is_valid_recording(item: dict[str, Any]) -> bool:
        probe = item.get("ffprobe", {})
        streams = probe.get("streams", [])
        try:
            duration_seconds = float(probe.get("format", {}).get("duration", 0))
        except (TypeError, ValueError):
            return False
        return (
            item.get("bytes", 0) > 0
            and duration_seconds > 0
            and any(stream.get("codec_type") == "video" for stream in streams)
            and (
                not config.stream.require_audio
                or any(stream.get("codec_type") == "audio" for stream in streams)
            )
        )

    valid_aliases = {
        item["camera"]
        for item in recordings
        if is_valid_recording(item)
    }
    expected_aliases = {camera.alias for camera in config.cameras}
    if failure is None and valid_aliases != expected_aliases:
        failure = f"录像验证不完整：expected={sorted(expected_aliases)}, actual={sorted(valid_aliases)}"
    if failure:
        manifest.status("failed", error=failure)
        raise SyntheticTestError(failure)
    manifest.status("complete")
    return session_dir
