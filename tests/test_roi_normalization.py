"""
测试脚本：验证ROI过滤和坐标归一化功能

模拟不同位置的物体检测，验证：
1. ROI外的物体是否被正确过滤
2. ROI内的物体坐标是否正确归一化到0-1范围
"""
import json
import os

# 加载ROI配置
# 标定产物在 calibration/ 下；找不到就退回工程根目录，兼容旧位置
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CANDIDATES = [
    os.path.join(_ROOT, "calibration", "roi_config.json"),
    os.path.join(_ROOT, "roi_config.json"),
    "roi_config.json",
]
ROI_CONFIG_PATH = next((p for p in _CANDIDATES if os.path.exists(p)), _CANDIDATES[0])
with open(ROI_CONFIG_PATH, 'r') as f:
    roi = json.load(f)

print("ROI配置(%s):" % ROI_CONFIG_PATH)
print(f"  左上角: ({roi['x']}, {roi['y']})")
print(f"  右下角: ({roi['x'] + roi['width']}, {roi['y'] + roi['height']})")
print(f"  尺寸: {roi['width']} x {roi['height']}\n")


def is_in_roi(center_x, center_y, roi_config):
    """检查物体中心坐标是否在ROI矩形内"""
    x_in = roi_config["x"] <= center_x <= roi_config["x"] + roi_config["width"]
    y_in = roi_config["y"] <= center_y <= roi_config["y"] + roi_config["height"]
    return x_in and y_in


def normalize_coordinates(center_x, center_y, roi_config):
    """将物体坐标归一化到ROI坐标系（0-1范围）"""
    rel_x = center_x - roi_config["x"]
    rel_y = center_y - roi_config["y"]
    norm_x = rel_x / roi_config["width"]
    norm_y = rel_y / roi_config["height"]
    norm_x = max(0.0, min(1.0, norm_x))
    norm_y = max(0.0, min(1.0, norm_y))
    return norm_x, norm_y


# 测试用例
test_cases = [
    ("ROI左上角", 150, 100),
    ("ROI右下角", 470, 410),
    ("ROI中心", 310, 255),
    ("ROI左侧外", 100, 200),
    ("ROI右侧外", 500, 200),
    ("ROI上方外", 300, 50),
    ("ROI下方外", 300, 450),
    ("ROI内左上", 200, 150),
    ("ROI内右下", 420, 360),
]

print("测试结果:")
print("-" * 70)
for name, x, y in test_cases:
    in_roi = is_in_roi(x, y, roi)
    if in_roi:
        norm_x, norm_y = normalize_coordinates(x, y, roi)
        print(f"{name:12} ({x:3}, {y:3}) -> 在ROI内 -> 归一化: ({norm_x:.4f}, {norm_y:.4f})")
    else:
        print(f"{name:12} ({x:3}, {y:3}) -> 在ROI外 -> 已过滤")

print("-" * 70)
print("\n预期结果:")
print("- ROI左上角应归一化为 (0.0000, 0.0000)")
print("- ROI右下角应归一化为 (1.0000, 1.0000)")
print("- ROI中心应归一化为约 (0.5000, 0.5000)")
print("- ROI外的点应该被过滤")
