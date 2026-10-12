# -*- coding: utf-8 -*-
"""复核必须拒绝残缺请求，且不得把"识别不了"当成"清单一致"

历史缺陷（本测试覆盖的回归点）：
1) 未知颜色的目标先被剔除再比对，剔除后实际清单变空，
   期望清单也为空时判成 match=1
2) RECHECK_REQ 声明 2 件却只给 1 件时，解码器返回残缺清单并照常比对
"""
import os
import struct
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
    """吞掉被测代码的 stdout，让测试输出干净"""

    def __enter__(self):
        import io
        self._old = sys.stdout
        sys.stdout = io.StringIO()
        return self

    def __exit__(self, *a):
        sys.stdout = self._old
        return False


def run_recheck(expected, detections):
    serial = FakeSerial()
    h = ph.ProtocolHandler(serial)
    with Quiet():
        match, _n = h.send_recheck_result(expected, detections)
    return match, serial


print("=== 1. 期望为空 + 画面有一件颜色未识别的物料 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
with Quiet():
    match, _ = h.send_recheck_result(
        [], [("cube", "orange", 0.5, 0.5, (0, 0, 10, 10), 0.9)])
check(FAIL, "不得判为一致", match == 0, "match=%d" % match)

print("\n=== 2. 期望为空 + 画面真的没有物料 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
with Quiet():
    match, _ = h.send_recheck_result([], [])
check(FAIL, "空对空才算一致", match == 1, "match=%d" % match)

print("\n=== 3. 期望 1 件 + 画面 1 件颜色未识别 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
with Quiet():
    match, _ = h.send_recheck_result(
        [(pmod.Protocol.SHAPE_SQUARE, pmod.Protocol.COLOR_BLUE)],
        [("cube", "orange", 0.5, 0.5, (0, 0, 10, 10), 0.9)])
check(FAIL, "未识别不得算匹配", match == 0, "match=%d" % match)

print("\n=== 4. 期望与画面完全一致 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
with Quiet():
    match, _ = h.send_recheck_result(
        [(2, 5)],
        [("cube", "blue", 0.5, 0.5, (0, 0, 10, 10), 0.9)])
check(FAIL, "一致时 match=1", match == 1, "match=%d" % match)

print("\n=== 5. RECHECK_REQ 解码：完整请求 ===")
p = pmod.Protocol()
got = p.decode_recheck_req(bytes([2, 0x02, 0x05, 0x01, 0x03]))
check(FAIL, "完整请求正常解出", got == [(2, 5), (1, 3)], got)

print("\n=== 6. RECHECK_REQ 解码：声明 2 件只给 1 件 ===")
p = pmod.Protocol()
got = p.decode_recheck_req(bytes([2, 0x02, 0x05]))
check(FAIL, "残缺请求返回 None", got is None, got)
check(FAIL, "残缺计数被记录", p.recheck_req_truncated == 1,
      p.recheck_req_truncated)

print("\n=== 7. RECHECK_REQ 解码：声明 0 件（合法）===")
p = pmod.Protocol()
got = p.decode_recheck_req(bytes([0]))
check(FAIL, "空清单合法", got == [], got)

print("\n=== 8. RECHECK_REQ 解码：空 PAYLOAD ===")
p = pmod.Protocol()
got = p.decode_recheck_req(b"")
check(FAIL, "空负载返回 None", got is None, got)

print("\n=== 9. 残缺请求经 _handle_recheck_req 不产生应答 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
with Quiet():
    # PAYLOAD 声明 2 件只带 1 件
    h._handle_recheck_req(0x21, bytes([2, 0x02, 0x05]))
frames = [f for f in serial.written if f[3] == 0x03]
check(FAIL, "残缺请求不回复复核结果", len(frames) == 0, len(frames))

sys.exit(report(FAIL))
