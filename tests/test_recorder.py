from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gopro_multi_rtmp.config import CameraConfig
from gopro_multi_rtmp.mediamtx import MediaMTXError
from gopro_multi_rtmp.recorder import (
    CameraRuntime,
    CaptureError,
    CaptureSession,
    labs_rtmp_target_command,
    labs_start_command,
)


class _Client:
    def __init__(self, serial: str = "C3530000000001") -> None:
        self.serial = serial
        self.commands: list[str] = []
        self.encoding = False
        self.stop_calls = 0

    def keep_alive(self) -> None:
        return None

    def get_camera_info(self) -> dict[str, str]:
        return {
            "serial_number": self.serial,
            "model_name": "HERO13 Black",
            "firmware_version": "HD13.02.10.70",
        }

    def get_camera_state(self) -> dict[str, object]:
        return {"status": {"8": 0, "10": int(self.encoding)}}

    def run_labs_command(self, code: str) -> None:
        self.commands.append(code)
        return None

    def stop_shutter(self) -> None:
        self.stop_calls += 1
        self.encoding = False
        return None


def _runtime(alias: str, serial: str, key: str, ip: str, client: _Client) -> CameraRuntime:
    return CameraRuntime(
        CameraConfig(
            alias=alias,
            serial=serial,
            stream_key=key,
            cohn_ip=ip,
            cohn_username="gopro",
            cohn_password="secret",
        ),
        client,  # type: ignore[arg-type]
        threading.Lock(),
    )


class LabsCommandTests(unittest.TestCase):
    def test_official_live_stream_commands(self) -> None:
        self.assertEqual(labs_start_command(480, False), "!GS")
        self.assertEqual(labs_start_command(720, True), "!GMC")
        self.assertEqual(labs_start_command(1080, True), "!GLC")
        self.assertEqual(labs_start_command(1080, False), "!GL")
        self.assertEqual(
            labs_start_command(1080, True, auto_stop_seconds=21),
            "!GLC!21E",
        )
        with self.assertRaises(ValueError):
            labs_start_command(1080, True, auto_stop_seconds=86_401)

    def test_official_persistent_rtmp_command(self) -> None:
        self.assertEqual(
            labs_rtmp_target_command("rtmp://192.168.18.2:1935/live/cam_a"),
            '!MRTMP="rtmp://192.168.18.2:1935/live/cam_a"',
        )
        with self.assertRaises(ValueError):
            labs_rtmp_target_command('rtmp://host/live/bad"value')


class RecorderTests(unittest.TestCase):
    def make_session(self, *, require_audio: bool = True) -> CaptureSession:
        session = CaptureSession.__new__(CaptureSession)
        clients = (_Client("A0001"), _Client("B0002"))
        session.runtimes = (
            _runtime("cam_a", "A0001", "cam_a", "192.168.18.178", clients[0]),
            _runtime("cam_b", "B0002", "cam_b", "192.168.18.179", clients[1]),
        )
        session.resolved_host = "192.168.18.2"
        session.config = SimpleNamespace(
            network=SimpleNamespace(rtmp_port=1935),
            stream=SimpleNamespace(
                resolution=1080,
                encode_to_sd=True,
                require_audio=require_audio,
            ),
            timeouts=SimpleNamespace(
                cohn_request_seconds=1.0,
                publisher_ready_seconds=2.0,
                shutdown_seconds=2.0,
            ),
            server=SimpleNamespace(sha256=False),
            cameras=tuple(runtime.camera for runtime in session.runtimes),
        )
        session._start_attempted = False
        session._keepalive_stop = threading.Event()
        session._keepalive_threads = []
        session._manifest_lock = threading.Lock()
        session._event = lambda *args, **kwargs: None  # type: ignore[method-assign]
        return session

    def test_preflight_checks_each_camera_serial(self) -> None:
        session = self.make_session()
        session._preflight()
        session.runtimes[1].client.serial = "WRONG"  # type: ignore[attr-defined]
        with self.assertRaisesRegex(CaptureError, "序列号"):
            session._preflight()

    def test_preflight_rejects_stale_encoding(self) -> None:
        session = self.make_session()
        session.runtimes[0].client.get_camera_state = (  # type: ignore[method-assign]
            lambda: {"status": {"8": 0, "10": 1}}
        )
        with self.assertRaisesRegex(CaptureError, "遗留流"):
            session._preflight()

    def test_configure_sets_audio_and_per_camera_rtmp_targets(self) -> None:
        session = self.make_session(require_audio=True)
        with patch("gopro_multi_rtmp.recorder.time.sleep"):
            session._configure_streams()
        self.assertEqual(
            session.runtimes[0].client.commands,  # type: ignore[attr-defined]
            ["$DAUD=0", '!MRTMP="rtmp://192.168.18.2:1935/live/cam_a"'],
        )
        self.assertEqual(
            session.runtimes[1].client.commands,  # type: ignore[attr-defined]
            ["$DAUD=0", '!MRTMP="rtmp://192.168.18.2:1935/live/cam_b"'],
        )

    def test_audio_is_not_modified_when_not_required(self) -> None:
        session = self.make_session(require_audio=False)
        with patch("gopro_multi_rtmp.recorder.time.sleep"):
            session._configure_streams()
        self.assertEqual(
            session.runtimes[0].client.commands,  # type: ignore[attr-defined]
            ['!MRTMP="rtmp://192.168.18.2:1935/live/cam_a"'],
        )

    def test_start_dispatch_uses_one_parallel_barrier(self) -> None:
        session = self.make_session()
        dispatch_span, ack_span = session._dispatch_start(None)
        self.assertGreaterEqual(dispatch_span, 0)
        self.assertGreaterEqual(ack_span, 0)
        self.assertTrue(session._start_attempted)
        for runtime in session.runtimes:
            self.assertEqual(runtime.client.commands, ["!GLC"])  # type: ignore[attr-defined]

    def test_finite_recording_adds_camera_side_failsafe(self) -> None:
        session = self.make_session()
        session._dispatch_start(1.1)
        for runtime in session.runtimes:
            self.assertEqual(runtime.client.commands, ["!GLC!14E"])  # type: ignore[attr-defined]

    def test_media_api_failure_is_not_treated_as_online(self) -> None:
        session = self.make_session()
        session.server = SimpleNamespace(paths=lambda: (_ for _ in ()).throw(MediaMTXError("down")))
        self.assertIsNone(session._publishers_online())

    def test_sd_media_path_supports_official_folder_and_file_shape(self) -> None:
        self.assertEqual(
            CaptureSession._media_path(
                {"folder": "100GOPRO", "file": "GH010001.MP4"}
            ),
            "100GOPRO/GH010001.MP4",
        )
        self.assertEqual(
            CaptureSession._media_path({"path": "100GOPRO/GH010002.MP4"}),
            "100GOPRO/GH010002.MP4",
        )

    def test_recording_verification_requires_audio_track(self) -> None:
        session = self.make_session(require_audio=True)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        session.session_dir = Path(temporary.name)
        session.manifest = SimpleNamespace(recordings=lambda values: None)
        video_only = [
            {
                "camera": camera.alias,
                "bytes": 100,
                "ffprobe": {
                    "format": {"duration": "4.0"},
                    "streams": [{"codec_type": "video", "codec_name": "h264"}],
                },
            }
            for camera in session.config.cameras
        ]
        with patch("gopro_multi_rtmp.recorder.inspect_recordings", return_value=video_only):
            with self.assertRaisesRegex(CaptureError, "没有音频轨"):
                session._verify_recordings(4.0)

    def test_offline_publisher_does_not_fake_camera_side_stop(self) -> None:
        session = self.make_session()
        session._start_attempted = True
        session.config.timeouts.shutdown_seconds = 0.01
        session._stop_keepalive = lambda: None  # type: ignore[method-assign]
        session.server = SimpleNamespace(
            wait_paths=lambda *args, **kwargs: True,
            paths=lambda: {},
        )
        for runtime in session.runtimes:
            runtime.client.get_camera_state = (  # type: ignore[method-assign]
                lambda: (_ for _ in ()).throw(OSError("unreachable"))
            )
        with self.assertRaisesRegex(CaptureError, "机身上按快门"):
            session._stop_cameras()

    def test_stop_uses_shutter_before_waiting_for_offline_publisher(self) -> None:
        session = self.make_session()
        session._start_attempted = True
        session.config.timeouts.shutdown_seconds = 0.02
        session._stop_keepalive = lambda: None  # type: ignore[method-assign]

        def wait_paths(*args: object, **kwargs: object) -> bool:
            self.assertTrue(
                all(
                    runtime.client.stop_calls == 1  # type: ignore[attr-defined]
                    for runtime in session.runtimes
                ),
                "publisher observation must not precede immediate shutter-stop",
            )
            return True

        session.server = SimpleNamespace(
            wait_paths=wait_paths,
            paths=lambda: {},
        )
        for runtime in session.runtimes:
            runtime.client.encoding = True  # type: ignore[attr-defined]

        session._stop_cameras()

        for runtime in session.runtimes:
            self.assertEqual(runtime.client.stop_calls, 1)  # type: ignore[attr-defined]
            self.assertFalse(runtime.client.encoding)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
