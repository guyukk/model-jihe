"""Touch-only eight-color calibration app. No model or application UART."""
import copy
import os
import time as pytime

from maix import app, camera, display, image, touchscreen, err, comm

import lab_color

WIDTH, HEIGHT = 320, 256
PATH = "/root/lab_colors.json"
FONT = "/maixapp/share/font/SourceHanSansCN-Regular.otf"
ORDER = tuple(lab_color.COLOR_IDS)
ZH = ("红", "橙", "黄", "绿", "蓝", "紫", "黑", "白")


def screen_to_image(x, y, screen_width, screen_height):
    scale = min(screen_width / WIDTH, screen_height / HEIGHT)
    left = (screen_width - WIDTH * scale) / 2
    top = (screen_height - HEIGHT * scale) / 2
    px, py = int((x-left) / scale), int((y-top) / scale)
    return (px, py) if 0 <= px < WIDTH and 0 <= py < HEIGHT else (None, None)


class CalibrationSession:
    def __init__(self, path=PATH):
        self.path = path
        try:
            self.data = copy.deepcopy(lab_color.load_data(path))
            self.message = "已加载旧标定"
        except (OSError, ValueError, TypeError) as exc:
            self.data = {"version": lab_color.VERSION, "colors": {}}
            self.message = "旧标定不可用: %s" % exc
        self.dirty = False

    def count(self, name):
        return len(self.data["colors"].get(name, []))

    def add(self, name, raw, x, y):
        sample = lab_color.sample_patch(raw, x, y, WIDTH, HEIGHT)
        self.data["colors"].setdefault(name, []).append(sample)
        self.dirty = True
        self.message = "%s #%d LAB %.1f %.1f %.1f" % (
            name, self.count(name), *sample["lab"])
        return sample

    def undo(self, name):
        samples = self.data["colors"].get(name, [])
        if samples:
            samples.pop()
            self.dirty = True
        self.message = "%s: %d samples" % (name, len(samples))

    def clear(self, name):
        self.data["colors"][name] = []
        self.dirty = True
        self.message = "%s cleared" % name

    def save(self):
        try:
            lab_color.save_data(self.path, self.data)
        except (ValueError, OSError) as exc:
            self.message = "保存失败: %s" % exc
            return False
        self.dirty = False
        self.message = "已保存，重启几何体检测后生效"
        return True


def _label(img, x, y, text, color=image.COLOR_WHITE, scale=0.8):
    img.draw_string(x, y, text, color=color, scale=scale)


def _button(img, x, y, w, text, selected=False):
    img.draw_rect(x, y, w, 30, color=image.COLOR_GREEN if selected else image.COLOR_BLACK,
                  thickness=-1)
    img.draw_rect(x, y, w, 30, color=image.COLOR_WHITE, thickness=1)
    _label(img, x+6, y+5, text)


def preview_color(sample):
    """Maix draw APIs require image.Color, not an integer-packed RGB value."""
    return image.Color.from_rgb(*sample["rgb"])


def run():
    # Maix import may start its built-in serial listener. Close it: this app
    # deliberately has no application UART and must not contend with UART4.
    try:
        comm.rm_default_comm_listener()
    except Exception as exc:
        print("内置通信监听关闭失败: %s" % exc)
    try:
        err.check_raise(image.load_font("lab_cal", FONT, size=16), "load font")
        err.check_raise(image.set_default_font("lab_cal"), "select font")
        labels = ZH
        actions = ("撤销", "重采", "保存", "退出")
    except Exception:
        labels = tuple(name.upper()[:3] for name in ORDER)
        actions = ("UNDO", "CLEAR", "SAVE", "EXIT")
    session = CalibrationSession()
    cam = camera.Camera(WIDTH, HEIGHT, image.Format.FMT_RGB888)
    disp = display.Display()
    ts = touchscreen.TouchScreen()
    selected = ORDER[0]
    was_pressed = False
    down = None
    last_sample = None
    while not app.need_exit():
        raw = cam.read()
        if raw is None:
            continue
        view = raw.copy()
        view.draw_rect(0, 0, WIDTH, 66, color=image.COLOR_BLACK, thickness=-1)
        for i, name in enumerate(ORDER):
            x, y = (i % 4) * 80, (i // 4) * 32
            _button(view, x, y, 78, "%s %d" % (labels[i], session.count(name)), name == selected)
        view.draw_rect(0, 183, WIDTH, 73, color=image.COLOR_BLACK, thickness=-1)
        count = session.count(selected)
        _label(view, 3, 184, "%s %d/4 | tap object, vary angle/light" %
               (labels[ORDER.index(selected)], count), image.COLOR_YELLOW, 0.65)
        if last_sample:
            view.draw_rect(280, 183, 34, 25, color=preview_color(last_sample), thickness=-1)
            _label(view, 3, 201, "LAB %.1f %.1f %.1f d=%.1f" %
                   (*last_sample["lab"], last_sample["dispersion"]), image.COLOR_WHITE, 0.7)
        else:
            _label(view, 3, 201, session.message[:48], image.COLOR_WHITE, 0.65)
        for i, label in enumerate(actions):
            _button(view, i*80, 224, 78, label)
        disp.show(view)
        x, y, pressed = ts.read()
        px, py = screen_to_image(x, y, disp.width(), disp.height())
        if pressed and not was_pressed:
            down = (px, py)
        elif not pressed and was_pressed and down[0] is not None:
            dx, dy = down
            if py is not None and abs(px-dx) < 18 and abs(py-dy) < 18:
                if dy < 64:
                    selected = ORDER[min(7, (dy // 32)*4 + dx // 80)]
                    last_sample = None
                elif dy >= 224:
                    action = dx // 80
                    if action == 0:
                        session.undo(selected)
                    elif action == 1:
                        session.clear(selected)
                    elif action == 2:
                        session.save()
                    elif action == 3:
                        print("退出颜色标定；未保存更改不会写盘" if session.dirty else "退出颜色标定")
                        break
                    last_sample = None
                elif 66 <= dy < 183:
                    # The sample is from `raw` captured before all draw calls.
                    last_sample = session.add(selected, raw, dx, dy)
                    print(session.message)
        was_pressed = pressed
        pytime.sleep(0.02)
    del ts, disp, cam


if __name__ == "__main__":
    run()
