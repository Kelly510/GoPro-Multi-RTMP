"""Configuration loading, camera selection, and secret-safe snapshots."""

from __future__ import annotations

import ipaddress
import math
import os
import re
import socket
import stat
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable


class ConfigError(ValueError):
    """Raised when the local configuration is invalid."""


@dataclass(frozen=True)
class CameraConfig:
    alias: str
    serial: str
    stream_key: str
    cohn_ip: str | None = None
    cohn_username: str | None = None
    cohn_password: str | None = field(default=None, repr=False)
    cohn_ca_cert: Path | None = None
    enabled: bool = True

    @property
    def cohn_complete(self) -> bool:
        return all((self.cohn_ip, self.cohn_username, self.cohn_password))


@dataclass(frozen=True)
class NetworkConfig:
    rtmp_host: str
    rtmp_port: int


@dataclass(frozen=True)
class StreamConfig:
    resolution: int
    encode_to_sd: bool
    require_audio: bool


@dataclass(frozen=True)
class ServerConfig:
    mediamtx_binary: str
    output_root: Path
    api_port: int
    record_part_duration: str
    record_segment_duration: str
    sha256: bool


@dataclass(frozen=True)
class PreviewConfig:
    enabled: bool
    bind_host: str
    hls_port: int
    auto_open: bool


@dataclass(frozen=True)
class TimeoutConfig:
    cohn_request_seconds: float
    publisher_ready_seconds: float
    shutdown_seconds: float


@dataclass(frozen=True)
class AppConfig:
    source: Path
    network: NetworkConfig
    stream: StreamConfig
    server: ServerConfig
    preview: PreviewConfig
    timeouts: TimeoutConfig
    cameras: tuple[CameraConfig, ...]


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_DURATION = re.compile(r"^[1-9][0-9]*(ms|s|m|h)$")
_PLACEHOLDERS = ("CHANGE_ME", "YOUR_")
_CONFIG_SCHEMA_VERSION = 2
_TOP_LEVEL_KEYS = {
    "schema_version",
    "network",
    "stream",
    "server",
    "preview",
    "timeouts",
    "cameras",
}
_NETWORK_KEYS = {"rtmp_host", "rtmp_port"}
_STREAM_KEYS = {"resolution", "encode_to_sd", "require_audio"}
_SERVER_KEYS = {
    "mediamtx_binary",
    "output_root",
    "api_port",
    "record_part_duration",
    "record_segment_duration",
    "sha256",
}
_PREVIEW_KEYS = {"enabled", "bind_host", "hls_port", "auto_open"}
_TIMEOUT_KEYS = {"cohn_request_seconds", "publisher_ready_seconds", "shutdown_seconds"}
_CAMERA_KEYS = {
    "alias",
    "serial",
    "stream_key",
    "cohn_ip",
    "cohn_username",
    "cohn_password",
    "cohn_ca_cert",
    "enabled",
}
_COHN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"缺少 [{name}] 配置段")
    return value


def _reject_unknown_keys(section: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"{label} 包含未知字段：{', '.join(unknown)}")


def _int(section: dict[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数")
    return value


def _float(section: dict[str, Any], key: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} 必须是数值")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{key} 必须是有限数值")
    return result


def _bool(section: dict[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是 true 或 false")
    return value


def _optional_string(section: dict[str, Any], key: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须是字符串")
    stripped = value.strip()
    return stripped or None


def _optional_path(section: dict[str, Any], key: str, base: Path) -> Path | None:
    value = _optional_string(section, key)
    if value is None:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def default_cohn_ca_path(config_source: str | Path, alias: str) -> Path:
    """Return the per-camera trust-anchor path managed by ``enroll``."""
    source = Path(config_source).expanduser().resolve()
    return (source.parent / ".cohn-ca" / f"{alias}.crt").resolve()


def load_config(path: str | Path) -> AppConfig:
    """Load and strictly validate the current Labs+COHN TOML schema."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ConfigError(f"找不到配置文件：{source}")
    with source.open("rb") as handle:
        data = tomllib.load(handle)

    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "配置顶层")
    if data.get("schema_version") != _CONFIG_SCHEMA_VERSION:
        raise ConfigError(f"schema_version 必须为 {_CONFIG_SCHEMA_VERSION}")
    network_data = _section(data, "network")
    stream_data = _section(data, "stream")
    server_data = _section(data, "server")
    preview_data = data.get("preview", {})
    if not isinstance(preview_data, dict):
        raise ConfigError("[preview] 必须是 TOML 表")
    timeout_data = _section(data, "timeouts")
    _reject_unknown_keys(network_data, _NETWORK_KEYS, "[network]")
    _reject_unknown_keys(stream_data, _STREAM_KEYS, "[stream]")
    _reject_unknown_keys(server_data, _SERVER_KEYS, "[server]")
    _reject_unknown_keys(preview_data, _PREVIEW_KEYS, "[preview]")
    _reject_unknown_keys(timeout_data, _TIMEOUT_KEYS, "[timeouts]")

    network = NetworkConfig(
        rtmp_host=str(network_data.get("rtmp_host", "")).strip(),
        rtmp_port=_int(network_data, "rtmp_port", 1935),
    )
    stream = StreamConfig(
        resolution=_int(stream_data, "resolution", 1080),
        encode_to_sd=_bool(stream_data, "encode_to_sd", True),
        require_audio=_bool(stream_data, "require_audio", True),
    )
    output_root = Path(str(server_data.get("output_root", "./recordings"))).expanduser()
    if not output_root.is_absolute():
        output_root = (source.parent / output_root).resolve()
    server = ServerConfig(
        mediamtx_binary=str(server_data.get("mediamtx_binary", "auto")).strip(),
        output_root=output_root,
        api_port=_int(server_data, "api_port", 9997),
        record_part_duration=str(server_data.get("record_part_duration", "1s")),
        record_segment_duration=str(server_data.get("record_segment_duration", "1h")),
        sha256=_bool(server_data, "sha256", False),
    )
    preview = PreviewConfig(
        enabled=_bool(preview_data, "enabled", True),
        bind_host=str(preview_data.get("bind_host", "127.0.0.1")).strip(),
        hls_port=_int(preview_data, "hls_port", 8888),
        auto_open=_bool(preview_data, "auto_open", True),
    )
    timeouts = TimeoutConfig(
        cohn_request_seconds=_float(timeout_data, "cohn_request_seconds", 5.0),
        publisher_ready_seconds=_float(timeout_data, "publisher_ready_seconds", 45.0),
        shutdown_seconds=_float(timeout_data, "shutdown_seconds", 30.0),
    )

    camera_data = data.get("cameras")
    if not isinstance(camera_data, list) or not camera_data:
        raise ConfigError("必须至少配置一段 [[cameras]]")
    cameras: list[CameraConfig] = []
    for item in camera_data:
        if not isinstance(item, dict):
            raise ConfigError("每段 [[cameras]] 必须是 TOML 表")
        _reject_unknown_keys(item, _CAMERA_KEYS, "[[cameras]]")
        alias = str(item.get("alias", "")).strip()
        ca_certificate = _optional_path(item, "cohn_ca_cert", source.parent)
        if ca_certificate is None and _SAFE_NAME.fullmatch(alias):
            managed_ca = default_cohn_ca_path(source, alias)
            if managed_ca.is_file():
                ca_certificate = managed_ca
        cameras.append(
            CameraConfig(
                alias=alias,
                serial=str(item.get("serial", "")).strip(),
                stream_key=str(item.get("stream_key", "")).strip(),
                cohn_ip=_optional_string(item, "cohn_ip"),
                cohn_username=_optional_string(item, "cohn_username"),
                cohn_password=_optional_string(item, "cohn_password"),
                cohn_ca_cert=ca_certificate,
                enabled=_bool(item, "enabled", True),
            )
        )

    config = AppConfig(
        source=source,
        network=network,
        stream=stream,
        server=server,
        preview=preview,
        timeouts=timeouts,
        cameras=tuple(cameras),
    )
    validate_config(config, hardware=False)
    return config


def validate_config(config: AppConfig, *, hardware: bool) -> None:
    """Validate static invariants and, when requested, selected COHN cameras."""
    if not 1 <= config.network.rtmp_port <= 65535:
        raise ConfigError("network.rtmp_port 超出有效范围")
    if not 1 <= config.server.api_port <= 65535:
        raise ConfigError("server.api_port 超出有效范围")
    if config.network.rtmp_port == config.server.api_port:
        raise ConfigError("RTMP 端口与 MediaMTX API 端口不能相同")
    if not 1 <= config.preview.hls_port <= 65535:
        raise ConfigError("preview.hls_port 超出有效范围")
    try:
        preview_address = ipaddress.ip_address(config.preview.bind_host)
    except ValueError as exc:
        raise ConfigError("preview.bind_host 必须是 IPv4 地址") from exc
    if not isinstance(preview_address, ipaddress.IPv4Address):
        raise ConfigError("preview.bind_host 必须是 IPv4 地址")
    if (
        preview_address.is_unspecified
        or preview_address.is_multicast
        or preview_address.is_reserved
    ) or not (
        preview_address.is_loopback
        or preview_address.is_private
        or preview_address.is_link_local
    ):
        raise ConfigError("preview.bind_host 必须是回环、私有或 link-local IPv4")
    if config.preview.enabled and config.preview.hls_port in {
        config.network.rtmp_port,
        config.server.api_port,
    }:
        raise ConfigError("HLS 预览端口不能与 RTMP 或 MediaMTX API 端口相同")
    if config.stream.resolution not in {480, 720, 1080}:
        raise ConfigError("stream.resolution 只能是 480、720 或 1080")
    if not _DURATION.fullmatch(config.server.record_part_duration):
        raise ConfigError("server.record_part_duration 格式无效，例如 1s")
    if not _DURATION.fullmatch(config.server.record_segment_duration):
        raise ConfigError("server.record_segment_duration 格式无效，例如 1h")
    if not all(
        value > 0
        for value in (
            config.timeouts.cohn_request_seconds,
            config.timeouts.publisher_ready_seconds,
            config.timeouts.shutdown_seconds,
        )
    ):
        raise ConfigError("所有 timeout 秒数都必须大于 0")

    aliases = [camera.alias for camera in config.cameras]
    serials = [camera.serial for camera in config.cameras]
    stream_keys = [camera.stream_key for camera in config.cameras]
    for label, values in (("alias", aliases), ("serial", serials), ("stream_key", stream_keys)):
        if len({value.casefold() for value in values}) != len(values):
            raise ConfigError(f"所有相机的 {label} 必须不同")

    configured_ips: list[str] = []
    for camera in config.cameras:
        if not _SAFE_NAME.fullmatch(camera.alias):
            raise ConfigError(f"相机 alias 非法：{camera.alias!r}")
        if not _SAFE_NAME.fullmatch(camera.stream_key):
            raise ConfigError(f"相机 stream_key 非法：{camera.stream_key!r}")
        if len(camera.serial) < 4 or not re.fullmatch(r"[A-Za-z0-9_-]+", camera.serial):
            raise ConfigError(f"相机序列号格式无效：{camera.serial!r}")
        if camera.cohn_ip is not None:
            try:
                address = ipaddress.ip_address(camera.cohn_ip)
            except ValueError as exc:
                raise ConfigError(f"相机 {camera.alias} 的 cohn_ip 不是有效 IPv4") from exc
            if address.version != 4:
                raise ConfigError(f"相机 {camera.alias} 的 cohn_ip 必须是 IPv4")
            if not isinstance(address, ipaddress.IPv4Address) or not any(
                address in network for network in _COHN_NETWORKS
            ):
                raise ConfigError(
                    f"相机 {camera.alias} 的 cohn_ip 必须是私有或 link-local IPv4，"
                    "拒绝向公网/保留地址发送 COHN 凭据"
                )
            configured_ips.append(str(address))
        if hardware and camera.cohn_ca_cert is not None and not camera.cohn_ca_cert.is_file():
            raise ConfigError(
                f"相机 {camera.alias} 的 cohn_ca_cert 文件不存在：{camera.cohn_ca_cert}"
            )
    if len({value.casefold() for value in configured_ips}) != len(configured_ips):
        raise ConfigError("所有相机的 cohn_ip 必须不同")

    if not hardware:
        return
    validate_cohn_credentials(config)
    missing_ca = [camera.alias for camera in config.cameras if camera.cohn_ca_cert is None]
    if missing_ca:
        raise ConfigError(
            "以下相机尚未固定 COHN Root CA："
            + ", ".join(missing_ca)
            + "；请先执行 enroll --camera <alias> --accept-first-use"
        )
    public_values = [config.network.rtmp_host, *serials]
    if any(any(marker in value.upper() for marker in _PLACEHOLDERS) for value in public_values):
        raise ConfigError("config.toml 仍含 CHANGE_ME/YOUR_ 占位值")
    host = resolve_rtmp_host(config.network.rtmp_host)
    address = ipaddress.ip_address(host)
    if address.is_loopback or address.is_unspecified:
        raise ConfigError("rtmp_host 必须是所有相机可访问的本机局域网地址，不能是回环地址")


def validate_cohn_credentials(config: AppConfig) -> None:
    """Validate local secret handling before any authenticated COHN request."""
    if not config.cameras:
        raise ConfigError("没有选中相机")
    if os.name == "posix":
        mode = stat.S_IMODE(config.source.stat().st_mode)
        if mode & 0o077:
            raise ConfigError(
                f"配置文件权限过宽（{mode:04o}）；请执行 chmod 600 {config.source}"
            )
    incomplete = [camera.alias for camera in config.cameras if not camera.cohn_complete]
    if incomplete:
        raise ConfigError("以下相机缺少 COHN IP/用户名/密码：" + ", ".join(incomplete))


def select_cameras(config: AppConfig, aliases: Iterable[str] | None) -> AppConfig:
    """Return a config containing only the selected, enabled cameras."""
    by_alias = {camera.alias: camera for camera in config.cameras}
    if aliases:
        requested = list(dict.fromkeys(aliases))
        unknown = [alias for alias in requested if alias not in by_alias]
        if unknown:
            raise ConfigError(f"找不到相机 alias：{', '.join(unknown)}")
        disabled = [alias for alias in requested if not by_alias[alias].enabled]
        if disabled:
            raise ConfigError(f"相机已设置 enabled=false：{', '.join(disabled)}")
        cameras = tuple(by_alias[alias] for alias in requested)
    else:
        cameras = tuple(camera for camera in config.cameras if camera.enabled)
    if not cameras:
        raise ConfigError("没有启用的相机")
    selected = replace(config, cameras=cameras)
    validate_config(selected, hardware=False)
    return selected


def resolve_rtmp_host(value: str) -> str:
    """Resolve an explicit or best-effort local IPv4 address."""
    if value.lower() != "auto":
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ConfigError(f"rtmp_host 不是有效 IPv4：{value!r}") from exc
        if address.version != 4:
            raise ConfigError(f"rtmp_host 必须是 IPv4，不能使用 IPv6：{value!r}")
        return str(address)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        host = sock.getsockname()[0]
    except OSError as exc:
        raise ConfigError("无法自动判断本机局域网 IPv4，请在配置中明确填写 rtmp_host") from exc
    finally:
        sock.close()
    if ipaddress.ip_address(host).is_loopback:
        raise ConfigError("自动判断得到回环地址，请明确填写 rtmp_host")
    return host


def public_config(config: AppConfig, resolved_host: str) -> dict[str, Any]:
    """Return a manifest-safe snapshot. COHN credentials are never exported."""
    return {
        "schema_version": _CONFIG_SCHEMA_VERSION,
        "network": {
            "rtmp_host": resolved_host,
            "rtmp_port": config.network.rtmp_port,
        },
        "stream": {
            "resolution": config.stream.resolution,
            "encode_to_sd": config.stream.encode_to_sd,
            "require_audio": config.stream.require_audio,
        },
        "preview": {
            "enabled": config.preview.enabled,
            "protocol": "hls",
            "bind_host": config.preview.bind_host,
            "hls_port": config.preview.hls_port,
            "auto_open": config.preview.auto_open,
        },
        "cameras": [
            {
                "alias": camera.alias,
                "serial": camera.serial,
                "stream_key": camera.stream_key,
                "cohn_ip": camera.cohn_ip,
                "tls_ca_pinned": camera.cohn_ca_cert is not None,
            }
            for camera in config.cameras
        ],
    }
