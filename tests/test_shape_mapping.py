# -*- coding: utf-8 -*-
"""形状映射一致性回归

背景: 视觉侧一次改形状要同时动 5 处 — config.SHAPE_MAPPING(模型 label->协议字节)、
protocol.Protocol.SHAPE_* + SHAPE_NAMES(常量+显示名)、screen_ui.SHAPES_ZH/EN(界面名)、
main._draw_piece_picture(图标)、以及 .mud 里的 labels 字段。任何一处漂移都会造成
运行时"模型输出了映射里没有的 label -> 兜底 0x06 异形 -> MCU 判不可抓"这类
静默退化。本测试锁死三件事:

1. config.SHAPE_MAPPING.keys() 必须完全等于当前打包模型 .mud 的 labels 集合。
2. SHAPE_MAPPING 每个字节都能在 protocol.SHAPE_NAMES、screen_ui.SHAPES_ZH/EN
   里查到名字(不出现"未知")。
3. 7 类都是可抓形状, 没有任何 label 直接映射到 SHAPE_IRREGULAR(0x06);
   0x06 只作为未知 label 的兜底存在。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import ensure_project_path, install_maix_stub, check, report

ensure_project_path()
install_maix_stub()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAIL = []

import config
import protocol
from protocol import Protocol
import screen_ui


def find_active_mud():
    """跟随 app.yaml, 找到当前工程使用的那个 .mud 文件路径"""
    yaml = io.open(os.path.join(ROOT, "app.yaml"), encoding="utf-8").read()
    for line in yaml.splitlines():
        s = line.strip().lstrip("- ")
        if s.endswith(".mud"):
            name = s
            for cand in (os.path.join(ROOT, "models", name), os.path.join(ROOT, name)):
                if os.path.exists(cand):
                    return cand
    return None


def read_mud_labels(mud_path):
    text = io.open(mud_path, encoding="utf-8").read()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("labels") and "=" in line:
            raw = line.split("=", 1)[1]
            return [x.strip() for x in raw.split(",") if x.strip()]
    return []


mud = find_active_mud()
labels = read_mud_labels(mud) if mud else []
shape_ids = sorted(config.SHAPE_MAPPING.values())
known_names = set(Protocol.SHAPE_NAMES.keys()) if hasattr(Protocol, "SHAPE_NAMES") else set(protocol.SHAPE_NAMES.keys())
zh_keys = set(screen_ui.SHAPES_ZH.keys())
en_keys = set(screen_ui.SHAPES_EN.keys())

print("=== 1. SHAPE_MAPPING 覆盖 .mud labels ===")
check(FAIL, "找到当前使用的 .mud", mud is not None)
check(FAIL, "SHAPE_MAPPING.keys() == .mud labels (集合相等)",
      set(config.SHAPE_MAPPING.keys()) == set(labels),
      "mapping=%s mud=%s" % (sorted(config.SHAPE_MAPPING.keys()), labels))

print("\n=== 2. 每个映射字节都有显示名 (不出现未知) ===")
for label, sid in sorted(config.SHAPE_MAPPING.items(), key=lambda kv: kv[1]):
    check(FAIL, "%s -> 0x%02X 在 Protocol.SHAPE_NAMES" % (label, sid),
          sid in known_names, sorted(known_names))
    check(FAIL, "%s -> 0x%02X 在 SHAPES_ZH" % (label, sid), sid in zh_keys)
    check(FAIL, "%s -> 0x%02X 在 SHAPES_EN" % (label, sid), sid in en_keys)

print("\n=== 3. 未知 label 走 0x06 异形兜底, 且 0x06 不出现在 mapping 里 ===")
check(FAIL, "SHAPE_IRREGULAR == 0x06",
      Protocol.SHAPE_IRREGULAR == 0x06, Protocol.SHAPE_IRREGULAR)
check(FAIL, "没有任何已知 label 映射到 SHAPE_IRREGULAR (否则真未知就没兜底了)",
      Protocol.SHAPE_IRREGULAR not in config.SHAPE_MAPPING.values(),
      config.SHAPE_MAPPING)
check(FAIL, "SHAPE_MAPPING.get(未知, 6) == 6",
      config.SHAPE_MAPPING.get("__no_such_label__", 6) == 6)

print("\n=== 4. 形状协议字节唯一 ===")
check(FAIL, "SHAPE_MAPPING 各 value 互不重复",
      len(set(config.SHAPE_MAPPING.values())) == len(config.SHAPE_MAPPING),
      shape_ids)

sys.exit(report(FAIL))
