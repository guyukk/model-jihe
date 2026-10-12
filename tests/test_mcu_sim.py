"""PC MCU simulator: transport, protocol, business, timing and output evidence."""
import contextlib
import io
import json
from pathlib import Path
import queue
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.mcu_sim import (AsyncOutput, Receiver, Simulator, decode_pieces, render,
                           run_serial, main, build_parser)
from protocol import Protocol


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def advance(self, amount):
        self.now += amount


PIECE = (2, 5, 1, 0, 950, 1700, 65535)


def snapshot(number=1, pieces=None, seq=10):
    pieces = [PIECE] if pieces is None else pieces
    payload = bytes([number, len(pieces), 0, len(pieces)])
    payload += b"".join(struct.pack("<BBBBhhH", *p) for p in pieces)
    return Protocol().build_frame(2, payload, seq)[0]


class SimTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.wire = []
        def write(data):
            self.wire.append(bytes(data))
            return len(data)
        self.sim = Simulator(write, self.events.append, self.clock)

    def frame(self, cmd, payload=b"\x00", seq=None):
        seq = self.sim.pending.seq if seq is None else seq
        self.sim.receive(Protocol().build_frame(cmd, payload, seq)[0])

    def test_golden_frames_strict_validation_and_cap(self):
        # Actual external golden bytes, not generated using the simulator codec.
        golden = bytes.fromhex("aa 55 0e 02 05 09 01 00 01 02 05 01 00 b6 03 a4 06 ff ff d1")
        self.sim.receive(golden)
        self.assertEqual(self.sim.latest, (9, [PIECE]))
        self.sim.tick()
        self.assertTrue(any(f[3:5] == b"\x82\x05" for f in self.wire))
        self.sim.receive(bytes.fromhex("aa 55 04 02 04 08 00 00 00 a6"))
        self.assertEqual(self.sim.latest, (8, []))
        self.sim.receive(snapshot(10, [PIECE] * 12))
        self.assertEqual(len(self.sim.latest[1]), 12)
        invalid = [bytes([2, 1, 1, 1]) + struct.pack("<BBBBhhH", *PIECE),
                   bytes([2, 0, 0, 1]) + struct.pack("<BBBBhhH", *PIECE),
                   bytes([2, 13, 0, 13]) + struct.pack("<BBBBhhH", *PIECE) * 13]
        # Last malformed payload exceeds protocol LEN and cannot be encoded.
        for payload in invalid[:2]:
            self.sim.receive(Protocol().build_frame(2, payload, 40)[0])
        self.assertEqual(self.sim.counts["invalid_frames"], 2)
        for field, value in [(0, 7), (1, 0), (2, 2), (3, 1), (4, -1), (5, 2501), (6, 0)]:
            p = list(PIECE)
            p[field] = value
            with self.assertRaises(ValueError):
                decode_pieces(struct.pack("<BBBBhhH", *p), 0, 1)
        with self.assertRaises(ValueError):
            decode_pieces(b"" * 0, 0, 13)

    def test_parser_fragment_noise_crc_stale_and_embedded_frames(self):
        receiver = Receiver()
        valid = snapshot()
        self.assertEqual(receiver.feed(b"noise" + valid[:1], 1.0), [])
        frames = receiver.feed(valid[1:] + valid, 1.1)
        self.assertEqual([s for s, _ in frames], [1.0, 1.1])
        bad = valid[:-1] + bytes([valid[-1] ^ 1])
        self.assertEqual(receiver.feed(bad + valid, 2)[0][1], valid)
        self.assertEqual(receiver.counts["crc_errors"], 1)
        false_header = b"\xaa\x55\x80\x4f\x00"
        self.assertEqual(receiver.feed(false_header + valid, 3), [])
        self.assertEqual(receiver.feed(b"", 3.301)[0][1], valid)
        outer = Protocol().build_frame(0xCF, valid, 7)[0]
        self.assertEqual(receiver.feed(outer[:-1], 4), [])
        self.assertEqual(receiver.feed(outer[-1:], 4.1)[0][1], outer)
        self.assertEqual(receiver.buffer, b"")

    def test_repeat_wrap_stagnation_and_recovery(self):
        self.sim.profile = "snapshots"
        self.sim.receive(snapshot(255))
        self.sim.tick()
        for number in range(1, 10):
            self.clock.advance(2)
            self.sim.receive(snapshot(255))
            self.sim.tick()
        self.assertEqual(self.sim.counts["recognition_stalls"], 1)
        self.assertEqual(self.sim.counts["link_interruptions"], 0)
        changed = list(PIECE)
        changed[4] += 1
        self.sim.receive(snapshot(255, [changed]))
        self.assertEqual(self.sim.counts["invalid_frames"], 1)
        self.clock.advance(0.1)
        self.sim.receive(snapshot(0))
        self.sim.tick()
        self.assertFalse(self.sim.flags["stagnant"])
        self.assertTrue(any(e["kind"] == "RECOVER" for e in self.events))
        self.clock.advance(2.6)
        self.sim.tick()
        self.assertEqual(self.sim.counts["snapshot_overdue"], 1)
        self.assertEqual(self.sim.counts["link_interruptions"], 1)

    def test_business_retry_sequence_isolation_and_background_traffic(self):
        self.sim.receive(snapshot())
        self.sim.tick()
        tx = self.sim.pending
        self.assertEqual(tx.cmd, 0x42)
        self.frame(0xC2, b"\x00\x02", (tx.seq + 1) & 255)
        self.assertIs(self.sim.pending, tx)
        self.clock.advance(1.01)
        self.sim.receive(snapshot(2))
        self.sim.tick()
        starts = [f for f in self.wire if f[3] == 0x42]
        self.assertEqual(starts, [tx.frame, tx.frame])
        self.assertGreaterEqual(len([f for f in self.wire if f[3] == 0x41]), 2)
        self.assertGreaterEqual(len([f for f in self.wire if f[3] == 0x82]), 2)
        self.frame(0xC2, b"\x00\x02")
        self.sim.tick()
        self.assertEqual(self.sim.pending.cmd, 0x44)
        pick_seq = self.sim.pending.seq
        self.frame(0xC4)
        self.sim.tick()
        self.assertEqual(self.sim.pending.cmd, 0x45)
        self.frame(0xC4, seq=pick_seq)  # old ACK cannot advance RECHECK
        self.assertEqual(self.sim.pending.cmd, 0x45)
        self.frame(3, b"\x01\x01" + struct.pack("<BBBBhhH", *PIECE))
        self.sim.tick()
        self.assertEqual(self.sim.pending.cmd, 0x48)
        self.frame(0xC8)
        self.sim.tick()
        self.assertEqual(self.sim.counts["cycles_completed"], 1)
        self.assertEqual(self.sim.counts["transaction_ok"], 4)
        self.assertFalse(any(f[3] == 0x43 for f in self.wire))

    def test_timeout_exhaustion_invalid_ack_and_empty_coverage(self):
        self.sim.receive(snapshot(1, []))
        self.sim.tick()
        self.frame(0xC2, b"\x00")  # invalid length, must still wait
        self.assertIsNotNone(self.sim.pending)
        for _ in range(4):
            self.clock.advance(1.01)
            self.sim.tick()
        self.assertEqual(self.sim.counts["transaction_failed"], 1)
        self.assertEqual(self.sim.counts["retries"], 3)
        self.assertFalse(self.sim.summary()["pick_display_covered"])

    def test_exact_tx_bytes_partial_writes_do_not_interleave(self):
        actual = bytearray()
        def short(data):
            n = min(3, len(data))
            actual.extend(data[:n])
            return n
        self.sim.write = short
        self.sim.profile = "snapshots"
        frame, _ = self.sim._queue(0x41, b"\x01\x00\x00")
        for _ in range(3):
            self.sim._flush()
        self.assertEqual(bytes(actual), frame)
        logged = b"".join(bytes.fromhex(e["fields"]["hex"]) for e in self.events if e["kind"] == "TX")
        self.assertEqual(logged, bytes(actual))
        self.assertEqual(self.sim.counts["tx_frames"], 1)
        self.sim.write = lambda _: -1
        self.sim._queue(0x41, b"\x01\x00\x00")
        self.sim._flush()
        self.assertEqual(self.sim.counts["write_errors"], 1)

    def test_render_milliseconds_names_and_millimetres(self):
        text = render({"t": 2.014, "kind": "RX", "message": "SNAPSHOT", "fields": {"pieces": [PIECE]}})
        self.assertIn("00:00:02.014", text)
        self.assertIn("蓝色立方", text)
        self.assertIn("X=95.0mm Y=170.0mm", text)

    def test_virtual_thirty_minutes_business_and_summary(self):
        wire = []
        sim = Simulator(lambda b: wire.append(bytes(b)) or len(b), lambda _: None, self.clock)
        next_snap = next_hb = 0
        number = 0
        vision = Protocol()
        for step in range(36001):
            self.clock.now = step * 0.05
            if self.clock() >= next_hb:
                sim.receive(vision.build_frame(1, b"\x01\x00\x00")[0])
                next_hb = self.clock() + 0.25
            if self.clock() >= next_snap:
                number = (number + 1) & 255
                sim.receive(snapshot(number))
                next_snap = self.clock() + 2
            sim.tick()
            for frame in wire:
                cmd, seq = frame[3], frame[4]
                if cmd == 0x42:
                    sim.receive(vision.build_frame(0xC2, b"\x00\x02", seq)[0])
                elif cmd == 0x44:
                    sim.receive(vision.build_frame(0xC4, b"\x00", seq)[0])
                elif cmd == 0x45:
                    sim.receive(vision.build_frame(3, b"\x01\x01" + struct.pack("<BBBBhhH", *PIECE), seq)[0])
                elif cmd == 0x48:
                    sim.receive(vision.build_frame(0xC8, b"\x00", seq)[0])
            wire.clear()
        summary = sim.summary()
        self.assertEqual(summary["duration_s"], 1800)
        self.assertEqual(summary["protocol"], "PASS")
        self.assertEqual(summary["business"], "PASS")
        self.assertEqual(summary["recognition_progress"], "OBSERVED")
        self.assertEqual(summary["snapshot_cadence"], "OBSERVED")
        self.assertGreaterEqual(sim.counts["cycles_completed"], 29)
        self.assertGreaterEqual(sim.counts["snapshots"], 900)

    def test_current_project_handler_interoperability(self):
        # Exercise the actual vision protocol implementation, including display-gated ACK.
        from protocol_handler import ProtocolHandler
        class VisionPort:
            incoming = bytearray()
            outgoing = bytearray()
            def available(self): return len(self.incoming)
            def read(self, n):
                value = bytes(self.incoming[:n])
                del self.incoming[:n]
                return value
            def write(self, data):
                self.outgoing.extend(data)
                return len(data)
        port = VisionPort()
        def pc_write(data):
            port.incoming.extend(data)
            return len(data)
        sim = Simulator(pc_write, lambda _: None, self.clock)
        with patch("protocol_handler.pytime.monotonic", self.clock), contextlib.redirect_stdout(io.StringIO()):
            vision = ProtocolHandler(port)
            vision.publish_snapshot([(2, 5, 1, 950, 1700, 65535)])
            for step in range(401):
                self.clock.now = step * 0.01
                vision.update()
                if vision.pick_report_queue:
                    vision.send_pick_ack(vision.pick_report_queue[0][0])
                if vision.recheck_requested:
                    vision.recheck_requested = False
                    vision.send_recheck_result(vision.recheck_expected, [], vision.recheck_req_seq)
                if port.outgoing:
                    sim.receive(bytes(port.outgoing))
                    port.outgoing.clear()
                sim.tick()
        self.assertEqual(sim.counts["transaction_ok"], 4)
        self.assertEqual(sim.counts["cycles_completed"], 1)
        self.assertTrue(sim.summary()["pick_display_covered"])
        self.assertEqual(sim.counts["recheck_mismatches"], 1)
        self.assertEqual(vision.current_state, Protocol.STATE_FINISHED)


class OutputTests(unittest.TestCase):
    def test_persist_raw_and_decoded_no_ansi_when_redirected(self):
        with tempfile.TemporaryDirectory() as folder:
            stream = io.StringIO()
            out = AsyncOutput(folder, stream=stream)
            out.emit({"t": 0.019, "kind": "RX_RAW", "message": "2B: AA 55", "fields": {"hex": "AA 55"}})
            out.emit({"t": 1, "kind": "RX", "message": "SNAPSHOT", "fields": {"pieces": [PIECE]}})
            out.close()
            events = [json.loads(line) for line in (Path(folder) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 2)
            self.assertIn("蓝色立方", stream.getvalue())
            self.assertNotIn("\033", stream.getvalue())
            self.assertEqual(out.metrics()["disk_dropped"], 0)

    def test_queue_overflow_is_bounded_and_evidence_incomplete(self):
        out = AsyncOutput.__new__(AsyncOutput)
        out.quiet = False
        out.disk_queue = queue.Queue(maxsize=1)
        out.console_queue = queue.Queue(maxsize=1)
        out.disk_dropped = out.console_dropped = out.disk_errors = out.console_errors = 0
        out.threads = []
        event = {"t": 0, "kind": "RX", "message": "test", "fields": {}}
        out.emit(event)
        out.emit(event)
        self.assertEqual(out.disk_dropped, 1)
        self.assertEqual(out.console_dropped, 1)
        sim = Simulator(lambda b: len(b), lambda _: None)
        self.assertFalse(sim.summary(out.metrics())["evidence_complete"])

    def test_quiet_console_retains_errors_and_disk_keeps_all(self):
        with tempfile.TemporaryDirectory() as folder:
            stream = io.StringIO()
            out = AsyncOutput(folder, quiet=True, stream=stream)
            for kind in ("RX", "ERROR", "STATUS"):
                out.emit({"t": 0, "kind": kind, "message": kind, "fields": {}})
            out.close()
            self.assertNotIn("] RX", stream.getvalue())
            self.assertIn("] ERROR", stream.getvalue())
            self.assertEqual(len((Path(folder) / "events.jsonl").read_text(encoding="utf-8").splitlines()), 3)

    def test_disk_failure_and_slow_console_do_not_block_uart_output(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "events.jsonl").mkdir()  # opening a directory for append must fail
            out = AsyncOutput(folder, stream=io.StringIO())
            out.emit({"t": 0, "kind": "RX", "message": "data", "fields": {}})
            out.close()
            self.assertGreater(out.metrics()["disk_errors"], 0)
        entered = threading.Event()
        release = threading.Event()
        class SlowConsole(io.StringIO):
            def write(self, text):
                entered.set()
                release.wait(timeout=3)
                return super().write(text)
        with tempfile.TemporaryDirectory() as folder:
            out = AsyncOutput(folder, stream=SlowConsole(), capacity=1)
            try:
                out.emit({"t": 0, "kind": "RX", "message": "first", "fields": {}})
                self.assertTrue(entered.wait(timeout=1))
                out.emit({"t": 0, "kind": "RX", "message": "second", "fields": {}})
                out.emit({"t": 0, "kind": "RX", "message": "third", "fields": {}})
                self.assertGreater(out.metrics()["console_dropped"], 0)
            finally:
                release.set()
                out.close()

    def test_successful_cli_persists_summary_and_closes_port(self):
        clock = Clock()
        class Serial:
            in_waiting = 0
            opened = closed = False
            def __init__(self, **_): pass
            def open(self): self.opened = True
            def read(self, _): return snapshot(int(clock() / 0.5) & 255)
            def write(self, data): return len(data)
            def close(self): self.closed = True
        connection = Serial()
        real_run = run_serial
        def virtual_run(serial, output, duration, profile, interval):
            return real_run(serial, output, duration, profile, interval, clock, clock.advance)
        with tempfile.TemporaryDirectory() as folder, patch("tools.mcu_sim.ROOT", Path(folder)), patch("serial.Serial", return_value=connection), patch("tools.mcu_sim.run_serial", virtual_run), patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["--port", "COM_FAKE", "--profile", "snapshots", "--duration", "1"]), 0)
            reports = list(Path(folder).glob(".maixpy/mcu_sim/*/summary.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["port"], "COM_FAKE")
            self.assertTrue(report["evidence_complete"])
        self.assertTrue(connection.opened and connection.closed)

    def test_run_duration_interrupt_and_disconnected_port_close(self):
        class Output:
            def emit(self, _): pass
            def close(self): self.closed = True
            def metrics(self): return {}
        class Serial:
            in_waiting = 0
            def read(self, _): raise OSError("unplugged")
            def write(self, _): raise OSError("unplugged")
            def close(self): self.closed = True
        clock = Clock()
        serial, out = Serial(), Output()
        result = run_serial(serial, out, 3, "snapshots", 60, clock, clock.advance)
        self.assertTrue(serial.closed and out.closed)
        self.assertEqual(result["reason"], "duration")
        self.assertGreater(result["counts"]["read_errors"], 0)
        def interrupt(_): raise KeyboardInterrupt
        serial, out = Serial(), Output()
        result = run_serial(serial, out, 0, "snapshots", 60, clock, interrupt)
        self.assertEqual(result["reason"], "interrupt")
        self.assertTrue(serial.closed)

    def test_cli_required_port_and_defaults(self):
        args = build_parser().parse_args([])
        self.assertEqual((args.duration, args.profile, args.cycle_interval), (1800, "business", 60))
        with self.assertRaises(SystemExit) as cm:
            main([])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
