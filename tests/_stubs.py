# -*- coding: utf-8 -*-
"""离线测试共用的桩件：把 maix 与串口替换掉

只提供被测代码真正用到的那部分接口，避免为了跑测试去装整个 MaixPy。
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)


def ensure_project_path():
    """把工程根目录加入模块搜索路径"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


def install_maix_stub():
    """安装最小 maix 桩件，返回 (maix, image, time)"""
    if "maix" in sys.modules and getattr(sys.modules["maix"], "_is_stub", False):
        return (sys.modules["maix"], sys.modules["maix.image"],
                sys.modules["maix.time"])

    maix = types.ModuleType("maix")
    maix._is_stub = True

    def _mod(name):
        m = types.ModuleType("maix." + name)
        setattr(maix, name, m)
        sys.modules["maix." + name] = m
        return m

    mtime = _mod("time")
    mtime.sleep_ms = lambda ms: None
    mtime.time = lambda: 0

    mimage = _mod("image")
    # vision.py 与 screen_ui.py 在构造/绘制时用到这些常量
    for name, val in (("COLOR_WHITE", 0xFFFFFF), ("COLOR_BLACK", 0),
                      ("COLOR_RED", 0xFF0000), ("COLOR_GREEN", 0x00FF00),
                      ("COLOR_BLUE", 0x0000FF), ("COLOR_YELLOW", 0xFFFF00),
                      ("COLOR_CYAN", 0x00FFFF), ("COLOR_ORANGE", 0xFF8000),
                      ("COLOR_PURPLE", 0x800080)):
        setattr(mimage, name, val)
    mimage.Format = types.SimpleNamespace(FMT_RGB888="rgb888")
    mimage.string_size = lambda text: (len(text) * 8, 16)

    # app：退出标志，测试里可复位
    mapp = _mod("app")
    mapp._exit = False
    mapp.need_exit = lambda: mapp._exit
    mapp.set_exit_flag = lambda v: setattr(mapp, "_exit", bool(v))

    msys = _mod("sys")
    msys.device_id = lambda: "maixcam2"

    merr = _mod("err")
    merr.check_raise = lambda *a, **k: None

    _mod("camera")
    _mod("display")
    _mod("nn")
    _mod("gpio")
    _mod("pinmap")
    muart = _mod("uart")
    muart.UART = object

    mts = _mod("touchscreen")
    mts.TouchScreen = object

    mcomm = _mod("comm")
    mcomm.rm_default_comm_listener = lambda: True
    mcomm.add_default_comm_listener = lambda: None

    sys.modules["maix"] = maix
    return maix, mimage, mtime


class FakeSerial:
    """可配置返回值的串口桩件

    ret: 模拟 uart.write 的返回值
         None  -> 不返回长度（按成功处理）
         -1    -> 错误码
         整数  -> 实际写入字节数
    raise_on_write: 为真时抛异常
    """

    def __init__(self, ret=None, raise_on_write=False, rx=b""):
        self.written = []
        self.ret = ret
        self.raise_on_write = raise_on_write
        self._rx = bytearray(rx)

    def write(self, data):
        if self.raise_on_write:
            raise OSError("simulated write error")
        self.written.append(bytes(data))
        if self.ret is None:
            return None
        if isinstance(self.ret, int):
            return self.ret
        return len(data)

    def available(self):
        return len(self._rx)

    def read(self, n):
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def feed(self, data):
        self._rx.extend(data)


def check(results, name, cond, extra=""):
    """统一断言输出，返回是否通过"""
    ok = bool(cond)
    print(("PASS " if ok else "FAIL ") + name + ((" " + str(extra)) if extra else ""))
    if not ok:
        results.append(name)
    return ok


def report(results):
    """打印汇总并返回进程退出码"""
    print("\n" + "=" * 50)
    print("失败项:", results if results else "无")
    return 1 if results else 0
