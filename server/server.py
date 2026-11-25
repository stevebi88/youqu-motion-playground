#!/usr/bin/env python3
"""同源 HTTP/HTTPS + 单主控 WebSocket + 单设备 TCP 桥接。"""
import argparse
import asyncio
import hashlib
import hmac
import json
import mimetypes
import pathlib
import secrets
import sys
import time

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

WEB_CLIENTS = set()
BOARD = None
CONTROLLER = None
EPOCH = 0
LAST_SEQ = -1
LAST_INPUT = None
HEARTBEAT = 1.0
BOARD_ACK_TIMEOUT = 3.0  # 加上发起间隔，失去板端应答约 4 秒判离线
INPUT_TTL = 1.0
AUTH_PASSWORD = None     # --auth 启用网页登录；令牌 30 天有效
AUTH_TOKEN_DAYS = 30
AUTH_SECRET = b""
BOARD_IP = None
INJECT = False
WEB_ROOT = None

BIT_KEYS = (
    "y", "b", "a", "x", "l", "r", "z", "t", "-", "=", "shift", "enter",
    "esc", "up", "down", "left", "right",
)
KB = None
KB_KEYS = None
INJECT_MASK = 0


def inject_diff(mask):
    global INJECT_MASK
    if KB is None:
        return
    diff = mask ^ INJECT_MASK
    for bit, key in enumerate(BIT_KEYS):
        if diff & (1 << bit):
            target = getattr(KB_KEYS, key, key)
            if mask & (1 << bit):
                KB.press(target)
            else:
                KB.release(target)
    INJECT_MASK = mask


def make_token(now):
    """令牌 = 到期时间 + 随机数，用服务器密钥签名；重启服务不失效。"""
    expires = int(now) + AUTH_TOKEN_DAYS * 86400
    nonce = secrets.token_hex(16)
    payload = f"{expires}.{nonce}"
    sig = hmac.new(AUTH_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_token(token, now):
    try:
        expires_str, nonce, sig = token.split(".")
        payload = f"{expires_str}.{nonce}"
        good = hmac.new(AUTH_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return False
        return int(expires_str) > now
    except (ValueError, AttributeError):
        return False


def send_board(line):
    if BOARD is None or BOARD.is_closing():
        return
    try:
        if BOARD.transport.get_write_buffer_size() > 256:
            BOARD.transport.abort()  # 慢连接不排队回放旧输入
        else:
            BOARD.write((line + "\n").encode("ascii"))
    except (OSError, RuntimeError):
        BOARD.transport.abort()


def reset_input():
    global EPOCH, LAST_SEQ, LAST_INPUT
    EPOCH += 1
    LAST_SEQ = -1
    LAST_INPUT = None
    send_board("R")
    inject_diff(0)


def status_for(ws):
    return {"type": "control", "owner": ws is CONTROLLER, "epoch": EPOCH,
            "esp32": BOARD is not None and not BOARD.is_closing(), "inject": INJECT,
            "clients": len(WEB_CLIENTS)}


async def notify():
    async def send(ws):
        if ws.transport.get_write_buffer_size() > 2048:
            ws.transport.abort()
            return
        try:
            await asyncio.wait_for(ws.send(json.dumps(status_for(ws))), 0.5)
        except (websockets.exceptions.ConnectionClosed, TimeoutError):
            ws.transport.abort()
    await asyncio.gather(*(send(ws) for ws in tuple(WEB_CLIENTS)))


async def tcp_handler(reader, writer):
    global BOARD
    peer = writer.get_extra_info("peername")
    try:
        if BOARD_IP and (not peer or peer[0] != BOARD_IP):
            return
        # 必须应答首次探测；仅建立 TCP 或发送 H 不能取代已验证的板。
        if await asyncio.wait_for(reader.readline(), 2) != b"H\n":
            return
        writer.write(b"P\n")
        if await asyncio.wait_for(reader.readline(), BOARD_ACK_TIMEOUT) != b"A\n":
            return
        old, BOARD = BOARD, writer
        if old is not None:
            old.transport.abort()
        reset_input()
        print(f"[tcp] ESP32 在线: {peer}")
        await notify()
        while BOARD is writer and not writer.is_closing():
            await asyncio.sleep(HEARTBEAT)
            if BOARD is not writer or writer.is_closing():
                break
            send_board("P")
            if await asyncio.wait_for(reader.readline(), BOARD_ACK_TIMEOUT) != b"A\n":
                break
    except (asyncio.TimeoutError, TimeoutError, OSError, ValueError):
        pass
    finally:
        if BOARD is writer:
            BOARD = None
            reset_input()
            print(f"[tcp] ESP32 离线: {peer}")
            await notify()
        writer.transport.abort()


def parse_state(m):
    mask, seq = m.get("buttons"), m.get("seq")
    if type(mask) is not int or not 0 <= mask <= 0x1FFFF:
        raise ValueError("buttons")
    if type(seq) is not int or not 1 <= seq < 2**53:
        raise ValueError("seq")
    axes = tuple(m.get(k) for k in ("lx", "ly", "rx", "ry"))
    if any(type(v) not in (int, float) or not -1 <= v <= 1 for v in axes):
        raise ValueError("axes")
    return mask, axes, seq


async def handler(ws):
    global CONTROLLER, LAST_SEQ, LAST_INPUT
    try:
        async for raw in ws:
            try:
                m = json.loads(raw)
            except (ValueError, TypeError, RecursionError):
                continue
            if not isinstance(m, dict):
                continue
            t = m.get("type")
            if t == "login":
                if AUTH_PASSWORD and hmac.compare_digest(str(m.get("password", "")), AUTH_PASSWORD):
                    await ws.send(json.dumps({"type": "login_ok",
                                              "token": make_token(time.time())}))
                else:
                    await ws.send('{"type":"login_required"}')
                continue
            if ws not in WEB_CLIENTS:
                if t != "hello" or m.get("role") != "web":
                    continue
                if AUTH_PASSWORD and not check_token(m.get("token", ""), time.time()):
                    await ws.send('{"type":"login_required"}')
                    continue
                WEB_CLIENTS.add(ws)
                if CONTROLLER is None:
                    CONTROLLER = ws
                    reset_input()
                await notify()
            elif t == "hello" and m.get("takeover") is True and ws is not CONTROLLER:
                CONTROLLER = ws
                reset_input()
                await notify()
            elif (t == "state" and ws is CONTROLLER and type(m.get("epoch")) is int
                  and m["epoch"] == EPOCH and (BOARD is not None or INJECT)):
                now = asyncio.get_running_loop().time()
                if LAST_INPUT is not None and now - LAST_INPUT >= INPUT_TTL:
                    reset_input()
                    await notify()
                    continue  # 不能用迟到的旧代状态抢在定时器前续期
                try:
                    mask, axes, seq = parse_state(m)
                except ValueError:
                    continue
                if seq <= LAST_SEQ or (LAST_SEQ < 0 and (mask or any(axes))):
                    continue  # 每次交接必须先收到该代的中性帧
                LAST_SEQ = seq
                LAST_INPUT = now
                lx, ly, rx, ry = axes
                send_board(f"T:{mask}:{lx:.3f}:{ly:.3f}:{rx:.3f}:{ry:.3f}:{seq}")
                inject_diff(mask)
            elif t == "ping":
                await ws.send(json.dumps({"type": "pong", "t": m.get("t", 0)}))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if ws in WEB_CLIENTS:
            WEB_CLIENTS.discard(ws)
            if ws is CONTROLLER:
                CONTROLLER = None
                reset_input()
            await notify()


def serve_static(path):
    path = path.partition("?")[0]
    f = (WEB_ROOT / (path.lstrip("/") or "index.html")).resolve()
    if not f.is_relative_to(WEB_ROOT) or not f.is_file():
        return Response(404, "Not Found", Headers(), b"Not Found")
    mime = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
    return Response(200, "OK", Headers([("Content-Type", mime), ("Cache-Control", "no-cache")]), f.read_bytes())


async def process_request(connection, request):
    if (request.headers.get("Upgrade") or "").lower() == "websocket":
        if request.path == "/ws":
            return None
        return Response(404, "Not Found", Headers(), b"Not Found")
    return serve_static(request.path)


async def expire_inputs():
    while True:
        await asyncio.sleep(0.1)
        now = asyncio.get_running_loop().time()
        if LAST_INPUT is not None and now - LAST_INPUT >= INPUT_TTL:
            reset_input()
            await notify()


async def amain(args):
    options = dict(process_request=process_request, max_size=2048, max_queue=4,
                   ping_interval=15, ping_timeout=5)
    async with (serve(handler, args.host, args.http_port, **options),
                await asyncio.start_server(tcp_handler, args.host, args.tcp_port, limit=64)):
        print(f"[server] HTTP {args.http_port} / ESP32 TCP {args.tcp_port}")
        await expire_inputs()


def main():
    global INJECT, WEB_ROOT, AUTH_PASSWORD, AUTH_SECRET, BOARD_IP, KB, KB_KEYS
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1] / "web")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--http-port", type=int, default=8443)
    ap.add_argument("--tcp-port", type=int, default=8446)
    ap.add_argument("--board-ip", help="仅接受指定局域网 ESP32 地址；建议使用 DHCP 保留")
    auth = ap.add_mutually_exclusive_group()
    auth.add_argument("--auth", metavar="PASSWORD", help="网页登录密码；令牌 30 天有效")
    auth.add_argument("--auth-file", type=pathlib.Path, help="从文件读登录密码（web 目录外，权限 600）")
    ap.add_argument("--inject", action="store_true")
    args = ap.parse_args()
    WEB_ROOT = args.web.resolve()
    if args.auth_file is not None:
        if args.auth_file.resolve().is_relative_to(WEB_ROOT):
            ap.error("密码文件不能位于 web 目录内")
        AUTH_PASSWORD = args.auth_file.read_text().strip()
    elif args.auth is not None:
        AUTH_PASSWORD = args.auth
    BOARD_IP = args.board_ip
    INJECT = args.inject
    if AUTH_PASSWORD is not None:
        secret_path = pathlib.Path.home() / ".motion-gamepad" / "auth-secret"
        if secret_path.exists():
            AUTH_SECRET = secret_path.read_bytes().strip()
        if len(AUTH_SECRET) < 32:
            AUTH_SECRET = secrets.token_bytes(32)
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_bytes(AUTH_SECRET + b"\n")
            secret_path.chmod(0o600)
        print(f"[auth] 网页登录已启用，令牌 {AUTH_TOKEN_DAYS} 天有效")
    if INJECT:
        from pynput.keyboard import Controller, Key
        KB, KB_KEYS = Controller(), Key
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    finally:
        inject_diff(0)


if __name__ == "__main__":
    main()
