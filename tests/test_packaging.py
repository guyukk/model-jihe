# -*- coding: utf-8 -*-
"""部署包必须自包含

回归背景：app.yaml 原先只列 model_8988.mud，权重 .axmodel 不在包内，
模型路径又固定指向应用目录外的 /root/models/model-jihe/。
换台设备或清掉那个目录就跑不起来。
"""
import io
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import ensure_project_path, check, report

ensure_project_path()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAIL = []


def read_app_yaml():
    text = io.open(os.path.join(ROOT, "app.yaml"), encoding="utf-8").read()
    files, in_files = [], False
    for line in text.splitlines():
        if re.match(r"^files\s*:", line):
            in_files = True
            continue
        if in_files:
            m = re.match(r"^\s+-\s+(\S+)\s*$", line)
            if m:
                files.append(m.group(1))
            elif line and not line.startswith((" ", "\t")):
                in_files = False
    return files


def mud_assets(name="model_8988.mud"):
    for cand in (os.path.join(ROOT, name), os.path.join(ROOT, "models", name)):
        if os.path.exists(cand):
            text = io.open(cand, encoding="utf-8").read()
            out = []
            for line in text.splitlines():
                line = line.strip()
                if line.startswith(("model_npu", "model_vnpu")) and "=" in line:
                    out.append(line.split("=", 1)[1].strip())
            return out
    return []


def locate(name):
    for cand in (os.path.join(ROOT, name),
                 os.path.join(ROOT, "models", name),
                 os.path.join(ROOT, "shared", name)):
        if os.path.exists(cand):
            return cand
    return None


print("=== 1. app.yaml 声明了模型权重 ===")
declared = read_app_yaml()
check(FAIL, "app.yaml 列出了 npu 权重",
      "model_8988_npu.axmodel" in declared, declared)
check(FAIL, "app.yaml 列出了 vnpu 权重",
      "model_8988_vnpu.axmodel" in declared, declared)

print("\n=== 2. .mud 引用的权重都能在源码树里找到 ===")
for name in mud_assets():
    check(FAIL, "找得到 %s" % name, locate(name) is not None)

print("\n=== 3. app.yaml 声明的文件都能定位 ===")
for name in declared:
    check(FAIL, "定位 %s" % name, locate(name) is not None)

print("\n=== 4. config 的模型路径解析逻辑 ===")
import config  # noqa: E402

resolved = config.MODEL_PATH
local_mud = os.path.join(ROOT, "model_8988.mud")
local_ok = all(os.path.exists(os.path.join(ROOT, n)) for n in mud_assets())
if local_ok:
    check(FAIL, "权重齐备时优先用包内 .mud",
          os.path.abspath(resolved) == os.path.abspath(local_mud), resolved)
else:
    check(FAIL, "权重缺失时退回设备路径",
          resolved == "/root/models/model-jihe/model_8988.mud", resolved)

print("\n=== 5. 打包产物自包含（若已构建）===")
meta = io.open(os.path.join(ROOT, "app.yaml"), encoding="utf-8").read()
ver = re.search(r"^version\s*:\s*(\S+)", meta, re.M).group(1)
app_id = re.search(r"^id\s*:\s*(\S+)", meta, re.M).group(1)
zip_path = os.path.join(ROOT, "dist", "maix-%s-v%s.zip" % (app_id, ver))
if not os.path.exists(zip_path):
    print("  (未构建，跳过: %s)" % os.path.basename(zip_path))
else:
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
    check(FAIL, "包内含 app.yaml", "app.yaml" in names, sorted(names))
    for name in declared:
        check(FAIL, "包内含 %s" % name, name in names)
    for name in mud_assets():
        check(FAIL, "包内含权重 %s" % name, name in names)
    check(FAIL, "包内含 app.png", "app.png" in names)
    # 权重与 .mud 必须在同一层，否则相对引用解析不到
    check(FAIL, "权重与 .mud 同层（无子目录）",
          all("/" not in n for n in names), sorted(names))

sys.exit(report(FAIL))
