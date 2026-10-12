# -*- coding: utf-8 -*-
"""对 protocol.Protocol 的接收解析做模糊测试

问题：收到非法/畸形串口数据，会不会导致解析器异常、内存无界增长或耗时爆炸？
"""
import os
import random
import sys
import time

# 允许从 tests/ 子目录直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import protocol as proto_module

FAIL = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra else ""))
    if not cond:
        FAIL.append(name)


def run_case(name, chunks, max_buf=4096):
    """反复投喂数据，观察缓冲区上限、异常与耗时"""
    p = proto_module.Protocol()
    total_frames = 0
    max_seen = 0
    t0 = time.time()
    try:
        for ch in chunks:
            p.rx_buffer.extend(ch)
            if len(p.rx_buffer) > max_seen:
                max_seen = len(p.rx_buffer)
            total_frames += len(p.process_rx_buffer())
        err = None
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    dt = time.time() - t0
    print("  %-34s 帧=%-6d 缓冲峰值=%-6d 残留=%-5d 耗时=%.3fs %s"
          % (name, total_frames, max_seen, len(p.rx_buffer), dt,
             ("异常! " + err) if err else ""))
    check(name + " 无异常", err is None, err or "")
    check(name + " 缓冲有界", max_seen <= max_buf, max_seen)
    return p, total_frames


random.seed(20261003)

print("=== 1. 纯随机字节 ===")
run_case("随机字节 200KB",
         [bytes(random.randrange(256) for _ in range(256)) for _ in range(800)])

print("\n=== 2. 只有帧头 AA 55 反复 ===")
run_case("AA55 重复 20000 次",
         [b"\xAA\x55" * 128 for _ in range(157)])

print("\n=== 3. 帧头 + 非法长度 ===")
run_case("LEN=0xFF (超限)",
         [b"\xAA\x55\xFF" + bytes(random.randrange(256) for _ in range(61)) for _ in range(500)])
run_case("LEN=0x00",
         [b"\xAA\x55\x00" + bytes(random.randrange(256) for _ in range(61)) for _ in range(500)])
run_case("LEN=0x80 (128, 永不补齐)",
         [b"\xAA\x55\x80" + bytes(random.randrange(256) for _ in range(10)) for _ in range(500)])
run_case("LEN=0x7F 后接空",
         [b"\xAA\x55\x7F" for _ in range(2000)])

print("\n=== 4. 逐字节投喂合法帧（模拟分包到达）===")
p = proto_module.Protocol()
frame, _ = p.build_frame(0x43, bytes([1, 0]), seq=7)
p2 = proto_module.Protocol()
got = 0
for b in frame:
    p2.rx_buffer.extend(bytes([b]))
    got += len(p2.process_rx_buffer())
check("逐字节可正确成帧", got == 1, got)

print("\n=== 5. 帧头嵌在负载里（伪同步）===")
run_case("负载含 AA55",
         [b"\xAA\x55\x06\x43\x01" + b"\xAA\x55\xAA\x55\xAA\x55" for _ in range(2000)])

print("\n=== 6. 超大突发（模拟对端灌数据）===")
big = bytes(random.randrange(256) for _ in range(200000))
t0 = time.time()
p3 = proto_module.Protocol()
p3.rx_buffer.extend(big)
frames = p3.process_rx_buffer()
dt = time.time() - t0
print("  200KB 一次性投入: 帧=%d 残留=%d 耗时=%.3fs" % (len(frames), len(p3.rx_buffer), dt))
check("200KB 后缓冲清空", len(p3.rx_buffer) == 0, len(p3.rx_buffer))
check("200KB 处理耗时 < 2s", dt < 2.0, dt)

print("\n=== 7. 合法帧 + 随机噪声交替（最接近现场）===")
p4 = proto_module.Protocol()
seq = 0
chunks = []
for i in range(2000):
    f, _ = p4.build_frame(0x41, bytes([1, 0, 0]), seq=seq & 0xFF)
    seq += 1
    chunks.append(f + bytes(random.randrange(256) for _ in range(random.randrange(0, 20))))
p5 = proto_module.Protocol()
ok = 0
for ch in chunks:
    p5.rx_buffer.extend(ch)
    for cmd, s, pay in p5.process_rx_buffer():
        if cmd == 0x41:
            ok += 1
check("噪声中合法帧全部解出", ok == 2000, ok)
check("噪声场景缓冲有界", len(p5.rx_buffer) < 1024, len(p5.rx_buffer))

print("\n" + "=" * 50)
print("失败项:", FAIL if FAIL else "无")
sys.exit(1 if FAIL else 0)
