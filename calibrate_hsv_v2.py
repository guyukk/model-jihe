"""
HSV颜色阈值标定工具 - 改进版

特点：
1. 画面分屏显示：左侧原图，右侧掩码效果（黑白）
2. 实时显示HSV统计信息（帮助判断调整方向）
3. 一键式快捷调整（单个按键完成操作）
4. 显示调整建议

串口命令（单个字符）：
颜色选择：
  '1' - 蓝色   '2' - 黄色   '3' - 红色   '4' - 白色

调整下限（LOWER）：
  'Q' - H下限-5    'W' - H下限+5
  'A' - S下限-10   'S' - S下限+10
  'Z' - V下限-10   'X' - V下限+10

调整上限（UPPER）：
  'E' - H上限-5    'R' - H上限+5
  'D' - S上限-10   'F' - S上限+10
  'C' - V上限-10   'V' - V上限+10

其他：
  'P' - 保存阈值   'O' - 退出程序
  'I' - 显示当前颜色的HSV统计信息
"""
import cv2
import numpy as np
from maix import app, camera, display, image, time, uart

# 初始化
CAMERA_WIDTH = 320
CAMERA_HEIGHT = 256
cam = camera.Camera(CAMERA_WIDTH, CAMERA_HEIGHT, image.Format.FMT_RGB888)
disp = display.Display()
serial = uart.UART("/dev/ttyS4", 115200)

# 颜色阈值（更宽松的初始值）
thresholds = {
    "blue": {
        "lower": [90, 80, 60],
        "upper": [130, 255, 255],
        "color": image.COLOR_BLUE
    },
    "yellow": {
        "lower": [10, 80, 60],
        "upper": [40, 255, 255],
        "color": image.COLOR_YELLOW
    },
    "red": {
        "lower": [0, 80, 60],
        "upper": [15, 255, 255],
        "color": image.COLOR_RED
    },
    "white": {
        "lower": [0, 0, 160],
        "upper": [179, 60, 255],
        "color": image.COLOR_WHITE
    }
}

current_color = "blue"
sample_step = 3  # 采样步长

print("HSV阈值标定工具（改进版）")
print("命令：1234选颜色 | QWAS调H/S下限 | ERDF调H/S上限 | ZXCV调V | P保存 O退出")


def _get_rgb_pixel(img, x, y):
    """读取RGB像素"""
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


def sample_image_to_numpy(img):
    """采样图像转numpy"""
    sw = (CAMERA_WIDTH + sample_step - 1) // sample_step
    sh = (CAMERA_HEIGHT + sample_step - 1) // sample_step
    rgb = np.empty((sh, sw, 3), dtype=np.uint8)
    for sy, y in enumerate(range(0, CAMERA_HEIGHT, sample_step)):
        for sx, x in enumerate(range(0, CAMERA_WIDTH, sample_step)):
            rgb[sy, sx] = _get_rgb_pixel(img, x, y)
    return rgb


def create_mask(hsv_img, thresh):
    """创建掩码"""
    lower = np.array(thresh["lower"], dtype=np.uint8)
    upper = np.array(thresh["upper"], dtype=np.uint8)
    return cv2.inRange(hsv_img, lower, upper)


def get_hsv_stats(hsv_img):
    """获取HSV统计信息"""
    h_mean = int(np.mean(hsv_img[:, :, 0]))
    s_mean = int(np.mean(hsv_img[:, :, 1]))
    v_mean = int(np.mean(hsv_img[:, :, 2]))
    h_min, h_max = int(np.min(hsv_img[:, :, 0])), int(np.max(hsv_img[:, :, 0]))
    s_min, s_max = int(np.min(hsv_img[:, :, 1])), int(np.max(hsv_img[:, :, 1]))
    v_min, v_max = int(np.min(hsv_img[:, :, 2])), int(np.max(hsv_img[:, :, 2]))
    return {
        "h": (h_mean, h_min, h_max),
        "s": (s_mean, s_min, s_max),
        "v": (v_mean, v_min, v_max)
    }


def draw_mask_on_image(img, mask, offset_x=0):
    """将掩码绘制到图像上（黑白显示）"""
    mask_h, mask_w = mask.shape
    for y in range(min(mask_h, CAMERA_HEIGHT)):
        for x in range(min(mask_w, CAMERA_WIDTH)):
            draw_x = offset_x + x
            if draw_x >= CAMERA_WIDTH:
                break
            color = image.COLOR_WHITE if mask[y, x] > 0 else image.COLOR_BLACK
            img.draw_rectangle(draw_x, y, 1, 1, color=color, fill=True)


def save_thresholds():
    """保存阈值"""
    with open('/root/hsv_thresholds.txt', 'w') as f:
        f.write("# HSV颜色阈值 - 复制到main.py\n\n")
        f.write("LOWER_BLUE1 = np.array([{}, {}, {}], dtype=np.uint8)\n".format(*thresholds["blue"]["lower"]))
        f.write("UPPER_BLUE1 = np.array([{}, {}, {}], dtype=np.uint8)\n".format(*thresholds["blue"]["upper"]))
        f.write("LOWER_YELLOW = np.array([{}, {}, {}], dtype=np.uint8)\n".format(*thresholds["yellow"]["lower"]))
        f.write("UPPER_YELLOW = np.array([{}, {}, {}], dtype=np.uint8)\n".format(*thresholds["yellow"]["upper"]))
        f.write("LOWER_RED = np.array([{}, {}, {}], dtype=np.uint8)\n".format(*thresholds["red"]["lower"]))
        f.write("UPPER_RED = np.array([{}, {}, {}], dtype=np.uint8)\n".format(*thresholds["red"]["upper"]))
        f.write("LOWER_WHITE = np.array([{}, {}, {}], dtype=np.uint8)\n".format(*thresholds["white"]["lower"]))
        f.write("UPPER_WHITE = np.array([{}, {}, {}], dtype=np.uint8)\n".format(*thresholds["white"]["upper"]))
    print("已保存到 /root/hsv_thresholds.txt")


running = True
show_stats = False

try:
    while not app.need_exit() and running:
        # 读取图像
        img = cam.read()

        # 采样并转换
        rgb_array = sample_image_to_numpy(img)
        hsv_array = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2HSV)

        # 创建掩码
        thresh = thresholds[current_color]
        mask = create_mask(hsv_array, thresh)

        # 计算覆盖率
        coverage = cv2.countNonZero(mask) / (mask.shape[0] * mask.shape[1]) * 100

        # 绘制分割线（左侧原图，右侧掩码）
        split_x = CAMERA_WIDTH // 2
        img.draw_line(split_x, 0, split_x, CAMERA_HEIGHT, color=image.COLOR_GREEN, thickness=2)

        # 在右侧绘制掩码（缩小到右半边）
        mask_small = cv2.resize(mask, (split_x, CAMERA_HEIGHT), interpolation=cv2.INTER_NEAREST)
        for y in range(CAMERA_HEIGHT):
            for x in range(split_x):
                if mask_small[y, x] > 0:
                    img.draw_rect(split_x + x, y, 1, 1, color=image.COLOR_WHITE, thickness=-1)
                else:
                    img.draw_rect(split_x + x, y, 1, 1, color=image.COLOR_BLACK, thickness=-1)

        # 显示信息
        img.draw_string(5, 5, f"{current_color.upper()}", color=thresholds[current_color]["color"], scale=1.5)
        img.draw_string(5, 30, f"Cov:{coverage:.0f}%", color=image.COLOR_GREEN, scale=1.2)

        # 显示阈值
        img.draw_string(5, 50, f"L: {thresh['lower'][0]:3d} {thresh['lower'][1]:3d} {thresh['lower'][2]:3d}",
                       color=image.COLOR_YELLOW, scale=0.8)
        img.draw_string(5, 65, f"U: {thresh['upper'][0]:3d} {thresh['upper'][1]:3d} {thresh['upper'][2]:3d}",
                       color=image.COLOR_YELLOW, scale=0.8)

        # 显示HSV统计（如果请求）
        if show_stats:
            stats = get_hsv_stats(hsv_array)
            cyan = getattr(image, "COLOR_CYAN", image.COLOR_WHITE)
            img.draw_string(5, 85, f"H:{stats['h'][1]}-{stats['h'][2]}", color=cyan, scale=0.7)
            img.draw_string(5, 100, f"S:{stats['s'][1]}-{stats['s'][2]}", color=cyan, scale=0.7)
            img.draw_string(5, 115, f"V:{stats['v'][1]}-{stats['v'][2]}", color=cyan, scale=0.7)

        disp.show(img)

        # 处理串口命令
        if serial.available() > 0:
            cmd = serial.read(1).decode('utf-8', errors='ignore').upper()

            # 选择颜色
            if cmd == '1':
                current_color = "blue"
                print(f"→ 蓝色")
            elif cmd == '2':
                current_color = "yellow"
                print(f"→ 黄色")
            elif cmd == '3':
                current_color = "red"
                print(f"→ 红色")
            elif cmd == '4':
                current_color = "white"
                print(f"→ 白色")

            # 调整下限
            elif cmd == 'Q':
                thresh["lower"][0] = max(0, thresh["lower"][0] - 5)
                print(f"H下限: {thresh['lower'][0]}")
            elif cmd == 'W':
                thresh["lower"][0] = min(179, thresh["lower"][0] + 5)
                print(f"H下限: {thresh['lower'][0]}")
            elif cmd == 'A':
                thresh["lower"][1] = max(0, thresh["lower"][1] - 10)
                print(f"S下限: {thresh['lower'][1]}")
            elif cmd == 'S':
                thresh["lower"][1] = min(255, thresh["lower"][1] + 10)
                print(f"S下限: {thresh['lower'][1]}")
            elif cmd == 'Z':
                thresh["lower"][2] = max(0, thresh["lower"][2] - 10)
                print(f"V下限: {thresh['lower'][2]}")
            elif cmd == 'X':
                thresh["lower"][2] = min(255, thresh["lower"][2] + 10)
                print(f"V下限: {thresh['lower'][2]}")

            # 调整上限
            elif cmd == 'E':
                thresh["upper"][0] = max(0, thresh["upper"][0] - 5)
                print(f"H上限: {thresh['upper'][0]}")
            elif cmd == 'R':
                thresh["upper"][0] = min(179, thresh["upper"][0] + 5)
                print(f"H上限: {thresh['upper'][0]}")
            elif cmd == 'D':
                thresh["upper"][1] = max(0, thresh["upper"][1] - 10)
                print(f"S上限: {thresh['upper'][1]}")
            elif cmd == 'F':
                thresh["upper"][1] = min(255, thresh["upper"][1] + 10)
                print(f"S上限: {thresh['upper'][1]}")
            elif cmd == 'C':
                thresh["upper"][2] = max(0, thresh["upper"][2] - 10)
                print(f"V上限: {thresh['upper'][2]}")
            elif cmd == 'V':
                thresh["upper"][2] = min(255, thresh["upper"][2] + 10)
                print(f"V上限: {thresh['upper'][2]}")

            # 其他功能
            elif cmd == 'I':
                show_stats = not show_stats
                print("统计信息:" + ("开" if show_stats else "关"))
            elif cmd == 'P':
                save_thresholds()
            elif cmd == 'O':
                running = False
                print("退出...")

        time.sleep_ms(30)

except KeyboardInterrupt:
    print("\n已取消")
except Exception as e:
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()
finally:
    save_thresholds()
    serial.close()
    print("完成")
