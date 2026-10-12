# -*- coding: utf-8 -*-
"""验证 EXIT 短按不再触发（离线，stub 掉 maix）"""
import os
import sys
import types

# 允许从 tests/ 子目录直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

maix = types.ModuleType("maix")
image = types.ModuleType("maix.image")
touchscreen = types.ModuleType("maix.touchscreen")


class Fit:
    FIT_CONTAIN = "contain"


image.Fit = Fit
for name in ("COLOR_BLACK", "COLOR_WHITE", "COLOR_GREEN", "COLOR_RED", "COLOR_YELLOW"):
    setattr(image, name, 0)


def resize_map_pos(iw, ih, dw, dh, fit, x, y, w, h):
    s = min(dw / iw, dh / ih)
    ox = (dw - iw * s) / 2
    oy = (dh - ih * s) / 2
    return [int(ox + x * s), int(oy + y * s), int(w * s), int(h * s)]


image.resize_map_pos = resize_map_pos
image.string_size = lambda t: (len(t) * 8, 12)
maix.image = image
maix.touchscreen = touchscreen
maix.err = types.SimpleNamespace(check_raise=lambda *a: None)
sys.modules["maix"] = maix
sys.modules["maix.image"] = image
sys.modules["maix.touchscreen"] = touchscreen


class FakeTS:
    point = (0, 0, False)

    def read(self):
        return self.point


touchscreen.TouchScreen = FakeTS


class FakeDisp:
    def width(self):
        return 640

    def height(self):
        return 480


import screen_ui

clock = [1000.0]
screen_ui.pytime.monotonic = lambda: clock[0]
ui = screen_ui.ScreenUI(FakeDisp(), 320, 256)

FAIL = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra else ""))
    if not cond:
        FAIL.append(name)


def tap(key, hold_ms):
    """按下并按住 hold_ms 后松开，返回触发序列"""
    rect = ui.hit_areas[(ui.page, key)]
    cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
    base = clock[0]
    out = []
    ui.ts.point = (cx, cy, True)
    out.append(ui.poll())
    steps = max(1, hold_ms // 30)
    for i in range(steps):
        clock[0] = base + (i + 1) * 0.03
        out.append(ui.poll())
    ui.ts.point = (cx, cy, False)
    out.append(ui.poll())
    return [o for o in out if o is not None]


# 1. EXIT 短按不应触发
clock[0] += 1.0
r = tap("exit", 100)
check("EXIT 短按(100ms)不触发", r == [], r)

clock[0] += 1.0
r = tap("exit", 900)
check("EXIT 短按(900ms)不触发", r == [], r)

clock[0] += 1.0
r = tap("exit", 1400)
check("EXIT 未达阈值(1400ms)不触发", r == [], r)

# 2. EXIT 长按应触发一次
clock[0] += 1.0
r = tap("exit", 1600)
check("EXIT 长按(1600ms)触发一次", r == ["exit"], r)

# 3. 短按键不受影响
clock[0] += 1.0
check("正式主页仅有详情和退出", [k for k, _ in screen_ui.BUTTONS[ui.page]] == ["result", "exit"])

clock[0] += 1.0
r = tap("result", 100)
check("INFO 短按正常触发", r == ["result"], r)

# 移出后再移回，不得继承长按计时；墙上时钟跳变不影响长按。
rect = ui.hit_areas[(ui.page, "exit")]
cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
clock[0] += 1
ui.ts.point = (cx, cy, True)
ui.poll()
clock[0] += 1.0
screen_ui.pytime.time = lambda: -1000000
ui.ts.point = (0, 0, True)
check("移出取消", ui.poll() is None and ui.press_key is None)
clock[0] += 1.0
ui.ts.point = (cx, cy, True)
check("移回不能续上长按", ui.poll() is None)
ui.ts.point = (cx, cy, False)
ui.poll()

print("=" * 44)
print("失败项:", FAIL if FAIL else "无")
sys.exit(1 if FAIL else 0)
