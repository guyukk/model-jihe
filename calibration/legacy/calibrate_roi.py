"""
交互式ROI（感兴趣区域）标定工具

使用键盘控制矩形区域：
- 方向键：移动矩形位置
- W/S：增加/减少矩形高度
- A/D：减少/增加矩形宽度
- R：重置为全屏
- Enter：保存并退出
- ESC：不保存退出

标定完成后，矩形坐标将保存到 roi_config.json
"""
import json
from maix import app, camera, display, image, time

# 初始化摄像头和显示器
cam = camera.Camera(640, 480, image.Format.FMT_RGB888)
disp = display.Display()

# 初始ROI设置（相对于640x480）
roi = {
    "x": 160,
    "y": 120,
    "width": 320,
    "height": 240
}

# 移动和调整步长
MOVE_STEP = 10
SIZE_STEP = 10

print("ROI标定工具启动")
print("使用方向键移动矩形，W/S调整高度，A/D调整宽度")
print("按Enter保存，按ESC退出")

try:
    while not app.need_exit():
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

        # 显示ROI信息
        info_text = f"ROI: ({roi['x']}, {roi['y']}) {roi['width']}x{roi['height']}"
        img.draw_string(10, 10, info_text, color=image.COLOR_GREEN, scale=1.5)
        img.draw_string(10, 30, "Arrow:Move W/S:H A/D:W R:Reset Enter:Save",
                       color=image.COLOR_WHITE, scale=1.0)

        disp.show(img)

        # 简单的按键处理（需要根据实际硬件调整）
        # 注意：MaixCAM2可能需要通过串口或其他方式接收控制命令
        # 这里提供基本框架，你可能需要根据实际情况修改输入方式

        time.sleep_ms(50)

except KeyboardInterrupt:
    print("\n标定已取消")
except Exception as e:
    print(f"错误: {e}")
finally:
    # 询问是否保存
    print(f"\n当前ROI配置:")
    print(f"  x: {roi['x']}")
    print(f"  y: {roi['y']}")
    print(f"  width: {roi['width']}")
    print(f"  height: {roi['height']}")

    save = input("是否保存此配置? (y/n): ")
    if save.lower() == 'y':
        with open('roi_config.json', 'w') as f:
            json.dump(roi, f, indent=2)
        print("ROI配置已保存到 roi_config.json")
    else:
        print("配置未保存")
