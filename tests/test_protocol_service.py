"""通信分片的集成回归：虚拟时钟、事务隔离与实际接收处理器。"""
import contextlib
import io
import os
import time as realtime
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import ensure_project_path, install_maix_stub, FakeSerial

ensure_project_path()
maix, _image, mtime = install_maix_stub()
import config
import main
import protocol
import protocol_handler
import vision


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep_ms(self, ms):
        self.sleeps.append(ms)
        self.now += ms / 1000.0


class ProtocolServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(main, "pytime", types.SimpleNamespace(
            monotonic=lambda: self.clock.now,
            # _pump 必须独立于可能跳变的墙上时钟。
            time=lambda: -99999, strftime=realtime.strftime, localtime=realtime.localtime)))
        self.stack.enter_context(patch.object(mtime, "sleep_ms", self.clock.sleep_ms))
        self.stack.enter_context(patch.object(maix.app, "_exit", False))
        for key, value in {"SCAN_DEBUG_LOG": False, "SCAN_FRAME_COUNT": 5,
                           "SCAN_MIN_HITS": 3, "SCAN_FRAME_DELAY_MS": 50}.items():
            self.stack.enter_context(patch.object(config, key, value))

    def make_app(self, handler):
        obj = main.SortingVisionApp.__new__(main.SortingVisionApp)
        obj.protocol = handler
        return obj

    def make_vision(self, script):
        obj = vision.VisionDetector.__new__(vision.VisionDetector)
        frames = iter(script)

        def detect(_img):
            self.clock.now += 0.011  # 模拟单次推理；不在其内部服务协议。
            dets = next(frames)
            obj.last_scan_stats = dict(yolo_raw=len(dets), roi_out=0,
                                       color_unknown=0, accepted=len(dets))
            return dets

        obj.detect_objects = detect
        return obj

    def test_frame_waits_and_service_preserve_voting_and_enqueue(self):
        handler = protocol_handler.ProtocolHandler(FakeSerial())
        app = self.make_app(handler)
        sample_times, updates, waits = [], [], []
        det = ("cube", "blue", 0.5, 0.5, (100, 100, 20, 20), 0.9)
        detector = self.make_vision([[det]] * 5)
        original_update = handler.update

        def update():
            updates.append(self.clock.now)
            if len(updates) == 3:  # 第一帧后的等待中到达。
                frame, _ = handler.proto.build_frame(0x43, b"\x01\x00", seq=0x22)
                handler.serial.feed(frame)
            original_update()

        handler.update = update

        def read():
            sample_times.append(self.clock.now)
            self.clock.now += 0.007

        def wait(ms):
            begin = self.clock.now
            app._pump(ms)
            waits.append((ms, self.clock.now - begin))

        result = detector.detect_objects_stable(read, update, wait)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], det)
        self.assertEqual(len(sample_times), 5)
        self.assertEqual([ms for ms, _ in waits], [50] * 4)
        for _, elapsed in waits:
            self.assertAlmostEqual(elapsed, 0.05, places=3)
        for first, second in zip(sample_times, sample_times[1:]):
            self.assertAlmostEqual(second - first, 0.068, places=3)
        self.assertGreaterEqual(len(updates), 10)
        self.assertLessEqual(max(b - a for a, b in zip(updates, updates[1:])), 0.031)
        self.assertEqual(handler.scan_count, 0)
        self.assertEqual(handler.cmd_counts[0x43], 1)

    def test_without_callbacks_uses_original_waits(self):
        detector = self.make_vision([[]] * 5)
        self.assertEqual(detector.detect_objects_stable(lambda: None), [])
        self.assertEqual(self.clock.sleeps, [50] * 4)

    def test_main_passes_service_and_wait_callbacks(self):
        updates = []
        handler = types.SimpleNamespace(update=lambda: updates.append(True))
        obj = self.make_app(handler)
        img = types.SimpleNamespace(draw_string=lambda *a, **k: None)
        copied = types.SimpleNamespace()
        img.copy = lambda: copied
        obj.cam = types.SimpleNamespace(read=lambda: img)
        obj.disp = types.SimpleNamespace(show=lambda _img: None)
        observed = []

        def stable(read_frame, service_protocol=None, wait_ms=None):
            self.assertIs(read_frame(), img)
            service_protocol()
            wait_ms(50)
            return []

        obj.vision = types.SimpleNamespace(detect_objects_stable=stable,
            last_scan_stats=dict(yolo_raw=0, roi_out=0, color_unknown=0))
        with patch.object(config, "SCAN_SETTLE_MS", 0), \
                patch.object(config, "SCAN_FLUSH_FRAMES", 0), \
                patch.object(config, "SCAN_MULTI_FRAME", True):
            self.assertEqual(obj._execute_scan(), ([], copied))
        self.assertGreater(len(updates), 1)

        def send(dets):
            observed.append(dets)
            return 0

        handler.finished = False
        handler.publish_snapshot = send
        obj._execute_scan = lambda: ([], img)
        obj._handle_snapshot_scan(img)
        self.assertEqual(observed, [[]])

    def test_pump_uses_monotonic_and_exits_promptly(self):
        calls = []
        obj = self.make_app(types.SimpleNamespace(update=lambda: calls.append(self.clock.now)))
        begin = self.clock.now
        obj._pump(120)
        self.assertGreaterEqual(self.clock.now - begin, 0.12 - 1e-9)
        self.assertLessEqual(self.clock.now - begin, 0.121 + 1e-9)
        self.assertGreaterEqual(len(calls), 4)
        calls.clear()
        self.clock.sleeps.clear()

        def stop():
            calls.append(self.clock.now)
            maix.app.set_exit_flag(True)

        obj.protocol.update = stop
        obj._pump(2000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.clock.sleeps, [])
        obj._pump(2000)
        self.assertEqual(len(calls), 1)

    def test_handler_recovers_good_frame_once_after_stale_header(self):
        handler = protocol_handler.ProtocolHandler(FakeSerial())
        handler.proto._now_ms = lambda: int(self.clock.now * 1000)
        frame, _ = handler.proto.build_frame(0x4f, b"\x01\x00", seq=0x21)
        handler.serial.feed(b"\xaa\x55\x80\x00\x00\x00" + frame)
        handler.process_incoming()
        self.assertEqual(handler.scan_count, 0)
        self.clock.now += 0.301
        handler.process_incoming()
        self.assertEqual(len([f for f in handler.serial.written if f[3] == 0xcf]), 1)
        self.assertEqual(handler.rx_frames_total, 1)
        self.assertEqual(handler.proto.resync_stale_count, 1)
        handler.process_incoming()
        self.assertEqual(len([f for f in handler.serial.written if f[3] == 0xcf]), 1)

    def test_handler_does_not_execute_nested_frame_before_outer_crc(self):
        handler = protocol_handler.ProtocolHandler(FakeSerial())
        handler.proto._now_ms = lambda: int(self.clock.now * 1000)
        inner, _ = handler.proto.build_frame(0x43, b"\x01\x00", seq=0x77)
        outer, _ = handler.proto.build_frame(0x4f, inner, seq=0x11)
        handler.serial.feed(outer[:-1])
        handler.process_incoming()
        self.assertEqual(handler.rx_frames_total, 0)
        self.clock.now += 0.1
        handler.serial.feed(outer[-1:])
        handler.process_incoming()
        self.assertEqual(handler.rx_frames_total, 1)
        self.assertEqual(handler.scan_count, 0)
        reply = handler.proto.parse_frame(handler.serial.written[-1])
        self.assertEqual(reply, (0xcf, 0x11, b"\x00" + inner))


if __name__ == "__main__":
    unittest.main()
