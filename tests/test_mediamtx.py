from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_multi_rtmp.config import load_config, public_config
from gopro_multi_rtmp.manifest import Manifest
from gopro_multi_rtmp.mediamtx import MediaMTXError, MediaMTXRunner, render_mediamtx_config

from test_config import VALID


class MediaMTXTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config_path = self.root / "config.toml"
        config_path.write_text(VALID, encoding="utf-8")
        self.config = load_config(config_path)

    def test_renders_all_fixed_recording_paths_without_auto_delete(self) -> None:
        text = render_mediamtx_config(
            self.root / "session",
            self.config.network.rtmp_port,
            self.config.server.api_port,
            self.config.cameras,
            self.config.server,
        )
        self.assertIn('"live/cam_a":', text)
        self.assertIn('"live/cam_b":', text)
        self.assertEqual(text.count("record: true"), 2)
        self.assertEqual(text.count("recordDeleteAfter: 0s"), 1)
        self.assertIn("streams/%path", text)
        self.assertIn("recordFormat: fmp4", text)
        self.assertIn("moq: false", text)

    def test_manifest_never_contains_secret_snapshot(self) -> None:
        manifest = Manifest(
            self.root / "manifest.json",
            "session",
            public_config(self.config, "192.168.10.5"),
        )
        manifest.event("system", "ready")
        content = (self.root / "manifest.json").read_text(encoding="utf-8")
        self.assertIn('"status": "initializing"', content)
        self.assertNotIn("cohn-secret-a", content)
        self.assertNotIn("cohn-secret-b", content)
        self.assertNotIn("cohn_password", content)
        self.assertNotIn("cohn_username", content)

    def test_start_timeout_cleans_partial_process_and_log_handle(self) -> None:
        class Process:
            returncode = None

            def __init__(self) -> None:
                self.signals: list[int] = []

            def poll(self) -> int | None:
                return self.returncode

            def send_signal(self, value: int) -> None:
                self.signals.append(value)

            def wait(self, timeout: float) -> int:
                self.returncode = 0
                return 0

        process = Process()
        runner = MediaMTXRunner(
            Path("/not-executed/mediamtx"),
            self.root / "session",
            self.config.network.rtmp_port,
            self.config.server.api_port,
            self.config.cameras,
            self.config.server,
        )
        with patch("gopro_multi_rtmp.mediamtx.subprocess.Popen", return_value=process):
            with self.assertRaises(MediaMTXError):
                runner.start(timeout=0)

        self.assertTrue(process.signals)
        self.assertIsNone(runner.process)
        self.assertIsNone(runner._log_handle)


if __name__ == "__main__":
    unittest.main()
