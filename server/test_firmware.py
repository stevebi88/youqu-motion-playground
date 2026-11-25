"""用 CPython 和假硬件直接执行 CircuitPython 固件入口。"""

from collections import deque
from contextlib import redirect_stdout
import errno
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "code.py"
NEUTRAL = bytes((0, 0, 8, 128, 128, 128, 128, 0))
B_PRESSED = bytes((2, 0, 8, 128, 128, 128, 128, 0))
A_PRESSED = bytes((4, 0, 8, 128, 128, 128, 128, 0))
FULL_INPUT = bytes((4, 16, 1, 255, 1, 191, 64, 0))
FULL_MASK = (1 << 2) | (1 << 12) | (1 << 13) | (1 << 16)  # A、Home、十字键上和右
ARM = b"T:0:0:0:0:0:1\n"
FULL_STATE = f"T:{FULL_MASK}:1:-1:0.5:-0.5:2\n".encode("ascii")


class WatchDogTimeout(Exception):
    pass


class SimulationDone(BaseException):
    pass


class BoardReset(BaseException):
    pass


class CircuitBytearray(bytearray):
    @property
    def clear(self):
        raise AttributeError("'bytearray' object has no attribute 'clear'")


class FakeSocket:
    def __init__(self, board, receives):
        self.board = board
        self.receives = deque(receives)

    def settimeout(self, timeout):
        self.board.record("timeout", timeout)

    def connect(self, address):
        self.board.record("connect", address[1])

    def send(self, data):
        self.board.record("send", bytes(data))
        return len(data)

    def recv_into(self, buf):
        if not self.receives:
            raise SimulationDone
        elapsed, data = self.receives.popleft()
        self.board.advance(elapsed)
        if data is None:
            raise OSError(errno.ETIMEDOUT, "recv timeout")
        if isinstance(data, Exception):
            raise data
        if len(data) > len(buf):
            raise AssertionError("模拟 TCP 分片不能超出固件接收缓冲区")
        buf[:len(data)] = data
        return len(data)

    def close(self):
        self.board.record("close")


class FakeBoard:
    def __init__(self, *connections, fault=None):
        self.now = 0.0
        self.connections = deque(connections)
        self.events = []
        self.fault = fault
        self.feeds = 0
        self.connected_checks = 0
        self._connected = fault != "wifi_connect"
        self.ipv4_address = "192.0.2.1"
        self.watchdog = SimpleNamespace(feed=self.feed)
        self.stdout = io.StringIO()

    def advance(self, elapsed):
        self.now += elapsed
        if self.now > 30:
            raise SimulationDone

    def record(self, event, value=None):
        self.events.append((self.now, event, value))

    def reports(self):
        return [(t, value) for t, event, value in self.events if event == "hid"]

    def feed(self):
        self.feeds += 1
        if (self.feeds == 1 and self.fault == "watchdog_start") or (
            self.feeds == 2 and self.fault == "watchdog_feed"
        ):
            raise WatchDogTimeout

    @property
    def connected(self):
        self.connected_checks += 1
        if self.fault == "wifi_status" and self.connected_checks == 1:
            raise WatchDogTimeout
        return self._connected

    @connected.setter
    def connected(self, value):
        self._connected = value

    def connect(self, *_args, **_kwargs):
        if self.fault == "wifi_connect":
            raise WatchDogTimeout
        self.connected = True

    def new_socket(self, *_args):
        if not self.connections:
            raise SimulationDone
        return FakeSocket(self, self.connections.popleft())

    def reset(self):
        self.record("reset")
        raise BoardReset

    def run(self):
        modules = {
            "microcontroller": SimpleNamespace(
                cpu=SimpleNamespace(reset_reason="POWER_ON"),
                watchdog=self.watchdog,
                RunMode=SimpleNamespace(NORMAL="NORMAL"),
                on_next_reset=lambda mode: self.record("mode", mode),
                reset=self.reset,
            ),
            "socketpool": SimpleNamespace(SocketPool=lambda _radio: SimpleNamespace(
                socket=self.new_socket, AF_INET=2, SOCK_STREAM=1,
            )),
            "time": SimpleNamespace(monotonic=lambda: self.now, sleep=self.advance),
            "usb_hid": SimpleNamespace(devices=[SimpleNamespace(
                send_report=lambda report: self.record("hid", bytes(report)),
            )]),
            "watchdog": SimpleNamespace(
                WatchDogMode=SimpleNamespace(RAISE="RAISE"), WatchDogTimeout=WatchDogTimeout,
            ),
            "wifi": SimpleNamespace(radio=self),
        }
        source = compile(FIRMWARE.read_text(encoding="utf-8"), str(FIRMWARE), "exec")
        with patch.dict(sys.modules, modules), redirect_stdout(self.stdout):
            try:
                exec(source, {
                    "__name__": "__main__",
                    "__file__": str(FIRMWARE),
                    "bytearray": CircuitBytearray,
                })
            except SimulationDone:
                return
            except BoardReset:
                return
        raise AssertionError("固件在模拟结束或重启前意外退出")


class FirmwareTests(unittest.TestCase):
    def assert_no_reset(self, board):
        self.assertFalse(any(event == "reset" for _, event, _ in board.events))

    def test_greeting_heartbeat_and_fragmented_coalesced_frames(self):
        board = FakeBoard([
            (0.0, ARM[:5]),
            (0.01, ARM[5:] + b"P\n" + FULL_STATE[:-1]),
            (0.01, FULL_STATE[-1:]),
        ])
        board.run()
        self.assert_no_reset(board)
        self.assertEqual(
            [(t, value) for t, event, value in board.events if event == "send"],
            [(0.0, b"H\n"), (0.01, b"A\n")],
        )
        self.assertTrue(all(report == NEUTRAL for t, report in board.reports() if t < 0.02))
        self.assertEqual(board.reports()[-1], (0.02, FULL_INPUT))

    def test_overlong_line_is_dropped_and_next_frame_still_parses(self):
        board = FakeBoard([
            (0.0, b"x" * 128),
            (0.01, b"P\n" + ARM + FULL_STATE),
        ])
        board.run()
        self.assert_no_reset(board)
        self.assertEqual([data for _, event, data in board.events if event == "send"], [b"H\n"])
        self.assertEqual(board.reports()[-1], (0.01, FULL_INPUT))

    def test_short_press_survives_coalesced_release_for_about_40_ms(self):
        board = FakeBoard([
            (0.0, ARM),
            (0.001, b"T:2:0:0:0:0:2\nT:0:0:0:0:0:3\n"),
            (0.039, None),
            (0.002, None),
        ])
        board.run()
        self.assert_no_reset(board)
        reports = board.reports()
        press_index = next(i for i, (_, report) in enumerate(reports) if report == B_PRESSED)
        release_index = next(i for i in range(press_index + 1, len(reports)) if reports[i][1] == NEUTRAL)
        held_for = reports[release_index][0] - reports[press_index][0]
        self.assertGreaterEqual(held_for, 0.04)
        self.assertLess(held_for, 0.043)
        self.assertTrue(all(report == B_PRESSED for _, report in reports[press_index:release_index]))

    def test_only_heartbeats_cannot_renew_one_second_input_lease(self):
        board = FakeBoard([
            (0.0, ARM + FULL_STATE),
            (0.25, b"P\n"), (0.25, b"P\n"), (0.25, b"P\n"), (0.25, b"P\n"),
        ])
        board.run()
        self.assert_no_reset(board)
        self.assertEqual([(t, report) for t, report in board.reports() if t > 0.0], [
            (0.25, FULL_INPUT), (0.5, FULL_INPUT), (0.75, FULL_INPUT), (1.0, NEUTRAL),
        ])
        self.assertEqual([t for t, event, data in board.events if event == "send" and data == b"A\n"],
                         [0.25, 0.5, 0.75])
        self.assertEqual([event for event in board.events if event[0] == 1.0],
                         [(1.0, "hid", NEUTRAL), (1.0, "close", None)])

    def test_heartbeat_only_connections_without_an_input_lease_stay_open(self):
        heartbeats = [(0.75, b"P\n")] * 3
        for initial_receive in ([], [(0.0, ARM + FULL_STATE), (0.01, b"R\n")]):
            with self.subTest(after_release=bool(initial_receive)):
                board = FakeBoard(initial_receive + heartbeats)
                board.run()
                self.assert_no_reset(board)
                self.assertEqual([data for _, event, data in board.events if event == "send"],
                                 [b"H\n", b"A\n", b"A\n", b"A\n"])
                self.assertFalse(any(event == "close" for _, event, _ in board.events))
                self.assertEqual(board.reports()[-1][1], NEUTRAL)

    def test_expiry_discards_buffered_neutral_and_action_from_old_connection(self):
        board = FakeBoard(
            [(0.0, ARM + FULL_STATE), (1.0, b"T:0:0:0:0:0:3\nT:4:0:0:0:0:4\n")],
            [(0.125, b"T:4:0:0:0:0:1\n"), (0.125, b"T:0:0:1:0:0:1\n"),
             (0.125, ARM), (0.125, b"T:4:0:0:0:0:2\n")],
        )
        board.run()
        self.assert_no_reset(board)
        self.assertEqual([event for event in board.events if event[0] == 1.0],
                         [(1.0, "hid", NEUTRAL), (1.0, "close", None)])
        self.assertFalse(any(1.5 < t < 1.875 for t, _ in board.reports()))
        self.assertTrue(all(report == NEUTRAL for t, report in board.reports() if 1.0 <= t < 2.0))
        self.assertEqual(board.reports()[-1], (2.0, A_PRESSED))

    def test_expiry_discards_buffered_action_even_when_previous_state_was_neutral(self):
        board = FakeBoard([(0.0, ARM), (1.0, b"T:4:0:0:0:0:2\n")])
        board.run()
        self.assert_no_reset(board)
        self.assertEqual([event for event in board.events if event[0] == 1.0],
                         [(1.0, "hid", NEUTRAL), (1.0, "close", None)])
        self.assertTrue(all(report == NEUTRAL for _, report in board.reports()))

    def test_r_releases_immediately_even_inside_hold_and_resets_sequence(self):
        board = FakeBoard([
            (0.0, ARM + FULL_STATE),
            (0.01, b"R\n"),
            (0.01, ARM + b"T:2:0:0:0:0:2\n"),
        ])
        board.run()
        self.assert_no_reset(board)
        self.assertIn((0.0, FULL_INPUT), board.reports())
        self.assertEqual([report for t, report in board.reports() if t == 0.01], [NEUTRAL])
        self.assertEqual(board.reports()[-1], (0.02, B_PRESSED))

    def test_eof_immediately_releases_before_close_and_reconnect(self):
        board = FakeBoard(
            [(0.0, ARM + FULL_STATE), (0.01, b"")],
            [(0.01, b"T:2:0:0:0:0:3\n"), (0.01, ARM + b"T:2:0:0:0:0:2\n")],
        )
        board.run()
        self.assert_no_reset(board)
        self.assertIn((0.0, FULL_INPUT), board.reports())
        self.assertEqual([event for event in board.events if event[0] == 0.01],
                         [(0.01, "hid", NEUTRAL), (0.01, "close", None)])
        self.assertEqual([value for _, event, value in board.events if event == "send"], [b"H\n", b"H\n"])
        self.assertTrue(all(report == NEUTRAL for t, report in board.reports() if 0.01 <= t < 0.52))
        self.assertEqual(board.reports()[-1][1], B_PRESSED)

    def test_duplicate_older_and_out_of_range_sequences_do_not_renew_lease(self):
        too_large = b"T:2:0:0:0:0:" + str(2**53).encode() + b"\n"
        board = FakeBoard([
            (0.0, ARM + FULL_STATE),
            (0.25, b"T:2:0:0:0:0:2\n"),
            (0.25, b"T:2:0:0:0:0:1\n"),
            (0.25, too_large),
            (0.25, None),
        ])
        board.run()
        self.assert_no_reset(board)
        self.assertEqual([(t, report) for t, report in board.reports() if t > 0.0], [
            (0.25, FULL_INPUT), (0.5, FULL_INPUT), (0.75, FULL_INPUT), (1.0, NEUTRAL),
        ])

    def assert_watchdog_restart(self, board):
        self.assertIn("[safe] restart: watchdog timeout", board.stdout.getvalue())
        self.assertEqual([value for _, event, value in board.events if event == "mode"], ["NORMAL"])
        self.assertEqual([event for _, event, _ in board.events if event in ("mode", "reset")],
                         ["mode", "reset"])
        reset_index = next(i for i, (_, event, _) in enumerate(board.events) if event == "reset")
        self.assertEqual([value for _, event, value in board.events[:reset_index] if event == "hid"][-1],
                         NEUTRAL)

    def test_watchdog_timeout_is_not_swallowed_by_startup_feed_or_wifi(self):
        for fault in ("watchdog_start", "watchdog_feed", "wifi_status", "wifi_connect"):
            with self.subTest(fault=fault):
                board = FakeBoard(fault=fault)
                board.run()
                self.assert_watchdog_restart(board)

    def test_watchdog_timeout_releases_active_hid_before_normal_restart(self):
        board = FakeBoard([(0.0, ARM + FULL_STATE), (0.01, WatchDogTimeout())])
        board.run()
        self.assert_watchdog_restart(board)
        self.assertIn((0.0, FULL_INPUT), board.reports())
        self.assertEqual(board.reports()[-1], (0.01, NEUTRAL))


if __name__ == "__main__":
    unittest.main()
