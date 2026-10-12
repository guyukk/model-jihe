"""
屏幕交互按键模块 - MaixCAM2 触摸按键与扫描结果面板

职责边界：
1) 只做触摸采集、命中判定、按键绘制、结果面板绘制
2) 不碰业务：main.py 调用 poll() 拿到按键标识，自行决定做什么

坐标系：
按键表用画面坐标系（与 camera 分辨率一致），触摸事件用屏幕坐标系。
两者在初始化时用 image.resize_map_pos 一次性换算，运行期只做屏幕坐标比较。
"""
import logging
import math
import time as pytime

from maix import image, touchscreen, err

logger = logging.getLogger("screen_ui")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


# ==================== 页面 ====================
PAGE_MAIN = "main"
PAGE_RESULT = "result"

# ==================== 按键布局（画面坐标） ====================
# 按键条压在 ROI 下方空闲带。默认 ROI (80,50,160,155) 下边界为 205，
# 因此 bar 从 212 开始，保证不遮挡任何检测区域。
BAR_Y = 216
BAR_H = 36
BUTTON_W = 77
BUTTON_START_X = 1
BUTTON_STEP_X = 80          # 77 宽 + 3 间隔
TOUCH_PAD_Y = 8             # 命中区上下外扩，补偿按键偏矮（屏上约 8mm）

# 按键表：页面 -> [(按键标识, 起始X), ...]
BUTTONS = {
    PAGE_MAIN: [
        ("result", 1),
        ("exit", 161),
    ],
    PAGE_RESULT: [
        ("back",   BUTTON_START_X + 0 * BUTTON_STEP_X),
        ("prev",   BUTTON_START_X + 1 * BUTTON_STEP_X),
        ("next",   BUTTON_START_X + 2 * BUTTON_STEP_X),
        ("exit",   BUTTON_START_X + 3 * BUTTON_STEP_X),
    ],
}

# 按键标签统一用 ASCII：MaixPy 默认字体只保证 ASCII 可渲染，
# 中文字形依赖设备自带 CJK 字体（需 image.load_font + set_default_font）。
LABELS = {
    "result": "Details",
    "exit": "Hold: Exit",
    "back": "BACK",
    "prev": "PREV",
    "next": "NEXT",
}
LABELS_ZH = dict(result="物料详情", exit="长按退出", back="返回", prev="上页", next="下页")
SHAPES_EN = {1: "Cone", 2: "Cube", 3: "Pyramid", 4: "Cuboid", 5: "Hex prism", 6: "Irregular"}
COLORS_EN = {1: "Red", 2: "Orange", 3: "Yellow", 4: "Green", 5: "Blue", 6: "Cyan", 7: "Purple", 8: "White"}
SHAPES_ZH = {1: "圆锥", 2: "立方体", 3: "棱锥", 4: "长方体", 5: "六棱柱", 6: "不规则"}
COLORS_ZH = {1: "红色", 2: "橙色", 3: "黄色", 4: "绿色", 5: "蓝色", 6: "青色", 7: "紫色", 8: "白色"}


def load_chinese_font(path):
    try:
        err.check_raise(image.load_font("vision_ui", path, size=16), "load UI font")
        err.check_raise(image.set_default_font("vision_ui"), "select UI font")
        return True
    except Exception as exc:
        logger.warning("中文字体不可用，切换完整英文界面: %s", exc)
        try:
            image.set_default_font("hershey_plain")
        except Exception:
            pass
        return False


def names(shape, color, chinese=False):
    return ((COLORS_ZH if chinese else COLORS_EN).get(color, "未知" if chinese else "Unknown"),
            (SHAPES_ZH if chinese else SHAPES_EN).get(shape, "未知" if chinese else "Unknown"))


def fit_text(img, x, y, text, width, color, scale=1.0):
    """按可用宽度缩小文字，避免中文名称/状态覆盖相邻区域。"""
    w, _h = _text_size(text)
    if w:
        scale = min(scale, width / w)
    img.draw_string(x, y, text, color=color, scale=scale)

# 长按触发的按键（毫秒）。未列出的按键松手即触发。
LONG_PRESS_MS = {"exit": 1500}

CLICK_COOLDOWN_S = 0.25     # 一次点击后的冷却，防手抖连击

# ==================== 结果面板布局（画面坐标） ====================
PANEL_TITLE_Y = 4
PANEL_TITLE_SCALE = 0.8
PANEL_ROW_Y = 46
PANEL_ROW_H = 20
PANEL_ROWS = 8
PANEL_ROW_SCALE = 0.8


def _text_size(text):
    """兼容 string_size 的不同返回形态（对象或元组）"""
    size = image.string_size(text)
    if hasattr(size, "width"):
        return int(size.width()), int(size.height())
    return int(size[0]), int(size[1])


class ScreenUI:
    """触摸按键与结果面板"""

    def __init__(self, disp, img_width, img_height, page=PAGE_MAIN, chinese=False):
        self.disp = disp
        self.img_width = img_width
        self.img_height = img_height
        self.page = page
        self.chinese = chinese

        # 步骤1： 初始化触摸屏
        # 说明：
        # 1) TouchScreen 与 Display 无耦合，可独立创建
        # 2) 首次 read 在部分固件上返回 (0,0,False)，属正常现象
        logger.info("开始初始化触摸按键: 画面=%dx%d", img_width, img_height)

        # 1.1 打开触摸设备
        self.ts = touchscreen.TouchScreen()
        logger.info("触摸屏初始化完成")

        # 步骤2： 预换算按键命中区
        # 目标：把画面坐标的按键矩形映射为屏幕坐标，运行期不再重复计算
        # 操作：
        # 1) 遍历所有页面的按键表
        # 2) 命中区上下外扩 TOUCH_PAD_Y 像素
        logger.info("开始换算按键命中区...")

        # 2.1 逐页逐键映射
        self.hit_areas = {}
        for page_name, buttons in BUTTONS.items():
            for key, bx in buttons:
                rect = image.resize_map_pos(
                    img_width, img_height,
                    disp.width(), disp.height(),
                    image.Fit.FIT_CONTAIN,
                    bx, BAR_Y - TOUCH_PAD_Y,
                    self.button_width(page_name), BAR_H + TOUCH_PAD_Y * 2)
                self.hit_areas[(page_name, key)] = (
                    rect[0], rect[1], rect[2], rect[3])
        logger.info("按键命中区换算完成, 共%d个按键", len(self.hit_areas))

        # 步骤3： 触摸状态机初始化
        # 说明：
        # 1) pressed 是电平信号，按住会连续为真，必须做边沿检测
        # 2) 长按键在达到阈值时立即触发，不等松手，便于显示进度
        self.pressing = False
        self.press_key = None
        self.press_start = 0.0
        self.long_fired = False
        self.last_click_time = -float("inf")

        logger.info("屏幕按键模块就绪, 初始页面=%s", page)

    # ------------------------------------------------------------------
    # 触摸
    # ------------------------------------------------------------------

    def _hit(self, x, y):
        """屏幕坐标命中判定，返回当前页面的按键标识或 None"""
        for key, _bx in BUTTONS[self.page]:
            rx, ry, rw, rh = self.hit_areas[(self.page, key)]
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return key
        return None

    @staticmethod
    def button_width(page):
        return 157 if page == PAGE_MAIN else BUTTON_W

    def hold_ratio(self):
        """长按键的按住进度，0.0~1.0，用于画进度条"""
        if not self.pressing or self.press_key not in LONG_PRESS_MS:
            return 0.0
        held_ms = (pytime.monotonic() - self.press_start) * 1000.0
        return max(0.0, min(1.0, held_ms / LONG_PRESS_MS[self.press_key]))

    def poll(self):
        """轮询触摸，返回一次点击对应的按键标识，无事件返回 None

        事件规则：
        1) 短按键：松手时触发，且松手前的按下点必须落在按键内
        2) 长按键（LONG_PRESS_MS 里的键）：按住达到阈值时触发一次，
           松手不再触发；**短按松手一律不触发**
        3) 触发后 CLICK_COOLDOWN_S 内忽略后续点击
        """
        x, y, pressed = self.ts.read()
        now = pytime.monotonic()

        if pressed:
            # 3.1 新一轮按下：记录起点并判命中
            if not self.pressing:
                self.pressing = True
                self.press_start = now
                self.press_key = self._hit(x, y)
                self.long_fired = False
            elif self._hit(x, y) != self.press_key:
                # 移出后整次按压作废；移回也不能继承已累计的长按时间。
                self.press_key = None

            # 3.2 长按达到阈值即刻触发
            if (not self.long_fired
                    and self.press_key in LONG_PRESS_MS
                    and (now - self.press_start) * 1000.0
                    >= LONG_PRESS_MS[self.press_key]):
                self.long_fired = True
                logger.info("长按触发: %s", self.press_key)
                return self.press_key
            return None

        # 3.3 未按下：无进行中的按压则直接返回
        if not self.pressing:
            return None

        self.pressing = False
        key = self.press_key
        self.press_key = None

        if self.long_fired:
            self.long_fired = False
            return None
        if key is None:
            return None

        # 3.4 长按键只认长按：短按松手一律不触发
        # 否则轻碰一下 EXIT 就会退出应用，而应用退出会触发媒体框架交接，
        # 实测交接失败会让 ISP 进入坏状态（每秒刷 os04d10 越界）直至整机复位。
        if key in LONG_PRESS_MS:
            return None

        if now - self.last_click_time < CLICK_COOLDOWN_S:
            return None

        self.last_click_time = now
        logger.info("点击触发: %s", key)
        return key

    def reset_press(self):
        """丢弃进行中的按压状态，切换页面时调用"""
        self.pressing = False
        self.press_key = None
        self.long_fired = False

    def set_page(self, page):
        """切换页面"""
        if page not in BUTTONS:
            raise ValueError("未知页面: {}".format(page))
        self.page = page
        self.reset_press()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def draw_buttons(self, img, page=None):
        """绘制当前页面的按键条"""
        page = self.page if page is None else page
        pressed = self.press_key if self.pressing else None

        for key, bx in BUTTONS[page]:
            width = self.button_width(page)
            bg_color = image.COLOR_GREEN if key == pressed else image.COLOR_BLACK
            img.draw_rect(bx, BAR_Y, width, BAR_H,
                          color=bg_color, thickness=-1)
            img.draw_rect(bx, BAR_Y, width, BAR_H,
                          color=image.COLOR_WHITE, thickness=1)

            text = (LABELS_ZH if self.chinese else LABELS)[key]
            tw, th = _text_size(text)
            scale = min(1.0, (width - 8) / max(1, tw))
            img.draw_string(bx + int((width - tw * scale) / 2),
                            BAR_Y + int((BAR_H - th * scale) / 2),
                            text, color=image.COLOR_WHITE, scale=scale)

            # 长按键显示按住进度
            if key == pressed and key in LONG_PRESS_MS:
                bar_w = int((width - 4) * self.hold_ratio())
                if bar_w > 0:
                    img.draw_rect(bx + 2, BAR_Y + BAR_H - 6, bar_w, 4,
                                  color=image.COLOR_RED, thickness=-1)

    def draw_result_panel(self, img, title, lines, page, empty_message=None, status="", status_error=False, history=""):
        """绘制扫描结果面板，返回 (实际页号, 总页数)

        lines: 每行一个字符串，行内容由调用方决定
        """
        total_pages = max(1, math.ceil(len(lines) / PANEL_ROWS)) if lines else 1
        page = max(0, min(page, total_pages - 1))

        img.draw_rect(0, 0, self.img_width, self.img_height,
                      color=image.COLOR_BLACK, thickness=-1)
        fit_text(img, 4, PANEL_TITLE_Y, title, self.img_width - 68,
                 image.COLOR_YELLOW, PANEL_TITLE_SCALE)
        fit_text(img, 4, 25, status, self.img_width - 8,
                 image.COLOR_RED if status_error else image.COLOR_GREEN, 0.85)
        fit_text(img, 4, 205, history, self.img_width - 8, image.COLOR_WHITE, 0.6)

        if not lines:
            fit_text(img, 4, PANEL_ROW_Y + 20, empty_message or
                     ("尚未完成首次识别" if self.chinese else "Waiting for first recognition"),
                     self.img_width - 8, image.COLOR_WHITE)
            return page, total_pages

        start = page * PANEL_ROWS
        for i, text in enumerate(lines[start:start + PANEL_ROWS]):
            color = image.COLOR_RED if ("Excluded:" in text or "未上报:" in text) else image.COLOR_WHITE
            fit_text(img, 4, PANEL_ROW_Y + i * PANEL_ROW_H, text,
                     self.img_width - 8, color, PANEL_ROW_SCALE)

        if total_pages > 1:
            img.draw_string(self.img_width - 56, PANEL_TITLE_Y,
                            "{}/{}".format(page + 1, total_pages),
                            color=image.COLOR_GREEN, scale=PANEL_TITLE_SCALE)
        return page, total_pages
