# -*- coding: utf-8 -*-
"""串口写必须检查返回值

历史缺陷：_write() 只看有没有抛异常，忽略 uart.write 的返回值。
返回 -1（错误）或只写出一部分时仍被当作成功，错误计数保持 0，
屏幕和日志都显示"已发送"，而 MCU 实际没收到完整帧。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import (ensure_project_path, install_maix_stub, FakeSerial,
                    check, report)

ensure_project_path()
install_maix_stub()

import config
import protocol as pmod
import protocol_handler as ph

FAIL = []
config.SCAN_DEBUG_LOG = False


class Quiet:
    def __enter__(self):
        import io
        self._old = sys.stdout
        sys.stdout = io.StringIO()
        return self

    def __exit__(self, *a):
        sys.stdout = self._old
        return False


FRAME, _ = pmod.Protocol().build_frame(0x01, b"\x01\x00\x00")
FRAME = bytes(FRAME)

print("=== 1. 正常写入 ===")
serial = FakeSerial(ret=len(FRAME))
h = ph.ProtocolHandler(serial)
with Quiet():
    ok = h._write(FRAME)
check(FAIL, "返回 True", ok is True, ok)
check(FAIL, "无错误计数", h.write_error_count == 0, h.write_error_count)
check(FAIL, "无截断计数", h.write_partial_count == 0, h.write_partial_count)

print("\n=== 2. 返回负值（官方接口约定为错误）===")
serial = FakeSerial(ret=-1)
h = ph.ProtocolHandler(serial)
with Quiet():
    ok = h._write(FRAME)
check(FAIL, "返回 False", ok is False, ok)
check(FAIL, "错误计数 +1", h.write_error_count == 1, h.write_error_count)

print("\n=== 3. 只写出一部分字节 ===")
serial = FakeSerial(ret=len(FRAME) - 2)
h = ph.ProtocolHandler(serial)
with Quiet():
    ok = h._write(FRAME)
check(FAIL, "返回 False", ok is False, ok)
check(FAIL, "截断计数 +1", h.write_partial_count == 1, h.write_partial_count)
check(FAIL, "不计入写错误", h.write_error_count == 0, h.write_error_count)

print("\n=== 4. 返回 0 字节 ===")
serial = FakeSerial(ret=0)
h = ph.ProtocolHandler(serial)
with Quiet():
    ok = h._write(FRAME)
check(FAIL, "返回 False", ok is False, ok)
check(FAIL, "截断计数 +1", h.write_partial_count == 1, h.write_partial_count)

print("\n=== 5. 返回 None 视为不提供长度信息 ===")
serial = FakeSerial(ret=None)
h = ph.ProtocolHandler(serial)
with Quiet():
    ok = h._write(FRAME)
check(FAIL, "返回 True", ok is True, ok)
check(FAIL, "无错误计数", h.write_error_count == 0)

print("\n=== 6. 抛异常仍被兜住 ===")
serial = FakeSerial(raise_on_write=True)
h = ph.ProtocolHandler(serial)
with Quiet():
    ok = h._write(FRAME)
check(FAIL, "返回 False", ok is False, ok)
check(FAIL, "错误计数 +1", h.write_error_count == 1, h.write_error_count)

print("\n=== 7. 返回值异常类型时按成功处理，不崩溃 ===")
serial = FakeSerial(ret="nonsense")
h = ph.ProtocolHandler(serial)
with Quiet():
    ok = h._write(FRAME)
check(FAIL, "不抛异常并返回 True", ok is True, ok)

print("\n=== 8. 心跳发送经 _write 统计正确 ===")
serial = FakeSerial(ret=-1)
h = ph.ProtocolHandler(serial)
with Quiet():
    h.send_heartbeat()
check(FAIL, "心跳失败被计数", h.write_error_count == 1, h.write_error_count)

sys.exit(report(FAIL))
