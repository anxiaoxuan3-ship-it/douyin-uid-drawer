# -*- coding: utf-8 -*-
"""参与名单窗（给观众看「谁参与成功了」）。

跟展示窗完全独立：自己的位置、大小、标题、字体，可以单独给直播伴侣抓一路。
背景/字体/描边沿用「展示窗口」页的设置，这样两路素材只需抠同一种颜色。

显示逻辑（**B 站弹幕式**）：
  * 有人参与成功时，生成一条「UID + 参与成功」的弹幕，从右往左飘过
  * 多条弹幕落在不同轨道（垂直方向错开），互不重叠
  * 弹幕滚到左边缘外自动消失
  * 底部显示「已参与 N 人」计数
"""

from __future__ import annotations

import time
import tkinter as tk
from typing import List, Optional

from . import bus as B
from .config import BgMode, BoardConfig
from .overlay import _Outlined, pick_font_family
from .state import DanmakuRecord, Session
from . import winutil as W
from .winutil import (grab_window, list_capturable_windows,
                      make_capturable, root_hwnd, set_layered)


class _Danmaku:
    """一条正在飘的弹幕：一个 UID 文字 + 一个后缀文字，从右往左移动。"""

    __slots__ = ("uid", "track", "x", "speed", "born", "t_uid", "t_suf",
                 "uid_w", "suf_w", "y", "done")

    def __init__(self, uid: str, track: int, y: float, speed: float,
                 t_uid: _Outlined, t_suf: Optional[_Outlined],
                 uid_w: float, suf_w: float) -> None:
        self.uid = uid
        self.track = track
        self.y = y
        self.speed = speed
        self.born = time.time()
        self.t_uid = t_uid
        self.t_suf = t_suf
        self.uid_w = uid_w
        self.suf_w = suf_w
        # 从窗口右边缘外开始
        self.x = uid_w + suf_w
        self.done = False

    @property
    def total_w(self) -> float:
        return self.uid_w + self.suf_w


class BoardWindow:
    def __init__(self, master: tk.Misc, session: Session) -> None:
        self.session = session
        self.bc: BoardConfig = session.cfg.board
        self.oc = session.cfg.overlay       # 背景/字体/描边沿用展示窗的设置

        self.win = tk.Toplevel(master)
        self.win.title(self.bc.window_title)

        self.family = pick_font_family(master, self.oc.font_family)
        self.canvas = tk.Canvas(self.win, highlightthickness=0, bd=0,
                                bg=self.bg_color())
        self.canvas.pack(fill="both", expand=True)

        self._bg_image = None
        self._bg_item = None
        self._drag = (0, 0)
        self._visible = True
        self._locked = self.bc.locked if hasattr(self.bc, "locked") else False
        self.hwnd = 0

        self.t_title = _Outlined(self.canvas, self.oc.font(self.bc.title_size),
                                 self.bc.title_color, self.oc.outline_color,
                                 self.oc.outline_width)
        self.t_footer = _Outlined(self.canvas,
                                  self.oc.font(self.bc.footer_size),
                                  self.bc.footer_color, self.oc.outline_color,
                                  max(2, self.oc.outline_width - 2))

        # 弹幕运行时状态
        self._danmaku: List[_Danmaku] = []
        self._anim_job: Optional[str] = None
        self._last_frame = 0.0
        self._total = len(self.session.pool.all_entries())

        self._apply_window_attrs()
        self._apply_bg()
        self._bind()
        self.canvas.bind("<Configure>", lambda e: self.relayout())

        session.bus.on(B.POOL_CHANGED, self._on_pool_changed)
        session.bus.on(B.DANMAKU, self._on_danmaku)
        session.bus.on(B.CONFIG_CHANGED, self._on_config_changed)

        self.relayout()
        self._ensure_anim()

    # ================================================================== #
    # 外观
    # ================================================================== #

    def bg_color(self) -> str:
        return self.oc.bg_color

    def _apply_window_attrs(self) -> None:
        self.win.attributes("-topmost", bool(self.bc.always_on_top))
        try:
            self.win.overrideredirect(bool(self.bc.borderless))
        except Exception:
            pass
        self.win.attributes("-topmost", bool(self.bc.always_on_top))
        self.win.title(self.bc.window_title)
        try:
            self.win.geometry(self.bc.geom)
        except Exception:
            self.win.geometry("1600x900")
        self.ensure_capturable()

    def place_again(self) -> None:
        self.win.title(self.bc.window_title)
        try:
            self.win.geometry(self.bc.geom)
        except Exception:
            pass
        self.win.attributes("-topmost", bool(self.bc.always_on_top))
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        self.ensure_capturable()
        self.relayout()

    def ensure_capturable(self, verbose: bool = False) -> dict:
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        hwnd = root_hwnd(self.win)
        if not hwnd:
            return {}
        self.hwnd = hwnd
        want_layered = (self.oc.bg_mode == BgMode.TRANSPARENT)
        info = make_capturable(hwnd, drop_layered=not want_layered)
        if want_layered:
            set_layered(hwnd, True)
        if verbose:
            self.session.log(
                f"参与名单窗句柄 {hwnd}  可被采集="
                f"{'是' if info.get('capturable') else '否'}")
        return info

    def capture_report(self) -> str:
        info = self.ensure_capturable()
        lines = [
            "【参与名单窗】",
            f"窗口标题：{info.get('title') or '(空)'}",
            f"窗口句柄：{info.get('hwnd')}",
            "扩展样式：" + ("、".join(info.get("exstyle_names") or []) or "无"),
            f"● 采集软件能不能列到它：{'能' if info.get('capturable') else '不能'}",
        ]
        if not info.get("capturable"):
            lines.append("  → 点「一键修复采集样式」，或者把「无边框」去掉")
        if info.get("has_layered"):
            lines.append("  ⚠ 真透明模式带 WS_EX_LAYERED，采集会偏黑，"
                         "建议用「纯色背景」")
        res = grab_window(int(info.get("hwnd") or 0), "bitblt")
        if res and res.get("ok"):
            lines.append(f"BitBlt 抓取：{res['w']}x{res['h']} "
                         f"颜色数 {res.get('colors')}  "
                         + ("→ 像是黑屏 ❌" if res.get("blank")
                            else "→ 画面正常 ✅"))
        wins = list_capturable_windows()
        inside = any(w["hwnd"] == info.get("hwnd") for w in wins)
        lines.append(f"在采集列表里：{'在 ✅' if inside else '不在 ❌'}"
                     f"（列表共 {len(wins)} 个窗口）")
        return "\n".join(lines)

    def _apply_bg(self) -> None:
        mode = self.oc.bg_mode
        color = self.oc.bg_color
        try:
            self.win.attributes("-transparentcolor", "")
        except Exception:
            pass

        self._bg_item = W.apply_bg(
            self.win, self.canvas, self._bg_item, self.oc.image_path,
            color, mode, BgMode.TRANSPARENT)
        self._bg_image = getattr(self.canvas, "_bg_photo", None)
        self.ensure_capturable()

    # ================================================================== #
    # 交互
    # ================================================================== #

    def _bind(self) -> None:
        for w in (self.win, self.canvas):
            w.bind("<Button-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<Button-3>", self._on_menu)
            w.bind("<Double-Button-1>", lambda e: None)

    def _on_press(self, ev) -> None:
        self._drag = (ev.x_root - self.win.winfo_x(),
                      ev.y_root - self.win.winfo_y())

    def _on_drag(self, ev) -> None:
        if self._locked:
            return
        x = ev.x_root - self._drag[0]
        y = ev.y_root - self._drag[1]
        self.win.geometry(f"+{x}+{y}")
        w, h = self._配置尺寸()
        self.bc.geom = f"{w}x{h}+{x}+{y}"
        self.session.bus.emit(B.CONFIG_DIRTY, None)

    def _配置尺寸(self) -> tuple:
        """从 bc.geom 里取宽高；取不到就用默认 1600x900。"""
        try:
            w_h = (self.bc.geom or "").split("+")[0]
            w, h = w_h.split("x")
            w, h = int(w), int(h)
            if w >= 200 and h >= 160:
                return (w, h)
        except Exception:
            pass
        return (1600, 900)

    def reset_size(self, 提示: bool = True) -> None:
        """把参与名单窗恢复成默认 16:9 大小（1600x900），并尽量居中。"""
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        w, h = 1600, 900
        if w > sw - 40:
            w = max(480, sw - 40)
            h = int(w * 9 / 16)
        if h > sh - 80:
            h = max(270, sh - 80)
            w = int(h * 16 / 9)
        x = max(20, (sw - w) // 2)
        y = max(40, (sh - h) // 2)

        self.bc.geom = f"{w}x{h}+{x}+{y}"
        try:
            self.win.geometry(self.bc.geom)
            self.win.update_idletasks()
        except Exception:
            pass
        try:
            self.relayout()
        except Exception:
            pass

        try:
            self.session.bus.emit(B.CONFIG_DIRTY, None)
        except Exception:
            pass
        if 提示:
            try:
                self.session.log(f"参与名单窗口已重置为 {w}x{h}")
                self.session.bus.emit(B.STATUS, f"参与名单窗口已重置为 {w}x{h}")
            except Exception:
                pass

    def _on_menu(self, ev) -> None:
        m = tk.Menu(self.win, tearoff=0)
        m.add_command(label="锁定位置" if not self._locked else "解锁位置",
                      command=self.toggle_lock)
        m.add_command(label="显示/隐藏标题栏", command=self.toggle_borderless)
        m.add_separator()
        m.add_command(label="重置窗口大小和位置（16:9）", command=self.reset_size)
        m.add_command(label="隐藏这个窗口", command=self.hide)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def toggle_lock(self) -> None:
        self._locked = not self._locked
        self.session.log("参与名单窗已" + ("锁定" if self._locked else "解锁"))

    def toggle_borderless(self) -> None:
        self.bc.borderless = not self.bc.borderless
        self._apply_window_attrs()

    def hide(self) -> None:
        self.win.withdraw()
        self._visible = False

    def show(self) -> None:
        self.win.deiconify()
        self.win.attributes("-topmost", bool(self.bc.always_on_top))
        self._visible = True

    def toggle_visible(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    @property
    def visible(self) -> bool:
        return self._visible

    # ================================================================== #
    # 布局
    # ================================================================== #

    def relayout(self) -> None:
        w = self.canvas.winfo_width() or 1600
        h = self.canvas.winfo_height() or 900
        bc, oc = self.bc, self.oc

        self.t_title.configure(oc.font(bc.title_size), bc.title_color,
                               oc.outline_color, oc.outline_width)
        self.t_title.set(bc.title_text)
        self.t_title.place(w / 2, max(24, h * 0.06), "center")
        self.t_title.show(bc.show_title and bool(bc.title_text))

        self.t_footer.configure(oc.font(bc.footer_size), bc.footer_color,
                                oc.outline_color, max(2, oc.outline_width - 2))
        self._refresh_footer()

        # 窗口尺寸变了，正在飘的弹幕要按新宽度/轨道重新落位
        for dm in self._danmaku:
            dm.y = self._track_y(dm.track)

    def _track_y(self, track: int) -> float:
        """第 track 条轨道的垂直中心坐标。

        标题默认不显示，弹幕直接从顶部往下铺；若用户手动开启标题，
        则往下让出标题的高度。
        """
        h = self.canvas.winfo_height() or 900
        top = h * 0.16 if self.bc.show_title else h * 0.05
        return top + track * self.bc.track_gap

    def _refresh_footer(self) -> None:
        if not self.bc.show_footer:
            self.t_footer.show(False)
            return
        h = self.canvas.winfo_height() or 900
        w = self.canvas.winfo_width() or 1600
        tip = f"已参与 {self._total} 人" if self._total else "还没有人参与"
        self.t_footer.set(tip)
        self.t_footer.place(w / 2, h * 0.965, "center")
        self.t_footer.show(True)

    # ================================================================== #
    # 弹幕动画
    # ================================================================== #

    def _spawn(self, uid: str) -> None:
        """生成一条新弹幕「UID 参与成功」，落在最空闲的轨道。"""
        bc, oc = self.bc, self.oc
        w = self.canvas.winfo_width() or 1600

        # 挑一条最空闲的轨道：该轨道上最后一条弹幕已经滚进来足够远，
        # 或者整条弹幕已经滚出屏幕。
        track = self._pick_track()

        font = oc.font(bc.uid_size)
        # 用测量值估算文字宽度（tkinter 没有直接量文本宽度，按字符数估）
        uid_w = self._measure(uid, bc.uid_size)
        suf_text = f" {bc.suffix}" if bc.show_suffix else ""
        suf_w = self._measure(suf_text, bc.uid_size)

        t_uid = _Outlined(self.canvas, font, bc.uid_color,
                          oc.outline_color, max(2, oc.outline_width - 2))
        t_uid.set(uid)
        if suf_text:
            t_suf = _Outlined(self.canvas, font, bc.suffix_color,
                              oc.outline_color, max(2, oc.outline_width - 2))
            t_suf.set(suf_text)
        else:
            t_suf = None

        y = self._track_y(track)
        dm = _Danmaku(uid, track, y, bc.speed, t_uid, t_suf, uid_w, suf_w)
        # 初始 x 在窗口右边缘外
        dm.x = w + dm.total_w
        self._danmaku.append(dm)
        self._place_danmaku(dm)
        self._ensure_anim()

    def _measure(self, text: str, size: int) -> float:
        """粗略估算文字宽度（像素）。CJK 全宽按字号，ASCII 按 0.6 字号。"""
        if not text:
            return 0.0
        total = 0.0
        for ch in text:
            if ord(ch) > 0x2E80:  # CJK 及全角符号
                total += size * 1.0
            else:
                total += size * 0.62
        return total

    def _pick_track(self) -> int:
        """选一条最空闲的轨道。"""
        n = max(1, int(self.bc.track_count))
        w = self.canvas.winfo_width() or 1600
        # 每条轨道记录「上一条弹幕的右边缘 x」，取右边缘最小（最靠左=最空闲）
        rightmost = {}
        for dm in self._danmaku:
            cur_right = dm.x + dm.total_w
            if dm.track not in rightmost or cur_right > rightmost[dm.track]:
                rightmost[dm.track] = cur_right

        best, best_val = 0, None
        for t in range(n):
            v = rightmost.get(t, -1e9)  # 没弹幕的轨道视为最空闲
            if best_val is None or v < best_val:
                best, best_val = t, v
        return best

    def _place_danmaku(self, dm: _Danmaku) -> None:
        """按 dm 当前 x 坐标摆放 UID 和后缀两个文字。"""
        # UID 左对齐在 (x, y)，后缀紧跟其后
        uid_left = dm.x
        dm.t_uid.place(uid_left, dm.y, "w")
        if dm.t_suf:
            dm.t_suf.place(uid_left + dm.uid_w, dm.y, "w")

    def _ensure_anim(self) -> None:
        if self._anim_job is not None:
            return
        self._last_frame = time.time()
        self._anim_job = self.win.after(16, self._anim_tick)

    def _anim_tick(self) -> None:
        self._anim_job = None
        now = time.time()
        dt = now - self._last_frame
        self._last_frame = now
        if dt > 0.5:  # 长时间挂起（如拖窗口）时别跳一大步
            dt = 0.016

        w = self.canvas.winfo_width() or 1600
        alive = []
        for dm in self._danmaku:
            dm.x -= dm.speed * dt
            if dm.x + dm.total_w < -20:
                # 完全滚出左边缘，回收
                dm.t_uid.destroy()
                if dm.t_suf:
                    dm.t_suf.destroy()
                continue
            self._place_danmaku(dm)
            alive.append(dm)
        self._danmaku = alive

        # 只要还有弹幕在飘就继续跑
        if self._danmaku:
            self._anim_job = self.win.after(16, self._anim_tick)

    # ================================================================== #
    # 事件
    # ================================================================== #

    def _on_pool_changed(self, payload=None) -> None:
        reason = (payload or {}).get("reason") if isinstance(payload, dict) else None
        if reason in ("reset", "remove", "blacklist", "offline", "manual_clear",
                      "carry_clear", "watch_reset"):
            self._total = len(self.session.pool.all_entries())
            self._refresh_footer()
        elif reason == "new":
            uid = (payload or {}).get("uid", "")
            self._total = len(self.session.pool.all_entries())
            self._refresh_footer()
            if uid:
                self._spawn(uid)

    def _on_danmaku(self, rec: DanmakuRecord) -> None:
        pass

    def _on_config_changed(self, payload=None) -> None:
        self.bc = self.session.cfg.board
        self.oc = self.session.cfg.overlay
        self._locked = self.bc.locked if hasattr(self.bc, "locked") else False
        self._apply_window_attrs()
        self._apply_bg()
        self.relayout()

    def destroy(self) -> None:
        if self._anim_job is not None:
            try:
                self.win.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        for dm in self._danmaku:
            dm.t_uid.destroy()
            if dm.t_suf:
                dm.t_suf.destroy()
        self._danmaku.clear()
        try:
            self.win.destroy()
        except Exception:
            pass
