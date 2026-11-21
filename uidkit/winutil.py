# -*- coding: utf-8 -*-
"""Windows 窗口样式调整与「能不能被采集」自检。

为什么要这个模块
----------------
无边框窗口（tkinter 的 ``overrideredirect(True)``）拿到的是 ``WS_POPUP``：

    STYLE   = WS_POPUP | WS_VISIBLE          （没有 WS_CAPTION）
    EXSTYLE = WS_EX_TOPMOST                  （没有 WS_EX_APPWINDOW）

而直播伴侣 / OBS 这类软件列窗口时，普遍用的是「Alt+Tab 式」过滤：

    保留 = (style & WS_CAPTION) != 0  或者  (exstyle & WS_EX_APPWINDOW) != 0

两条都不满足 → 窗口被过滤掉，下拉列表里根本看不到它（虽然 EnumWindows 明明能枚举到）。

解决办法：给无边框窗口补上 ``WS_EX_APPWINDOW``。这个样式的作用正是
「让一个没有标题栏的顶层窗口照样出现在任务栏 / Alt+Tab 列表里」，
于是采集软件也能列到它了。

顺带还会：
  * 清掉 ``WS_EX_TOOLWINDOW``（有些软件专门排除工具窗口）
  * 在纯色背景下清掉 ``WS_EX_LAYERED``（分层窗口 BitBlt 抓不到内容）
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Dict, Optional, Tuple

GWL_STYLE = -16
GWL_EXSTYLE = -20
GA_ROOT = 2

WS_CAPTION = 0x00C00000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000

WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020

_STYLE_NAMES = [
    (WS_CAPTION, "WS_CAPTION"), (WS_POPUP, "WS_POPUP"),
    (WS_VISIBLE, "WS_VISIBLE"), (WS_THICKFRAME, "WS_THICKFRAME"),
    (WS_SYSMENU, "WS_SYSMENU"),
]
_EX_NAMES = [
    (WS_EX_LAYERED, "WS_EX_LAYERED"), (WS_EX_TOOLWINDOW, "WS_EX_TOOLWINDOW"),
    (WS_EX_TOPMOST, "WS_EX_TOPMOST"), (WS_EX_APPWINDOW, "WS_EX_APPWINDOW"),
    (WS_EX_NOACTIVATE, "WS_EX_NOACTIVATE"),
    (WS_EX_TRANSPARENT, "WS_EX_TRANSPARENT"),
]

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _LONG_PTR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 \
        else ctypes.c_long
    try:
        _get_long = _user32.GetWindowLongPtrW
        _set_long = _user32.SetWindowLongPtrW
        _get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _get_long.restype = _LONG_PTR
        _set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, _LONG_PTR]
        _set_long.restype = _LONG_PTR
    except AttributeError:                      # 32 位系统没有 *Ptr 版本
        _get_long = _user32.GetWindowLongW
        _set_long = _user32.SetWindowLongW
        _get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _get_long.restype = ctypes.c_long
        _set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        _set_long.restype = ctypes.c_long


# --------------------------------------------------------------------------- #
# 句柄
# --------------------------------------------------------------------------- #

def root_hwnd(widget) -> int:
    """拿到一个 tkinter 控件所属的顶层窗口句柄（采集软件看的就是这个）。"""
    if not _IS_WIN:
        return 0
    try:
        inner = widget.winfo_id()
    except Exception:
        return 0
    root = _user32.GetAncestor(ctypes.c_void_p(inner), GA_ROOT)
    return int(root or inner)


def window_title(hwnd: int) -> str:
    if not _IS_WIN or not hwnd:
        return ""
    n = _user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
    buf = ctypes.create_unicode_buffer(n + 2)
    _user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, n + 2)
    return buf.value


def window_class(hwnd: int) -> str:
    if not _IS_WIN or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
    return buf.value


def window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    if not _IS_WIN or not hwnd:
        return (0, 0, 0, 0)

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    r = _RECT()
    _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


# --------------------------------------------------------------------------- #
# 显示器配置
# --------------------------------------------------------------------------- #

#: GetSystemMetrics 的索引
SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_CMONITORS = 80


def display_signature() -> Tuple:
    """当前显示器配置的「指纹」，用来判断副屏是不是被关掉/拔掉了。

    ★ 为什么不能用 tkinter 的 ``winfo_screenwidth()``：
      它**只报主屏**的尺寸。把副屏关掉时主屏没变，所以每次都读到一样的值，
      程序根本察觉不到显示器变了 —— 这正是「副屏关掉后两个窗口黏在一起」
      却一直不自我修复的原因。

    这里改用 Win32 的 **虚拟桌面**（所有显示器合起来的矩形）和**显示器个数**，
    副屏一关，这两个值立刻会变。

    返回 ``(显示器数, 左, 上, 宽, 高)``；拿不到时返回 ``()``。
    """
    if not _IS_WIN:
        return ()
    try:
        return (
            _user32.GetSystemMetrics(SM_CMONITORS),
            _user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            _user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
        )
    except Exception:
        return ()


def virtual_screen() -> Tuple[int, int, int, int]:
    """虚拟桌面矩形 (左, 上, 宽, 高)。所有显示器合起来的那块区域。"""
    sig = display_signature()
    if not sig:
        return (0, 0, 0, 0)
    _, x, y, w, h = sig
    return (x, y, w, h)


def on_screen(x: int, y: int, w: int, h: int, 留边: int = 40) -> bool:
    """一个矩形是不是还落在「当前存在的」显示器范围内。

    用来判断窗口有没有跑到已经关掉的副屏上去。
    只要还有一小块露在虚拟桌面里就算在屏幕内（用户还够得着）。
    """
    vx, vy, vw, vh = virtual_screen()
    if vw <= 0 or vh <= 0:
        # 拿不到虚拟桌面信息就退回「主屏」判断
        sw = _user32.GetSystemMetrics(SM_CXSCREEN) if _IS_WIN else 1920
        sh = _user32.GetSystemMetrics(SM_CYSCREEN) if _IS_WIN else 1080
        vx, vy, vw, vh = 0, 0, sw, sh
    return (x + w > vx + 留边 and y + h > vy + 留边
            and x < vx + vw - 留边 and y < vy + vh - 留边)


# --------------------------------------------------------------------------- #
# 样式
# --------------------------------------------------------------------------- #

def get_styles(hwnd: int) -> Tuple[int, int]:
    if not _IS_WIN or not hwnd:
        return (0, 0)
    st = int(_get_long(ctypes.c_void_p(hwnd), GWL_STYLE)) & 0xFFFFFFFF
    ex = int(_get_long(ctypes.c_void_p(hwnd), GWL_EXSTYLE)) & 0xFFFFFFFF
    return st, ex


def describe(hwnd: int) -> Dict[str, object]:
    st, ex = get_styles(hwnd)
    return {
        "hwnd": hwnd,
        "title": window_title(hwnd),
        "class": window_class(hwnd),
        "rect": window_rect(hwnd),
        "style": st,
        "exstyle": ex,
        "style_names": [n for b, n in _STYLE_NAMES if st & b],
        "exstyle_names": [n for b, n in _EX_NAMES if ex & b],
        "has_caption": bool(st & WS_CAPTION),
        "has_appwindow": bool(ex & WS_EX_APPWINDOW),
        "has_toolwindow": bool(ex & WS_EX_TOOLWINDOW),
        "has_layered": bool(ex & WS_EX_LAYERED),
        "capturable": passes_capture_filter(st, ex),
    }


def passes_capture_filter(style: int, exstyle: int) -> bool:
    """模拟采集软件列窗口时的「Alt+Tab 式」过滤。

    这是绝大多数窗口捕获实现的判据（OBS、直播伴侣、各种录屏工具都类似）：

        (style & WS_CAPTION) 或 (exstyle & WS_EX_APPWINDOW)

    再加上排除工具窗口、排除不可见的窗口。
    """
    if not (style & WS_VISIBLE):
        return False
    if exstyle & WS_EX_TOOLWINDOW:
        return False
    return bool((style & WS_CAPTION) or (exstyle & WS_EX_APPWINDOW))


def make_capturable(hwnd: int, drop_layered: bool = False) -> Dict[str, object]:
    """让这个窗口能被「窗口捕获」列出来。

    * 补上 ``WS_EX_APPWINDOW`` —— 无边框窗口靠它才能进 Alt+Tab / 捕获列表
    * 清掉 ``WS_EX_TOOLWINDOW``
    * 可选地清掉 ``WS_EX_LAYERED``（纯色背景模式下不该有）

    返回应用后的样式描述。
    """
    if not _IS_WIN or not hwnd:
        return {}
    st, ex = get_styles(hwnd)
    new_ex = ex | WS_EX_APPWINDOW
    new_ex &= ~WS_EX_TOOLWINDOW
    if drop_layered:
        new_ex &= ~WS_EX_LAYERED
    if new_ex != ex:
        _set_long(ctypes.c_void_p(hwnd), GWL_EXSTYLE, new_ex)
    return describe(hwnd)


def set_layered(hwnd: int, on: bool) -> None:
    """手工开关 WS_EX_LAYERED（真透明模式需要它）。"""
    if not _IS_WIN or not hwnd:
        return
    _st, ex = get_styles(hwnd)
    new_ex = (ex | WS_EX_LAYERED) if on else (ex & ~WS_EX_LAYERED)
    if new_ex != ex:
        _set_long(ctypes.c_void_p(hwnd), GWL_EXSTYLE, new_ex)


# --------------------------------------------------------------------------- #
# 枚举（模拟采集软件的列表）
# --------------------------------------------------------------------------- #

def list_capturable_windows() -> list:
    """把「采集软件会列出来的窗口」列一遍，方便对照。"""
    if not _IS_WIN:
        return []
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            return True
        if _user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)) <= 0:
            return True
        st, ex = get_styles(hwnd)
        if passes_capture_filter(st, ex):
            out.append({"hwnd": hwnd, "title": window_title(hwnd),
                        "class": window_class(hwnd)})
        return True

    _user32.EnumWindows(_cb, None)
    return out


# --------------------------------------------------------------------------- #
# 自抓测试：把窗口画面抓下来，判断是不是黑屏 / 纯色
# --------------------------------------------------------------------------- #

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", ctypes.c_uint32 * 3)]


def grab_window(hwnd: int, method: str = "bitblt") -> Optional[dict]:
    """抓一次窗口画面，返回统计信息（不依赖 Pillow）。

    ``method``：``bitblt``（传统窗口采集用的）/ ``printwindow``。
    """
    if not _IS_WIN or not hwnd:
        return None
    left, top, right, bottom = window_rect(hwnd)
    w, h = max(1, right - left), max(1, bottom - top)
    if w < 4 or h < 4:
        return {"ok": False, "reason": "窗口尺寸异常", "w": w, "h": h}

    hdc_win = _user32.GetWindowDC(ctypes.c_void_p(hwnd))
    if not hdc_win:
        return {"ok": False, "reason": "拿不到窗口 DC"}
    hdc_mem = _gdi32.CreateCompatibleDC(ctypes.c_void_p(hdc_win))
    hbmp = _gdi32.CreateCompatibleBitmap(ctypes.c_void_p(hdc_win), w, h)
    _gdi32.SelectObject(ctypes.c_void_p(hdc_mem), ctypes.c_void_p(hbmp))

    SRCCOPY = 0x00CC0020
    if method == "printwindow":
        ok = _user32.PrintWindow(ctypes.c_void_p(hwnd),
                                 ctypes.c_void_p(hdc_mem), 2)
    else:
        ok = _gdi32.BitBlt(ctypes.c_void_p(hdc_mem), 0, 0, w, h,
                           ctypes.c_void_p(hdc_win), 0, 0, SRCCOPY)

    result = {"ok": bool(ok), "w": w, "h": h, "method": method}
    if ok:
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        got = _gdi32.GetDIBits(ctypes.c_void_p(hdc_mem), ctypes.c_void_p(hbmp),
                               0, h, buf, ctypes.byref(bi), 0)
        if got:
            data = buf.raw
            step = max(1, (w * h) // 4000)      # 抽样，别把大图算爆
            seen = set()
            rsum = gsum = bsum = 0
            n = 0
            for i in range(0, w * h, step):
                o = i * 4
                b, g, r = data[o], data[o + 1], data[o + 2]
                rsum += r
                gsum += g
                bsum += b
                n += 1
                if len(seen) < 4096:
                    seen.add((r // 8, g // 8, b // 8))
            result["colors"] = len(seen)
            result["avg"] = (rsum // n, gsum // n, bsum // n)
            result["blank"] = len(seen) <= 1
        else:
            result["ok"] = False
            result["reason"] = "GetDIBits 失败"

    _gdi32.DeleteObject(ctypes.c_void_p(hbmp))
    _gdi32.DeleteDC(ctypes.c_void_p(hdc_mem))
    _user32.ReleaseDC(ctypes.c_void_p(hwnd), ctypes.c_void_p(hdc_win))
    return result


# --------------------------------------------------------------------------- #
# 两个捕获窗口共用的背景渲染
# --------------------------------------------------------------------------- #

def apply_bg(win, canvas, bg_item, image_path: str, transparent_color: str,
             mode: str, bg_mode_transparent: str) -> object:
    """按背景模式设置窗口和画布背景，返回（可能新建的）背景图 item id。

    overlay.py 和 board.py 原来各有一份完全一样的实现，这里合并成一份。

    参数
    ----
    win / canvas   : Tk 窗口和画布
    bg_item        : 已有的背景图 item id（没有就传 None）
    image_path     : 背景图片路径（bg_mode == 图片时才用）
    transparent_color : 透明模式下要抠掉的颜色
    mode           : 当前的背景模式
    bg_mode_transparent : 透明模式的取值（各模块的 BgMode.TRANSPARENT）

    返回
    ----
    背景图 item 的 id；没建图时原样返回传进来的 bg_item。
    """
    import tkinter as tk

    if mode == bg_mode_transparent:
        # 透明：把窗口背景设成要抠掉的颜色，并告诉 Windows 抠掉它
        win.configure(bg=transparent_color)
        canvas.configure(bg=transparent_color)
        try:
            win.attributes("-transparentcolor", transparent_color)
        except Exception:
            pass
        if bg_item is not None:
            canvas.itemconfig(bg_item, state="hidden")
        return bg_item

    if mode == "image" and image_path and os.path.exists(image_path):
        # 图片：底色调黑，把图铺上去
        win.configure(bg="#000000")
        canvas.configure(bg="#000000")
        try:
            img = tk.PhotoImage(file=image_path)
        except Exception:
            img = None
        if img is not None:
            if bg_item is None:
                bg_item = canvas.create_image(0, 0, image=img, anchor="nw")
            else:
                canvas.itemconfig(bg_item, image=img, state="normal")
            # 把引用挂到 canvas 上，防止被垃圾回收（图会变空白）
            canvas._bg_photo = img
            return bg_item

    # 纯色 / 图片加载失败：退化成纯色
    win.configure(bg=transparent_color)
    canvas.configure(bg=transparent_color)
    if bg_item is not None:
        canvas.itemconfig(bg_item, state="hidden")
    return bg_item
