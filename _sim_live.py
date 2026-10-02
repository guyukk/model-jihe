# -*- coding: utf-8 -*-
"""sim_vision 实时日志包装器：不改动MCU项目文件，仅实时打印事件流"""
import sys, time
sys.path.insert(0, r"D:\文档\26工创\code_yuanshi\3d214f5f\python")

import serial
from proto import messages as M
from golden_vectors import make_scan12_payload
from sim_vision import VisionSim, _items_in_travel

PORT = "COM19"

scanned = M.ScanResult.from_bytes(make_scan12_payload()).pieces
items = _items_in_travel(scanned)
print(f"[wrapper] {len(items)} 件合成物料:")
for p in items:
    print(f"  shape=0x{p.shape:02X} color=0x{p.color:02X} "
          f"pickable={p.pickable} x={p.x/10}mm y={p.y/10}mm")
print(f"[wrapper] 打开 {PORT} @115200，开始模拟视觉端...")
print("=" * 60)

ser = serial.Serial(PORT, 115200, timeout=0.01)
vision = VisionSim(items)
start = time.monotonic()
vision.boot(0)
seen = 0
try:
    while not vision.finished:
        now = int((time.monotonic() - start) * 1000)
        data = ser.read(ser.in_waiting or 1)
        if data:
            vision.deliver(data)
        out = vision.take_out()
        if out:
            ser.write(out)
        vision.tick(now)
        # 实时打印新事件
        while seen < len(vision.events):
            t, kind, info = vision.events[seen]
            print(f"[{t:8d}ms] {kind} {info}", flush=True)
            seen += 1
        time.sleep(0.002)
except KeyboardInterrupt:
    pass
finally:
    ser.close()
print("=" * 60)
print("[wrapper] 结束，事件总数:", len(vision.events))
