"""PC MCU simulator: v3.1 serial traffic, business transactions and bounded logging.

Run: python tools/mcu_sim.py --list-ports
     python tools/mcu_sim.py --port COM19 --duration 1800
No Maix imports and no remote device control.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import struct
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from protocol import Protocol

SHAPES = {1: "圆锥", 2: "立方", 3: "棱锥", 4: "长方", 5: "六棱", 6: "异形"}
COLORS = {1: "红色", 2: "橙色", 3: "黄色", 4: "绿色", 5: "蓝色", 6: "紫色", 7: "黑色", 8: "白色"}
STATES = {0: "INIT", 1: "READY", 2: "SCANNING", 3: "PICKING", 4: "DISPLAYING",
          5: "RECHECKING", 6: "DUMPING", 7: "FINISHED", 8: "FAULT"}
NAMES = {1: "HB_V", 2: "SNAPSHOT", 3: "RECHECK_RESULT", 4: "ALARM_V",
         0x41: "HB_M", 0x42: "SYS_START", 0x44: "PICK_REPORT", 0x45: "RECHECK_REQ",
         0x48: "FINISH", 0x82: "ACK_SNAPSHOT", 0x83: "ACK_RECHECK",
         0xC2: "ACK_SYS_START", 0xC4: "ACK_PICK_REPORT", 0xC8: "ACK_FINISH"}


def hex_bytes(data):
    return bytes(data).hex(" ").upper()


def timestamp(seconds):
    ms = max(0, int(round(seconds * 1000)))
    return "%02d:%02d:%02d.%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def render(event):
    """One event -> human output, independent of transport and clock."""
    prefix = "[%s] %s" % (timestamp(event["t"]), event["kind"])
    fields = event.get("fields", {})
    lines = [prefix + " " + event.get("message", "")]
    if "hex" in fields and event["kind"] != "RX_RAW":
        lines.append("              HEX: " + fields["hex"])
    for i, piece in enumerate(fields.get("pieces", []), 1):
        lines.append("              #%d %s%s  %s  X=%.1fmm Y=%.1fmm" % (
            i, COLORS[piece[1]], SHAPES[piece[0]], "可抓" if piece[2] else "不可抓",
            piece[4] / 10, piece[5] / 10))
    return "\n".join(lines)


class AsyncOutput:
    """Console and disk have separate queues: slow console must not block disk/UART."""
    def __init__(self, directory, quiet=False, stream=None, capacity=4096):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = stream if stream is not None else sys.stdout
        self.quiet = quiet
        self.color = bool(getattr(self.stream, "isatty", lambda: False)())
        self.disk_queue = queue.Queue(maxsize=capacity)
        self.console_queue = queue.Queue(maxsize=capacity)
        self.disk_dropped = self.console_dropped = self.disk_errors = self.console_errors = 0
        self.stop = threading.Event()
        self.threads = [threading.Thread(target=self._disk, daemon=True, name="sim-log"),
                        threading.Thread(target=self._console, daemon=True, name="sim-console")]
        for thread in self.threads:
            thread.start()

    def emit(self, event):
        try:
            self.disk_queue.put_nowait(event)
        except queue.Full:
            self.disk_dropped += 1
        if not self.quiet or event["kind"] in ("WARN", "ERROR", "STATUS", "START", "END", "RECOVER"):
            try:
                self.console_queue.put_nowait(event)
            except queue.Full:
                self.console_dropped += 1

    def _disk(self):
        try:
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
                while not self.stop.is_set() or not self.disk_queue.empty():
                    try:
                        event = self.disk_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    try:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                        handle.flush()
                    except (OSError, ValueError):
                        self.disk_errors += 1
        except OSError:
            self.disk_errors += 1

    def _console(self):
        while not self.stop.is_set() or not self.console_queue.empty():
            try:
                event = self.console_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                text = render(event)
                if self.color:
                    color = "31" if event["kind"] == "ERROR" else "33" if event["kind"] == "WARN" else "36" if event["kind"].startswith("TX") else "32"
                    text = "\033[%sm%s\033[0m" % (color, text)
                self.stream.write(text + "\n")
                self.stream.flush()
            except (OSError, ValueError, UnicodeError):
                self.console_errors += 1

    def metrics(self):
        return {"disk_dropped": self.disk_dropped, "console_dropped": self.console_dropped,
                "disk_errors": self.disk_errors, "console_errors": self.console_errors,
                "disk_pending": self.disk_queue.qsize(), "console_pending": self.console_queue.qsize(),
                "writer_alive": any(t.is_alive() for t in self.threads)}

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=2)


class Receiver:
    """Bounded streaming parser, preserving the PC receive time of each header byte."""
    def __init__(self):
        self.buffer = bytearray()
        self.stamps = []
        self.last_rx = None
        self.codec = Protocol()
        self.counts = Counter()

    def _drop(self, n):
        del self.buffer[:n]
        del self.stamps[:n]

    def feed(self, data, now):
        if data:
            self.buffer.extend(data)
            self.stamps.extend([now] * len(data))
            self.last_rx = now
        frames = []
        while self.buffer:
            index = self.buffer.find(b"\xaa\x55")
            if index < 0:
                n = len(self.buffer) - int(self.buffer[-1] == 0xAA)
                self.counts["noise_bytes"] += n
                self._drop(n)
                break
            if index:
                self.counts["noise_bytes"] += index
                self._drop(index)
            if len(self.buffer) >= 3 and self.buffer[2] > 128:
                self.counts["bad_len"] += 1
                self._drop(2)
                continue
            size = self.buffer[2] + 6 if len(self.buffer) >= 3 else None
            if size is not None and len(self.buffer) >= size:
                frame = bytes(self.buffer[:size])
                if self.codec.crc8(frame[:-1]) != frame[-1]:
                    self.counts["crc_errors"] += 1
                    self._drop(2)
                    continue
                frames.append((self.stamps[0], frame))
                self._drop(size)
                continue
            if self.last_rx is not None and now - self.last_rx > 0.3:
                self.counts["stale_resync"] += 1
                self._drop(min(2, len(self.buffer)))
                continue  # immediately parse again, never clear a following valid frame
            break
        return frames


def decode_pieces(payload, offset, count):
    if count > 12 or len(payload) != offset + count * 10:
        raise ValueError("载荷长度或件数错误")
    pieces = []
    for i in range(count):
        piece = struct.unpack_from("<BBBBhhH", payload, offset + i * 10)
        shape, color, pickable, reserved, x, y, angle = piece
        if shape not in SHAPES or color not in COLORS or pickable not in (0, 1):
            raise ValueError("物料枚举无效")
        if shape == 6 and pickable or reserved != 0 or angle != 0xFFFF:
            raise ValueError("异形可抓标志、保留位或角度错误")
        if not (0 <= x <= 1800 and 0 <= y <= 2500):
            raise ValueError("坐标越界")
        pieces.append(piece)
    return pieces


@dataclass
class Transaction:
    cmd: int
    response: int
    seq: int
    frame: bytes
    timeout: float
    retries: int
    started: float
    deadline: float
    attempts: int = 1


class Simulator:
    """Deterministic state machine; injectable clock/transport/output for offline tests."""
    def __init__(self, write, emit, clock=time.monotonic, profile="business", cycle_interval=60):
        self.write = write
        self.emit = emit
        self.clock = clock
        self.profile = profile
        self.cycle_interval = cycle_interval
        self.start = clock()
        self.codec = Protocol()
        self.receiver = Receiver()
        self.counts = Counter()
        self.pending = None
        self.outgoing = deque()
        self.latest = None
        self.last_snap_payload = None
        self.last_snapshot = None
        self.last_progress = None
        self.last_byte = self.start
        self.last_tick = self.start
        self.next_hb = self.next_status = self.next_cycle = self.start
        self.flags = {"link": False, "snapshot": False, "stagnant": False}
        self.max_snapshot_gap = self.max_tick_gap = 0.0
        self.max_transaction_ms = 0.0
        self.cycle_items = []
        self.pick_index = self.total_ok = 0
        self.stage = "idle"
        self.cycle_started = self.start
        self.expected = []
        self.latency = {}

    def event(self, kind, message, **fields):
        self.emit({"t": round(self.clock() - self.start, 6), "kind": kind,
                   "message": message, "fields": fields})

    def _queue(self, cmd, payload, seq=None):
        frame, seq = self.codec.build_frame(cmd, payload, seq)
        self._enqueue(frame)
        return frame, seq

    def _enqueue(self, frame):
        if len(self.outgoing) >= 128:
            self.counts["tx_queue_dropped"] += 1
            self.event("ERROR", "发送队列已满，帧未发送")
            return
        self.outgoing.append([frame, 0])

    def _flush(self):
        for _ in range(16):
            if not self.outgoing:
                break
            frame, offset = self.outgoing[0]
            remaining = frame[offset:]
            try:
                n = self.write(remaining)
                if not isinstance(n, int) or not 0 <= n <= len(remaining):
                    raise ValueError("串口返回无效写入长度: %r" % n)
            except Exception as exc:
                self.outgoing.popleft()
                self.counts["write_errors"] += 1
                self.event("ERROR", "TX CMD=%02X SEQ=%02X 写失败，实际写入未知: %s" % (
                    frame[3], frame[4], exc), requested_hex=hex_bytes(remaining))
                break
            self.counts["tx_bytes"] += n
            self.event("TX", "%s SEQ=%02X 实际写入=%d/%dB%s" % (
                NAMES.get(frame[3], "CMD_%02X" % frame[3]), frame[4], n, len(remaining),
                " " + self._tx_description(frame) if offset == 0 else " 续写"),
                hex=hex_bytes(remaining[:n]), cmd=frame[3], seq=frame[4], written=n,
                requested=len(remaining), requested_hex=hex_bytes(remaining))
            if n < len(remaining):
                self.counts["partial_writes"] += 1
                self.event("ERROR", "TX 截断/未完成：%d/%dB，剩余字节将先续写" % (n, len(remaining)))
                self.outgoing[0][1] += n
                break  # never interleave another frame into this one
            self.outgoing.popleft()
            self.counts["tx_frames"] += 1

    @staticmethod
    def _tx_description(frame):
        p = frame[5:-1]
        cmd = frame[3]
        if cmd == 0x41:
            return "state=%s err=%d" % (STATES.get(p[0], p[0]), p[1])
        if cmd == 0x42:
            return "mode=%d ver=%02X" % (p[0], p[1])
        if cmd == 0x44:
            number, shape, color, result, total = struct.unpack("<BBBBH", p)
            return "#%d %s%s result=%02X simulated_ok=%d" % (number, COLORS[color], SHAPES[shape], result, total)
        if cmd == 0x45:
            return "expected=%s" % [(p[i], p[i + 1]) for i in range(1, len(p), 2)]
        if cmd == 0x48:
            return "simulated_ok=%d total=%d elapsed=%ds" % struct.unpack("<HBH", p)
        return "result=%02X" % p[0] if p else ""

    def receive(self, data):
        now = self.clock()
        if data:
            self.last_byte = now
            self.counts["rx_bytes"] += len(data)
            self.event("RX_RAW", "%dB: %s" % (len(data), hex_bytes(data)), hex=hex_bytes(data))
        before = self.receiver.counts.copy()
        for header_time, frame in self.receiver.feed(data, now):
            self.counts["rx_frames"] += 1
            try:
                self._frame(frame, header_time)
            except (ValueError, struct.error) as exc:
                self.counts["invalid_frames"] += 1
                self.event("ERROR", "RX CMD=%02X SEQ=%02X 业务载荷错误: %s" % (frame[3], frame[4], exc))
        for key, value in self.receiver.counts.items():
            if value > before[key]:
                self.event("ERROR", "RX %s +%d (累计%d)" % (key, value - before[key], value))

    def _frame(self, frame, header_time):
        cmd, seq, p = frame[3], frame[4], frame[5:-1]
        self.counts["rx_%02X" % cmd] += 1
        if cmd == 2:
            if len(p) < 4 or p[1] != p[3] or p[2] != 0:
                raise ValueError("快照 total/count/batch 错误")
            pieces = decode_pieces(p, 4, p[3])
            repeat = self.last_snap_payload is not None and p[0] == self.last_snap_payload[0]
            if repeat and p != self.last_snap_payload:
                raise ValueError("同快照号的载荷发生变化")
            gap = header_time - self.last_snapshot if self.last_snapshot is not None else None
            if gap is not None:
                self.max_snapshot_gap = max(self.max_snapshot_gap, gap)
                if gap > 2.5 and not self.flags["snapshot"]:
                    self.counts["snapshot_overdue"] += 1
                    self.event("WARN", "快照接收间隔 %.0fms > 2500ms" % (gap * 1000))
            self.last_snapshot = header_time
            if not repeat:
                self.last_progress = self.clock()
                self.counts["new_snapshots"] += 1
            else:
                self.counts["repeat_snapshots"] += 1
            self.latest = (p[0], pieces)
            self.last_snap_payload = p
            self.counts["snapshots"] += 1
            self.counts["nonempty_snapshots"] += bool(pieces)
            self.event("RX", "SNAPSHOT SEQ=%02X snap=%02X count=%d gap=%s %s" % (
                seq, p[0], len(pieces), "-" if gap is None else "%.0fms" % (gap * 1000),
                "REPEAT" if repeat else "NEW"), pieces=pieces, cmd=cmd, seq=seq,
                header_received_t=header_time - self.start)
            self._queue(0x82, b"\x00", seq)
            return
        if cmd == 1:
            if len(p) != 3 or p[0] not in STATES or p[2] != 0:
                raise ValueError("心跳字段无效")
            self.event("RX", "HB_V SEQ=%02X state=%s err=%02X" % (seq, STATES[p[0]], p[1]), cmd=cmd, seq=seq)
            if p[1] or p[0] == 8:
                self.counts["vision_faults"] += 1
                self.event("WARN", "视觉报告 FAULT 或错误码")
            return
        if cmd == 4:
            if len(p) != 2:
                raise ValueError("ALARM 长度错误")
            self.counts["alarms"] += 1
            self.event("WARN", "RX ALARM_V code=%02X ref_seq=%02X" % tuple(p))
            return
        if cmd == 3:
            if len(p) < 2 or p[0] not in (0, 1):
                raise ValueError("复核结果字段无效")
            pieces = decode_pieces(p, 2, p[1])
            self.event("RX", "RECHECK_RESULT SEQ=%02X match=%d count=%d" % (seq, p[0], p[1]), pieces=pieces)
            self._queue(0x83, b"\x00", seq)
            if self.pending and (cmd, seq) == (self.pending.response, self.pending.seq):
                actual = sorted((item[0], item[1]) for item in pieces)
                calculated = int(actual == sorted(self.expected))
                if calculated != p[0]:
                    self.counts["recheck_flag_disagreements"] += 1
                    self.event("WARN", "复核 match 与本机清单比对不同（未知目标可能被视觉过滤）")
                if not p[0]:
                    self.counts["recheck_mismatches"] += 1
                    self.event("WARN", "复核不一致；模拟抓取未移走物料，清单变化单独记录")
        elif cmd in (0xC2, 0xC4, 0xC8):
            if len(p) != (2 if cmd == 0xC2 else 1):
                raise ValueError("ACK 载荷长度错误")
            self.event("RX", "%s SEQ=%02X result=%02X%s" % (
                NAMES[cmd], seq, p[0], " ver=%02X" % p[1] if cmd == 0xC2 else ""))
        else:
            self.event("RX", "CMD=%02X SEQ=%02X payload=%s" % (cmd, seq, hex_bytes(p)))
            return
        tx = self.pending
        if tx is None or (cmd, seq) != (tx.response, tx.seq):
            self.counts["unmatched_responses"] += 1
            self.event("WARN", "迟到、重复或非当前事务应答，未推进业务")
            return
        success = cmd == 3 or p[0] == 0 and (cmd != 0xC2 or p[1] == 2)
        self._complete(success)

    def _begin(self, cmd, payload, response, timeout, retries):
        now = self.clock()
        frame, seq = self._queue(cmd, payload)
        self.pending = Transaction(cmd, response, seq, frame, timeout, retries, now, now + timeout)
        self.counts["transactions"] += 1

    def _complete(self, success):
        tx = self.pending
        self.pending = None
        cost = (self.clock() - tx.started) * 1000
        self.max_transaction_ms = max(self.max_transaction_ms, cost)
        stats = self.latency.setdefault(NAMES[tx.cmd], {"count": 0, "sum_ms": 0.0, "max_ms": 0.0})
        stats["count"] += 1
        stats["sum_ms"] += cost
        stats["max_ms"] = max(stats["max_ms"], cost)
        self.counts["transaction_ok" if success else "transaction_failed"] += 1
        if tx.cmd == 0x44 and success:
            self.counts["pick_ack_ok"] += 1
        self.event("ACK" if success else "ERROR", "%s SEQ=%02X %s 耗时=%.0fms attempts=%d" % (
            NAMES[tx.cmd], tx.seq, "完成" if success else "失败", cost, tx.attempts))
        if self.stage == "start":
            self.stage = "pick" if success else "end"
        elif self.stage == "pick":
            self.pick_index += 1
        elif self.stage == "recheck":
            self.stage = "finish"
        elif self.stage == "finish":
            self.stage = "end"

    def _business(self, now):
        if self.pending or self.profile != "business":
            return
        if self.stage == "idle":
            if now < self.next_cycle or self.latest is None:
                return
            self.cycle_started = now
            self.cycle_items = list(self.latest[1])
            self.pick_index = self.total_ok = 0
            self.counts["cycles_started"] += 1
            self.stage = "start"
            self._begin(0x42, b"\x00\x02", 0xC2, 1.0, 3)
        elif self.stage == "pick":
            if self.pick_index < len(self.cycle_items):
                shape, color, pickable, *_ = self.cycle_items[self.pick_index]
                self.total_ok += bool(pickable)
                payload = struct.pack("<BBBBH", self.pick_index + 1, shape, color,
                                      0 if pickable else 2, self.total_ok)
                self.counts["pick_reports"] += 1
                self._begin(0x44, payload, 0xC4, 1.5, 3)
            else:
                if not self.cycle_items:
                    self.counts["empty_cycles"] += 1
                self.expected = [(p[0], p[1]) for p in self.latest[1]]
                payload = bytes([len(self.expected)]) + bytes(v for pair in self.expected for v in pair)
                self.stage = "recheck"
                self._begin(0x45, payload, 3, 2.0, 2)
        elif self.stage == "finish":
            elapsed = min(65535, int(now - self.cycle_started))
            self._begin(0x48, struct.pack("<HBH", self.total_ok, len(self.cycle_items), elapsed), 0xC8, 2.0, 2)
        elif self.stage == "end":
            self.counts["cycles_completed"] += 1
            self.next_cycle = max(self.cycle_started + self.cycle_interval, now + 0.1)
            self.stage = "idle"

    def tick(self):
        now = self.clock()
        gap = now - self.last_tick
        self.max_tick_gap = max(self.max_tick_gap, gap)
        if gap > 0.5:
            self.counts["host_delays"] += 1
            self.event("WARN", "本机调度延迟 %.0fms，接收时间证据可能受影响" % (gap * 1000))
        self.last_tick = now
        self.receive(b"")  # stale partial frames also expire when no bytes arrive
        if now >= self.next_hb:
            state = 3 if self.stage == "pick" else 5 if self.stage == "recheck" else 7 if self.stage in ("finish", "end") else 1
            self._queue(0x41, bytes([state, 0, 0]))
            self.next_hb = now + 0.5
        if self.pending and now >= self.pending.deadline:
            tx = self.pending
            if tx.attempts <= tx.retries:
                tx.attempts += 1
                tx.deadline = now + tx.timeout
                self.counts["retries"] += 1
                self.event("WARN", "%s SEQ=%02X 超时，重试 %d/%d" % (NAMES[tx.cmd], tx.seq, tx.attempts - 1, tx.retries))
                self._enqueue(tx.frame)
            else:
                self.counts["transaction_timeouts"] += 1
                self._complete(False)
        self._business(now)
        conditions = {"link": now - self.last_byte > 2.5,
                      "snapshot": now - (self.last_snapshot if self.last_snapshot is not None else self.start) > 2.5,
                      "stagnant": self.last_progress is not None and now - self.last_progress > 15}
        labels = {"link": "通信中断：2500ms 无接收字节", "snapshot": "快照周期违规：2500ms 无有效快照",
                  "stagnant": "疑似识别停滞：快照号持续15秒未更新（不能确认画面冻结或重启）"}
        for key, value in conditions.items():
            if value and not self.flags[key]:
                self.counts[{"link": "link_interruptions", "snapshot": "snapshot_overdue", "stagnant": "recognition_stalls"}[key]] += 1
                self.event("WARN", labels[key])
            elif not value and self.flags[key]:
                self.event("RECOVER", labels[key].split("：")[0] + "恢复")
            self.flags[key] = value
        if now >= self.next_status:
            self.event("STATUS", "rx=%dB snapshots=%d new=%d repeat=%d tx=%dB stage=%s errors=%d" % (
                self.counts["rx_bytes"], self.counts["snapshots"], self.counts["new_snapshots"],
                self.counts["repeat_snapshots"], self.counts["tx_bytes"], self.stage,
                self.counts["invalid_frames"] + self.counts["transaction_failed"] + self.counts["write_errors"]))
            self.next_status = now + 5
        self._flush()

    def summary(self, output=None, reason="duration"):
        output = output or {}
        counts = dict(self.counts)
        parser = dict(self.receiver.counts)
        incomplete = any(output.get(k, 0) for k in ("disk_dropped", "console_dropped", "disk_errors",
                         "console_errors", "disk_pending", "console_pending", "writer_alive")) or bool(self.counts["host_delays"])
        protocol_errors = self.counts["invalid_frames"] + sum(parser.values()) + self.counts["write_errors"] + self.counts["partial_writes"] + self.counts["tx_queue_dropped"] + self.counts["read_errors"]
        return {"reason": reason, "duration_s": self.clock() - self.start, "profile": self.profile,
                "counts": counts, "parser": parser, "output": output,
                "max_snapshot_gap_ms": self.max_snapshot_gap * 1000,
                "max_host_tick_gap_ms": self.max_tick_gap * 1000,
                "transactions": self.latency, "evidence_complete": not incomplete,
                "protocol": "NO_DATA" if not self.counts["snapshots"] else "FAIL" if protocol_errors else "PASS",
                "business": "NOT_REQUESTED" if self.profile == "snapshots" else "FAIL" if self.counts["transaction_failed"] else "INCOMPLETE" if not self.counts["cycles_completed"] or not self.counts["pick_reports"] else "PASS",
                "pick_display_covered": bool(self.counts["pick_ack_ok"]),
                "recognition_progress": "NO_DATA" if self.latest is None else "SUSPECTED_STALL" if self.counts["recognition_stalls"] else "INSUFFICIENT" if self.counts["new_snapshots"] < 2 else "OBSERVED",
                "link": "INTERRUPTED" if self.counts["link_interruptions"] else "NO_DATA" if not self.counts["rx_bytes"] else "OBSERVED",
                "snapshot_cadence": "VIOLATION" if self.counts["snapshot_overdue"] else "INSUFFICIENT" if self.counts["snapshots"] < 2 else "OBSERVED",
                "transaction_pending_at_exit": None if self.pending is None else NAMES[self.pending.cmd],
                "limitations": ["时间是PC接收块观测值，不是硬件首字节测量",
                    "仅串口无法确认画面冻结、整机重启或真实显示完成",
                    "模拟抓取没有移走物料，模拟成功数不是实物抓取成绩",
                    "未复现真实MCU、电机和共用电源的电气负载"]}


def build_parser():
    p = argparse.ArgumentParser(description="MCU模拟器：实时串口数据与30分钟稳定性测试")
    p.add_argument("--list-ports", action="store_true")
    p.add_argument("--port", help="必须明确指定，如COM19")
    p.add_argument("--duration", type=float, default=1800, help="秒；0表示手动停止")
    p.add_argument("--profile", choices=("business", "snapshots"), default="business")
    p.add_argument("--cycle-interval", type=float, default=60)
    p.add_argument("--quiet", action="store_true")
    return p


def run_serial(serial, output, duration, profile, cycle_interval, clock=time.monotonic, sleep=time.sleep):
    sim = Simulator(serial.write, output.emit, clock, profile, cycle_interval)
    reason = "duration"
    next_output_check = sim.start
    previous_output_errors = 0
    output.emit({"t": 0, "kind": "START", "message": "仅串口测试；模拟抓取不移动物料", "fields": {}})
    try:
        while duration == 0 or clock() - sim.start < duration:
            try:
                data = serial.read(min(4096, serial.in_waiting) or 1)
                if data:
                    sim.receive(data)
            except Exception as exc:
                sim.counts["read_errors"] += 1
                # The port is not reopened/reset. Avoid unbounded identical messages.
                if sim.counts["read_errors"] == 1 or clock() >= getattr(sim, "next_read_error", 0):
                    sim.event("ERROR", "串口读失败，继续观察但不重开端口: %s" % exc)
                    sim.next_read_error = clock() + 5
            sim.tick()
            if clock() >= next_output_check:
                metrics = output.metrics()
                errors = sum(metrics.get(k, 0) for k in ("disk_dropped", "console_dropped", "disk_errors", "console_errors"))
                if errors > previous_output_errors:
                    sim.event("WARN", "输出记录不完整: %s" % metrics)
                previous_output_errors = errors
                next_output_check = clock() + 5
            sleep(0.005)
    except KeyboardInterrupt:
        reason = "interrupt"
    except Exception as exc:
        reason = "error"
        sim.counts["runtime_errors"] += 1
        sim.event("ERROR", "模拟器运行异常: %s" % exc)
    finally:
        try:
            serial.close()
        except Exception as exc:
            sim.counts["close_errors"] += 1
            sim.event("ERROR", "串口关闭失败: %s" % exc)
        verdict = sim.summary()
        sim.event("END", "测试结束 reason=%s protocol=%s business=%s recognition=%s；报告=%s/summary.json" % (
            reason, verdict["protocol"], verdict["business"], verdict["recognition_progress"],
            getattr(output, "directory", "日志目录")))
        output.close()
    return sim.summary(output.metrics(), reason)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or not math.isfinite(args.cycle_interval) or args.duration < 0 or args.cycle_interval <= 0:
        parser.error("duration必须≥0，cycle-interval必须>0")
    if not args.list_ports and not args.port:
        parser.error("请用--list-ports查询，然后用--port COMxx指定串口")
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        parser.exit(2, "缺少PySerial，请运行 python -m pip install pyserial\n")
    if args.list_ports:
        ports = list(list_ports.comports())
        for item in ports:
            print("%s  %s  %s" % (item.device, item.description, item.hwid))
        if not ports:
            print("未发现串口，请连接USB串口适配器。")
        return 0
    started_at = datetime.now().astimezone()
    directory = ROOT / ".maixpy" / "mcu_sim" / started_at.strftime("%Y%m%d-%H%M%S-%f")
    output = AsyncOutput(directory, quiet=args.quiet)
    output.emit({"t": 0, "kind": "START", "message": "port=%s 115200/8N1 duration=%ss profile=%s logs=%s" % (
        args.port, args.duration, args.profile, directory), "fields": {}})
    connection = serial.Serial(port=None, baudrate=115200, timeout=0, write_timeout=0.05,
                               xonxoff=False, rtscts=False, dsrdtr=False)
    connection.dtr = connection.rts = False
    connection.port = args.port
    try:
        connection.open()
    except Exception as exc:
        output.emit({"t": 0, "kind": "ERROR", "message": "串口打开失败: %s" % exc, "fields": {}})
        connection.close()
        output.close()
        summary = {"reason": "open_error", "error": str(exc), "output": output.metrics(), "evidence_complete": False}
    else:
        summary = run_serial(connection, output, args.duration, args.profile, args.cycle_interval)
    summary["port"] = args.port
    summary["started_at"] = started_at.isoformat()
    summary["settings"] = vars(args)
    try:
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print("summary.json写入失败: %s" % exc, file=sys.stderr)
        return 2
    if summary["reason"] in ("open_error", "error") or not summary["evidence_complete"]:
        return 2
    failures = summary.get("protocol") in ("FAIL", "NO_DATA") or summary.get("business") == "FAIL" or summary.get("snapshot_cadence") == "VIOLATION" or summary.get("link") == "INTERRUPTED" or summary.get("recognition_progress") == "SUSPECTED_STALL"
    return int(failures)


if __name__ == "__main__":
    sys.exit(main())
