"""MediaMTX lifecycle, configuration, and recording inspection."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from .config import CameraConfig, ServerConfig


class MediaMTXError(RuntimeError):
    """MediaMTX failed to start or report expected state."""


def find_mediamtx(requested: str) -> Path | None:
    """Locate MediaMTX without modifying the host."""
    if requested != "auto":
        requested_candidate = Path(requested).expanduser().resolve()
        return requested_candidate if requested_candidate.is_file() else None
    which = shutil.which("mediamtx")
    candidates = [
        Path(which) if which else None,
        Path("/opt/homebrew/opt/mediamtx/bin/mediamtx"),
        Path("/usr/local/bin/mediamtx"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def _yaml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render_mediamtx_config(
    session_dir: Path,
    rtmp_port: int,
    api_port: int,
    cameras: Iterable[CameraConfig],
    server: ServerConfig,
) -> str:
    """Render a minimal, fixed-path MediaMTX configuration."""
    lines = [
        "logLevel: info",
        "logDestinations: [stdout]",
        "rtsp: false",
        "rtmp: true",
        'rtmpEncryption: "no"',
        f"rtmpAddress: :{rtmp_port}",
        "hls: false",
        "webrtc: false",
        "srt: false",
        "moq: false",
        "api: true",
        f"apiAddress: 127.0.0.1:{api_port}",
        "pathDefaults:",
        f"  recordPath: {_yaml_string((session_dir / 'streams' / '%path' / '%Y-%m-%d_%H-%M-%S-%f').resolve())}",
        "  recordFormat: fmp4",
        f"  recordPartDuration: {server.record_part_duration}",
        "  recordMaxPartSize: 50M",
        f"  recordSegmentDuration: {server.record_segment_duration}",
        "  recordDeleteAfter: 0s",
        "paths:",
    ]
    for camera in cameras:
        lines.extend(
            [
                f"  {_yaml_string('live/' + camera.stream_key)}:",
                "    source: publisher",
                "    overridePublisher: false",
                "    record: true",
            ]
        )
    return "\n".join(lines) + "\n"


class MediaMTXRunner:
    """Run a session-scoped MediaMTX process."""

    def __init__(
        self,
        binary: Path,
        session_dir: Path,
        rtmp_port: int,
        api_port: int,
        cameras: tuple[CameraConfig, ...],
        server: ServerConfig,
    ) -> None:
        self.binary = binary
        self.session_dir = session_dir
        self.rtmp_port = rtmp_port
        self.api_port = api_port
        self.cameras = cameras
        self.server = server
        self.process: subprocess.Popen[str] | None = None
        self._log_handle: Any = None
        self.config_path = session_dir / "runtime" / "mediamtx.yml"

    def start(self, timeout: float = 15.0) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            render_mediamtx_config(
                self.session_dir,
                self.rtmp_port,
                self.api_port,
                self.cameras,
                self.server,
            ),
            encoding="utf-8",
        )
        log_path = self.session_dir / "logs" / "mediamtx.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = log_path.open("w", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [str(self.binary), str(self.config_path)],
                cwd=self.session_dir,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise MediaMTXError(
                        f"MediaMTX 启动失败，退出码 {self.process.returncode}；查看 {log_path}"
                    )
                if self._tcp_open("127.0.0.1", self.rtmp_port) and self._api_alive():
                    return
                time.sleep(0.1)
            raise MediaMTXError(f"MediaMTX 未在 {timeout:.0f} 秒内监听 RTMP/API 端口")
        except BaseException:
            # start() owns partial resources until readiness, including Ctrl+C.
            self.stop(timeout=3.0)
            raise

    @staticmethod
    def _tcp_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            return False

    def _api_alive(self) -> bool:
        try:
            # The host can define HTTP(S)_PROXY; localhost control traffic must bypass it.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{self.api_port}/v3/paths/list", timeout=0.5) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def paths(self) -> dict[str, dict[str, Any]]:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{self.api_port}/v3/paths/list", timeout=2) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise MediaMTXError(f"无法读取 MediaMTX API：{exc}") from exc
        return {item["name"]: item for item in payload.get("items", []) if "name" in item}

    def wait_paths(self, names: set[str], *, online: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = self.paths()
            except MediaMTXError:
                time.sleep(0.25)
                continue
            matched = all(bool(state.get(name, {}).get("online", False)) is online for name in names)
            if matched:
                return True
            time.sleep(0.25)
        return False

    def stop(self, timeout: float = 10.0) -> None:
        try:
            if self.process and self.process.poll() is None:
                if os.name == "nt":
                    self.process.terminate()
                else:
                    self.process.send_signal(signal.SIGINT)
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
        finally:
            self.process = None
            if self._log_handle:
                self._log_handle.close()
                self._log_handle = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_recordings(session_dir: Path, cameras: Iterable[CameraConfig], *, sha256: bool) -> list[dict[str, Any]]:
    """Collect file size and optional ffprobe metadata."""
    ffprobe = shutil.which("ffprobe")
    recordings: list[dict[str, Any]] = []
    for camera in cameras:
        stream_dir = session_dir / "streams" / "live" / camera.stream_key
        for path in sorted(stream_dir.rglob("*.mp4")):
            item: dict[str, Any] = {
                "camera": camera.alias,
                "path": str(path.relative_to(session_dir)),
                "bytes": path.stat().st_size,
            }
            if ffprobe:
                result = subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration,format_name:stream=codec_name,codec_type,width,height,r_frame_rate",
                        "-of",
                        "json",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if result.returncode == 0:
                    try:
                        item["ffprobe"] = json.loads(result.stdout)
                    except json.JSONDecodeError as exc:
                        item["ffprobe_error"] = f"ffprobe 返回无效 JSON：{exc}"
                else:
                    item["ffprobe_error"] = result.stderr.strip()
            if sha256:
                item["sha256"] = _sha256(path)
            recordings.append(item)
    return recordings
