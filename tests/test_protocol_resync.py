# -*- coding: utf-8 -*-
"""串口接收解析器：噪声之后能重新对齐，且不误拆合法分片帧

两组合约：

A. 噪声恢复
   1) 末尾单独一个 0xAA 要保留，下一片的 0x55 才能接上
   2) CRC 不过时只丢 2 字节帧头，不按声称长度整帧丢弃
   3) 长度字段非法(>128)的伪帧头立即丢弃
   4) 半截伪帧头在接收停滞超时后判定并丢弃

B. 不误拆合法帧（新增回归点）
   5) 合法帧的负载里出现 AA 55 序列时，分片到达不得被抢先重同步
   6) 负载里内嵌一条 CRC 正确的完整帧时，不得被当成独立命令执行
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import ensure_project_path, check, report

ensure_project_path()

import protocol as pmod

FAIL = []


def build(cmd, payload=b"", seq=0x10):
    p = pmod.Protocol()
    frame, _ = p.build_frame(cmd, payload, seq=seq)
    return bytes(frame)


def feed(chunks, timeout_ms=None):
    p = pmod.Protocol()
    if timeout_ms is not None:
        p.rx_resync_timeout_ms = timeout_ms
    out = []
    for ch in chunks:
        p.rx_buffer.extend(ch)
        p.note_rx()
        out.extend(p.process_rx_buffer())
    return out, p


LEGAL = build(0x43, bytes([0x01, 0x00]), seq=0x2E)      # SCAN_REQ 9 字节
LEGAL2 = build(0x4F, b"\xDE\xAD\xBE\xEF", seq=0x11)     # PING 10 字节

print("=== 0. 基线 ===")
frames, _ = feed([LEGAL])
check(FAIL, "单个合法帧解出", len(frames) == 1 and frames[0][0] == 0x43, frames)
frames, _ = feed([LEGAL + LEGAL2])
check(FAIL, "两个连续合法帧都解出", len(frames) == 2, len(frames))

print("\n=== A1. AA 在上一片末尾，55 在下一片开头 ===")
frames, p = feed([b"\x00\x11\x22\x33\x44\xAA", b"\x55" + LEGAL[2:]])
check(FAIL, "跨片帧头能接上", len(frames) == 1 and frames[0][0] == 0x43, frames)
check(FAIL, "残余缓冲清空", len(p.rx_buffer) == 0, len(p.rx_buffer))

frames, p = feed([b"\x00\xAA", b"\x55" + LEGAL[2:]])
check(FAIL, "短噪声后也能接上", len(frames) == 1 and frames[0][0] == 0x43, frames)

print("\n=== A2. 逐字节投喂 ===")
frames, p = feed([b"\x99\x88"] + [bytes([b]) for b in LEGAL])
check(FAIL, "逐字节也能成帧", len(frames) == 1 and frames[0][0] == 0x43, frames)

print("\n=== A3. CRC 失败的伪帧不吞后续帧 ===")
broken = bytearray(LEGAL)
broken[-1] ^= 0xFF
frames, p = feed([bytes(broken) + LEGAL2])
check(FAIL, "坏帧之后的合法帧仍解出",
      len(frames) == 1 and frames[0][0] == 0x4F,
      [hex(f[0]) for f in frames])
check(FAIL, "CRC 错误被计数", p.crc_errors == 1, p.crc_errors)

print("\n=== A4. 长度非法(>128)的伪帧头立即丢弃 ===")
frames, p = feed([bytes([0xAA, 0x55, 0xFF]) + LEGAL])
check(FAIL, "超长长度字段后仍解出合法帧",
      len(frames) == 1 and frames[0][0] == 0x43, [hex(f[0]) for f in frames])
check(FAIL, "非法长度被计数", p.bad_len >= 1, p.bad_len)

print("\n=== A5. 半截伪帧头：停滞超时后才丢弃 ===")
p = pmod.Protocol()
p.rx_resync_timeout_ms = 60
fake = b"\xAA\x55\x10" + b"\x00" * 5            # 声明 22 字节，只给 8 字节
p.rx_buffer.extend(fake + LEGAL)
p.note_rx()
frames = p.process_rx_buffer()
check(FAIL, "未超时时先等待，不丢数据", len(frames) == 0, len(frames))
check(FAIL, "超时前缓冲保留", len(p.rx_buffer) == len(fake) + len(LEGAL),
      len(p.rx_buffer))

time.sleep(0.09)
check(FAIL, "停滞超时后判定伪帧头", p.resync_stale() is True)
check(FAIL, "停滞丢弃被计数", p.resync_stale_count == 1, p.resync_stale_count)
frames = p.process_rx_buffer()
check(FAIL, "丢弃伪帧头后解出合法帧",
      len(frames) == 1 and frames[0][0] == 0x43, [hex(f[0]) for f in frames])

print("\n=== B1. 负载含 AA 55 序列，分片到达不被拆散 ===")
inner = build(0x43, bytes([0x01, 0x00]), seq=0x77)       # 9 字节，以 AA 55 开头
outer = build(0x4F, inner, seq=0x11)                    # PING 负载=内嵌整帧
check(FAIL, "构造的外层帧确实内嵌帧头",
      inner[:2] == pmod.Protocol.FRAME_HEAD and inner in outer[5:], outer[:8])

# 第一片：外层头 + 完整的内嵌帧，外层自身还差最后 1 字节 CRC
chunk1 = outer[:len(outer) - 1]
chunk2 = outer[len(outer) - 1:]
frames, p = feed([chunk1])
check(FAIL, "第一片不产生任何帧", len(frames) == 0,
      [hex(f[0]) for f in frames])
check(FAIL, "外层帧被完整保留等待",
      len(p.rx_buffer) == len(chunk1), len(p.rx_buffer))

frames, p = feed([chunk1, chunk2])
check(FAIL, "补齐后解出外层 PING，而不是内嵌的 SCAN_REQ",
      len(frames) == 1 and frames[0][0] == 0x4F,
      [hex(f[0]) for f in frames])
check(FAIL, "外层负载完整", frames and frames[0][2] == inner,
      frames[0][2] if frames else None)
check(FAIL, "内嵌帧没有被当成独立命令",
      sum(1 for f in frames if f[0] == 0x43) == 0,
      [hex(f[0]) for f in frames])

print("\n=== B2. 内嵌帧逐字节到达也不被拆 ===")
chunks = [bytes([b]) for b in outer]
frames, p = feed(chunks)
check(FAIL, "逐字节仍解出外层 PING",
      len(frames) == 1 and frames[0][0] == 0x4F,
      [hex(f[0]) for f in frames])

print("\n=== B3. 负载含 AA 55 但不构成完整帧 ===")
outer2 = build(0x4F, bytes([0xAA, 0x55, 0x02, 0x99, 0x88]), seq=0x12)
half = len(outer2) - 2
frames, p = feed([outer2[:half]])
check(FAIL, "半截时等待", len(frames) == 0, len(frames))
frames, p = feed([outer2[:half], outer2[half:]])
check(FAIL, "补齐后正确解出",
      len(frames) == 1 and frames[0][0] == 0x4F, [hex(f[0]) for f in frames])

print("\n=== 6. 纯噪声不产生帧且缓冲有界 ===")
noise = bytes((i * 7 + 3) % 256 for i in range(500))
frames, p = feed([noise])
check(FAIL, "噪声不产生帧", len(frames) == 0, len(frames))
check(FAIL, "噪声后缓冲有界", len(p.rx_buffer) <= 1, len(p.rx_buffer))

print("\n=== 7. 大量帧夹杂噪声全部解出 ===")
chunks = []
for i in range(50):
    chunks.append(b"\x7E" + build(0x41, bytes([0x01, 0x00, 0x00]), seq=i & 0xFF))
frames, p = feed(chunks)
check(FAIL, "50 帧全部解出", len(frames) == 50, len(frames))

print("\n=== 8. 结尾半截帧头保留到下一片 ===")
frames, p = feed([LEGAL + b"\xAA"])
check(FAIL, "半截帧头被保留", frames and len(p.rx_buffer) == 1,
      len(p.rx_buffer))
frames, p = feed([LEGAL + b"\xAA", b"\x55" + LEGAL[2:]])
check(FAIL, "下一片补全后半截帧头", len(frames) == 2, len(frames))

print("\n=== 9. 反复出现的伪帧头可终止、缓冲收敛 ===")
p = pmod.Protocol()
p.rx_buffer.extend(b"\xAA\x55" * 200)
frames = p.process_rx_buffer()
check(FAIL, "可终止且收敛", len(p.rx_buffer) <= 5, len(p.rx_buffer))

sys.exit(report(FAIL))
