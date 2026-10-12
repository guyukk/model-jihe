# -*- coding: utf-8 -*-
"""多帧投票：必须按"帧"计票，且同一帧一个聚类只收一个框

历史缺陷（本测试覆盖的回归点）：
1) 同一帧里几个相邻框可以塞进同一个聚类，凑够 min_hits 而通过
2) 两件相距很近（小于 SCAN_MATCH_DIST）的同形状物料会被并成一件
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import ensure_project_path, install_maix_stub, check, report

ensure_project_path()
install_maix_stub()

import config
import vision

FAIL = []

config.SCAN_DEBUG_LOG = False
config.SCAN_FRAME_DELAY_MS = 0
config.SCAN_FRAME_COUNT = 5
config.SCAN_MIN_HITS = 3
config.SCAN_MATCH_DIST = 20


def make_det(shape, color, bbox, score=0.8):
    """构造一条检出：取 bbox 中心作为归一化坐标"""
    cx = bbox[0] + bbox[2] / 2
    cy = bbox[1] + bbox[3] / 2
    return (shape, color, cx / 320.0, cy / 256.0, bbox, score)


def run_script(script):
    """按脚本逐帧返回检出，跑一次 detect_objects_stable"""
    state = {"i": 0}

    def fake_detect_objects(self, img):
        i = min(state["i"], len(script) - 1)
        state["i"] += 1
        dets = list(script[i])
        self.last_scan_stats = {"yolo_raw": len(dets), "roi_out": 0,
                                "color_unknown": 0, "accepted": len(dets),
                                "frames": 1, "merged": len(dets)}
        return dets

    original = vision.VisionDetector.detect_objects
    vision.VisionDetector.detect_objects = fake_detect_objects
    try:
        v = vision.VisionDetector(None, 320, 256)
        return v.detect_objects_stable(lambda: None)
    finally:
        vision.VisionDetector.detect_objects = original


print("=== 1. 两件相距 10px 的同形状物料，5 帧都在 ===")
# 中心分别 (110,110) 与 (120,110)，相距 10 < SCAN_MATCH_DIST(20)
pair = [make_det("cone", "red", (100, 100, 20, 20)),
        make_det("cone", "red", (110, 100, 20, 20))]
res = run_script([[make_det("cone", "red", (100, 100, 20, 20)),
                   make_det("cone", "red", (110, 100, 20, 20))]] * 5)
check(FAIL, "两件不被合并成一件", len(res) == 2, "得到%d件" % len(res))
if len(res) == 2:
    xs = sorted(round(r[4][0] + r[4][2] / 2) for r in res)
    check(FAIL, "两件坐标各自正确", xs == [110, 120], xs)

print("\n=== 2. 只有第 1 帧出现三个相邻框，其余帧为空 ===")
# 旧逻辑：三个框依次并入同一聚类 -> 3 票 -> 通过
# 新逻辑：同一帧一个聚类只收一个框 -> 每帧最多 1 票 -> 全部丢弃
adjacent = [make_det("cube", "blue", (100, 100, 20, 20)),
            make_det("cube", "blue", (112, 100, 20, 20)),
            make_det("cube", "blue", (124, 100, 20, 20))]
script = [adjacent] + [[] for _ in range(4)]
res = run_script(script)
check(FAIL, "同帧三框不算三票", len(res) == 0, "得到%d件" % len(res))

print("\n=== 3. 真实目标稳定出现 3 帧，后 2 帧消失 ===")
one = [make_det("cone", "yellow", (100, 100, 20, 20))]
res = run_script([[one[0]]] * 3 + [[]] * 2)
check(FAIL, "满 3 帧的目标保留", len(res) == 1, "得到%d件" % len(res))

print("\n=== 4. 真实目标只出现 2 帧，不足门槛 ===")
res = run_script([[one[0]]] * 2 + [[]] * 3)
check(FAIL, "不足 min_hits 的目标丢弃", len(res) == 0, "得到%d件" % len(res))

print("\n=== 5. 缓慢漂移的目标仍算同一件 ===")
drift = [[make_det("pyramid", "red", (100 + i * 4, 100, 20, 20))] for i in range(5)]
res = run_script(drift)
check(FAIL, "漂移目标合并为一件", len(res) == 1, "得到%d件" % len(res))

print("\n=== 6. 两件先后出现，互不串票 ===")
script = [[make_det("cone", "red", (60, 60, 20, 20))],
          [make_det("cone", "red", (60, 60, 20, 20)),
           make_det("cube", "blue", (200, 150, 20, 20))],
          [make_det("cone", "red", (60, 60, 20, 20)),
           make_det("cube", "blue", (200, 150, 20, 20))],
          [make_det("cube", "blue", (200, 150, 20, 20))],
          [make_det("cube", "blue", (200, 150, 20, 20))]]
res = run_script(script)
check(FAIL, "锥 3 帧、方 4 帧都保留", len(res) == 2, "得到%d件" % len(res))
if len(res) == 2:
    shapes = sorted(r[0] for r in res)
    check(FAIL, "形状正确", shapes == ["cone", "cube"], shapes)

sys.exit(report(FAIL))
