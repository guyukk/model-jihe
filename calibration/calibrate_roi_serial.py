
"""
通过串口控制的ROI标定工具（320x256分辨率）

通过串口发送命令来调整ROI：
- 'U': 向上移动
- 'D': 向下移动
- 'L': 向左移动
- 'R': 向右移动
- 'W': 增加高度
- 'S': 减少高度
- 'A': 减少宽度
- 'E': 增加宽度
- 'Z': 重置为全屏
- 'Q': 保存并退出

标定完成后，矩形坐标将保存到 roi_config.json
"""
import json
from maix import app, camera, display, image, time, uart, sys

# 初始化摄像头和显示器
CAMERA_WIDTH = 320
CAMERA_HEIGHT = 256
cam = camera.Camera(CAMERA_WIDTH, CAMERA_HEIGHT, image.Format.FMT_RGB888)
disp = display.Display()

# 初始化串口 UART4
serial = uart.UART("/dev/ttyS4", 115200)

# 初始ROI设置（相对于320x256，居中的160x155区域）
roi = {
    "x": 80,
    "y": 50,
    "width": 160,
    "height": 155
}

# 移动和调整步长
MOVE_STEP = 10
SIZE_STEP = 10

print("ROI标定工具启动（串口控制）")
print("通过串口发送命令: U/D/L/R移动, W/S高度, A/D宽度, Z重置, Q保存退出")

running = True

try:
    while not app.need_exit() and running:
        img = cam.read()

        # 绘制ROI矩形
        img.draw_rect(
            roi["x"],
            roi["y"],
            roi["width"],
            roi["height"],
            color=image.COLOR_GREEN,
            thickness=3
        )

        # 绘制中心十字线
        center_x = roi["x"] + roi["width"] // 2
        center_y = roi["y"] + roi["height"] // 2
        img.draw_line(center_x - 10, center_y, center_x + 10, center_y,
                     color=image.COLOR_BLUE, thickness=2)
        img.draw_line(center_x, center_y - 10, center_x, center_y + 10,
                     color=image.COLOR_BLUE, thickness=2)

        # 显示ROI信息
        info_text = f"ROI: ({roi['x']},{roi['y']}) {roi['width']}x{roi['height']}"
        img.draw_string(10, 10, info_text, color=image.COLOR_GREEN, scale=1.5)
        img.draw_string(10, 35, "Send: U/D/L/R/W/S/A/D/Z/Q",
                       color=image.COLOR_WHITE, scale=1.0)

        disp.show(img)

        # 检查串口命令
        if serial.available() > 0:
            cmd = serial.read(1).decode('utf-8', errors='ignore').upper()

            if cmd == 'U':  # 向上移动
                roi["y"] = max(0, roi["y"] - MOVE_STEP)
                print(f"向上移动: y={roi['y']}")
            elif cmd == 'D':  # 向下移动（实际是向下，不是宽度）
                roi["y"] = min(CAMERA_HEIGHT - roi["height"], roi["y"] + MOVE_STEP)
                print(f"向下移动: y={roi['y']}")
            elif cmd == 'L':  # 向左移动
                roi["x"] = max(0, roi["x"] - MOVE_STEP)
                print(f"向左移动: x={roi['x']}")
            elif cmd == 'R':  # 向右移动
                roi["x"] = min(CAMERA_WIDTH - roi["width"], roi["x"] + MOVE_STEP)
                print(f"向右移动: x={roi['x']}")
            elif cmd == 'W':  # 增加高度
                if roi["y"] + roi["height"] + SIZE_STEP <= CAMERA_HEIGHT:
                    roi["height"] += SIZE_STEP
                    print(f"增加高度: height={roi['height']}")
            elif cmd == 'S':  # 减少高度
                roi["height"] = max(50, roi["height"] - SIZE_STEP)
                print(f"减少高度: height={roi['height']}")
            elif cmd == 'A':  # 减少宽度
                roi["width"] = max(50, roi["width"] - SIZE_STEP)
                print(f"减少宽度: width={roi['width']}")
            elif cmd == 'E':  # 增加宽度（E替代D避免冲突）
                if roi["x"] + roi["width"] + SIZE_STEP <= CAMERA_WIDTH:
                    roi["width"] += SIZE_STEP
                    print(f"增加宽度: width={roi['width']}")
            elif cmd == 'Z':  # 重置为全屏
                roi = {
                    "x": 0,
                    "y": 0,
                    "width": CAMERA_WIDTH,
                    "height": CAMERA_HEIGHT
                }
                print("重置为全屏")
            elif cmd == 'Q':  # 保存并退出
                running = False
                print("准备保存并退出...")

        time.sleep_ms(30)

except KeyboardInterrupt:
    print("\n标定已取消")
except Exception as e:
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()
finally:
    # 保存配置
    print(f"\n最终ROI配置:")
    print(f"  x: {roi['x']}")
    print(f"  y: {roi['y']}")
    print(f"  width: {roi['width']}")
    print(f"  height: {roi['height']}")

    with open('/root/roi_config.json', 'w') as f:
        json.dump(roi, f, indent=2)
    print("ROI配置已保存到 /root/roi_config.json")

    serial.close()
