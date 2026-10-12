# -*- coding: utf-8 -*-
"""标定工具必须从原始帧采样，不能采自己画过的界面

历史缺陷：标定画面先画准星、绿色采样框、黄色分割线(x=160)和右侧白色掩码，
然后才在中心 20x20 区域采样。采样区恰好横跨分割线与掩码，
采到的样本里混进界面颜色，自动设出的阈值就被带偏，越标越错。

本测试分两部分：
  1) 结构断言：源码里 sample_center_region 的调用必须早于任何 draw_ 调用
  2) 行为断言：同一份中心区域，纯色画面与叠加界面后的采样结果差异显著，
     证明"先采样"不是形式要求
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import ensure_project_path, check, report

ensure_project_path()

import cv2
import numpy as np

FAIL = []

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "calibration", "calibrate_hsv_v3.py")
SOURCE = open(TOOL, encoding="utf-8").read()
TREE = ast.parse(SOURCE)

print("=== 1. 结构断言：采样早于绘制 ===")
# 找到主循环 while not app.need_exit()
loop = None
for node in ast.walk(TREE):
    if isinstance(node, ast.While):
        seg = ast.get_source_segment(SOURCE, node.test) or ""
        if "need_exit" in seg:
            loop = node
            break
check(FAIL, "找到主循环", loop is not None)

draw_lines = []
sample_lines = []
for node in ast.walk(loop):
    if not isinstance(node, ast.Call):
        continue
    # 采样是裸函数调用，绘制是 img.draw_xxx 属性调用，两种都要认
    if isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        if attr.startswith("draw_"):
            draw_lines.append(node.lineno)
        elif attr == "sample_center_region":
            sample_lines.append(node.lineno)
    elif isinstance(node.func, ast.Name):
        if node.func.id == "sample_center_region":
            sample_lines.append(node.lineno)

check(FAIL, "主循环内有采样调用", len(sample_lines) > 0, sample_lines)
check(FAIL, "主循环内有绘制调用", len(draw_lines) > 0, len(draw_lines))
if sample_lines and draw_lines:
    check(FAIL, "采样行号小于所有绘制行号",
          min(sample_lines) < min(draw_lines),
          "采样在%d行，首次绘制在%d行" % (min(sample_lines), min(draw_lines)))

# G 键处理里不能重新采样
g_calls = []
for node in ast.walk(loop):
    if isinstance(node, ast.If):
        seg = ast.get_source_segment(SOURCE, node.test) or ""
        if "'G'" in seg or '"G"' in seg:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "sample_center_region"):
                    g_calls.append(sub.lineno)
check(FAIL, "G 键分支不再重新采样", len(g_calls) == 0, g_calls)

print("\n=== 2. 行为断言：界面颜色确实会污染采样 ===")
# 取出 sample_center_region 的源码，注入常量与像素访问器后单独执行
fn_node = next(n for n in TREE.body
               if isinstance(n, ast.FunctionDef) and n.name == "sample_center_region")
fn_src = ast.get_source_segment(SOURCE, fn_node)

CAMERA_WIDTH, CAMERA_HEIGHT, CENTER_SAMPLE_SIZE = 320, 256, 20

# 纯蓝色 RGB；(0,0,255) 在 OpenCV 的 HSV 里 H≈120
PURE_BLUE = (0, 0, 255)
UI_YELLOW = (255, 255, 0)   # 分割线 COLOR_YELLOW
UI_GREEN = (0, 255, 0)      # 准星与采样框 COLOR_GREEN
UI_WHITE = (255, 255, 255)  # 右侧掩码


def make_pixel(overlay=False):
    cx, cy = CAMERA_WIDTH // 2, CAMERA_HEIGHT // 2
    half = CENTER_SAMPLE_SIZE // 2
    split_x = CAMERA_WIDTH // 2

    def get(x, y):
        if not overlay:
            return PURE_BLUE
        # 黄色分割线：x == split_x，纵贯全画面
        if x == split_x:
            return UI_YELLOW
        # 右侧掩码：x >= split_x
        if x >= split_x:
            return UI_WHITE
        # 绿色采样框边框
        if (cx - half <= x <= cx + half - 1
                and (y == cy - half or y == cy + half - 1)):
            return UI_GREEN
        if (cy - half <= y <= cy + half - 1
                and (x == cx - half or x == cx + half - 1)):
            return UI_GREEN
        return PURE_BLUE

    return get


def sample_with(get_pixel):
    ns = {"np": np, "cv2": cv2,
          "CAMERA_WIDTH": CAMERA_WIDTH, "CAMERA_HEIGHT": CAMERA_HEIGHT,
          "CENTER_SAMPLE_SIZE": CENTER_SAMPLE_SIZE,
          "_get_rgb_pixel": lambda img, x, y: get_pixel(x, y)}
    exec(fn_src, ns)
    return ns["sample_center_region"](None)


clean = sample_with(make_pixel(overlay=False))
dirty = sample_with(make_pixel(overlay=True))
print("  纯色画面 H 均值 = %d" % clean["h"][0])
print("  叠加界面 H 均值 = %d" % dirty["h"][0])

check(FAIL, "纯色画面 H 落在蓝色区间", 110 <= clean["h"][0] <= 130, clean["h"][0])
check(FAIL, "叠加界面会显著改变 H 均值",
      abs(dirty["h"][0] - clean["h"][0]) > 20,
      "差 %d" % abs(dirty["h"][0] - clean["h"][0]))
check(FAIL, "纯色画面 S 饱和度高", clean["s"][0] > 200, clean["s"][0])

sys.exit(report(FAIL))
