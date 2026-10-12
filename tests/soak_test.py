"""
链路浸泡测试 - 定位 MaixCAM2 长时间运行后卡死/重启的原因

用途：把"串口收发"和"摄像头+显示"两条负载拆开单独跑，
      哪一路跑挂了，问题就落在哪一路。

用法（设备上执行）：
    python soak_test.py sniff 60   # 只监听 MCU 发来的数据，量速率与命令分布
    python soak_test.py uart      # 只跑串口心跳收发
    python soak_test.py vision    # 只跑摄像头+显示
    python soak_test.py both      # 两者一起，等价于主程序稳态负载

参数：
    sniff  嗅探模式，统计 MCU 的字节率、帧率、命令直方图，落盘 /root/mcu_sniff.log
    uart   串口模式，默认端口 /dev/ttyS4 @115200
    vision 视觉模式，320x256 采集 + 640x480 显示，30ms 节流
    both   串口 + 视觉

默认运行 600 秒后自行退出，也可 Ctrl+C 结束。

观察要点：
    1) max_write_ms 是否出现远大于 1ms 的尖峰 -> 串口写阻塞
    2) rss_kb 是否单调上涨 -> 内存泄漏
    3) uptime_s 是否突然归零 -> 系统整体重启（而不是程序退出）
    4) 挂掉时的 loop 计数 -> 复现耗时
"""
import os
import sys
import time as pytime

# 位于 tests/ 子目录，把工程根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from maix import app, time

# ==================== 测试参数 ====================
UART_PORT = "/dev/ttyS4"
UART_BAUDRATE = 115200
HEARTBEAT_INTERVAL = 0.25     # 与 config.HEARTBEAT_INTERVAL 一致
REPORT_INTERVAL = 5.0         # 诊断打印间隔(秒)
DEFAULT_DURATION = 600        # 默认运行时长(秒)
CAMERA_WIDTH = 320
CAMERA_HEIGHT = 256
LOOP_SLEEP_MS = 30


def _crc8(data):
    """CRC-8/SMBUS，与 protocol.py 一致"""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def _build_heartbeat(seq, state=1):
    """构造心跳帧"""
    body = bytes([0xAA, 0x55, 0x03, 0x01, seq & 0xFF, state, 0x00, 0x00])
    return body + bytes([_crc8(body)])


def _read_uptime():
    """系统开机时长(秒)，归零说明整机重启过"""
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return -1.0


def _read_mem():
    """返回 (进程RSS kB, 系统可用内存 kB)"""
    rss = -1
    avail = -1
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                    break
    except Exception:
        pass
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
                    break
    except Exception:
        pass
    return rss, avail


class SoakStats:
    """计数与耗时统计"""

    def __init__(self):
        self.loop = 0
        self.tx_frames = 0
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.rx_chunks = 0
        self.write_total_ms = 0.0
        self.write_max_ms = 0.0
        self.write_slow = 0        # 超过 50ms 的写次数
        self.write_error = 0

    def note_write(self, cost_ms):
        self.tx_frames += 1
        self.write_total_ms += cost_ms
        if cost_ms > self.write_max_ms:
            self.write_max_ms = cost_ms
        if cost_ms > 50.0:
            self.write_slow += 1


def run_uart(duration):
    """步骤1： 串口模式
    目标：只跑心跳收发，观察串口写是否阻塞、写是否报错
    操作：
    1) 打开 /dev/ttyS4
    2) 每 250ms 发一帧心跳并计时
    3) 每轮把接收缓冲读空并计数
    """
    from maix import uart

    print("[SOAK] 串口模式启动: %s @ %d" % (UART_PORT, UART_BAUDRATE))
    logger_start = pytime.time()

    # 1.1 打开串口
    try:
        serial = uart.UART(UART_PORT, UART_BAUDRATE)
        print("[SOAK] 串口已打开")
    except Exception as e:
        print("[SOAK] 串口打开失败: %s: %s" % (type(e).__name__, e))
        return None

    # 1.2 进入收发循环
    stats = SoakStats()
    seq = 0
    last_hb = 0.0
    last_report = pytime.time()

    while not app.need_exit():
        now = pytime.time()
        if duration > 0 and now - logger_start >= duration:
            print("[SOAK] 达到设定时长，正常退出")
            break

        # 1.3 到点发心跳，并测量写耗时
        if now - last_hb >= HEARTBEAT_INTERVAL:
            frame = _build_heartbeat(seq)
            seq = (seq + 1) & 0xFF
            t0 = pytime.time()
            try:
                serial.write(frame)
                stats.note_write((pytime.time() - t0) * 1000.0)
                stats.tx_bytes += len(frame)
            except Exception as e:
                stats.write_error += 1
                print("[SOAK] 写失败: %s: %s" % (type(e).__name__, e))
            last_hb = now

            # 1.4 周期性打印诊断
            if now - last_report >= REPORT_INTERVAL:
                _report(stats, logger_start, "uart")
                last_report = now

        # 1.5 读空接收缓冲
        try:
            n = serial.available()
            if n > 0:
                data = serial.read(n)
                stats.rx_bytes += len(data)
                stats.rx_chunks += 1
        except Exception as e:
            print("[SOAK] 读失败: %s: %s" % (type(e).__name__, e))

        stats.loop += 1
        time.sleep_ms(1)

    return stats


def run_vision(duration):
    """步骤2： 视觉模式
    目标：只跑摄像头采集与屏幕刷新，观察是否内存增长或卡死
    操作：
    1) 打开 320x256 摄像头与显示器
    2) 每轮采集一帧、画ROI框与文字、刷屏
    3) 30ms 节流，与主程序一致
    """
    from maix import camera, display, image

    print("[SOAK] 视觉模式启动: 采集 %dx%d" % (CAMERA_WIDTH, CAMERA_HEIGHT))

    # 2.1 初始化设备
    cam = camera.Camera(CAMERA_WIDTH, CAMERA_HEIGHT, image.Format.FMT_RGB888)
    disp = display.Display()
    print("[SOAK] 摄像头与显示器已初始化: 屏幕 %dx%d"
          % (disp.width(), disp.height()))

    # 2.2 进入采集显示循环
    stats = SoakStats()
    start = pytime.time()
    last_report = start
    roi = (80, 50, 160, 155)

    while not app.need_exit():
        now = pytime.time()
        if duration > 0 and now - start >= duration:
            print("[SOAK] 达到设定时长，正常退出")
            break

        img = cam.read()
        img.draw_rect(roi[0], roi[1], roi[2], roi[3],
                      color=image.COLOR_BLUE, thickness=2)
        img.draw_string(5, 5, "SOAK %d" % stats.loop,
                        color=image.COLOR_GREEN, scale=1.0)
        disp.show(img)

        stats.loop += 1
        stats.tx_frames += 1
        if now - last_report >= REPORT_INTERVAL:
            _report(stats, start, "vision")
            last_report = now
        time.sleep_ms(LOOP_SLEEP_MS)

    return stats


def run_sniff(duration):
    """步骤4： 嗅探模式
    目标：只监听 MCU 发来的数据，量出速率与命令分布，不跑视觉
    操作：
    1) 打开串口，逐轮读空缓冲
    2) 用 protocol.py 的解析器切帧，统计每秒字节数/帧数与命令直方图
    3) 每秒落一行到 /root/mcu_sniff.log，重启后仍可查阅
    """
    import protocol as proto_module

    print("[SOAK] 嗅探模式启动: %s @ %d" % (UART_PORT, UART_BAUDRATE))
    start = pytime.time()
    proto = proto_module.Protocol()

    from maix import uart
    serial = uart.UART(UART_PORT, UART_BAUDRATE)

    total_bytes = 0
    total_frames = 0
    max_chunk = 0
    peak_bps = 0
    peak_fps = 0
    cmd_hist = {}
    bad_crc = 0

    sec_bytes = 0
    sec_frames = 0
    last_sec = start

    log_path = "/root/mcu_sniff.log"
    try:
        log = open(log_path, "w")
    except Exception as e:
        print("[SOAK] 无法写日志文件 %s: %s" % (log_path, e))
        log = None

    def emit(line):
        print(line)
        if log is not None:
            log.write(line + "\n")
            log.flush()

    emit("[SNIFF] 开始嗅探，%ss，日志 %s" % (duration, log_path))

    while not app.need_exit():
        now = pytime.time()
        if duration > 0 and now - start >= duration:
            emit("[SNIFF] 达到设定时长，结束")
            break

        # 4.1 读空接收缓冲
        try:
            n = serial.available()
            if n > 0:
                data = serial.read(n)
                if len(data) > max_chunk:
                    max_chunk = len(data)
                sec_bytes += len(data)
                total_bytes += len(data)
                proto.rx_buffer.extend(data)
        except Exception as e:
            emit("[SNIFF] 读失败: %s: %s" % (type(e).__name__, e))

        # 4.2 切帧并统计
        try:
            frames = proto.process_rx_buffer()
        except Exception as e:
            emit("[SNIFF] 解析异常: %s: %s" % (type(e).__name__, e))
            proto.rx_buffer.clear()
            frames = []

        for cmd, seq, payload in frames:
            total_frames += 1
            sec_frames += 1
            cmd_hist[cmd] = cmd_hist.get(cmd, 0) + 1

        # 4.3 每秒落一行
        if now - last_sec >= 1.0:
            if sec_bytes > peak_bps:
                peak_bps = sec_bytes
            if sec_frames > peak_fps:
                peak_fps = sec_frames
            emit("[SNIFF] t=%.0fs 本秒 %d 字节 / %d 帧  缓冲余 %d  累计 %d 字节 / %d 帧"
                 % (now - start, sec_bytes, sec_frames,
                    len(proto.rx_buffer), total_bytes, total_frames))
            sec_bytes = 0
            sec_frames = 0
            last_sec = now

        time.sleep_ms(5)

    # 4.4 输出汇总
    elapsed = pytime.time() - start
    emit("=" * 60)
    emit("[SNIFF] 汇总: 时长%.0fs 总字节%d 总帧%d 平均%.0fB/s %.1f帧/s"
         % (elapsed, total_bytes, total_frames,
            total_bytes / elapsed if elapsed > 0 else 0,
            total_frames / elapsed if elapsed > 0 else 0))
    emit("[SNIFF] 峰值: %dB/s %d帧/s 单次最大读取%d字节 缓冲残留%d字节"
         % (peak_bps, peak_fps, max_chunk, len(proto.rx_buffer)))
    emit("[SNIFF] 链路容量参考: 115200 8N1 = 11520 B/s，占用 %.1f%%"
         % (peak_bps * 100.0 / 11520.0))
    emit("[SNIFF] 命令直方图:")
    for cmd in sorted(cmd_hist, key=lambda c: -cmd_hist[c]):
        emit("    CMD 0x%02X : %d 帧" % (cmd, cmd_hist[cmd]))
    if not cmd_hist:
        emit("    (没有解析出任何完整帧)")
    emit("=" * 60)

    if log is not None:
        log.close()
    return None


def _report(stats, start, tag):
    """打印一行诊断"""
    elapsed = pytime.time() - start
    rss, avail = _read_mem()
    fps = stats.loop / elapsed if elapsed > 0 else 0.0
    print("[SOAK][%s] 运行%.0fs 循环%d (%.1f/s) 写%d次 "
          "最大写耗时%.2fms 慢写%d 写错误%d 收%d字节 RSS=%dkB 可用=%dkB 开机%.0fs"
          % (tag, elapsed, stats.loop, fps, stats.tx_frames,
             stats.write_max_ms, stats.write_slow, stats.write_error,
             stats.rx_bytes, rss, avail, _read_uptime()))


def run_both(duration):
    """步骤3： 串口+视觉同时跑
    目标：复现主程序稳态负载，看是否在其中一路先出问题
    """
    from maix import camera, display, image, uart

    print("[SOAK] 双负载模式启动")

    # 3.1 初始化串口
    serial = uart.UART(UART_PORT, UART_BAUDRATE)

    # 3.2 初始化摄像头与显示
    cam = camera.Camera(CAMERA_WIDTH, CAMERA_HEIGHT, image.Format.FMT_RGB888)
    disp = display.Display()

    stats = SoakStats()
    start = pytime.time()
    last_report = start
    last_hb = 0.0
    seq = 0
    roi = (80, 50, 160, 155)

    while not app.need_exit():
        now = pytime.time()
        if duration > 0 and now - start >= duration:
            print("[SOAK] 达到设定时长，正常退出")
            break

        # 3.3 串口收发
        if now - last_hb >= HEARTBEAT_INTERVAL:
            frame = _build_heartbeat(seq)
            seq = (seq + 1) & 0xFF
            t0 = pytime.time()
            try:
                serial.write(frame)
                stats.note_write((pytime.time() - t0) * 1000.0)
                stats.tx_bytes += len(frame)
            except Exception as e:
                stats.write_error += 1
                print("[SOAK] 写失败: %s: %s" % (type(e).__name__, e))
            last_hb = now

        try:
            n = serial.available()
            if n > 0:
                data = serial.read(n)
                stats.rx_bytes += len(data)
                stats.rx_chunks += 1
        except Exception as e:
            print("[SOAK] 读失败: %s: %s" % (type(e).__name__, e))

        # 3.4 视觉
        img = cam.read()
        img.draw_rect(roi[0], roi[1], roi[2], roi[3],
                      color=image.COLOR_BLUE, thickness=2)
        img.draw_string(5, 5, "SOAK %d" % stats.loop,
                        color=image.COLOR_GREEN, scale=1.0)
        disp.show(img)

        stats.loop += 1
        if now - last_report >= REPORT_INTERVAL:
            _report(stats, start, "both")
            last_report = now
        time.sleep_ms(LOOP_SLEEP_MS)

    return stats


def main():
    """入口：按命令行参数选择测试模式"""
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    duration = DEFAULT_DURATION
    if len(sys.argv) > 2:
        duration = float(sys.argv[2])

    print("=" * 60)
    print("链路浸泡测试: 模式=%s 时长=%ss" % (mode, duration))
    print("开机时长 %.0fs" % _read_uptime())
    print("=" * 60)

    stats = None
    started = pytime.time()
    try:
        if mode == "uart":
            stats = run_uart(duration)
        elif mode == "vision":
            stats = run_vision(duration)
        elif mode == "sniff":
            stats = run_sniff(duration)
        else:
            stats = run_both(duration)
    except KeyboardInterrupt:
        print("\n[SOAK] 用户中断")
    except Exception as e:
        import traceback
        print("\n[SOAK] 异常退出: %s: %s" % (type(e).__name__, e))
        traceback.print_exc()
    finally:
        if stats is not None:
            _report(stats, started, "final")
        print("=" * 60)
        print("[SOAK] 结束，开机时长 %.0fs" % _read_uptime())
        print("=" * 60)


if __name__ == "__main__":
    main()
