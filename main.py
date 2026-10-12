"""
智能分拣视觉系统 - 主程序入口
MaixCAM2 视觉从站
对接协议: v3.1 周期快照（线上版本字节仍为 0x02）
"""
import contextlib
import importlib
import os
import signal
import time as pytime
import config
from diagnostics import Journal, install_fault_dump

# 在 Maix 导入（会启动内置监听）及任何媒体初始化之前保留启动证据。
STARTUP = None
FAULT_FILE = None
if __name__ == "__main__":
    STARTUP = Journal(config.STARTUP_LOG_PATH, limit=config.DIAG_LOG_MAX_BYTES,
                      backups=config.DIAG_LOG_BACKUPS)
    os.environ["VISION_RUN_ID"] = STARTUP.run_id
    STARTUP.event("BOOT_APPLICATION", critical=True)
    if config.FAULT_DUMP_ENABLED:
        FAULT_FILE = install_fault_dump(config.FAULT_DUMP_PATH, STARTUP,
            config.DIAG_LOG_MAX_BYTES, config.DIAG_LOG_BACKUPS)


def startup_phase(name):
    return STARTUP.phase(name) if STARTUP is not None else contextlib.nullcontext()


with startup_phase("maix_import"):
    from maix import app, camera, display, image, nn, gpio, pinmap, time, sys, err, uart
    from maix import comm as maix_comm

# ========================================================================
# 启动闸门：先清掉占着媒体框架和串口的其它 MaixCAM 应用
# ========================================================================
# AXERA 的 VIN/VO/IVPS/POOL/NPU 与 /dev/ttyS4 都是全局独占资源。
# launcher 或其它应用还在跑时初始化，会出现两类故障：
#   1) 媒体管线被抢坏，ISP 每秒刷 os04d10 寄存器越界，最终整机复位
#   2) 紧接着创建 uart.UART 抛 ValueError: basic_string::_M_create
# 因此必须在摘除内置监听、创建串口之前，先把它们结束并等干净。
COMM_RELEASE_SETTLE_MS = 300    # 监听线程退场等待(毫秒)
UART_OPEN_RETRY = 3             # 串口打开重试次数
UART_OPEN_RETRY_DELAY_MS = 300  # 重试间隔(毫秒)


def _conflicting_processes():
    """列出占用全局媒体框架的进程

    /maixapp/apps/ 下任何应用（含 launcher 自身）在跑，
    就占着媒体框架与串口。
    """
    self_pid = os.getpid()
    found = []
    try:
        entries = os.listdir("/proc")
    except Exception:
        return found

    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == self_pid:
            continue
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmd = f.read().decode("utf-8", "replace").replace("\x00", " ").strip()
        except Exception:
            continue
        if "/maixapp/apps/" not in cmd:
            continue
        found.append((pid, cmd))
    return found


def _is_launcher(cmd):
    """判断是否 launcher 相关进程

    launcher 从屏幕拉起本应用时会先把媒体驱动释放掉
    （日志里的 "maix multi-media driver used count: 0, driver released."），
    属于正常路径，不能算占用者。
    """
    return ("launcher daemon" in cmd) or ("launcher_daemon" in cmd)


def _warn_conflicting_apps():
    """启动提示：列出可能仍占着媒体框架的其它应用

    只提示，不阻止启动，也不结束它们。
    - 从设备屏幕点开应用：launcher 已完成交接，这里通常没有残留
    - 从 SSH / MaixVision 直接启动：launcher 或其它应用可能仍占着媒体框架，
      相机初始化会抛 "VO init failed"，下面会给出处理办法
    """
    if not getattr(config, "WAIT_OTHER_APPS", True):
        return

    others = [item for item in _conflicting_processes() if not _is_launcher(item[1])]
    if not others:
        return

    print("· 检测到其它 MaixCAM 应用在运行：")
    for pid, cmd in others:
        print(f"    PID={pid} {cmd}")
    print("  若接下来相机初始化失败，请先停掉它们再启动本程序。")


_warn_conflicting_apps()

# 关于 Maix 内置通信协议的摘除：
# import maix 时 maix/__init__.py 会自动调用 add_default_comm_listener()，
# 该监听常驻读取 /dev/ttyS4（A21/A22），与 MCU 链路争抢同一串口的字节。
# 摘除动作**不能放在这里**：若此刻进程里只有监听持有该口，关闭它就是关掉
# 最后一个 fd，会触发 tty hangup，紧接着打开会撞上驱动拆解，抛
# ValueError: basic_string::_M_create。因此改为在 __init__ 里
# 「先建 uart.UART 持有该口、再摘监听」，顺序见下。

import protocol as proto_module
import screen_ui
from coordinate import CoordinateConverter
from vision import VisionDetector
from link_service import LinkProcess


class SortingVisionApp:
    """分拣视觉应用"""

    def __init__(self):
        self.diag_start_time = self.diag_last_time = pytime.monotonic()
        self.loop_count = 0
        self.journal = Journal(config.DIAG_LOG_PATH, run_id=STARTUP.run_id if STARTUP else None,
            limit=config.DIAG_LOG_MAX_BYTES, backups=config.DIAG_LOG_BACKUPS)
        self._cleaned = False
        print("=" * 50)
        print("智能分拣视觉系统启动")
        print("=" * 50)

        # 初始化GPIO
        self._init_gpio()

        # 初始化串口
        # 顺序很关键：先建 uart.UART 持有 /dev/ttyS4，再摘除内置监听。
        # 若反序，摘除监听会关掉该口上最后一个 fd，触发 tty hangup，
        # 紧接着打开就撞上驱动拆解，抛 ValueError: basic_string::_M_create。
        if STARTUP:
            STARTUP.event("ENTER", critical=True, phase="uart_guard")
        self.serial = None
        for attempt in range(1, UART_OPEN_RETRY + 1):
            try:
                self.serial = uart.UART(config.UART_PORT, config.UART_BAUDRATE)
                break
            except Exception as e:
                print(f"⚠ 串口打开失败(第{attempt}/{UART_OPEN_RETRY}次): "
                      f"{type(e).__name__}: {e}")
                time.sleep_ms(UART_OPEN_RETRY_DELAY_MS)
        if self.serial is None:
            if STARTUP:
                STARTUP.event("FAILED", critical=True, phase="uart_guard")
            raise RuntimeError(f"串口打开失败: {config.UART_PORT}")
        if STARTUP:
            STARTUP.event("OK", critical=True, phase="uart_guard")
        print(f"✓ 串口初始化: {config.UART_PORT} @ {config.UART_BAUDRATE}")

        # 已持有串口，此刻摘除内置监听不会再触发 hangup
        try:
            if maix_comm.rm_default_comm_listener():
                print("✓ 已摘除 Maix 内置通信协议，/dev/ttyS4 归本应用独占")
            else:
                print("· Maix 内置通信协议未在运行")
        except Exception as e:
            print(f"⚠ 摘除 Maix 内置通信协议失败: {e}")
        time.sleep_ms(COMM_RELEASE_SETTLE_MS)

        # 初始化协议处理器
        self.protocol = LinkProcess()
        with startup_phase("uart_worker"):
            self.protocol.start()
        # 子进程已持有串口，关闭父进程的保活 fd 不会触发最后一个 fd hangup。
        self.serial.close()
        self.serial = None
        print("✓ 协议 v3.1 周期快照，线上版本字节 0x02")

        # 初始化YOLO检测器
        with startup_phase("model"):
            self.detector = nn.YOLO26(model=config.MODEL_PATH)
        print(f"✓ 模型加载: {config.MODEL_PATH}")

        # 初始化摄像头
        # Keep the calibrated camera/display geometry independent of the model input size.
        self.camera_width = config.ROI_CALIBRATION_WIDTH
        self.camera_height = config.ROI_CALIBRATION_HEIGHT

        # 媒体框架(VIN/VO/IVPS/POOL)是全局独占资源。
        # 正常从设备屏幕点开应用时 launcher 已完成交接；若从 SSH/MaixVision
        # 直接启动，其它应用可能仍占着它，这里会抛 VO init failed。
        try:
            with startup_phase("camera"):
                self.cam = camera.Camera(self.camera_width, self.camera_height,
                                         image.Format.FMT_RGB888)
            print(f"✓ 摄像头: {self.camera_width}x{self.camera_height}")

            # 初始化显示器
            with startup_phase("display"):
                self.disp = display.Display()
            print(f"✓ 显示器初始化")
        except Exception as e:
            print("=" * 60)
            print(f"⚠ 媒体框架初始化失败: {type(e).__name__}: {e}")
            print("  原因通常是 launcher 或其它应用仍占着相机/显示。")
            print("  请正常退出其它应用，再从设备桌面启动本程序。")
            print("=" * 60)
            raise

        # 初始化视觉检测器
        self.vision = VisionDetector(self.detector, self.camera_width, self.camera_height)
        print(f"✓ 视觉检测器初始化")

        # 初始化屏幕交互按键
        with startup_phase("font_touch"):
            self.chinese = screen_ui.load_chinese_font(config.UI_FONT_PATH)
            self.ui = screen_ui.ScreenUI(self.disp, self.camera_width, self.camera_height,
                                         chinese=self.chinese)
        print(f"✓ 屏幕按键初始化")

        # 最近一次扫描结果（供"INFO"键查看）
        self.result_lines = []
        self.result_title = "no scan result"
        self.result_page = 0
        self.result_total_pages = 1
        self.result_image = None
        self.result_detections = []
        self.result_completed_at = None
        self.result_version = 0
        self.result_kind = "initial"
        self.result_stamp = "--:--:--"
        self.scan_error = ""
        self.scan_error_count = 0
        self.result_filter_stats = {}
        self.recognizing = False
        self.display_locked = False
        self.deferred_buttons = []
        self.next_snapshot_scan = pytime.monotonic()

        # 诊断计数
        self.loop_count = 0
        # 定时一律用单调时钟。
        # time.time() 会被系统校时跳变：后跳推迟心跳，前跳造成假的链路超时。
        # 给人看的时间戳仍用 pytime.strftime(pytime.localtime())。
        self.diag_start_time = pytime.monotonic()
        self.diag_last_time = self.diag_start_time

        # 预热YOLO检测器
        with startup_phase("warmup"):
            self._warmup_detector()

        # 状态名称
        self.state_names = ["INIT", "READY", "SCAN", "PICK", "DISP", "RECHK", "DUMP", "FINISH", "FAULT"]

        print("=" * 50)
        print("等待MCU连接...")
        print("=" * 50)

        # 故障取证：冻结时用 kill -USR1 <pid> 取全部 Python 线程栈
        self._fault_file = FAULT_FILE
        if STARTUP:
            STARTUP.event("READY", critical=True, chinese=self.chinese)

    def _install_fault_dump(self):
        """注册栈转储，供冻结现场取证

        冻结时进程通常还活着，只是某个调用不返回。此时
        kill -USR1 <pid> 就能拿到所有 Python 线程栈，直接看到主循环
        停在哪一行；faulthandler.enable() 还覆盖段错误等致命信号。
        """
        if not getattr(config, "FAULT_DUMP_ENABLED", True):
            print("· 故障栈转储已在配置中关闭")
            return

        path = getattr(config, "FAULT_DUMP_PATH", "/root/fault_dump.txt")
        try:
            import faulthandler
            self._fault_file = open(path, "a")
            faulthandler.enable(file=self._fault_file, all_threads=True)

            register = getattr(faulthandler, "register", None)
            if register is not None:
                register(signal.SIGUSR1, file=self._fault_file,
                         all_threads=True, chain=False)
                print(f"✓ 故障栈转储: {path}  (kill -USR1 {os.getpid()})")
            else:
                print(f"✓ 致命信号栈转储: {path}"
                      f"（本版本无 faulthandler.register，SIGUSR1 不可用）")
        except Exception as e:
            print(f"⚠ 启用故障栈转储失败: {e}")

    def _warmup_detector(self):
        """预热YOLO检测器
        NPU首次推理需要加载图/权重缓存，耗时远高于后续推理。
        若不预热，第一次扫描会因超时或空结果导致首轮漏检。
        """
        print("预热YOLO检测器(首次推理较慢，请等待)...")
        warmup_count = 3
        for i in range(warmup_count):
            start = pytime.monotonic()
            warmup_img = self.cam.read()
            self.detector.detect(warmup_img,
                                 conf_th=config.YOLO_CONF_THRESHOLD,
                                 iou_th=config.YOLO_IOU_THRESHOLD)
            elapsed = (pytime.monotonic() - start) * 1000
            print(f"  预热推理 #{i + 1}: {elapsed:.0f}ms")
        print("✓ 检测器预热完成")

    def _init_gpio(self):
        """初始化GPIO（LED）"""
        try:
            pin_name = config.LED_PIN_NAME if sys.device_id() == "maixcam2" else "B3"
            gpio_name = config.LED_GPIO_NAME if sys.device_id() == "maixcam2" else "GPIOB3"

            err.check_raise(pinmap.set_pin_function(pin_name, gpio_name), "set pin failed")
            self.led = gpio.GPIO(gpio_name, gpio.Mode.OUT)
            self.led.value(0)
            print(f"✓ GPIO初始化: {pin_name} -> {gpio_name}")
        except Exception as e:
            print(f"⚠ GPIO初始化失败: {e}")
            self.led = None

    def _draw_status(self, img):
        """心跳接收、新鲜度、完整写出三项独立；不宣称对端已收到。"""
        version = getattr(self, "result_version", 0)
        age = self._result_age()
        stale = age is not None and age > config.RESULT_STALE_SECONDS
        img.draw_rect(0, 0, self.camera_width, 46, color=image.COLOR_BLACK, thickness=-1)
        title = self._tr("最近识别结果", "Latest recognition")
        title += " @" + getattr(self, "result_stamp", "--:--:--")
        screen_ui.fit_text(img, 4, 2, title, 245, image.COLOR_WHITE, 0.9)
        if getattr(self, "recognizing", False):
            screen_ui.fit_text(img, 254, 2, self._tr("识别中", "Scanning"), 62, image.COLOR_YELLOW, 0.8)
        hb = self._tr("MCU心跳:有", "MCU HB: RX") if self.protocol.link_alive else self._tr("MCU心跳:无", "MCU HB: absent")
        screen_ui.fit_text(img, 4, 24, hb, 147,
            image.COLOR_GREEN if self.protocol.link_alive else image.COLOR_RED, 0.8)
        freshness = (self._tr("等待首次识别", "First result pending") if age is None else
            self._tr("结果滞留", "STALE") + " %.1fs" % age if stale else
            self._tr("结果年龄", "Age") + " %.1fs" % age)
        screen_ui.fit_text(img, 155, 24, freshness, 161,
            image.COLOR_RED if stale else image.COLOR_GREEN, 0.8)
        img.draw_rect(0, 178, self.camera_width, 36, color=image.COLOR_BLACK, thickness=-1)
        send, send_error = self._send_status()
        screen_ui.fit_text(img, 4, 179, "v%d %s" % (version, send), 312,
            image.COLOR_RED if send_error else image.COLOR_GREEN, 0.85)
        text = self._result_message()
        log_error = bool(getattr(self.protocol, "log_current_error", "") or (
            getattr(self, "journal", None) and self.journal.current_error))
        if log_error:
            text = self._tr("日志写入失败，证据不完整", "Log write failed; evidence incomplete")
        screen_ui.fit_text(img, 4, 197, text, 312,
            image.COLOR_RED if self._result_problem() or log_error else image.COLOR_GREEN, 0.85)

    def _tr(self, zh, en):
        return zh if getattr(self, "chinese", False) else en

    def _result_age(self):
        at = getattr(self, "result_completed_at", None)
        return None if at is None else max(0.0, pytime.monotonic() - at)

    def _send_status(self):
        if getattr(self.protocol, "ipc_error", ""):
            return self._tr("发送异常: IPC积压", "Send error: IPC backlog"), True
        if getattr(self.protocol, "snapshot_write_error", ""):
            return self._tr("发送异常: UART截断/失败", "Send error: UART failed/partial"), True
        version = getattr(self, "result_version", 0)
        if version == 0:
            return self._tr("尚无完整结果", "No completed result"), False
        if getattr(self.protocol, "last_snapshot_write_version", 0) >= version:
            return self._tr("本轮已写出 UART", "Written to UART"), False
        return self._tr("本轮待发送", "Pending UART write"), False

    def _result_problem(self):
        return bool(getattr(self, "scan_error", "") or getattr(self, "result_kind", "") == "filtered"
                    or any(getattr(self, "result_filter_stats", {}).values()))

    def _result_message(self):
        if getattr(self, "scan_error", ""):
            return self._tr("识别失败，保留旧结果", "Recognition failed; keeping previous result")
        kind = getattr(self, "result_kind", "initial")
        if kind == "initial":
            return self._tr("尚未完成首次识别", "Waiting for first recognition")
        if kind == "empty":
            return self._tr("本轮未检出物料", "No material detected this round")
        stats = getattr(self, "result_filter_stats", {})
        filtered = sum(stats.values())
        count = len(getattr(self.protocol, "last_snapshot_pieces", []))
        if filtered:
            return (self._tr("全部过滤:" if kind == "filtered" else "未上报:",
                            "All excluded: " if kind == "filtered" else "Excluded: ") +
                    self._tr("颜色%d 越界%d 超限%d", "color%d bounds%d excess%d")) % (
                stats.get("unknown_color", 0), stats.get("invalid_coord", 0), stats.get("truncated", 0))
        if kind == "filtered":
            return self._tr("检测到目标，全部未通过 ROI/稳定性筛选", "Targets detected; none passed ROI/stability checks")
        return self._tr("本轮上报 %d 件", "Reported %d items") % count

    def _service(self):
        """只消费链路事件和触摸，不在等待/采样回调中执行业务。"""
        now = pytime.monotonic()
        previous = getattr(self, "last_service_time", now)
        self.max_service_gap_ms = max(getattr(self, "max_service_gap_ms", 0), (now - previous) * 1000)
        self.last_service_time = now
        self.protocol.update()
        ui = getattr(self, "ui", None)
        if ui is not None:
            key = ui.poll()
            if key is not None:
                if key == "exit":
                    self._on_button(key)
                elif getattr(self, "display_locked", False):
                    if len(self.deferred_buttons) < 8:
                        self.deferred_buttons.append(key)
                else:
                    self._on_button(key)

    def _render_result(self, fallback):
        cached = getattr(self, "result_image", None)
        # 缓存和相机帧永不接受界面绘制，所有覆盖层画到副本。
        img = cached.copy() if cached is not None else fallback.copy()
        if self.ui.page == screen_ui.PAGE_RESULT:
            send, error = self._send_status()
            age = self._result_age()
            status = send + (" / %.1fs" % age if age is not None else "")
            if age is not None and age > config.RESULT_STALE_SECONDS:
                status += " / " + self._tr("结果滞留", "STALE")
                error = True
            if self.scan_error:
                status = self._result_message()
                error = True
            self.result_page, self.result_total_pages = self.ui.draw_result_panel(
                img, self.result_title, self.result_lines, self.result_page,
                empty_message=self._result_message(), status=status, status_error=error,
                history=self._tr("累计:识别错%d 写错%d 截断%d IPC丢%d 日志丢%d",
                                 "History: detect%d write%d partial%d IPCdrop%d logdrop%d") % (
                    self.scan_error_count, self.protocol.write_error_count,
                    self.protocol.write_partial_count, self.protocol.ipc_dropped_events,
                    self.protocol.log_dropped))
        else:
            if cached is not None:
                self.vision.draw_roi(img)
                self._draw_result_boxes(img)
            else:
                img.draw_rect(0, 0, self.camera_width, self.camera_height,
                              color=image.COLOR_BLACK, thickness=-1)
            self._draw_status(img)
            summary = getattr(self.protocol, "finish_summary", None)
            if summary is not None:
                ok, total, seconds = summary
                img.draw_rect(4, 49, self.camera_width - 8, 26,
                              color=image.COLOR_BLACK, thickness=-1)
                screen_ui.fit_text(img, 7, 53,
                    self._tr("完赛 成功%d/%d 用时%ds", "Finished: OK %d/%d time %ds") % (ok, total, seconds),
                    self.camera_width - 14, image.COLOR_YELLOW)
        self.ui.draw_buttons(img)
        return img

    def _draw_result_boxes(self, img):
        for idx, det in enumerate(self.result_detections):
            shape, color, _nx, _ny, bbox, _score = det
            sid = config.SHAPE_MAPPING.get(shape, 6)
            cid = config.COLOR_MAPPING.get(color, 0)
            cname, sname = screen_ui.names(sid, cid, getattr(self, "chinese", False))
            x, y, w, h = bbox
            items = getattr(self.protocol, "local_items", [])
            reason = items[idx]["reason"] if idx < len(items) else ""
            written = getattr(self.protocol, "last_snapshot_write_version", 0) >= self.result_version
            flag = (self._tr("未上报", "Excluded") if reason else
                    self._tr("已写出", "Written") if written else self._tr("待发送", "Pending"))
            color_code = image.COLOR_RED if reason else image.COLOR_GREEN if written else image.COLOR_YELLOW
            img.draw_rect(x, y, w, h, color=color_code, thickness=2)
            screen_ui.fit_text(img, x, max(46, y - 14), "%s %s %s" % (cname, sname, flag),
                max(20, self.camera_width - x - 4), color_code, 0.8)

    def _execute_scan(self):
        """执行扫描检测（含稳定等待、旧帧冲刷、多帧投票）
        返回: (detections, scan_img) scan_img为实际用于检测的干净帧
        """
        # 扫描前准备：等待物料稳定 + 冲刷摄像头缓冲区旧帧
        # 解决"首轮扫描漏检"：请求到达时托盘可能刚停稳（画面模糊），
        # 且缓冲区可能积压运动过程中的旧帧
        settle_ms = getattr(config, "SCAN_SETTLE_MS", 0)
        flush_frames = getattr(config, "SCAN_FLUSH_FRAMES", 0)
        if settle_ms > 0:
            print(f"[SCAN] 等待物料稳定 {settle_ms}ms...")
            # 等待期间继续收串口、发心跳，避免 MCU 判定掉线
            self._pump(settle_ms)
        if flush_frames > 0:
            for _ in range(flush_frames):
                self._service()
                if app.need_exit():
                    raise InterruptedError("scan cancelled")
                self.cam.read()
            print(f"[SCAN] 已冲刷{flush_frames}帧缓冲区旧帧")

        # 执行检测（使用干净帧，含详细过程日志）
        if getattr(config, "SCAN_MULTI_FRAME", False):
            # 保存实际采样帧的副本；不额外读取一帧冒充识别画面。
            sampled = [None]
            def read_sample():
                if app.need_exit():
                    raise InterruptedError("scan cancelled")
                frame = self.cam.read()
                sampled[0] = frame.copy()
                return frame
            detections = self.vision.detect_objects_stable(
                read_sample, service_protocol=self._service, wait_ms=self._pump)
            scan_img = sampled[0]
        else:
            # 单帧：重新读取干净帧（不要用绘制过标注的图）
            self._service()
            frame = self.cam.read()
            scan_img = frame.copy()
            detections = self.vision.detect_objects(frame)

        if app.need_exit():
            raise InterruptedError("scan cancelled")

        return detections, scan_img

    def _handle_snapshot_scan(self, img):
        """周期或本地触发完整识别；通信进程独立定时发送已完成的快照。
        返回: (detections, scan_img) scan_img为实际用于检测的干净帧
        """
        self.protocol.current_state = (proto_module.Protocol.STATE_FINISHED
            if self.protocol.finished else proto_module.Protocol.STATE_SCANNING)

        print("[SCAN] 开始执行扫描...")

        # 显示扫描中
        # 注意：该文字及之前绘制的ROI框/状态文字会污染图像，
        # 检测前必须重新读取干净帧，严禁在绘制过的图上检测！
        self.recognizing = True
        img.draw_string(254, 2, self._tr("识别中", "Scanning"), color=image.COLOR_YELLOW, scale=0.7)
        self.disp.show(img)

        # 执行检测（含详细过程日志）
        scan_start = pytime.monotonic()
        try:
            detections, scan_img = self._execute_scan()
        except InterruptedError:
            self.recognizing = False
            return None, img
        except Exception as e:
            self.recognizing = False
            self.scan_error = str(e)
            self.scan_error_count = getattr(self, "scan_error_count", 0) + 1
            print(f"⚠ 本轮快照识别失败，保留旧缓存: {e}")
            self.next_snapshot_scan = pytime.monotonic() + config.SNAPSHOT_INTERVAL_MS / 1000.0
            self.protocol.current_state = proto_module.Protocol.STATE_FAULT
            self._diag_event("SNAP_FAILED %s" % type(e).__name__)
            return None, img
        scan_elapsed = (pytime.monotonic() - scan_start) * 1000
        journal = getattr(self, "journal", None)
        if journal:
            journal.event("SCAN_TIMING", elapsed_ms=scan_elapsed,
                frames=getattr(self.vision, "last_frame_metrics", []))

        # 安装已完成的快照，发送由通信进程的独立时钟调度
        self.recognizing = False
        self.scan_error = ""
        self.result_image = scan_img
        self.result_detections = list(detections)
        self.result_completed_at = pytime.monotonic()
        self.result_stamp = pytime.strftime("%H:%M:%S", pytime.localtime())
        try:
            sent_count = self.protocol.publish_snapshot(detections)
        except Exception as exc:
            sent_count = len(getattr(self.protocol, "last_snapshot_pieces", []))
            self._diag_event("SNAP_IPC_FAILED %s" % exc)
        self.result_version = getattr(self.protocol, "local_result_version",
                                      getattr(self, "result_version", 0) + 1)
        self.result_filter_stats = dict(getattr(self.protocol, "snapshot_filter_stats", {}))
        raw_count = self.vision.last_scan_stats.get("yolo_raw", 0)
        self.result_kind = "materials" if sent_count else "filtered" if detections or raw_count else "empty"
        self._capture_scan_result()
        period = config.SNAPSHOT_INTERVAL_MS / 1000.0
        elapsed = pytime.monotonic() - scan_start
        # 跳过已错过的扫描槽，不积压一串补扫任务。
        self.next_snapshot_scan = scan_start + (int(elapsed / period) + 1) * period

        # 打印本次扫描总结
        stats = self.vision.last_scan_stats
        print(f"[SCAN] 扫描完成: 耗时{scan_elapsed:.0f}ms, "
              f"YOLO检出={stats['yolo_raw']}, ROI外剔除={stats['roi_out']}, "
              f"颜色未识别={stats['color_unknown']}, 实际上报={sent_count}")

        # 返回READY状态
        self._restore_state()

        return detections, scan_img

    def _handle_recheck_request(self, img):
        """处理复核请求：重新扫描并与MCU期望清单比对
        返回: (detections, scan_img)
        """
        self.protocol.current_state = proto_module.Protocol.STATE_RECHECKING

        # 本轮事务的期望清单与 SEQ 必须在这里固定成本地快照。
        # 复核扫描期间会继续收串口，新的 RECHECK_REQ 会改写共享字段，
        # 若等扫描结束后再去读，本轮就会用下一份清单做判定。
        req_seq, expected = self.protocol.take_recheck_request()

        print(f"[RECHECK] 开始复核扫描... (seq={req_seq:02X}, "
              f"期望{len(expected)}件)")

        img.draw_string(254, 2, self._tr("复核中", "Recheck"), color=image.COLOR_YELLOW, scale=0.7)
        self.disp.show(img)

        # 复用扫描流程
        scan_start = pytime.monotonic()
        try:
            detections, scan_img = self._execute_scan()
        except InterruptedError:
            return None, img
        except Exception as exc:
            self._diag_event("RECHECK_FAILED %s" % exc)
            self._restore_state()
            return None, img
        scan_elapsed = (pytime.monotonic() - scan_start) * 1000

        # 比对并发送复核结果，显式带上本轮固定的清单与 SEQ
        match, sent_count = self.protocol.send_recheck_result(
            expected, detections, req_seq)

        print(f"[RECHECK] 复核完成: 耗时{scan_elapsed:.0f}ms, "
              f"期望{len(expected)}件, 实际{sent_count}件, "
              f"{'✓ 一致' if match else '✗ 不一致'}")

        # 返回READY状态
        self._restore_state()

        return detections, scan_img

    def _restore_state(self):
        self.protocol.current_state = (proto_module.Protocol.STATE_FINISHED
            if self.protocol.finished else proto_module.Protocol.STATE_READY)

    def _pump(self, ms, img=None, step_ms=30):
        """等待期间持续服务协议与显示

        扫描的稳定等待、PICK/DUMP/FINISH 的屏显会占用几百毫秒到 2 秒。
        每片都调用 protocol.update() 消费通信进程事件并检查其状态。
        UART 收发、心跳与缓存快照由独立通信进程持续服务。
        """
        deadline = pytime.monotonic() + ms / 1000.0
        while not app.need_exit():
            self._service()
            if app.need_exit():
                break
            if img is not None:
                if getattr(self, "ui", None) is not None:
                    self.ui.draw_buttons(img)
                self.disp.show(img)
            remain = deadline - pytime.monotonic()
            if remain <= 0:
                break
            # 下限 1ms：避免算出 0 让 sleep_ms 变成空转
            time.sleep_ms(max(1, int(min(step_ms, remain * 1000))))

    def _handle_dump_display(self, img):
        """处理倾倒完成显示"""
        if not self.protocol.dump_display_pending:
            return False

        self.protocol.dump_display_pending = False
        self.protocol.current_state = proto_module.Protocol.STATE_DUMPING

        img.draw_rect(0, 100, self.camera_width, 60,
                      color=image.COLOR_BLACK, thickness=-1)
        img.draw_string(60, 115, self._tr("倾倒完成", "Dump Done"), color=image.COLOR_GREEN, scale=1.2)
        self.disp.show(img)
        self._pump(config.DISPLAY_DURATION, img)

        self._restore_state()
        return True

    def _handle_finish_display(self, img):
        """处理比赛结束显示（保持FINISHED状态）"""
        if self.protocol.finish_display_pending is None:
            return False

        total_ok, total, elapsed_s = self.protocol.finish_display_pending
        self.protocol.finish_display_pending = None

        img.draw_rect(0, 50, self.camera_width, 150,
                      color=image.COLOR_BLACK, thickness=-1)
        img.draw_string(20, 60, self._tr("分拣完成", "Match Finished!"), color=image.COLOR_YELLOW, scale=1.2)
        img.draw_string(20, 95, f"OK: {total_ok}/{total}", color=image.COLOR_GREEN, scale=1.5)
        img.draw_string(20, 130, f"Time: {elapsed_s}s", color=image.COLOR_WHITE, scale=1.5)
        self.disp.show(img)
        self._pump(2000, img)

        # 保持FINISHED状态，等待MCU后续指令
        return True

    def _handle_pick_display(self, img):
        """处理PICK_REPORT显示"""
        if not self.protocol.pick_report_queue:
            return False

        self.protocol.current_state = proto_module.Protocol.STATE_DISPLAYING

        req_seq, seq_no, shape, color, res, total_ok = self.protocol.pick_report_queue.popleft()
        self._diag_event("PICK 序号=%d 形状=%d 颜色=%d 结果=%d"
                         % (seq_no, shape, color, res))

        color_name, shape_name = screen_ui.names(shape, color, getattr(self, "chinese", False))
        result_text = (self._tr("成功", "Success") if res == proto_module.Protocol.R_OK
                       else self._tr("失败", "Failed"))
        result_color = image.COLOR_GREEN if res == proto_module.Protocol.R_OK else image.COLOR_RED

        # 只有显示完成才可回成功 ACK；失败留给 MCU 显示超时策略
        self.display_locked = True
        try:
            # 安全取色：部分颜色常量在某些MaixPy版本不存在(如COLOR_CYAN)
            cyan = getattr(image, "COLOR_CYAN", image.COLOR_WHITE)

            # 清空背景
            img.draw_rect(0, 50, self.camera_width, 150, color=image.COLOR_BLACK, thickness=-1)

            # 显示四要素
            screen_ui.fit_text(img, 20, 60, self._tr("序号: %d", "Item: %d") % seq_no,
                214, image.COLOR_WHITE, 1.2)
            screen_ui.fit_text(img, 20, 90, f"{color_name} {shape_name}", 214, cyan, 1.2)
            img.draw_string(20, 120, result_text, color=result_color, scale=1.2)
            screen_ui.fit_text(img, 20, 150, self._tr("累计成功: %d", "Total OK: %d") % total_ok,
                214, image.COLOR_YELLOW, 1.2)
            self._draw_piece_picture(img, shape)

            self.disp.show(img)
            self._pump(config.DISPLAY_DURATION, img)
        except Exception as e:
            print(f"⚠ PICK_REPORT显示失败，不回成功ACK: {e}")
            self.protocol.report_pick_failed(req_seq)
            self._restore_state()
            return True
        finally:
            self.display_locked = False

        if app.need_exit():
            self.protocol.report_pick_failed(req_seq)
            return True

        # 显示完成后发送应答，满足 MCU 锁步门控
        self.protocol.send_pick_ack(req_seq)
        print(f"✓ 已显示并应答PICK_REPORT #{seq_no}")

        self._restore_state()
        return True

    @staticmethod
    def _draw_piece_picture(img, shape):
        """按形状 ID 绘制物料图示，避免快照重排后拿错实物裁剪图片。"""
        points = {
            1: [(270, 75), (245, 125), (295, 125)],
            2: [(246, 80), (286, 80), (286, 120), (246, 120)],
            3: [(270, 75), (245, 125), (295, 125)],
            4: [(240, 90), (300, 90), (300, 120), (240, 120)],
            5: [(255, 80), (285, 80), (300, 100), (285, 120), (255, 120), (240, 100)],
            6: [(245, 85), (275, 80), (295, 100), (280, 125), (240, 110)],
        }.get(shape, [(245, 85), (295, 85), (270, 125)])
        for a, b in zip(points, points[1:] + points[:1]):
            img.draw_line(*a, *b, color=image.COLOR_WHITE, thickness=2)
        if shape == 1:
            # 圆锥为平滑的椭圆底面，与棱锥的棱线区分。
            import math
            ellipse = [(int(270 + 25 * math.cos(a * math.pi / 8)),
                        int(125 + 7 * math.sin(a * math.pi / 8))) for a in range(16)]
            for a, b in zip(ellipse, ellipse[1:] + ellipse[:1]):
                img.draw_line(*a, *b, color=image.COLOR_WHITE, thickness=1)
        elif shape == 3:
            img.draw_line(270, 75, 270, 115, color=image.COLOR_WHITE, thickness=1)
            img.draw_line(270, 115, 245, 125, color=image.COLOR_WHITE, thickness=1)
            img.draw_line(270, 115, 295, 125, color=image.COLOR_WHITE, thickness=1)

    def _capture_scan_result(self):
        """只取周期识别数据；每件两行，未上报件明确说明原因。"""
        self.result_lines = []
        items = getattr(self.protocol, "local_items", [])
        for idx, item in enumerate(items):
            cname, sname = screen_ui.names(item["shape"], item["color"], getattr(self, "chinese", False))
            pick = self._tr("可抓", "Pickable") if item["pickable"] else self._tr("不可抓", "Not pickable")
            reason = item["reason"]
            reason_text = {
                "unknown_color": self._tr("未上报:颜色未知", "Excluded: unknown color"),
                "invalid_coord": self._tr("未上报:坐标越界", "Excluded: out of bounds"),
                "truncated": self._tr("未上报:超过12件", "Excluded: over 12 items"),
            }.get(reason, pick)
            self.result_lines.append("#%d %s %s %s" % (idx + 1, cname, sname, reason_text))
            self.result_lines.append("  X=%.1fmm Y=%.1fmm" % (item["x"] / 10, item["y"] / 10))
        self.result_title = self._tr("物料 %d 件", "%d materials") % len(items)
        self.result_title += " @" + getattr(self, "result_stamp", "--:--:--")
        self.result_total_pages = max(1, (len(self.result_lines) + screen_ui.PANEL_ROWS - 1) // screen_ui.PANEL_ROWS)
        self.result_page = min(getattr(self, "result_page", 0), self.result_total_pages - 1)

    def _reload_config(self):
        """重新加载 ROI 标定与 HSV 阈值，不必重启进程

        全部构件都先建成临时对象，成功后才统一替换。
        否则 reload(config) 已经改掉了共享配置，一旦后续构造失败，
        就会出现"新配置 + 旧检测器 + 旧转换器"的混合状态。
        """
        print("[UI] 开始重载配置...")

        # 步骤1： 备份 reload 之前的配置常量，供失败回滚
        snapshot = {k: v for k, v in vars(config).items() if k.isupper()}

        # 步骤2： 在临时对象上完成全部构造
        try:
            importlib.reload(config)
            new_vision = VisionDetector(self.detector,
                                        self.camera_width, self.camera_height)
            # 比例因子在构造时写入，不重建则 COORD_SCALE_FACTOR 仍旧值
            new_converter = CoordinateConverter()
        except Exception as e:
            # 2.1 回滚：把 config 的公开常量写回 reload 之前的值，
            #     检测器与转换器保持原样，三者仍然一致
            for k, v in snapshot.items():
                setattr(config, k, v)
            print(f"⚠ 配置重载失败，已回滚到原配置: {e}")
            return

        # 步骤3： 全部成功后再统一生效
        self.vision = new_vision
        self.protocol.converter = new_converter
        print("✓ 已重载 ROI/HSV 配置")

    def _on_button(self, key):
        """处理屏幕按键事件"""
        # 步骤1： 手动扫描
        # 本地键只请求刷新快照，不制造 MCU 扫描事务。
        if key == "result":
            self.ui.set_page(screen_ui.PAGE_RESULT)
            print("[UI] 打开扫描结果面板")

        elif key == "back":
            self.ui.set_page(screen_ui.PAGE_MAIN)
            print("[UI] 返回主界面")

        elif key == "prev":
            self.result_page = max(0, self.result_page - 1)

        elif key == "next":
            self.result_page = min(max(0, self.result_total_pages - 1),
                                   self.result_page + 1)

        # 步骤4： 退出
        # 说明：exit 为长按键，能走到这里说明已按住 1.5s
        elif key == "exit":
            print("[UI] 长按确认退出视觉程序")
            app.set_exit_flag(True)

    @staticmethod
    def _read_proc_value(path, key):
        """从 /proc 文件里取一行数值（单位 kB），失败返回 -1"""
        try:
            with open(path, "r") as f:
                for line in f:
                    if line.startswith(key):
                        return int(line.split()[1])
        except Exception:
            pass
        return -1

    @staticmethod
    def _read_uptime():
        """系统开机时长(秒)，归零说明整机重启过"""
        try:
            with open("/proc/uptime", "r") as f:
                return float(f.read().split()[0])
        except Exception:
            return -1.0

    def _diag_tick(self):
        """步骤1： 周期性落盘运行现场

        目的：卡死/重启后仍能读到现场。stdout 上的日志随进程一起丢，
        这里每行 flush 到 eMMC 上的文件，重启后依然可读。

        记录内容：
        1) 系统开机时长 —— 归零即为整机重启，不归零说明只是进程退出
        2) 循环速率 —— 掉到接近 0 说明主循环被拖死
        3) MCU 命令直方图与接收字节 —— 判断对端是否在灌数据
        4) 串口最大单次读取与缓冲残留 —— 判断是否出现突发
        5) 进程 RSS 与系统可用内存 —— 判断是否内存泄漏
        """
        if not getattr(config, "DIAG_LOG_ENABLED", False):
            return

        now = pytime.monotonic()
        interval = getattr(config, "DIAG_LOG_INTERVAL", 5.0)
        if now - self.diag_last_time < interval:
            return
        self.diag_last_time = now

        # 1.1 采集运行指标
        rss = self._read_proc_value("/proc/self/status", "VmRSS:")
        avail = self._read_proc_value("/proc/meminfo", "MemAvailable:")
        elapsed = now - self.diag_start_time
        rate = self.loop_count / elapsed if elapsed > 0 else 0.0

        # 1.2 拼命令直方图，只取最多的前6个
        top = sorted(self.protocol.cmd_counts.items(), key=lambda kv: -kv[1])[:6]
        cmds = " ".join("%02X=%d" % (c, n) for c, n in top) or "-"

        # 1.3 落盘
        line = ("t=%.0fs up=%.0fs loop=%d(%.1f/s) scan=%d rechk=%d "
                "rx=%dB/%d帧 maxchunk=%d buf=%d txerr=%d pickdrop=%d "
                "crc=%d badlen=%d drop=%d rss=%dkB avail=%dkB cmd[%s]"
                % (elapsed, self._read_uptime(), self.loop_count, rate,
                   self.protocol.scan_count, self.protocol.recheck_count,
                   self.protocol.rx_bytes_total, self.protocol.rx_frames_total,
                   self.protocol.max_rx_chunk, len(self.protocol.proto.rx_buffer),
                   self.protocol.write_error_count, self.protocol.pick_drop_count,
                   self.protocol.proto.crc_errors, self.protocol.proto.bad_len,
                   self.protocol.proto.dropped_bytes,
                   rss, avail, cmds))
        self.journal.event("MEDIA", text=line, result_version=self.result_version,
            result_age_s=self._result_age(), scan_error=self.scan_error,
            scan_error_count=self.scan_error_count, filter_stats=self.result_filter_stats,
            tx_partial=self.protocol.write_partial_count,
            last_written_version=self.protocol.last_snapshot_write_version,
            snapshot_write_error=self.protocol.snapshot_write_error,
            ipc_dropped=self.protocol.ipc_dropped_events,
            media_service_gap_max_ms=getattr(self, "max_service_gap_ms", 0))

    def _diag_event(self, text):
        """把关键事件立即落盘（带相对时间戳），供崩溃/重启后回溯

        比 2 秒一次的周期快照更贴近现场：媒体池损坏往往只发生在
        一两秒之内，事件行能对上当时在处理哪条 MCU 命令。
        """
        if not getattr(config, "DIAG_LOG_ENABLED", False):
            return
        journal = getattr(self, "journal", None)
        if journal:
            journal.event("MEDIA_EVENT", loop=self.loop_count, text=text)

    def run(self):
        """主循环"""
        try:
            while not app.need_exit():
                # 更新协议
                self._service()
                if app.need_exit():
                    break
                while self.deferred_buttons:
                    self._on_button(self.deferred_buttons.pop(0))

                # 诊断计数与落盘（放在耗时分支之前，保证每轮都执行）
                self.loop_count += 1
                self._diag_tick()

                # 读取图像
                raw = self.cam.read()
                img = self._render_result(raw)

                # 处理PICK显示
                if self._handle_pick_display(img):
                    continue  # 已在显示中刷新过

                # 处理倾倒完成显示
                if self._handle_dump_display(img):
                    continue

                # 处理比赛结束显示
                if self._handle_finish_display(img):
                    continue

                # 显示义务优先；复核仍是请求事务，不覆盖周期快照缓存。
                if self.protocol.recheck_requested:
                    self._diag_event("RECHECK_REQ")
                    self._handle_recheck_request(img)
                    self._diag_event("RECHECK_DONE")
                elif pytime.monotonic() >= self.next_snapshot_scan:
                    self._diag_event("SNAP_SCAN")
                    self._handle_snapshot_scan(img)
                    self._diag_event("SNAP_DONE 有效=%d" % len(self.protocol.last_scan_pieces))

                # 正常显示
                if not app.need_exit():
                    self.disp.show(self._render_result(raw))
                time.sleep_ms(30)

        except KeyboardInterrupt:
            print("\n用户中断")
        except Exception as e:
            print(f"\n错误: {e}")
            import traceback
            traceback.print_exc()
            if getattr(self, "journal", None):
                self.journal.event("FAILED", critical=True, phase="run", stack=traceback.format_exc())
        finally:
            self.cleanup()

    def cleanup(self):
        """清理资源"""
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        if getattr(self, "protocol", None) is not None:
            self.protocol.close()
        if getattr(self, "serial", None) is not None:
            self.serial.close()
        if getattr(self, "led", None):
            self.led.value(0)
        if getattr(self, "journal", None):
            self.journal.event("EXIT", critical=True)
            self.journal.close()
        print("程序退出")


def main():
    """入口函数"""
    # 步骤1： 安装优雅退出处理
    # 背景：MaixCAM 上应用被 SIGTERM 强杀时，媒体/ISP 管线会留下残留，
    #      下一个进程初始化摄像头会撞上
    #        ax_isp_iq_cfg_set_hdr: pipe[0] work on sdr, not support to set hdr param
    #        AX_ISP_ALG_AeInit: Lens Iris Init Failed
    #      随后 ISP 进入坏状态，每秒刷 os04d10 寄存器越界，十几分钟后整机复位。
    #      实测：每次 launcher.service 被停、应用收到 signal 15，几秒内洪流就开始。
    #      这里把信号转成正常退出请求，让主循环走完 finally 里的清理。
    def _graceful_exit(signum, _frame):
        print(f"\n收到信号 {signum}，请求正常退出")
        app.set_exit_flag(True)

    for _sig in [getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP")
                 if hasattr(signal, name)]:
        try:
            signal.signal(_sig, _graceful_exit)
        except Exception as e:
            print(f"⚠ 注册信号 {_sig} 处理失败: {e}")

    # 步骤2： 启动应用主循环
    vision_app = SortingVisionApp.__new__(SortingVisionApp)
    try:
        vision_app.__init__()
        vision_app.run()
    except BaseException:
        if STARTUP:
            import traceback
            STARTUP.event("FAILED", critical=True, phase="application", stack=traceback.format_exc())
        raise
    finally:
        vision_app.cleanup()


if __name__ == "__main__":
    main()
