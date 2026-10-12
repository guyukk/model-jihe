"""旧扫描队列退场：v3.1 收到任何 0x43 都不扫描、不应答。"""
import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import ensure_project_path, FakeSerial
ensure_project_path()
from protocol_handler import ProtocolHandler

class ObsoleteRequestTests(unittest.TestCase):
    def test_burst_and_truncated_requests_do_not_trigger_results_or_alarms(self):
        serial = FakeSerial()
        events = []
        h = ProtocolHandler(serial, lambda *event: events.append(event))
        for i in range(20):
            frame, _ = h.proto.build_frame(0x43, b"" if i % 2 else bytes([1, 0]), seq=i)
            serial.feed(frame)
        h.process_incoming()
        self.assertEqual(h.cmd_counts[0x43], 20)
        self.assertEqual(h.scan_count, 0)
        self.assertEqual(serial.written, [])
        self.assertEqual(events, [])

if __name__ == "__main__":
    unittest.main()
