# -*- coding: utf-8 -*-
"""定时必须走单调时钟，不能被系统校时影响

已离线验证的两种危害：
  - 墙上时钟后跳：心跳迟迟不发
  - 墙上时钟前跳：造成假的链路超时（LINK 变 DISC）

本测试把 pytime.time 换成剧烈跳变的值，断言心跳节奏与链路判定不受影响，
并用源码检查守住"不再出现 pytime.time() 做定时"这条约定。
"""
import io
import os
import sys
import time as _real_time

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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print("=== 1. 源码约定：定时不得用 pytime.time() ===")
for name in ("protocol_handler.py", "main.py"):
    src = io.open(os.path.join(ROOT, name), encoding="utf-8").read()
    check(FAIL, "%s 无 pytime.time()" % name, "pytime.time()" not in src)
    check(FAIL, "%s 使用 monotonic" % name, "pytime.monotonic()" in src)


def hb_frames(serial):
    return [f for f in serial.written if f[3] == pmod.Protocol.CMD_HB_V]


print("\n=== 2. 墙上时钟后跳不影响心跳 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
interval = config.HEARTBEAT_INTERVAL

# 让心跳到期（用单调时钟设置）
h.last_heartbeat_time = h.last_heartbeat_time - interval - 0.05

# 把墙上时钟换成"往后跳 1000 秒"的假时钟
orig_time = ph.pytime.time
ph.pytime.time = lambda: orig_time() - 1000.0
try:
    h.update()
finally:
    ph.pytime.time = orig_time

check(FAIL, "心跳照常发出", len(hb_frames(serial)) >= 1, len(hb_frames(serial)))

print("\n=== 3. 墙上时钟前跳不造成假链路超时 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
h.link_alive = True
# MCU 心跳刚刚来过
h.last_mcu_heartbeat_time = ph.pytime.monotonic()

ph.pytime.time = lambda: orig_time() + 100000.0
try:
    h.check_link_alive()
    alive_after_jump = h.link_alive
finally:
    ph.pytime.time = orig_time

check(FAIL, "时钟前跳不判 DISC", alive_after_jump is True, alive_after_jump)

print("\n=== 4. 真正超时仍能判 DISC（功能没被削弱）===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
h.link_alive = True
h.last_mcu_heartbeat_time = ph.pytime.monotonic() - 10.0
h.check_link_alive()
check(FAIL, "确实超时会判 DISC", h.link_alive is False, h.link_alive)

print("\n=== 5. 心跳周期由单调时钟决定 ===")
serial = FakeSerial()
h = ph.ProtocolHandler(serial)
h.update()
first = len(hb_frames(serial))
# 立刻再调一次：未到期，不应重复发
h.update()
check(FAIL, "未到期不重复发心跳",
      len(hb_frames(serial)) == first, (first, len(hb_frames(serial))))

# 把上次发送时刻往前挪一个周期，应当再发一条
h.last_heartbeat_time = h.last_heartbeat_time - interval - 0.01
ph.pytime.time = lambda: orig_time() - 5000.0     # 墙上时钟捣乱
try:
    h.update()
finally:
    ph.pytime.time = orig_time
check(FAIL, "到期后照常发一条",
      len(hb_frames(serial)) == first + 1, len(hb_frames(serial)))

print("\n=== 6. 时间戳仍用墙上时钟（给人和日志看）===")
src = io.open(os.path.join(ROOT, "protocol_handler.py"), encoding="utf-8").read()
check(FAIL, "仍保留 strftime/localtime 用于显示",
      "pytime.strftime" in src and "pytime.localtime" in src)

sys.exit(report(FAIL))
