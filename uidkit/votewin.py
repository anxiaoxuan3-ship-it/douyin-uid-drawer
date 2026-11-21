# -*- coding: utf-8 -*-
"""投票捕捉窗口：给观众看「这个 UID 大家怎么评价」。

独立窗口，和展示窗/名单窗一样能被直播伴侣捕获、支持绿幕抠像。

窗口内容：
    标题：投的是谁
    五档：柱状条 + 票数 + 百分比（从低到高排列）
    底部：总票数 / 当前评价

票数变动有动画：
    柱条长度平滑过渡，票数数字滚动上涨，
    刚变动的那一档会闪一下。
"""

import tkinter as tk
from typing import Dict, List, Optional

from . import bus as B
from . import winutil as W
from .config import BgMode
from .overlay import _Outlined, pick_font_family
from .vote import VoteBox, 档位, 档位颜色, 档位数字

#: 柱条动画每帧的过渡速度（0~1，越大越快）
过渡速度 = 0.22

#: 变动高亮持续多少秒
高亮秒 = 1.2


class VoteWindow:
    """投票窗口。"""

    def __init__(self, master: tk.Misc, session) -> None:
        self.session = session
        self.vc = session.cfg.vote
        self.oc = session.cfg.overlay      # 背景/字体沿用展示窗

        self.win = tk.Toplevel(master)
        self.win.title(self.vc.window_title)

        self.family = pick_font_family(master, self.oc.font_family)
        self.canvas = tk.Canvas(self.win, highlightthickness=0, bd=0,
                                bg=self.oc.bg_color)
        self.canvas.pack(fill="both", expand=True)

        self._bg_image = None
        self._bg_item = None
        self._drag = (0, 0)
        self._visible = True
        self._locked = bool(self.vc.locked)
        self.hwnd = 0

        # ---- 动画状态 ----
        #: 每档当前显示的「柱长比例」和「显示票数」，逐帧逼近真实值
        self._bar: Dict[str, float] = {d: 0.0 for d in 档位}
        self._shown: Dict[str, float] = {d: 0.0 for d in 档位}
        self._highlight: Dict[str, float] = {d: 0.0 for d in 档位}
        self._last_revision = -1
        self._job: Optional[str] = None
        self._tick_ms = 33

        # ---- 文字元素 ----
        self.t_title = _Outlined(self.canvas,
                                 self.oc.font(self.vc.title_size),
                                 self.vc.title_color, self.oc.outline_color,
                                 self.oc.outline_width)
        self.t_target = _Outlined(self.canvas,
                                  self.oc.font(self.vc.target_size),
                                  self.vc.target_color, self.oc.outline_color,
                                  self.oc.outline_width)
        self.t_footer = _Outlined(self.canvas,
                                  self.oc.font(self.vc.footer_size),
                                  self.vc.footer_color, self.oc.outline_color,
                                  max(2, self.oc.outline_width - 2))

        # 每档一个文字对象
        self.t_name: Dict[str, _Outlined] = {}
        self.t_count: Dict[str, _Outlined] = {}
        self.t_pct: Dict[str, _Outlined] = {}
        for d in 档位:
            self.t_name[d] = _Outlined(
                self.canvas, self.oc.font(self.vc.style_size),
                self.vc.style_color, self.oc.outline_color,
                max(2, self.oc.outline_width - 2))
            self.t_count[d] = _Outlined(
                self.canvas, self.oc.font(self.vc.count_size),
                self.vc.count_color, self.oc.outline_color,
                max(2, self.oc.outline_width - 2))
            self.t_pct[d] = _Outlined(
                self.canvas, self.oc.font(self.vc.pct_size),
                self.vc.pct_color, self.oc.outline_color,
                max(1, self.oc.outline_width - 3))

        self._apply_window_attrs()
        self._apply_bg()
        self._bind()
        self.canvas.bind("<Configure>", lambda e: self.relayout())

        session.bus.on(B.VOTE_CHANGED, self._on_vote_changed)
        session.bus.on(B.DANMAKU, self._on_danmaku)
        session.bus.on(B.CONFIG_CHANGED, self._on_config_changed)
        session.bus.on(B.DRAW_DONE, self._on_draw_done)

        self.relayout()
        self.refresh()
        self._start_tick()

    # ================================================================== #
    # 外观
    # ================================================================== #

    def _apply_window_attrs(self) -> None:
        self.win.attributes("-topmost", bool(self.vc.always_on_top))
        try:
            self.win.overrideredirect(bool(self.vc.borderless))
        except Exception:
            pass
        self.win.attributes("-topmost", bool(self.vc.always_on_top))
        self.win.title(self.vc.window_title)
        try:
            self.win.geometry(self.vc.geom)
        except Exception:
            self.win.geometry("760x600+200+120")
        self.ensure_capturable()

    def place_again(self) -> None:
        self.win.title(self.vc.window_title)
        try:
            self.win.geometry(self.vc.geom)
        except Exception:
            pass
        self.win.attributes("-topmost", bool(self.vc.always_on_top))
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
        hwnd = W.root_hwnd(self.win)
        if not hwnd:
            return {}
        self.hwnd = hwnd
        want_layered = (self.oc.bg_mode == BgMode.TRANSPARENT)
        info = W.make_capturable(hwnd, drop_layered=not want_layered)
        if want_layered:
            W.set_layered(hwnd, True)
        if verbose:
            self.session.log(
                f"投票窗句柄 {hwnd}  可被采集="
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
    # 布局
    # ================================================================== #

    def relayout(self) -> None:
        w = self.canvas.winfo_width() or 760
        h = self.canvas.winfo_height() or 600
        vc = self.vc
        oc = self.oc

        # 标题
        self.t_title.configure(oc.font(vc.title_size), vc.title_color,
                               oc.outline_color, oc.outline_width)
        self.t_title.set(vc.title_text)
        self.t_title.place(w / 2, h * 0.075, "center")
        self.t_title.show(vc.show_title and bool(vc.title_text))

        # 投票对象
        self.t_target.configure(oc.font(vc.target_size), vc.target_color,
                                oc.outline_color, oc.outline_width)
        self.t_target.place(w / 2, h * 0.175, "center")

        # 五档：从高到低从上往下排（夯在最上面，拉完了在最下面）
        顶 = h * 0.28
        底 = h * 0.86
        行数 = len(档位)
        行高 = (底 - 顶) / 行数

        左 = w * 0.06
        名字宽 = w * 0.16
        条左 = 左 + 名字宽
        条宽 = w * 0.50
        数字左 = 条左 + 条宽 + w * 0.02

        self._布局 = {
            "左": 左, "名字宽": 名字宽, "条左": 条左, "条宽": 条宽,
            "数字左": 数字左, "顶": 顶, "行高": 行高, "宽": w, "高": h,
        }

        for i, d in enumerate(reversed(档位)):
            y = 顶 + 行高 * (i + 0.5)
            self.t_name[d].configure(
                oc.font(vc.style_size), vc.style_color,
                oc.outline_color, max(2, oc.outline_width - 2))
            # 前面标上数字，观众才知道发「1」就是这一档
            数 = 档位数字.get(d, "")
            self.t_name[d].set(f"{数} {d}" if 数 else d)
            self.t_name[d].place(左 + 名字宽 * 0.48, y, "center")

            self.t_count[d].configure(
                oc.font(vc.count_size), vc.count_color,
                oc.outline_color, max(2, oc.outline_width - 2))
            self.t_count[d].place(数字左, y, "w")

            self.t_pct[d].configure(
                oc.font(vc.pct_size), vc.pct_color,
                oc.outline_color, max(1, oc.outline_width - 3))
            self.t_pct[d].place(数字左 + w * 0.14, y, "w")

        # 底部
        self.t_footer.configure(oc.font(vc.footer_size), vc.footer_color,
                                oc.outline_color,
                                max(2, oc.outline_width - 2))
        self.t_footer.place(w / 2, h * 0.93, "center")
        self.t_footer.show(vc.show_footer)

        if self._bg_item is not None and self._bg_image is not None:
            self.canvas.coords(self._bg_item, w / 2, h / 2)
            self.canvas.itemconfig(self._bg_item, anchor="center")

    # ================================================================== #
    # 刷新
    # ================================================================== #

    def refresh(self) -> None:
        """把投票数据读出来，更新文字。柱条由 tick 做过渡动画。"""
        box: VoteBox = self.session.votes
        状态 = box.state
        vc = self.vc

        if 状态.target_uid:
            谁 = (f"{状态.target_nick}({状态.target_uid})"
                  if 状态.target_nick else 状态.target_uid)
            # 手动指定的对象标出来，免得观众以为这轮抽奖结果变了
            前 = "手动指定：" if getattr(状态, "manual", False) else "评价对象："
            self.t_target.set(f"{前}{谁}")
            self.t_target.show(True)
        else:
            # 还没开投票时，如果刚抽完奖，就把"待开启"的对象亮出来，
            # 主播一眼能看到这轮该投谁
            待 = getattr(self.session, "pending_vote_uid", "")
            待昵 = getattr(self.session, "pending_vote_nick", "")
            if 待:
                谁 = f"{待昵}({待})" if 待昵 else 待
                self.t_target.set(f"待开启投票：{谁}")
            else:
                self.t_target.set(vc.idle_text)
            self.t_target.show(True)

        总 = 状态.total
        for d in 档位:
            票 = 状态.counts.get(d, 0)
            self.t_count[d].set(str(票))
            百 = 状态.percent(d)
            self.t_pct[d].set(f"{百:.0f}%" if 总 else "")

        # 底部：票数 / 投票倒计时 / 清池倒计时 统一由 _compose_footer 拼
        self.t_footer.set(self._compose_footer())
        self.t_footer.show(vc.show_footer)

    # ================================================================== #
    # 动画
    # ================================================================== #

    def _start_tick(self) -> None:
        if self._job is not None:
            return
        self._job = self.win.after(self._tick_ms, self._tick)

    def _stop_tick(self) -> None:
        if self._job is not None:
            try:
                self.win.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self) -> None:
        self._job = None
        try:
            self._draw_bars()
        except Exception:
            pass
        try:
            self._update_countdown()
        except Exception:
            pass
        self._job = self.win.after(self._tick_ms, self._tick)

    def _compose_footer(self) -> str:
        """拼底部那行：票数/状态 · 投票剩余 · 清池倒计时。

        每一段都可能缺，缺了就跳过，中间用 · 分隔。
        """
        vc = self.vc
        片段 = []
        状态 = self.session.votes.state

        if self.session.votes.is_open:
            头 = 状态.leader()
            文 = f"共 {状态.total} 票" + (f" {头}" if 头 else "")
            秒 = float(getattr(vc, "vote_seconds", 0) or 0)
            if 秒 > 0:
                文 += f" · 投票剩 {self.session.vote_remaining():.0f}s"
            片段.append(文)
        else:
            待 = getattr(self.session, "pending_vote_uid", "")
            片段.append("等待开始投票" if 待 else vc.idle_text)

        # 参与池自动清空的倒计时
        try:
            if bool(getattr(self.session.cfg, "pool_auto_clear", False)):
                剩 = int(self.session.pool_clear_remaining())
                if 剩 > 0:
                    分, 余 = divmod(剩, 60)
                    片段.append(f"清池 {分}分{余:02d}s" if 分
                                else f"清池 {余}s")
                else:
                    片段.append("清池 即将…")
        except Exception:
            pass

        return "　·　".join(片段)

    def _update_countdown(self) -> None:
        """每帧刷新底部（投票剩余 + 清池倒计时）。"""
        try:
            self.t_footer.set(self._compose_footer())
            self.t_footer.show(self.vc.show_footer)
        except Exception:
            pass

    def _draw_bars(self) -> None:
        """画柱状条：每帧向目标值过渡，做出「票数在涨」的观感。"""
        box: VoteBox = self.session.votes
        状态 = box.state
        总 = max(1, 状态.total)
        布 = getattr(self, "_布局", None)
        if not 布:
            return

        self.canvas.delete("bar")
        self.canvas.delete("barhl")

        行高 = 布["行高"]
        条高 = max(10, 行高 * 0.42)
        条左 = 布["条左"]
        条宽 = 布["条宽"]

        需要重绘 = False
        for i, d in enumerate(reversed(档位)):
            y = 布["顶"] + 行高 * (i + 0.5)

            # 目标比例
            目标 = 状态.counts.get(d, 0) / 总
            if 状态.total == 0:
                目标 = 0.0

            # 平滑逼近
            当前 = self._bar.get(d, 0.0)
            if abs(目标 - 当前) > 0.001:
                当前 += (目标 - 当前) * 过渡速度
                self._bar[d] = 当前
                需要重绘 = True
            else:
                当前 = 目标
                self._bar[d] = 当前

            # 高亮衰减
            高 = self._highlight.get(d, 0.0)
            if 高 > 0.01:
                self._highlight[d] = max(0.0, 高 - self._tick_ms / 1000.0
                                         / 高亮秒)
                需要重绘 = True

            # 底色槽
            self.canvas.create_rectangle(
                条左, y - 条高 / 2, 条左 + 条宽, y + 条高 / 2,
                fill="#1A2233", outline="", tags="bar")

            # 实际柱
            if 当前 > 0:
                宽 = 条宽 * min(1.0, 当前)
                色 = 档位颜色.get(d, "#888888")
                self.canvas.create_rectangle(
                    条左, y - 条高 / 2, 条左 + 宽, y + 条高 / 2,
                    fill=色, outline="", tags="bar")

            # 高亮描边
            高 = self._highlight.get(d, 0.0)
            if 高 > 0.02:
                边 = 条左 + 条宽 * min(1.0, 当前)
                self.canvas.create_rectangle(
                    条左, y - 条高 / 2, 边, y + 条高 / 2,
                    fill="", outline="#FFFFFF",
                    width=max(1, int(3 * 高)), tags="barhl")

        # 柱条要垫在文字下面
        self.canvas.tag_lower("bar")
        self.canvas.tag_lower("barhl")

        # 数字滚动：显示票数也做平滑过渡
        for d in 档位:
            真 = float(状态.counts.get(d, 0))
            显示 = self._shown.get(d, 0.0)
            if abs(真 - 显示) > 0.01:
                显示 += (真 - 显示) * 0.28
                if abs(真 - 显示) < 0.5:
                    显示 = 真
                self._shown[d] = 显示
                self.t_count[d].set(str(int(round(显示))))

    # ================================================================== #
    # 事件
    # ================================================================== #

    def _on_vote_changed(self, payload=None) -> None:
        box: VoteBox = self.session.votes
        # 刚变动的档位高亮一下
        档 = getattr(box, "last_style", "")
        if 档:
            self._highlight[档] = 1.0
        self.refresh()
        self._start_tick()

    def _on_draw_done(self, result=None) -> None:
        # 抽完把窗口显示出来（如果被关掉了），让主播看见"待开启投票：xxx"
        if (bool(getattr(self.vc, "auto_show_on_draw", True))
                and not self._visible):
            try:
                self.show()
            except Exception:
                pass
        self.refresh()

    def _on_danmaku(self, rec=None) -> None:
        # 投票由 Session 统一处理，这里只负责在数据变了之后刷新
        if self.session.votes.revision != self._last_revision:
            self._last_revision = self.session.votes.revision
            self.refresh()

    def _on_config_changed(self, payload=None) -> None:
        self.vc = self.session.cfg.vote
        self.oc = self.session.cfg.overlay
        self._apply_window_attrs()
        self._apply_bg()
        self.relayout()
        self.refresh()

    # ================================================================== #
    # 交互（和另外两个窗口一致）
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
        self.vc.geom = f"{w}x{h}+{x}+{y}"
        self.session.bus.emit(B.CONFIG_DIRTY, None)

    def _配置尺寸(self) -> tuple:
        try:
            w_h = (self.vc.geom or "").split("+")[0]
            w, h = w_h.split("x")
            w, h = int(w), int(h)
            if w >= 200 and h >= 120:
                return (w, h)
        except Exception:
            pass
        return (760, 600)

    def _on_menu(self, ev) -> None:
        m = tk.Menu(self.win, tearoff=0)
        m.add_command(label="锁定位置" if not self._locked else "解锁位置",
                      command=self.toggle_lock)
        m.add_command(label="显示/隐藏标题栏", command=self.toggle_borderless)
        m.add_checkbutton(label="总在最前", onvalue=True, offvalue=False,
                          variable=tk.BooleanVar(value=self.vc.always_on_top),
                          command=self._toggle_topmost)
        m.add_separator()
        m.add_command(label="重置窗口大小和位置", command=self.reset_size)
        m.add_command(label="清空本轮投票", command=self._clear_votes)
        m.add_command(label="隐藏投票窗口", command=self.hide)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def _clear_votes(self) -> None:
        self.session.votes.reset()
        self.session.bus.emit(B.VOTE_CHANGED, None)
        self.refresh()

    def toggle_lock(self) -> None:
        self._locked = not self._locked
        self.vc.locked = self._locked
        self.session.log("投票窗口已" + ("锁定" if self._locked else "解锁"))

    def toggle_borderless(self) -> None:
        self.vc.borderless = not self.vc.borderless
        self._apply_window_attrs()

    def _toggle_topmost(self) -> None:
        self.vc.always_on_top = not self.vc.always_on_top
        self.win.attributes("-topmost", self.vc.always_on_top)

    def reset_size(self, 提示: bool = True) -> None:
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        w, h = 760, 600
        if w > sw - 100:
            w = max(480, sw - 100)
        if h > sh - 150:
            h = max(360, sh - 150)
        x = max(20, (sw - w) // 2)
        y = max(40, int(sh * 0.15))
        self.vc.geom = f"{w}x{h}+{x}+{y}"
        try:
            self.win.geometry(self.vc.geom)
            self.win.update_idletasks()
        except Exception:
            pass
        self.relayout()
        try:
            self.session.bus.emit(B.CONFIG_DIRTY, None)
        except Exception:
            pass
        if 提示:
            self.session.log(f"投票窗口已重置为 {w}x{h}")

    def hide(self) -> None:
        self._stop_tick()
        self.win.withdraw()
        self._visible = False

    def show(self) -> None:
        self.win.deiconify()
        self.win.attributes("-topmost", bool(self.vc.always_on_top))
        self._visible = True
        self._start_tick()

    def toggle_visible(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    @property
    def visible(self) -> bool:
        return self._visible

    def destroy(self) -> None:
        self._stop_tick()
        try:
            self.win.destroy()
        except Exception:
            pass
