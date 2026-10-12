# -*- coding: utf-8 -*-
"""事务隔离：本轮的 SEQ 与期望清单不能被执行期间到达的新请求改写

回归背景（分片服务引入后暴露）：
  扫描取出 0x21 后在等待期间收到 0x22，_handle_scan_req() 会改写共享的
  scan_req_seq，导致第一轮响应使用 0x22、队列里还留 0x22，0x21 永远没有响应。
  复核同理，新的 RECHECK_REQ 会替换共享的期望清单，改变本轮的判定结果。
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

DET = [("cube", "blue", 0.5, 0.5, (10, 10, 20, 20), 0.9)]
SCAN = pmod.Protocol.CMD_SCAN_REQ


class Quiet:
    def __enter__(self):
        import io
        self._old = sys.stdout
        sys.stdout = io.StringIO()
        return self

    def __exit__(self, *a):
        sys.stdout = self._old
        return False


def frames_of(serial, cmd):
    out = []
    for f in serial.written:
        if f[3] == cmd:
            out.append(f)
    return out


print("=== v3.1 周期快照不回用 MCU 请求 SEQ ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
with Quiet():
    h._handle_scan_req(0x21, bytes([1, 0]))
    h.publish_snapshot([(2, 5, 1, 100, 200, 65535)])
    h.send_snapshot()
got = frames_of(serial, pmod.Protocol.CMD_SCAN_RESULT)
check(FAIL, "主动快照使用视觉发送序号", got[0][4] == 0)
check(FAIL, "快照号独立递增", got[0][5] == 1)

print("\n=== 4. 复核：执行期间新请求不改写本轮期望清单 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
with Quiet():
    # 第一条：期望 1 件 方形/蓝色
    h._handle_recheck_req(0x30, bytes([1, 0x02, 0x05]))
    expected_a = list(h.recheck_expected)
    seq_a = h.recheck_req_seq
    # 复核扫描期间来了第二条：期望 2 件不同清单
    h._handle_recheck_req(0x31, bytes([2, 0x01, 0x01, 0x03, 0x03]))
    # 本轮用快照 A 判定，且画面确实命中 A
    match, _ = h.send_recheck_result(expected_a, DET, seq_a)

check(FAIL, "快照 A 与共享字段已不同", expected_a != h.recheck_expected,
      (expected_a, h.recheck_expected))
check(FAIL, "本轮判定基于快照 A（一致）", match == 1, match)
got = frames_of(serial, pmod.Protocol.CMD_RECHECK_RESULT)
check(FAIL, "复核结果回 0x30", got and got[0][4] == 0x30,
      hex(got[0][4]) if got else None)

print("\n=== 5. 第二条复核请求在下一轮用自己的清单 ===")
serial2 = FakeSerial()
h2 = ph.ProtocolHandler(serial2)
with Quiet():
    h2._handle_recheck_req(0x40, bytes([1, 0x02, 0x05]))
    exp_a, seq_a = list(h2.recheck_expected), h2.recheck_req_seq
    h2._handle_recheck_req(0x41, bytes([2, 0x01, 0x01, 0x03, 0x03]))
    # 第二轮用快照 B；画面仍是方形/蓝色，与 B 不一致
    exp_b, seq_b = list(h2.recheck_expected), h2.recheck_req_seq
    m_a, _ = h2.send_recheck_result(exp_a, DET, seq_a)
    m_b, _ = h2.send_recheck_result(exp_b, DET, seq_b)
check(FAIL, "第一轮匹配", m_a == 1, m_a)
check(FAIL, "第二轮不匹配", m_b == 0, m_b)
got = frames_of(serial2, pmod.Protocol.CMD_RECHECK_RESULT)
check(FAIL, "两条复核结果各自回原 SEQ",
      [f[4] for f in got] == [0x40, 0x41], [hex(f[4]) for f in got])

sys.exit(report(FAIL))
