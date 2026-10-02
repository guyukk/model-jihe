"""
配置文件 - 所有可调参数集中管理
"""

# ==================== 硬件配置 ====================
UART_PORT = "/dev/ttyS4"
UART_BAUDRATE = 115200

MODEL_PATH = "/root/models/model-jihe/model_8988.mud"

# LED GPIO
LED_PIN_NAME = "B25"  # maixcam2
LED_GPIO_NAME = "GPIOB25"

# ==================== ROI配置 ====================
# ROI标定基准分辨率
ROI_CALIBRATION_WIDTH = 320
ROI_CALIBRATION_HEIGHT = 256

# ROI区域（基于标定分辨率）
ROI_X = 70
ROI_Y = 50
ROI_WIDTH = 170
ROI_HEIGHT = 165

# ==================== HSV颜色阈值 ====================
# 蓝色
LOWER_BLUE = [0, 132, 97]
UPPER_BLUE = [93, 255, 255]

# 黄色
LOWER_YELLOW = [52, 139, 185]
UPPER_YELLOW = [112, 255, 255]

# 红色
LOWER_RED = [40, 173, 191]
UPPER_RED = [156, 255, 255]

# 白色
LOWER_WHITE = [0, 0, 173]
UPPER_WHITE = [179, 60, 255]

# 颜色检测参数
COLOR_MIN_RATIO = 0.05  # 最小颜色占比
PIXEL_SAMPLE_STEP = 2   # 像素采样步长
COLOR_BOX_SHRINK = 0.6  # 颜色采样收缩到bbox中心区域比例(0~1)，减少边缘背景干扰

# 红色Hue环绕区间（可选）：OpenCV HSV中红色跨0点(H~0-15 和 H~170-179)
# 设置后红色掩码 = 范围1 OR 范围2，不需要时保持为 None
LOWER_RED2 = None   # 例如 [170, 173, 191]
UPPER_RED2 = None   # 例如 [179, 255, 255]

# ==================== YOLO检测参数 ====================
YOLO_CONF_THRESHOLD = 0.5
YOLO_IOU_THRESHOLD = 0.45

# ==================== 坐标转换配置 ====================
# ROI坐标映射范围（两个轴均为160mm，协议单位0.1mm）
COORD_SCALE_FACTOR = 1600
# ROI局部坐标的中点，以及该中点在机械坐标系中的标定位置
COORD_ROI_CENTER = 800
COORD_MECHANICAL_CENTER_X = 600
COORD_MECHANICAL_CENTER_Y = 1400

# ==================== 协议配置 ====================
HEARTBEAT_INTERVAL = 0.25  # 心跳间隔(秒)
LINK_TIMEOUT = 1.5         # 链路超时(秒)
DISPLAY_DURATION = 500     # 四要素显示时长(毫秒)

# ==================== 形状映射 ====================
# 模型输出 -> 协议形状ID
SHAPE_MAPPING = {
    "cube": 0x02,      # 方形
    "cone": 0x01,      # 圆形
    "pyramid": 0x03    # 三角形
}

# ==================== 颜色映射 ====================
# HSV检测结果 -> 协议颜色ID
COLOR_MAPPING = {
    "red": 0x01,
    "yellow": 0x03,
    "blue": 0x05,
    "white": 0x08
}

# ==================== 显示配置 ====================
SHOW_FPS = False
SHOW_DEBUG_INFO = True

# ==================== 扫描稳定性配置 ====================
# 扫描前准备：解决"首轮扫描漏检，后续正常"问题
# SCAN_REQ到达时托盘可能刚停稳，且摄像头缓冲区可能积压运动过程中的旧帧
SCAN_SETTLE_MS = 300        # 扫描前等待物料稳定的时间(毫秒)
SCAN_FLUSH_FRAMES = 3       # 采样前丢弃的缓冲旧帧数量

# 多帧投票检测：连续采多帧，跨帧合并，过滤偶发误检/漏检，提高稳定性
SCAN_MULTI_FRAME = True     # True=多帧投票, False=单帧检测
SCAN_FRAME_COUNT = 5        # 每次扫描采样帧数
SCAN_MIN_HITS = 4           # 目标至少在几帧中出现才上报
SCAN_MATCH_DIST = 20        # 跨帧匹配的最大中心距离(像素)
SCAN_FRAME_DELAY_MS = 50    # 帧间延迟(毫秒)

# ==================== 调试日志配置 ====================
# 扫描流程详细日志（排查漏检时开启）
SCAN_DEBUG_LOG = True
# 是否打印每个检测框的各颜色占比详情
SCAN_DEBUG_COLOR_DETAIL = True
