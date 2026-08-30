# HERO13 多相机无线同步采集

这个项目用于在同一局域网中控制任意数量的 GoPro HERO13 Black：相机通过 GoPro Labs 和 COHN 接收控制命令，将 RTMP 视频流发送到 Mac 上的 MediaMTX，并可同时在各自的 SD 卡保存副本。

日常录制时，相机只需保持开机、已经连接采集 Wi-Fi，并停留在普通预览界面。主机可以一条命令同时启动或停止所选相机，不需要再次进入“等待配对”界面。

同步指标分为两层：

- 程序用并发屏障分发启动命令，记录各相机请求发出的时间跨度；
- MediaMTX 记录各路 RTMP publisher 第一次上线的观测跨度。

这不是硬件 genlock。无线网络、相机编码器和 RTMP 启动都会引入可变延迟。需要逐帧双目对齐时，应让所有相机拍到同一个 LED 闪光或拍板，再根据共同事件做离线帧对齐。

## 1. 安装 HERO13 GoPro Labs 固件

每台相机都需要单独完成一次。

1. 从 [GoPro Labs 官方页面](https://gopro.github.io/labs/) 下载 HERO13 Black 对应的 Labs 固件。本文已经验证 HERO13 Labs `2.10.70`。
2. 确保相机电池电量不低于 50%。
3. 取出 microSD 卡，通过读卡器连接电脑。固件文件不能通过相机的 USB 连接复制。
4. 解压下载的固件压缩包，得到名为 `UPDATE` 的目录。目录内通常包含 `CAMFWV.bin`、`DATA.bin` 和 `FWUPD.txt`。
5. 将整个 `UPDATE` 目录复制到 SD 卡根目录。不要复制 ZIP 文件，不要复制成 `UPDATE(1)`，也不要多嵌套一层目录。

正确结构如下：

```text
SD 卡根目录/
├── UPDATE/
│   ├── CAMFWV.bin
│   ├── DATA.bin
│   └── FWUPD.txt
├── DCIM/
└── MISC/
```

6. 安全弹出 SD 卡。
7. 相机关机后插入 SD 卡，再开机。
8. 更新期间相机会蜂鸣并多次重启。不要关机、拔电池或取卡；前屏显示完成勾号后更新结束。
9. 在相机信息页面确认固件版本。

完整步骤见 [GoPro Labs 固件安装说明](https://gopro.github.io/labs/install/)。

### 恢复 GoPro 正式固件

如果以后需要恢复正式固件，只从 [GoPro HERO13 官方更新页面](https://gopro.com/en/cf/support/hero13-black-product-update/macos) 获取当时提供的正式 `UPDATE` 包，并按相同的 SD 卡手动更新流程安装。官方没有保证任意版本都能直接同版本回刷；如果页面没有可用包或相机拒绝安装，应联系 GoPro 支持，不要使用第三方固件包。

## 2. 用 GoPro Quik 完成一次配对

新机、更新后尚未使用直播功能的相机，以及恢复出厂设置后的相机，需要先完成一次手机 App 初始化。

1. 在手机上打开 GoPro Quik。
2. 让当前相机进入配对界面，并在 Quik 中添加它。
3. 确认 Quik 能识别相机后，让相机回到普通预览界面。
4. 对每台相机分别执行一次。

这是一次性初始化。正常关机、重启以及每次录制前都不需要重新配对。恢复出厂设置后可能需要再次执行。

## 3. 准备采集网络

推荐使用独立的 5 GHz 路由器或 AP：

- Mac 和所有相机连接同一个局域网；
- 使用 WPA2 或 WPA3，不使用开放网络；
- 关闭 AP isolation、client isolation 或访客网络隔离；
- 在路由器中为每台相机设置 DHCP 地址保留；
- 确保相机能够访问 Mac 的 TCP `1935` 端口；
- 不要把 MediaMTX 的 RTMP 或 API 端口暴露到公网。

正式采集时还应准备：

- 每台相机一张速度足够且空间充足的 microSD 卡；
- 稳定 USB-C 供电和必要的散热；
- 相机设置 `Auto Power Off = Never`；
- 采集期间关闭手机 Quik，避免同时控制相机。

## 4. 首次写入 Wi-Fi 并初始化 COHN

全新相机尚不能被主机通过 COHN 访问，因此第一次网络写入需要使用相机扫描 Labs QR 码。

### 4.1 保存采集 Wi-Fi

打开 [GoPro Labs Live-Stream Setup](https://gopro.github.io/labs/control/rtmp/)，填写采集 Wi-Fi 的 SSID 和密码，生成并让相机扫描 Wi-Fi QR 码。对应的 Labs 命令形式为：

```text
!MJOIN="SSID:Wi-Fi密码"
```

`MJOIN` 会把网络信息保存到相机的非易失存储。QR 码本身含有明文 Wi-Fi 密码，不要截图分享或提交到版本库。

### 4.2 初始化 COHN

打开 [GoPro Labs QR Control](https://gopro.github.io/labs/control/set/)，让相机扫描：

```text
$COHN=1
```

每台相机只初始化一次。完成后重启相机，等待它重新加入采集 Wi-Fi。

### 4.3 查看地址和 HTTPS 凭据

相机已经联网后，让它扫描：

```text
$ADDR=15$SHPS=15
```

记录每台相机的：

- 局域网 IPv4；
- COHN 用户名；
- COHN 密码。

当前 HERO13 Labs `2.10.70` 实机通常显示一行 `gopro:<密码>`，下一行是同一凭据的编码形式。配置中填写冒号后的密码本体，不要把第二行拼接进去；不同固件应以相机实际显示为准。

这些信息也可能写入 SD 卡的 `MISC/qrlog.txt`。该文件包含敏感凭据，应像密码文件一样保护。

COHN 初始化后会在后续开机时恢复，无需每次重新扫描。它不能从彻底关机或已经完全休眠的相机中凭空唤醒控制服务，因此日常采集仍要求相机保持开机并已入网。

## 5. 安装主机程序

支持 Python 3.11、3.12 和 3.13。

在 macOS 上安装 MediaMTX 和 FFmpeg：

```bash
brew install mediamtx ffmpeg
```

创建干净的虚拟环境并安装程序：

```bash
cd /path/to/gopro-multi-rtmp
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

程序运行时只使用 Python 标准库；MediaMTX 负责接收并落盘 RTMP，FFmpeg/ffprobe 负责本机链路测试和媒体检查。

## 6. 创建 `config.toml`

在项目根目录新建 `config.toml`，并限制文件权限：

```bash
chmod 600 config.toml
```

当前配置格式为 `schema_version = 2`。程序会拒绝未知字段，避免拼写错误被静默忽略。

完整示例：

```toml
schema_version = 2

[network]
# Mac 在采集网络中的 IPv4，不是相机 IP。
rtmp_host = "192.168.18.10"
rtmp_port = 1935

[stream]
resolution = 1080       # 480、720 或 1080
encode_to_sd = true     # 同时要求每台相机保存 SD 副本
require_audio = true    # 要求每个本机 MP4 存在音频轨

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
cohn_ip = "192.168.18.101"
cohn_username = "gopro"
cohn_password = "相机生成的 COHN 密码"

[[cameras]]
alias = "cam_b"
serial = "C3530000000002"
stream_key = "cam_b"
cohn_ip = "192.168.18.102"
cohn_username = "gopro"
cohn_password = "另一台相机生成的 COHN 密码"
# enabled = false
```

字段说明：

- `rtmp_host`：Mac 在采集局域网中的 IPv4；不能填写相机 IP、`localhost` 或 `127.0.0.1`。
- `resolution`：Labs 直播分辨率，只支持 `480`、`720`、`1080`。
- `encode_to_sd`：为 `true` 时，启动命令要求相机同步保存 SD 副本。
- `require_audio`：为 `true` 时，本机文件没有音频轨会使会话失败；程序不会判断音频内容是否静音。
- `sha256`：为 `true` 时，为本机录制文件计算 SHA-256。
- `alias`：主机使用的相机名称。
- `serial`：相机完整序列号。
- `stream_key`：该相机独占的 RTMP 路径名。
- `cohn_ip`、`cohn_username`、`cohn_password`：第 4 步得到的逐机 COHN 信息。
- `enabled = false`：暂时排除这台相机；省略时默认为启用。

所有相机的 `alias`、完整 `serial`、`stream_key` 和 `cohn_ip` 必须分别唯一。继续追加 `[[cameras]]` 即可扩展相机数量。

Wi-Fi 密码不写入 `config.toml`。它只在首次 QR 配置或后面的隐藏输入中使用。

## 7. 固定每台相机的 COHN Root CA

COHN 通过 HTTPS 和 HTTP Basic Auth 控制相机。每台相机有独立的 Root CA，不能混用。

先只读取并显示 Root CA 指纹，不保存：

```bash
.venv/bin/gopro-multi-rtmp --config config.toml enroll \
  --camera cam_a
```

预览模式会故意以退出码 `2` 结束，表示尚未接受首次信任。确认 IP 和指纹后保存证书并核验完整序列号：

```bash
.venv/bin/gopro-multi-rtmp --config config.toml enroll \
  --camera cam_a --accept-first-use
```

多台相机可以一次登记：

```bash
.venv/bin/gopro-multi-rtmp --config config.toml enroll \
  --camera cam_a --camera cam_b --accept-first-use
```

证书默认保存到：

```text
.cohn-ca/<alias>.crt
```

目录权限为 `0700`，证书权限为 `0600`。程序随后只使用固定 CA、证书中的 IP SAN 和配置中的完整序列号建立信任。

## 8. 统一或更换直播 Wi-Fi

第 4 步已经给新相机完成了网络 bootstrap。相机已经能够通过 COHN 访问后，可以使用主机命令为多台相机统一或更换 Labs 直播网络。

先预览计划，不连接相机，也不读取密码：

```bash
.venv/bin/gopro-multi-rtmp --config config.toml \
  provision-stream-network \
  --camera cam_a --camera cam_b \
  --ssid "采集WiFi"
```

确认后实际写入：

```bash
.venv/bin/gopro-multi-rtmp --config config.toml \
  provision-stream-network \
  --camera cam_a --camera cam_b \
  --ssid "采集WiFi" --apply
```

程序会通过隐藏提示读取一次 Wi-Fi 密码。密码不会进入命令行、`config.toml`、manifest 或日志。所有相机必须已经完成 `enroll` 且处于空闲状态；任一相机预检失败时，不会向任何所选相机写入设置。

Labs 没有定义 JOIN 字符串中冒号、双引号和控制字符的转义规则，因此本程序会拒绝含这些字符的 SSID 或密码。修改采集 Wi-Fi 的 SSID 或密码时重新执行本步骤。

完成后重启相机。

## 9. 检查系统

### 9.1 检查配置和本机依赖

```bash
.venv/bin/gopro-multi-rtmp --config config.toml doctor
```

`doctor` 不连接相机，检查 Python、MediaMTX、FFmpeg、FFprobe、配置、证书、文件权限和端口。

### 9.2 用合成流测试本机链路

```bash
.venv/bin/gopro-multi-rtmp --config config.toml selftest \
  --camera cam_a --camera cam_b --duration 5
```

该命令不用真实相机，而是按所选相机数量生成合成音视频，验证 MediaMTX、RTMP 和本机 MP4 落盘。

### 9.3 查询真实相机

```bash
.venv/bin/gopro-multi-rtmp --config config.toml probe \
  --camera cam_a --camera cam_b
```

`probe` 会通过固定 CA 的 COHN HTTPS 连接相机，核对完整序列号并读取状态。省略 `--camera` 时查询全部启用相机。

## 10. 开始录制

录制前确认：

- 所有相机已经开机并连接采集 Wi-Fi；
- 相机停留在普通预览界面；
- 没有相机仍在编码；
- 上一段直播结束后已经留出恢复时间。HERO13 实测中 7 秒可能不足，建议保守等待约 30 秒。

### 单机定时录制

```bash
.venv/bin/gopro-multi-rtmp --config config.toml record \
  --camera cam_a --duration 10 --label pilot
```

### 双机或多机定时录制

```bash
.venv/bin/gopro-multi-rtmp --config config.toml record \
  --camera cam_a --camera cam_b \
  --duration 30 --label stereo
```

`--camera` 可以重复任意次数。省略它时，程序选择所有 `enabled = true` 的相机：

```bash
.venv/bin/gopro-multi-rtmp --config config.toml record \
  --duration 30 --label all_cameras
```

### 手动停止

省略 `--duration` 后，程序立即开始，按一次 Enter 统一停止：

```bash
.venv/bin/gopro-multi-rtmp --config config.toml record
```

无人值守采集应优先使用 `--duration`。有限时长会给每台相机附加相机侧自停保险；交互模式无法预先确定保险时长，主机异常退出后相机可能继续录制。

## 11. 录制流程和成功条件

每次会话按以下顺序执行：

1. 启动会话专用 MediaMTX，只配置本次所选 RTMP 路径。
2. 通过 COHN 核对每台相机的完整序列号和状态。
3. 需要音频时发送临时 `$DAUD=0`，确保音频未被 Labs 设置禁用。
4. 为每台相机写入独立的 `MRTMP` 目标。
5. 记录各相机启动前的 SD `last_captured` 路径。
6. 用并发屏障发送 Labs 直播命令。1080p 并保留 SD 副本时为 `!GLC`。
7. 等待全部 RTMP publisher 上线后开始正式计时。
8. 录制期间每 3 秒发送 keep-alive，并监测所有 RTMP 路径。
9. 到达时长后发送 Labs `!E`，紧接着发送标准 COHN shutter-stop。
10. 确认所有 publisher 下线、所有相机停止编码，再关闭 MediaMTX。
11. 用 ffprobe 验证每个本机 MP4 的视频轨、所需音频轨和最低时长。
12. `encode_to_sd = true` 时，确认每台相机的 `last_captured` 路径发生变化。

SD 验证只能证明相机报告了新的媒体路径；程序不会通过网络下载并校验 SD 文件内容。

## 12. 输出目录

每次会话生成：

```text
recordings/<UTC时间>_<label>_<随机后缀>/
├── streams/live/<stream_key>/*.mp4
├── manifest.json
├── logs/mediamtx.log
└── runtime/mediamtx.yml
```

- `streams/live/<stream_key>/*.mp4`：本机收到的各路视频。
- `manifest.json`：会话状态、相机事件、同步观测值、ffprobe 结果和 SD 路径验证。
- `logs/mediamtx.log`：MediaMTX 运行日志。
- `runtime/mediamtx.yml`：本次会话生成的服务配置，不含相机密码。

本机 MP4 可能包含相机启动预缓冲和编码收尾，文件长度可以略大于请求的采集时长。

### 同步指标

`manifest.json` 中的主要同步指标：

- `command_dispatch_span_ms`：各相机启动请求实际发出的时间跨度；
- `command_ack_span_ms`：各相机 HTTPS 调用完成的时间跨度；
- `publisher_first_seen_span_ms`：MediaMTX 第一次观察到各路 publisher 的时间跨度。

第一项反映主机命令分发同步性。第三项还包含相机启动、编码、Wi-Fi、RTMP 和约 100 ms 轮询粒度，不等于相机曝光偏差。

## 13. 新增相机

新增 `cam_c` 时：

1. 安装相同的 HERO13 Labs 固件。
2. 在 Quik 中完成一次配对。
3. 扫描采集 Wi-Fi 的 `MJOIN` QR。
4. 扫描 `$COHN=1`。
5. 重启并扫描 `$ADDR=15$SHPS=15`。
6. 在 `config.toml` 中追加新的 `[[cameras]]` 段。
7. 执行 `enroll --camera cam_c --accept-first-use`。
8. 需要统一网络时执行 `provision-stream-network --camera cam_c --ssid ... --apply`。
9. 重启后执行 `probe --camera cam_c`。
10. 录制时追加 `--camera cam_c`，或省略 `--camera` 选择全部启用相机。

删除或暂时停用相机时，可以删除对应配置段，或设置：

```toml
enabled = false
```

## 14. 常见故障

### COHN TLS 能连接，但认证失败

- 重新扫描 `$SHPS=15`；
- 核对 `cohn_username` 和密码本体；
- 不要把屏幕上的编码行拼接到密码；
- 确保 `config.toml` 权限为 `0600`。

### Root CA 缺失或不匹配

先核对相机 IP 和序列号。重新初始化 COHN 后，人工移走旧 `.cohn-ca/<alias>.crt`，重新查看指纹并执行 `enroll`。程序不会覆盖内容不同的现有证书。

### `probe` 无法访问相机

检查相机是否开机、自动关机是否禁用、是否加入正确 Wi-Fi、DHCP 地址是否变化，以及路由器是否开启了客户端隔离。

### 相机接受启动命令，但 MediaMTX 看不到 publisher

依次检查：

1. `rtmp_host` 是否是相机可访问的 Mac 局域网 IPv4；
2. Mac 防火墙是否允许 TCP `1935`；
3. 相机是否已经完成一次 Quik 配对；
4. `MJOIN` 是否保存了正确 Wi-Fi；
5. 上一段直播结束后是否留出了约 30 秒恢复时间；
6. 相机是否停留在普通预览且没有处于 BUSY/ENCODING 状态。

### 本机文件没有音频轨

保持 `require_audio = true`。程序会在启动前发送 `$DAUD=0`，最终仍以 ffprobe 是否看到音频轨为准。

### SD 路径没有更新

检查 `encode_to_sd = true`、SD 卡空间和速度，以及相机是否正常停止并完成文件封装。

### 端口占用或依赖缺失

运行 `doctor`。确认没有其他 MediaMTX 实例占用 `1935` 或 `9997`。

## 15. 安全说明

- `config.toml` 包含逐机 COHN 密码，必须保持 `0600`，且已被 `.gitignore` 排除。
- `.cohn-ca/` 目录保持 `0700`，证书文件保持 `0600`。
- Wi-Fi 密码只通过相机 QR 或隐藏提示输入，不进入 `config.toml`、命令行、manifest 或日志。
- `$SHPS` 屏幕、`MISC/qrlog.txt` 和 `MJOIN` QR 都可能暴露凭据，不要分享截图。
- 程序只允许私有或 link-local 的相机 IPv4，拒绝向公网地址发送 COHN 凭据。
- 正常 HTTPS 请求禁用系统代理和重定向，并强制固定 Root CA 与 IP hostname 校验。
- `enroll --accept-first-use` 是唯一的首次信任步骤；获取 Root CA 的请求不携带认证信息。
- OpenGoPro 文档说明 COHN Root CA 有效期为一年，应安排到期前重新登记。
- RTMP 本身未加密，只应在可信、隔离的采集局域网中使用。

## 16. 开发检查

安装开发依赖：

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

运行完整检查：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/mypy src
.venv/bin/python -m compileall -q src tests
.venv/bin/gopro-multi-rtmp --help
```

## 17. 官方资料

- [GoPro Labs 固件](https://gopro.github.io/labs/)
- [GoPro Labs 固件安装说明](https://gopro.github.io/labs/install/)
- [GoPro Labs Live-Stream Setup](https://gopro.github.io/labs/control/rtmp/)
- [GoPro Labs Action Commands](https://gopro.github.io/labs/control/actions/)
- [GoPro Labs Command Language](https://gopro.github.io/labs/control/tech/)
- [GoPro Labs Release Notes](https://gopro.github.io/labs/control/notes/)
- [OpenGoPro Camera on the Home Network](https://gopro.github.io/OpenGoPro/docs/ble/cohn/)
