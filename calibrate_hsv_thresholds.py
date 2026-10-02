"""
HSV颜色阈值标定工具

通过串口命令实时调整HSV阈值，观察颜色掩码效果：
- '1': 选择蓝色进行标定
- '2': 选择黄色进行标定
- '3': 选择红色进行标定
- '4': 选择白色进行标定
- 'H+/H-': 调整色调(Hue)上下限
- 'S+/S-': 调整饱和度(Saturation)上下限
- 'V+/V-': 调整亮度(Value)上下限
- 'U': 切换调整上限/下限
- 'R': 重置当前颜色阈值
- 'D': 显示当前所有阈值
- 'S': 保存所有阈值到文件
- 'Q': 退出

标定流程：
1. 将待标定颜色的物体放在摄像头前
2. 通过串口发送'1'/'2'/'3'/'4'选择要标定的颜色
3. 调整HSV阈值，使掩码只显示该颜色（白色区域）
4. 重复步骤1-3标定其他颜色
5. 发送'S'保存阈值
"""
import cv2
import numpy as np
from maix import app, camera, display, image, time, uart, sys

# 初始化摄像头和显示器
CAMERA_WIDTH = 320
CAMERA_HEIGHT = 256
cam = camera.Camera(CAMERA_WIDTH, CAMERA_HEIGHT, image.Format.FMT_RGB888)
disp = display.Display()

# 初始化串口 UART4
serial = uart.UART("/dev/ttyS4", 115200)

# 颜色阈值（OpenCV HSV范围：H[0-179], S[0-255], V[0-255]）
thresholds = {
    "blue": {
        "lower": np.array([100, 100, 80], dtype=np.uint8),
        "upper": np.array([130, 255, 255], dtype=np.uint8)
    },
    "yellow": {
        "lower": np.array([15, 80, 80], dtype=np.uint8),
        "upper": np.array([35, 255, 255], dtype=np.uint8)
    },
    "red": {
        "lower": np.array([0, 100, 80], dtype=np.uint8),
        "upper": np.array([10, 255, 255], dtype=np.uint8),
        "lower2": np.array([170, 100, 80], dtype=np.uint8),
        "upper2": np.array([179, 255, 255], dtype=np.uint8)
    },
    "white": {
        "lower": np.array([0, 0, 180], dtype=np.uint8),
        "upper": np.array([179, 50, 255], dtype=np.uint8)
    }
}

# 当前标定的颜色
current_color = "blue"
adjust_upper = False  # True=调整上限，False=调整下限
step_h = 5
step_sv = 10

print("HSV颜色阈值标定工具启动")
print("串口命令: 1-4选择颜色, H±/S±/V±调整, U切换上下限, R重置, D显示, S保存, Q退出")


def _get_rgb_pixel(img, x, y):
    """读取MaixPy Image的RGB像素"""
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


def sample_image_to_numpy(img, step=2):
    """将MaixPy图像采样转为numpy数组"""
    sample_width = (CAMERA_WIDTH + step - 1) // step
    sample_height = (CAMERA_HEIGHT + step - 1) // step
    rgb_array = np.empty((sample_height, sample_width, 3), dtype=np.uint8)
    for sample_y, y in enumerate(range(0, CAMERA_HEIGHT, step)):
        for sample_x, x in enumerate(range(0, CAMERA_WIDTH, step)):
            rgb_array[sample_y, sample_x] = _get_rgb_pixel(img, x, y)
    return rgb_array


def create_red_mask(hsv_img, thresh):
    """红色需要两个H范围的掩码"""
    mask1 = cv2.inRange(hsv_img, thresh["lower"], thresh["upper"])
    if "lower2" in thresh:
        mask2 = cv2.inRange(hsv_img, thresh["lower2"], thresh["upper2"])
        return cv2.bitwise_or(mask1, mask2)
    return mask1


def apply_mask_to_image(rgb_array, mask):
    """将掩码应用到图像，返回只显示掩码区域的图像"""
    mask_3ch = cv2.cvtColor(mask, cv2.COLOR_GRAY2RGB)
    return cv2.bitwise_and(rgb_array, mask_3ch)


def print_threshold(color_name, thresh):
    """打印阈值"""
    print(f"{color_name}:")
    print(f"  lower: H={thresh['lower'][0]}, S={thresh['lower'][1]}, V={thresh['lower'][2]}")
    print(f"  upper: H={thresh['upper'][0]}, S={thresh['upper'][1]}, V={thresh['upper'][2]}")
    if "lower2" in thresh:
        print(f"  lower2: H={thresh['lower2'][0]}, S={thresh['lower2'][1]}, V={thresh['lower2'][2]}")
        print(f"  upper2: H={thresh['upper2'][0]}, S={thresh['upper2'][1]}, V={thresh['upper2'][2]}")


def save_thresholds():
    """保存阈值到Python代码格式"""
    with open('/root/hsv_thresholds.txt', 'w') as f:
        f.write("# HSV颜色阈值配置\n")
        f.write("# OpenCV HSV范围：H[0-179], S[0-255], V[0-255]\n\n")

        for color_name, thresh in thresholds.items():
            f.write(f"LOWER_{color_name.upper()} = np.array([{thresh['lower'][0]}, {thresh['lower'][1]}, {thresh['lower'][2]}], dtype=np.uint8)\n")
            f.write(f"UPPER_{color_name.upper()} = np.array([{thresh['upper'][0]}, {thresh['upper'][1]}, {thresh['upper'][2]}], dtype=np.uint8)\n")
            if "lower2" in thresh:
                f.write(f"LOWER_{color_name.upper()}2 = np.array([{thresh['lower2'][0]}, {thresh['lower2'][1]}, {thresh['lower2'][2]}], dtype=np.uint8)\n")
                f.write(f"UPPER_{color_name.upper()}2 = np.array([{thresh['upper2'][0]}, {thresh['upper2'][1]}, {thresh['upper2'][2]}], dtype=np.uint8)\n")
            f.write("\n")
    print("阈值已保存到 /root/hsv_thresholds.txt")


running = True

try:
    while not app.need_exit() and running:
        img = cam.read()

        # 采样并转换为numpy数组
        rgb_array = sample_image_to_numpy(img, step=2)
        hsv_array = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2HSV)

        # 创建当前颜色的掩码
        if current_color == "red":
            mask = create_red_mask(hsv_array, thresholds[current_color])
        else:
            mask = cv2.inRange(hsv_array,
                              thresholds[current_color]["lower"],
                              thresholds[current_color]["upper"])

        # 计算掩码覆盖率
        pixel_count = cv2.countNonZero(mask)
        total_pixels = mask.shape[0] * mask.shape[1]
        coverage = pixel_count / total_pixels * 100

        # 在原图上绘制信息
        info_text = f"{current_color.upper()} | {'UPPER' if adjust_upper else 'LOWER'}"
        img.draw_string(10, 10, info_text, color=image.COLOR_GREEN, scale=1.5)

        thresh = thresholds[current_color]
        target = "upper" if adjust_upper else "lower"
        hsv_text = f"H:{thresh[target][0]} S:{thresh[target][1]} V:{thresh[target][2]}"
        img.draw_string(10, 35, hsv_text, color=image.COLOR_YELLOW, scale=1.2)

        img.draw_string(10, 60, f"Coverage: {coverage:.1f}%",
                       color=image.COLOR_WHITE, scale=1.0)
        img.draw_string(10, 80, "Cmd: 1-4/H±/S±/V±/U/R/D/S/Q",
                       color=image.COLOR_WHITE, scale=0.8)

        disp.show(img)

        # 处理串口命令
        if serial.available() > 0:
            cmd = serial.read(1).decode('utf-8', errors='ignore').upper()

            # 选择颜色
            if cmd == '1':
                current_color = "blue"
                print(f"切换到蓝色标定")
                print_threshold(current_color, thresholds[current_color])
            elif cmd == '2':
                current_color = "yellow"
                print(f"切换到黄色标定")
                print_threshold(current_color, thresholds[current_color])
            elif cmd == '3':
                current_color = "red"
                print(f"切换到红色标定")
                print_threshold(current_color, thresholds[current_color])
            elif cmd == '4':
                current_color = "white"
                print(f"切换到白色标定")
                print_threshold(current_color, thresholds[current_color])

            # 切换上下限
            elif cmd == 'U':
                adjust_upper = not adjust_upper
                print(f"切换到调整{'上限' if adjust_upper else '下限'}")

            # 调整H
            elif cmd == 'H':
                # 读取下一个字符判断+/-
                if serial.available() > 0:
                    sign = serial.read(1).decode('utf-8', errors='ignore')
                    target = "upper" if adjust_upper else "lower"
                    if sign == '+':
                        thresholds[current_color][target][0] = min(179, thresholds[current_color][target][0] + step_h)
                    elif sign == '-':
                        thresholds[current_color][target][0] = max(0, thresholds[current_color][target][0] - step_h)
                    print(f"H = {thresholds[current_color][target][0]}")

            # 调整S
            elif cmd == 'S':
                if serial.available() > 0:
                    sign = serial.read(1).decode('utf-8', errors='ignore')
                    if sign in ['+', '-']:  # 只有S+/S-才是调整，单独S是保存
                        target = "upper" if adjust_upper else "lower"
                        if sign == '+':
                            thresholds[current_color][target][1] = min(255, thresholds[current_color][target][1] + step_sv)
                        elif sign == '-':
                            thresholds[current_color][target][1] = max(0, thresholds[current_color][target][1] - step_sv)
                        print(f"S = {thresholds[current_color][target][1]}")
                    else:
                        # 保存阈值
                        save_thresholds()
                        print_threshold(current_color, thresholds[current_color])

            # 调整V
            elif cmd == 'V':
                if serial.available() > 0:
                    sign = serial.read(1).decode('utf-8', errors='ignore')
                    target = "upper" if adjust_upper else "lower"
                    if sign == '+':
                        thresholds[current_color][target][2] = min(255, thresholds[current_color][target][2] + step_sv)
                    elif sign == '-':
                        thresholds[current_color][target][2] = max(0, thresholds[current_color][target][2] - step_sv)
                    print(f"V = {thresholds[current_color][target][2]}")

            # 重置当前颜色
            elif cmd == 'R':
                if current_color == "blue":
                    thresholds["blue"]["lower"] = np.array([100, 100, 80], dtype=np.uint8)
                    thresholds["blue"]["upper"] = np.array([130, 255, 255], dtype=np.uint8)
                elif current_color == "yellow":
                    thresholds["yellow"]["lower"] = np.array([15, 80, 80], dtype=np.uint8)
                    thresholds["yellow"]["upper"] = np.array([35, 255, 255], dtype=np.uint8)
                elif current_color == "red":
                    thresholds["red"]["lower"] = np.array([0, 100, 80], dtype=np.uint8)
                    thresholds["red"]["upper"] = np.array([10, 255, 255], dtype=np.uint8)
                elif current_color == "white":
                    thresholds["white"]["lower"] = np.array([0, 0, 180], dtype=np.uint8)
                    thresholds["white"]["upper"] = np.array([179, 50, 255], dtype=np.uint8)
                print(f"{current_color}已重置")
                print_threshold(current_color, thresholds[current_color])

            # 显示所有阈值
            elif cmd == 'D':
                print("\n=== 当前所有阈值 ===")
                for color_name, thresh in thresholds.items():
                    print_threshold(color_name, thresh)
                print("==================\n")

            # 退出
            elif cmd == 'Q':
                running = False
                print("准备退出...")

        time.sleep_ms(30)

except KeyboardInterrupt:
    print("\n标定已取消")
except Exception as e:
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()
finally:
    save_thresholds()
    print("\n最终阈值已保存")
    serial.close()
