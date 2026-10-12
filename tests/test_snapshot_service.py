"""通信隔离、PICK 显示门控和完赛后的快照行为。"""
import contextlib
from collections import deque
import io
import os
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import ensure_project_path, install_maix_stub, FakeSerial
ensure_project_path()
install_maix_stub()
import main
from link_service import Messages, LinkProcess, LinkWorker
from protocol import Protocol
from protocol_handler import ProtocolHandler


class SnapshotServiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_ack_after_display_and_no_success_ack_on_failure(self):
        order = []
        class Img:
            def draw_rect(self, *a, **kw):
                order.append("rect")
            def draw_string(self, *a, **kw):
                order.append("text")
            def draw_line(self, *a, **kw):
                order.append("picture")
        app = main.SortingVisionApp.__new__(main.SortingVisionApp)
        class Handler:
            finished = False
            pick_report_queue = deque([(0x21, 1, 2, 5, 0, 3)])
            def send_pick_ack(self, seq):
                order.append(("ack", seq))
            def report_pick_failed(self, seq):
                order.append(("failed", seq))
        app.protocol = Handler()
        app.camera_width = 320
        app._diag_event = lambda _: None
        app._pump = lambda *args: order.append("display_complete")
        app.disp = type("Display", (), {"show": lambda *_: order.append("show")})()
        self.assertTrue(app._handle_pick_display(Img()))
        self.assertIn("picture", order)
        self.assertLess(order.index("display_complete"), order.index(("ack", 0x21)))
        order.clear()
        app.protocol.pick_report_queue.append((0x22, 2, 1, 1, 0, 4))
        def broken(_):
            raise RuntimeError("display failed")
        app.disp.show = broken
        app._handle_pick_display(Img())
        self.assertFalse(any(isinstance(x, tuple) and x[0] == "ack" for x in order))

    def test_report_retries_do_not_skip_display(self):
        serial = FakeSerial()
        events = []
        h = ProtocolHandler(serial, lambda *args: events.append(args))
        payload = bytes([1, 2, 5, 0, 3, 0])
        h._handle_pick_report(0x21, payload)
        h._handle_pick_report(0x21, payload)
        self.assertEqual(len(events), 1)
        self.assertEqual(serial.written, [])
        h.send_pick_ack(0x21)
        h._handle_pick_report(0x21, payload)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(serial.written), 2)
        for i in range(h.MAX_PICK_QUEUE):
            h._handle_pick_report(i, payload)
        before = len(serial.written)
        h._handle_pick_report(0x40, payload)
        self.assertEqual(len(serial.written), before)

    def test_failed_ipc_delivery_allows_pick_report_retry(self):
        class Channel:
            accept = False
            def send(self, *_):
                return self.accept
        channel = Channel()
        serial = FakeSerial()
        worker = LinkWorker(serial, channel)
        payload = bytes([1, 2, 5, 0, 3, 0])
        worker.handler._handle_pick_report(0x21, payload)
        self.assertEqual(worker.dropped_events, 1)
        self.assertEqual(worker.handler.pick_report_queue, [])
        self.assertEqual(serial.written, [])
        channel.accept = True
        worker.handler._handle_pick_report(0x21, payload)
        self.assertEqual(len(worker.handler.pick_report_queue), 1)
        self.assertEqual(serial.written, [])

    def test_finish_and_recheck_do_not_replace_periodic_cache(self):
        h = ProtocolHandler(FakeSerial())
        h.publish_snapshot([(2, 5, 1, 100, 200, 65535)])
        old = h.snapshot_seq, h.snapshot_pieces
        h.send_recheck_result([], [], req_seq=0x31)
        h._handle_finish(0x40, bytes([1, 0, 1, 2, 0]))
        h.send_snapshot()
        self.assertEqual((h.snapshot_seq, h.snapshot_pieces), old)
        self.assertEqual(h.current_state, Protocol.STATE_FINISHED)
        self.assertEqual(h.serial.written[-1][3], 2)

    def test_proxy_keeps_recheck_requests_and_failed_snapshot_scan(self):
        proxy = LinkProcess()
        proxy._receive("recheck", [0x21, [[2, 5]]])
        proxy._receive("recheck", [0x22, [[1, 1]]])
        self.assertEqual(proxy.take_recheck_request(), (0x21, [(2, 5)]))
        self.assertTrue(proxy.recheck_requested)
        self.assertEqual(proxy.take_recheck_request(), (0x22, [(1, 1)]))
        self.assertFalse(proxy.recheck_requested)
        proxy._receive("finish", [1, 1, 10])
        app = main.SortingVisionApp.__new__(main.SortingVisionApp)
        app.protocol = proxy
        app._restore_state()
        self.assertEqual(proxy.current_state, Protocol.STATE_FINISHED)

        app._diag_event = lambda _: None
        app._execute_scan = lambda: (_ for _ in ()).throw(RuntimeError("inference failed"))
        img = type("Img", (), {"draw_string": lambda *a, **k: None})()
        app.disp = type("Display", (), {"show": lambda *_: None})()
        proxy.last_scan_pieces = [(2, 5, 100, 200, 65535)]
        dets, _ = app._handle_snapshot_scan(img)
        self.assertIsNone(dets)
        self.assertEqual(proxy.last_scan_pieces, [(2, 5, 100, 200, 65535)])

    def test_subprocess_publishes_while_parent_does_not_service_ipc(self):
        # 真正独立解释器；父进程模拟慢推理 4.3 秒，不调用 update/pump。
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        script = r'''
import socket,sys,os,time
from link_service import Messages,LinkWorker
sock = (socket.fromshare(bytes.fromhex(sys.stdin.readline().strip()))
        if os.name == "nt" else socket.socket(fileno=int(sys.argv[1])))
channel = Messages(sock)
class Serial:
    def available(self): return 0
    def write(self, frame):
        channel.send("wire", [time.monotonic(), list(frame)])
        return len(frame)
worker = LinkWorker(Serial(), channel)
while worker.tick(): time.sleep(0.01)
'''
        args = [sys.executable, "-u", "-B", "-c", script]
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                      cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if os.name == "nt":
            process = subprocess.Popen(args, stdin=subprocess.PIPE, **kwargs)
            process.stdin.write(child.share(process.pid).hex().encode() + b"\n")
            process.stdin.flush()
            process.stdin.close()
        else:
            process = subprocess.Popen(args + [str(child.fileno())],
                                       pass_fds=(child.fileno(),), **kwargs)
        child.close()
        channel = Messages(parent)
        try:
            time.sleep(4.3)
            messages = channel.receive()
            wire = [data for kind, data in messages if kind == "wire"]
            snaps = [(stamp, frame) for stamp, frame in wire if frame[3] == 2]
            self.assertGreaterEqual(len(snaps), 3)
            for (a, _), (b, _) in zip(snaps, snaps[1:]):
                self.assertLessEqual(b - a, 2.5)
            self.assertTrue(all(frame[5:9] == [0, 0, 0, 0] for _, frame in snaps))
            self.assertGreaterEqual(len([f for _, f in wire if f[3] == 1]), 12)
        finally:
            channel.send("stop")
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            stderr = process.stderr.read().decode("utf-8", "replace")
            process.stderr.close()
        self.assertEqual(process.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main()
