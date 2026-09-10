from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_multi_rtmp.config import CameraConfig, PreviewConfig
from gopro_multi_rtmp.preview import (
    hls_url,
    open_preview_dashboard,
    write_preview_dashboard,
)


class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.preview = PreviewConfig(
            enabled=True,
            bind_host="127.0.0.1",
            hls_port=8888,
            auto_open=True,
        )
        self.cameras = (
            CameraConfig(alias="cam_a", serial="A0001", stream_key="cam_a"),
            CameraConfig(alias="cam_b", serial="B0002", stream_key="cam_b"),
        )

    def test_hls_url_uses_camera_path_and_muted_autoplay(self) -> None:
        self.assertEqual(
            hls_url(self.preview, self.cameras[0]),
            "http://127.0.0.1:8888/live/cam_a"
            "?muted=true&autoplay=true&controls=true&playsInline=true",
        )

    def test_dashboard_contains_every_camera_without_external_assets(self) -> None:
        path = write_preview_dashboard(self.root, self.cameras, self.preview)
        content = path.read_text(encoding="utf-8")
        self.assertEqual(path, self.root / "runtime" / "preview.html")
        self.assertIn("cam_a", content)
        self.assertIn("cam_b", content)
        self.assertEqual(content.count("<iframe"), 2)
        self.assertNotIn("https://", content)

    def test_browser_open_failure_is_nonfatal(self) -> None:
        dashboard = write_preview_dashboard(self.root, self.cameras, self.preview)
        with patch("gopro_multi_rtmp.preview.webbrowser.open_new_tab", return_value=False):
            self.assertFalse(open_preview_dashboard(dashboard))


if __name__ == "__main__":
    unittest.main()
