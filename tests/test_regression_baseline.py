# -*- coding: utf-8 -*-
"""确认新增回归测试在旧实现上会失败（用旧算法原地复算，不改动源码）"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import protocol as pmod

FAIL = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)


def old_cluster(script, match_dist=20, min_hits=3):
    """旧聚类：hits 存框，不做逐帧一对一"""
    clusters = []
    for dets in script:
        for det in dets:
            shape_name = det[0]
            bbox = det[4]
            cx = bbox[0] + bbox[2] / 2
            cy = bbox[1] + bbox[3] / 2
            best = None
            best_d = match_dist
            for cluster in clusters:
                if cluster["shape"] != shape_name:
                    continue
                d = ((cluster["cx"] - cx) ** 2 + (cluster["cy"] - cy) ** 2) ** 0.5
                if d < best_d:
                    best_d = d
                    best = cluster
            if best is None:
                clusters.append({"shape": shape_name, "cx": cx, "cy": cy, "hits": [det]})
            else:
                best["hits"].append(det)
                n = len(best["hits"])
                best["cx"] = sum(h[4][0] + h[4][2] / 2 for h in best["hits"]) / n
                best["cy"] = sum(h[4][1] + h[4][3] / 2 for h in best["hits"]) / n
    return [c for c in clusters if len(c["hits"]) >= min_hits]


def det(bbox):
    cx = bbox[0] + bbox[2] / 2
    cy = bbox[1] + bbox[3] / 2
    return ("cone", "red", cx / 320.0, cy / 256.0, bbox, 0.8)


print("=== 旧聚类对测试用例 1（两件相距10px，5帧都在）===")
pair_frames = [[det((100, 100, 20, 20)), det((110, 100, 20, 20))]] * 5
old = old_cluster(pair_frames)
check("旧实现把两件并成一件（应触发回归）", len(old) == 1, "旧得到%d件" % len(old))

print("\n=== 旧聚类对测试用例 2（仅首帧三个相邻框）===")
adj = [det((100, 100, 20, 20)), det((112, 100, 20, 20)), det((124, 100, 20, 20))]
old = old_cluster([adj] + [[] for _ in range(4)])
check("旧实现凑够3票而通过（应触发回归）", len(old) == 1, "旧得到%d件" % len(old))


def old_process(buf, match_dist=None):
    """旧解析：找不到帧头整块清空；CRC 失败按声称长度整帧丢弃"""
    frames = []
    while len(buf) >= 6:
        idx = buf.find(pmod.Protocol.FRAME_HEAD)
        if idx == -1:
            buf.clear()
            break
        if idx > 0:
            del buf[:idx]
        if len(buf) < 6:
            break
        length = buf[2]
        if length > 128:
            del buf[:2]
            continue
        frame_len = 6 + length
        if len(buf) < frame_len:
            break
        frame_data = bytes(buf[:frame_len])
        del buf[:frame_len]
        if pmod.Protocol().parse_frame(frame_data):
            frames.append(frame_data)
    return frames


p = pmod.Protocol()
LEGAL = bytes(p.build_frame(0x43, b"\x01\x00", seq=0x2E)[0])

print("\n=== 旧解析对用例 1（AA 跨片，前导噪声 6 字节）===")
buf = bytearray(b"\x00\x11\x22\x33\x44\xAA")
old_process(buf)
check("旧实现把末尾 AA 清掉（应触发回归）", len(buf) == 0, len(buf))
buf.extend(b"\x55" + LEGAL[2:])
got = old_process(buf)
check("旧实现再也接不上该帧（应触发回归）", len(got) == 0, len(got))

print("\n=== 旧解析对用例 3（伪帧头长度过大）===")
garbage = b"\xAA\x55\x10" + b"\x00" * 5
buf = bytearray(garbage + LEGAL + b"\x00" * 10)
got = old_process(buf)
check("旧实现吞掉后续合法帧（应触发回归）", len(got) == 0, len(got))

print("\n=== 旧解析对用例 4（伪帧头声明 128）===")
buf = bytearray(b"\xAA\x55\x80" + b"\x00" * 3 + LEGAL)
got = old_process(buf)
check("旧实现被 128 字节伪帧堵住（应触发回归）", len(got) == 0, len(got))


def old_process_with_preempt(buf):
    """上一版实现：帧未到齐时，若后面有"长度凑得齐"的帧头就抢先重同步"""
    frames = []
    while True:
        idx = buf.find(pmod.Protocol.FRAME_HEAD)
        if idx == -1:
            buf.clear()
            break
        if idx > 0:
            del buf[:idx]
        if len(buf) < 6:
            break
        length = buf[2]
        if length > 128:
            del buf[:2]
            continue
        frame_len = 6 + length
        if len(buf) < frame_len:
            # 只查长度，不查 CRC
            pos = buf.find(pmod.Protocol.FRAME_HEAD, 2)
            ok = False
            while pos != -1:
                if len(buf) - pos >= 6:
                    nlen = buf[pos + 2]
                    if nlen <= 128 and len(buf) - pos >= 6 + nlen:
                        ok = True
                        break
                pos = buf.find(pmod.Protocol.FRAME_HEAD, pos + 1)
            if not ok:
                break
            del buf[:2]
            continue
        frame_data = bytes(buf[:frame_len])
        del buf[:frame_len]
        r = pmod.Protocol().parse_frame(frame_data)
        if r:
            frames.append(r)
    return frames


print("\n=== 旧抢先重同步会破坏「负载内嵌完整帧」的场景 ===")
inner = bytes(pmod.Protocol().build_frame(0x43, b"\x01\x00", seq=0x77)[0])
outer = bytes(pmod.Protocol().build_frame(0x4F, inner, seq=0x11)[0])
buf = bytearray(outer[:len(outer) - 1])
got = old_process_with_preempt(buf)
check("旧实现把内嵌的 SCAN_REQ 当成独立命令执行（应触发回归）",
      len(got) == 1 and got[0][0] == 0x43,
      [hex(f[0]) for f in got])

print("\n=== 旧共享字段对事务 SEQ 的破坏 ===")


class OldHandler:
    """旧行为：响应时再去读共享的 scan_req_seq"""

    def __init__(self):
        self.scan_req_seq = 0
        self.queue = []
        self.sent = []

    def on_req(self, seq):
        self.scan_req_seq = seq
        self.queue.append(seq)

    def take(self):
        if not self.queue:
            return None
        self.scan_req_seq = self.queue.pop(0)
        return self.scan_req_seq

    def reply(self):
        self.sent.append(self.scan_req_seq)


old = OldHandler()
old.on_req(0x21)
old.take()              # 取出 0x21
old.on_req(0x22)        # 执行期间又来一条，改写共享字段
old.reply()             # 旧的应答逻辑此时读到的是 0x22
check("旧实现用 0x22 应答本应回 0x21 的事务（应触发回归）",
      old.sent == [0x22], [hex(s) for s in old.sent])
check("旧实现的队列里仍留着 0x22，0x21 无响应",
      old.queue == [0x22], old.queue)

print("\n" + "=" * 50)
print("旧实现上未能复现的项:", FAIL if FAIL else "无（全部复现）")
sys.exit(0 if not FAIL else 1)
