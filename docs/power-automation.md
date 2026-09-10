# 无电池供电自动化

本文面向已经安装 GoPro Labs 的 HERO13，并且相机已按主 README 完成直播 Wi-Fi、COHN 和证书登记。

## 先理解关机边界

GoPro 官方的 [USB Power Trigger](https://gopro.github.io/labs/control/usb/) 可以在 USB 通电时启动相机，并在 USB 断电后结束拍摄再关机，但官方明确说明这个完整流程需要电池：USB 消失后，相机必须继续获得一小段时间的电力，才能关闭正在写入的视频。

无电池时，切断 USB 就是立即失去全部电力。此时不存在还能运行的“自动关机”代码，也不能再补写 MP4 尾部。因此安全顺序必须是：

1. USB 通电，Labs 自动唤醒相机；
2. 主机通过 COHN 启动和停止直播；
3. 程序确认所有相机停止编码、RTMP publisher 下线，并关闭 MediaMTX 完成本机文件；
4. 程序以退出码 `0` 成功结束后，外部电源控制器才切断 USB。

## 一次性设置通电唤醒

对每台相机分别执行：

1. 保留 SD 卡并临时正常启动相机。
2. 打开[已经填好 `*WAKE=2` 的 GoPro Labs 官方 QR 页面](https://gopro.github.io/labs/control/set/?cmd=%2AWAKE%3D2&title=Wake%20on%20Power)，让相机扫描下面的永久命令：

   ```text
   *WAKE=2
   ```

   Labs 官方命令表将 `WAKE=2` 定义为 HERO8/10–13 的“任何电源接入都唤醒”，无需等待已有定时任务。
3. 如果此前扫描过完整 USB Power Trigger，请先在[官方 USB Power Trigger 页面](https://gopro.github.io/labs/control/usb/)选择 “Disable the USB trigger” 并扫描生成的 QR，再单独扫描 `*WAKE=2`。完整 Trigger 会自行启动拍摄，可能与本项目启动前的“相机必须空闲”检查冲突。
4. 正常关机，取出电池，只保留稳定的 USB-C 供电。
5. 切断并重新接通 USB。确认相机自动启动、加入此前保存的 `MJOIN` 网络，并恢复 COHN。
6. 在主机验证：

   ```bash
   .venv/bin/gopro-multi-rtmp --config config.toml probe
   ```

需要取消通电唤醒时，让相机扫描永久命令 `*WAKE=0`。

## 总电源自动化顺序

多台相机可以接到同一个受控电源，但电源和 USB 集线器必须能同时稳定承载所有相机。外部智能插座、继电器或 PDU 不属于本项目；把下面的 `power_on_all_cameras` 和 `power_off_all_cameras` 替换成实际设备的 API 或命令。

```bash
power_on_all_cameras

until .venv/bin/gopro-multi-rtmp --config config.toml probe; do
  sleep 2
done

if .venv/bin/gopro-multi-rtmp --config config.toml record --duration 3600; then
  power_off_all_cameras
else
  echo "采集或封装未确认成功，保持 USB 供电并人工处理" >&2
fi
```

这里最重要的保护条件是：只有 `record` 返回 `0` 才允许切断电源。成功退出前，程序会停止相机、等待流下线、关闭 MediaMTX、检查本机录制；`encode_to_sd = true` 时还会确认相机报告了新的 SD 媒体路径。若返回非零，不要让自动化继续断电，应保持供电并查看本次会话的 `manifest.json` 和 `logs/mediamtx.log`。

## 与官方完整 USB Power Trigger 的区别

- 有电池并希望“拔 USB 后相机自己收尾”：使用官方完整 USB Power Trigger。
- 无电池并使用本项目集中录制：只用 `*WAKE=2` 做通电唤醒，由本项目先完成停止和封装，再切断外部电源。
- 直接在录制过程中切断无电池相机的 USB，不属于安全关机，可能损坏相机 SD 文件；主机端当时正在写入的录制也可能不完整。

参考：[GoPro Labs USB Power Trigger](https://gopro.github.io/labs/control/usb/)、[GoPro Labs Extension Commands](https://gopro.github.io/labs/control/extensions/)、[GoPro Labs Command Language](https://gopro.github.io/labs/control/tech/)。
