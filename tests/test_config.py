from __future__ import annotations

import tempfile
import unittest
import re
from pathlib import Path

from gopro_multi_rtmp.config import (
    ConfigError,
    load_config,
    public_config,
    select_cameras,
    validate_config,
)


VALID = """
schema_version = 2
[network]
rtmp_host = "192.168.10.5"
rtmp_port = 1935
[stream]
resolution = 1080
encode_to_sd = true
require_audio = true
[server]
mediamtx_binary = "auto"
output_root = "./recordings"
api_port = 9997
record_part_duration = "1s"
record_segment_duration = "1h"
sha256 = false
[timeouts]
cohn_request_seconds = 5
publisher_ready_seconds = 45
shutdown_seconds = 30
[[cameras]]
alias = "cam_a"
serial = "C3530000000001"
stream_key = "cam_a"
cohn_ip = "192.168.18.178"
cohn_username = "gopro"
cohn_password = "cohn-secret-a"
[[cameras]]
alias = "cam_b"
serial = "C3530000000002"
stream_key = "cam_b"
cohn_ip = "192.168.18.179"
cohn_username = "gopro"
cohn_password = "cohn-secret-b"
"""


class ConfigTests(unittest.TestCase):
    def write_config(self, text: str, *, with_managed_ca: bool = True) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        if with_managed_ca:
            ca_directory = path.parent / ".cohn-ca"
            ca_directory.mkdir(mode=0o700)
            for alias in re.findall(r'^alias = "([A-Za-z0-9_-]+)"$', text, re.MULTILINE):
                ca_path = ca_directory / f"{alias}.crt"
                ca_path.write_text("test trust anchor", encoding="utf-8")
                ca_path.chmod(0o600)
        return path

    def test_valid_hardware_config_never_exports_credentials(self) -> None:
        config = load_config(self.write_config(VALID))
        validate_config(config, hardware=True)
        snapshot = public_config(config, "192.168.10.5")
        self.assertTrue(snapshot["stream"]["require_audio"])
        rendered = repr(snapshot)
        self.assertNotIn("cohn-secret", rendered)
        self.assertNotIn("cohn_username", rendered)
        self.assertNotIn("cohn_password", rendered)
        self.assertNotIn("cohn-secret-a", repr(config.cameras[0]))

    def test_selected_complete_camera_works_while_other_is_incomplete(self) -> None:
        incomplete = VALID.replace(
            'cohn_ip = "192.168.18.179"\ncohn_username = "gopro"\ncohn_password = "cohn-secret-b"\n',
            "",
        )
        config = load_config(self.write_config(incomplete))
        selected = select_cameras(config, ["cam_a"])
        validate_config(selected, hardware=True)
        self.assertEqual([camera.alias for camera in selected.cameras], ["cam_a"])
        with self.assertRaisesRegex(ConfigError, "cam_b"):
            validate_config(select_cameras(config, None), hardware=True)

    def test_partial_nonselected_camera_does_not_block_complete_camera(self) -> None:
        text = VALID.replace('cohn_password = "cohn-secret-b"\n', "")
        config = load_config(self.write_config(text))
        validate_config(select_cameras(config, ["cam_a"]), hardware=True)
        with self.assertRaisesRegex(ConfigError, "cam_b"):
            validate_config(select_cameras(config, ["cam_b"]), hardware=True)

    def test_enabled_false_is_excluded_by_default(self) -> None:
        text = VALID.replace('alias = "cam_b"', 'alias = "cam_b"\nenabled = false')
        config = load_config(self.write_config(text))
        selected = select_cameras(config, None)
        self.assertEqual([camera.alias for camera in selected.cameras], ["cam_a"])
        with self.assertRaisesRegex(ConfigError, "enabled=false"):
            select_cameras(config, ["cam_b"])

    def test_repeated_camera_selection_is_deduplicated_in_requested_order(self) -> None:
        config = load_config(self.write_config(VALID))
        selected = select_cameras(config, ["cam_b", "cam_a", "cam_b"])
        self.assertEqual([camera.alias for camera in selected.cameras], ["cam_b", "cam_a"])

    def test_unknown_camera_is_rejected(self) -> None:
        config = load_config(self.write_config(VALID))
        with self.assertRaisesRegex(ConfigError, "cam_z"):
            select_cameras(config, ["cam_z"])

    def test_unknown_config_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"\[network\].*ssid"):
            load_config(
                self.write_config(
                    VALID.replace(
                        'rtmp_port = 1935',
                        'rtmp_port = 1935\nssid = "unexpected"',
                    )
                )
            )
        with self.assertRaisesRegex(ConfigError, r"\[\[cameras\]\].*fov"):
            load_config(
                self.write_config(
                    VALID.replace(
                        'alias = "cam_a"',
                        'alias = "cam_a"\nfov = "wide"',
                    )
                )
            )

    def test_config_schema_version_is_current(self) -> None:
        with self.assertRaisesRegex(ConfigError, "schema_version 必须为 2"):
            load_config(self.write_config(VALID.replace("schema_version = 2", "schema_version = 1")))

    def test_camera_count_is_variable(self) -> None:
        third = """
[[cameras]]
alias = "cam_c"
serial = "C3530000000003"
stream_key = "cam_c"
cohn_ip = "192.168.18.180"
cohn_username = "gopro"
cohn_password = "cohn-secret-c"
"""
        config = load_config(self.write_config(VALID + third))
        validate_config(config, hardware=True)
        self.assertEqual(len(config.cameras), 3)

    def test_duplicate_stream_keys_and_ips_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "stream_key"):
            load_config(self.write_config(VALID.replace('stream_key = "cam_b"', 'stream_key = "cam_a"')))
        with self.assertRaisesRegex(ConfigError, "cohn_ip"):
            load_config(self.write_config(VALID.replace("192.168.18.179", "192.168.18.178")))

    def test_case_only_alias_collision_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "alias"):
            load_config(self.write_config(VALID.replace('alias = "cam_b"', 'alias = "CAM_A"')))

    def test_resolution_is_limited_to_labs_live_stream_values(self) -> None:
        with self.assertRaisesRegex(ConfigError, "480、720 或 1080"):
            load_config(self.write_config(VALID.replace("resolution = 1080", "resolution = 4")))

    def test_ipv6_and_loopback_rtmp_hosts_are_rejected_for_hardware(self) -> None:
        ipv6 = load_config(self.write_config(VALID.replace("192.168.10.5", "2001:db8::1")))
        with self.assertRaisesRegex(ConfigError, "IPv4"):
            validate_config(ipv6, hardware=True)
        loopback = load_config(self.write_config(VALID.replace("192.168.10.5", "127.0.0.1")))
        with self.assertRaisesRegex(ConfigError, "回环"):
            validate_config(loopback, hardware=True)

    def test_placeholder_host_is_not_hardware_ready(self) -> None:
        config = load_config(self.write_config(VALID.replace("192.168.10.5", "CHANGE_ME_HOST")))
        with self.assertRaisesRegex(ConfigError, "占位值"):
            validate_config(config, hardware=True)

    def test_managed_ca_is_auto_loaded_and_required_for_hardware(self) -> None:
        path = self.write_config(VALID)
        config = load_config(path)
        self.assertEqual(
            config.cameras[0].cohn_ca_cert,
            (path.parent / ".cohn-ca" / "cam_a.crt").resolve(),
        )

        without_ca = load_config(self.write_config(VALID, with_managed_ca=False))
        with self.assertRaisesRegex(ConfigError, "Root CA"):
            validate_config(without_ca, hardware=True)

    def test_cohn_ip_rejects_public_and_reserved_destinations(self) -> None:
        for unsafe in ("8.8.8.8", "192.0.2.1", "255.255.255.255"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ConfigError, "私有或 link-local"):
                    load_config(
                        self.write_config(
                            VALID.replace("192.168.18.178", unsafe),
                        )
                    )


if __name__ == "__main__":
    unittest.main()
