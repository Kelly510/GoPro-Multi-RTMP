"""Small, dependency-free client for GoPro Labs commands over COHN HTTPS."""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import __version__


class LabsCohnError(RuntimeError):
    """Raised when a COHN request cannot be completed safely."""


@dataclass(frozen=True)
class RootCACertificate:
    """One parsed COHN Root CA and its standard DER SHA-256 fingerprint."""

    pem: bytes = field(repr=False)
    fingerprint_sha256: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a COHN Authorization header to a redirect target."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class _Response(Protocol):
    status: int

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...

    def read(self, amount: int = -1) -> bytes: ...


class _Opener(Protocol):
    def open(self, request: urllib.request.Request, *, timeout: float) -> _Response: ...


_COHN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
_PEM_END = b"-----END CERTIFICATE-----"
_MAX_ROOT_CA_BYTES = 16 * 1024


def labs_persistent_join_command(ssid: str, password: str) -> str:
    """Build the official persistent Labs JOIN metadata command safely.

    The password is deliberately accepted only as an in-memory argument; callers
    must not place it in argv, config snapshots, logs, or exception messages.
    """
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValueError("Wi-Fi SSID must contain 1 to 32 UTF-8 bytes")
    if not password:
        raise ValueError("Wi-Fi password must not be empty")
    if ":" in ssid or ":" in password:
        raise ValueError(
            "Wi-Fi SSID/password cannot contain ':' because Labs JOIN escaping is undocumented"
        )
    if any(
        character == '"' or ord(character) < 32 or ord(character) == 127
        for character in ssid
    ):
        raise ValueError("Wi-Fi SSID contains a character unsafe for Labs JOIN metadata")
    if any(
        character == '"' or ord(character) < 32 or ord(character) == 127
        for character in password
    ):
        raise ValueError("Wi-Fi password contains a character unsafe for Labs JOIN metadata")
    command = f'!MJOIN="{ssid}:{password}"'
    if len(command) > 400:
        raise ValueError("Wi-Fi JOIN metadata exceeds the HERO13 Labs 400-character limit")
    return command


def _cohn_ipv4(ip_address: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(ip_address)
    except ValueError as exc:
        raise ValueError("COHN address must be a valid IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("COHN address must be IPv4")
    if not any(address in network for network in _COHN_NETWORKS):
        raise ValueError("COHN address must be private or IPv4 link-local")
    return address


def parse_root_ca(payload: bytes) -> RootCACertificate:
    """Validate a bounded, single-certificate PEM with Python's SSL parser."""
    if not payload:
        raise LabsCohnError("COHN Root CA response was empty")
    if len(payload) > _MAX_ROOT_CA_BYTES:
        raise LabsCohnError("COHN Root CA response exceeded 16 KiB")

    pem = payload.strip()
    if (
        pem.count(_PEM_BEGIN) != 1
        or pem.count(_PEM_END) != 1
        or re.fullmatch(
            rb"-----BEGIN CERTIFICATE-----[\r\n]+[A-Za-z0-9+/=\r\n]+"
            rb"-----END CERTIFICATE-----",
            pem,
        )
        is None
    ):
        raise LabsCohnError("COHN Root CA response was not exactly one PEM certificate")
    try:
        text = pem.decode("ascii")
        der = ssl.PEM_cert_to_DER_cert(text)
        # Loading the PEM into an SSL trust store catches structures that merely
        # resemble a base64 certificate block but cannot be used as a CA input.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=text)
    except (UnicodeDecodeError, ValueError, ssl.SSLError):
        raise LabsCohnError("COHN Root CA response was not a parseable certificate") from None
    return RootCACertificate(
        pem=pem + b"\n",
        fingerprint_sha256=hashlib.sha256(der).hexdigest(),
    )


def _build_bootstrap_opener() -> urllib.request.OpenerDirector:
    """Build the deliberately unauthenticated TOFU bootstrap transport."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )


def fetch_root_ca(
    ip_address: str,
    *,
    timeout: float = 5.0,
    opener: _Opener | None = None,
) -> RootCACertificate:
    """Fetch the camera Root CA without credentials for explicit TOFU enrollment.

    This is the only production path that disables TLS verification. It sends no
    Authorization header, disables environment proxies, rejects redirects, limits
    the response to 16 KiB, and validates the result before returning it.
    """
    address = _cohn_ipv4(ip_address)
    if timeout <= 0:
        raise ValueError("COHN timeout must be greater than zero")
    direct_opener = opener or _build_bootstrap_opener()
    request = urllib.request.Request(
        f"https://{address}/GoProRootCA.crt",
        data=b"",
        method="POST",
        headers={
            "Accept": "application/x-pem-file",
            "Connection": "close",
            "User-Agent": f"gopro-multi-rtmp/{__version__}",
        },
    )
    try:
        with direct_opener.open(request, timeout=float(timeout)) as response:
            if response.status != 200:
                raise LabsCohnError(
                    f"COHN Root CA enrollment returned HTTP status {response.status} for {address}"
                )
            payload = response.read(_MAX_ROOT_CA_BYTES + 1)
    except LabsCohnError:
        raise
    except urllib.error.HTTPError as exc:
        raise LabsCohnError(
            f"COHN Root CA enrollment returned HTTP status {exc.code} for {address}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise LabsCohnError(f"COHN Root CA enrollment could not reach camera {address}") from None
    return parse_root_ca(payload)


def save_root_ca(certificate: RootCACertificate, target: str | Path) -> bool:
    """Atomically create a 0600 trust anchor without replacing another CA.

    Returns ``True`` only when this call created the target. An existing copy of
    the same certificate is retained (and tightened to 0600); an existing
    different or invalid file is never overwritten.
    """
    path = Path(target)
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise LabsCohnError(f"COHN CA directory is not a real directory: {directory}")
    try:
        directory.chmod(0o700)
    except OSError:
        raise LabsCohnError(f"Could not secure COHN CA directory: {directory}") from None

    def use_existing() -> bool:
        if path.is_symlink():
            raise LabsCohnError(f"Refusing symlink at COHN CA path: {path}")
        try:
            existing = parse_root_ca(path.read_bytes())
        except OSError:
            raise LabsCohnError(f"Could not read existing COHN CA file: {path}") from None
        except LabsCohnError:
            raise LabsCohnError(
                f"Existing COHN CA file is invalid and was not overwritten: {path}"
            ) from None
        if existing.fingerprint_sha256 != certificate.fingerprint_sha256:
            raise LabsCohnError(
                "Existing COHN CA fingerprint differs; refusing to overwrite "
                f"{path} (existing {existing.fingerprint_sha256}, "
                f"camera {certificate.fingerprint_sha256})"
            )
        try:
            path.chmod(0o600)
        except OSError:
            raise LabsCohnError(f"Could not secure existing COHN CA file: {path}") from None
        return False

    if path.exists() or path.is_symlink():
        return use_existing()

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=directory,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(certificate.pem)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return use_existing()
        path.chmod(0o600)
        return True
    except LabsCohnError:
        raise
    except OSError:
        raise LabsCohnError(f"Could not save COHN CA file: {path}") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def remove_root_ca_if_matches(
    target: str | Path,
    certificate: RootCACertificate,
) -> bool:
    """Remove a newly enrolled CA only when it still matches the expected CA."""
    path = Path(target)
    if path.is_symlink():
        return False
    try:
        current = parse_root_ca(path.read_bytes())
        if current.fingerprint_sha256 != certificate.fingerprint_sha256:
            return False
        path.unlink()
        return True
    except (LabsCohnError, OSError):
        return False


class LabsCohnClient:
    """Call the HERO13 Labs-over-COHN endpoints using HTTP Basic Auth.

    Passing an opener is supported for deterministic tests; production callers should
    leave it unset. Production use requires the enrolled camera Root CA, verifies the
    certificate and IP hostname, disables environment proxies, and rejects redirects.
    """

    _MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        ip_address: str,
        username: str,
        password: str,
        *,
        timeout: float = 5.0,
        ca_certificate: str | Path | None = None,
        opener: _Opener | None = None,
    ) -> None:
        address = _cohn_ipv4(ip_address)
        if not username or not password:
            raise ValueError("COHN username and password must not be empty")
        if timeout <= 0:
            raise ValueError("COHN timeout must be greater than zero")

        self.ip_address = str(address)
        self.username = username
        self.timeout = float(timeout)
        self.ca_certificate = Path(ca_certificate).resolve() if ca_certificate else None
        self._authorization = "Basic " + base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        if opener is None and self.ca_certificate is None:
            raise LabsCohnError(
                "An enrolled COHN Root CA is required before authenticated requests"
            )
        self._opener = opener or self._build_direct_opener(self.ca_certificate)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(ip_address={self.ip_address!r}, "
            f"username={self.username!r}, password=<redacted>, timeout={self.timeout!r}, "
            f"tls_verified={self.ca_certificate is not None!r})"
        )

    @staticmethod
    def _build_direct_opener(ca_certificate: Path | None) -> urllib.request.OpenerDirector:
        if ca_certificate is None:
            # Only reachable through explicit test injection; production construction
            # rejects a missing trust anchor before calling this method.
            raise LabsCohnError("An enrolled COHN Root CA is required")
        try:
            context = ssl.create_default_context(cafile=str(ca_certificate))
        except (OSError, ValueError, ssl.SSLError):
            raise LabsCohnError(f"Could not load enrolled COHN Root CA: {ca_certificate}") from None
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        # HERO13's camera-generated CA is intentionally minimal. Python 3.13
        # enables stricter chain policies that can reject this documented COHN
        # chain even when its explicit trust anchor and IP SAN are correct.
        for flag_name in ("VERIFY_X509_STRICT", "VERIFY_X509_PARTIAL_CHAIN"):
            flag = getattr(ssl, flag_name, 0)
            if flag:
                context.verify_flags &= ~flag
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )

    def get_camera_state(self) -> dict[str, Any]:
        """Return the read-only Open GoPro camera state document."""
        return self._get_json("/gopro/camera/state", operation="camera state probe")

    def get_camera_info(self) -> dict[str, Any]:
        """Return model, firmware, and serial information for identity checks."""
        return self._get_json("/gopro/camera/info", operation="camera info probe")

    def keep_alive(self) -> dict[str, Any] | None:
        """Reset the camera's HTTP keep-alive timer.

        Current cameras normally return JSON, but firmware variants are allowed to
        acknowledge successful control requests with an empty response body.
        """
        return self._get_optional_json(
            "/gopro/camera/keep_alive",
            operation="camera keep alive",
        )

    def get_last_captured_media(self) -> dict[str, Any]:
        """Return the last SD-card media path reported by the camera."""
        return self._get_json(
            "/gopro/media/last_captured",
            operation="last captured media probe",
        )

    def run_labs_command(self, code: str) -> dict[str, Any] | None:
        """Execute one Labs command through the HERO13 Labs COHN endpoint.

        This method intentionally is not exposed as a free-form CLI argument. Labs
        commands can start/stop capture or change persistent settings, so higher-level
        code must choose and validate commands for a specific workflow.
        """
        if not code or len(code) > 400:
            raise ValueError("Labs command must contain 1 to 400 characters")
        query = urllib.parse.urlencode(
            {"labs": "1", "code": code},
            quote_via=urllib.parse.quote,
        )
        # GoPro Labs 2.10.70 acknowledges this endpoint with HTTP 200 and an
        # empty body. An empty body is therefore success, not malformed JSON.
        return self._get_optional_json(
            f"/gopro/qrcode?{query}",
            operation="Labs command",
        )

    def stop_shutter(self) -> dict[str, Any] | None:
        """Stop encoding through the standard COHN HTTP control endpoint.

        The recorder sends this immediately after documented Labs ``!E`` instead
        of waiting for a publisher timeout, then verifies both RTMP and encoder state.
        """
        return self._get_optional_json(
            "/gopro/camera/shutter/stop",
            operation="camera shutter stop",
        )

    def _get_json(self, path: str, *, operation: str) -> dict[str, Any]:
        payload = self._request(path, operation=operation)
        if not payload:
            raise LabsCohnError(f"COHN {operation} returned an empty response")
        return self._decode_json(payload, operation=operation)

    def _get_optional_json(
        self,
        path: str,
        *,
        operation: str,
    ) -> dict[str, Any] | None:
        payload = self._request(path, operation=operation)
        if not payload or not payload.strip():
            return None
        return self._decode_json(payload, operation=operation)

    def _request(self, path: str, *, operation: str) -> bytes:
        request = urllib.request.Request(
            f"https://{self.ip_address}{path}",
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization,
                "Connection": "close",
                "User-Agent": f"gopro-multi-rtmp/{__version__}",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read(self._MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise LabsCohnError(
                f"COHN {operation} failed with HTTP status {exc.code} for {self.ip_address}"
            ) from None
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise LabsCohnError(
                self._safe_transport_error(exc, operation=operation)
            ) from None

        if len(payload) > self._MAX_RESPONSE_BYTES:
            raise LabsCohnError(f"COHN {operation} returned an oversized response")
        return payload

    def _safe_transport_error(self, exc: BaseException, *, operation: str) -> str:
        """Classify transport failures without reflecting credentials or URLs."""
        reason: object = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        prefix = f"COHN {operation} for camera {self.ip_address}"
        if isinstance(reason, http.client.RemoteDisconnected):
            return (
                f"{prefix}: TLS connected, but the camera closed the HTTPS request "
                "without an HTTP response; verify the current SHPS username/password "
                "and that the COHN control service is ready"
            )
        if isinstance(reason, ssl.SSLCertVerificationError):
            return f"{prefix}: TLS certificate or IP-hostname verification failed"
        if isinstance(reason, ssl.SSLError):
            return f"{prefix}: TLS negotiation failed"
        if isinstance(reason, (TimeoutError,)):
            return f"{prefix}: HTTPS request timed out"
        if isinstance(reason, ConnectionRefusedError):
            return f"{prefix}: HTTPS connection was refused"
        if isinstance(reason, ConnectionResetError):
            return f"{prefix}: HTTPS connection was reset by the camera"
        if isinstance(reason, http.client.HTTPException):
            return f"{prefix}: camera returned an invalid HTTP response"
        return f"{prefix}: HTTPS transport failed"

    @staticmethod
    def _decode_json(payload: bytes, *, operation: str) -> dict[str, Any]:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LabsCohnError(f"COHN {operation} returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise LabsCohnError(f"COHN {operation} returned an unexpected JSON value")
        return decoded
