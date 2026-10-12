"""启动阶段定位、日志保留/轮转、通信侧独立证据与写盘故障。"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _stubs import ensure_project_path, install_maix_stub, FakeSerial
ensure_project_path()
maix, image, _ = install_maix_stub()
import main
import config
from diagnostics import Journal
from link_service import LinkWorker


class DiagnosticTests(unittest.TestCase):
    def test_history_rotation_identifiers_and_phase_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "startup.log"
            path.write_text("old history\n", encoding="utf-8")
            journal = Journal(str(path), run_id="test-run", limit=500, backups=2)
            for idx in range(12):
                journal.event("tick", index=idx)
            with self.assertRaises(RuntimeError):
                with journal.phase("camera"):
                    raise RuntimeError("camera init failed")
            records = []
            for candidate in Path(tmp).glob("startup.log*"):
                self.assertLess(candidate.stat().st_size, 2500)
                records.extend(json.loads(line) for line in candidate.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(len(list(Path(tmp).glob("startup.log*"))), 3)
            failed = [r for r in records if r["event"] == "FAILED"]
            self.assertEqual(failed[-1]["phase"], "camera")
            self.assertIn("camera init failed", failed[-1]["stack"])
            self.assertEqual(failed[-1]["run"], "test-run")
            self.assertIn("mono", failed[-1])
            self.assertIn("boot", failed[-1])

    def test_actual_model_and_camera_init_failure_have_enter_and_stack(self):
        class Guard:
            def __init__(self, *args):
                pass
            def close(self):
                pass
        for target in ("model", "camera"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                journal = Journal(os.path.join(tmp, "startup.log"))
                stack.enter_context(patch.object(main, "STARTUP", journal))
                stack.enter_context(patch.object(config, "DIAG_LOG_PATH", os.path.join(tmp, "media.log")))
                stack.enter_context(patch.object(main.uart, "UART", Guard))
                stack.enter_context(patch.object(main.LinkProcess, "start", lambda self: None))
                stack.enter_context(patch.object(main.signal, "signal", lambda *a: None))
                stack.enter_context(patch.object(main.nn, "YOLO26",
                    side_effect=RuntimeError("model broken") if target == "model" else None,
                    create=True))
                stack.enter_context(patch.object(main.camera, "Camera", side_effect=RuntimeError("camera broken"), create=True))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                with self.assertRaises(RuntimeError):
                    main.main()
                records = [json.loads(line) for line in Path(journal.path).read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(r["event"] == "ENTER" and r.get("phase") == target for r in records))
                self.assertTrue(any(r["event"] == "FAILED" and r.get("phase") == target and "stack" in r for r in records))
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertLess(source.index("FAULT_FILE = install_fault_dump"), source.index("from maix import app"))

    def test_bounded_async_queue_and_disk_failure_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal(os.path.join(tmp, "log"), asynchronous=True, capacity=1)
            # 停止消费者后真实填满有界队列，验证调度线程不等待磁盘。
            j.close()
            j.event("first")
            j.event("second")
            self.assertEqual(j.dropped, 1)
            with patch("builtins.open", side_effect=OSError("disk failed")):
                j.event("critical", critical=True)
            self.assertEqual(j.write_errors, 1)
            self.assertIn("disk failed", j.current_error)

    def test_worker_records_old_cache_age_without_media_parent(self):
        class Channel:
            closed, tx = False, b""
            def receive(self):
                return []
            def send(self, *args):
                return True
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(os.path.join(tmp, "link.log"))
            worker = LinkWorker(FakeSerial(), Channel(), journal)
            with patch("time.monotonic", return_value=100):
                worker.handler.publish_snapshot([], result_version=7, completed_at=80)
                worker.tick()
            record = json.loads(Path(journal.path).read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["result_age_s"], 20)
            self.assertEqual(record["last_snapshot_write_version"], 7)
            self.assertEqual(record["snapshot_result_version"], 7)
            self.assertEqual(record["snapshot_send_count"], 1)
            self.assertGreater(record["tx_success_count"], 0)
            self.assertIn("write_partial_count", record)


if __name__ == "__main__":
    unittest.main()
