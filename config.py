"""设备端配置。长度单位见各项注释；修改后从桌面重启应用生效。"""
import os

# ==================== 硬件配置 ====================
UART_PORT = "/dev/ttyS4"
UART_BAUDRATE = 115200


def _resolve_model_path():
    """本地 .mud 及其引用权重齐全时使用本地文件，否则用设备预装模型。"""
    local_dir = os.path.dirname(os.path.abspath(__file__))
    local_mud = os.path.join(local_dir, "model_8988.mud")
    fallback = "/root/models/model-jihe/model_8988.mud"

    if not os.path.exists(local_mud):
        return fallback

    try:
        with open(local_mud, "r") as f:
            text = f.read()
        for key in ("model_npu", "model_vnpu"):
            for line in text.splitlines():
                line = line.strip()
                if line.startswith(key) and "=" in line:
                    name = line.split("=", 1)[1].strip()
                    if name and not os.path.exists(os.path.join(local_dir, name)):
                        return fallback        # 权重不在包内，退回设备目录
    except Exception:
        return fallback

    return local_mud


MODEL_PATH = _resolve_model_path()

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
ROI_CONFIG_PATH = "/root/roi_config.json"  # 存在时覆盖上面四项

# ==================== HSV颜色阈值 ====================
# auto 在有效四色基础标定文件存在时启用 LAB；hsv 强制沿用旧判色。
COLOR_MODE = "auto"
LAB_CONFIG_PATH = "/root/lab_colors.json"
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

# 颜色检测：先用上述标定掩码；覆盖率不足时仍给出最可能颜色并标记为估测。
# 此值是“可靠命中”门槛，不再是丢弃物料的门槛。
COLOR_MIN_RATIO = 0.05
PIXEL_SAMPLE_STEP = 2   # 像素采样步长
COLOR_BOX_SHRINK = 0.6  # 颜色采样收缩到bbox中心区域比例(0~1)，减少边缘背景干扰
# 所有掩码均为零时，根据 OpenCV HSV 色调选择红/黄/蓝；低饱和且足够亮判白。
# 色调在 0..179 上成环，红色 0 与 179 相邻。此结果属于估测，不保证正确。
COLOR_FALLBACK_HUES = {"red": 0, "yellow": 30, "blue": 120}
COLOR_FALLBACK_WHITE_MAX_S = 60
COLOR_FALLBACK_WHITE_MIN_V = 100

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
# LAB/HSV 检测结果 -> 协议颜色ID（协议 v3.1）
COLOR_MAPPING = {
    "red": 0x01,
    "orange": 0x02,
    "yellow": 0x03,
    "green": 0x04,
    "blue": 0x05,
    "purple": 0x06,
    "black": 0x07,
    "white": 0x08
}

# ==================== 屏幕交互配置 ====================
UI_FONT_PATH = "/maixapp/share/font/SourceHanSansCN-Regular.otf"
RESULT_STALE_SECONDS = 15.0

# ==================== 托盘快照（协议 v3.1） ====================
# v3.1 起 0x02 不再由 MCU 请求触发：视觉进程起来就每 SNAPSHOT_INTERVAL_MS
# 主动扫描托盘并推一帧整盘快照；0x43 SCAN_REQ 作废。
SNAPSHOT_INTERVAL_MS = 2000

# 两次快照「首字节」间隔的硬上限。MCU 连续 2500ms 收不到任何字节就判失联，
# 所以某一轮识别超过这个时限时必须重发上一帧（snap_seq 与内容都不变）：
# 宁可重复，也不能让线路静默超过窗口。
# MCU 的 2500ms 静默超时由对端检查；通信进程的发送调度见 link_service.py。
# 单帧最大件数（协议 §3.3）。超出时只发前 N 件，total 填实际发送条数
SNAPSHOT_MAX_PIECES = 12

# 坐标有效域（协议 §3.2 硬要求）：0 ≤ x ≤ 1800、0 ≤ y ≤ 2500，单位 0.1mm。
# 越界的件 MCU 不会下发电机，判为"这一件放弃"并上报 PICK_REPORT(R_PARAM)。
# 业务快照过滤越界件并记录统计，不钳位或伪造坐标。
SNAPSHOT_COORD_X_MIN = 0
SNAPSHOT_COORD_X_MAX = 1800
SNAPSHOT_COORD_Y_MIN = 0
SNAPSHOT_COORD_Y_MAX = 2500

# ==================== 启动提示 ====================
# AXERA 的媒体框架(VIN/VO/IVPS/POOL)、NPU 与 /dev/ttyS4 都是全局独占资源，
# 媒体资源冲突曾伴随 VO init failed、UART 构造异常和 ISP 错误日志；
# 这些现象尚不能单独证明整机重启的原因。
#
# 正常路径：从设备屏幕点开应用，launcher 会先释放媒体驱动再拉起本程序，
# 不会冲突。仅当从 SSH / MaixVision 直接启动、且 launcher 或其它应用还在
# 占着媒体框架时才会出问题。
#
# 这里只做提示，不阻止启动、也不结束其它进程：
# 实测从被抢占的进程内部去杀占用者会破坏 C++ 侧状态，反而触发
# _M_create / bad_alloc，所以让路必须在启动本程序之前、在外部完成。
WAIT_OTHER_APPS = True        # 是否列出仍在运行的其它 MaixCAM 应用

# ==================== 诊断日志 ====================
# 每 DIAG_LOG_INTERVAL 秒把运行现场追加到 DIAG_LOG_PATH。
# 每行都 flush，程序被杀死或整机重启后仍能读到最后的现场，
# 用于排查"连着 MCU 才卡死/重启"这类只能在现场复现的问题。
DIAG_LOG_ENABLED = True
DIAG_LOG_PATH = "/root/diag.log"
DIAG_LOG_INTERVAL = 2.0
DIAG_LOG_MAX_BYTES = 2 * 1024 * 1024
DIAG_LOG_BACKUPS = 3
STARTUP_LOG_PATH = "/root/startup.log"
LINK_LOG_PATH = "/root/link.log"

# ==================== 故障取证 ====================
# 冻结现场最有价值的信息是"主循环停在哪个调用"。冻结时进程通常还活着，
# 只是某个调用不返回，此时 kill -USR1 <pid> 就能把所有 Python 线程栈
# 写进 FAULT_DUMP_PATH，直接看到卡在哪一行，不需要额外调试工具。
# 同时 faulthandler.enable() 会在段错误等致命信号时自动转储。
FAULT_DUMP_ENABLED = True
FAULT_DUMP_PATH = "/root/fault_dump.txt"

# ==================== 扫描稳定性配置 ====================
# 扫描前准备：解决"首轮扫描漏检，后续正常"问题
# 采样前托盘可能刚运动，且摄像头缓冲区可能积压旧帧
SCAN_SETTLE_MS = 300        # 扫描前等待物料稳定的时间(毫秒)
SCAN_FLUSH_FRAMES = 3       # 采样前丢弃的缓冲旧帧数量

# 多帧投票检测：连续采多帧，跨帧合并，过滤偶发误检/漏检，提高稳定性
SCAN_MULTI_FRAME = True     # True=多帧投票, False=单帧检测
SCAN_FRAME_COUNT = 5        # 每次扫描采样帧数
SCAN_MIN_HITS = 3           # 目标至少在几帧中出现才上报
SCAN_MATCH_DIST = 20        # 跨帧匹配的最大中心距离(像素)
SCAN_FRAME_DELAY_MS = 50    # 帧间延迟(毫秒)

# ==================== 调试日志配置 ====================
# 扫描流程详细日志（排查漏检时开启）
SCAN_DEBUG_LOG = False
# 是否打印每个检测框的各颜色占比详情
SCAN_DEBUG_COLOR_DETAIL = False
