# 柚趣体感游乐场（YouQu Motion Playground）

把手机变成低成本体感手柄：手机浏览器用摄像头做本地肢体识别（TF.js + MoveNet，全程不上传画面），把动作映射成手柄按键，经 WebSocket 发到自建中继服务，再由 ESP32-S3 以 USB HID 手柄（HORIPAD S 伪装）直插 Switch——让 Switch 游戏变成体感玩法。

> Turn your phone into a low-cost motion gamepad: camera body-tracking → gamepad buttons → ESP32-S3 USB → Switch
>
> 出品方：杭州智转科技有限公司（YouQu Motion Playground）

## 架构

```
手机（浏览器，摄像头本地推理）
      │  HTTPS/HTTP 8443 + WSS/WS /ws（网页登录鉴权）
      ▼
服务器（server.py 或 Docker）
      │  TCP 8446（自定义轻量协议 H/P/R/T）
      ▼
ESP32-S3（CircuitPython，HORIPAD S USB HID 伪装）
      │  USB
      ▼
Switch（设置「Pro 手柄 有线连接」开启）
```

- 网页端与 WebSocket 同源同端口（8443），姿态识别全部在手机本地完成，服务器只转发按键状态，近乎零 CPU 负载。
- ESP32-S3 与服务器之间走 TCP 8446 的极简文本协议：`H` 握手、`P→A` 心跳探测、`R` 立即中和、`T:<mask>:<lx>:<ly>:<rx>:<ry>:<seq>` 输入帧；带输入租期与看门狗，断链/超时自动松键防卡键。

## 硬件清单

| 硬件 | 说明 |
|---|---|
| ESP32-S3 开发板 | 推荐 N16R8（16MB Flash / 8MB PSRAM），双 Type-C 口板更佳 |
| Switch | 任意型号，需在系统设置开启「Pro 手柄 有线连接」 |
| 手机 | iOS / Android 现代浏览器（需摄像头权限） |
| USB 数据线 | ESP32-S3 原生 USB 口 → Switch 底座/机身 |

## 快速开始

### ① 刷固件（详见 `firmware/FIRMWARE.md`）

1. 下载 CircuitPython 10.3.2 官方 YD-ESP32-S3 N16R8 英文版 `.bin`；
2. 板子进下载模式（按住 BOOT 再点 RESET），用 esptool 写入 `0x0`；
3. 复位后出现 `CIRCUITPY` 盘，把 `firmware/` 下三个文件（`boot.py` / `code.py` / `safemode.py`）拷进去；
4. 编辑 `code.py` 头部 4 项配置：`WIFI_SSID` / `WIFI_PASS` / `SERVER_IP` / `SERVER_PORT`，保存即生效。

### ② 启动服务器

Docker 方式：

```bash
cd server
AUTH_PASSWORD=你的网页密码 docker compose up -d --build
```

或裸 Python 方式（需 Python 3.9+，`pip install "websockets>=13"`）：

```bash
python3 server/server.py --auth YOUR_PASSWORD
```

`--auth` 是网页登录密码（登录一次，浏览器令牌 30 天有效）；也可用 `--auth-file` 从权限 600 的文件读取。NAS 一键部署脚本见 `server/deploy-nas.sh`。

### ③ 手机连接

手机与服务器连同一 WiFi，浏览器打开 `http://SERVER_IP:8443`，输入密码登录，授权摄像头后点「开始检测」。

### ④ 连接 Switch

用 USB 数据线把 ESP32-S3 的**原生 USB 口**插到 Switch，系统设置 → 手柄 → 「Pro 手柄 有线连接」调到开启。服务器日志出现 `[tcp] ESP32 在线` 即成功。

## 配置一览

| 必改项 | 默认值 | 在哪改 | 说明 |
|---|---|---|---|
| `WIFI_SSID` | `YOUR_WIFI_SSID` | `firmware/code.py` | WiFi 名称（2.4GHz） |
| `WIFI_PASS` | `YOUR_WIFI_PASSWORD` | `firmware/code.py` | WiFi 密码 |
| `SERVER_IP` | `192.168.1.100`（示例，按实际网段修改） | `firmware/code.py` | 运行 server 的电脑/NAS 内网 IP |
| `SERVER_PORT` | `8446` | `firmware/code.py` | 与 server 的 `--tcp-port` 一致，一般不用改 |
| 网页登录密码 | 无（必须显式传入） | server 启动参数 `--auth` / Docker `AUTH_PASSWORD` | 登录令牌 30 天有效 |

server 可选参数：`--host`（默认 0.0.0.0）、`--http-port`（默认 8443）、`--tcp-port`（默认 8446）、`--board-ip`（仅接受指定 ESP32 IP，建议配合 DHCP 保留）、`--inject`（把按键同时注入本机键盘，用于无板调试）。

## 动作映射

默认动作（网页上可逐个开关、调灵敏度）：

| 动作 | 识别方式 | 手柄映射 | 默认 |
|---|---|---|---|
| 原地踏步 | 单脚踝抬起（小幅） | 左摇杆 ↑（走路） | 开 |
| 快速高抬腿 | 膝盖大幅上抬 | B（冲刺/奔跑） | 开 |
| 跳 | 双脚离地 | X（跳跃） | 开 |
| 下蹲 | 髋部快速下降 | L3/LS（蹲下潜行） | 开 |
| 左右转头 | 头部水平偏移 | 右摇杆 X（转视角） | 开 |
| 抬头低头 | 头部俯仰 | 右摇杆 Y（上下看） | 关 |
| 右臂挥砍 | 右手腕从肩下快速上挥过肩 | Y（挥剑攻击） | 开 |
| 双手举过头 | 双手腕高于肩 | L（符文炸弹 投/引爆） | 开 |
| 双臂胸前合拢 | 双手在胸前靠拢合掌 | ZL 按住（举盾·锁定） | 开 |
| 手到嘴边 | 一手近鼻一手在下 | 十字 ↓（吹口哨召马） | 关 |
| 单臂高举 | 一臂高举另一臂下垂 | R（投掷武器） | 开 |
| 右后撤步 | 右脚向身体侧后方迈出 | A（互动·对话） | 开 |

另有低区分度扩展动作（左/右跨步、快速侧跳、抬左腿拉弓、双手框眼望远镜等）与「赛车预设」（身体倾斜=方向盘、踏步=油门、下蹲=漂移），均默认关闭，可在网页端开启。

- 摇杆语义：左摇杆=移动（支持斜向），右摇杆=视角；十字键可手动按（切武器/切盾/技能/口哨）。
- 按住类动作持续输入，点触类动作自动释放；所有动作的触发帧数（enter/exit）与冷却时间可在 `web/index.html` 的 `ACTIONS` 数组中调整。

## FAQ

**刷完固件后电脑上看不到串口了？**
`boot.py` 启用了 USB HID 后，板子枚举为 HID 手柄，CircuitPython 串口控制台与 CIRCUITPY 盘可能不再出现（部分主板上两者可共存）。重新进入下载模式：按住 BOOT 键不放，点按 RESET，再松开 BOOT，即可用 esptool 重新烧录。

**双 Type-C 口的板子，两个口有什么区别？**
标有 `COM`/`UART` 的口通常走 USB-UART 芯片，只用于烧录和看日志；标 `USB`/`OTG` 的口才是 ESP32-S3 的**原生 USB**，HID 手柄功能必须用这个口插 Switch。

**延迟多大？**
摄像头动作识别是延迟大头（MoveNet lightning 每帧推理 + 动作确认帧数），网络与固件链路在局域网内约 5-10ms。想更跟手可降低动作的 `enter` 帧数或提高手机性能模式。

**Switch 能识别吗？**
固件以 HORIPAD S（HORI 出品的第三方有线手柄）身份枚举，Switch 需在「手柄」设置中开启「Pro 手柄 有线连接」后即可识别。

**iOS 上能用吗？**
能。本项目走 WiFi + WebSocket，不依赖 Web Bluetooth（iOS Safari 不支持 Web Bluetooth 也不受影响）；只需 iOS Safari 允许摄像头访问（需 HTTPS 时可在反代侧终结 TLS）。

## 致谢

- [radiantwf/esp32-circuitpython-switch-joystick](https://github.com/radiantwf/esp32-circuitpython-switch-joystick)（MIT）——HORIPAD S USB 描述符与身份伪装方案来源，见 `firmware/boot.py` 头部致谢注释；
- TensorFlow.js / MoveNet——浏览器端姿态识别模型（`web/lib/` 内置，无需外网）；
- [websockets](https://github.com/python-websockets/websockets)——Python WebSocket 库。

## License

[MIT](LICENSE)
