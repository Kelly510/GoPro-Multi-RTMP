from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import ssl
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gopro_multi_rtmp.cli import main
from gopro_multi_rtmp.config import load_config
from gopro_multi_rtmp.labs_cohn import (
    LabsCohnClient,
    LabsCohnError,
    RootCACertificate,
    fetch_root_ca,
    labs_persistent_join_command,
    parse_root_ca,
    save_root_ca,
)


TEST_ROOT_CA = b"""-----BEGIN CERTIFICATE-----
MIIBOzCB4qADAgECAgEBMAoGCCqGSM49BAMCMBMxETAPBgNVBAMTCG1lZGlhbXR4
MB4XDTI2MDgyODEzMDgzMloXDTM2MDgyNTEzMDgzMlowEzERMA8GA1UEAxMIbWVk
aWFtdHgwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATpHnSJIabhxUhUMgCiPaKX
aQgRqPMsFjGgWPHulQ9UmBVc77p8K2ekuPKXz594LvSp9sj4uXI4VroO+3vgBrG6
oycwJTAOBgNVHQ8BAf8EBAMCB4AwEwYDVR0lBAwwCgYIKwYBBQUHAwEwCgYIKoZI
zj0EAwIDSAAwRQIhAORZ1L03m2k+8OnPBIjvemXwJAzErovP+oTKZOq7Uj30AiAb
bJpD3MocL65CO0bc9I5tKDJ90WgSzEXxDmHCiS0RmA==
-----END CERTIFICATE-----
"""


CONFIG_TEXT = """
schema_version = 2
[network]
rtmp_host = "192.168.18.2"
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
serial = "C3530000000784"
stream_key = "cam_a"
cohn_ip = "192.168.18.178"
cohn_username = "gopro"
cohn_password = "cohn-secret"
"""


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self.payload[:amount]


class _Opener:
    def __init__(self, payload: object) -> None:
        self.response = _Response(payload)
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def open(self, request: urllib.request.Request, *, timeout: float) -> _Response:
        self.requests.append((request, timeout))
        return self.response


class LabsCohnClientTests(unittest.TestCase):
    def write_config(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config_path = Path(temporary.name) / "config.toml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config_path.chmod(0o600)
        return config_path

    def test_camera_state_uses_direct_https_and_preemptive_basic_auth(self) -> None:
        opener = _Opener({"status": {"8": 0}})
        client = LabsCohnClient(
            "192.168.18.178",
            "gopro",
            "not-for-output",
            timeout=2.5,
            opener=opener,
        )

        self.assertEqual(client.get_camera_state(), {"status": {"8": 0}})
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://192.168.18.178/gopro/camera/state")
        self.assertEqual(timeout, 2.5)
        expected = base64.b64encode(b"gopro:not-for-output").decode("ascii")
        self.assertEqual(request.get_header("Authorization"), f"Basic {expected}")
        self.assertNotIn("not-for-output", repr(client))

    def test_production_opener_disables_proxies_and_uses_https(self) -> None:
        fake_opener = _Opener({})
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        ca_path = Path(temporary.name) / "camera.crt"
        ca_path.write_bytes(TEST_ROOT_CA)
        with patch("gopro_multi_rtmp.labs_cohn.urllib.request.build_opener", return_value=fake_opener) as build:
            LabsCohnClient(
                "192.168.18.178",
                "gopro",
                "secret",
                ca_certificate=ca_path,
            )
        handlers = build.call_args.args
        proxy_handler = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
        https_handler = next(item for item in handlers if isinstance(item, urllib.request.HTTPSHandler))
        self.assertEqual(getattr(proxy_handler, "proxies"), {})
        context = getattr(https_handler, "_context")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        for flag_name in ("VERIFY_X509_STRICT", "VERIFY_X509_PARTIAL_CHAIN"):
            flag = getattr(ssl, flag_name, 0)
            if flag:
                self.assertEqual(context.verify_flags & flag, 0)

    def test_production_client_rejects_missing_ca(self) -> None:
        with self.assertRaisesRegex(LabsCohnError, "Root CA"):
            LabsCohnClient("192.168.18.178", "gopro", "secret")

    def test_root_ca_bootstrap_is_post_without_auth_proxy_or_redirect(self) -> None:
        fake_opener = _Opener(TEST_ROOT_CA)
        with patch(
            "gopro_multi_rtmp.labs_cohn.urllib.request.build_opener",
            return_value=fake_opener,
        ) as build:
            certificate = fetch_root_ca("192.168.18.178", timeout=2.5)

        request, timeout = fake_opener.requests[0]
        self.assertEqual(request.full_url, "https://192.168.18.178/GoProRootCA.crt")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(timeout, 2.5)
        handlers = build.call_args.args
        proxy_handler = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
        https_handler = next(item for item in handlers if isinstance(item, urllib.request.HTTPSHandler))
        redirect_handler = next(
            item for item in handlers if isinstance(item, urllib.request.HTTPRedirectHandler)
        )
        self.assertEqual(getattr(proxy_handler, "proxies"), {})
        self.assertEqual(getattr(https_handler, "_context").verify_mode, ssl.CERT_NONE)
        self.assertEqual(type(redirect_handler).__name__, "_NoRedirect")
        expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(TEST_ROOT_CA.decode())).hexdigest()
        self.assertEqual(certificate.fingerprint_sha256, expected)

    def test_root_ca_parser_rejects_oversized_and_multiple_pem(self) -> None:
        with self.assertRaisesRegex(LabsCohnError, "16 KiB"):
            parse_root_ca(b"x" * (16 * 1024 + 1))
        with self.assertRaisesRegex(LabsCohnError, "exactly one"):
            parse_root_ca(TEST_ROOT_CA + TEST_ROOT_CA)

    def test_root_ca_save_is_atomic_private_and_never_replaces_different(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / ".cohn-ca" / "cam_a.crt"
        certificate = parse_root_ca(TEST_ROOT_CA)
        self.assertTrue(save_root_ca(certificate, target))
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertFalse(save_root_ca(certificate, target))

        different = RootCACertificate(
            pem=certificate.pem,
            fingerprint_sha256="0" * 64,
        )
        with self.assertRaisesRegex(LabsCohnError, "refusing to overwrite"):
            save_root_ca(different, target)
        self.assertEqual(target.read_bytes(), certificate.pem)

    def test_labs_command_is_encoded_as_one_query_value(self) -> None:
        opener = _Opener({"code": 0})
        client = LabsCohnClient("192.168.18.178", "gopro", "secret", opener=opener)
        self.assertEqual(client.run_labs_command("!GLC RTMP=hello world&x=1"), {"code": 0})
        request, _ = opener.requests[0]
        parsed = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(parsed.path, "/gopro/qrcode")
        self.assertEqual(
            urllib.parse.parse_qs(parsed.query),
            {"labs": ["1"], "code": ["!GLC RTMP=hello world&x=1"]},
        )

    def test_empty_labs_response_is_a_successful_acknowledgement(self) -> None:
        opener = _Opener(b"")
        client = LabsCohnClient("192.168.18.178", "gopro", "secret", opener=opener)
        self.assertIsNone(client.run_labs_command("!GLC"))

    def test_labs_command_limit_matches_hero13_firmware(self) -> None:
        client = LabsCohnClient("192.168.18.178", "gopro", "secret", opener=_Opener(b""))
        self.assertIsNone(client.run_labs_command("x" * 400))
        with self.assertRaisesRegex(ValueError, "400"):
            client.run_labs_command("x" * 401)

    def test_persistent_join_command_is_bounded_and_rejects_unsafe_delimiters(self) -> None:
        self.assertEqual(
            labs_persistent_join_command("lab-5g", "wifi-test-secret"),
            '!MJOIN="lab-5g:wifi-test-secret"',
        )
        for ssid, password in (
            ("", "secret"),
            ("bad:ssid", "secret"),
            ('bad"ssid', "secret"),
            ("lab", ""),
            ("lab", 'bad"password'),
            ("lab", "bad:password"),
            ("lab", "bad\npassword"),
        ):
            with self.subTest(ssid=ssid, has_password=bool(password)):
                with self.assertRaises(ValueError):
                    labs_persistent_join_command(ssid, password)

    def test_info_keepalive_and_shutter_stop_endpoints(self) -> None:
        opener = _Opener({"serial_number": "C3530000000784"})
        client = LabsCohnClient("192.168.18.178", "gopro", "secret", opener=opener)
        client.get_camera_info()
        client.keep_alive()
        client.stop_shutter()
        self.assertEqual(
            [urllib.parse.urlsplit(item[0].full_url).path for item in opener.requests],
            [
                "/gopro/camera/info",
                "/gopro/camera/keep_alive",
                "/gopro/camera/shutter/stop",
            ],
        )

    def test_network_errors_do_not_expose_password_or_labs_code(self) -> None:
        class FailedOpener:
            def open(self, request: urllib.request.Request, *, timeout: float) -> _Response:
                raise urllib.error.URLError("offline")

        client = LabsCohnClient(
            "192.168.18.178",
            "gopro",
            "top-secret",
            opener=FailedOpener(),
        )
        with self.assertRaises(LabsCohnError) as caught:
            client.run_labs_command("*RTMP=contains-sensitive-data")
        message = str(caught.exception)
        self.assertNotIn("top-secret", message)
        self.assertNotIn("contains-sensitive-data", message)

    def test_remote_close_reports_tls_success_and_shps_hint_without_secret(self) -> None:
        class ClosedOpener:
            def open(self, request: urllib.request.Request, *, timeout: float) -> _Response:
                raise http.client.RemoteDisconnected("sensitive-low-level-detail")

        client = LabsCohnClient(
            "192.168.18.178",
            "gopro",
            "top-secret",
            opener=ClosedOpener(),
        )
        with self.assertRaises(LabsCohnError) as caught:
            client.get_camera_info()
        message = str(caught.exception)
        self.assertIn("TLS connected", message)
        self.assertIn("SHPS", message)
        self.assertNotIn("top-secret", message)
        self.assertNotIn("sensitive-low-level-detail", message)

    def test_probe_is_read_only_and_redacts_response_secrets(self) -> None:
        config_path = self.write_config()
        ca_path = config_path.parent / ".cohn-ca" / "cam_a.crt"
        ca_path.parent.mkdir(mode=0o700)
        ca_path.write_bytes(TEST_ROOT_CA)
        ca_path.chmod(0o600)

        output = io.StringIO()
        with patch("gopro_multi_rtmp.cli.LabsCohnClient") as client_class:
            client_class.return_value.get_camera_info.return_value = {
                "serial_number": "C3530000000784",
                "model_name": "HERO13 Black",
            }
            client_class.return_value.get_camera_state.return_value = {
                "status": {"8": 0},
                "password": "response-secret",
            }
            with redirect_stdout(output):
                result = main(["--config", str(config_path), "probe", "--json"])

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        client_class.return_value.get_camera_info.assert_called_once_with()
        client_class.return_value.get_camera_state.assert_called_once_with()
        self.assertIn('"ok": true', rendered)
        self.assertIn("<redacted>", rendered)
        self.assertNotIn("cohn-secret", rendered)
        self.assertNotIn("response-secret", rendered)

    def test_probe_refuses_authentication_until_ca_is_enrolled(self) -> None:
        config_path = self.write_config()
        with patch("gopro_multi_rtmp.cli.LabsCohnClient") as client_class:
            result = main(["--config", str(config_path), "probe", "--camera", "cam_a"])
        self.assertEqual(result, 2)
        client_class.assert_not_called()

    def test_enroll_without_acceptance_only_prints_fingerprint(self) -> None:
        config_path = self.write_config()
        certificate = parse_root_ca(TEST_ROOT_CA)
        output = io.StringIO()
        with (
            patch("gopro_multi_rtmp.cli.fetch_root_ca", return_value=certificate),
            patch("gopro_multi_rtmp.cli.LabsCohnClient") as client_class,
            redirect_stdout(output),
        ):
            result = main(
                ["--config", str(config_path), "enroll", "--camera", "cam_a"]
            )

        self.assertEqual(result, 2)
        self.assertIn(certificate.fingerprint_sha256, output.getvalue())
        self.assertFalse((config_path.parent / ".cohn-ca" / "cam_a.crt").exists())
        client_class.assert_not_called()

    def test_enroll_accepts_saves_then_pins_and_checks_full_serial(self) -> None:
        config_path = self.write_config()
        target = config_path.parent / ".cohn-ca" / "cam_a.crt"
        certificate = parse_root_ca(TEST_ROOT_CA)
        with (
            patch("gopro_multi_rtmp.cli.fetch_root_ca", return_value=certificate),
            patch("gopro_multi_rtmp.cli.LabsCohnClient") as client_class,
        ):
            client_class.return_value.get_camera_info.return_value = {
                "serial_number": "C3530000000784"
            }
            result = main(
                [
                    "--config",
                    str(config_path),
                    "enroll",
                    "--camera",
                    "cam_a",
                    "--accept-first-use",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(target.read_bytes(), certificate.pem)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(load_config(config_path).cameras[0].cohn_ca_cert, target.resolve())
        self.assertEqual(client_class.call_args.kwargs["ca_certificate"], target.resolve())
        client_class.return_value.get_camera_info.assert_called_once_with()

    def test_enroll_serial_mismatch_removes_only_new_file(self) -> None:
        config_path = self.write_config()
        target = config_path.parent / ".cohn-ca" / "cam_a.crt"
        certificate = parse_root_ca(TEST_ROOT_CA)
        with (
            patch("gopro_multi_rtmp.cli.fetch_root_ca", return_value=certificate),
            patch("gopro_multi_rtmp.cli.LabsCohnClient") as client_class,
        ):
            client_class.return_value.get_camera_info.return_value = {
                "serial_number": "C3530000000784-extra"
            }
            result = main(
                [
                    "--config",
                    str(config_path),
                    "enroll",
                    "--camera",
                    "cam_a",
                    "--accept-first-use",
                ]
            )

        self.assertEqual(result, 2)
        self.assertFalse(target.exists())

    def test_stream_network_provision_preview_has_no_secret_or_camera_request(self) -> None:
        config_path = self.write_config()
        ca_path = config_path.parent / ".cohn-ca" / "cam_a.crt"
        ca_path.parent.mkdir(mode=0o700)
        ca_path.write_bytes(TEST_ROOT_CA)
        ca_path.chmod(0o600)
        output = io.StringIO()
        with (
            patch("gopro_multi_rtmp.cli.LabsCohnClient") as client_class,
            patch("gopro_multi_rtmp.cli.getpass.getpass") as password_prompt,
            redirect_stdout(output),
        ):
            result = main(
                [
                    "--config",
                    str(config_path),
                    "provision-stream-network",
                    "--camera",
                    "cam_a",
                    "--ssid",
                    "lab-5g",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("--apply", output.getvalue())
        client_class.assert_not_called()
        password_prompt.assert_not_called()

    def test_stream_network_provision_prompts_once_and_never_persists_secret(self) -> None:
        config_path = self.write_config()
        ca_path = config_path.parent / ".cohn-ca" / "cam_a.crt"
        ca_path.parent.mkdir(mode=0o700)
        ca_path.write_bytes(TEST_ROOT_CA)
        ca_path.chmod(0o600)
        output = io.StringIO()
        wifi_secret = "wifi-super-secret"
        with (
            patch("gopro_multi_rtmp.cli.LabsCohnClient") as client_class,
            patch("gopro_multi_rtmp.cli.getpass.getpass", return_value=wifi_secret) as prompt,
            redirect_stdout(output),
        ):
            client_class.return_value.get_camera_info.return_value = {
                "serial_number": "C3530000000784"
            }
            client_class.return_value.get_camera_state.return_value = {
                "status": {"8": 0, "10": 0}
            }
            result = main(
                [
                    "--config",
                    str(config_path),
                    "provision-stream-network",
                    "--camera",
                    "cam_a",
                    "--ssid",
                    "lab-5g",
                    "--apply",
                ]
            )

        self.assertEqual(result, 0)
        prompt.assert_called_once_with("Wi-Fi 密码（输入不显示、不保存）：")
        client_class.return_value.run_labs_command.assert_called_once_with(
            '!MJOIN="lab-5g:wifi-super-secret"'
        )
        self.assertNotIn(wifi_secret, output.getvalue())
        self.assertNotIn(wifi_secret, config_path.read_text(encoding="utf-8"))

    def test_stream_network_provision_refuses_identity_mismatch_before_write(self) -> None:
        config_path = self.write_config()
        ca_path = config_path.parent / ".cohn-ca" / "cam_a.crt"
        ca_path.parent.mkdir(mode=0o700)
        ca_path.write_bytes(TEST_ROOT_CA)
        ca_path.chmod(0o600)
        errors = io.StringIO()
        with (
            patch("gopro_multi_rtmp.cli.LabsCohnClient") as client_class,
            patch("gopro_multi_rtmp.cli.getpass.getpass", return_value="wifi-secret"),
            redirect_stderr(errors),
        ):
            client_class.return_value.get_camera_info.return_value = {
                "serial_number": "WRONG-CAMERA"
            }
            result = main(
                [
                    "--config",
                    str(config_path),
                    "provision-stream-network",
                    "--camera",
                    "cam_a",
                    "--ssid",
                    "lab-5g",
                    "--apply",
                ]
            )

        self.assertEqual(result, 2)
        client_class.return_value.run_labs_command.assert_not_called()
        self.assertIn("序列号不符", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
