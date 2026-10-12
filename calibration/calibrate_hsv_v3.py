"""
HSV颜色阈值标定工具 - V3版（中心点采样）

特点：
1. 画面中心显示十字准星
2. 将色块放在画面中心，按G键一键采样HSV值
3. 自动设置该颜色的阈值范围
4. 在此基础上微调

串口命令（单个字符）：
颜色选择：
  '1' - 蓝色   '2' - 黄色   '3' - 红色   '4' - 白色

一键采样：
  'G' - 采样中心区域HSV，自动设置阈值范围

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
  'I' - 切换显示中心区域HSV值
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

# 中心采样区域大小
CENTER_SAMPLE_SIZE = 20  # 中心20x20像素区域

# 颜色阈值
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
sample_step = 3
show_center_hsv = True  # 默认显示中心HSV值

print("HSV阈值标定工具 V3（中心点采样）")
print("1.选颜色(1234) 2.放色块到中心 3.按G采样 4.微调(QWAS/ERDF/ZXCV) 5.保存(P)")


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


def sample_center_region(img):
    """采样中心区域的HSV值"""
    center_x = CAMERA_WIDTH // 2
    center_y = CAMERA_HEIGHT // 2
    half_size = CENTER_SAMPLE_SIZE // 2

    # 采样中心区域
    samples = []
    for y in range(center_y - half_size, center_y + half_size, 2):
        for x in range(center_x - half_size, center_x + half_size, 2):
            if 0 <= x < CAMERA_WIDTH and 0 <= y < CAMERA_HEIGHT:
                r, g, b = _get_rgb_pixel(img, x, y)
                samples.append([r, g, b])

    if len(samples) == 0:
        return None

    # 转换为HSV
    rgb_array = np.array(samples, dtype=np.uint8).reshape(-1, 1, 3)
    hsv_array = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2HSV)

    # 计算统计值
    h_values = hsv_array[:, 0, 0]
    s_values = hsv_array[:, 0, 1]
    v_values = hsv_array[:, 0, 2]

    h_mean = int(np.mean(h_values))
    s_mean = int(np.mean(s_values))
    v_mean = int(np.mean(v_values))
    h_std = int(np.std(h_values))
    s_std = int(np.std(s_values))
    v_std = int(np.std(v_values))

    return {
        "h": (h_mean, h_std, int(np.min(h_values)), int(np.max(h_values))),
        "s": (s_mean, s_std, int(np.min(s_values)), int(np.max(s_values))),
        "v": (v_mean, v_std, int(np.min(v_values)), int(np.max(v_values)))
    }


def auto_set_threshold_from_sample(stats, color_name):
    """根据采样的HSV统计自动设置阈值"""
    h_mean, h_std, h_min, h_max = stats["h"]
    s_mean, s_std, s_min, s_max = stats["s"]
    v_mean, v_std, v_min, v_max = stats["v"]

    # 设置阈值范围（均值 ± 一定余量）
    h_tolerance = max(15, h_std * 2)
    s_tolerance = max(30, s_std * 2)
    v_tolerance = max(30, v_std * 2)

    thresh = thresholds[color_name]
    thresh["lower"][0] = max(0, h_mean - h_tolerance)
    thresh["upper"][0] = min(179, h_mean + h_tolerance)
    thresh["lower"][1] = max(0, s_mean - s_tolerance)
    thresh["upper"][1] = min(255, s_mean + s_tolerance)
    thresh["lower"][2] = max(0, v_mean - v_tolerance)
    thresh["upper"][2] = min(255, v_mean + v_tolerance)

    print(f"✓ 已设置{color_name}阈值:")
    print(f"  H: {thresh['lower'][0]}-{thresh['upper'][0]} (中心={h_mean})")
    print(f"  S: {thresh['lower'][1]}-{thresh['upper'][1]} (中心={s_mean})")
    print(f"  V: {thresh['lower'][2]}-{thresh['upper'][2]} (中心={v_mean})")


def create_mask(hsv_img, thresh):
    """创建掩码"""
    lower = np.array(thresh["lower"], dtype=np.uint8)
    upper = np.array(thresh["upper"], dtype=np.uint8)
    return cv2.inRange(hsv_img, lower, upper)


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

try:
    while not app.need_exit() and running:
        # 读取图像
        img = cam.read()

        # ====================================================================
        # 步骤1： 在任何绘制之前完成采样与掩码计算
        # ====================================================================
        # 说明：采样窗口位于画面正中，而分割线(x=160)、采样框边框、右侧白色
        #      掩码都与它有重叠。若先画后采，采到的样本里混着界面颜色，
        #      阈值会被越标越偏。所以采样必须用未经绘制的原始帧。
        center_x = CAMERA_WIDTH // 2
        center_y = CAMERA_HEIGHT // 2
        half_size = CENTER_SAMPLE_SIZE // 2

        # 1.1 中心区 HSV 采样（原始帧）
        center_stats = sample_center_region(img)

        # 1.2 掩码与覆盖率（原始帧）
        rgb_array = sample_image_to_numpy(img)
        hsv_array = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2HSV)
        thresh = thresholds[current_color]
        mask = create_mask(hsv_array, thresh)
        coverage = cv2.countNonZero(mask) / (mask.shape[0] * mask.shape[1]) * 100

        # ====================================================================
        # 步骤2： 绘制界面
        # ====================================================================

        # 2.1 十字准星
        img.draw_line(center_x - 30, center_y, center_x - half_size, center_y,
                     color=image.COLOR_GREEN, thickness=2)
        img.draw_line(center_x + half_size, center_y, center_x + 30, center_y,
                     color=image.COLOR_GREEN, thickness=2)
        img.draw_line(center_x, center_y - 30, center_x, center_y - half_size,
                     color=image.COLOR_GREEN, thickness=2)
        img.draw_line(center_x, center_y + half_size, center_x, center_y + 30,
                     color=image.COLOR_GREEN, thickness=2)

        # 2.2 采样区域框
        img.draw_rect(center_x - half_size, center_y - half_size,
                     CENTER_SAMPLE_SIZE, CENTER_SAMPLE_SIZE,
                     color=image.COLOR_GREEN, thickness=2)

        # 2.3 绘制分割线（左侧原图，右侧掩码）
        split_x = CAMERA_WIDTH // 2
        img.draw_line(split_x, 0, split_x, CAMERA_HEIGHT, color=image.COLOR_YELLOW, thickness=1)

        # 2.4 在右侧绘制掩码
        mask_small = cv2.resize(mask, (split_x, CAMERA_HEIGHT), interpolation=cv2.INTER_NEAREST)
        for y in range(CAMERA_HEIGHT):
            for x in range(split_x):
                if mask_small[y, x] > 0:
                    img.draw_rect(split_x + x, y, 1, 1, color=image.COLOR_WHITE, thickness=-1)

        # 2.5 显示信息
        img.draw_string(5, 5, f"{current_color.upper()}",
                       color=thresholds[current_color]["color"], scale=1.5)
        img.draw_string(5, 30, f"Cov:{coverage:.0f}%", color=image.COLOR_GREEN, scale=1.2)

        # 2.6 显示阈值
        img.draw_string(5, 50, f"L:{thresh['lower'][0]:3d},{thresh['lower'][1]:3d},{thresh['lower'][2]:3d}",
                       color=image.COLOR_YELLOW, scale=0.8)
        img.draw_string(5, 65, f"U:{thresh['upper'][0]:3d},{thresh['upper'][1]:3d},{thresh['upper'][2]:3d}",
                       color=image.COLOR_YELLOW, scale=0.8)

        # 2.7 显示中心区域HSV值（用步骤1 采到的值）
        if show_center_hsv and center_stats:
            img.draw_string(5, 85, f"H:{center_stats['h'][0]:3d}",
                           color=image.COLOR_WHITE, scale=0.8)
            img.draw_string(5, 100, f"S:{center_stats['s'][0]:3d}",
                           color=image.COLOR_WHITE, scale=0.8)
            img.draw_string(5, 115, f"V:{center_stats['v'][0]:3d}",
                           color=image.COLOR_WHITE, scale=0.8)

        img.draw_string(5, 240, "Press G to sample", color=image.COLOR_WHITE, scale=0.7)

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

            # 一键采样
            # 用步骤1 在原始帧上采到的结果，不能在这里重新采样 —
            # 此时 img 已经画满界面，采到的会混进黄线/白掩码/绿框
            elif cmd == 'G':
                if center_stats:
                    auto_set_threshold_from_sample(center_stats, current_color)
                else:
                    print("采样失败")

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
                show_center_hsv = not show_center_hsv
                print("中心HSV显示:" + ("开" if show_center_hsv else "关"))
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
