# -*- coding: utf-8 -*-
"""组装可安装的应用包

为什么需要这个脚本：
    源码目录是按用途分类的（calibration/ tests/ docs/ models/ ...），
    但 .mud 里的 model_npu / model_vnpu 是**相对文件名**，
    运行时会相对 .mud 所在目录解析。所以打包时必须把权重和 .mud
    放在同一层，且 app.yaml 的 files 列表要与包内实际内容一致。

产物：
    dist/maix-<id>-v<version>.zip

用法：
    python tools/package_app.py
    python tools/package_app.py --out dist/xxx.zip
"""
import argparse
import io
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR = os.path.join(ROOT, "build")


def read_app_meta():
    """从 app.yaml 读出 id / version / files"""
    path = os.path.join(ROOT, "app.yaml")
    text = io.open(path, encoding="utf-8").read()
    meta = {}
    files = []
    in_files = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if re.match(r"^files\s*:", line):
            in_files = True
            continue
        if in_files:
            m = re.match(r"^\s+-\s+(\S+)\s*$", line)
            if m:
                files.append(m.group(1))
                continue
            if not line.startswith((" ", "\t")):
                in_files = False
        m = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", line)
        if m and not in_files:
            meta[m.group(1)] = m.group(2).strip()
    meta["files"] = files
    return meta


def mud_assets(mud_path):
    """从 .mud 里取出引用的权重文件名"""
    names = []
    text = io.open(mud_path, encoding="utf-8").read()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("model_npu", "model_vnpu")) and "=" in line:
            name = line.split("=", 1)[1].strip()
            if name:
                names.append(name)
    return names


def locate(name):
    """在源码树里定位一个待打包文件

    依次查找根目录（部署契约文件都在这里）、models/（权重按分类存放）、
    以及 shared/（被多个 App 共用的模块，如 lab_color.py）。
    """
    for cand in (os.path.join(ROOT, name),
                 os.path.join(ROOT, "models", name),
                 os.path.join(ROOT, "shared", name)):
        if os.path.exists(cand):
            return cand
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    meta = read_app_meta()
    app_id = meta.get("id", "app")
    version = meta.get("version", "0.0.0")
    out = args.out or os.path.join(ROOT, "dist",
                                   "maix-%s-v%s.zip" % (app_id, version))

    staging = os.path.join(BUILD_DIR, app_id)
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    missing = []

    # 步骤1： 复制 app.yaml 声明的全部文件
    for name in meta["files"]:
        src = locate(name)
        if src is None:
            missing.append(name)
            continue
        shutil.copy2(src, os.path.join(staging, name))

    # 步骤2： 复制应用元数据
    for name in ("app.yaml", "app.png"):
        src = os.path.join(ROOT, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(staging, name))

    # 步骤3： 确保 .mud 引用的权重与 .mud 同层
    mud_name = next((n for n in meta["files"] if n.endswith(".mud")), None)
    mud = os.path.join(staging, mud_name) if mud_name else None
    if mud and os.path.exists(mud):
        for name in mud_assets(mud):
            if os.path.exists(os.path.join(staging, name)):
                continue
            src = locate(name)
            if src is None:
                missing.append(name)
                continue
            shutil.copy2(src, os.path.join(staging, name))

    if missing:
        print("✗ 以下文件找不到，无法打包:")
        for m in missing:
            print("    " + m)
        return 1

    # 步骤4： 校验包内内容满足运行要求
    errors = []
    for name in meta["files"]:
        if not os.path.exists(os.path.join(staging, name)):
            errors.append("app.yaml 声明但包内缺失: %s" % name)
    if mud and os.path.exists(mud):
        for name in mud_assets(mud):
            if not os.path.exists(os.path.join(staging, name)):
                errors.append(".mud 引用但包内缺失: %s" % name)
    if errors:
        print("✗ 包内容校验失败:")
        for e in errors:
            print("    " + e)
        return 1

    # 步骤5： 打包
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(staging)):
            z.write(os.path.join(staging, name), name)

    size = os.path.getsize(out)
    print("✓ 已打包 %s" % out)
    print("  包含 %d 个文件，%.1f MB"
          % (len(os.listdir(staging)), size / 1024.0 / 1024.0))
    for name in sorted(os.listdir(staging)):
        print("    %-28s %8d B" % (name, os.path.getsize(os.path.join(staging, name))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
