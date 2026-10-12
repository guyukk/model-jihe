"""v3.1 黄金帧、快照号/帧序号、调度、坐标域与截断。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import protocol_handler as ph
from protocol import Protocol


class Serial:
    def __init__(self):
        self.written = []
    def write(self, data):
        self.written.append(bytes(data))
        return len(data)
    def available(self):
        return 0


class SnapshotTests(unittest.TestCase):
    def test_document_vectors(self):
        p = Protocol()
        pieces = [(1, 1, 1, 120, 80, 65535), (2, 5, 1, 950, 1700, 65535),
                  (6, 4, 0, -200, 300, 65535)]
        frame, _ = p.encode_snapshot(7, pieces, seq=3)
        self.assertEqual(frame, bytes.fromhex(
            "aa 55 22 02 03 07 03 00 03 01 01 01 00 78 00 50 00 ff ff "
            "02 05 01 00 b6 03 a4 06 ff ff 06 04 00 00 38 ff 2c 01 ff ff 7c"))
        self.assertEqual(p.encode_snapshot(8, [], seq=4)[0],
            bytes.fromhex("aa 55 04 02 04 08 00 00 00 a6"))
        self.assertEqual(p.encode_snapshot(9, pieces[1:2], seq=5)[0],
            bytes.fromhex("aa 55 0e 02 05 09 01 00 01 02 05 01 00 b6 03 a4 06 ff ff d1"))
        full, _ = p.encode_snapshot(10, [(1, 1, 1, 100+i, 200+i, 65535)
                                        for i in range(12)], seq=6)
        self.assertEqual(len(full), 130)
        self.assertEqual(full[-1], 0xc9)
        self.assertEqual(p.parse_frame(full)[2][:4], bytes([10, 12, 0, 12]))

    def test_one_frame_limit(self):
        p = Protocol()
        frame, _ = p.encode_snapshot(1, [(1, 1, 1, 100, 200, 65535)] * 20)
        self.assertEqual(frame[2], 124)
        self.assertEqual(p.parse_frame(frame)[2][:4], bytes([1, 12, 0, 12]))

    def test_periodic_cache_independent_of_ack_and_new_result(self):
        serial = Serial()
        now = [1000.0]
        with patch.object(ph.pytime, "monotonic", lambda: now[0]):
            h = ph.ProtocolHandler(serial)
            h.update()  # 无 SYS_START、无 MCU 数据也立即发空快照。
            initial = [f for f in serial.written if f[3] == 2]
            self.assertEqual(len(initial), 1)
            self.assertEqual(initial[0][5:9], b"\x00\x00\x00\x00")
            pieces = [[2, 5, 1, 950, 1700, 65535]]
            h.publish_snapshot(pieces)
            pieces[0][3] = 123  # 缓存必须脱离调用方的可变对象。
            now[0] += 2
            h.update()
            now[0] += 2
            h.update()  # 没有新检测、没有 ACK，内容和快照号仍不变。
            snapshots = [f for f in serial.written if f[3] == 2]
            self.assertEqual(len(snapshots), 3)
            self.assertEqual(snapshots[1][5:-1], snapshots[2][5:-1])
            self.assertNotEqual(snapshots[1][4], snapshots[2][4])
            self.assertEqual(snapshots[1][5], 1)
            self.assertEqual(h.snapshot_repeat_count, 1)
            h.snapshot_seq = 255
            h.publish_snapshot([])
            self.assertEqual(h.snapshot_seq, 0)
            now[0] += 2
            h.update()
            self.assertEqual(serial.written[-1][5:9], b"\x00\x00\x00\x00")

    def test_shared_counter_wrap_and_request_version(self):
        h = ph.ProtocolHandler(Serial())
        h.proto.seq = 255
        h.send_heartbeat()
        h.send_snapshot()
        self.assertEqual([f[4] for f in h.serial.written], [255, 0])
        h._handle_sys_start(0x44, b"\x01\x02")
        self.assertEqual(h.serial.written[-1][3:7], b"\xc2\x44\x00\x02")

    def test_coordinate_bounds_without_clamping(self):
        class Converter:
            def normalized_to_mechanical(self, x, y):
                return x, y
        dets = [("cube", "blue", x, y, (), 0.9) for x, y in
                [(0, 0), (1800, 2500), (-1, 100), (1801, 100), (100, 2501)]]
        dets.append(("not-trained", "red", 100, 100, (), 0.9))
        pieces, stats = ph.prepare_snapshot_pieces(dets, Converter())
        self.assertEqual(len(pieces), 3)
        self.assertEqual(stats["invalid_coord"], 3)
        self.assertEqual(pieces[-1][:3], (6, 1, 0))
        self.assertEqual([p[3:5] for p in pieces[:2]], [(0, 0), (1800, 2500)])


if __name__ == "__main__":
    unittest.main()
