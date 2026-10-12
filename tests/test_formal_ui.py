"""正式界面的结果所有权、故障状态、采样隔离与屏显门控。"""
import contextlib
import io
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _stubs import ensure_project_path, install_maix_stub, FakeSerial
ensure_project_path()
maix, image, mtime = install_maix_stub()
import config
import main
import screen_ui
from link_service import LinkProcess, LinkWorker
from protocol_handler import ProtocolHandler, snapshot_details
from protocol import Protocol


class Frame:
    def __init__(self, number=0):
        self.number, self.ops = number, []
    def copy(self):
        result = Frame(self.number)
        result.ops = list(self.ops)
        return result
    def draw_rect(self, *args, **kw):
        self.ops.append(("rect", args, kw))
    def draw_string(self, *args, **kw):
        self.ops.append(("text", args, kw))
    def draw_line(self, *args, **kw):
        self.ops.append(("line", args, kw))


class Channel:
    closed = False
    tx = b""
    accept = True
    def __init__(self):
        self.sent, self.incoming = [], []
    def send(self, kind, data=None):
        if self.accept:
            self.sent.append((kind, data))
        return self.accept
    def receive(self):
        incoming, self.incoming = self.incoming, []
        return incoming


class UI:
    page = screen_ui.PAGE_MAIN
    def __init__(self):
        self.events = []
    def poll(self):
        return self.events.pop(0) if self.events else None
    def set_page(self, page):
        self.page = page
    def draw_buttons(self, img):
        img.draw_string(0, 216, "Details Hold: Exit")
    draw_result_panel = screen_ui.ScreenUI.draw_result_panel
    img_width, img_height, chinese = 320, 256, False


class FormalTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.now = 100.0
        self.stack.enter_context(patch.object(main.pytime, "monotonic", lambda: self.now))
        self.stack.enter_context(patch.object(mtime, "sleep_ms", lambda ms: setattr(self, "now", self.now + ms / 1000)))
        maix.app.set_exit_flag(False)
        self.addCleanup(maix.app.set_exit_flag, False)
        self.proxy = LinkProcess()
        self.channel = Channel()
        self.proxy.channel = self.channel
        self.obj = main.SortingVisionApp.__new__(main.SortingVisionApp)
        self.obj.__dict__.update(protocol=self.proxy, chinese=False, camera_width=320,
            camera_height=256, ui=UI(), result_image=None, result_detections=[],
            result_lines=[], result_title="Waiting", result_page=0, result_total_pages=1,
            result_version=0, result_completed_at=None, result_stamp="--:--:--",
            result_kind="initial", result_filter_stats={}, scan_error="", scan_error_count=0,
            recognizing=False, display_locked=False, deferred_buttons=[])
        self.shown = []
        self.obj.disp = types.SimpleNamespace(show=lambda img: self.shown.append(img))
        self.obj.vision = types.SimpleNamespace(last_scan_stats=dict(yolo_raw=0, roi_out=0,
            color_unknown=0), draw_roi=lambda img: img.draw_rect(50, 50, 100, 100))
        self.obj._diag_event = lambda text: None
        self.det = ("cube", "blue", .5, .5, (100, 80, 20, 20), .9)

    def scan(self, dets):
        self.obj._execute_scan = lambda: (dets, Frame(17))
        return self.obj._handle_snapshot_scan(Frame())

    def text(self, frame):
        return " ".join(op[1][2] for op in frame.ops if op[0] == "text")

    def test_initial_empty_and_failed_are_distinct(self):
        self.assertIn("first", self.obj._result_message())
        self.scan([])
        self.assertEqual(self.obj.result_kind, "empty")
        self.assertIn("No material", self.obj._result_message())
        self.assertIsNotNone(self.obj.result_completed_at)
        old = self.obj.result_image, self.obj.result_version
        self.obj._execute_scan = lambda: (_ for _ in ()).throw(RuntimeError("NPU failure"))
        self.obj._handle_snapshot_scan(Frame())
        self.assertEqual((self.obj.result_image, self.obj.result_version), old)
        self.assertIn("Recognition failed", self.obj._result_message())
        self.scan([self.det])
        self.assertEqual(self.obj.scan_error, "")
        self.assertEqual(self.obj.scan_error_count, 1)

    def test_excluded_items_have_reason_names_and_mm(self):
        dets = [("cube", "unknown", .5, .5, (1, 1, 5, 5), .8),
                ("cube", "blue", 0, .5, (1, 1, 5, 5), .8)]
        self.scan(dets)
        self.assertEqual(self.obj.result_kind, "filtered")
        self.assertIn("All excluded", self.obj._result_message())
        text = " ".join(self.obj.result_lines)
        self.assertIn("unknown color", text)
        self.assertIn("out of bounds", text)
        self.assertIn("X=-20.0mm", text)
        self.assertNotIn("S:", text)
        self.scan([self.det] * 13)
        self.assertEqual(self.obj.result_filter_stats["truncated"], 1)
        self.assertIn("over 12 items", " ".join(self.obj.result_lines))

    def test_page_preserved_for_12_items_and_clamped_when_shrinking(self):
        self.scan([self.det] * 12)
        self.obj.result_page = 2
        self.scan([self.det] * 12)
        self.assertEqual(self.obj.result_page, 2)
        self.obj.ui.set_page(screen_ui.PAGE_RESULT)
        rendered = self.obj._render_result(Frame(99))
        self.assertIn("3/3", self.text(rendered))
        self.scan([self.det])
        self.assertEqual(self.obj.result_page, 0)

    def test_render_uses_sample_copy_and_does_not_contaminate_cache(self):
        self.scan([self.det])
        raw = Frame(99)
        frame1 = self.obj._render_result(raw)
        frame2 = self.obj._render_result(Frame(100))
        self.assertEqual(frame1.number, 17)
        self.assertEqual(frame2.number, 17)
        self.assertEqual(raw.ops, [])
        self.assertEqual(self.obj.result_image.ops, [])
        self.assertIsNot(frame1, frame2)
        self.assertIn("Blue Cube Pending", self.text(frame1))

    def test_actual_last_sample_retained_no_extra_camera_read(self):
        frames = [Frame(i) for i in range(5)]
        reads = []
        def read():
            frame = frames[len(reads)]
            reads.append(frame)
            return frame
        def stable(read_frame, service_protocol, wait_ms):
            for idx in range(5):
                service_protocol()
                self.assertEqual(read_frame().ops, [])
                service_protocol()
                if idx < 4:
                    wait_ms(50)
            return [self.det]
        self.obj.cam = types.SimpleNamespace(read=read)
        self.obj.vision.detect_objects_stable = stable
        with patch.object(config, "SCAN_SETTLE_MS", 0), patch.object(config, "SCAN_FLUSH_FRAMES", 0):
            dets, frame = self.obj._execute_scan()
        self.assertEqual(len(reads), 5)
        self.assertEqual(frame.number, 4)
        self.assertIsNot(frame, frames[-1])
        self.assertAlmostEqual(self.now, 100.2)
        self.assertEqual(dets, [self.det])

    def test_send_state_is_successful_version_not_heartbeat_or_ipc(self):
        self.scan([self.det])
        self.proxy.link_alive = True
        self.assertIn("Pending", self.obj._send_status()[0])
        worker_channel = Channel()
        worker = LinkWorker(FakeSerial(ret=-1), worker_channel)
        worker_channel.incoming = [m for m in self.channel.sent if m[0] == "snapshot"]
        worker.tick()
        status = next(data for kind, data in worker_channel.sent if kind == "status")
        self.proxy._receive("status", status)
        self.assertTrue(self.obj._send_status()[1])
        worker.handler.serial.ret = None
        self.now += 2.0
        worker.tick()
        status = [data for kind, data in worker_channel.sent if kind == "status"][-1]
        self.proxy._receive("status", status)
        self.assertIn("Written to UART", self.obj._send_status()[0])
        self.assertEqual(self.proxy.write_error_count, 2)  # 心跳和快照
        self.assertEqual(self.proxy.snapshot_write_error, "")
        self.now += 16
        stale = self.obj._render_result(Frame())
        self.assertIn("STALE", self.text(stale))
        worker.handler.send_snapshot()
        self.assertEqual(worker.handler.snapshot_result_version, 1)

    def test_ipc_failure_visible_and_recoverable(self):
        self.channel.accept = False
        self.scan([self.det])
        self.assertIn("IPC backlog", self.obj._send_status()[0])
        self.assertEqual(self.obj.result_version, 1)
        self.channel.accept = True
        self.scan([self.det])
        self.assertEqual(self.proxy.ipc_error, "")

    def test_recheck_does_not_replace_home_or_detail_and_finish_persists(self):
        self.scan([self.det])
        old = self.obj.result_image, list(self.obj.result_lines), list(self.proxy.last_scan_pieces)
        self.proxy.send_recheck_result([], [], 0x22)
        self.assertEqual((self.obj.result_image, self.obj.result_lines, self.proxy.last_scan_pieces), old)
        self.proxy._receive("finish", [2, 3, 90])
        self.scan([self.det])
        self.assertIn("Finished", self.text(self.obj._render_result(Frame())))
        self.assertEqual(self.proxy.finish_summary, (2, 3, 90))
        self.proxy._receive("start", None)
        self.assertIsNone(self.proxy.finish_summary)

    def test_wait_exit_and_deferred_page_change_interrupt_pick_without_ack(self):
        self.proxy.pick_report_queue.append((0x21, 1, 1, 5, 0, 1))
        self.obj.ui.events = ["result", "exit"]
        self.obj._handle_pick_display(Frame())
        self.assertTrue(maix.app.need_exit())
        self.assertEqual(self.obj.ui.page, screen_ui.PAGE_MAIN)
        self.assertEqual(self.obj.deferred_buttons, ["result"])
        self.assertNotIn("pick_ack", [kind for kind, data in self.channel.sent])
        self.assertIn(("pick_failed", 0x21), self.channel.sent)
        self.assertLess(self.now, 100.5)

    def test_pick_has_four_visible_elements_then_ack_and_different_pictures(self):
        self.proxy.pick_report_queue.append((0x21, 7, 1, 5, 0, 8))
        self.obj._handle_pick_display(Frame())
        self.assertIn(("pick_ack", 0x21), self.channel.sent)
        self.assertGreaterEqual(self.now, 100.5)
        text = self.text(self.shown[0])
        for value in ("Item: 7", "Blue Cone", "Total OK: 8", "Success"):
            self.assertIn(value, text)
        self.assertTrue(any(op[0] == "line" for op in self.shown[0].ops))
        cone, pyramid = Frame(), Frame()
        self.obj._draw_piece_picture(cone, 1)
        self.obj._draw_piece_picture(pyramid, 3)
        self.assertNotEqual(cone.ops, pyramid.ops)

    def test_font_failure_has_complete_english_fallback(self):
        with patch.object(image, "load_font", side_effect=RuntimeError("font missing"), create=True), \
             patch.object(image, "set_default_font", return_value=0, create=True):
            self.assertFalse(screen_ui.load_chinese_font("missing.otf"))
        self.scan([self.det])
        frame = self.obj._render_result(Frame())
        self.assertTrue(self.text(frame).isascii())
        self.assertEqual(screen_ui.names(3, 1, False), ("Red", "Pyramid"))


if __name__ == "__main__":
    unittest.main()
