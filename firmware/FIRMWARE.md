# 固件烧录指南（ESP32-S3 + CircuitPython）

本目录三个文件需拷入 ESP32-S3 的 `CIRCUITPY` 盘：

| 文件 | 作用 |
|---|---|
| `boot.py` | 把板子伪装成 HORIPAD S USB HID 手柄（描述符来自 radiantwf/esp32-circuitpython-switch-joystick，MIT） |
| `code.py` | 主程序：连 WiFi → TCP 连 server → 收输入帧 → 上报 HID 手柄状态（含输入租期、看门狗、断链自动松键） |
| `safemode.py` | 板子进入 CircuitPython 安全模式时打印原因，避免无限掉盘重启 |

## 第一步：烧录 CircuitPython 固件

1. 下载 CircuitPython **10.3.2** 官方 YD-ESP32-S3 N16R8 英文版镜像：
   在 <https://circuitpython.org/board/espressif_yd_esp32s3_n16r8/> 下载 `.bin`（文件名类似
   `adafruit-circuitpython-yd_esp32s3_n16r8-en_US-10.3.2.bin`）。
2. 安装 esptool：`pip install esptool`
3. 让板子进入**下载模式**：按住 `BOOT` 键不放 → 点按一下 `RESET` → 松开 `BOOT`。
4. 烧录（端口按实际修改，macOS 形如 `/dev/cu.usbmodem*`，Windows 形如 `COM5`）：

   ```bash
   esptool.py --chip esp32s3 --port /dev/cu.usbmodem* --baud 460800 write_flash 0x0 adafruit-circuitpython-yd_esp32s3_n16r8-en_US-10.3.2.bin
   ```

5. 点按 `RESET` 复位，电脑上会出现 `CIRCUITPY` U 盘。

> 已烧录过 CircuitPython 的板子，串口控制台和 CIRCUITPY 盘可能因 HID 被占用而消失，同样用「按住 BOOT + 点按 RESET」回到下载模式即可恢复。

## 第二步：部署固件文件

把本目录的 `boot.py`、`code.py`、`safemode.py` 三个文件拷贝到 `CIRCUITPY` 盘根目录（覆盖同名文件）。

## 第三步：修改配置（唯一要改的地方）

打开 `CIRCUITPY/code.py`，头部配置区只有 4 项需要按环境修改，保存即生效（无需重新烧录）：

```python
WIFI_SSID = "YOUR_WIFI_SSID"       # 你的 WiFi 名称（2.4GHz）
WIFI_PASS = "YOUR_WIFI_PASSWORD"   # 你的 WiFi 密码
SERVER_IP = "192.168.1.100"        # 运行 server 的电脑/NAS 内网 IP（按实际网段修改）
SERVER_PORT = 8446                 # 与 server.py 的 --tcp-port 一致，默认不用改
```

保存后 `code.py` 会自动重启运行；串口日志（如可见）会打印 `[wifi] ip = ...` 和 `[tcp] connected to server`。

## 双 Type-C 口板子说明

- 标 `COM`/`UART` 的口：经 USB-UART 芯片，用于烧录固件和查看日志；
- 标 `USB`/`OTG` 的口：ESP32-S3 **原生 USB**，HID 手柄功能必须用它插 Switch。

## 常见问题

- **保存 code.py 后板子反复重启 / 掉盘**：检查是否误改了配置区以外的代码；板子进入安全模式时 `safemode.py` 会在串口打印原因。
- **Switch 不识别手柄**：确认用的是原生 USB 口 + 数据线（非纯充电线），并在 Switch「设置 → 手柄 → Pro 手柄 有线连接」开启。
- **串口没了怎么调试**：HID 模式下部分主板的串口与 CIRCUITPY 盘会被手柄占用；需要重新调试时按住 `BOOT` + 点按 `RESET` 进下载模式重刷即可。
