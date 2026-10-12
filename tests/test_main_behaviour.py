# -*- coding: utf-8 -*-
"""主程序行为：分片等待（_pump）与配置重载回滚

回归背景：
  1) 屏显与稳定等待原先直接 sleep，期间不收串口、不发心跳，
     MCU 会判定掉线并重试。改为 _pump 后必须真的在跑协议服务。
  2) _reload_config 原先先 reload(config) 再建对象，构造失败时只打日志，
     配置已是新的、检测器与转换器还是旧的，三者不一致。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import (ensure_project_path, install_maix_stub, FakeSerial,
                    check, report)

ensure_project_path()
install_maix_stub()

import config
import protocol as pmod
import protocol_handler as ph

FAIL = []


class Quiet:
    def __enter__(self):
        import io
        self._old = sys.stdout
        sys.stdout = io.StringIO()
        return self

    def __exit__(self, *a):
        sys.stdout = self._old
        return False


with Quiet():
    import main

print("=== 1. _pump 期间持续服务协议 ===")


class CountingProtocol:
    def __init__(self):
        self.calls = 0
        self.received = []

    def update(self):
        self.calls += 1


class CountingDisplay:
    def __init__(self):
        self.shows = 0

    def show(self, img):
        self.shows += 1


class FakeSelf(main.SortingVisionApp):
    def __init__(self):
        pass


obj = FakeSelf()
obj.protocol = CountingProtocol()
obj.disp = CountingDisplay()

t0 = time.time()
main.SortingVisionApp._pump(obj, 120, img=object())
elapsed = (time.time() - t0) * 1000

check(FAIL, "等待时长不小于请求值", elapsed >= 110, "%.0fms" % elapsed)
check(FAIL, "等待时长没有明显超时", elapsed < 400, "%.0fms" % elapsed)
check(FAIL, "协议服务被反复调用", obj.protocol.calls >= 3, obj.protocol.calls)
check(FAIL, "期间刷新了显示", obj.disp.shows >= 3, obj.disp.shows)

print("\n=== 2. _pump 不传 img 时不刷屏 ===")
obj2 = FakeSelf()
obj2.protocol = CountingProtocol()
obj2.disp = CountingDisplay()
main.SortingVisionApp._pump(obj2, 60)
check(FAIL, "协议仍被服务", obj2.protocol.calls >= 1, obj2.protocol.calls)
check(FAIL, "未刷新显示", obj2.disp.shows == 0, obj2.disp.shows)

print("\n=== 3. _pump 期间到达的 SCAN_REQ 会被入队 ===")
serial = FakeSerial()
handler = ph.ProtocolHandler(serial)
obj3 = FakeSelf()
obj3.protocol = handler
obj3.disp = CountingDisplay()

real_update = handler.update


def update_with_injection():
    """第一次服务时塞入一条 SCAN_REQ，模拟等待期间 MCU 发来请求"""
    if not getattr(update_with_injection, "_done", False):
        update_with_injection._done = True
        frame, _ = handler.proto.build_frame(pmod.Protocol.CMD_SCAN_REQ,
                                             b"\x01\x00", seq=0x55)
        serial.feed(frame)
    real_update()


handler.update = update_with_injection

with Quiet():
    main.SortingVisionApp._pump(obj3, 90)
check(FAIL, "废弃请求被记录但不扫描", handler.cmd_counts.get(0x43) == 1 and handler.scan_count == 0)

print("\n=== 4. _reload_config 构造失败时回滚配置 ===")
before = {k: v for k, v in vars(config).items() if k.isupper()}
marker_before = config.SCAN_MIN_HITS

obj4 = FakeSelf()
obj4.detector = object()
obj4.camera_width = 320
obj4.camera_height = 256
obj4.vision = "OLD_VISION"
obj4.protocol = FakeSelf()
obj4.protocol.converter = "OLD_CONVERTER"

original_vision_cls = main.VisionDetector


class BoomVision:
    def __init__(self, *a, **k):
        raise RuntimeError("simulated reload failure")


main.VisionDetector = BoomVision
try:
    with Quiet():
        main.SortingVisionApp._reload_config(obj4)
finally:
    main.VisionDetector = original_vision_cls

after = {k: v for k, v in vars(config).items() if k.isupper()}
check(FAIL, "配置已回滚", after == before,
      [k for k in set(before) | set(after) if before.get(k) != after.get(k)])
check(FAIL, "关键项回到原值", config.SCAN_MIN_HITS == marker_before,
      config.SCAN_MIN_HITS)
check(FAIL, "检测器未被替换", obj4.vision == "OLD_VISION", obj4.vision)
check(FAIL, "转换器未被替换", obj4.protocol.converter == "OLD_CONVERTER",
      obj4.protocol.converter)

print("\n=== 5. _reload_config 成功时三者一起替换 ===")


class OkVision:
    def __init__(self, *a, **k):
        self.tag = "NEW"


main.VisionDetector = OkVision
obj5 = FakeSelf()
obj5.detector = object()
obj5.camera_width = 320
obj5.camera_height = 256
obj5.vision = "OLD_VISION"
obj5.protocol = FakeSelf()
obj5.protocol.converter = "OLD_CONVERTER"
try:
    with Quiet():
        main.SortingVisionApp._reload_config(obj5)
finally:
    main.VisionDetector = original_vision_cls

check(FAIL, "检测器已替换", getattr(obj5.vision, "tag", None) == "NEW",
      obj5.vision)
check(FAIL, "转换器已替换", obj5.protocol.converter != "OLD_CONVERTER",
      obj5.protocol.converter)

sys.exit(report(FAIL))
