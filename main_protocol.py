import cv2
import json
import numpy as np
import os
import time as pytime

from maix import app, camera, display, image, nn, gpio, pinmap, time, sys, err, uart

# 导入协议模块
import protocol as proto_module
from coordinate import CoordinateConverter

pin_name = "B25" if sys.device_id() == "maixcam2" else "B3"
gpio_name = "GPIOB25" if sys.device_id() == "maixcam2" else "GPIOB3"

err.check_raise(pinmap.set_pin_function(pin_name, gpio_name), "set pin failed")
led = gpio.GPIO(gpio_name, gpio.Mode.OUT)
led.value(0)

# 初始化串口 UART4
serial = uart.UART("/dev/ttyS4", 115200)

# 初始化协议
proto = proto_module.Protocol()
coordinate_converter = CoordinateConverter()

MODEL = "/root/models/model-jihe/model_9338.mud"
CAMERA_WIDTH = 320
CAMERA_HEIGHT = 256
ROI_CONFIG_PATH = "/root/roi_config.json"

# ROI配置（基于320x256分辨率标定）
roi = {
    "x": 70,
    "y": 50,
    "width": 170,
    "height": 165
}
if os.path.exists(ROI_CONFIG_PATH):
    with open(ROI_CONFIG_PATH, "r") as roi_file:
        saved_roi = json.load(roi_file)
    if not isinstance(saved_roi, dict):
        raise ValueError(f"ROI配置必须是JSON对象: {ROI_CONFIG_PATH}")
    for key in ("x", "y", "width", "height"):
        value = saved_roi.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"ROI配置字段 {key} 必须为整数: {ROI_CONFIG_PATH}")
        roi[key] = value
    if (roi["x"] < 0 or roi["y"] < 0 or roi["width"] <= 0 or
            roi["height"] <= 0 or roi["x"] + roi["width"] > CAMERA_WIDTH or
            roi["y"] + roi["height"] > CAMERA_HEIGHT):
        raise ValueError(f"ROI配置超出标定分辨率范围: {ROI_CONFIG_PATH}")
print(f"ROI配置: ({roi['x']}, {roi['y']}) {roi['width']}x{roi['height']}")

# HSV thresholds
LOWER_BLUE1 = np.array([0, 132, 97], dtype=np.uint8)
UPPER_BLUE1 = np.array([93, 255, 255], dtype=np.uint8)
LOWER_YELLOW = np.array([52, 139, 185], dtype=np.uint8)
UPPER_YELLOW = np.array([112, 255, 255], dtype=np.uint8)
LOWER_RED = np.array([40, 173, 191], dtype=np.uint8)
UPPER_RED = np.array([156, 255, 255], dtype=np.uint8)
LOWER_WHITE = np.array([0, 0, 173], dtype=np.uint8)
UPPER_WHITE = np.array([179, 60, 255], dtype=np.uint8)

COLOR_MIN_RATIO = 0.05
PIXEL_SAMPLE_STEP = 2

# 形状名称映射（模型输出 -> 协议ID）
SHAPE_MAP = {
    "cube": proto_module.Protocol.SHAPE_SQUARE,
    "cone": proto_module.Protocol.SHAPE_CIRCLE,
    "pyramid": proto_module.Protocol.SHAPE_TRIANGLE,
    "5edge": proto_module.Protocol.SHAPE_PENTAGON
}

# 颜色映射（HSV检测 -> 协议ID）
COLOR_MAP = {
    "red": proto_module.Protocol.COLOR_RED,
    "yellow": proto_module.Protocol.COLOR_YELLOW,
    "blue": proto_module.Protocol.COLOR_BLUE,
    "white": proto_module.Protocol.COLOR_WHITE
}

# 全局状态
current_state = proto_module.Protocol.STATE_INIT
last_heartbeat_time = 0
last_mcu_heartbeat_time = 0
link_alive = False
scan_requested = False
scan_req_seq = 0
pick_report_queue = []  # 待显示的PICK_REPORT队列


def _red_mask(hsv_roi):
    """Build the red mask using the calibrated thresholds."""
    return cv2.inRange(hsv_roi, LOWER_RED, UPPER_RED)


def detect_color(hsv_roi):
    """Return the dominant configured color and its pixel ratio in an ROI."""
    masks = {
        "blue": cv2.inRange(hsv_roi, LOWER_BLUE1, UPPER_BLUE1),
        "yellow": cv2.inRange(hsv_roi, LOWER_YELLOW, UPPER_YELLOW),
        "red": _red_mask(hsv_roi),
        "white": cv2.inRange(hsv_roi, LOWER_WHITE, UPPER_WHITE),
    }
    roi_area = hsv_roi.shape[0] * hsv_roi.shape[1]
    if roi_area == 0:
        return "unknown", 0.0

    color, pixel_count = max(
        ((name, cv2.countNonZero(mask)) for name, mask in masks.items()),
        key=lambda item: item[1],
    )
    ratio = pixel_count / roi_area
    if ratio < COLOR_MIN_RATIO:
        return "unknown", ratio
    return color, ratio


def _get_rgb_pixel(img, x, y):
    """Read one RGB pixel from a MaixPy Image."""
    pixel = img.get_pixel(x, y)
    if isinstance(pixel, (tuple, list)):
        if len(pixel) >= 3:
            return pixel[0], pixel[1], pixel[2]
        if len(pixel) == 1:
            pixel = pixel[0]
        else:
            return 0, 0, 0

    pixel = int(pixel)
    return (pixel >> 16) & 0xFF, (pixel >> 8) & 0xFF, pixel & 0xFF


def detect_color_in_box(img, x1, y1, x2, y2):
    """Sample a detection box and classify it with the configured HSV masks."""
    width = x2 - x1
    height = y2 - y1
    if width <= 0 or height <= 0:
        return "unknown", 0.0

    step = PIXEL_SAMPLE_STEP
    sample_width = (width + step - 1) // step
    sample_height = (height + step - 1) // step
    rgb_roi = np.empty((sample_height, sample_width, 3), dtype=np.uint8)
    for sample_y, y in enumerate(range(y1, y2, step)):
        for sample_x, x in enumerate(range(x1, x2, step)):
            rgb_roi[sample_y, sample_x] = _get_rgb_pixel(img, x, y)

    hsv_roi = cv2.cvtColor(rgb_roi, cv2.COLOR_RGB2HSV)
    return detect_color(hsv_roi)


def is_in_roi(center_x, center_y, roi_config):
    """检查物体中心坐标是否在ROI矩形内"""
    if roi_config is None:
        return True

    x_in = roi_config["x"] <= center_x <= roi_config["x"] + roi_config["width"]
    y_in = roi_config["y"] <= center_y <= roi_config["y"] + roi_config["height"]
    return x_in and y_in


def normalize_coordinates(center_x, center_y, roi_config):
    """将物体坐标归一化到ROI坐标系（0-1范围）"""
    if roi_config is None:
        return center_x, center_y

    rel_x = center_x - roi_config["x"]
    rel_y = center_y - roi_config["y"]

    norm_x = rel_x / roi_config["width"]
    norm_y = rel_y / roi_config["height"]

    norm_x = max(0.0, min(1.0, norm_x))
    norm_y = max(0.0, min(1.0, norm_y))

    return norm_x, norm_y


def send_heartbeat():
    """发送心跳"""
    global last_heartbeat_time, current_state
    frame, _ = proto.encode_heartbeat(current_state, err=0)
    serial.write(frame)
    last_heartbeat_time = pytime.time()


def check_link_alive():
    """检查链路是否存活"""
    global link_alive, last_mcu_heartbeat_time
    elapsed = pytime.time() - last_mcu_heartbeat_time
    if elapsed > 1.5:  # 1500ms
        if link_alive:
            print("链路中断！")
            link_alive = False
    else:
        if not link_alive:
            print("链路恢复")
            link_alive = True


def handle_sys_start(seq, payload):
    """处理SYS_START"""
    global current_state
    mode, proto_ver = proto.decode_sys_start(payload)
    print(f"收到SYS_START: mode={mode}, proto_ver=0x{proto_ver:02X}")

    if proto_ver != proto_module.Protocol.PROTO_VER:
        # 版本不匹配
        ack_frame, _ = proto.encode_ack(
            proto_module.Protocol.CMD_ACK_SYS_START,
            proto_module.Protocol.R_PARAM,
            seq,
            extra=bytes([proto_module.Protocol.PROTO_VER])
        )
        serial.write(ack_frame)
        print(f"协议版本不匹配！期望0x{proto_module.Protocol.PROTO_VER:02X}")
        return

    # 应答成功
    ack_frame, _ = proto.encode_ack(
        proto_module.Protocol.CMD_ACK_SYS_START,
        proto_module.Protocol.R_OK,
        seq,
        extra=bytes([proto_module.Protocol.PROTO_VER])
    )
    serial.write(ack_frame)
    current_state = proto_module.Protocol.STATE_READY
    print("系统启动确认，进入READY状态")


def handle_scan_req(seq, payload):
    """处理SCAN_REQ"""
    global scan_requested, scan_req_seq, current_state
    reason = proto.decode_scan_req(payload)
    print(f"收到SCAN_REQ: reason={reason}")

    scan_requested = True
    scan_req_seq = seq
    current_state = proto_module.Protocol.STATE_SCANNING


def handle_pick_report(seq, payload):
    """处理PICK_REPORT"""
    global pick_report_queue
    result = proto.decode_pick_report(payload)
    if result:
        seq_no, shape, color, res, total_ok = result
        print(f"收到PICK_REPORT: 序号={seq_no}, 形状={shape}, 颜色={color}, 结果={res}, 累计={total_ok}")

        # 加入显示队列
        pick_report_queue.append((seq, seq_no, shape, color, res, total_ok))


def process_incoming_frames():
    """处理接收到的帧"""
    global last_mcu_heartbeat_time

    # 读取串口数据
    if serial.available() > 0:
        data = serial.read(serial.available())
        proto.rx_buffer.extend(data)

    # 解析帧
    frames = proto.process_rx_buffer()

    for cmd, seq, payload in frames:
        if cmd == proto_module.Protocol.CMD_HB_M:
            # MCU心跳
            last_mcu_heartbeat_time = pytime.time()

        elif cmd == proto_module.Protocol.CMD_SYS_START:
            handle_sys_start(seq, payload)

        elif cmd == proto_module.Protocol.CMD_SCAN_REQ:
            handle_scan_req(seq, payload)

        elif cmd == proto_module.Protocol.CMD_PICK_REPORT:
            handle_pick_report(seq, payload)

        elif cmd == proto_module.Protocol.CMD_PING:
            # PING测试
            ack_frame, _ = proto.encode_ack(
                proto_module.Protocol.CMD_ACK_PING,
                proto_module.Protocol.R_OK,
                seq,
                extra=payload
            )
            serial.write(ack_frame)
            print("回应PING")


# 初始化检测器和摄像头
detector = nn.YOLO26(model=MODEL)
cam = camera.Camera(CAMERA_WIDTH, CAMERA_HEIGHT, image.Format.FMT_RGB888)
disp = display.Display()

# ROI坐标缩放
ROI_CALIBRATION_WIDTH = CAMERA_WIDTH
ROI_CALIBRATION_HEIGHT = CAMERA_HEIGHT
scale_x = CAMERA_WIDTH / ROI_CALIBRATION_WIDTH
scale_y = CAMERA_HEIGHT / ROI_CALIBRATION_HEIGHT
roi_scaled = {
    "x": int(roi["x"] * scale_x),
    "y": int(roi["y"] * scale_y),
    "width": int(roi["width"] * scale_x),
    "height": int(roi["height"] * scale_y)
}
print(f"摄像头分辨率: {CAMERA_WIDTH}x{CAMERA_HEIGHT}")
print(f"ROI缩放比例: x={scale_x:.3f}, y={scale_y:.3f}")
print(f"缩放后ROI: ({roi_scaled['x']}, {roi_scaled['y']}) {roi_scaled['width']}x{roi_scaled['height']}")

print("视觉系统启动，等待MCU连接...")
last_mcu_heartbeat_time = pytime.time()

try:
    while not app.need_exit():
        current_time = pytime.time()

        # 处理接收帧
        process_incoming_frames()

        # 发送心跳 (250ms)
        if current_time - last_heartbeat_time >= 0.25:
            send_heartbeat()

        # 检查链路
        check_link_alive()

        # 读取图像
        img = cam.read()

        # 绘制ROI
        if roi_scaled:
            img.draw_rect(
                roi_scaled["x"], roi_scaled["y"],
                roi_scaled["width"], roi_scaled["height"],
                color=image.COLOR_BLUE, thickness=2
            )

        # 显示状态
        state_names = ["INIT", "READY", "SCAN", "PICK", "DISP", "RECHK", "DUMP", "FINISH", "FAULT"]
        state_text = state_names[current_state] if current_state < len(state_names) else "UNKN"
        link_text = "LINK" if link_alive else "DISC"
        img.draw_string(5, 5, f"{state_text} {link_text}", color=image.COLOR_GREEN, scale=1.0)

        # 如果收到扫描请求，执行扫描
        if scan_requested:
            scan_requested = False
            current_state = proto_module.Protocol.STATE_SCANNING

            img.draw_string(5, 25, "Scanning...", color=image.COLOR_YELLOW, scale=1.0)
            disp.show(img)

            # 执行检测
            objects = detector.detect(img, conf_th=0.5, iou_th=0.45)

            pieces = []
            for obj in objects:
                x1 = max(0, int(obj.x))
                y1 = max(0, int(obj.y))
                x2 = min(CAMERA_WIDTH, x1 + max(0, int(obj.w)))
                y2 = min(CAMERA_HEIGHT, y1 + max(0, int(obj.h)))

                center_x = int(obj.x + obj.w / 2)
                center_y = int(obj.y + obj.h / 2)

                # 检查ROI
                if not is_in_roi(center_x, center_y, roi_scaled):
                    continue

                # 归一化坐标
                norm_x, norm_y = normalize_coordinates(center_x, center_y, roi_scaled)

                # 颜色检测
                color_name, _ = detect_color_in_box(img, x1, y1, x2, y2)

                # 获取形状
                shape_name = detector.labels[obj.class_id]

                # 映射到协议ID
                shape_id = SHAPE_MAP.get(shape_name, proto_module.Protocol.SHAPE_IRREGULAR)
                color_id = COLOR_MAP.get(color_name, 0)

                if color_id == 0:
                    continue  # 颜色未识别，跳过

                # 模型检测得到的ROI归一化坐标 -> 实际机械坐标 (0.1mm单位)
                x_mm, y_mm = coordinate_converter.normalized_to_mechanical(
                    norm_x, norm_y)

                angle = 0  # 暂时不支持角度检测

                pieces.append((shape_id, color_id, x_mm, y_mm, angle))

                # 在图上标记
                img.draw_rect(obj.x, obj.y, obj.w, obj.h, color=image.COLOR_RED, thickness=2)
                img.draw_string(int(obj.x), int(obj.y),
                               f"{shape_name} {color_name}",
                               color=image.COLOR_RED, scale=0.8)

            # 发送扫描结果
            if pieces:
                frames, _ = proto.encode_scan_result(1, pieces, scan_req_seq)
                for frame in frames:
                    serial.write(frame)
                    time.sleep_ms(50)  # 批次间延迟
                print(f"发送扫描结果: {len(pieces)}件物料")
            else:
                # 无物料，发送空结果
                frames, _ = proto.encode_scan_result(1, [], scan_req_seq)
                serial.write(frames[0])
                print("发送扫描结果: 无物料")

            current_state = proto_module.Protocol.STATE_READY

        # 处理PICK_REPORT显示队列
        if pick_report_queue:
            current_state = proto_module.Protocol.STATE_DISPLAYING

            req_seq, seq_no, shape, color, res, total_ok = pick_report_queue.pop(0)

            # 显示四要素
            shape_name = proto_module.SHAPE_NAMES.get(shape, "未知")
            color_name = proto_module.COLOR_NAMES.get(color, "未知")
            result_text = "成功" if res == proto_module.Protocol.R_OK else "失败"

            img.draw_string(10, 50, f"序号: {seq_no}", color=image.COLOR_WHITE, scale=1.5)
            img.draw_string(10, 75, f"{shape_name} {color_name}", color=image.COLOR_WHITE, scale=1.5)
            img.draw_string(10, 100, f"结果: {result_text}", color=image.COLOR_WHITE, scale=1.5)
            img.draw_string(10, 125, f"累计: {total_ok}", color=image.COLOR_WHITE, scale=1.5)

            disp.show(img)
            time.sleep_ms(500)  # 显示500ms

            # 发送0xC4应答
            ack_frame, _ = proto.encode_ack(
                proto_module.Protocol.CMD_ACK_PICK_REPORT,
                proto_module.Protocol.R_OK,
                req_seq
            )
            serial.write(ack_frame)
            print(f"已显示并应答PICK_REPORT #{seq_no}")

            current_state = proto_module.Protocol.STATE_READY
        else:
            # 正常显示
            disp.show(img)

        time.sleep_ms(30)

finally:
    led.value(0)
    print("程序退出")
