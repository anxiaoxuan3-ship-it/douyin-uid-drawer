# -*- coding: utf-8 -*-
"""在线榜窗口（给观众看「在线前五」+ 贡献分）。

跟展示窗、参与名单窗一样是独立捕获窗口，背景/字体/描边沿用展示窗设置，
直播伴侣再抓一路「窗口捕获 + 绿幕抠图」就能用。

数据来源：抖音直连桥接解析 ``WebcastRoomUserSeqMessage`` 后 POST 到
``/rank``，Session 存进 ``rank``，本窗口订阅 ``RANK_CHANGED`` 刷新。

显示逻辑（按用户要求）：
  * 标题「在线榜 TOP5」
  * 每条：排名 + 昵称 + 贡献分（纯数字，无单位）
  * 可选一行「在线 N 人」
"""

from __future__ import annotations

import tkinter as tk
from typing import List, Optional

from . import bus as B
from .config import BgMode, RankConfig
from .overlay import _Outlined, pick_font_family
from .state import Session
from . import winutil as W
from .winutil import make_capturable, root_hwnd, set_layered


class RankWindow:
    def __init__(self, master: tk.Misc, session: Session) -> None:
        self.session = session
        self.rc: RankConfig = session.cfg.rank
        self.oc = session.cfg.overlay       # 背景/字体/描边沿用展示窗设置

        self.win = tk.Toplevel(master)
        self.win.title(self.rc.window_title)

        self.family = pick_font_family(master, self.oc.font_family)
        self.canvas = tk.Canvas(self.win, highlightthickness=0, bd=0,
                                bg=self.oc.bg_color)
        self.canvas.pack(fill="both", expand=True)

        self._rows: List[_Outlined] = []
        self._num_items: List[_Outlined] = []
        self._score_items: List[_Outlined] = []
        self._bg_image = None
        self._bg_item = None
        self._drag = (0, 0)
        self._visible = True
        self._locked = self.rc.locked
        self.hwnd = 0

        self.t_title = _Outlined(self.canvas, self.oc.font(self.rc.title_size),
                                 self.rc.title_color, self.oc.outline_color,
                                 self.oc.outline_width)
        self.t_total = _Outlined(self.canvas, self.oc.font(self.rc.footer_size),
                                 self.rc.footer_color, self.oc.outline_color,
                                 max(2, self.oc.outline_width - 2))

        self._apply_window_attrs()
        self._apply_bg()
        self._bind()
        self.canvas.bind("<Configure>", lambda e: self.relayout())

        session.bus.on(B.RANK_CHANGED, self._on_rank_changed)
        session.bus.on(B.CONFIG_CHANGED, self._on_config_changed)

        self._ensure_rows()
        self.relayout()

    # ================================================================== #
    # 外观
    # ================================================================== #

    def _apply_window_attrs(self) -> None:
        self.win.attributes("-topmost", bool(self.rc.always_on_top))
        try:
            self.win.overrideredirect(bool(self.rc.borderless))
        except Exception:
            pass
        self.win.attributes("-topmost", bool(self.rc.always_on_top))
        self.win.title(self.rc.window_title)
        try:
            self.win.geometry(self.rc.geom)
        except Exception:
            self.win.geometry("420x360+80+80")
        self.ensure_capturable()

    def place_again(self) -> None:
        self.win.title(self.rc.window_title)
        try:
            self.win.geometry(self.rc.geom)
        except Exception:
            pass
        self.win.attributes("-topmost", bool(self.rc.always_on_top))
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
                f"在线榜窗口句柄 {hwnd}  可被采集="
                f"{'是' if info.get('capturable') else '否'}")
        return info

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
        self.rc.geom = f"{w}x{h}+{x}+{y}"
        self.session.bus.emit(B.CONFIG_DIRTY, None)

    def _配置尺寸(self) -> tuple:
        try:
            w_h = (self.rc.geom or "").split("+")[0]
            w, h = w_h.split("x")
            w, h = int(w), int(h)
            if w >= 200 and h >= 120:
                return (w, h)
        except Exception:
            pass
        return (420, 360)

    def _on_menu(self, ev) -> None:
        m = tk.Menu(self.win, tearoff=0)
        m.add_command(label="锁定位置" if not self._locked else "解锁位置",
                      command=self.toggle_lock)
        m.add_command(label="显示/隐藏标题栏", command=self.toggle_borderless)
        m.add_separator()
        m.add_command(label="隐藏这个窗口", command=self.hide)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def toggle_lock(self) -> None:
        self._locked = not self._locked
        self.rc.locked = self._locked
        self.session.log("在线榜窗口已" + ("锁定" if self._locked else "解锁"))

    def toggle_borderless(self) -> None:
        self.rc.borderless = not self.rc.borderless
        self._apply_window_attrs()

    def hide(self) -> None:
        self.win.withdraw()
        self._visible = False

    def show(self) -> None:
        self.win.deiconify()
        self.win.attributes("-topmost", bool(self.rc.always_on_top))
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
    # 绘制
    # ================================================================== #

    def _ensure_rows(self) -> None:
        need = max(1, int(self.rc.max_rows))
        while len(self._rows) < need:
            # 每行三件：排名数字（左）、昵称（中）、贡献分（右）
            self._num_items.append(_Outlined(
                self.canvas, self.oc.font(self.rc.rank_size),
                self.rc.rank_num_color, self.oc.outline_color,
                max(2, self.oc.outline_width - 2)))
            self._rows.append(_Outlined(
                self.canvas, self.oc.font(self.rc.rank_size),
                self.rc.rank_color, self.oc.outline_color,
                max(2, self.oc.outline_width - 2)))
            self._score_items.append(_Outlined(
                self.canvas, self.oc.font(self.rc.rank_size),
                self.rc.score_color, self.oc.outline_color,
                max(2, self.oc.outline_width - 2)))

    def relayout(self) -> None:
        w = self.canvas.winfo_width() or 420
        h = self.canvas.winfo_height() or 360
        rc, oc = self.rc, self.oc

        self.t_title.configure(oc.font(rc.title_size), rc.title_color,
                               oc.outline_color, oc.outline_width)
        self.t_title.set(rc.title_text)
        self.t_title.place(w / 2, max(22, h * 0.10), "center")
        self.t_title.show(rc.show_title and bool(rc.title_text))

        self.t_total.configure(oc.font(rc.footer_size), rc.footer_color,
                               oc.outline_color, max(2, oc.outline_width - 2))

        self._ensure_rows()
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        rc, oc = self.rc, self.oc
        w = self.canvas.winfo_width() or 420
        h = self.canvas.winfo_height() or 360

        rank = self.session.rank
        rows = (rank.get("ranks") or [])[:max(1, int(rc.max_rows))]
        total = int(rank.get("total") or 0)

        top = h * (0.20 if rc.show_title else 0.08)
        if rc.show_total and total > 0:
            self.t_total.set(f"在线 {total} 人")
            self.t_total.place(w / 2, top, "center")
            self.t_total.show(True)
            top = h * 0.30
        else:
            self.t_total.show(False)

        need = max(1, int(rc.max_rows))
        if rows:
            usable = h * 0.92 - top
            step = usable / max(1, need)
            # 左：排名数字；中：昵称；右：贡献分
            num_x = w * 0.16
            name_x = w * 0.42
            score_x = w * 0.86
            for i in range(need):
                cy = top + step * (i + 0.5)
                if i < len(rows):
                    r = rows[i]
                    self._num_items[i].configure(
                        oc.font(rc.rank_size), rc.rank_num_color,
                        oc.outline_color, max(2, oc.outline_width - 2))
                    self._num_items[i].set(str(r.get("rank") or (i + 1)))
                    self._num_items[i].place(num_x, cy, "center")
                    self._num_items[i].show(True)

                    self._rows[i].configure(
                        oc.font(rc.rank_size), rc.rank_color,
                        oc.outline_color, max(2, oc.outline_width - 2))
                    self._rows[i].set(r.get("nick") or r.get("uid") or "")
                    self._rows[i].place(name_x, cy, "w")
                    self._rows[i].show(True)

                    self._score_items[i].configure(
                        oc.font(rc.rank_size), rc.score_color,
                        oc.outline_color, max(2, oc.outline_width - 2))
                    if rc.show_score:
                        self._score_items[i].set(str(r.get("score") or ""))
                    else:
                        self._score_items[i].set("")
                    self._score_items[i].place(score_x, cy, "e")
                    self._score_items[i].show(rc.show_score)
                else:
                    for it in (self._num_items[i], self._rows[i],
                               self._score_items[i]):
                        it.set("")
                        it.show(False)
        else:
            # 没数据：显示占位提示
            for i in range(need):
                for it in (self._num_items[i], self._rows[i],
                           self._score_items[i]):
                    it.set("")
                    it.show(False)
            self.t_total.set("等待榜数据…")
            self.t_total.place(w / 2, h * 0.45, "center")
            self.t_total.show(True)

    # ================================================================== #
    # 事件
    # ================================================================== #

    def _on_rank_changed(self, payload=None) -> None:
        self._refresh_rows()

    def _on_config_changed(self, payload=None) -> None:
        self.rc = self.session.cfg.rank
        self.oc = self.session.cfg.overlay
        self._locked = self.rc.locked
        self._apply_window_attrs()
        self._apply_bg()
        self.relayout()

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except Exception:
            pass
