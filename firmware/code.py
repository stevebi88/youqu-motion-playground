# Motion Gamepad Pro — HORIPAD S 模拟 + 有时效的完整手柄状态（CircuitPython 10.x）
# 出品方：杭州智转科技有限公司
# 协议：H(固件握手) / P→A(连接探测) / R(立即中和) / T:<mask>:<lx>:<ly>:<rx>:<ry>:<seq>
# 1 秒无新 T：先松开和回中，再丢弃旧连接。RAISE 看门狗另行处理可捕获的异常。
import errno, time, usb_hid, wifi, socketpool
import microcontroller
from watchdog import WatchDogMode, WatchDogTimeout

# ===== 使用前必改：只有下面 4 项需要按你的环境修改 =====
WIFI_SSID = "YOUR_WIFI_SSID"       # 你的 WiFi 名称（2.4GHz）
WIFI_PASS = "YOUR_WIFI_PASSWORD"   # 你的 WiFi 密码
SERVER_IP = "192.168.1.100"        # 运行 server 的电脑/NAS 内网 IP（按实际网段修改）
SERVER_PORT = 8446                 # 与 server.py 的 --tcp-port 一致，默认不用改
# =====================================================

INPUT_TTL = 1.0    # 超过 1 秒没有新的完整状态：松开所有键、双摇杆回中
HB_TIMEOUT = 8.0   # 超过 8 秒没有 P/T/R：TCP 连接判死并重连（不影响输入租期）
WDT_TIMEOUT = 15.0
BUTTON_MAX = 0x1FFFF

# ---- 报告结构: [Y/B/A/X/L/R/ZL/ZR, -/+/LS/RS/Home/Cap, HAT, lx, ly, rx, ry, 0] ----
B0 = {"GP_Y": 0x01, "GP_B": 0x02, "GP_A": 0x04, "GP_X": 0x08,
      "GP_L": 0x10, "GP_R": 0x20, "GP_ZL": 0x40, "GP_ZR": 0x80}
B1 = {"GP_Minus": 0x01, "GP_Plus": 0x02, "GP_LS": 0x04, "GP_RS": 0x08, "GP_Home": 0x10}
# 完整状态位序：0..7=B0，8..12=B1，13..16=十字键
DPAD_BITS = {"Up": 13, "Down": 14, "Left": 15, "Right": 16}

report = bytearray(8)
report[2] = 8      # HAT center
for i in (3, 4, 5, 6):
    report[i] = 128
dpad = {"Up": False, "Down": False, "Left": False, "Right": False}
lx, ly, rx, ry = 0.0, 0.0, 0.0, 0.0

device = None
for d in usb_hid.devices:
    device = d
    break

def dpad_hat():
    u, d, l, r = dpad["Up"], dpad["Down"], dpad["Left"], dpad["Right"]
    if u and r: return 1
    if d and r: return 3
    if d and l: return 5
    if u and l: return 7
    if u: return 0
    if r: return 2
    if d: return 4
    if l: return 6
    return 8

def send():
    report[2] = dpad_hat()
    report[3] = int(max(0, min(255, 128 + lx * 127)))
    report[4] = int(max(0, min(255, 128 + ly * 127)))
    report[5] = int(max(0, min(255, 128 + rx * 127)))
    report[6] = int(max(0, min(255, 128 + ry * 127)))
    if device:
        try:
            device.send_report(bytes(report))
        except OSError:
            pass

def apply_mask(mask):
    if mask < 0 or mask > BUTTON_MAX:
        raise ValueError("buttons mask out of range")
    report[0] = mask & 0xFF
    report[1] = (mask >> 8) & 0x1F
    for name, bit in DPAD_BITS.items():
        dpad[name] = bool(mask & (1 << bit))

def parse_axis(text):
    v = float(text)
    if not (-1.0 <= v <= 1.0) or v != v:   # v != v 过滤 NaN
        raise ValueError("axis out of range")
    return v

def handle(line):
    """只用通过校验的 T 续输入租期；P/R 依协议分别探活/中和。"""
    global lx, ly, rx, ry, last_seq, last_rx, last_state, fresh, armed, target_mask
    now = time.monotonic()
    if line == "P":
        write_line(sock, b"A\n")
        last_rx = now
        return
    if line == "R":
        release_all_keys()
        last_seq = -1
        last_rx = now
        return
    parts = line.split(":")
    if len(parts) != 7 or parts[0] != "T":
        return
    mask, seq = int(parts[1]), int(parts[6])
    vals = [parse_axis(x) for x in parts[2:6]]
    if not 0 <= mask <= BUTTON_MAX or not last_seq < seq < 2**53:
        return
    if not armed and (mask or any(vals)):
        return
    armed = True
    last_seq = seq
    last_state = last_rx = now
    fresh = True
    lx, ly, rx, ry = vals
    # 同一个 TCP 批次里也保留短按边沿；R/断链/租期过期不受最短按压限制。
    rising = mask & ~target_mask
    for bit in range(17):
        if rising & (1 << bit):
            hold_until[bit] = now + 0.04
    target_mask = mask
    flush_report(now)


def flush_report(now):
    mask = target_mask
    for bit, until in enumerate(hold_until):
        if now < until:
            mask |= 1 << bit
    apply_mask(mask)
    send()


def write_line(conn, data):
    if conn.send(data) != len(data):
        raise OSError("incomplete heartbeat")


def close_socket():
    global sock
    if sock is not None:
        try:
            sock.close()
        finally:
            sock = None

def release_all_keys():
    """清空全部按键/摇杆并立即上报；断链、超时、重连前都调用，防 Switch 卡键。"""
    global lx, ly, rx, ry, fresh, armed, target_mask
    armed = False
    target_mask = 0
    for bit in range(17):
        hold_until[bit] = 0
    report[0] = 0
    report[1] = 0
    for name in dpad:
        dpad[name] = False
    lx = ly = rx = ry = 0.0
    fresh = False
    send()

def feed_wdt():
    if microcontroller.watchdog is not None:
        microcontroller.watchdog.feed()

def ensure_wifi():
    """网络错误才重试；WatchDogTimeout 交给外层先中和再重启。"""
    if wifi.radio.connected:
        return True
    try:
        feed_wdt()
        print("[wifi] connecting...")
        try:
            wifi.radio.connect(WIFI_SSID, WIFI_PASS, timeout=5)
        except TypeError:
            wifi.radio.connect(WIFI_SSID, WIFI_PASS)
        try:
            wifi.radio.power_management = wifi.PowerManagement.NONE
        except (AttributeError, NotImplementedError, OSError):
            pass  # 仅为降低游戏输入延迟，不阻断不支持此设置的构建
        print("[wifi] ip =", wifi.radio.ipv4_address)
        return True
    except (OSError, ConnectionError) as e:
        print("[wifi] connect failed:", e)
        return False

def safe_restart(reason):
    """先中和 HID，再以正常模式重启；避免 RESET 看门狗把 ESP32 带进安全模式。"""
    try:
        release_all_keys()
    except Exception:
        pass
    print("[safe] restart:", reason)
    try:
        microcontroller.on_next_reset(microcontroller.RunMode.NORMAL)
    except Exception:
        pass
    microcontroller.reset()

sock = None
fresh = armed = False
last_seq = -1
target_mask = 0
hold_until = [0.0] * 17
last_state = last_rx = 0.0


def run():
    global sock, last_seq, last_rx

    send()
    print("[boot] reset reason:", microcontroller.cpu.reset_reason)
    # RAISE 模式：超时抛出 WatchDogTimeout，由 safe_restart 先松键再正常重启。
    if microcontroller.watchdog is not None:
        try:
            microcontroller.watchdog.timeout = WDT_TIMEOUT
            microcontroller.watchdog.mode = WatchDogMode.RAISE
            microcontroller.watchdog.feed()
            print("[safe] watchdog armed 15s (RAISE)")
        except (ValueError, NotImplementedError) as e:
            print("[safe] watchdog unavailable:", e)

    # 启动连 WiFi：带重试，绝不退出（退出会停在 REPL）
    while not ensure_wifi():
        feed_wdt()
        time.sleep(2)

    pool = socketpool.SocketPool(wifi.radio)

    sock = None
    buf = bytearray(128)
    line_buf = bytearray()
    line_overflow = False
    last_rx = time.monotonic()

    while True:
        feed_wdt()

        if not wifi.radio.connected:
            release_all_keys()
            close_socket()
            if not ensure_wifi():
                time.sleep(2)
                continue

        if sock is None:
            try:
                sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
                sock.settimeout(4)
                sock.connect((SERVER_IP, SERVER_PORT))
                sock.settimeout(0.05)
                write_line(sock, b"H\n")
                line_buf = bytearray()
                line_overflow = False
                last_seq = -1
                last_rx = time.monotonic()
                release_all_keys()
                print("[tcp] connected to server")
            except OSError:
                close_socket()
                time.sleep(1.5)
                continue

        try:
            try:
                n = sock.recv_into(buf)
            except OSError as e:
                if not e.args or e.args[0] not in (errno.ETIMEDOUT, errno.EAGAIN):
                    raise
                n = None
            if n == 0:
                raise OSError("closed")
            # 期限一到就丢弃整条旧连接，积压的中性帧也不能重新解锁旧动作。
            now = time.monotonic()
            if fresh and now - last_state >= INPUT_TTL:
                raise OSError("input lease expired")
            if now - last_rx >= HB_TIMEOUT:
                raise OSError("heartbeat expired")
            for b in buf[:n] if n else ():
                if b == 10:
                    if not line_overflow:
                        try:
                            handle(bytes(line_buf).decode("ascii"))
                        except (ValueError, UnicodeError):
                            pass
                    line_buf = bytearray()
                    line_overflow = False
                elif not line_overflow:
                    if len(line_buf) >= 127:
                        line_buf = bytearray()
                        line_overflow = True
                    else:
                        line_buf.append(b)
            if fresh:
                flush_report(time.monotonic())
        except OSError as e:
            print("[tcp] lost:", e)
            release_all_keys()
            close_socket()
            time.sleep(0.5)

try:
    run()
except WatchDogTimeout:
    safe_restart("watchdog timeout")
except Exception:
    # 未知程序错误保留原始 traceback，不能像旧版那样不断重启并使 USB 磁盘反复掉线。
    try:
        release_all_keys()
    finally:
        close_socket()
    raise
