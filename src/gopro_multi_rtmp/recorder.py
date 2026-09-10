"""Labs+COHN-only multi-camera RTMP recording orchestration."""

from __future__ import annotations

import math
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from .config import AppConfig, CameraConfig, public_config, resolve_rtmp_host, validate_config
from .labs_cohn import LabsCohnClient
from .manifest import Manifest
from .mediamtx import MediaMTXError, MediaMTXRunner, find_mediamtx, inspect_recordings
from .preview import hls_url, open_preview_dashboard, write_preview_dashboard


class CaptureError(RuntimeError):
    """The capture session could not meet its all-or-nothing checks."""


@dataclass
class CameraRuntime:
    camera: CameraConfig
    client: LabsCohnClient
    request_lock: threading.Lock = field(default_factory=threading.Lock)


T = TypeVar("T")


def _session_id(label: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    safe_label = "".join(char for char in (label or "") if char.isalnum() or char in "_-")
    return "_".join(part for part in (stamp, safe_label, suffix) if part)


def labs_start_command(
    resolution: int,
    encode_to_sd: bool,
    *,
    auto_stop_seconds: int | None = None,
) -> str:
    """Build the official Labs live-stream action for HERO12/13."""
    quality = {480: "S", 720: "M", 1080: "L"}.get(resolution)
    if quality is None:
        raise ValueError("Labs live-stream resolution must be 480, 720, or 1080")
    command = f"!G{quality}{'C' if encode_to_sd else ''}"
    if auto_stop_seconds is not None:
        if not 1 <= auto_stop_seconds <= 86_400:
            raise ValueError("Labs auto-stop must be between 1 and 86400 seconds")
        command += f"!{auto_stop_seconds}E"
    return command


def labs_rtmp_target_command(url: str) -> str:
    """Build the official permanent RTMP target command."""
    if not url.startswith("rtmp://") or '"' in url or len(url) > 370:
        raise ValueError("unsafe or oversized RTMP URL")
    return f'!MRTMP="{url}"'


def _state_flag(state: dict[str, Any], status_id: int) -> bool:
    """Read a required raw Open GoPro boolean status by numeric ID."""
    statuses = state.get("status")
    value = statuses.get(str(status_id)) if isinstance(statuses, dict) else None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return value.strip() == "1"
    raise CaptureError(f"相机状态缺少或无法解析 status {status_id}")


class CaptureSession:
    """Record the selected HERO13 cameras through Labs commands over COHN."""

    def __init__(self, config: AppConfig, *, label: str | None = None) -> None:
        validate_config(config, hardware=True)
        self.config = config
        self.resolved_host = resolve_rtmp_host(config.network.rtmp_host)
        self.session_id = _session_id(label)
        self.session_dir = config.server.output_root / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self.manifest = Manifest(
            self.session_dir / "manifest.json",
            self.session_id,
            public_config(config, self.resolved_host),
        )
        self._manifest_lock = threading.Lock()
        binary = find_mediamtx(config.server.mediamtx_binary)
        if binary is None:
            raise CaptureError("找不到 mediamtx；请先执行 brew install mediamtx")
        self.server = MediaMTXRunner(
            binary,
            self.session_dir,
            config.network.rtmp_port,
            config.server.api_port,
            config.cameras,
            config.server,
            config.preview,
        )
        self.runtimes = tuple(
            CameraRuntime(
                camera,
                LabsCohnClient(
                    camera.cohn_ip or "",
                    camera.cohn_username or "",
                    camera.cohn_password or "",
                    timeout=config.timeouts.cohn_request_seconds,
                    ca_certificate=camera.cohn_ca_cert,
                ),
            )
            for camera in config.cameras
        )
        self._server_started = False
        self._start_attempted = False
        self._keepalive_stop = threading.Event()
        self._keepalive_threads: list[threading.Thread] = []
        self._sd_baseline: dict[str, str | None] = {}

    @property
    def _path_names(self) -> set[str]:
        return {f"live/{runtime.camera.stream_key}" for runtime in self.runtimes}

    def _event(self, source: str, state: str, **details: Any) -> None:
        with self._manifest_lock:
            self.manifest.event(source, state, **details)
        suffix = ""
        if state.endswith("warning") or state.endswith("failed"):
            suffix = f"：{details.get('message', '')}"
        elif state == "identified":
            suffix = f"：{details.get('model_name')} / {details.get('serial_number')}"
        elif state == "publisher_online":
            suffix = f"：{details.get('path')}"
        output = sys.stderr if state.endswith(("warning", "failed")) else sys.stdout
        print(f"[{source}] {state}{suffix}", file=output, flush=True)

    def _parallel(
        self,
        operation: str,
        function: Callable[[CameraRuntime], T],
        *,
        barrier: bool = False,
    ) -> tuple[dict[str, T], dict[str, int]]:
        """Run one bounded request per camera and raise only after all settle."""
        gate = threading.Barrier(len(self.runtimes)) if barrier and len(self.runtimes) > 1 else None

        def invoke(runtime: CameraRuntime) -> tuple[int, T]:
            if gate is not None:
                gate.wait(timeout=max(2.0, self.config.timeouts.cohn_request_seconds * 2))
            dispatched_ns = time.monotonic_ns()
            with runtime.request_lock:
                return dispatched_ns, function(runtime)

        results: dict[str, T] = {}
        dispatched: dict[str, int] = {}
        failures: dict[str, str] = {}
        with ThreadPoolExecutor(
            max_workers=len(self.runtimes),
            thread_name_prefix=f"gopro-{operation}",
        ) as executor:
            futures = {executor.submit(invoke, runtime): runtime for runtime in self.runtimes}
            for future in as_completed(futures):
                runtime = futures[future]
                try:
                    dispatched_ns, value = future.result()
                    results[runtime.camera.alias] = value
                    dispatched[runtime.camera.alias] = dispatched_ns
                except BaseException as exc:
                    failures[runtime.camera.alias] = str(exc) or type(exc).__name__
        if failures:
            detail = "；".join(f"{alias}: {message}" for alias, message in failures.items())
            raise CaptureError(f"{operation}失败：{detail}")
        return results, dispatched

    def _preflight(self) -> None:
        def probe(runtime: CameraRuntime) -> dict[str, Any]:
            runtime.client.keep_alive()
            info = runtime.client.get_camera_info()
            actual_serial = str(info.get("serial_number", "")).strip()
            if actual_serial.casefold() != runtime.camera.serial.casefold():
                raise CaptureError(
                    f"IP {runtime.camera.cohn_ip} 返回序列号 {actual_serial or '<missing>'}，"
                    f"配置期望 {runtime.camera.serial}"
                )
            state = runtime.client.get_camera_state()
            if _state_flag(state, 10):
                raise CaptureError("相机启动前仍在编码；拒绝把遗留流当成本次采集")
            if _state_flag(state, 8):
                raise CaptureError("相机启动前处于 BUSY；请等待相机空闲后重试")
            return info

        results, _ = self._parallel("COHN 预检", probe)
        for runtime in self.runtimes:
            info = results[runtime.camera.alias]
            self._event(
                runtime.camera.alias,
                "identified",
                cohn_ip=runtime.camera.cohn_ip,
                model_name=str(info.get("model_name", "unknown")),
                firmware_version=str(info.get("firmware_version", "unknown")),
                serial_number=str(info.get("serial_number", "unknown")),
            )

    def _rtmp_url(self, camera: CameraConfig) -> str:
        return (
            f"rtmp://{self.resolved_host}:{self.config.network.rtmp_port}"
            f"/live/{camera.stream_key}"
        )

    def _configure_streams(self) -> None:
        """Force audio on and store each session's exact RTMP target."""
        def configure(runtime: CameraRuntime) -> None:
            if self.config.stream.require_audio:
                # Labs DAUD=0 explicitly re-enables audio if an earlier Labs
                # command disabled it. The output file is still verified later.
                runtime.client.run_labs_command("$DAUD=0")
            runtime.client.run_labs_command(
                labs_rtmp_target_command(self._rtmp_url(runtime.camera))
            )

        self._parallel("RTMP/音频配置", configure)
        for runtime in self.runtimes:
            self._event(
                runtime.camera.alias,
                "stream_config_accepted",
                resolution=self.config.stream.resolution,
                audio_requested=self.config.stream.require_audio,
                sd_backup_requested=self.config.stream.encode_to_sd,
                rtmp_path=f"live/{runtime.camera.stream_key}",
            )
        # The endpoint confirms acceptance rather than completion. Give the Labs
        # parser a short, common settle window before releasing the start barrier.
        time.sleep(0.5)

    @staticmethod
    def _media_path(response: dict[str, Any]) -> str | None:
        value = response.get("path")
        if isinstance(value, str) and value.strip():
            return value.strip()
        # Open GoPro's MediaPath JSON uses separate folder/file fields.
        folder = response.get("folder")
        filename = response.get("file")
        if (
            isinstance(folder, str)
            and folder.strip()
            and isinstance(filename, str)
            and filename.strip()
        ):
            return f"{folder.strip().rstrip('/')}/{filename.strip().lstrip('/')}"
        return None

    def _snapshot_sd_before(self) -> None:
        """Remember last media so the post-stop query can prove a new SD file."""
        if not self.config.stream.encode_to_sd:
            return
        results, _ = self._parallel(
            "SD 基线查询",
            lambda runtime: runtime.client.get_last_captured_media(),
        )
        self._sd_baseline = {
            alias: self._media_path(response) for alias, response in results.items()
        }

    def _dispatch_start(self, duration: float | None) -> tuple[float, float]:
        # For finite sessions, add a camera-side end action as a last-resort
        # safety timer. Publisher readiness plus a 10-second margin prevents the
        # fallback timer from shortening a slow-starting but otherwise valid run.
        fallback_total = (
            duration + self.config.timeouts.publisher_ready_seconds + 10.0
            if duration is not None
            else None
        )
        auto_stop_seconds = (
            math.ceil(fallback_total)
            if fallback_total is not None and fallback_total <= 86_400
            else None
        )
        command = labs_start_command(
            self.config.stream.resolution,
            self.config.stream.encode_to_sd,
            auto_stop_seconds=auto_stop_seconds,
        )

        def start(runtime: CameraRuntime) -> int:
            runtime.client.run_labs_command(command)
            return time.monotonic_ns()

        self._start_attempted = True
        acknowledged, dispatched = self._parallel("同步启动", start, barrier=True)
        for alias, at_ns in dispatched.items():
            self._event(
                alias,
                "start_command_accepted",
                request_monotonic_ns=at_ns,
                response_monotonic_ns=acknowledged[alias],
                camera_auto_stop_seconds=auto_stop_seconds,
            )
        request_values = list(dispatched.values())
        response_values = list(acknowledged.values())
        request_span = (
            (max(request_values) - min(request_values)) / 1_000_000
            if request_values
            else 0.0
        )
        response_span = (
            (max(response_values) - min(response_values)) / 1_000_000
            if response_values
            else 0.0
        )
        return request_span, response_span

    def _wait_publishers(self) -> tuple[float, float]:
        deadline = time.monotonic() + self.config.timeouts.publisher_ready_seconds
        first_seen: dict[str, int] = {}
        while time.monotonic() < deadline:
            if self.server.process is None or self.server.process.poll() is not None:
                raise CaptureError("等待 RTMP 发布期间 MediaMTX 意外退出")
            try:
                paths = self.server.paths()
            except MediaMTXError:
                time.sleep(0.1)
                continue
            now_ns = time.monotonic_ns()
            for runtime in self.runtimes:
                path = f"live/{runtime.camera.stream_key}"
                if bool(paths.get(path, {}).get("online", False)) and runtime.camera.alias not in first_seen:
                    first_seen[runtime.camera.alias] = now_ns
                    self._event(runtime.camera.alias, "publisher_online", path=path)
            if len(first_seen) == len(self.runtimes):
                values = list(first_seen.values())
                return time.monotonic(), (max(values) - min(values)) / 1_000_000
            time.sleep(0.1)
        missing = [
            runtime.camera.alias
            for runtime in self.runtimes
            if runtime.camera.alias not in first_seen
        ]
        raise CaptureError("MediaMTX 未在期限内看到发布流：" + ", ".join(missing))

    def _keepalive_loop(self, runtime: CameraRuntime) -> None:
        warning_active = False
        while not self._keepalive_stop.wait(3.0):
            try:
                with runtime.request_lock:
                    runtime.client.keep_alive()
                warning_active = False
            except BaseException as exc:
                if not warning_active:
                    self._event(
                        runtime.camera.alias,
                        "keep_alive_warning",
                        message=str(exc) or type(exc).__name__,
                    )
                    warning_active = True

    def _start_keepalive(self) -> None:
        self._keepalive_stop.clear()
        for runtime in self.runtimes:
            thread = threading.Thread(
                target=self._keepalive_loop,
                args=(runtime,),
                name=f"gopro-keepalive-{runtime.camera.alias}",
                daemon=True,
            )
            thread.start()
            self._keepalive_threads.append(thread)

    def _start_preview(self) -> None:
        """Create and optionally open the HLS dashboard after all paths are online."""
        if not self.config.preview.enabled:
            return
        dashboard = write_preview_dashboard(
            self.session_dir,
            self.config.cameras,
            self.config.preview,
        )
        urls = {
            camera.alias: hls_url(self.config.preview, camera)
            for camera in self.config.cameras
        }
        opened = False
        if self.config.preview.auto_open:
            opened = open_preview_dashboard(dashboard)
        self._event(
            "system",
            "preview_ready",
            dashboard=str(dashboard.relative_to(self.session_dir)),
            hls_urls=urls,
            browser_opened=opened,
        )
        print(f"实时预览页面：{dashboard.resolve().as_uri()}", flush=True)
        if self.config.preview.auto_open and not opened:
            self._event(
                "system",
                "preview_open_warning",
                message="无法自动打开浏览器，请手动打开上面的实时预览页面",
            )

    def _stop_keepalive(self) -> None:
        self._keepalive_stop.set()
        deadline = time.monotonic() + self.config.timeouts.cohn_request_seconds + 1.0
        for thread in self._keepalive_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._keepalive_threads.clear()

    def _publishers_online(self) -> set[str] | None:
        try:
            paths = self.server.paths()
        except MediaMTXError:
            return None
        return {
            runtime.camera.alias
            for runtime in self.runtimes
            if bool(
                paths.get(f"live/{runtime.camera.stream_key}", {}).get("online", False)
            )
        }

    def _best_effort_parallel(
        self,
        operation: str,
        function: Callable[[CameraRuntime], Any],
    ) -> dict[str, str]:
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(self.runtimes)) as executor:
            futures = {}
            for runtime in self.runtimes:
                def invoke(item: CameraRuntime = runtime) -> Any:
                    with item.request_lock:
                        return function(item)

                futures[executor.submit(invoke)] = runtime
            for future in as_completed(futures):
                runtime = futures[future]
                try:
                    future.result()
                except BaseException as exc:
                    errors[runtime.camera.alias] = str(exc) or type(exc).__name__
        for alias, message in errors.items():
            self._event(alias, f"{operation}_warning", message=message)
        return errors

    def _wait_not_encoding(self, timeout: float) -> None:
        """Require camera-side encoding status 10 to become false for every camera."""
        pending = {runtime.camera.alias for runtime in self.runtimes}
        last_errors: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            with ThreadPoolExecutor(max_workers=len(pending)) as executor:
                futures = {}
                for runtime in self.runtimes:
                    if runtime.camera.alias not in pending:
                        continue

                    def query(item: CameraRuntime = runtime) -> dict[str, Any]:
                        with item.request_lock:
                            return item.client.get_camera_state()

                    futures[executor.submit(query)] = runtime
                for future in as_completed(futures):
                    runtime = futures[future]
                    alias = runtime.camera.alias
                    try:
                        if not _state_flag(future.result(), 10):
                            pending.discard(alias)
                            last_errors.pop(alias, None)
                    except BaseException as exc:
                        last_errors[alias] = str(exc) or type(exc).__name__
            if pending:
                time.sleep(0.25)
        if pending:
            details = "; ".join(
                f"{alias}: {last_errors.get(alias, '仍在编码')}" for alias in sorted(pending)
            )
            raise CaptureError(
                "无法确认相机端编码已停止，请立即在机身上按快门：" + details
            )

    def _stop_cameras(self) -> None:
        # Prevent another keep-alive iteration, but do not wait for all worker
        # threads before asking the cameras to stop: that would extend the clip.
        self._keepalive_stop.set()
        if not self._start_attempted:
            self._stop_keepalive()
            return
        self._event("system", "stop_requested")

        # End the Labs action, then immediately use the standard HTTPS shutter
        # endpoint as the authoritative encoder stop. There must be no observation
        # grace between these two commands, since that would extend the clip.
        self._best_effort_parallel(
            "labs_stop",
            lambda runtime: runtime.client.run_labs_command("!E"),
        )
        self._best_effort_parallel(
            "shutter_stop",
            lambda runtime: runtime.client.stop_shutter(),
        )
        self._stop_keepalive()

        def observe_stop(timeout: float) -> tuple[bool, CaptureError | None]:
            # RTMP going offline alone is insufficient: SD copy can keep encoding.
            with ThreadPoolExecutor(max_workers=2) as executor:
                offline_future = executor.submit(
                    self.server.wait_paths,
                    self._path_names,
                    online=False,
                    timeout=timeout,
                )
                encoding_future = executor.submit(self._wait_not_encoding, timeout)
                offline_result = offline_future.result()
                try:
                    encoding_future.result()
                    encoding_error = None
                except CaptureError as exc:
                    encoding_error = exc
            return offline_result, encoding_error

        offline, encoding_error = observe_stop(
            self.config.timeouts.shutdown_seconds
        )
        if not offline:
            observed = self._publishers_online()
            online = sorted(observed) if observed is not None else []
            raise CaptureError(
                "无法确认以下相机已停止，请立即在机身上按快门："
                + ", ".join(online or [runtime.camera.alias for runtime in self.runtimes])
            )
        if encoding_error is not None:
            raise encoding_error
        for runtime in self.runtimes:
            self._event(runtime.camera.alias, "stopped")

    def _monitor_publishers(self, offline_since: float | None) -> float | None:
        if self.server.process is None or self.server.process.poll() is not None:
            raise CaptureError("录制期间 MediaMTX 意外退出")
        online = self._publishers_online()
        if online is not None and len(online) == len(self.runtimes):
            return None
        offline_since = offline_since or time.monotonic()
        grace = min(5.0, max(1.0, self.config.timeouts.publisher_ready_seconds))
        if time.monotonic() - offline_since > grace:
            raise CaptureError(f"录制期间存在 RTMP 路径离线超过 {grace:.0f} 秒")
        return offline_since

    def _wait_capture(self, duration: float | None) -> None:
        offline_since: float | None = None
        if duration is not None:
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                offline_since = self._monitor_publishers(offline_since)
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            return

        entered = threading.Event()
        input_errors: list[BaseException] = []

        def wait_enter() -> None:
            try:
                input(f"\n正在录制 {len(self.runtimes)} 路。按 Enter 同步停止；也可按 Ctrl+C：")
            except BaseException as exc:
                input_errors.append(exc)
            finally:
                entered.set()

        threading.Thread(target=wait_enter, daemon=True).start()
        while not entered.wait(timeout=0.25):
            offline_since = self._monitor_publishers(offline_since)
        if input_errors:
            raise CaptureError(f"无法读取停止确认：{input_errors[0]}")

    def _verify_recordings(self, requested_duration: float | None) -> list[dict[str, Any]]:
        recordings = inspect_recordings(
            self.session_dir,
            self.config.cameras,
            sha256=self.config.server.sha256,
        )
        missing: list[str] = []
        no_audio: list[str] = []
        too_short: list[str] = []
        for camera in self.config.cameras:
            candidates = [
                item
                for item in recordings
                if item["camera"] == camera.alias and item.get("bytes", 0) > 0
            ]
            valid: list[dict[str, Any]] = []
            for item in candidates:
                probe = item.get("ffprobe", {})
                streams = probe.get("streams", [])
                try:
                    actual = float(probe.get("format", {}).get("duration", 0))
                except (TypeError, ValueError):
                    continue
                if actual > 0 and any(stream.get("codec_type") == "video" for stream in streams):
                    valid.append(item)
            if not valid:
                missing.append(camera.alias)
                continue
            if self.config.stream.require_audio and any(
                not any(
                    stream.get("codec_type") == "audio"
                    for stream in item.get("ffprobe", {}).get("streams", [])
                )
                for item in valid
            ):
                no_audio.append(camera.alias)
            if requested_duration is not None:
                actual_duration = sum(
                    float(item.get("ffprobe", {}).get("format", {}).get("duration", 0))
                    for item in valid
                )
                tolerance = max(2.0, requested_duration * 0.05)
                if actual_duration < max(0.1, requested_duration - tolerance):
                    too_short.append(f"{camera.alias}={actual_duration:.2f}s")
        self.manifest.recordings(recordings)
        if missing:
            raise CaptureError("本机未得到有效录像：" + ", ".join(missing))
        if no_audio:
            raise CaptureError("以下本机录像没有音频轨：" + ", ".join(no_audio))
        if too_short:
            raise CaptureError(
                f"本机录像短于请求时长 {requested_duration:.2f}s：" + ", ".join(too_short)
            )
        return recordings

    def _query_sd_backups(self) -> None:
        if not self.config.stream.encode_to_sd:
            return
        results: dict[str, dict[str, Any]] | None = None
        last_error: CaptureError | None = None
        deadline = time.monotonic() + min(10.0, self.config.timeouts.shutdown_seconds)
        while time.monotonic() < deadline:
            try:
                candidate, _ = self._parallel(
                    "SD 备份路径查询",
                    lambda runtime: runtime.client.get_last_captured_media(),
                )
                results = candidate
                pending = []
                for runtime in self.runtimes:
                    alias = runtime.camera.alias
                    current = self._media_path(results[alias])
                    baseline_known = alias in self._sd_baseline
                    if not current or (
                        baseline_known and current == self._sd_baseline.get(alias)
                    ):
                        pending.append(alias)
                if not pending:
                    break
            except CaptureError as exc:
                last_error = exc
            time.sleep(0.5)
        if results is None:
            raise CaptureError(
                "无法查询 SD 备份路径"
                + (f"：{last_error}" if last_error is not None else "")
            )
        unconfirmed: list[str] = []
        for runtime in self.runtimes:
            response = results[runtime.camera.alias]
            current = self._media_path(response)
            baseline_known = runtime.camera.alias in self._sd_baseline
            changed = bool(current) and (
                not baseline_known or current != self._sd_baseline.get(runtime.camera.alias)
            )
            self._event(
                runtime.camera.alias,
                "sd_backup_reported",
                media_path=current,
                changed_from_before=changed if baseline_known else None,
            )
            if not changed:
                unconfirmed.append(runtime.camera.alias)
        if unconfirmed:
            raise CaptureError(
                "无法确认以下相机生成了新的 SD 备份文件：" + ", ".join(unconfirmed)
            )

    @staticmethod
    def _append_message(existing: str | None, detail: str) -> str:
        if not existing:
            return detail
        return existing if detail in existing else f"{existing}；{detail}"

    def run(self, *, duration: float | None) -> Path:
        """Start immediately, then stop after duration or one Enter press."""
        outcome = "failed"
        message: str | None = None
        try:
            self._event("system", "mediamtx_starting", binary=str(self.server.binary))
            self.server.start()
            self._server_started = True
            self._event(
                "system",
                "mediamtx_ready",
                rtmp_port=self.config.network.rtmp_port,
                api_port=self.config.server.api_port,
            )
            self._preflight()
            self._configure_streams()
            self._snapshot_sd_before()
            if not self.server.wait_paths(
                self._path_names,
                online=False,
                timeout=min(5.0, self.config.timeouts.publisher_ready_seconds),
            ):
                raise CaptureError("启动屏障前目标 RTMP 路径并非全部离线；拒绝复用遗留流")
            self.manifest.status("ready")
            command_span_ms, command_ack_span_ms = self._dispatch_start(duration)
            publishers_online_at, publisher_span_ms = self._wait_publishers()
            self._start_keepalive()
            self.manifest.status(
                "recording",
                started_at_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                camera_count=len(self.runtimes),
                command_dispatch_span_ms=round(command_span_ms, 3),
                command_ack_span_ms=round(command_ack_span_ms, 3),
                publisher_first_seen_span_ms=round(publisher_span_ms, 3),
                sd_backup_requested=self.config.stream.encode_to_sd,
                audio_required=self.config.stream.require_audio,
            )
            self._start_preview()
            print(
                f"{len(self.runtimes)} 路 RTMP 已上线；命令分发跨度 {command_span_ms:.3f} ms，"
                f"HTTP 完成跨度 {command_ack_span_ms:.3f} ms，"
                f"发布流首次观测跨度 {publisher_span_ms:.3f} ms。",
                flush=True,
            )
            # Duration is measured from the authoritative all-publishers-online point.
            remaining = None if duration is None else max(
                0.0,
                duration - (time.monotonic() - publishers_online_at),
            )
            self._wait_capture(remaining)
            outcome = "stopping"
        except KeyboardInterrupt:
            outcome = "interrupted"
            message = "用户按下 Ctrl+C"
            print("\n收到 Ctrl+C，正在停止所有相机并封装文件……", flush=True)
        except BaseException as exc:
            outcome = "failed"
            message = str(exc)
            print(f"\n采集失败：{exc}", file=sys.stderr, flush=True)
        finally:
            try:
                self._stop_cameras()
            except BaseException as exc:
                outcome = "failed"
                message = self._append_message(message, str(exc))
            if self._server_started or self.server.process is not None:
                self.server.stop(timeout=self.config.timeouts.shutdown_seconds)

        recordings: list[dict[str, Any]] = []
        if self._start_attempted:
            try:
                recordings = self._verify_recordings(duration)
            except BaseException as exc:
                outcome = "failed"
                message = self._append_message(message, str(exc))
            try:
                self._query_sd_backups()
            except BaseException as exc:
                outcome = "failed"
                message = self._append_message(message, str(exc))
        if outcome == "stopping":
            outcome = "complete"
        elif outcome == "interrupted" and recordings:
            outcome = "complete_interrupted"
        self.manifest.status(
            outcome,
            finished_at_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            error=message,
        )
        if outcome not in {"complete", "complete_interrupted"}:
            raise CaptureError(message or "采集未完成")
        return self.session_dir
