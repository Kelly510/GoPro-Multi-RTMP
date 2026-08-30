"""Local prerequisite checks that never connect to a camera."""

from __future__ import annotations

import shutil
import socket
import stat
import sys
from typing import Any

from .config import AppConfig, ConfigError, resolve_rtmp_host, validate_config
from .mediamtx import find_mediamtx


def _port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def run_doctor(config: AppConfig) -> tuple[bool, list[dict[str, Any]]]:
    """Return machine-readable diagnostic checks and overall readiness."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "required": required})

    python_ok = (3, 11) <= sys.version_info[:2] < (3, 14)
    add("Python", python_ok, sys.version.split()[0])

    binary = find_mediamtx(config.server.mediamtx_binary)
    add("MediaMTX", binary is not None, str(binary) if binary else "未找到；执行 brew install mediamtx")
    for executable in ("ffmpeg", "ffprobe"):
        path = shutil.which(executable)
        add(executable, path is not None, path or "未找到；执行 brew install ffmpeg")

    try:
        validate_config(config, hardware=True)
        host = resolve_rtmp_host(config.network.rtmp_host)
        add("config.toml", True, f"有效；RTMP 本机地址 {host}")
    except ConfigError as exc:
        add("config.toml", False, str(exc))

    config_mode = stat.S_IMODE(config.source.stat().st_mode)
    add(
        "配置文件权限",
        config_mode & 0o077 == 0,
        f"{config_mode:04o}；建议 chmod 600 {config.source.name}",
        required=False,
    )

    try:
        config.server.output_root.mkdir(parents=True, exist_ok=True)
        probe = config.server.output_root / ".write-test"
        probe.touch(exist_ok=True)
        probe.unlink()
        add("输出目录", True, str(config.server.output_root))
    except OSError as exc:
        add("输出目录", False, str(exc))

    rtmp_port_available = _port_available(config.network.rtmp_port)
    api_port_available = _port_available(config.server.api_port)
    add(
        f"TCP {config.network.rtmp_port}",
        rtmp_port_available,
        "可用" if rtmp_port_available else "已被其他进程占用或当前终端无监听权限",
    )
    add(
        f"TCP {config.server.api_port}",
        api_port_available,
        "可用" if api_port_available else "已被其他进程占用或当前终端无监听权限",
    )
    add(
        "采集网络检查",
        True,
        "请人工确认：主机与相机同一局域网、关闭 AP/客户端隔离，并为相机保留固定 DHCP 地址",
        required=False,
    )
    add(
        "相机检查",
        True,
        "请人工确认：所有所选 HERO13 已安装 Labs、COHN 自动联网、SD 卡有空间",
        required=False,
    )
    add(
        "COHN TLS",
        all(camera.cohn_ca_cert is not None for camera in config.cameras),
        (
            "所有所选相机均已固定 Root CA"
            if all(camera.cohn_ca_cert is not None for camera in config.cameras)
            else "部分相机未固定 Root CA；probe/record 会拒绝发送凭据，请先执行 enroll"
        ),
        required=True,
    )
    ready = all(item["ok"] for item in checks if item["required"])
    return ready, checks


def format_doctor(checks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in checks:
        icon = "✓" if item["ok"] else ("!" if not item["required"] else "✗")
        lines.append(f"{icon} {item['name']}: {item['detail']}")
    return "\n".join(lines)
