"""串口进程：独占 UART，在媒体调用阻塞/GIL 被占用时仍发送缓存快照。"""
import argparse
import contextlib
from collections import deque
import json
import os
import socket
import subprocess
import sys
import time
import traceback

import config
from coordinate import CoordinateConverter
from protocol import Protocol
from protocol_handler import ProtocolHandler, snapshot_details
from diagnostics import Journal


class Messages:
    """非阻塞 IPC；协议收发不等待媒体进程读取状态。"""
    MAX_PENDING = 65536

    def __init__(self, sock):
        self.sock = sock
        sock.setblocking(False)
        self.rx = bytearray()
        self.tx = bytearray()
        self.closed = False
        self.last_error = ""

    def send(self, kind, data=None):
        if self.closed:
            return False
        packet = (json.dumps([kind, data], separators=(",", ":")) + "\n").encode()
        if len(self.tx) + len(packet) > self.MAX_PENDING:
            return False
        self.tx.extend(packet)
        self.flush()
        return True

    def flush(self):
        if self.closed or not self.tx:
            return
        try:
            n = self.sock.send(self.tx)
            del self.tx[:n]
        except BlockingIOError:
            pass
        except OSError as exc:
            self.closed = True
            self.last_error = "send: %s" % exc

    def receive(self):
        self.flush()
        for _ in range(16):
            try:
                chunk = self.sock.recv(4096)
            except BlockingIOError:
                break
            except OSError as exc:
                self.closed = True
                self.last_error = "recv: %s" % exc
                break
            if not chunk:
                self.closed = True
                self.last_error = "peer EOF"
                break
            self.rx.extend(chunk)
        messages = []
        while b"\n" in self.rx:
            line, _, rest = self.rx.partition(b"\n")
            self.rx = bytearray(rest)
            messages.append(json.loads(line))
        if len(self.rx) > self.MAX_PENDING:
            raise ValueError("IPC message too large")
        return messages


class UART:
    """Linux tty 的非阻塞 8N1 UART，通信进程不导入 Maix 媒体库。"""
    def __init__(self, port, baudrate):
        import termios
        self.termios = termios
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = getattr(termios, "B%d" % baudrate)
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(self.fd)
            raise

    def available(self):
        import array
        import fcntl
        result = array.array("i", [0])
        fcntl.ioctl(self.fd, self.termios.FIONREAD, result, True)
        return result[0]

    def read(self, n):
        try:
            return os.read(self.fd, n)
        except BlockingIOError:
            return b""

    def write(self, data):
        return os.write(self.fd, data)

    def close(self):
        os.close(self.fd)


STATUS_FIELDS = ("current_state", "link_alive", "rx_bytes_total", "rx_frames_total",
    "max_rx_chunk", "write_error_count", "write_partial_count", "pick_drop_count",
    "scan_count", "recheck_count", "cmd_counts", "snapshot_send_count",
    "snapshot_repeat_count", "snapshot_seq", "tx_attempt_count", "tx_success_count",
    "tx_bytes_total", "snapshot_result_version", "snapshot_completed_at",
    "last_snapshot_write_version", "last_snapshot_write_at", "snapshot_write_error")


def status_of(handler):
    status = {k: getattr(handler, k) for k in STATUS_FIELDS}
    status["cmd_counts"] = {str(k): v for k, v in handler.cmd_counts.items()}
    status["parser"] = {k: getattr(handler.proto, k) for k in
        ("crc_errors", "bad_len", "dropped_bytes", "resync_stale_count")}
    status["rx_buffer_size"] = len(handler.proto.rx_buffer)
    return status


class LinkWorker:
    def __init__(self, serial, channel, journal=None):
        self.channel = channel
        self.handler = ProtocolHandler(serial, event_sink=self.emit)
        self.stopping = False
        self.next_status = 0.0
        self.dropped_events = 0
        self.journal = journal
        self.next_diag = 0.0
        self.last_tick = None
        self.max_tick_gap_ms = 0

    def emit(self, kind, data):
        if not self.channel.send(kind, data):
            # 不阻塞 UART；PICK 不获 ACK，MCU 按显示超时处理。
            self.dropped_events += 1
            if kind == "pick":
                # 未投递到显示端的报告不能一直占着去重队列，否则重试也被吞掉。
                self.handler.pick_report_queue.remove(tuple(data))

    def tick(self):
        now = time.monotonic()
        if self.last_tick is not None:
            self.max_tick_gap_ms = max(self.max_tick_gap_ms, (now - self.last_tick) * 1000)
        self.last_tick = now
        for kind, data in self.channel.receive():
            if kind == "snapshot":
                if isinstance(data, dict):
                    self.handler.publish_snapshot(data["pieces"], data["version"], data["completed_at"])
                else:
                    self.handler.publish_snapshot(data)
            elif kind == "pick_ack":
                self.handler.send_pick_ack(data)
            elif kind == "pick_failed":
                self.handler.pick_report_queue[:] = [p for p in
                    self.handler.pick_report_queue if p[0] != data]
            elif kind == "frame":
                self.handler._write(bytes(data))
            elif kind == "state":
                self.handler.current_state = data
            elif kind == "stop":
                self.stopping = True
        if self.stopping or self.channel.closed:
            return False
        self.handler.update()
        if time.monotonic() >= self.next_status:
            # 状态可合并/丢弃，避免媒体冻结时把 IPC 写满而阻塞串口。
            if not self.channel.tx:
                status = status_of(self.handler)
                status["ipc_dropped_events"] = self.dropped_events
                status["log_write_errors"] = self.journal.write_errors if self.journal else 0
                status["log_dropped"] = self.journal.dropped if self.journal else 0
                status["log_current_error"] = self.journal.current_error if self.journal else ""
                if not self.channel.send("status", status):
                    self.dropped_events += 1
            self.next_status = time.monotonic() + 0.25
        if self.journal is not None and time.monotonic() >= self.next_diag:
            status = status_of(self.handler)
            completed = self.handler.snapshot_completed_at
            self.journal.event("LINK", result_age_s=(None if completed is None
                else round(time.monotonic() - completed, 3)),
                ipc_dropped_events=self.dropped_events,
                uart_service_gap_max_ms=round(self.max_tick_gap_ms, 3), **status)
            self.next_diag = time.monotonic() + config.DIAG_LOG_INTERVAL
        return True


class LinkProcess:
    """媒体侧代理：IPC 发布结果/显示完成事件，不直接读取或写入 UART。"""
    def __init__(self):
        self.converter = CoordinateConverter()
        self.proto = Protocol()
        self._current_state = Protocol.STATE_INIT
        self.link_alive = False
        self.finished = False
        self.pick_report_queue = deque()
        self.recheck_queue = deque()
        self.recheck_requested = False
        self.recheck_expected = []
        self.recheck_req_seq = 0
        self.dump_display_pending = False
        self.finish_display_pending = None
        self.finish_summary = None
        self.last_scan_pieces = []
        self.last_recheck_pieces = []
        self.snapshot_filter_stats = {}
        self.local_result_version = 0
        self.local_completed_at = None
        self.last_snapshot_pieces = []
        self.local_items = []
        self.ipc_error = ""
        self.ipc_dropped_events = self.log_write_errors = self.log_dropped = 0
        self.log_current_error = ""
        self.snapshot_seq = 0
        for key in STATUS_FIELDS:
            if key not in ("current_state", "link_alive", "cmd_counts"):
                setattr(self, key, 0)
        self.cmd_counts = {}
        self.snapshot_completed_at = self.last_snapshot_write_at = None
        self.snapshot_write_error = ""
        self.channel = None
        self.process = None

    def start(self, port=None, baudrate=None):
        parent, child = socket.socketpair()
        self.channel = Messages(parent)
        try:
            self.process = subprocess.Popen([
                sys.executable, "-u", os.path.abspath(__file__), "--fd", str(child.fileno()),
                "--port", port or config.UART_PORT,
                "--baud", str(baudrate or config.UART_BAUDRATE)],
                pass_fds=(child.fileno(),))
        finally:
            child.close()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            messages = self.channel.receive()
            ready = False
            for kind, data in messages:
                if kind == "ready":
                    ready = True
                else:
                    self._receive(kind, data)
            if ready:
                return
            if self.process.poll() is not None:
                raise RuntimeError("串口进程启动失败，exit=%s" % self.process.returncode)
            time.sleep(0.01)
        self.close()
        raise RuntimeError("串口进程启动超时")

    @property
    def current_state(self):
        return self._current_state

    @current_state.setter
    def current_state(self, state):
        self._current_state = state
        if self.channel is not None:
            try:
                self._command("state", state)
            except RuntimeError as exc:
                # 状态提示积压不能把视觉程序结束；快照发布另行显示失败。
                self.ipc_error = str(exc)

    def _command(self, kind, data=None):
        if not self.channel.send(kind, data):
            raise RuntimeError("串口进程 IPC 发送积压")

    def _receive(self, kind, data):
        if kind == "status":
            for key in STATUS_FIELDS:
                if key not in data:
                    continue
                if key == "current_state":
                    self._current_state = data[key]
                elif key == "cmd_counts":
                    self.cmd_counts = {int(k): v for k, v in data[key].items()}
                else:
                    setattr(self, key, data[key])
            for k, v in data["parser"].items():
                setattr(self.proto, k, v)
            self.proto.rx_buffer = bytearray(data["rx_buffer_size"])
            for key in ("ipc_dropped_events", "log_write_errors", "log_dropped", "log_current_error"):
                setattr(self, key, data.get(key, 0 if key != "log_current_error" else ""))
        elif kind == "pick":
            self.pick_report_queue.append(tuple(data))
        elif kind == "recheck":
            self.recheck_queue.append(data)
            self.recheck_requested = True
        elif kind == "finish":
            self.finished = True
            self.finish_display_pending = tuple(data)
            self.finish_summary = tuple(data)
        elif kind == "dump":
            self.dump_display_pending = True
        elif kind == "start":
            self.finished = False
            self.finish_summary = None
            self.finish_display_pending = None
        elif kind == "log":
            print("[LINK] %s" % data)

    def update(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError("串口进程退出，exit=%s" % self.process.returncode)
        if self.channel is not None:
            for kind, data in self.channel.receive():
                self._receive(kind, data)
            if self.channel.closed:
                raise RuntimeError("串口进程 IPC 断开: %s" % self.channel.last_error)

    def publish_snapshot(self, detections, completed_at=None):
        pieces, stats, self.local_items = snapshot_details(detections, self.converter)
        self.local_result_version += 1
        self.local_completed_at = time.monotonic() if completed_at is None else completed_at
        self.last_snapshot_pieces = pieces
        self.snapshot_filter_stats = stats
        self.last_scan_pieces = [(s, c, x, y, a) for s, c, _p, x, y, a in pieces]
        try:
            self._command("snapshot", dict(pieces=pieces, version=self.local_result_version,
                completed_at=self.local_completed_at))
            self.ipc_error = ""
        except Exception as exc:
            self.ipc_error = str(exc)
            raise
        if any(stats.values()):
            print("[SNAP] 过滤/截断统计: %s" % stats)
        return len(pieces)

    def take_recheck_request(self):
        seq, expected = self.recheck_queue.popleft()
        self.recheck_requested = bool(self.recheck_queue)
        self.recheck_req_seq = seq
        self.recheck_expected = [tuple(p) for p in expected]
        return seq, list(self.recheck_expected)

    def _write(self, frame):
        self._command("frame", list(frame))
        return True

    def send_recheck_result(self, expected, detections, req_seq):
        return ProtocolHandler.send_recheck_result(self, expected, detections, req_seq)

    def send_pick_ack(self, seq):
        self._command("pick_ack", seq)

    def report_pick_failed(self, seq):
        self._command("pick_failed", seq)

    def close(self):
        if self.channel is not None and not self.channel.closed:
            self.channel.send("stop")
            self.channel.flush()
        if self.process is not None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=2)
        if self.channel is not None:
            self.channel.sock.close()
        self.channel = None
        self.process = None


def worker_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args()
    channel = Messages(socket.socket(fileno=args.fd))
    journal = Journal(getattr(config, "LINK_LOG_PATH", "/root/link.log"),
        run_id=os.environ.get("VISION_RUN_ID"),
        limit=getattr(config, "DIAG_LOG_MAX_BYTES", 2097152),
        backups=getattr(config, "DIAG_LOG_BACKUPS", 3), asynchronous=True)
    with journal.phase("worker_uart"):
        serial = UART(args.port, args.baud)
    parent_pid = os.getppid()
    # 业务日志与控制 IPC 分离；不持有任何媒体资源。
    worker = LinkWorker(serial, channel, journal)
    class LogMessages:
        def write(self, text):
            if text.strip():
                journal.event("BUSINESS", text=text.strip()[:1000])
                if not channel.send("log", text.strip()[:1000]):
                    worker.dropped_events += 1
            return len(text)
        def flush(self):
            pass
    try:
        channel.send("ready")
        # UART 定时路径不做同步落盘；日志由媒体侧消费，积压时有界丢弃。
        with contextlib.redirect_stdout(LogMessages()):
            while os.getppid() == parent_pid and worker.tick():
                time.sleep(0.01)
    except BaseException:
        journal.event("FAILED", critical=True, phase="worker", stack=traceback.format_exc())
        raise
    finally:
        serial.close()
        channel.sock.close()
        journal.event("EXIT", critical=True, stopping=worker.stopping,
            ipc_closed=channel.closed, ipc_error=channel.last_error,
            expected_parent=parent_pid, actual_parent=os.getppid())
        journal.close()


if __name__ == "__main__":
    worker_main()
