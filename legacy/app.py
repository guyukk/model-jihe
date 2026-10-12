"""
智能分拣视觉系统 - 主应用
MaixCAM2 视觉从站
协议版本: v2.0
"""
from maix import app, camera, display, image, nn, gpio, pinmap, time, sys, err, uart
import time as pytime

import config
import protocol as proto_module
from vision import VisionDetector
from protocol_handler import ProtocolHandler


class SortingVisionApp:
    """分拣视觉应用"""

    def __init__(self):
        print("=" * 50)
        print("智能分拣视觉系统启动")
        print("=" * 50)

        # 初始化GPIO
        self._init_gpio()

        # 初始化串口
        self.serial = uart.UART(config.UART_PORT, config.UART_BAUDRATE)
        print(f"✓ 串口初始化: {config.UART_PORT} @ {config.UART_BAUDRATE}")

        # 初始化协议处理器
        self.protocol = ProtocolHandler(self.serial)
        print(f"✓ 协议版本: v{proto_module.Protocol.PROTO_VER:02X}")

        # 初始化YOLO检测器
        self.detector = nn.YOLO26(model=config.MODEL_PATH)
        print(f"✓ 模型加载: {config.MODEL_PATH}")

        # 初始化摄像头
        self.camera_width = self.detector.input_width()
        self.camera_height = self.detector.input_height()
        self.cam = camera.Camera(
            self.camera_width,
            self.camera_height,
            image.Format.FMT_RGB888
        )
        print(f"✓ 摄像头: {self.camera_width}x{self.camera_height}")

        # 初始化显示器
        self.disp = display.Display()
        print(f"✓ 显示器初始化")

        # 初始化视觉检测器
        self.vision = VisionDetector(self.detector, self.camera_width, self.camera_height)
        print(f"✓ 视觉检测器初始化")

        # 预热YOLO检测器
        self._warmup_detector()

        # 状态名称
        self.state_names = ["INIT", "READY", "SCAN", "PICK", "DISP", "RECHK", "DUMP", "FINISH", "FAULT"]

        print("=" * 50)
        print("等待MCU连接...")
        print("=" * 50)

        # 初始化心跳时间
        self.protocol.last_mcu_heartbeat_time = pytime.time()

    def _warmup_detector(self):
        """预热YOLO检测器
        NPU首次推理需要加载图/权重缓存，耗时远高于后续推理。
        若不预热，第一次扫描会因超时或空结果导致首轮漏检。
        """
        print("预热YOLO检测器(首次推理较慢，请等待)...")
        warmup_count = 3
        for i in range(warmup_count):
            start = pytime.time()
            warmup_img = self.cam.read()
            self.detector.detect(warmup_img,
                                 conf_th=config.YOLO_CONF_THRESHOLD,
                                 iou_th=config.YOLO_IOU_THRESHOLD)
            elapsed = (pytime.time() - start) * 1000
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
        """绘制状态信息"""
        # 状态文本
        state = self.protocol.current_state
        state_text = self.state_names[state] if state < len(self.state_names) else "UNKN"

        # 链路状态
        link_text = "LINK" if self.protocol.link_alive else "DISC"
        link_color = image.COLOR_GREEN if self.protocol.link_alive else image.COLOR_RED

        # 绘制状态
        img.draw_string(5, 5, f"{state_text}", color=image.COLOR_GREEN, scale=1.2)
        img.draw_string(5, 25, f"{link_text}", color=link_color, scale=1.0)

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
            time.sleep_ms(settle_ms)
        if flush_frames > 0:
            for _ in range(flush_frames):
                self.cam.read()
            print(f"[SCAN] 已冲刷{flush_frames}帧缓冲区旧帧")

        # 执行检测（使用干净帧，含详细过程日志）
        if getattr(config, "SCAN_MULTI_FRAME", False):
            # 多帧投票：每次读取新的干净帧
            detections = self.vision.detect_objects_stable(lambda: self.cam.read())
            scan_img = self.cam.read()  # 用于结果显示的干净帧
        else:
            # 单帧：重新读取干净帧（不要用绘制过标注的图）
            scan_img = self.cam.read()
            detections = self.vision.detect_objects(scan_img)

        return detections, scan_img

    def _handle_scan_request(self, img):
        """处理扫描请求
        返回: (detections, scan_img) scan_img为实际用于检测的干净帧
        """
        self.protocol.scan_requested = False
        self.protocol.current_state = proto_module.Protocol.STATE_SCANNING

        print("[SCAN] 开始执行扫描...")

        # 显示扫描中
        # 注意：该文字及之前绘制的ROI框/状态文字会污染图像，
        # 检测前必须重新读取干净帧，严禁在绘制过的图上检测！
        img.draw_string(80, 100, "Scanning...", color=image.COLOR_YELLOW, scale=1.5)
        self.disp.show(img)

        # 执行检测（含详细过程日志）
        scan_start = pytime.time()
        detections, scan_img = self._execute_scan()
        scan_elapsed = (pytime.time() - scan_start) * 1000

        # 发送结果（含逐物料映射日志）
        sent_count = self.protocol.send_scan_result(detections)

        # 打印本次扫描总结
        stats = self.vision.last_scan_stats
        print(f"[SCAN] 扫描完成: 耗时{scan_elapsed:.0f}ms, "
              f"YOLO检出={stats['yolo_raw']}, ROI外剔除={stats['roi_out']}, "
              f"颜色未识别={stats['color_unknown']}, 实际上报={sent_count}")

        # 返回READY状态
        self.protocol.current_state = proto_module.Protocol.STATE_READY

        return detections, scan_img

    def _handle_recheck_request(self, img):
        """处理复核请求：重新扫描并与MCU期望清单比对
        返回: (detections, scan_img)
        """
        self.protocol.recheck_requested = False
        self.protocol.current_state = proto_module.Protocol.STATE_RECHECKING

        print("[RECHECK] 开始复核扫描...")

        img.draw_string(80, 100, "Rechecking...", color=image.COLOR_YELLOW, scale=1.5)
        self.disp.show(img)

        # 复用扫描流程
        scan_start = pytime.time()
        detections, scan_img = self._execute_scan()
        scan_elapsed = (pytime.time() - scan_start) * 1000

        # 比对并发送复核结果
        expected = self.protocol.recheck_expected
        match, sent_count = self.protocol.send_recheck_result(expected, detections)

        print(f"[RECHECK] 复核完成: 耗时{scan_elapsed:.0f}ms, "
              f"期望{len(expected)}件, 实际{sent_count}件, "
              f"{'✓ 一致' if match else '✗ 不一致'}")

        # 返回READY状态
        self.protocol.current_state = proto_module.Protocol.STATE_READY

        return detections, scan_img

    def _handle_dump_display(self, img):
        """处理倾倒完成显示"""
        if not self.protocol.dump_display_pending:
            return False

        self.protocol.dump_display_pending = False
        self.protocol.current_state = proto_module.Protocol.STATE_DUMPING

        img.draw_rect(0, 100, self.camera_width, 60,
                      color=image.COLOR_BLACK, thickness=-1)
        img.draw_string(60, 115, "Dump Done", color=image.COLOR_GREEN, scale=1.5)
        self.disp.show(img)
        time.sleep_ms(config.DISPLAY_DURATION)

        self.protocol.current_state = proto_module.Protocol.STATE_READY
        return True

    def _handle_finish_display(self, img):
        """处理比赛结束显示（保持FINISHED状态）"""
        if self.protocol.finish_display_pending is None:
            return False

        total_ok, total, elapsed_s = self.protocol.finish_display_pending
        self.protocol.finish_display_pending = None

        img.draw_rect(0, 50, self.camera_width, 150,
                      color=image.COLOR_BLACK, thickness=-1)
        img.draw_string(20, 60, "Match Finished!", color=image.COLOR_YELLOW, scale=1.5)
        img.draw_string(20, 95, f"OK: {total_ok}/{total}", color=image.COLOR_GREEN, scale=1.5)
        img.draw_string(20, 130, f"Time: {elapsed_s}s", color=image.COLOR_WHITE, scale=1.5)
        self.disp.show(img)
        time.sleep_ms(2000)

        # 保持FINISHED状态，等待MCU后续指令
        return True

    def _handle_pick_display(self, img):
        """处理PICK_REPORT显示"""
        if not self.protocol.pick_report_queue:
            return False

        self.protocol.current_state = proto_module.Protocol.STATE_DISPLAYING

        req_seq, seq_no, shape, color, res, total_ok = self.protocol.pick_report_queue.pop(0)

        # 获取名称
        shape_name = proto_module.SHAPE_NAMES.get(shape, "未知")
        color_name = proto_module.COLOR_NAMES.get(color, "未知")
        result_text = "✓ 成功" if res == proto_module.Protocol.R_OK else "✗ 失败"
        result_color = image.COLOR_GREEN if res == proto_module.Protocol.R_OK else image.COLOR_RED

        # 显示失败不影响协议应答，绘制过程做容错
        try:
            # 安全取色：部分颜色常量在某些MaixPy版本不存在(如COLOR_CYAN)
            cyan = getattr(image, "COLOR_CYAN", image.COLOR_WHITE)

            # 清空背景
            img.draw_rect(0, 50, self.camera_width, 150, color=image.COLOR_BLACK, thickness=-1)

            # 显示四要素
            img.draw_string(20, 60, f"序号: {seq_no}", color=image.COLOR_WHITE, scale=1.5)
            img.draw_string(20, 90, f"{shape_name} {color_name}", color=cyan, scale=1.5)
            img.draw_string(20, 120, result_text, color=result_color, scale=1.5)
            img.draw_string(20, 150, f"累计: {total_ok}", color=image.COLOR_YELLOW, scale=1.5)

            self.disp.show(img)
            time.sleep_ms(config.DISPLAY_DURATION)
        except Exception as e:
            print(f"⚠ PICK_REPORT显示异常(不影响应答): {e}")

        # 发送应答（必须执行，否则MCU会一直等待）
        self.protocol.send_pick_ack(req_seq)
        print(f"✓ 已显示并应答PICK_REPORT #{seq_no}")

        self.protocol.current_state = proto_module.Protocol.STATE_READY
        return True

    def run(self):
        """主循环"""
        try:
            while not app.need_exit():
                # 更新协议
                self.protocol.update()

                # 读取图像
                img = self.cam.read()

                # 绘制ROI
                self.vision.draw_roi(img)

                # 绘制状态
                self._draw_status(img)

                # 处理扫描请求
                if self.protocol.scan_requested:
                    detections, img = self._handle_scan_request(img)
                    # 在实际用于检测的干净帧上标记检测结果
                    if detections:
                        self.vision.draw_detections(img, detections)

                # 处理复核请求
                if self.protocol.recheck_requested:
                    detections, img = self._handle_recheck_request(img)
                    if detections:
                        self.vision.draw_detections(img, detections)

                # 处理PICK显示
                if self._handle_pick_display(img):
                    continue  # 已在显示中刷新过

                # 处理倾倒完成显示
                if self._handle_dump_display(img):
                    continue

                # 处理比赛结束显示
                if self._handle_finish_display(img):
                    continue

                # 正常显示
                self.disp.show(img)
                time.sleep_ms(30)

        except KeyboardInterrupt:
            print("\n用户中断")
        except Exception as e:
            print(f"\n错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.cleanup()

    def cleanup(self):
        """清理资源"""
        if self.led:
            self.led.value(0)
        print("程序退出")


def main():
    """入口函数"""
    app = SortingVisionApp()
    app.run()


if __name__ == "__main__":
    main()
