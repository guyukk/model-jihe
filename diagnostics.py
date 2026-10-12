"""不导入 Maix 的启动取证与有界日志；通信线程不等待磁盘。"""
import contextlib
import faulthandler
import json
import os
import queue
import signal
import threading
import time
import traceback
import uuid


def rotate(path, limit, backups):
    if not os.path.exists(path) or os.path.getsize(path) < limit:
        return
    for n in range(backups, 0, -1):
        src = path if n == 1 else path + ".%d" % (n - 1)
        dst = path + ".%d" % n
        if os.path.exists(src):
            os.replace(src, dst)


class Journal:
    def __init__(self, path, run_id=None, limit=2 * 1024 * 1024, backups=3,
                 asynchronous=False, capacity=256):
        self.path, self.limit, self.backups = path, limit, backups
        self.run_id = run_id or uuid.uuid4().hex
        try:
            with open("/proc/sys/kernel/random/boot_id") as f:
                self.boot_id = f.read().strip()
        except OSError:
            self.boot_id = "unavailable"
        self.dropped = self.write_errors = 0
        self.current_error = ""
        self.queue = queue.Queue(capacity) if asynchronous else None
        self.stopping = threading.Event()
        self.thread = None
        if asynchronous:
            self.thread = threading.Thread(target=self._drain, daemon=True)
            self.thread.start()

    def event(self, kind, critical=False, **fields):
        record = dict(boot=self.boot_id, run=self.run_id, pid=os.getpid(),
                      mono=round(time.monotonic(), 6), wall=time.time(), event=kind,
                      log_dropped=self.dropped, log_errors=self.write_errors, **fields)
        if self.queue is not None and not critical:
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                self.dropped += 1
        else:
            self._write(record, sync=critical)

    def _write(self, record, sync=False):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            rotate(self.path, self.limit, self.backups)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                if sync:
                    os.fsync(f.fileno())
            self.current_error = ""
        except OSError as exc:
            self.write_errors += 1
            self.current_error = str(exc)

    def _drain(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                record = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._write(record)

    @contextlib.contextmanager
    def phase(self, name):
        self.event("ENTER", critical=True, phase=name)
        started = time.monotonic()
        try:
            yield
        except BaseException:
            self.event("FAILED", critical=True, phase=name, stack=traceback.format_exc())
            raise
        self.event("OK", critical=True, phase=name,
                   duration_ms=round((time.monotonic() - started) * 1000, 3))

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


def install_fault_dump(path, journal, limit=2 * 1024 * 1024, backups=3):
    """只在启动时轮转栈文件；运行中保持 fd 有效，避免信号写入已关闭的 fd。"""
    try:
        rotate(path, limit, backups)
        f = open(path, "a", encoding="utf-8")
        f.write("boot=%s run=%s mono=%.6f pid=%s\n" %
                (journal.boot_id, journal.run_id, time.monotonic(), os.getpid()))
        f.flush()
        os.fsync(f.fileno())
        faulthandler.enable(file=f, all_threads=True)
        if hasattr(signal, "SIGUSR1") and hasattr(faulthandler, "register"):
            faulthandler.register(signal.SIGUSR1, file=f, all_threads=True, chain=False)
        return f
    except Exception as exc:
        journal.event("FAULT_SETUP_FAILED", critical=True, error=str(exc))
        return None
