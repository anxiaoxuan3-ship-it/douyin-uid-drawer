# -*- coding: utf-8 -*-
"""展示窗口（给直播伴侣「窗口捕获」抓的那个窗口）。

三种背景模式
------------
* **纯色背景（默认，推荐）**：整个窗口铺一个绝对纯色（默认纯绿 #00AA00）。
  直播伴侣 →「添加素材」→「窗口捕获」→ 选本窗口 → 开「绿幕抠图」→
  用吸管点一下底色 → 调「相似度 / 平滑度」到边缘干净。这是实测最稳的方案。
* **真透明**：``-transparentcolor`` 把底色抠成透明，自己屏幕上直接看穿。
  但窗口采集（BitBlt / WGC）拿不到 alpha，直播伴侣里多半显示成黑块或色块，
  所以只当作"自己预览"用。
* **图片背景**：拿一张图当底。

文字一律带描边，保证压在任何画面上都看得清。
"""

from __future__ import annotations

import os
import tkinter as tk
import tkinter.font as tkfont
from typing import List, Optional

from . import bus as B
from .config import BgMode, OverlayConfig
from .draw import DrawResult
from .state import DanmakuRecord, Session
from . import winutil as W
from .winutil import (describe as win_describe, grab_window,
                      list_capturable_windows, make_capturable,
                      root_hwnd, set_layered)

# 描边偏移（8 向）
_OUT8 = ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0))
_OUT4 = ((0, -1), (0, 1), (-1, 0), (1, 0))


def pick_font_family(root: tk.Misc, want: str) -> str:
    """挑一个系统里真实存在的字体。"""
    try:
        fams = set(tkfont.families(root))
    except Exception:
        return want
    for cand in (want, "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI",
                 "SimHei", "Arial"):
        if cand in fams:
            return cand
    return "TkDefaultFont"


class _Outlined:
    """带描边的一段文字（共用一组 canvas item，更新时只改文字不动结构）。"""

    def __init__(self, canvas: tk.Canvas, font: tuple, fill: str,
                 outline: str, outline_w: int) -> None:
        self.c = canvas
        self.font = font
        self.fill = fill
        self.outline = outline
        self.ow = max(0, int(outline_w))
        self._x = 0
        self._y = 0
        self._anchor = "center"
        self._text = ""
        self._shadow_ids: List[int] = []
        self._main_id: Optional[int] = None
        self._ensure()

    # ---- 内部 ---------------------------------------------------------- #

    def _offsets(self):
        if self.ow <= 0:
            return ()
        if self.ow == 1:
            return _OUT4
        return _OUT8

    def _ensure(self) -> None:
        """重建 canvas item（字体/颜色变了才需要）。"""
        self._destroy()
        offs = self._offsets()
        step = max(1, self.ow)
        for dx, dy in offs:
            self._shadow_ids.append(self.c.create_text(
                self._x + dx * step, self._y + dy * step, text=self._text,
                font=self.font, fill=self.outline, anchor=self._anchor))
        self._main_id = self.c.create_text(
            self._x, self._y, text=self._text,
            font=self.font, fill=self.fill, anchor=self._anchor)

    def _destroy(self) -> None:
        for i in self._shadow_ids:
            try:
                self.c.delete(i)
            except Exception:
                pass
        self._shadow_ids.clear()
        if self._main_id is not None:
            try:
                self.c.delete(self._main_id)
            except Exception:
                pass
            self._main_id = None

    # ---- 对外 ---------------------------------------------------------- #

    def configure(self, font: tuple, fill: str, outline: str, ow: int) -> None:
        if (font, fill, outline, ow) != (self.font, self.fill,
                                         self.outline, self.ow):
            self.font, self.fill, self.outline, self.ow = font, fill, outline, ow
            self._ensure()

    def place(self, x: float, y: float, anchor: str = "center") -> None:
        self._x, self._y, self._anchor = x, y, anchor
        offs = self._offsets()
        step = max(1, self.ow)
        for (dx, dy), item in zip(offs, self._shadow_ids):
            self.c.coords(item, x + dx * step, y + dy * step)
        if self._main_id is not None:
            self.c.coords(self._main_id, x, y)
        for item in self._shadow_ids + ([self._main_id] if self._main_id else []):
            self.c.itemconfig(item, anchor=anchor)

    def set(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        for item in self._shadow_ids + ([self._main_id] if self._main_id else []):
            self.c.itemconfig(item, text=text)

    def set_fast(self, text: str) -> None:
        """快速滚动模式：只改主文字、跳过描边层。

        抽号滚动的每一帧都调这个 —— 描边是 8 个影子 item，逐帧刷新它们
        是滚动撕裂/掉帧的主要来源。快速滚动时观众看不清描边，直接把描边层
        藏起来、只更新单个主文字 item，重绘开销降到原来的约 1/9，
        滚动会明显顺滑。定格后再恢复描边。
        """
        self._text = text
        for item in self._shadow_ids:
            self.c.itemconfig(item, state="hidden")
        if self._main_id is not None:
            self.c.itemconfig(self._main_id, text=text, state="normal")

    def restore_outline(self) -> None:
        """恢复描边层（滚动结束、定格时调用）。"""
        for item in self._shadow_ids:
            self.c.itemconfig(item, state="normal")

    def show(self, yes: bool = True) -> None:
        state = "normal" if yes else "hidden"
        for item in self._shadow_ids + ([self._main_id] if self._main_id else []):
            self.c.itemconfig(item, state=state)

    def destroy(self) -> None:
        self._destroy()


class OverlayWindow:
    def __init__(self, master: tk.Misc, session: Session) -> None:
        self.session = session
        self.oc: OverlayConfig = session.cfg.overlay

        self.win = tk.Toplevel(master)
        self.win.title(self.oc.window_title)
        self.win.configure(bg=self.oc.bg_color)

        self.family = pick_font_family(master, self.oc.font_family)
        self.oc.font_family = self.family

        self.canvas = tk.Canvas(self.win, highlightthickness=0, bd=0,
                                bg=self.oc.bg_color)
        self.canvas.pack(fill="both", expand=True)

        self._bg_image = None          # PhotoImage 引用（防被 GC）
        self._bg_item = None
        self._anim_job = None
        self._anim_frame = 0
        self._anim_queue: List[str] = []
        self._anim_start = 0.0
        self._winner_text = ""
        self._drag = (0, 0)
        self._visible = True
        self._locked = self.oc.locked
        self.hwnd = 0

        # 文字元素
        F = self.oc.font
        self.t_title = _Outlined(self.canvas, F(self.oc.title_size),
                                 self.oc.title_color, self.oc.outline_color,
                                 self.oc.outline_width)
        self.t_uid = _Outlined(self.canvas, F(self.oc.uid_size),
                               self.oc.uid_color, self.oc.outline_color,
                               self.oc.outline_width)
        self.t_count = _Outlined(self.canvas, F(self.oc.count_size),
                                 self.oc.count_color, self.oc.outline_color,
                                 max(2, self.oc.outline_width - 2))
        self.t_feed = _Outlined(self.canvas, F(self.oc.feed_size),
                                self.oc.feed_color, self.oc.outline_color, 2)

        self._apply_window_attrs()
        self._apply_bg()
        self._bind()

        self.canvas.bind("<Configure>", lambda e: self.relayout())

        session.bus.on(B.DRAW_START, self._on_draw_start)
        session.bus.on(B.DRAW_DONE, self._on_draw_done)
        session.bus.on(B.POOL_CHANGED, self._on_pool_changed)
        session.bus.on(B.DANMAKU, self._on_danmaku)
        session.bus.on(B.CONFIG_CHANGED, self._on_config_changed)

        self.show_idle()
        self.relayout()

    # ================================================================== #
    # 窗口属性
    # ================================================================== #

    def _apply_window_attrs(self) -> None:
        self.win.attributes("-topmost", bool(self.oc.always_on_top))
        try:
            self.win.overrideredirect(bool(self.oc.borderless))
        except Exception:
            pass
        # overrideredirect 切换后需要重新置顶
        self.win.attributes("-topmost", bool(self.oc.always_on_top))
        self.win.title(self.oc.window_title)
        try:
            self.win.geometry(self.oc.geom)
        except Exception:
            self.win.geometry("1000x560+400+300")
        self.ensure_capturable()

    # ------------------------------------------------------------------ #
    # 让「窗口捕获」能列到这个窗口
    # ------------------------------------------------------------------ #

    def ensure_capturable(self, verbose: bool = False) -> dict:
        """给窗口补上采集软件需要的样式。

        无边框窗口只有 WS_POPUP，既没有 WS_CAPTION 也没有 WS_EX_APPWINDOW，
        会被「Alt+Tab 式」过滤挡掉 —— 直播伴侣的下拉列表里就看不到它。
        这里补上 WS_EX_APPWINDOW，并顺手清掉 WS_EX_TOOLWINDOW。
        """
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
                f"展示窗句柄 {hwnd}  样式 "
                f"{'+'.join(info.get('style_names') or [])} / "
                f"{'+'.join(info.get('exstyle_names') or [])}  "
                f"可被采集={'是' if info.get('capturable') else '否'}")
        return info

    def capture_report(self) -> str:
        """给主播看的「这个窗口能不能被采集」体检报告。"""
        info = self.ensure_capturable() or win_describe(root_hwnd(self.win))
        lines = [
            f"窗口标题：{info.get('title') or '(空)'}",
            f"窗口类名：{info.get('class')}",
            f"窗口句柄：{info.get('hwnd')}",
        ]
        r = info.get("rect") or (0, 0, 0, 0)
        lines += [
            f"位置大小：{r[0]},{r[1]}  {r[2] - r[0]}x{r[3] - r[1]}",
            "",
            "窗口样式：" + ("、".join(info.get("style_names") or []) or "无"),
            "扩展样式：" + ("、".join(info.get("exstyle_names") or []) or "无"),
            "",
            f"● 采集软件能不能列到它：{'能' if info.get('capturable') else '不能'}",
        ]
        if not info.get("capturable"):
            if not info.get("has_caption") and not info.get("has_appwindow"):
                lines.append("  原因：既没有标题栏(WS_CAPTION)也没有 "
                             "WS_EX_APPWINDOW，被列表过滤挡掉了。")
            if info.get("has_toolwindow"):
                lines.append("  原因：带 WS_EX_TOOLWINDOW，"
                             "很多软件会排除工具窗口。")
        if info.get("has_layered"):
            lines.append("  注意：带 WS_EX_LAYERED（真透明模式），分层窗口用 "
                         "BitBlt 抓不到画面，多半是黑屏。")

        lines.append("")
        for method, label in (("bitblt", "传统窗口采集(BitBlt)"),
                              ("printwindow", "PrintWindow")):
            res = grab_window(int(info.get("hwnd") or 0), method)
            if not res:
                lines.append(f"{label}：抓取失败")
            elif not res.get("ok"):
                lines.append(f"{label}：抓取失败（{res.get('reason')}）")
            else:
                tail = ("→ 像是黑屏/纯色 ❌" if res.get("blank")
                        else "→ 画面正常 ✅")
                lines.append(f"{label}：{res['w']}x{res['h']}  "
                             f"颜色数 {res.get('colors')}  "
                             f"平均色 {res.get('avg')}  {tail}")

        wins = list_capturable_windows()
        inside = any(w["hwnd"] == info.get("hwnd") for w in wins)
        lines += ["",
                  f"采集列表里共有 {len(wins)} 个窗口，本窗口"
                  + ("在里面 ✅" if inside else "不在里面 ❌")]
        return "\n".join(lines)

    def place_again(self) -> None:
        """主窗口映射完成之后再套一次窗口属性/几何。

        无边框 + 置顶的 Toplevel 如果在地图之前设 geometry，容易被窗口管理器
        忽略或改回去，所以启动完成时补设一次。
        """
        self.win.title(self.oc.window_title)
        try:
            self.win.geometry(self.oc.geom)
        except Exception:
            pass
        self.win.attributes("-topmost", bool(self.oc.always_on_top))
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        self.ensure_capturable()
        self.relayout()

    def _apply_bg(self) -> None:
        mode = self.oc.bg_mode
        color = self.oc.bg_color

        try:
            self.win.attributes("-transparentcolor", "")
        except Exception:
            pass

        # 背景渲染走公共函数（和参与名单窗共用一份实现）
        self._bg_item = W.apply_bg(
            self.win, self.canvas, self._bg_item, self.oc.image_path,
            color, mode, BgMode.TRANSPARENT)
        self._bg_image = getattr(self.canvas, "_bg_photo", None)

        # 背景模式会改变 WS_EX_LAYERED，样式改完必须重补一次采集样式
        self.ensure_capturable()

    def _bind(self) -> None:
        for w in (self.win, self.canvas):
            w.bind("<Button-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<Button-3>", self._on_menu)
            w.bind("<Double-Button-1>", lambda e: self.toggle_lock())
        self.win.bind("<Configure>", self._on_configure)

    def _on_configure(self, ev) -> None:
        if ev.widget is not self.win:
            return

    # ================================================================== #
    # 拖动 / 右键菜单
    # ================================================================== #

    def _on_press(self, ev) -> None:
        self._drag = (ev.x_root - self.win.winfo_x(),
                      ev.y_root - self.win.winfo_y())

    def _on_drag(self, ev) -> None:
        if self._locked:
            return
        x = ev.x_root - self._drag[0]
        y = ev.y_root - self._drag[1]
        self.win.geometry(f"+{x}+{y}")

        # ★ 只存**位置**，尺寸始终沿用配置里那个（拖动本来也改不了大小）。
        #   原来这里用 winfo_width()/winfo_height() 重新拼 geom，
        #   一旦窗口当时被系统改过（缩放、分辨率变化、远程桌面重连），
        #   就会把那个错误尺寸**永久写进配置**，之后每次启动都变小。
        w, h = self._配置尺寸()
        self.oc.geom = f"{w}x{h}+{x}+{y}"
        # 通知主程序「配置变了，该存盘了」——
        # 以前拖动只改内存不存盘，重启后位置就丢了。
        # 这里发 DIRTY 而不是 CHANGED：只要存盘，不要重新应用外观（否则会打架）。
        self.session.bus.emit(B.CONFIG_DIRTY, None)

    def _配置尺寸(self) -> tuple:
        """从 oc.geom 里取出宽高；取不到就用默认 1100x470。"""
        try:
            w_h = (self.oc.geom or "").split("+")[0]
            w, h = w_h.split("x")
            w, h = int(w), int(h)
            if w >= 200 and h >= 120:
                return (w, h)
        except Exception:
            pass
        return (1100, 470)

    def reset_size(self, 提示: bool = True) -> None:
        """把展示窗恢复成默认大小，并挪回屏幕中间偏上的位置。

        什么时候用：窗口变得特别小／特别大、或者跑到屏幕外面点不到的时候。
        重置后会把新位置存进配置，下次启动也正常。
        """
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        w, h = 1100, 470
        # 屏幕太小时按比例缩一下，但留出最小可用尺寸
        if w > sw - 100:
            w = max(640, sw - 100)
        if h > sh - 200:
            h = max(360, sh - 200)
        x = max(20, (sw - w) // 2)
        y = max(40, int(sh * 0.10))

        self.oc.geom = f"{w}x{h}+{x}+{y}"
        try:
            self.win.geometry(self.oc.geom)
            self.win.update_idletasks()
        except Exception:
            pass
        self.relayout()

        # 存盘，并让界面上的坐标显示同步
        try:
            self.session.bus.emit(B.CONFIG_DIRTY, None)
        except Exception:
            pass
        if 提示:
            try:
                self.session.log(f"展示窗口已重置为 {w}x{h}")
                self.session.bus.emit(B.STATUS, f"展示窗口已重置为 {w}x{h}")
            except Exception:
                pass

    def _on_menu(self, ev) -> None:
        m = tk.Menu(self.win, tearoff=0)
        m.add_command(label="锁定位置" if not self._locked else "解锁位置",
                      command=self.toggle_lock)
        m.add_command(label="显示/隐藏标题栏",
                      command=self.toggle_borderless)
        m.add_checkbutton(label="总在最前", onvalue=True, offvalue=False,
                          variable=tk.BooleanVar(value=self.oc.always_on_top),
                          command=self._toggle_topmost)
        m.add_separator()
        m.add_command(label="重置窗口大小和位置", command=self.reset_size)
        m.add_command(label="重置为等待状态", command=self.show_idle)
        m.add_command(label="隐藏展示窗口", command=self.hide)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def toggle_lock(self) -> None:
        self._locked = not self._locked
        self.oc.locked = self._locked
        self.session.log("展示窗口已" + ("锁定" if self._locked else "解锁"))

    def toggle_borderless(self) -> None:
        self.oc.borderless = not self.oc.borderless
        self._apply_window_attrs()
        self.session.log("标题栏已" + ("隐藏" if self.oc.borderless else "显示"))

    def _toggle_topmost(self) -> None:
        self.oc.always_on_top = not self.oc.always_on_top
        self.win.attributes("-topmost", self.oc.always_on_top)

    def hide(self) -> None:
        self.win.withdraw()
        self._visible = False

    def show(self) -> None:
        self.win.deiconify()
        self.win.attributes("-topmost", bool(self.oc.always_on_top))
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
    # 排版
    # ================================================================== #

    def relayout(self) -> None:
        w = self.canvas.winfo_width() or int(self.oc.geom.split("x")[0])
        h = self.canvas.winfo_height() or 560
        oc = self.oc
        fam = self.family

        # 标题
        self.t_title.configure(oc.font(oc.title_size), oc.title_color,
                               oc.outline_color, oc.outline_width)
        self.t_title.set(oc.title_text)
        title_y = int(h * 0.11)
        self.t_title.place(w / 2, title_y, "center")
        self.t_title.show(oc.show_title and bool(oc.title_text))

        # 主 UID
        self.t_uid.configure(oc.font(oc.uid_size), oc.uid_color,
                             oc.outline_color, oc.outline_width)
        self.t_uid.place(w / 2, h * 0.45, "center")

        # 参与人数
        self.t_count.configure(oc.font(oc.count_size), oc.count_color,
                               oc.outline_color, max(2, oc.outline_width - 2))
        # 原来在 0.72（下面还有中奖名单占着 0.87）。
        # 中奖名单删掉之后，往下挪到 0.76，配合更大的字号看起来更平衡。
        self.t_count.place(w / 2, h * 0.76, "center")
        self.t_count.show(oc.show_count)

        # 右下角弹幕上墙
        self.t_feed.configure(oc.font(oc.feed_size), oc.feed_color,
                              oc.outline_color, 2)
        self.t_feed.place(w - int(w * 0.02), h * 0.08, "ne")
        self.t_feed.show(oc.show_feed)

        if self._bg_item is not None and self._bg_image is not None:
            self.canvas.coords(self._bg_item, w / 2, h / 2)
            self.canvas.itemconfig(self._bg_item, anchor="center")

    def _fit_uid_font(self, text: str) -> tuple:
        """UID 太长就自动缩字号，别让它溢出窗口。"""
        oc = self.oc
        w = self.canvas.winfo_width() or 1000
        limit = w * 0.94
        size = oc.uid_size
        while size > 24:
            f = tkfont.Font(family=self.family, size=size, weight=oc.font_weight)
            if f.measure(text) <= limit:
                break
            size -= 4
        return oc.font(size)

    # ================================================================== #
    # 状态显示
    # ================================================================== #

    def show_idle(self) -> None:
        self._cancel_anim()
        self.t_uid.configure(oc_font(self.oc), self.oc.idle_color,
                             self.oc.outline_color, self.oc.outline_width)
        self.t_uid.set(self.oc.idle_text)
        self.t_uid.restore_outline()
        self._winner_text = ""
        self._refresh_count()

    def _refresh_count(self) -> None:
        oc = self.oc
        n = len(self.session.pool)
        self.t_count.set(f"{oc.count_prefix} {n}")
        self.t_count.show(oc.show_count)

    def _refresh_feed(self) -> None:
        oc = self.oc
        if not oc.show_feed:
            self.t_feed.show(False)
            return
        rows = [r.uid for r in list(self.session.recent) if r.uid][-oc.feed_limit:]
        self.t_feed.set("\n".join(reversed(rows)))
        self.t_feed.show(True)

    # ================================================================== #
    # 事件
    # ================================================================== #

    def _on_danmaku(self, rec: DanmakuRecord) -> None:
        if self.oc.show_feed:
            self._refresh_feed()
        self._refresh_count()

    def _on_pool_changed(self, payload=None) -> None:
        self._refresh_count()

    def _on_config_changed(self, payload=None) -> None:
        self.oc = self.session.cfg.overlay
        self._apply_window_attrs()
        self._apply_bg()
        self._locked = self.oc.locked
        self.relayout()
        if not self._anim_job and not self._winner_text:
            self.show_idle()

    def _on_draw_start(self, result: DrawResult) -> None:
        self._start_anim(result)

    def _on_draw_done(self, result: DrawResult) -> None:
        # 动画可能因为掉帧没跑到终点，这里强制定格成最终结果
        self._cancel_anim()
        self._land(result)

    # ================================================================== #
    # 滚动动画
    # ================================================================== #

    def _cancel_anim(self) -> None:
        if self._anim_job is not None:
            try:
                self.win.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None

    def _start_anim(self, result: DrawResult) -> None:
        self._cancel_anim()
        oc = self.oc
        self._anim_queue = [u for u in (result.reel or []) if u]
        if not self._anim_queue:
            self._land(result)
            return

        self._final_text = "  ".join(result.winner_uids)
        self.t_uid.configure(self._fit_uid_font(self._final_text),
                             "#CFD8FF", oc.outline_color, oc.outline_width)
        self.t_uid.show(True)
        self._anim_frame = 0
        self._anim_total = max(1, int(self.session.cfg.animation_ms))
        self._anim_elapsed = 0

        self._tick()

    def _tick(self) -> None:
        """缓出节奏：前 70% 飞快，最后 30% 明显减速。

        滚动阶段用 ``set_fast``（跳过描边层）降低每帧重绘开销，减少撕裂。
        """
        if not self._anim_queue:
            return
        total = self._anim_total
        elapsed = self._anim_elapsed
        p = min(1.0, elapsed / total)

        idx = int((p ** 0.55) * (len(self._anim_queue) - 1))
        idx = max(0, min(len(self._anim_queue) - 1, idx))
        self.t_uid.set_fast(self._anim_queue[idx])

        # 下一帧延迟
        delay = 26 + int(240 * (p ** 3.2))
        self._anim_elapsed += delay
        if self._anim_elapsed >= total:
            self._anim_job = self.win.after(delay, lambda: self._land(None))
        else:
            self._anim_job = self.win.after(delay, self._tick)

    def _land(self, result: Optional[DrawResult]) -> None:
        self._anim_job = None
        oc = self.oc
        if result is None:
            text = getattr(self, "_final_text", "") or self._winner_text
            uids = text.split("  ") if text else []
        else:
            uids = result.winner_uids
            text = "  ".join(uids)

        if not text:
            self.show_idle()
            return

        self._winner_text = text
        self.t_uid.configure(self._fit_uid_font(text), oc.uid_color,
                             oc.outline_color, oc.outline_width)
        self.t_uid.set(text)
        self.t_uid.restore_outline()
        self.t_uid.show(True)
        self._anim_hide_uid = False
        self._refresh_count()

    def destroy(self) -> None:
        self._cancel_anim()
        try:
            self.win.destroy()
        except Exception:
            pass


def oc_font(oc: OverlayConfig) -> tuple:
    return oc.font(oc.idle_size)
