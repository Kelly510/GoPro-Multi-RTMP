"""Command line entry point."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import sys
import warnings
from pathlib import Path
from typing import Any

from .config import (
    AppConfig,
    ConfigError,
    default_cohn_ca_path,
    load_config,
    select_cameras,
    validate_cohn_credentials,
    validate_config,
)
from .diagnostics import format_doctor, run_doctor
from .labs_cohn import (
    LabsCohnClient,
    LabsCohnError,
    fetch_root_ca,
    labs_persistent_join_command,
    remove_root_ca_if_matches,
    save_root_ca,
)
from .recorder import CaptureError, CaptureSession
from .synthetic import SyntheticTestError, run_synthetic_test


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gopro-multi-rtmp",
        description="通过 Labs+COHN 一键采集任意数量 HERO13 的 RTMP，并保留 SD 副本",
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="TOML 配置路径（默认：./config.toml）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查本机依赖和配置，不连接相机")
    doctor.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    doctor.add_argument("--camera", action="append", default=None, help="仅检查指定 alias；可重复")
    doctor.add_argument("--json", action="store_true", help="输出 JSON")

    selftest = subparsers.add_parser("selftest", help="按相机配置数量生成 FFmpeg 合成流，验证本机录制链路")
    selftest.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    selftest.add_argument("--camera", action="append", default=None, help="仅测试指定 alias；可重复")
    selftest.add_argument("--duration", type=float, default=5.0, help="测试秒数（至少 2）")

    enroll = subparsers.add_parser(
        "enroll",
        help="首次获取并固定每台相机的 COHN Root CA；不会启动录制",
    )
    enroll.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    enroll.add_argument(
        "--camera",
        action="append",
        required=True,
        help="要登记的相机 alias；可重复",
    )
    enroll.add_argument("--timeout", type=float, default=5.0, help="每台相机的超时秒数")
    enroll.add_argument(
        "--accept-first-use",
        action="store_true",
        help="明确接受本次首次信任指纹并保存；省略时只显示指纹",
    )

    provision = subparsers.add_parser(
        "provision-stream-network",
        help="一次性为所选相机持久化 Labs 直播 Wi-Fi JOIN 元数据",
    )
    provision.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    provision.add_argument(
        "--camera",
        action="append",
        required=True,
        help="要配置的相机 alias；可重复，必须显式指定",
    )
    provision.add_argument("--ssid", required=True, help="采集路由器的 Wi-Fi SSID")
    provision.add_argument("--timeout", type=float, default=5.0, help="每台相机的超时秒数")
    provision.add_argument(
        "--apply",
        action="store_true",
        help="明确写入每台相机的非易失 Labs JOIN 元数据；省略时只显示计划",
    )

    probe = subparsers.add_parser(
        "probe",
        help="通过 COHN 只读查询 Labs 相机状态；不会启动或停止录制",
    )
    probe.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    probe.add_argument(
        "--camera",
        action="append",
        default=None,
        help="仅查询指定 alias；可重复，省略则查询全部已启用相机",
    )
    probe.add_argument("--timeout", type=float, default=5.0, help="每台相机的超时秒数")
    probe.add_argument("--json", action="store_true", help="输出 JSON")

    record = subparsers.add_parser("record", help="通过 Labs+COHN 控制所选相机并同步采集")
    record.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    record.add_argument(
        "--duration",
        type=float,
        default=None,
        help="自动录制秒数（至少 1 秒）；省略则立即开始、按 Enter 停止",
    )
    record.add_argument("--label", default=None, help="追加到会话目录名的短标签")
    record.add_argument(
        "--camera",
        action="append",
        default=None,
        help="仅录制指定 alias；可重复，省略则录制所有 enabled=true 相机",
    )
    return parser


_SECRET_KEYS = (
    "password",
    "passwd",
    "pwd",
    "authorization",
    "credential",
    "secret",
    "token",
)


def _redact_secrets(value: Any) -> Any:
    """Redact credential-like response fields before terminal output."""
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if any(marker in str(key).casefold() for marker in _SECRET_KEYS)
                else _redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _run_probe(config: AppConfig, args: argparse.Namespace) -> int:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ConfigError("--timeout 必须是大于 0 的有限数值")
    config = select_cameras(config, args.camera)
    validate_config(config, hardware=True)
    cameras = config.cameras

    results: list[dict[str, Any]] = []
    all_ok = True
    for camera in cameras:
        # Config validation guarantees these values for labs_cohn cameras.
        assert camera.cohn_ip is not None
        assert camera.cohn_username is not None
        assert camera.cohn_password is not None
        try:
            client = LabsCohnClient(
                camera.cohn_ip,
                camera.cohn_username,
                camera.cohn_password,
                timeout=args.timeout,
                ca_certificate=camera.cohn_ca_cert,
            )
            info = client.get_camera_info()
            actual_serial = str(info.get("serial_number", "")).strip()
            if actual_serial.casefold() != camera.serial.casefold():
                raise LabsCohnError(
                    f"COHN 相机 {camera.cohn_ip} 序列号不符："
                    f"实际 {actual_serial or '<missing>'}，配置 {camera.serial}"
                )
            state = client.get_camera_state()
            results.append(
                {
                    "alias": camera.alias,
                    "cohn_ip": camera.cohn_ip,
                    "ok": True,
                    "info": _redact_secrets(info),
                    "state": _redact_secrets(state),
                }
            )
        except LabsCohnError as exc:
            all_ok = False
            results.append(
                {
                    "alias": camera.alias,
                    "cohn_ip": camera.cohn_ip,
                    "ok": False,
                    "error": str(exc),
                }
            )

    if args.json:
        print(json.dumps({"ok": all_ok, "cameras": results}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            if result["ok"]:
                state_json = json.dumps(result["state"], ensure_ascii=False, sort_keys=True)
                print(f"[{result['alias']}] COHN 可达：{result['cohn_ip']} state={state_json}")
            else:
                print(f"[{result['alias']}] COHN 查询失败：{result['error']}", file=sys.stderr)
    return 0 if all_ok else 2


def _run_enroll(config: AppConfig, args: argparse.Namespace) -> int:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ConfigError("--timeout 必须是大于 0 的有限数值")

    config = select_cameras(config, args.camera)
    if args.accept_first_use:
        # This check happens before the first request that could carry Basic Auth.
        validate_cohn_credentials(config)

    all_ok = True
    preview_only = not args.accept_first_use
    for camera in config.cameras:
        if camera.cohn_ip is None:
            print(
                f"[{camera.alias}] 登记失败：配置缺少 cohn_ip",
                file=sys.stderr,
            )
            all_ok = False
            continue
        target = default_cohn_ca_path(config.source, camera.alias)
        if camera.cohn_ca_cert is not None and camera.cohn_ca_cert != target:
            print(
                f"[{camera.alias}] 登记失败：配置显式 cohn_ca_cert 指向 {camera.cohn_ca_cert}；"
                f"enroll 只管理默认路径 {target}",
                file=sys.stderr,
            )
            all_ok = False
            continue
        try:
            certificate = fetch_root_ca(
                camera.cohn_ip,
                timeout=args.timeout,
            )
            print(
                f"[{camera.alias}] Root CA：{camera.cohn_ip} "
                f"SHA-256={certificate.fingerprint_sha256}"
            )
            if preview_only:
                print(
                    f"[{camera.alias}] 仅显示，未保存；核对后使用 --accept-first-use"
                )
                continue

            assert camera.cohn_username is not None
            assert camera.cohn_password is not None
            created = save_root_ca(certificate, target)
            try:
                client = LabsCohnClient(
                    camera.cohn_ip,
                    camera.cohn_username,
                    camera.cohn_password,
                    timeout=args.timeout,
                    ca_certificate=target,
                )
                info = client.get_camera_info()
                actual_serial = str(info.get("serial_number", "")).strip()
                if actual_serial.casefold() != camera.serial.casefold():
                    raise LabsCohnError(
                        f"COHN 相机 {camera.cohn_ip} 序列号不符："
                        f"实际 {actual_serial or '<missing>'}，配置 {camera.serial}"
                    )
            except (LabsCohnError, ValueError) as exc:
                cleanup = True
                if created:
                    cleanup = remove_root_ca_if_matches(target, certificate)
                suffix = ""
                if created and not cleanup:
                    suffix = f"；本次新证书未能自动清理，请检查 {target}"
                raise LabsCohnError(f"保存后身份核验失败：{exc}{suffix}") from None

            state = "已安全保存" if created else "已有证书一致"
            print(
                f"[{camera.alias}] {state}并通过完整序列号核验：{target}"
            )
        except (LabsCohnError, ValueError) as exc:
            all_ok = False
            print(f"[{camera.alias}] 登记失败：{exc}", file=sys.stderr)

    if preview_only:
        print("未写入任何证书；确认指纹后重新运行并加入 --accept-first-use")
        return 2
    return 0 if all_ok else 2


def _required_status_flag(state: dict[str, Any], status_id: int) -> bool:
    statuses = state.get("status")
    value = statuses.get(str(status_id)) if isinstance(statuses, dict) else None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return value.strip() == "1"
    raise LabsCohnError(f"相机状态缺少或无法解析 status {status_id}")


def _run_provision_stream_network(config: AppConfig, args: argparse.Namespace) -> int:
    """Persist MJOIN only after explicit preview/apply and pinned identity checks."""
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ConfigError("--timeout 必须是大于 0 的有限数值")

    config = select_cameras(config, args.camera)
    validate_config(config, hardware=True)
    try:
        # Validate the public half before prompting for the secret half.
        labs_persistent_join_command(args.ssid, "validation-placeholder")
    except ValueError as exc:
        raise ConfigError(str(exc)) from None

    aliases = ", ".join(camera.alias for camera in config.cameras)
    print(
        f"计划：为 {aliases} 持久化 Labs 直播网络 SSID={args.ssid!r}；"
        "不修改 config.toml，不显示或保存 Wi-Fi 密码。"
    )
    if not args.apply:
        print("未写入相机；确认目标后重新运行并加入 --apply")
        return 2

    try:
        with warnings.catch_warnings():
            # Python getpass otherwise falls back to echoed stdin when /dev/tty
            # is unavailable. A Wi-Fi password must never use that fallback.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Wi-Fi 密码（输入不显示、不保存）：")
        command = labs_persistent_join_command(args.ssid, password)
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise ConfigError("无法安全读取 Wi-Fi 密码；请在交互终端重试") from None
    except ValueError as exc:
        raise ConfigError(str(exc)) from None

    clients: list[tuple[Any, LabsCohnClient]] = []
    preflight_ok = True
    for camera in config.cameras:
        assert camera.cohn_ip is not None
        assert camera.cohn_username is not None
        assert camera.cohn_password is not None
        try:
            client = LabsCohnClient(
                camera.cohn_ip,
                camera.cohn_username,
                camera.cohn_password,
                timeout=args.timeout,
                ca_certificate=camera.cohn_ca_cert,
            )
            info = client.get_camera_info()
            actual_serial = str(info.get("serial_number", "")).strip()
            if actual_serial.casefold() != camera.serial.casefold():
                raise LabsCohnError(
                    f"COHN 相机 {camera.cohn_ip} 序列号不符："
                    f"实际 {actual_serial or '<missing>'}，配置 {camera.serial}"
                )
            state = client.get_camera_state()
            if _required_status_flag(state, 8) or _required_status_flag(state, 10):
                raise LabsCohnError("相机正忙或仍在编码，拒绝写入持久网络设置")
            clients.append((camera, client))
        except (LabsCohnError, ValueError) as exc:
            preflight_ok = False
            print(f"[{camera.alias}] 直播网络预检失败：{exc}", file=sys.stderr)

    if not preflight_ok:
        print("至少一台相机预检失败；未向任何所选相机写入 JOIN。", file=sys.stderr)
        return 2

    all_ok = True
    for camera, client in clients:
        try:
            client.run_labs_command(command)
            print(
                f"[{camera.alias}] 已接受持久 JOIN 设置并通过序列号核验；"
                "密码未写入主机文件。"
            )
        except (LabsCohnError, ValueError) as exc:
            all_ok = False
            print(f"[{camera.alias}] 直播网络写入失败：{exc}", file=sys.stderr)

    if all_ok:
        print("一次性配置完成；请重启相机后用 probe 确认它仍会自动加入该网络。")
    return 0 if all_ok else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(Path(args.config))
        if args.command == "doctor":
            config = select_cameras(config, args.camera)
            ready, checks = run_doctor(config)
            if args.json:
                print(json.dumps({"ready": ready, "checks": checks}, ensure_ascii=False, indent=2))
            else:
                print(format_doctor(checks))
                print("\n结论：" + ("本机必需项通过" if ready else "仍有必需项未通过"))
            return 0 if ready else 2

        if args.command == "selftest":
            config = select_cameras(config, args.camera)
            session_dir = run_synthetic_test(config, duration=args.duration)
            print(f"多路本机链路测试通过：{session_dir}")
            return 0

        if args.command == "enroll":
            return _run_enroll(config, args)

        if args.command == "provision-stream-network":
            return _run_provision_stream_network(config, args)

        if args.command == "probe":
            return _run_probe(config, args)

        if args.duration is not None and (not math.isfinite(args.duration) or args.duration < 1):
            raise ConfigError("--duration 必须是至少 1 秒的有限数值")
        selected = select_cameras(config, args.camera)
        session = CaptureSession(selected, label=args.label)
        session_dir = session.run(duration=args.duration)
        print(f"\n采集完成：{session_dir}")
        print(f"会话清单：{session_dir / 'manifest.json'}")
        return 0
    except (ConfigError, CaptureError, SyntheticTestError, LabsCohnError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except EOFError:
        print("错误：当前终端无法读取交互输入；请使用 --duration", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
