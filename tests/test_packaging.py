# -*- coding: utf-8 -*-
"""部署包必须自包含

回归背景：早期 app.yaml 只列 model_8988.mud、权重 .axmodel 不在包内，
且模型路径固定指向应用目录外的 /root/models/model-jihe/，
换台设备或清掉那个目录就跑不起来。

现在从 app.yaml 里派生 .mud 名与权重名，不再硬编码 8988/9974，切模型
只需改 app.yaml.files，本测试自动跟随。
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


def find_mud_name(declared):
    """app.yaml.files 里那个 .mud 的名字"""
    for name in declared:
        if name.endswith(".mud"):
            return name
    return None


def mud_dir_candidates(mud_name):
    """与 config._resolve_model_path 保持一致：先看 models/，再看根目录"""
    return [os.path.join(ROOT, "models", mud_name), os.path.join(ROOT, mud_name)]


def mud_assets(mud_path):
    if not mud_path or not os.path.exists(mud_path):
        return []
    text = io.open(mud_path, encoding="utf-8").read()
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("model_npu", "model_vnpu")) and "=" in line:
            out.append(line.split("=", 1)[1].strip())
    return out


def locate(name):
    for cand in (os.path.join(ROOT, name),
                 os.path.join(ROOT, "models", name),
                 os.path.join(ROOT, "shared", name)):
        if os.path.exists(cand):
            return cand
    return None


declared = read_app_yaml()
mud_name = find_mud_name(declared)

print("=== 1. app.yaml 声明了 .mud 与对应权重 ===")
check(FAIL, "app.yaml 里存在 .mud 项", mud_name is not None, declared)
# 从第一个能读到权重的路径解析 assets，作为后续断言的期望集合
primary_mud = next((p for p in mud_dir_candidates(mud_name) if os.path.exists(p)), None) if mud_name else None
assets = mud_assets(primary_mud)
for a in assets:
    check(FAIL, "app.yaml 列出了权重 %s" % a, a in declared, declared)

print("\n=== 2. .mud 引用的权重都能在源码树里找到 ===")
for name in assets:
    check(FAIL, "找得到 %s" % name, locate(name) is not None)

print("\n=== 3. app.yaml 声明的文件都能定位 ===")
for name in declared:
    check(FAIL, "定位 %s" % name, locate(name) is not None)

print("\n=== 4. config 的模型路径解析逻辑 ===")
import config  # noqa: E402

resolved = os.path.abspath(config.MODEL_PATH)
# 与 config 保持同样的探测顺序：models/ 优先，其次根；且要求权重同目录
expected_local = None
for cand in mud_dir_candidates(mud_name) if mud_name else []:
    if not os.path.exists(cand):
        continue
    sibling_ok = all(os.path.exists(os.path.join(os.path.dirname(cand), n)) for n in mud_assets(cand))
    if sibling_ok:
        expected_local = os.path.abspath(cand)
        break

if expected_local:
    check(FAIL, "权重齐备时优先用包内 .mud",
          resolved == expected_local, "%s != %s" % (resolved, expected_local))
else:
    check(FAIL, "权重缺失时退回设备路径",
          resolved == os.path.abspath(os.path.join("/root/models/model-jihe", mud_name)),
          resolved)

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
    for name in assets:
        check(FAIL, "包内含权重 %s" % name, name in names)
    check(FAIL, "包内含 app.png", "app.png" in names)
    # 权重与 .mud 必须在同一层，否则相对引用解析不到
    check(FAIL, "权重与 .mud 同层（无子目录）",
          all("/" not in n for n in names), sorted(names))

sys.exit(report(FAIL))
