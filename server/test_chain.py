#!/usr/bin/env python3
"""独立端口链路集成测试，不连接真实设备或现有服务。"""
import asyncio
import json
import pathlib
import socket
import subprocess
import sys
import tempfile

import websockets

HERE = pathlib.Path(__file__).resolve().parent


async def wait_port(port, timeout=6):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.02)
    raise RuntimeError(f"端口 {port} 未就绪")


async def recv_json(ws, timeout=3):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def recv_until(ws, predicate, timeout=4):
    end = asyncio.get_running_loop().time() + timeout
    while True:
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            raise TimeoutError("未收到预期 WebSocket 消息")
        message = await recv_json(ws, left)
        if predicate(message):
            return message


async def no_message(ws, timeout=0.25):
    try:
        await asyncio.wait_for(ws.recv(), timeout)
    except asyncio.TimeoutError:
        return True
    return False


async def no_event(queue, timeout=0.25):
    try:
        await asyncio.wait_for(queue.get(), timeout)
    except asyncio.TimeoutError:
        return True
    return False


def assert_t(line, mask, axes, seq):
    fields = line.split(":")
    assert fields[0] == "T" and len(fields) == 7, f"非法状态行：{line!r}"
    assert int(fields[1]) == mask and int(fields[6]) == seq
    for actual, expected in zip(fields[2:6], axes):
        assert abs(float(actual) - expected) <= 0.001, (line, expected)


def owner_of(message):
    return message.get("owner") if message.get("type") == "control" else None


class Board:
    """模拟一块板：握手阶段显式确认 P，随后自动确认心跳。"""

    def __init__(self, port):
        self.port = port
        self.reader = None
        self.writer = None
        self.events = asyncio.Queue()
        self.task = None

    async def connect(self, acknowledge=True):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writer.write(b"H\n")
        await self.writer.drain()
        line = await asyncio.wait_for(self.reader.readline(), 3)
        assert line == b"P\n", f"板握手后应收到 P，实际 {line!r}"
        if acknowledge:
            self.writer.write(b"A\n")
            await self.writer.drain()
            self.task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    return
                if line == b"P\n":
                    self.writer.write(b"A\n")
                    await self.writer.drain()
                else:
                    await self.events.put(line.decode().strip())
        except (asyncio.CancelledError, ConnectionError, OSError):
            return

    async def next_event(self, timeout=4):
        return await asyncio.wait_for(self.events.get(), timeout)

    async def next_matching(self, prefix, timeout=4):
        end = asyncio.get_running_loop().time() + timeout
        while True:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                raise TimeoutError(f"未收到板端事件 {prefix!r}")
            event = await self.next_event(left)
            if event.startswith(prefix):
                return event

    async def pause_ack(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None


class Server:
    def __init__(self, pair_file):
        self.http_port = self._free_port()
        self.tcp_port = self._free_port()
        self.pair_file = pair_file
        self.proc = None

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @property
    def ws_url(self):
        return f"ws://127.0.0.1:{self.http_port}/ws"

    async def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "server.py"),
             "--host", "127.0.0.1", "--http-port", str(self.http_port),
             "--tcp-port", str(self.tcp_port),
             "--auth-file", str(self.pair_file)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for _ in range(100):
            if await asyncio.to_thread(lambda: self.proc.poll()) is not None:
                output, _ = self.proc.communicate()
                raise RuntimeError(f"服务端提前退出（{self.proc.returncode}）: {output[-1000:]}")
            try:
                await wait_port(self.http_port, timeout=0.2)
                return
            except RuntimeError:
                await asyncio.sleep(0.05)
        raise RuntimeError(f"端口 {self.http_port} 未就绪")

    async def close(self):
        if not self.proc:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(self.proc.communicate), 3)
        except asyncio.TimeoutError:
            self.proc.kill()
            await asyncio.to_thread(self.proc.communicate)
        self.proc = None


async def test_chain():
    # 登录令牌只写入临时文件，不输出、不回显。
    with tempfile.TemporaryDirectory(prefix="motion-gamepad-chain-") as temp:
        pair_file = pathlib.Path(temp) / "pair-code"
        pair_file.write_text("chain-test-pair-code-1234\n")
        server = Server(pair_file)
        webs = []
        boards = []
        try:
            await server.start()
            await wait_port(server.tcp_port)

            # 无令牌和错误密码的客户端不能进入 Web 客户端集合，也收不到状态。
            wrong = await websockets.connect(server.ws_url, proxy=None)
            webs.append(wrong)
            await wrong.send(json.dumps({"type": "hello", "role": "web"}))
            assert (await recv_until(wrong, lambda m: m.get("type") == "login_required"))["type"] == "login_required"
            await wrong.send(json.dumps({"type": "login", "password": "wrong-pass"}))
            assert (await recv_until(wrong, lambda m: m.get("type") == "login_required"))["type"] == "login_required"
            await wrong.send(json.dumps({"type": "state", "epoch": 0, "seq": 1,
                                         "buttons": 1, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            assert await no_message(wrong), "未登录客户端不应收到状态"
            await wrong.send(json.dumps({"type": "login", "password": "chain-test-pair-code-1234"}))
            login = await recv_until(wrong, lambda m: m.get("type") == "login_ok")
            token = login["token"]
            await wrong.send(json.dumps({"type": "hello", "role": "web", "token": token}))
            assert owner_of(await recv_until(wrong, lambda m: m.get("type") == "control")) is True
            await wrong.close()
            webs.remove(wrong)

            # 先让无回应的半开连接发 H；仅 H 不得成为当前板。
            half_reader, half_writer = await asyncio.open_connection("127.0.0.1", server.tcp_port)
            half_writer.write(b"H\n")
            await half_writer.drain()
            assert await asyncio.wait_for(half_reader.readline(), 3) == b"P\n"
            good = await websockets.connect(server.ws_url, proxy=None)
            webs.append(good)
            await good.send(json.dumps({"type": "hello", "role": "web", "token": token,
                                        "clientId": "good"}))
            control = await recv_until(good, lambda m: m.get("type") == "control")
            assert owner_of(control) is True
            epoch = control["epoch"]
            # 没有板端 A 前，正确配对网页也不得下发输入。
            await good.send(json.dumps({"type": "state", "epoch": epoch, "seq": 1,
                                        "buttons": 1, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            assert await no_message(good), "未验证板端不应产生状态回执"

            board = Board(server.tcp_port)
            boards.append(board)
            await board.connect()
            control = await recv_until(good, lambda m: m.get("type") == "control" and m.get("esp32") is True)
            assert control["epoch"] > epoch
            epoch = control["epoch"]
            assert await board.next_event() == "R"

            # 每个新 epoch 的首帧必须中性；旧 epoch 和首帧非中性均忽略。
            await good.send(json.dumps({"type": "state", "epoch": epoch - 1, "seq": 1,
                                        "buttons": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            await good.send(json.dumps({"type": "state", "epoch": epoch, "seq": 1,
                                        "buttons": 4, "lx": 0, "ly": -1, "rx": 0, "ry": 0}))
            assert await no_event(board.events), "epoch 首帧非中性不应转发"
            await good.send(json.dumps({"type": "state", "epoch": epoch, "seq": 2,
                                        "buttons": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            await good.send(json.dumps({"type": "state", "epoch": epoch, "seq": 3,
                                        "buttons": 4, "lx": 0.1254, "ly": -1, "rx": 0, "ry": 0}))
            assert_t(await board.next_event(), 0, (0, 0, 0, 0), 2)
            assert_t(await board.next_event(), 4, (0.1254, -1, 0, 0), 3)

            # 旁观者不能控制；接管时先中和，再要求接管者首帧中性。
            spectator = await websockets.connect(server.ws_url, proxy=None)
            webs.append(spectator)
            await spectator.send(json.dumps({"type": "hello", "role": "web", "token": token,
                                             "clientId": "spectator"}))
            assert owner_of(await recv_until(spectator, lambda m: m.get("type") == "control")) is False
            await spectator.send(json.dumps({"type": "state", "epoch": epoch, "seq": 90,
                                             "buttons": 2, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            assert await no_event(board.events), "旁观者状态不应转发"
            await spectator.send(json.dumps({"type": "hello", "role": "web", "takeover": True,
                                             "clientId": "spectator"}))
            takeover = await recv_until(spectator, lambda m: m.get("type") == "control" and m.get("owner") is True)
            new_epoch = takeover["epoch"]
            assert new_epoch > epoch
            assert await board.next_event() == "R"
            await spectator.send(json.dumps({"type": "state", "epoch": new_epoch, "seq": 1,
                                             "buttons": 8, "lx": 0, "ly": 0, "rx": 0.5, "ry": 0}))
            assert await no_event(board.events), "接管后的首帧非中性不应转发"
            await spectator.send(json.dumps({"type": "state", "epoch": new_epoch, "seq": 2,
                                             "buttons": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            await spectator.send(json.dumps({"type": "state", "epoch": new_epoch, "seq": 3,
                                             "buttons": 8, "lx": 0, "ly": 0, "rx": 0.5, "ry": 0}))
            assert_t(await board.next_event(), 0, (0, 0, 0, 0), 2)
            assert_t(await board.next_event(), 8, (0, 0, 0.5, 0), 3)

            # 主控输入超时必须中和；断开主控也必须中和并释放控制权。
            await spectator.send(json.dumps({"type": "state", "epoch": new_epoch, "seq": 4,
                                             "buttons": 8, "lx": 0, "ly": 0, "rx": 0.5, "ry": 0}))
            assert await board.next_event() == "T:8:0.000:0.000:0.500:0.000:4"
            assert await board.next_event(timeout=3) == "R"
            await spectator.close()
            webs.remove(spectator)
            assert await board.next_event(timeout=3) == "R"

            # 恢复 good 为主控，再验证新板仅完成 H 时不替换旧板。
            await good.send(json.dumps({"type": "hello", "role": "web", "takeover": True,
                                        "clientId": "good"}))
            good_control = await recv_until(
                good, lambda m: m.get("type") == "control" and m.get("owner") is True
                and m.get("epoch", -1) > new_epoch)
            good_epoch = good_control["epoch"]
            assert await board.next_event(timeout=3) == "R"

            # 新板先完成真实 H/P/A 握手后才替换；仅 H 时旧板仍能收状态。
            old = board
            replacement = Board(server.tcp_port)
            boards.append(replacement)
            await replacement.connect(acknowledge=False)
            await good.send(json.dumps({"type": "state", "epoch": good_epoch, "seq": 1,
                                        "buttons": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            await good.send(json.dumps({"type": "state", "epoch": good_epoch, "seq": 2,
                                        "buttons": 1, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            assert_t(await old.next_matching("T:1:"), 1, (0, 0, 0, 0), 2)
            replacement.writer.write(b"A\n")
            await replacement.writer.drain()
            replacement.task = asyncio.create_task(replacement._read_loop())
            replaced = await recv_until(good, lambda m: m.get("type") == "control" and m.get("esp32") is True)
            assert replaced["epoch"] > good_epoch
            assert await old.reader.read(1) == b""
            await good.send(json.dumps({"type": "state", "epoch": replaced["epoch"], "seq": 1,
                                        "buttons": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            await good.send(json.dumps({"type": "state", "epoch": replaced["epoch"], "seq": 2,
                                        "buttons": 16, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            assert_t(await replacement.next_matching("T:16:"), 16, (0, 0, 0, 0), 2)
        finally:
            for ws in webs:
                try:
                    await ws.close()
                except Exception:
                    pass
            half_writer.close() if "half_writer" in locals() else None
            if "half_writer" in locals():
                await half_writer.wait_closed()
            for board in boards:
                await board.close()
            await server.close()


asyncio.run(test_chain())
print("链路集成测试通过（独立临时端口）")
