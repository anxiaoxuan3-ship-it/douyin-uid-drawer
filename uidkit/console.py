# -*- coding: utf-8 -*-
"""主播控制台界面。

分六个标签页：抽取 / 识别设置 / 弹幕来源 / 展示窗口 / 参与池 / 日志。
所有界面更新都走主线程（通过 ``Session.bus`` + ``after`` 轮询）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Any, Callable, List, Optional

from . import bus as B
from .config import APP_NAME, BgMode, data_dir
from .normalize import ExtractConfig, Mode, extract
from .pool import Entry
from .state import STATUS_LABELS, DanmakuRecord, Session

# --------------------------------------------------------------------------- #
# 视觉设计变量
#
# 集中放这里，改一处全局生效。命名按「用途」而不是「颜色」，
# 以后想换配色只改这一块就行。
# --------------------------------------------------------------------------- #

BG = "#eef1f6"            # 窗口底色（比卡片略深，卡片才浮得起来）
CARD = "#ffffff"          # 卡片 / 输入框底色
BORDER = "#dfe3ea"        # 细边框
BORDER_STRONG = "#c7cdd8"  # 输入框等需要更明显边框的地方
TEXT = "#1b1f24"          # 主文字
MUTED = "#6b7280"         # 次要说明文字
FAINT = "#9aa1ac"         # 更淡的辅助文字
ACCENT = "#2563eb"        # 主色
ACCENT_HOVER = "#1d4ed8"  # 主色悬停
ACCENT_SOFT = "#e8efff"   # 主色浅底（选中行之类）
GOOD = "#15803d"
BAD = "#c5221f"
WARN = "#b45309"

# 统一的间距节奏（4 的倍数，视觉上更整齐）
PAD_XS, PAD_S, PAD_M, PAD_L = 4, 8, 12, 18

BG_PRESETS = [
    ("绿幕 #00AA00", "#00AA00"),
    ("品红 #FF00FF", "#FF00FF"),
    ("蓝幕 #0000FF", "#0000FF"),
    ("纯白 #FFFFFF", "#FFFFFF"),
    ("纯黑 #000000", "#000000"),
]

#: 所有可滚动页面共用一个滚轮入口（bind_all 是替换语义，不能各绑各的）
_SCROLLERS: list = []


def _wheel_dispatch(event):
    """把滚轮事件交给指针底下那个滚动页去处理。"""
    for sf in list(_SCROLLERS):
        try:
            if sf.handle_wheel(event):
                return "break"
        except Exception:
            pass
    return None


def bridge_dir() -> str:
    """抖音直连桥接脚本所在目录（实现在 config 里）。"""
    from .config import bridge_dir as _bd
    return _bd()


def find_python_for_scripts() -> Optional[str]:
    """找能跑桥接脚本的 Python（实现在 config 里，不依赖 tkinter）。"""
    from .config import find_python_for_scripts as _fp
    return _fp()


class ScrollFrame(ttk.Frame):
    """可滚动容器。把内容放进 ``.inner`` 就行。

    设置项比窗口高的时候必须有它，否则下面的选项根本够不着。
    """

    _bound = False          # 全局只注册一次滚轮分发器

    def __init__(self, master, padding: int = 14) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=BG)
        self.vs = ttk.Scrollbar(self, orient="vertical",
                                command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vs.set)

        self.inner = ttk.Frame(self.canvas, padding=padding)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")

        self.vs.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        # 注意：这里**不能**各自 bind_all。
        # Tk 的 bind_all 是「替换」而不是「追加」，三个滚动页各绑一次的话
        # 后绑的会把先绑的顶掉，结果只有最后建出来的那一页滚轮有效。
        # 所以只注册一个全局分发器，由它挑出指针底下的那一页去滚。
        _SCROLLERS.append(self)
        if not ScrollFrame._bound:
            self.canvas.bind_all("<MouseWheel>", _wheel_dispatch)
            ScrollFrame._bound = True

    # ------------------------------------------------------------------ #

    def _on_inner(self, _e=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e) -> None:
        self.canvas.itemconfigure(self._win, width=e.width)

    def _blocks_scroll(self, widget) -> bool:
        """指针是不是落在「自己会滚」的控件上（这类要让它自己处理）。"""
        w = widget
        while w is not None and w is not self.canvas:
            try:
                cls = w.winfo_class()
            except Exception:
                return False
            if cls in ("Text", "Listbox", "Treeview", "TCombobox", "TSpinbox"):
                return True
            try:
                w = w.master
            except Exception:
                return False
        return False

    def handle_wheel(self, event) -> bool:
        """滚轮事件轮到本容器就滚一下；返回 True 表示已处理。"""
        try:
            if not self.canvas.winfo_ismapped():
                return False
            x, y = self.canvas.winfo_pointerxy()
            cx, cy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
            cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
            if not (cx <= x <= cx + cw and cy <= y <= cy + ch):
                return False
            if self._blocks_scroll(self.canvas.winfo_containing(x, y)):
                return False
            delta = int(getattr(event, "delta", 0) or 0)
            if delta == 0:
                return False
            # 一格通常 ±120；有些鼠标/触控板不是整数倍，做个兜底
            step = max(1, int(round(abs(delta) / 120.0)))
            self.canvas.yview_scroll(-step if delta > 0 else step, "units")
            return True
        except Exception:
            return False

    def scroll_to_top(self) -> None:
        try:
            self.canvas.yview_moveto(0.0)
        except Exception:
            pass


class Console:
    def __init__(self, app, root: tk.Tk, session: Session) -> None:
        self.app = app
        self.root = root
        self.session = session
        self.cfg = session.cfg

        self._dirty = False
        self._save_job = None
        self._pool_job = None
        self._pool_rows: List[Entry] = []
        self._test_job = None
        self._log_count = 0

        self._setup_style()
        self._build()

        session.bus.on(B.LOG, self._on_log)
        session.bus.on(B.ERROR, self._on_error)
        session.bus.on(B.STATUS, self._on_status)
        session.bus.on(B.DRAW_DONE, self._on_draw_done)
        session.bus.on(B.DANMAKU, self._on_danmaku)
        session.bus.on(B.SOURCE_STATE, self._on_source_state)

        self._bind_keys()
        self.schedule_pool_refresh(0)

    # ================================================================== #
    # 样式与骨架
    # ================================================================== #

    def _setup_style(self) -> None:
        r = self.root
        try:
            r.configure(bg=BG)
        except Exception:
            pass

        style = ttk.Style(r)
        # ★ 用 clam 而不是 vista：
        #   vista 是 Windows 原生外观，好看但**几乎不可定制**（背景色、边框、
        #   圆角全都改不动，设了也被忽略）。clam 完全由 Tk 自己画，
        #   所有细节都能控制 —— 要做统一的扁平现代风就必须用它。
        for theme in ("clam", "vista", "winnative"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        fam = self.cfg.overlay.font_family or "Microsoft YaHei UI"
        try:
            avail = set(tkfont.families(r))
            if fam not in avail:
                for c in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei"):
                    if c in avail:
                        fam = c
                        break
        except Exception:
            pass
        self.ui_family = fam

        try:
            self._setup_styles(style, fam)
        except Exception:
            traceback.print_exc()

        r.option_add("*Font", (fam, 10))

    def _setup_styles(self, style: ttk.Style, fam: str) -> None:
        """把整套 ttk 样式配好。

        分三类：
          * 容器类（Frame / LabelFrame / Notebook）—— 底色、边框、留白
          * 文字类（各种 TLabel）—— 字号层级和颜色
          * 交互类（Button / Entry / Checkbutton / Treeview）—— 悬停和选中反馈
        """
        # ---- 基础 ---------------------------------------------------------- #
        # ★ 全站统一成「白底 + 细边框分区」：
        #   之前的做法是「灰底页面 + 白卡片」，但设置类页面里有 15 处
        #   LabelFrame 和大量默认样式的标签/勾选框，它们用的是默认背景色，
        #   一改成白卡片就会在卡片上挖出一块块灰斑。
        #   统一白底之后，分区靠边框来区分，所有控件自动一致，不用逐个改。
        style.configure(".", font=(fam, 10), background=CARD, foreground=TEXT)
        style.configure("TFrame", background=CARD)
        style.configure("TLabel", background=CARD, foreground=TEXT)
        style.configure("Page.TFrame", background=BG)
        style.configure("Page.TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TFrame", background=CARD)

        # ---- 卡片 / 分区 ---------------------------------------------------- #
        style.configure("Card.TLabelframe", background=CARD,
                        borderwidth=1, relief="solid",
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        padding=(PAD_M, 18, PAD_M, PAD_M))
        style.configure("Card.TLabelframe.Label", background=CARD,
                        foreground=TEXT, font=(fam, 11, "bold"))
        # 默认的 LabelFrame 也做成同样的卡片外观 ——
        # 这样设置页里那 15 处 ttk.LabelFrame(...) 不用改一行代码就统一了
        # ★ padding 上边特意给到 18：clam 把 LabelFrame 的标题画在边框线上，
        #   上边距不够的话第一个控件会被标题压住（「识别模式」那个单选按钮就被压过）。
        style.configure("TLabelframe", background=CARD,
                        borderwidth=1, relief="solid",
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        padding=(PAD_M, 18, PAD_M, PAD_M))
        style.configure("TLabelframe.Label", background=CARD, foreground=TEXT,
                        font=(fam, 11, "bold"))

        # ---- 文字层级 ------------------------------------------------------- #
        style.configure("Hint.TLabel", background=CARD, foreground=MUTED,
                        font=(fam, 9))
        style.configure("HintCard.TLabel", background=CARD, foreground=MUTED,
                        font=(fam, 9))
        style.configure("Faint.TLabel", background=CARD, foreground=FAINT,
                        font=(fam, 9))
        style.configure("SectionHint.TLabel", background=CARD, foreground=FAINT,
                        font=(fam, 9))
        style.configure("Big.TLabel", background=CARD, foreground=TEXT,
                        font=(fam, 19, "bold"))
        style.configure("Stat.TLabel", background=CARD, foreground=MUTED,
                        font=(fam, 9))
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT,
                        font=(fam, 11, "bold"))
        style.configure("Good.TLabel", background=CARD,
                        foreground=GOOD, font=(fam, 9))
        style.configure("Bad.TLabel", background=CARD, foreground=BAD,
                        font=(fam, 9))
        style.configure("FieldLabel.TLabel", background=CARD,
                        foreground=MUTED, font=(fam, 9))

        # ---- 按钮：主次分明 -------------------------------------------------- #
        # 主按钮：实心主色、白字、无边框、悬停加深
        style.configure("Primary.TButton", font=(fam, 15, "bold"),
                        background=ACCENT, foreground="#ffffff",
                        borderwidth=0, focusthickness=0, padding=(22, 12),
                        relief="flat")
        style.map("Primary.TButton",
                  background=[("pressed", ACCENT_HOVER),
                              ("active", ACCENT_HOVER)])

        # 默认按钮 = 次要按钮的样子。
        # ★ 这么做是为了让设置页里那些 ttk.Button(...) **不用改一行代码**
        #   就统一成新外观 —— 全项目有几十个默认按钮。
        style.configure("TButton", font=(fam, 10),
                        background=CARD, foreground=TEXT,
                        bordercolor=BORDER_STRONG, borderwidth=1,
                        focusthickness=0, padding=(12, 6), relief="solid")
        style.map("TButton",
                  background=[("pressed", "#e6e9ef"), ("active", "#f2f4f8")],
                  bordercolor=[("active", ACCENT)],
                  foreground=[("disabled", FAINT)])

        # 次按钮：显式指定时和默认完全一致（保留这个名字给主页面用）
        style.configure("Secondary.TButton", font=(fam, 10),
                        background=CARD, foreground=TEXT,
                        bordercolor=BORDER_STRONG, borderwidth=1,
                        focusthickness=0, padding=(12, 6), relief="solid")
        style.map("Secondary.TButton",
                  background=[("pressed", "#e6e9ef"), ("active", "#f2f4f8")],
                  bordercolor=[("active", ACCENT)])

        # 危险操作（清空参与池）单独一个色，避免误点
        style.configure("Danger.TButton", font=(fam, 10),
                        background=CARD, foreground=BAD,
                        bordercolor="#f0c9c7", borderwidth=1,
                        focusthickness=0, padding=(12, 6), relief="solid")
        style.map("Danger.TButton",
                  background=[("active", "#fdf2f2")],
                  bordercolor=[("active", BAD)])

        # ---- 标签页：扁平 + 选中项加粗着色 ------------------------------------ #
        # bordercolor/lightcolor/darkcolor 都设成底色，消掉 clam 默认画的那圈
        # 边框 —— 否则页签下面会出现一条和卡片不搭的灰边线。
        style.configure("TNotebook", background=BG,
                        borderwidth=0, relief="flat",
                        bordercolor=BG, lightcolor=BG, darkcolor=BG,
                        tabmargins=(4, 6, 0, 0))
        style.configure("TNotebook.Tab", font=(fam, 10), padding=(18, 9),
                        background=BG, foreground=MUTED, borderwidth=0,
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.map("TNotebook.Tab",
                  background=[("selected", CARD)],
                  foreground=[("selected", ACCENT)],
                  expand=[("selected", (0, 0, 0, 0))])
        # 选中的那个页签用粗体，层次更清楚
        try:
            style.configure("Selected.TNotebook.Tab", font=(fam, 10, "bold"))
        except Exception:
            pass

        # ---- 输入类 ---------------------------------------------------------- #
        style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER_STRONG,
                        lightcolor=CARD, darkcolor=CARD, borderwidth=1,
                        padding=5)
        style.map("TEntry", bordercolor=[("focus", ACCENT)])
        style.configure("TSpinbox", fieldbackground=CARD,
                        bordercolor=BORDER_STRONG, lightcolor=CARD,
                        darkcolor=CARD, borderwidth=1, padding=3,
                        arrowcolor=MUTED)
        style.map("TSpinbox", bordercolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=CARD,
                        bordercolor=BORDER_STRONG, lightcolor=CARD,
                        darkcolor=CARD, borderwidth=1, padding=3)
        style.map("TCombobox", bordercolor=[("focus", ACCENT)])

        # ---- 勾选框：悬停时主色描边 ------------------------------------------ #
        style.configure("TCheckbutton", background=BG, foreground=TEXT,
                        focusthickness=0, focuscolor=BG, padding=(2, 3))
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  foreground=[("active", ACCENT)])
        # 卡片里的勾选框要白底，否则会在卡片上挖出一块灰
        style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT,
                        focusthickness=0, focuscolor=CARD, padding=(2, 3))
        style.map("Card.TCheckbutton",
                  background=[("active", CARD)],
                  foreground=[("active", ACCENT)])
        # 卡片里的普通文字标签
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)

        # ★ 单选按钮也要单独配色：不配的话它用 clam 默认的背景，
        #   放在白色卡片上会显出一条色带，看着像「标题压住了第一个选项」。
        # ★ focuscolor 必须设成卡片同色：
        #   第一行单选默认带键盘焦点，clam 会给它画一圈虚线框 ——
        #   在白底上看起来像一条横线穿过了文字。
        style.configure("TRadiobutton", background=CARD, foreground=TEXT,
                        focusthickness=0, focuscolor=CARD, padding=(2, 3))
        style.map("TRadiobutton",
                  background=[("active", CARD)],
                  foreground=[("active", ACCENT)])

        # ---- 表格：行高加大、表头浅灰、选中行用淡主色而不是刺眼的纯蓝 ---------- #
        style.configure("Treeview", rowheight=27, font=(fam, 10),
                        background=CARD, fieldbackground=CARD,
                        foreground=TEXT, borderwidth=1,
                        bordercolor=BORDER, relief="solid")
        style.map("Treeview",
                  background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", TEXT)])
        style.configure("Treeview.Heading", font=(fam, 10, "bold"),
                        background="#f7f8fb", foreground=MUTED,
                        borderwidth=1, bordercolor=BORDER, relief="flat",
                        padding=(6, 6))
        style.map("Treeview.Heading",
                  background=[("active", "#eef1f6")])

        # ---- 滚动条：细一点、去掉箭头按钮（更接近现代观感）-------------------- #
        style.configure("Vertical.TScrollbar", background="#d5dae3",
                        troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                        borderwidth=0, width=11)
        style.map("Vertical.TScrollbar",
                  background=[("active", "#b9c1ce")])
        try:
            style.layout("Vertical.TScrollbar", [
                ("Vertical.Scrollbar.trough", {"sticky": "ns",
                 "children": [("Vertical.Scrollbar.thumb",
                               {"expand": "1", "sticky": "nswe"})]})])
        except Exception:
            pass

        # ---- 分隔线 ---------------------------------------------------------- #
        style.configure("Sep.TFrame", background=BORDER)

    def _card(self, parent, title: str = "", **kw) -> ttk.Frame:
        """一张白色卡片（圆角做不了，用细边框 + 白底来区分层次）。

        返回**卡片内部的容器**，往里塞东西即可。
        """
        outer = tk.Frame(parent, bg=BORDER, bd=0,
                         highlightthickness=0)
        outer.pack(fill="x", **kw)
        inner = tk.Frame(outer, bg=CARD, bd=0, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        body = tk.Frame(inner, bg=CARD)
        body.pack(fill="both", expand=True, padx=PAD_L, pady=PAD_M)
        if title:
            tk.Label(body, text=title, bg=CARD, fg=TEXT,
                     font=(self.ui_family, 11, "bold")).pack(anchor="w")
        return body

    def _section(self, parent, title: str, hint: str = "",
                 pack: bool = True, **kw) -> tk.Frame:
        """一个带标题的分区（取代旧式 LabelFrame 那种标题压在边框上的样子）。

        返回值是**卡片内部的内容容器**，往里塞控件即可。
        需要 grid 摆放时传 ``pack=False``，然后对 ``.outer`` 做 grid：
        例：``sec = self._section(p, "标题", pack=False); sec.outer.grid(...)``
        """
        wrap = tk.Frame(parent, bg=CARD)
        if pack:
            wrap.pack(fill="x", **kw)
        head = tk.Frame(wrap, bg=CARD)
        head.pack(fill="x", pady=(0, PAD_S))
        # 标题前面加一小段主色竖条，比纯文字更醒目
        tk.Frame(head, bg=ACCENT, width=3, height=16).pack(side="left",
                                                           padx=(0, PAD_S))
        tk.Label(head, text=title, bg=CARD, fg=TEXT,
                 font=(self.ui_family, 11, "bold")).pack(side="left")
        if hint:
            tk.Label(head, text=hint, bg=CARD, fg=FAINT,
                     font=(self.ui_family, 9)).pack(side="left",
                                                    padx=(PAD_S, 0))
        body = self._card(wrap)
        # 把外框挂到返回的容器上，方便调用方自己 grid
        body.outer = wrap
        return body

    def _secondary_btn(self, parent, text: str, cmd, **kw):
        return ttk.Button(parent, text=text, command=cmd,
                          style="Secondary.TButton", **kw)

    def _danger_btn(self, parent, text: str, cmd, **kw):
        return ttk.Button(parent, text=text, command=cmd,
                          style="Danger.TButton", **kw)

    def _card_hint(self, parent, text: str, **kw):
        """卡片内部的说明文字（白底，用卡片专用的淡色）。"""
        lb = tk.Label(parent, text=text, bg=CARD, fg=MUTED,
                      font=(self.ui_family, 9), justify="left",
                      wraplength=kw.pop("wraplength", 900))
        lb.pack(anchor="w", **kw)
        return lb

    def _build(self) -> None:
        r = self.root
        r.title(f"{APP_NAME} · 控制台")
        r.geometry("1060x760")
        r.minsize(900, 620)

        self.nb = ttk.Notebook(r)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        # 状态栏要在标签页之前建好 —— _refresh_draw_tab 会用到 var_stats
        bar = ttk.Frame(r, padding=(12, 4))
        bar.pack(fill="x", side="bottom")
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.var_status,
                  style="Hint.TLabel").pack(side="left")
        self.var_stats = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_stats,
                  style="Hint.TLabel").pack(side="right")

        # ★ 抽取页也放进可滚动容器：
        #   这一页内容最多（统计卡片 + 设置 + 按钮 + 名单 + 手动加号），
        #   窗口小一点或系统字体偏大时，底部会被裁掉 ——
        #   之前「批量粘贴」框就只剩一条缝。可滚动之后永远够得着。
        self.sf_draw = ScrollFrame(self.nb)
        self.tab_draw = self.sf_draw.inner
        # 设置类页面内容比较长，放进可滚动容器，不然够不着底下的选项
        self.sf_extract = ScrollFrame(self.nb)
        self.sf_source = ScrollFrame(self.nb)
        self.sf_overlay = ScrollFrame(self.nb)
        self.tab_extract = self.sf_extract.inner
        self.tab_source = self.sf_source.inner
        self.tab_overlay = self.sf_overlay.inner
        self.tab_pool = ttk.Frame(self.nb, padding=14)
        self.tab_log = ttk.Frame(self.nb, padding=14)

        self.nb.add(self.sf_draw, text="  抽取  ")
        self.nb.add(self.sf_extract, text="  识别设置  ")
        self.nb.add(self.sf_source, text="  弹幕来源  ")
        self.nb.add(self.sf_overlay, text="  展示窗口  ")
        self.nb.add(self.tab_pool, text="  参与池  ")
        self.nb.add(self.tab_log, text="  日志  ")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_draw_tab()
        self._build_extract_tab()
        self._build_source_tab()
        self._build_overlay_tab()
        self._build_pool_tab()
        self._build_log_tab()
        self._restore_from_pool()

    def _restore_from_pool(self) -> None:
        """启动时把已有的中奖名单和凭证显示出来。

        ★ 以前只在「抽奖/撤销/清空」时才刷新中奖名单，所以**重启程序后
        名单是空的** —— 明明中过奖，看起来却像丢了。这是真事：
        主播重开一次就以为中奖记录没了。
        """
        try:
            self.refresh_winners()
        except Exception:
            traceback.print_exc()
        # 把最近一轮的凭证也补上，省得主播以为记录丢了
        try:
            ws = self.session.pool.winners
            if ws:
                最近轮次 = ws[-1].round_no
                本轮 = [w for w in ws if w.round_no == 最近轮次]
                self.var_proof.set(
                    f"上一轮（第{最近轮次}轮）中奖："
                    + "、".join(w.display for w in 本轮)
                    + f"　·　共 {len(ws)} 条中奖记录")
        except Exception:
            traceback.print_exc()

    # ---- 通用小工具 ---------------------------------------------------- #

    def _hint(self, parent, text: str, **kw) -> ttk.Label:
        lb = ttk.Label(parent, text=text, style="Hint.TLabel",
                       justify="left", wraplength=kw.pop("wraplength", 940))
        lb.pack(anchor="w", **kw)
        return lb

    def _row(self, parent, **kw) -> ttk.Frame:
        f = ttk.Frame(parent, **kw)
        f.pack(fill="x", pady=3)
        return f

    def _in_card(self, widget) -> bool:
        """这个控件的父容器是不是白色卡片（用来决定用哪套样式）。"""
        try:
            return str(widget.cget("bg")).lower() in (CARD.lower(), "#fff",
                                                      "#ffffff")
        except Exception:
            return False

    def _chk(self, parent, text: str, var: tk.BooleanVar,
             cmd: Optional[Callable] = None, **kw) -> ttk.Checkbutton:
        # 在卡片里要用白底样式，否则勾选框会在卡片上留下一块灰底
        st = "Card.TCheckbutton" if self._in_card(parent) else "TCheckbutton"
        c = ttk.Checkbutton(parent, text=text, variable=var,
                            command=cmd or self.mark_dirty, style=st)
        c.pack(side="left", padx=(0, 14), **kw)
        return c

    # ================================================================== #
    # 标签页 1：抽取
    # ================================================================== #

    def _build_draw_tab(self) -> None:
        p = self.tab_draw

        # ---- 顶部数据条：三个白底卡片，数字大、标签小 ---------------------- #
        stat = tk.Frame(p, bg=CARD)
        stat.pack(fill="x", pady=(0, PAD_M))
        self.var_participants = tk.StringVar(value="0")
        self.var_eligible = tk.StringVar(value="0")
        self.var_received = tk.StringVar(value="0")
        for i, (label, var) in enumerate([
            ("参与人数", self.var_participants),
            ("可抽人数", self.var_eligible),
            ("收到弹幕", self.var_received),
        ]):
            stat.columnconfigure(i, weight=1, uniform="stat")
            box = tk.Frame(stat, bg=BORDER)
            box.grid(row=0, column=i, sticky="nsew",
                     padx=(0, PAD_S) if i < 2 else 0)
            inner = tk.Frame(box, bg=CARD)
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            body = tk.Frame(inner, bg=CARD)
            body.pack(fill="both", expand=True, padx=PAD_M, pady=PAD_S)
            tk.Label(body, textvariable=var, bg=CARD, fg=TEXT,
                     font=(self.ui_family, 19, "bold")).pack(anchor="w")
            tk.Label(body, text=label, bg=CARD, fg=MUTED,
                     font=(self.ui_family, 9)).pack(anchor="w")

        # ---- 开始抽 ------------------------------------------------------- #
        ctl = self._section(p, "开始抽")
        for 子 in ctl.winfo_children():
            子.configure(bg=CARD)

        def _row_in(parent):
            """卡片内部的横向一行（白底）。"""
            f = tk.Frame(parent, bg=CARD)
            f.pack(fill="x", pady=(0, PAD_S))
            return f

        def _lbl(parent, text):
            return tk.Label(parent, text=text, bg=CARD, fg=TEXT,
                            font=(self.ui_family, 10))

        row = _row_in(ctl)
        _lbl(row, "一次抽几个人：").pack(side="left")
        self.var_count = tk.IntVar(value=self.cfg.draw_count)
        sp = ttk.Spinbox(row, from_=1, to=50, width=5, textvariable=self.var_count,
                         command=self._on_count_change)
        sp.pack(side="left", padx=(0, PAD_L))
        sp.bind("<KeyRelease>", lambda e: self._on_count_change())

        self.var_norepeat = tk.BooleanVar(value=self.cfg.no_repeat_winner)
        self._chk(row, "已中奖的不再参与", self.var_norepeat,
                  self._on_norepeat_change)
        self.var_weight = tk.BooleanVar(value=self.cfg.weighted_by_count)
        self._chk(row, "按发言次数加权", self.var_weight, self._on_weight_change)

        # ---- 观看时长加权（核心新功能）-------------------------------- #
        row = _row_in(ctl)
        self.var_watch = tk.BooleanVar(value=self.cfg.weight_by_watch)
        self._chk(row, "按观看时长加权", self.var_watch,
                  self._on_watch_change)
        _lbl(row, "看满").pack(side="left", padx=(0, PAD_XS))
        self.var_watchfull = tk.DoubleVar(value=self.cfg.watch_full_minutes)
        sp = ttk.Spinbox(row, from_=1, to=600, increment=5, width=5,
                         textvariable=self.var_watchfull,
                         command=self._on_watch_change)
        sp.pack(side="left")
        sp.bind("<KeyRelease>", lambda e: self._on_watch_change())
        _lbl(row, "分钟拿满，最多").pack(side="left", padx=(PAD_S, PAD_XS))
        self.var_watchbonus = tk.DoubleVar(value=self.cfg.watch_bonus)
        sp2 = ttk.Spinbox(row, from_=0, to=10, increment=0.5, width=4,
                          textvariable=self.var_watchbonus,
                          command=self._on_watch_change)
        sp2.pack(side="left")
        sp2.bind("<KeyRelease>", lambda e: self._on_watch_change())
        _lbl(row, "倍加成").pack(side="left", padx=(PAD_XS, 0))

        # ---- 无限叠加 ---------------------------------------------------- #
        row = _row_in(ctl)
        self.var_unlimited = tk.BooleanVar(value=self.cfg.weight_watch_unlimited)
        self._chk(row, "观看时长无限叠加", self.var_unlimited,
                  self._on_watch_change)
        self._hint(row, "　（勾上之后看越久权重越高，不封顶）", pady=(0, 0))

        # ---- 保底 -------------------------------------------------------- #
        row = _row_in(ctl)
        self.var_pity = tk.BooleanVar(value=self.cfg.pity_enabled)
        self._chk(row, "开启保底", self.var_pity, self._on_pity_change)
        _lbl(row, "　连续").pack(side="left")
        self.var_pityrounds = tk.IntVar(value=self.cfg.pity_rounds)
        sp3 = ttk.Spinbox(row, from_=1, to=99, width=4,
                          textvariable=self.var_pityrounds,
                          command=self._on_pity_change)
        sp3.pack(side="left", padx=(PAD_XS, PAD_XS))
        sp3.bind("<KeyRelease>", lambda e: self._on_pity_change())
        _lbl(row, "轮没中则").pack(side="left")

        self.var_pitymode = tk.StringVar(value=self.cfg.pity_mode)
        for 值, 文案 in (("guarantee", "必中"), ("multiply", "权重翻倍")):
            ttk.Radiobutton(row, text=文案, value=值,
                            variable=self.var_pitymode,
                            command=self._on_pity_change).pack(
                side="left", padx=(PAD_S, 0))

        # ---- 跨场次补偿 -------------------------------------------------- #
        row = _row_in(ctl)
        self.var_carry = tk.BooleanVar(value=self.cfg.carry_enabled)
        self._chk(row, "上期没中的人本期加权", self.var_carry,
                  self._on_carry_change)
        _lbl(row, "　倍率").pack(side="left", padx=(PAD_S, PAD_XS))
        self.var_carrymult = tk.DoubleVar(value=self.cfg.carry_mult)
        sp5 = ttk.Spinbox(row, from_=1.0, to=10.0, increment=0.5, width=4,
                          textvariable=self.var_carrymult,
                          command=self._on_carry_change)
        sp5.pack(side="left")
        sp5.bind("<KeyRelease>", lambda e: self._on_carry_change())
        _lbl(row, "倍").pack(side="left", padx=(PAD_XS, PAD_M))

        self.var_carryinfo = tk.StringVar(value="")
        tk.Label(row, textvariable=self.var_carryinfo, bg=CARD, fg=MUTED,
                 font=(self.ui_family, 9)).pack(side="left")

        self._secondary_btn(row, "清空待补偿名单",
                            self.on_clear_carry).pack(side="left", padx=PAD_S)

        # 手动倍率也是独立存的，单独给个清空入口
        self.var_manualinfo = tk.StringVar(value="")
        tk.Label(row, textvariable=self.var_manualinfo, bg=CARD, fg=MUTED,
                 font=(self.ui_family, 9)).pack(side="left", padx=(PAD_M, 0))
        self._secondary_btn(row, "清空手动倍率",
                            self.on_clear_manual).pack(side="left", padx=PAD_S)

        # ---- 观众投票 ---------------------------------------------------- #
        row = _row_in(ctl)
        self.var_vote = tk.BooleanVar(value=self.cfg.vote.enabled)
        self._chk(row, "开启观众投票", self.var_vote,
                  self._on_vote_setting_change)
        _lbl(row, "　投票时长").pack(side="left", padx=(PAD_S, PAD_XS))
        self.var_votedur = tk.DoubleVar(value=self.cfg.vote.vote_seconds)
        # 步进 1 秒：想设 15 秒、20 秒都能精确填，也能直接手打任意数字
        sp6 = ttk.Spinbox(row, from_=0, to=3600, increment=1, width=6,
                          textvariable=self.var_votedur,
                          command=self._on_vote_setting_change)
        sp6.pack(side="left")
        sp6.bind("<KeyRelease>", lambda e: self._on_vote_setting_change())
        _lbl(row, "秒（0=不限时）").pack(side="left", padx=(PAD_XS, 0))
        self._hint(row, "　（投票要手动点「开始投票」，观众才能投）",
                   pady=(0, 0))

        # ---- 手动指定投票对象 -------------------------------------------- #
        row = _row_in(ctl)
        _lbl(row, "手动投票对象：").pack(side="left")
        self.var_voteuid = tk.StringVar()
        e_vote = ttk.Entry(row, textvariable=self.var_voteuid, width=16)
        e_vote.pack(side="left", padx=(0, PAD_S))
        e_vote.bind("<Return>", lambda ev: self.on_manual_vote())
        self._secondary_btn(row, "开始投票",
                            self.on_manual_vote).pack(side="left")
        self._secondary_btn(row, "结束投票",
                            self.on_close_vote).pack(side="left", padx=PAD_S)
        self.var_voteleft = tk.StringVar(value="")
        tk.Label(row, textvariable=self.var_voteleft, bg=CARD, fg=ACCENT,
                 font=(self.ui_family, 10, "bold")).pack(
            side="left", padx=(PAD_M, 0))
        self._hint(row, "　（留空则用刚抽中的人；投票窗开着才能投）",
                   pady=(0, 0))

        # ---- 参与池定时自动清空 ------------------------------------------ #
        row = _row_in(ctl)
        self.var_poolauto = tk.BooleanVar(value=self.cfg.pool_auto_clear)
        self._chk(row, "自动清空参与池", self.var_poolauto,
                  self._on_pool_auto_change)
        _lbl(row, "每").pack(side="left", padx=(PAD_S, PAD_XS))
        self.var_poolmin = tk.DoubleVar(
            value=self.cfg.pool_auto_clear_minutes)
        sp7 = ttk.Spinbox(row, from_=0.5, to=180, increment=0.5, width=5,
                          textvariable=self.var_poolmin,
                          command=self._on_pool_auto_change)
        sp7.pack(side="left")
        sp7.bind("<KeyRelease>", lambda e: self._on_pool_auto_change())
        _lbl(row, "分钟清一次").pack(side="left", padx=(PAD_XS, 0))
        self.var_poolcountdown = tk.StringVar(value="")
        tk.Label(row, textvariable=self.var_poolcountdown, bg=CARD,
                 fg=ACCENT, font=(self.ui_family, 10, "bold")).pack(
            side="left", padx=(PAD_M, 0))

        # ---- 参与名单窗开关 ---------------------------------------------- #
        row = _row_in(ctl)
        self.var_board = tk.BooleanVar(value=bool(self.cfg.board.enabled))
        self._chk(row, "显示参与名单窗", self.var_board,
                  self._on_board_toggle)
        self._hint(row, "　（让观众看见谁参与成功了 · 也可以按 F5 切换）",
                   pady=(0, 0))

        # 权重预览：放在淡色底条里，和上面的设置拉开层次
        info = tk.Frame(ctl, bg="#f7f9fc")
        info.pack(fill="x", pady=(0, PAD_S))
        self.var_watchinfo = tk.StringVar(value="")
        tk.Label(info, textvariable=self.var_watchinfo, bg="#f7f9fc",
                 fg=ACCENT, font=(self.ui_family, 9), justify="left",
                 wraplength=980).pack(anchor="w", padx=PAD_S, pady=(6, 6))

        self._card_hint(ctl,
                        "「看了多久」= 抽奖那一刻 减去 这个人 UID 第一次出现在公屏的时间。"
                        "刚来的人权重 1.0，看满 30 分钟的人 3.0（3 倍中奖率）；"
                        "想去掉加权、回到纯等概率，把上面的勾取消即可。",
                        wraplength=960, pady=(0, PAD_M))

        # ---- 主按钮行 ----------------------------------------------------- #
        row = _row_in(ctl)
        self.btn_draw = tk.Button(
            row, text="开 始 抽 取   (F1)",
            font=(self.ui_family, 16, "bold"),
            bg=ACCENT, fg="white",
            activebackground=ACCENT_HOVER, activeforeground="white",
            relief="flat", cursor="hand2", bd=0,
            padx=30, pady=13, command=self.on_draw)
        self.btn_draw.pack(side="left")
        # 鼠标悬停加深 —— 让主按钮"活"起来
        self.btn_draw.bind("<Enter>",
                           lambda e: self.btn_draw.configure(bg=ACCENT_HOVER))
        self.btn_draw.bind("<Leave>",
                           lambda e: self.btn_draw.configure(bg=ACCENT))

        self._secondary_btn(row, "撤销上次中奖 (F2)",
                            self.on_undo).pack(side="left", padx=(PAD_M, 0))

        # 下播：归档本场没中的人，然后清空本场数据
        self.btn_offline = tk.Button(
            row, text="下 播", font=(self.ui_family, 11, "bold"),
            bg="#F5A623", fg="white", activebackground="#D98A10",
            activeforeground="white", relief="flat", cursor="hand2",
            bd=0, padx=16, pady=8, command=self.on_offline)
        self.btn_offline.pack(side="left", padx=(PAD_M, 0))
        self.btn_offline.bind(
            "<Enter>", lambda e: self.btn_offline.configure(bg="#D98A10"))
        self.btn_offline.bind(
            "<Leave>", lambda e: self.btn_offline.configure(bg="#F5A623"))
        self._danger_btn(row, "清空参与池",
                         self.on_reset).pack(side="left", padx=PAD_S)
        self._secondary_btn(row, "显示/隐藏展示窗 (F3)",
                            self.app.toggle_overlay).pack(side="left")
        self._secondary_btn(row, "投票窗 (F6)",
                            self.app.toggle_vote).pack(side="left", padx=PAD_S)
        self._secondary_btn(row, "在线榜窗 (F7)",
                            self.app.toggle_rank).pack(side="left")

        # ---- 本轮凭证 ----------------------------------------------------- #
        pv = tk.Frame(ctl, bg="#f7f9fc")
        pv.pack(fill="x", pady=(PAD_S, 0))
        self.var_proof = tk.StringVar(value="本轮还没有抽过")
        tk.Label(pv, textvariable=self.var_proof, bg="#f7f9fc", fg=MUTED,
                 font=(self.ui_family, 9), justify="left",
                 wraplength=980).pack(anchor="w", padx=PAD_S, pady=(6, 6))

        # ---- 下半部分：中奖名单 + 手动加 UID -------------------------------- #
        # 注意：这一页现在在可滚动容器里，所以**不能用 expand** 去抢高度 ——
        # 内层框架拿到的是自己的自然高度，让它按内容撑开就行。
        mid = tk.Frame(p, bg=CARD)
        mid.pack(fill="x")
        mid.columnconfigure(0, weight=3, uniform="mid")
        mid.columnconfigure(1, weight=2, uniform="mid")

        left = self._section(mid, "中奖名单", "点一行即复制 UID", pack=False)
        left.outer.grid(row=0, column=0, sticky="nsew", padx=(0, PAD_S))

        lb_wrap = tk.Frame(left, bg=CARD)
        lb_wrap.pack(fill="both", expand=True, pady=(0, PAD_S))
        self.lb_winners = tk.Listbox(
            lb_wrap, height=7, activestyle="none",
            font=(self.ui_family, 12),
            selectmode="extended",
            bg=CARD, fg=TEXT,
            selectbackground=ACCENT_SOFT, selectforeground=TEXT,
            # 用 Listbox 自己的 highlight 画那一圈细边框，
            # 不要再套一层 Frame —— 套两层会出现难看的"双层边框"
            bd=0, relief="flat",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=BORDER)
        self.lb_winners.pack(fill="both", expand=True)
        #: 和中奖名单每一行一一对应的 Winner 对象（用来把「点第几行」翻回 UID）
        self._winner_rows: list = []

        self._card_hint(left, "点一行 = 复制 UID　·　双击 = 复制「昵称(UID)」　·　"
                              "右键 = 更多选项", pady=(0, PAD_S))

        wbtn = tk.Frame(left, bg=CARD)
        wbtn.pack(fill="x", pady=(0, PAD_S))
        self._secondary_btn(wbtn, "复制最新中奖",
                            self.on_copy_last_winner).pack(side="left")
        self._secondary_btn(wbtn, "复制全部",
                            self.on_copy_all_winners).pack(side="left",
                                                           padx=PAD_S)
        self._secondary_btn(wbtn, "导出名单",
                            self.on_export_winners).pack(side="left")

        wopt = tk.Frame(left, bg=CARD)
        wopt.pack(fill="x")
        self.var_autocopy = tk.BooleanVar(value=self.cfg.copy_winner_after_draw)
        self._chk(wopt, "抽中后自动复制", self.var_autocopy,
                  self._on_autocopy_change)
        self.var_copynick = tk.BooleanVar(value=self.cfg.copy_with_nick)
        self._chk(wopt, "带上昵称", self.var_copynick,
                  self._on_copynick_change)

        # 点一行就复制；双击复制「昵称(UID)」；右键出菜单
        self.lb_winners.bind("<<ListboxSelect>>", self._on_winner_click)
        self.lb_winners.bind("<Double-1>", self._on_winner_double)
        self.lb_winners.bind("<Button-3>", self._on_winner_menu)

        right = self._section(mid, "手动加一个 UID", "临时用", pack=False)
        right.outer.grid(row=0, column=1, sticky="nsew")

        self._card_hint(right,
                        "直接把 UID 打进来加进参与池；空格、横线、点、表情会自动去掉。",
                        wraplength=420, pady=(0, PAD_S))
        self.var_manual = tk.StringVar()
        e = ttk.Entry(right, textvariable=self.var_manual,
                      font=(self.ui_family, 12))
        e.pack(fill="x", pady=(0, PAD_S))
        e.bind("<Return>", lambda ev: self.on_manual_add())
        self._secondary_btn(right, "加入参与池",
                            self.on_manual_add).pack(anchor="w")

        tk.Label(right, text="批量粘贴（一行一个）", bg=CARD, fg=MUTED,
                 font=(self.ui_family, 9)).pack(anchor="w", pady=(PAD_M, PAD_XS))
        self.txt_paste = tk.Text(right, height=4, font=(self.ui_family, 10),
                                 wrap="none", bg=CARD, fg=TEXT,
                                 relief="solid", bd=1,
                                 highlightthickness=0,
                                 highlightbackground=BORDER_STRONG,
                                 highlightcolor=ACCENT,
                                 padx=PAD_S, pady=PAD_S)
        self.txt_paste.pack(fill="both", expand=True)
        self._secondary_btn(right, "全部导入",
                            self.on_paste_import).pack(anchor="w",
                                                       pady=(PAD_S, 0))

        self._refresh_draw_tab()

    # ================================================================== #
    # 标签页 2：识别设置
    # ================================================================== #

    def _build_extract_tab(self) -> None:
        p = self.tab_extract
        ex: ExtractConfig = self.cfg.extract

        box = ttk.LabelFrame(p, text=" 识别模式 ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        self.var_mode = tk.StringVar(value=ex.mode)
        for m in Mode.ALL:
            ttk.Radiobutton(box, text=Mode.LABELS[m], value=m,
                            variable=self.var_mode,
                            command=self._on_mode_change).pack(anchor="w")
            ttk.Label(box, text="      " + Mode.HINTS[m].replace("\n", "\n      "),
                      style="Hint.TLabel", justify="left").pack(anchor="w",
                                                                pady=(0, 6))

        box = ttk.LabelFrame(p, text=" 位数范围 ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        row = self._row(box)
        ttk.Label(row, text="最短：").pack(side="left")
        self.var_minlen = tk.IntVar(value=ex.min_len)
        ttk.Spinbox(row, from_=1, to=30, width=5, textvariable=self.var_minlen,
                    command=self.mark_dirty).pack(side="left", padx=(0, 6))
        ttk.Label(row, text="位      最长：").pack(side="left")
        self.var_maxlen = tk.IntVar(value=ex.max_len)
        ttk.Spinbox(row, from_=1, to=30, width=5, textvariable=self.var_maxlen,
                    command=self.mark_dirty).pack(side="left", padx=(0, 6))
        ttk.Label(row, text="位").pack(side="left")
        ttk.Button(row, text="按当前参与池推荐",
                   command=self.on_suggest_len).pack(side="left", padx=16)
        self.var_lenhint = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.var_lenhint,
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))
        self._hint(box, "「666」「888」「233」这类短数字靠最短位数挡掉。\n"
                        "抖音数字 UID 一般 9~10 位；如果你不确定，先跑一场看"
                        "「日志」页里识别出来的号码有多长。", pady=(6, 0))

        box = ttk.LabelFrame(p, text=" 抗屏蔽 / 容错 ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        r1 = self._row(box)
        self.var_letterfix = tk.BooleanVar(value=ex.letter_fix)
        self._chk(r1, "易混字母还原（O→0  l/I→1  Z→2  S→5  B→8  g→9）",
                  self.var_letterfix)
        self.var_cn = tk.BooleanVar(value=ex.chinese_digits)
        self._chk(r1, "中文数字还原（一零零 → 100）", self.var_cn)
        r2 = self._row(box)
        self.var_blockcommon = tk.BooleanVar(value=ex.block_common_numbers)
        self._chk(r2, "拦截常见数字（666 / 123456 / 5201314…）",
                  self.var_blockcommon)
        self.var_strip = tk.BooleanVar(value=ex.strip_label)
        self._chk(r2, "抹掉「UID:」「抖音号:」等标签", self.var_strip)
        r3 = self._row(box)
        ttk.Label(r3, text="数字之间最多允许夹几个符号：").pack(side="left")
        self.var_gap = tk.IntVar(value=ex.max_gap)
        ttk.Spinbox(r3, from_=0, to=12, width=5, textvariable=self.var_gap,
                    command=self.mark_dirty).pack(side="left")
        ttk.Label(r3, text="　空格另外算，最多 ").pack(side="left")
        self.var_gapspace = tk.IntVar(
            value=getattr(ex, "max_gap_space", 12))
        ttk.Spinbox(r3, from_=0, to=30, width=5, textvariable=self.var_gapspace,
                    command=self.mark_dirty).pack(side="left")
        self._hint(box, "开启「易混字母还原」后，「UID:100591822」里的 ID 有可能被"
                        "误当成数字粘上去（已修复，但仍建议开着对照日志看两天）。",
                   pady=(6, 0))

        box = ttk.LabelFrame(p, text=" 关键词过滤（留空 = 不启用） ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        r = self._row(box)
        ttk.Label(r, text="弹幕必须包含：").pack(side="left")
        self.var_req = tk.StringVar(value=" ".join(ex.require_keywords))
        ttk.Entry(r, textvariable=self.var_req, width=34).pack(side="left")
        ttk.Label(r, text="  多个词用空格隔开，命中任意一个即可").pack(side="left")
        r = self._row(box)
        ttk.Label(r, text="包含就丢弃：  ").pack(side="left")
        self.var_blk = tk.StringVar(value=" ".join(ex.block_keywords))
        ttk.Entry(r, textvariable=self.var_blk, width=34).pack(side="left")
        ttk.Label(r, text="  例如「广告」").pack(side="left")

        box = ttk.LabelFrame(p, text=" 识别测试（改完设置立刻在这里验证） ", padding=(12, 18, 12, 12))
        box.pack(fill="both", expand=True)
        self._hint(box, "每行一条弹幕，边打边看识别结果。绿色 = 认出来了。", wraplength=940)
        self._hint(box,
                   "❗这里只是「试识别」，不会把人加进参与池 —— "
                   "要加人请用「抽取」页的「手动加一个 UID / 批量粘贴」，"
                   "或让弹幕真正进来。",
                   wraplength=940)
        self.txt_test = tk.Text(box, height=6, font=(self.ui_family, 11),
                                wrap="none")
        self.txt_test.pack(fill="x", pady=(6, 6))
        self.txt_test.insert("1.0",
                             "100 591 822\n主播抽我 100 591 823\n"
                             "UID:100 591 824\n我 100 591 825 求抽\n"
                             "666\n哈哈哈\n100.591.826\n１００５９１８２７")
        self.txt_test.bind("<KeyRelease>", self._schedule_test)
        self.lb_test = tk.Listbox(box, height=8, font=(self.ui_family, 10),
                                  activestyle="none")
        self.lb_test.pack(fill="both", expand=True)
        self._run_test()

    # ================================================================== #
    # 标签页 3：弹幕来源
    # ================================================================== #

    def _build_source_tab(self) -> None:
        p = self.tab_source
        sc = self.cfg.source

        # ---- HTTP ----
        box = ttk.LabelFrame(p, text=" ① HTTP 推送（最通用，推荐） ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        self.var_http = tk.BooleanVar(value=sc.http_enabled)
        r = self._row(box)
        self._chk(r, "启用弹幕接收服务", self.var_http, self._on_http_toggle)
        ttk.Label(r, text="端口：").pack(side="left")
        self.var_port = tk.IntVar(value=sc.http_port)
        ttk.Spinbox(r, from_=1024, to=65535, width=7,
                    textvariable=self.var_port).pack(side="left", padx=(0, 10))
        ttk.Label(r, text="监听地址：").pack(side="left")
        self.var_host = tk.StringVar(value=sc.http_host)
        ttk.Combobox(r, textvariable=self.var_host, width=12,
                     values=("127.0.0.1", "0.0.0.0")).pack(side="left")
        ttk.Button(r, text="应用/重启", command=self._on_http_toggle).pack(
            side="left", padx=10)

        r = self._row(box)
        self.var_http_url = tk.StringVar(value="未启动")
        ttk.Entry(r, textvariable=self.var_http_url, width=48,
                  state="readonly").pack(side="left")
        ttk.Button(r, text="复制", command=self.on_copy_url).pack(side="left", padx=6)
        ttk.Button(r, text="打开接口面板",
                   command=self.on_open_panel).pack(side="left")
        ttk.Button(r, text="打开浏览器叠加页",
                   command=self.on_open_overlay_page).pack(side="left", padx=6)

        # 端口被占用时的醒目提示（这正是「识别成功但参与池不涨」的典型原因）
        self.var_portwarn = tk.StringVar(value="")
        self.lb_portwarn = tk.Label(box, textvariable=self.var_portwarn,
                                    fg=BAD, bg=BG, justify="left",
                                    anchor="w", wraplength=920,
                                    font=(self.ui_family, 10, "bold"))
        self.lb_portwarn.pack(fill="x", pady=(6, 0))

        self.var_lan = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.var_lan,
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))
        self._hint(box,
                   "把你的弹幕工具/脚本往这个地址 POST 弹幕就能进来：\n"
                   "    POST <地址>/push     body 直接写弹幕原文\n"
                   "    GET  <地址>/push?text=100 591 822&nick=小明\n"
                   "监听 0.0.0.0 时手机也能推（同一 WiFi 下用局域网地址）。",
                   pady=(6, 0))

        # ---- 文件 ----
        box = ttk.LabelFrame(p, text=" ② 文件监听（配合第三方弹幕助手） ",
                             padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        self.var_file = tk.BooleanVar(value=sc.file_enabled)
        r = self._row(box)
        self._chk(r, "启用文件监听", self.var_file, self._on_file_toggle)
        self.var_filepath = tk.StringVar(value=sc.file_path)
        ttk.Entry(r, textvariable=self.var_filepath, width=44).pack(side="left")
        ttk.Button(r, text="选择文件…",
                   command=self.on_pick_file).pack(side="left", padx=6)
        r = self._row(box)
        ttk.Label(r, text="行格式：").pack(side="left")
        self.var_filefmt = tk.StringVar(value=sc.file_format)
        ttk.Combobox(r, textvariable=self.var_filefmt, width=16, state="readonly",
                     values=("auto", "text", "nick_tab_text", "jsonl",
                             "chat")).pack(side="left", padx=(0, 12))
        ttk.Label(r, text="编码：").pack(side="left")
        self.var_fileenc = tk.StringVar(value=sc.file_encoding)
        ttk.Combobox(r, textvariable=self.var_fileenc, width=10,
                     values=("utf-8", "utf-8-sig", "gbk", "utf-16")).pack(
            side="left")
        self.var_filestate = tk.StringVar(value="未启动")
        ttk.Label(box, textvariable=self.var_filestate,
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))
        self._hint(box,
                   "只读「新增」的内容，启动前已有的旧内容会被跳过。\n"
                   "auto = 自动识别纯文本 / 昵称+Tab+内容 / JSON Lines。",
                   pady=(6, 0))

        # ---- 剪贴板 ----
        box = ttk.LabelFrame(p, text=" ③ 剪贴板监听 ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        self.var_clip = tk.BooleanVar(value=sc.clipboard_enabled)
        r = self._row(box)
        self._chk(r, "复制到剪贴板就自动收录", self.var_clip)
        ttk.Label(r, text="轮询间隔(ms)：").pack(side="left")
        self.var_clipms = tk.IntVar(value=sc.clipboard_interval_ms)
        ttk.Spinbox(r, from_=150, to=5000, increment=50, width=7,
                    textvariable=self.var_clipms).pack(side="left")
        self._hint(box, "适合「主播自己从别处把弹幕复制过来」的场景。"
                        "注意：会读取你复制的任何文本，不想用就关掉。", pady=(6, 0))

        # ---- 模拟 ----
        box = ttk.LabelFrame(p, text=" ④ 模拟弹幕（彩排用，不用真开播） ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        self.var_demo = tk.BooleanVar(value=sc.demo_enabled)
        r = self._row(box)
        self._chk(r, "自动生成仿真弹幕", self.var_demo, self._on_demo_toggle)
        ttk.Label(r, text="间隔(ms)：").pack(side="left")
        self.var_demoms = tk.IntVar(value=sc.demo_interval_ms)
        ttk.Spinbox(r, from_=50, to=6000, increment=50, width=7,
                    textvariable=self.var_demoms).pack(side="left")
        self._hint(box, "会随机造出「100 591 822」「100.591.822」"
                        "「100(表情)591(表情)822」「100(零宽)591(零宽)822」"
                        "等各种规避写法，用来验证识别设置。",
                   pady=(6, 0))

        box = ttk.LabelFrame(p, text=" ⑤ token 保护（可选） ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        r = self._row(box)
        ttk.Label(r, text="token：").pack(side="left")
        self.var_token = tk.StringVar(value=sc.http_token)
        ttk.Entry(r, textvariable=self.var_token, width=28).pack(side="left")
        ttk.Button(r, text="随机生成",
                   command=self._gen_token).pack(side="left", padx=6)
        self._hint(box,
                   "token 是给「监听 0.0.0.0、要用手机推弹幕」这种情况准备的 —— "
                   "挡住局域网里别人乱推。\n"
                   "★ 本机（127.0.0.1）访问一律放行，不需要带 token，"
                   "所以接口面板 / 浏览器叠加页 / 抖音直连都不受影响。\n"
                   "★ 只有从别的设备访问时才必须带 ?token=xxx"
                   "（「复制」按钮给出的地址已经带好了）。\n"
                   "只在本机用的话，留空最省事。",
                   pady=(6, 0))

        # ---- 抖音直连 ----
        box = ttk.LabelFrame(p, text=" ⑥ 抖音弹幕直连（实验性 · 自动读公屏） ",
                             padding=(12, 18, 12, 12))
        box.pack(fill="x")
        self._hint(box,
                   "借你电脑上的真实浏览器去开抖音直播间，在旁边「听」它收到的弹幕，\n"
                   "再自动转发到本程序。不用手动粘贴，也不用管抖音的签名和风控。",
                   wraplength=940)
        r = self._row(box)
        ttk.Label(r, text="你的直播间号：").pack(side="left")
        self.var_dyroom = tk.StringVar()
        ttk.Entry(r, textvariable=self.var_dyroom, width=22).pack(side="left")
        ttk.Label(r, text="（网址 live.douyin.com/ 后面那串数字）",
                  style="Hint.TLabel").pack(side="left", padx=(6, 0))
        r = self._row(box)
        ttk.Button(r, text="打开桥接文件夹",
                   command=self.on_open_bridge_dir).pack(side="left")
        ttk.Button(r, text="复制启动命令",
                   command=self.on_copy_bridge_cmd).pack(side="left", padx=6)
        ttk.Button(r, text="直接启动桥接",
                   command=self.on_launch_bridge).pack(side="left")
        self.var_dystate = tk.StringVar(value="未运行")
        ttk.Label(box, textvariable=self.var_dystate,
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))
        self._hint(box,
                   "第一次用建议先看那个文件夹里的「说明.md」。\n"
                   "直接启动会弹一个独立的浏览器窗口（不影响你平时的浏览器），"
                   "退出时会自动关掉它。\n"
                   "⚠ 桥接对抖音只读不写（不注入、不发消息、不自己算签名），"
                   "实测网页看不到自动化特征；\n"
                   "   但自动化访问本身处在平台规则的灰色地带，"
                   "建议填自己的房间号、别登录大号、不用时关掉。\n"
                   "   完整实测数据看「说明.md」里的《会不会被抖音风控》。",
                   pady=(4, 0))

        self._refresh_source_tab()

    # ================================================================== #
    # 标签页 4：展示窗口
    # ================================================================== #

    def _build_overlay_tab(self) -> None:
        p = self.tab_overlay
        oc = self.cfg.overlay

        box = ttk.LabelFrame(p, text=" 背景（决定直播伴侣里怎么抠像） ", padding=(12, 18, 12, 12))
        box.pack(fill="x", pady=(0, 10))
        self.var_bgmode = tk.StringVar(value=oc.bg_mode)
        for m in BgMode.ALL:
            ttk.Radiobutton(box, text=BgMode.LABELS[m], value=m,
                            variable=self.var_bgmode,
                            command=self._on_bgmode_change).pack(anchor="w")
            ttk.Label(box, text="      " + BgMode.HINTS[m].replace("\n", "\n      "),
                      style="Hint.TLabel", justify="left").pack(anchor="w",
                                                                pady=(0, 6))
        r = self._row(box)
        ttk.Label(r, text="纯色颜色：").pack(side="left")
        self.btn_bgcolor = tk.Button(r, text="      ", width=6,
                                     bg=oc.bg_color, relief="solid", bd=1,
                                     command=self.on_pick_bgcolor)
        self.btn_bgcolor.pack(side="left", padx=(0, 10))
        for label, color in BG_PRESETS:
            b = tk.Button(r, text=label, relief="solid", bd=1,
                          command=lambda c=color: self.set_bgcolor(c))
            b.pack(side="left", padx=2)
        r = self._row(box)
        ttk.Label(r, text="背景图片：").pack(side="left")
        self.var_imgpath = tk.StringVar(value=oc.image_path)
        ttk.Entry(r, textvariable=self.var_imgpath, width=44).pack(side="left")
        ttk.Button(r, text="选择…", command=self.on_pick_image).pack(side="left",
                                                                     padx=6)

        # ---- 窗口大小（窗口显示不正常时的急救入口）---- #
        sizebox = ttk.LabelFrame(
            p, text=" 窗口大小 ", padding=(12, 18, 12, 12))
        sizebox.pack(fill="x", pady=(0, 10))

        r = self._row(sizebox)
        self.var_overlaygeom = tk.StringVar(value=oc.geom or "（未设置）")
        ttk.Label(r, text="展示窗：").pack(side="left")
        ttk.Label(r, textvariable=self.var_overlaygeom,
                  style="Hint.TLabel").pack(side="left", padx=(0, PAD_M))

        self.var_boardgeom = tk.StringVar(value=self.cfg.board.geom or "（未设置）")
        ttk.Label(r, text="名单窗：").pack(side="left")
        ttk.Label(r, textvariable=self.var_boardgeom,
                  style="Hint.TLabel").pack(side="left")

        r = self._row(sizebox)
        self._secondary_btn(r, "重置展示窗",
                            self.on_reset_overlay).pack(side="left")
        self._secondary_btn(r, "重置名单窗",
                            self.on_reset_board).pack(side="left", padx=PAD_S)
        self._secondary_btn(r, "两个一起重置",
                            self.on_reset_both).pack(side="left")
        self._hint(r, "　（窗口变得太小／跑到屏幕外时点这里）",
                   pady=(0, 0))

        nb = ttk.Frame(p)
        nb.pack(fill="both", expand=True)

        # ---- 文字 ----
        left = ttk.LabelFrame(nb, text=" 文字 ", padding=(12, 18, 12, 12))
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        self.var_uidcolor = tk.StringVar(value=oc.uid_color)
        self.var_titlecolor = tk.StringVar(value=oc.title_color)
        self.var_countcolor = tk.StringVar(value=oc.count_color)
        self.var_outlinecolor = tk.StringVar(value=oc.outline_color)

        r = self._row(left)
        ttk.Label(r, text="字体：").pack(side="left")
        self.var_font = tk.StringVar(value=oc.font_family)
        fams = sorted(set(tkfont.families(self.root)))
        ttk.Combobox(r, textvariable=self.var_font, width=22,
                     values=fams).pack(side="left", padx=(0, 12))
        self.var_font.bind_check = None  # noqa

        def size_row(parent, label, var):
            rr = self._row(parent)
            ttk.Label(rr, text=label, width=14).pack(side="left")
            ttk.Spinbox(rr, from_=8, to=400, width=6,
                        textvariable=var).pack(side="left")

        self.var_uidsize = tk.IntVar(value=oc.uid_size)
        self.var_titlesize = tk.IntVar(value=oc.title_size)
        self.var_countsize = tk.IntVar(value=oc.count_size)
        self.var_fontsize_hint = tk.IntVar(value=oc.outline_width)

        size_row(left, "中奖UID字号", self.var_uidsize)
        size_row(left, "标题字号", self.var_titlesize)
        size_row(left, "人数行字号", self.var_countsize)

        r = self._row(left)
        ttk.Label(r, text="描边粗细", width=14).pack(side="left")
        ttk.Spinbox(r, from_=0, to=12, width=6,
                    textvariable=self.var_fontsize_hint).pack(side="left")

        for label, var, key in [
            ("UID 颜色", self.var_uidcolor, "uid_color"),
            ("标题颜色", self.var_titlecolor, "title_color"),
            ("人数行颜色", self.var_countcolor, "count_color"),
            ("描边颜色", self.var_outlinecolor, "outline_color"),
        ]:
            r = self._row(left)
            ttk.Label(r, text=label, width=14).pack(side="left")
            ttk.Entry(r, textvariable=var, width=10).pack(side="left")
            ttk.Button(r, text="取色",
                       command=lambda v=var, k=key: self._pick_color(v, k)).pack(
                side="left", padx=6)

        r = self._row(left)
        ttk.Label(r, text="标题文字", width=14).pack(side="left")
        self.var_titletext = tk.StringVar(value=oc.title_text)
        ttk.Entry(r, textvariable=self.var_titletext, width=22).pack(side="left")
        r = self._row(left)
        ttk.Label(r, text="等待文字", width=14).pack(side="left")
        self.var_idletext = tk.StringVar(value=oc.idle_text)
        ttk.Entry(r, textvariable=self.var_idletext, width=22).pack(side="left")

        # ---- 显示开关 ----
        right = ttk.LabelFrame(nb, text=" 显示内容 / 窗口 ", padding=(12, 18, 12, 12))
        right.pack(side="left", fill="both", expand=True)

        self.var_showtitle = tk.BooleanVar(value=oc.show_title)
        self.var_showcount = tk.BooleanVar(value=oc.show_count)
        self.var_showfeed = tk.BooleanVar(value=oc.show_feed)
        for text, var in [
            ("显示标题", self.var_showtitle),
            ("显示参与人数", self.var_showcount),
            ("显示弹幕上墙", self.var_showfeed),
        ]:
            c = ttk.Checkbutton(right, text=text, variable=var,
                                command=self._apply_soon)
            c.pack(anchor="w", pady=2)


        r = self._row(right)
        ttk.Label(r, text="人数前缀：").pack(side="left")
        self.var_countprefix = tk.StringVar(value=oc.count_prefix)
        ttk.Entry(r, textvariable=self.var_countprefix, width=14).pack(side="left")

        r = self._row(right)
        self.var_borderless = tk.BooleanVar(value=oc.borderless)
        self._chk(r, "无边框（推荐）", self.var_borderless, self._apply_soon)
        self.var_topmost = tk.BooleanVar(value=oc.always_on_top)
        self._chk(r, "总在最前", self.var_topmost, self._apply_soon)
        self._hint(right,
                   "「总在最前」取消勾选后，展示窗就会被别的窗口盖住了。\n"
                   "　（窗口捕获不受遮挡影响，所以直播时不置顶也照样能抓到。）",
                   wraplength=430, pady=(2, 0))

        r = self._row(right)
        self.var_wintitle = tk.StringVar(value=oc.window_title)
        ttk.Label(r, text="窗口标题：").pack(side="left")
        ttk.Entry(r, textvariable=self.var_wintitle, width=20).pack(side="left")
        self._hint(right, "直播伴侣「窗口捕获」的下拉列表里就是靠这个标题找的。",
                   pady=(4, 0))

        r = self._row(right)
        ttk.Button(r, text="显示/隐藏展示窗",
                   command=self.app.toggle_overlay).pack(side="left")
        ttk.Button(r, text="重置位置大小",
                   command=self.on_reset_geom).pack(side="left", padx=6)
        ttk.Button(r, text="测试：立刻抽一次滚动",
                   command=self.on_draw).pack(side="left", padx=6)

        r = self._row(right)
        ttk.Button(r, text="应用外观到展示窗",
                   command=self.apply_overlay_now).pack(side="left")

        self._hint(right,
                   "用法：直播伴侣 → 添加素材 → 窗口捕获 → 选「%s」\n"
                   "→ 打开「绿幕抠图」→ 吸管点一下窗口底色 → 调相似度/平滑度。"
                   % oc.window_title, pady=(10, 0))

        # ---- 参与名单窗 ---- #
        bc = self.cfg.board
        board_box = ttk.LabelFrame(
            p, text=" 参与名单窗（让观众看见谁参与成功了 · 独立窗口） ",
            padding=(12, 18, 12, 12))
        board_box.pack(fill="x", pady=(10, 0))
        self._hint(board_box,
                   "这是一个**跟展示窗完全独立**的窗口，自己的位置和大小，\n"
                   "默认 16:9（1600×900）。直播伴侣里再添加一路「窗口捕获 + 绿幕抠图」，选：\n"
                   f"    {bc.window_title}\n"
                   "有人参与成功时，一条「UID + 参与成功」会像视频弹幕一样\n"
                   "从右往左飘过，多条自动落在不同轨道、互不重叠。\n"
                   "背景色 / 字体 / 描边沿用上面的「展示窗口」设置 —— "
                   "两路素材只需抠同一种颜色。",
                   wraplength=940)

        r = self._row(board_box)
        ttk.Label(r, text="UID 字号：").pack(side="left")
        self.var_bsize = tk.IntVar(value=bc.uid_size)
        ttk.Spinbox(r, from_=8, to=160, width=5,
                    textvariable=self.var_bsize).pack(side="left", padx=(0, 12))
        ttk.Label(r, text="弹幕速度：").pack(side="left")
        self.var_bspeed = tk.IntVar(value=bc.speed)
        ttk.Spinbox(r, from_=40, to=1200, increment=20, width=6,
                    textvariable=self.var_bspeed).pack(side="left", padx=(0, 6))
        ttk.Label(r, text="像素/秒").pack(side="left")

        r = self._row(board_box)
        ttk.Label(r, text="轨道数：").pack(side="left")
        self.var_btracks = tk.IntVar(value=bc.track_count)
        ttk.Spinbox(r, from_=1, to=30, width=4,
                    textvariable=self.var_btracks).pack(side="left", padx=(0, 12))
        ttk.Label(r, text="轨道间距：").pack(side="left")
        self.var_btgap = tk.IntVar(value=bc.track_gap)
        ttk.Spinbox(r, from_=20, to=200, width=5,
                    textvariable=self.var_btgap).pack(side="left", padx=(0, 12))
        ttk.Label(r, text="UID 颜色：").pack(side="left")
        self.var_bcolor = tk.StringVar(value=bc.uid_color)
        ttk.Entry(r, textvariable=self.var_bcolor, width=10).pack(side="left")

        r = self._row(board_box)
        self.var_btitle = tk.BooleanVar(value=bc.show_title)
        self._chk(r, "显示标题", self.var_btitle, self._apply_soon)
        ttk.Label(r, text="标题文字：").pack(side="left")
        self.var_btitletext = tk.StringVar(value=bc.title_text)
        ttk.Entry(r, textvariable=self.var_btitletext, width=14).pack(side="left")

        r = self._row(board_box)
        self.var_bsuffix = tk.BooleanVar(value=bc.show_suffix)
        self._chk(r, "UID 后显示「参与成功」", self.var_bsuffix, self._apply_soon)
        ttk.Label(r, text="后缀文字：").pack(side="left")
        self.var_bsuffixtext = tk.StringVar(value=bc.suffix)
        ttk.Entry(r, textvariable=self.var_bsuffixtext, width=14).pack(side="left")
        ttk.Label(r, text="后缀颜色：").pack(side="left")
        self.var_bsuffixcolor = tk.StringVar(value=bc.suffix_color)
        ttk.Entry(r, textvariable=self.var_bsuffixcolor, width=10).pack(side="left")

        r = self._row(board_box)
        self.var_bfooter = tk.BooleanVar(value=bc.show_footer)
        self._chk(r, "底部显示「已参与 N 人」", self.var_bfooter, self._apply_soon)
        self.var_btopmost = tk.BooleanVar(value=bc.always_on_top)
        self._chk(r, "总在最前", self.var_btopmost, self._apply_soon)

        r = self._row(board_box)
        ttk.Button(r, text="显示 / 隐藏名单窗 (F5)",
                   command=self.app.toggle_board).pack(side="left")
        ttk.Button(r, text="重置为 16:9（1600×900）",
                   command=self.on_reset_board_geom).pack(side="left", padx=6)
        self.var_bstate = tk.StringVar(value="")
        ttk.Label(board_box, textvariable=self.var_bstate,
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))

        # ---- 捕获助手 ---- #
        cap = ttk.LabelFrame(p, text=" 采集体检（直播伴侣里找不到窗口？点这里） ",
                             padding=(12, 18, 12, 12))
        cap.pack(fill="x", pady=(10, 0))
        self._hint(cap,
                   "「窗口捕获」能不能列到展示窗，取决于窗口的 Win32 样式：\n"
                   "    保留 = (有标题栏 WS_CAPTION) 或 (有 WS_EX_APPWINDOW)\n"
                   "无边框窗口默认两条都不满足，会被过滤掉 —— 程序已经会自动补上 "
                   "WS_EX_APPWINDOW 来绕过它。\n"
                   "还是找不到就点「一键修复采集样式」，或者把上面的「无边框」关掉"
                   "（有标题栏就一定能在列表里看到）。",
                   wraplength=940)
        r = self._row(cap)
        ttk.Button(r, text="检测能不能被采集",
                   command=self.on_capture_check).pack(side="left")
        ttk.Button(r, text="一键修复采集样式",
                   command=self.on_capture_fix).pack(side="left", padx=6)
        ttk.Button(r, text="列出采集软件能看到的窗口",
                   command=self.on_list_capturable).pack(side="left")
        ttk.Button(r, text="连名单窗一起检测",
                   command=self.on_capture_check_board).pack(side="left", padx=6)
        self.var_capstate = tk.StringVar(value="")
        ttk.Label(cap, textvariable=self.var_capstate,
                  style="Hint.TLabel", justify="left",
                  wraplength=940).pack(anchor="w", pady=(6, 0))

    # ================================================================== #
    # 标签页 5：参与池
    # ================================================================== #

    def _build_pool_tab(self) -> None:
        p = self.tab_pool
        r = self._row(p)
        ttk.Label(r, text="搜索：").pack(side="left")
        self.var_search = tk.StringVar()
        e = ttk.Entry(r, textvariable=self.var_search, width=26)
        e.pack(side="left", padx=(0, 10))
        e.bind("<KeyRelease>", lambda ev: self.schedule_pool_refresh(200))
        ttk.Button(r, text="刷新",
                   command=lambda: self.refresh_pool(force=True)).pack(side="left")
        ttk.Button(r, text="删除选中",
                   command=self.on_remove_selected).pack(side="left", padx=6)
        ttk.Button(r, text="加入黑名单",
                   command=self.on_blacklist_selected).pack(side="left")
        ttk.Button(r, text="导出 UID 列表",
                   command=self.on_export_uids).pack(side="left", padx=6)

        # ---- 第二行：按 UID 手动删除 + 观看时长记录管理 ---- #
        r2 = self._row(p)
        ttk.Label(r2, text="按 UID 删除：").pack(side="left")
        self.var_deluid = tk.StringVar()
        e2 = ttk.Entry(r2, textvariable=self.var_deluid, width=18)
        e2.pack(side="left", padx=(0, PAD_S))
        e2.bind("<Return>", lambda ev: self.on_delete_by_uid())
        self._secondary_btn(r2, "删除这个 UID",
                            self.on_delete_by_uid).pack(side="left")
        ttk.Label(r2, text="　　").pack(side="left")
        self._secondary_btn(r2, "清空观看时长记录",
                            self.on_clear_watch).pack(side="left")
        self._hint(r2, "　（清空参与池不会清观看时长，这里可以手动清）",
                   pady=(0, 0))

        self.var_poolinfo = tk.StringVar(value="")
        ttk.Label(p, textvariable=self.var_poolinfo,
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 4))

        wrap = ttk.Frame(p)
        wrap.pack(fill="both", expand=True)
        cols = ("uid", "nick", "count", "watch", "chance", "mult",
                "first", "source", "raw")
        self.tv = ttk.Treeview(wrap, columns=cols, show="headings",
                               selectmode="extended")
        # 「观看时长」和「中奖概率」是这版新加的：
        # 主播能直接看到每个人看了多久、中奖率多少，不用猜。
        heads = [("uid", "UID", 120), ("nick", "昵称", 105),
                 ("count", "发言", 55), ("watch", "观看时长", 85),
                 ("chance", "中奖概率", 85), ("mult", "倍率", 60),
                ("first", "首次出现", 85),
                 ("source", "来源", 75), ("raw", "原始弹幕", 240)]
        for key, text, width in heads:
            self.tv.heading(key, text=text)
            self.tv.column(key, width=width, anchor="w",
                           stretch=(key == "raw"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=vs.set)
        self.tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", lambda e: self.on_remove_selected())
        # 右键：复制参与池里的人（选中多个就一起复制，一行一个）
        self.tv.bind("<Button-3>", self._on_pool_menu)

    # ================================================================== #
    # 标签页 6：日志
    # ================================================================== #

    def _build_log_tab(self) -> None:
        p = self.tab_log
        r = self._row(p)
        ttk.Label(r, text="只显示识别成功的").pack(side="left")
        self.var_onlyok = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="", variable=self.var_onlyok).pack(side="left")
        ttk.Button(r, text="清空显示",
                   command=self.on_clear_log).pack(side="left", padx=10)
        ttk.Button(r, text="打开数据文件夹",
                   command=self.on_open_data_dir).pack(side="left")

        wrap = ttk.Frame(p)
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("time", "source", "raw", "uid", "status", "reason")
        self.tv_log = ttk.Treeview(wrap, columns=cols, show="headings")
        for key, text, width in [("time", "时间", 80), ("source", "来源", 80),
                                 ("raw", "原始弹幕", 300), ("uid", "识别结果", 120),
                                 ("status", "状态", 70), ("reason", "判据", 220)]:
            self.tv_log.heading(key, text=text)
            self.tv_log.column(key, width=width, anchor="w",
                               stretch=(key in ("raw", "reason")))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_log.yview)
        self.tv_log.configure(yscrollcommand=vs.set)
        self.tv_log.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_log.tag_configure("ok", foreground=GOOD)
        self.tv_log.tag_configure("bad", foreground=BAD)

    # ================================================================== #
    # 交互
    # ================================================================== #

    def _bind_keys(self) -> None:
        r = self.root
        r.bind("<F1>", lambda e: self.on_draw())
        r.bind("<F2>", lambda e: self.on_undo())
        r.bind("<F3>", lambda e: self.app.toggle_overlay())
        r.bind("<F4>", lambda e: self.on_hide_console())
        r.bind("<F5>", lambda e: self.app.toggle_board())
        r.bind("<F6>", lambda e: self.app.toggle_vote())
        r.bind("<F7>", lambda e: self.app.toggle_rank())
        r.protocol("WM_DELETE_WINDOW", self.app.quit)

    def _on_tab_changed(self, ev=None) -> None:
        try:
            idx = self.nb.index(self.nb.select())
        except Exception:
            return
        if idx == 4:
            self.schedule_pool_refresh(0)

    # ---- 抽取 ---------------------------------------------------------- #

    def on_draw(self) -> None:
        if self.session.animating:
            self._on_status("正在滚动中，稍等一下…")
            return
        self.session.do_draw(self.var_count.get())

    def on_undo(self) -> None:
        if not self.session.pool.winners:
            self._on_status("还没有中奖记录")
            return
        last = self.session.pool.winners[-1]
        if messagebox.askyesno("撤销中奖",
                               f"确定撤销最后一条中奖记录吗？\n\n"
                               f"第 {last.round_no} 轮：{last.display}",
                               parent=self.root):
            self.session.undo_draw(1)
            self.refresh_winners()

    def _on_carry_change(self) -> None:
        """补偿设置变了。"""
        try:
            self.cfg.carry_enabled = self.var_carry.get()
            self.cfg.carry_mult = max(1.0, float(self.var_carrymult.get()))
        except (ValueError, tk.TclError):
            return
        self.mark_dirty()
        self._刷新补偿提示()

    def _刷新补偿提示(self) -> None:
        """更新「上期有 N 人可获补偿」那行提示。"""
        try:
            n = self.session.carry_count()
            if not self.cfg.carry_enabled:
                self.var_carryinfo.set(f"（已关闭；名单里还有 {n} 人）")
            elif n:
                self.var_carryinfo.set(f"　待补偿：{n} 人")
            else:
                self.var_carryinfo.set("　暂无待补偿的人")

            # 手动倍率数量（清池不会丢，单独显示）
            if hasattr(self, "var_manualinfo"):
                手 = self.session.manual_mult_count()
                self.var_manualinfo.set(
                    f"　手动倍率：{手} 人" if 手 else "　手动倍率：无")
        except Exception:
            pass

    def on_clear_carry(self) -> None:
        """清空待补偿名单。"""
        n = self.session.carry_count()
        if not n:
            self._on_status("待补偿名单是空的")
            return
        if not messagebox.askyesno(
                "清空待补偿名单",
                f"有 {n} 人上一场没中、本期可获加权。\n\n"
                f"清空后他们这次就按普通观众算，没有补偿。\n\n确定吗？",
                parent=self.root):
            return
        n = self.session.clear_carry()
        self._刷新补偿提示()
        self.refresh_pool(force=True)
        self._on_status(f"已清空待补偿名单（{n} 人）")

    def on_manual_vote(self) -> None:
        """手动把输入框里的 UID 设为投票对象。"""
        uid = self.var_voteuid.get().strip()
        if not uid:
            # 留空就用刚抽中的人 —— 「抽完点一下就开投」是最常用的流程
            lr = self.session.last_result
            if lr is not None and lr.winners:
                uid = lr.winners[0].uid
            else:
                self._on_status("先填一个 UID，或者先抽一次奖")
                return
        # 顺手归一化（去掉横线、空格之类）
        try:
            from .normalize import normalize_uid
            规整 = normalize_uid(uid)
            if 规整:
                uid = 规整
        except Exception:
            pass
        if not self.session.open_vote_manual(uid):
            self._on_status(f"「{uid}」不是有效 UID")
            return
        self.var_voteuid.set("")
        # 窗口没开就顺手打开
        窗 = getattr(self.app, "vote_win", None)
        if 窗 is None:
            try:
                self.app.toggle_vote()
            except Exception:
                pass
        elif not 窗.visible:
            窗.show()
        self._on_status(f"投票已开始，对象：{uid}")

    def on_close_vote(self) -> None:
        """结束当前投票。"""
        if not self.session.votes.state.target_uid:
            self._on_status("当前没有进行中的投票")
            return
        self.session.close_vote()
        self._on_status("投票已结束")

    def on_vote_uid_from_pool(self, 选中) -> None:
        """右键菜单：把选中的人设为投票对象。"""
        if not 选中:
            return
        e = 选中[0]
        if not self.session.open_vote_manual(e.uid, e.nick or ""):
            self._on_status("开启投票失败")
            return
        窗 = getattr(self.app, "vote_win", None)
        if 窗 is not None and not 窗.visible:
            窗.show()
        self._on_status(f"投票已开始，对象：{e.nick or e.uid}")

    def _on_pool_auto_change(self) -> None:
        """参与池自动清空设置变了。"""
        try:
            self.cfg.pool_auto_clear = self.var_poolauto.get()
            self.cfg.pool_auto_clear_minutes = max(
                0.1, float(self.var_poolmin.get()))
        except (tk.TclError, AttributeError, ValueError):
            return
        self.mark_dirty()
        # 改完立刻重新计时，不然要等上一轮周期走完才生效
        self.session.reset_pool_timer()
        self._refresh_countdowns()
        if not self.cfg.pool_auto_clear:
            self._on_status("已关闭自动清空参与池")

    def _refresh_countdowns(self) -> None:
        """刷新两个倒计时（投票剩余 / 下次清池）。"""
        try:
            if self.session.votes.is_open and self.cfg.vote.vote_seconds > 0:
                剩 = self.session.vote_remaining()
                self.var_voteleft.set(f"剩余 {剩:.0f} 秒")
            else:
                self.var_voteleft.set("")
        except Exception:
            pass
        try:
            if not self.cfg.pool_auto_clear:
                self.var_poolcountdown.set("（未开启）")
                return
            秒 = int(self.session.pool_clear_remaining())
            if 秒 <= 0:
                self.var_poolcountdown.set("即将清空…")
            else:
                分, 余 = divmod(秒, 60)
                self.var_poolcountdown.set(f"下次清池：{分}分{余:02d}秒")
        except Exception:
            pass

    def _on_board_toggle(self) -> None:
        """参与名单窗开关。"""
        想要 = bool(self.var_board.get())
        try:
            self.app.set_board_visible(想要)
        except Exception as exc:
            self._on_status(f"切换名单窗失败：{exc}")
            return
        self._on_status("参与名单窗已" + ("显示" if 想要 else "隐藏"))

    def sync_board_toggle(self) -> None:
        """把开关状态和窗口实际可见性对齐。

        F5 快捷键、右键菜单、或者窗口被别的方式关掉之后，
        界面上的勾选框要跟着变，不能显示错状态。
        """
        try:
            实际 = self.app.board_visible()
            if self.var_board.get() != 实际:
                self.var_board.set(实际)
        except Exception:
            pass

    def _on_vote_setting_change(self) -> None:
        """投票设置变了。"""
        try:
            self.cfg.vote.enabled = self.var_vote.get()
            self.cfg.vote.vote_seconds = max(
                0.0, float(self.var_votedur.get()))
        except (tk.TclError, AttributeError, ValueError):
            return
        # 投票正在跑的时候改时长，立刻按新秒数重算 —— 不用等这一轮结束
        try:
            self.session.rearm_vote()
        except Exception:
            pass
        self.mark_dirty()
        # 关掉时顺便把窗口收起来，免得留在屏幕上
        if not self.cfg.vote.enabled:
            窗 = getattr(self.app, "vote_win", None)
            if 窗 is not None and 窗.visible:
                窗.hide()
        self._on_status("观众投票已" + ("开启" if self.cfg.vote.enabled
                                        else "关闭"))

    def on_clear_manual(self) -> None:
        """清空所有手动调整的倍率。"""
        n = self.session.manual_mult_count()
        if not n:
            self._on_status("没有手动调整过倍率")
            return
        if not messagebox.askyesno(
                "清空手动倍率",
                f"有 {n} 个人被手动调过倍率。\n\n"
                f"清空后他们都回到正常权重。\n\n确定吗？",
                parent=self.root):
            return
        n = self.session.clear_manual_mult()
        self._刷新补偿提示()
        self.refresh_pool(force=True)
        self._refresh_watch_preview()
        self._on_status(f"已清空 {n} 条手动倍率设置")

    def on_offline(self) -> None:
        """下播：归档本场没中的人 -> 清空本场数据。"""
        n_pool = len(self.session.pool)
        n_win = len(self.session.pool.winners)
        没中 = max(0, n_pool - len({w.uid for w in self.session.pool.winners}))

        提示 = (
            f"本场有 {n_pool} 人参与、{n_win} 人中奖。\n\n"
            f"下播后会：\n"
            f"  · 清空参与池（{n_pool} 人）\n"
            f"  · 清空观看时长记录\n"
            f"  · 清空中奖记录\n\n"
            f"其中 {没中} 人本场没中，会记入待补偿名单，\n"
            f"下次开播他们自动获得 "
            f"{self.cfg.carry_mult:g} 倍权重。\n\n确定下播吗？"
        )
        if not messagebox.askyesno("下播", 提示, parent=self.root):
            return

        摘要 = self.session.offline()
        self.refresh_pool(force=True)
        self.refresh_winners()
        self._refresh_draw_tab()
        self._refresh_watch_preview()
        self._刷新补偿提示()
        self.var_proof.set("本轮还没有抽过")
        self._on_status(
            f"已下播：清空 {摘要['participants']} 人，"
            f"{摘要['carried']} 人转入下期补偿"
            f"（累计 {摘要['carry_total']} 人）")

    def on_reset(self) -> None:
        n = len(self.session.pool)
        if not n:
            self._on_status("参与池已经是空的")
            return
        if messagebox.askyesno("清空参与池",
                               f"确定清空这 {n} 个人的参与记录吗？\n\n"
                               f"中奖名单会保留。", parent=self.root):
            self.session.reset_pool(keep_winners=True)
            self.refresh_winners()

    def on_manual_add(self) -> None:
        text = self.var_manual.get().strip()
        if not text:
            return
        rec = self.session.feed(text, source="手动")
        if rec and rec.uid:
            self._on_status(f"已加入：{rec.uid}")
            self.var_manual.set("")
        else:
            self._on_status("没识别出 UID：换个写法试试，或者调低「最短位数」")

    def on_paste_import(self) -> None:
        data = self.txt_paste.get("1.0", "end")
        lines = [x for x in data.splitlines() if x.strip()]
        if not lines:
            return
        n = self.session.feed_many(lines, source="批量粘贴")
        self._on_status(f"批量导入完成：{len(lines)} 行里识别出 {n} 个 UID")
        self.txt_paste.delete("1.0", "end")

    def on_export_uids(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出 UID 列表",
            initialfile="参与UID列表.txt", defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            self.session.pool.export_uids(path)
            self._on_status(f"已导出到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def on_export_winners(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出中奖名单",
            initialfile="中奖名单.csv", defaultextension=".csv",
            filetypes=[("CSV 表格", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            self.session.pool.export_winners(path)
            self._on_status(f"已导出到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    # ---- 识别设置 ------------------------------------------------------ #

    def _on_mode_change(self) -> None:
        self.mark_dirty()
        self._run_test()

    def on_suggest_len(self) -> None:
        hist = self.session.pool.uid_length_histogram()
        if not hist:
            self._on_status("参与池还是空的，先收一点弹幕再试")
            return
        # 取占 90% 的位数区间
        total = sum(hist.values())
        acc = 0
        lo = hi = None
        for length, n in sorted(hist.items()):
            acc += n
            if lo is None:
                lo = length
            hi = length
            if acc >= total * 0.9:
                break
        self.var_minlen.set(max(1, lo))
        self.var_maxlen.set(hi)
        self.mark_dirty()
        self.var_lenhint.set(
            "当前参与池位数分布：" +
            "、".join(f"{k}位×{v}" for k, v in sorted(hist.items())) +
            f"  → 已把范围设为 {lo}~{hi}")
        self._run_test()

    def _schedule_test(self, ev=None) -> None:
        if self._test_job is not None:
            try:
                self.root.after_cancel(self._test_job)
            except Exception:
                pass
        self._test_job = self.root.after(280, self._run_test)

    def _run_test(self) -> None:
        self._test_job = None
        self.sync_extract_config()
        cfg = self.cfg.extract
        self.lb_test.delete(0, "end")
        data = self.txt_test.get("1.0", "end")
        for line in data.splitlines():
            if not line.strip():
                continue
            cands = extract(line, cfg)
            if cands:
                c = cands[0]
                self.lb_test.insert(
                    "end", f"√  {line}    →    {c.uid}    （{c.reason}）")
                self.lb_test.itemconfig("end", foreground=GOOD)
            else:
                self.lb_test.insert("end", f"×  {line}    →    未识别")
                self.lb_test.itemconfig("end", foreground=BAD)

    def sync_extract_config(self) -> None:
        ex = self.cfg.extract
        ex.mode = self.var_mode.get() if hasattr(self, "var_mode") else ex.mode
        if hasattr(self, "var_minlen"):
            try:
                ex.min_len = max(1, int(self.var_minlen.get()))
                ex.max_len = max(ex.min_len, int(self.var_maxlen.get()))
            except Exception:
                pass
        if hasattr(self, "var_letterfix"):
            ex.letter_fix = self.var_letterfix.get()
            ex.chinese_digits = self.var_cn.get()
            ex.block_common_numbers = self.var_blockcommon.get()
            ex.strip_label = self.var_strip.get()
            try:
                ex.max_gap = max(0, int(self.var_gap.get()))
            except Exception:
                pass
            try:
                # 空格用的上限单独一项 —— 观众为了对齐数字会敲好几个空格，
                # 和「符号」（点、横线）共用一个上限会把正常人误伤掉。
                ex.max_gap_space = max(0, int(self.var_gapspace.get()))
            except Exception:
                pass
            ex.require_keywords = tuple(self.var_req.get().split())
            ex.block_keywords = tuple(self.var_blk.get().split())

    # ---- 弹幕来源 ------------------------------------------------------ #

    def _on_http_toggle(self) -> None:
        self.cfg.source.http_enabled = self.var_http.get()
        self.cfg.source.http_port = int(self.var_port.get() or 8787)
        self.cfg.source.http_host = self.var_host.get().strip() or "127.0.0.1"
        self.app.restart_http()
        self._refresh_source_tab()
        self.mark_dirty()

    def _on_file_toggle(self) -> None:
        self.cfg.source.file_enabled = self.var_file.get()
        self.cfg.source.file_path = self.var_filepath.get().strip()
        self.cfg.source.file_format = self.var_filefmt.get()
        self.cfg.source.file_encoding = self.var_fileenc.get()
        self.app.restart_file_source()
        self.mark_dirty()

    def _on_demo_toggle(self) -> None:
        self.cfg.source.demo_enabled = self.var_demo.get()
        self.cfg.source.demo_interval_ms = int(self.var_demoms.get() or 900)
        self.app.restart_demo()
        self.mark_dirty()

    def on_pick_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="选择弹幕日志文件",
            filetypes=[("文本/日志", "*.txt *.log *.jsonl *.json"),
                       ("所有文件", "*.*")])
        if path:
            self.var_filepath.set(path)
            self.var_file.set(True)
            self._on_file_toggle()

    def _gen_token(self) -> None:
        import secrets
        self.var_token.set(secrets.token_urlsafe(9))
        self.cfg.source.http_token = self.var_token.get()
        self.mark_dirty()
        self._on_status("token 已生成。记得把它加到推送地址后面：?token=…")

    # ---- 抖音直连桥接 -------------------------------------------------- #

    def bridge_dir(self) -> str:
        return bridge_dir()

    def find_bridge_python(self) -> Optional[str]:
        return find_python_for_scripts()

    def bridge_cmd(self) -> str:
        room = self.var_dyroom.get().strip()
        base = f'douyin_bridge.py {room}'.rstrip()
        if self.app.net is not None and self.app.net.running:
            base += f" --push-to {self.app.base_url()}"
            if self.cfg.source.http_token:
                base += f" --token {self.cfg.source.http_token}"
        return "python " + base

    def on_open_bridge_dir(self) -> None:
        path = self.bridge_dir()
        if not os.path.isdir(path):
            messagebox.showinfo(
                "找不到桥接脚本",
                f"没找到目录：\n{path}\n\n"
                "如果你是用 exe 运行的，请把 tools 文件夹和 exe 放在同一层。",
                parent=self.root)
            return
        try:
            os.startfile(path)  # noqa: S606
        except Exception as exc:
            messagebox.showerror("打不开", str(exc), parent=self.root)

    def on_copy_bridge_cmd(self) -> None:
        cmd = self.bridge_cmd()
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self._on_status(f"已复制启动命令（在那个文件夹里执行）：{cmd}")

    def on_launch_bridge(self) -> None:
        path = self.bridge_dir()
        script = os.path.join(path, "douyin_bridge.py")
        if not os.path.isfile(script):
            messagebox.showinfo(
                "找不到桥接脚本",
                f"没找到：\n{script}\n\n"
                "如果你是用 exe 运行的，请把 tools 文件夹和 exe 放在同一层。",
                parent=self.root)
            return
        if self.app.net is None or not self.app.net.running:
            messagebox.showwarning(
                "先把弹幕接收服务打开",
                "「① HTTP 推送」还没启用，弹幕转发过来也没人接。\n"
                "请先勾上「启用弹幕接收服务」。", parent=self.root)
            return

        room = self.var_dyroom.get().strip()
        if not room:
            if not messagebox.askyesno(
                    "没填直播间号",
                    "你没有填自己的直播间号。\n\n"
                    "这样桥接会随便挑一个公开直播间，里面的弹幕不是你的观众发的，\n"
                    "对抽号没用。\n\n"
                    "确定要这样启动吗？（建议点「否」，先填上你的直播间号）",
                    parent=self.root):
                self._on_status("请先填上你的直播间号：网址 live.douyin.com/ 后面那串数字")
                return

        bat = os.path.join(path, "启动抖音直连.bat")
        python = self.find_bridge_python()

        # 优先走 .bat：它开头有说明、结尾有 pause，出错时窗口不会一闪而过
        if os.path.isfile(bat) and python:
            args = ["cmd.exe", "/c", bat]
            if room:
                args.append(room)
            args += ["--push-to", self.app.base_url()]
            if self.cfg.source.http_token:
                args += ["--token", self.cfg.source.http_token]
            what = "启动抖音直连.bat"
        elif python:
            args = [python, script]
            if room:
                args.append(room)
            args += ["--push-to", self.app.base_url()]
            if self.cfg.source.http_token:
                args += ["--token", self.cfg.source.http_token]
            what = "douyin_bridge.py"
        else:
            messagebox.showinfo(
                "需要一个 Python 才能跑桥接",
                "抽号器本身不依赖 Python，但「抖音弹幕直连」这个桥接脚本需要。\n\n"
                "请到 https://www.python.org/downloads/ 装一个 Python 3.10 以上版本，\n"
                "安装时记得勾选「Add python.exe to PATH」，装完再点这个按钮。\n\n"
                "装好之前也可以用其它方式接入弹幕：\n"
                "  • 「② 文件监听」：让别的弹幕助手把弹幕写进一个 txt\n"
                "  • 「① HTTP 推送」：任何工具 POST 到接收地址\n"
                "  • 「手动加一个 UID」：临时手打",
                parent=self.root)
            self._on_status("桥接需要 Python，请先安装后再试")
            return

        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = 0x00000010 | 0x00000200  # NEW_CONSOLE|NEW_PROCESS_GROUP
            subprocess.Popen(args, cwd=path, creationflags=creationflags)
            self.var_dystate.set(f"已启动（{what}）· 看新弹出的黑色窗口")
            self._on_status("抖音直连桥接已启动；日志页的来源会显示「抖音直连」")
        except Exception as exc:
            messagebox.showerror(
                "启动失败",
                f"{exc}\n\n手动运行：\n"
                f"1) 打开文件夹：{path}\n"
                f"2) 双击「启动抖音直连.bat」，或在里面执行：\n{self.bridge_cmd()}",
                parent=self.root)

    def on_copy_url(self) -> None:
        url = self.app.push_url()
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._on_status(f"已复制推送地址：{url}")

    def page_url(self, path: str = "/") -> str:
        """给本程序自己的网页地址补上 token（设了 token 时）。

        本机访问其实不需要 token（服务端对本机一律放行），
        但把链接发给手机访问时就需要了。
        """
        import urllib.parse
        url = self.app.base_url().rstrip("/") + path
        tok = (self.cfg.source.http_token or "").strip()
        if tok:
            url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(tok)
        return url

    def on_open_panel(self) -> None:
        import webbrowser
        webbrowser.open(self.page_url("/"))

    def on_open_overlay_page(self) -> None:
        import webbrowser
        webbrowser.open(self.page_url("/overlay"))

    def _refresh_source_tab(self) -> None:
        net = self.app.net
        if net is not None and net.running:
            self.var_http_url.set(self.app.push_url())
            from .netserver import local_ip
            ip = local_ip()
            lan_tok = ("?token=" + self.cfg.source.http_token
                       if self.cfg.source.http_token else "")
            self.var_lan.set(
                f"接口面板：{self.page_url('/')}\n"
                f"浏览器叠加页：{self.page_url('/overlay')}\n"
                f"局域网推送地址（手机用）：http://{ip}:{net.port}/push{lan_tok}")
            # 标题带上端口，多开时一眼能分清哪个窗口是哪台
            try:
                self.root.title(f"{APP_NAME} · 控制台 〔端口 {net.port}〕")
            except Exception:
                pass
            if net.conflict:
                self.var_portwarn.set(
                    f"⚠ 端口 {net.requested_port} 被占用了，本程序临时改用 "
                    f"{net.port}。\n"
                    f"　　如果抖音直连/推送工具还在往 {net.requested_port} 发，"
                    f"弹幕会跑进另一个抽号器实例里 ——\n"
                    f"　　表现就是「日志里识别成功，但这里的参与人数不涨」。\n"
                    f"　　请结束多余的「UID抽号器」进程后重开，"
                    f"或改用上面的地址：http://127.0.0.1:{net.port}")
            else:
                self.var_portwarn.set("")
        else:
            self.var_http_url.set("未启动")
            self.var_lan.set("")
            self.var_portwarn.set("")

        if self.app.file_source is not None and self.app.file_source.running:
            self.var_filestate.set("监听中：" + self.cfg.source.file_path)
        else:
            self.var_filestate.set("未启动")

    # ---- 展示窗口 ------------------------------------------------------ #

    def _刷新窗口尺寸显示(self) -> None:
        try:
            self.var_overlaygeom.set(self.cfg.overlay.geom or "（未设置）")
            self.var_boardgeom.set(self.cfg.board.geom or "（未设置）")
        except Exception:
            pass

    def on_reset_overlay(self) -> None:
        """把展示窗恢复默认大小和位置。"""
        ov = getattr(self.app, "overlay", None)
        if ov is None:
            self._on_status("展示窗还没打开（在「抽取」页勾「显示展示窗口」）")
            return
        ov.reset_size()
        self._刷新窗口尺寸显示()
        self.refresh_pool(force=False)

    def on_reset_board(self) -> None:
        """把参与名单窗恢复默认大小和位置。"""
        bd = getattr(self.app, "board", None)
        if bd is None:
            self._on_status("参与名单窗还没打开")
            return
        bd.reset_size()
        self._刷新窗口尺寸显示()

    def on_reset_both(self) -> None:
        """两个窗口一起重置。"""
        n = 0
        ov = getattr(self.app, "overlay", None)
        bd = getattr(self.app, "board", None)
        if ov is not None:
            ov.reset_size(提示=False)
            n += 1
        if bd is not None:
            bd.reset_size(提示=False)
            n += 1
        self._刷新窗口尺寸显示()
        if n:
            self._on_status(f"已重置 {n} 个窗口的大小和位置")
        else:
            self._on_status("两个窗口都没打开")

    def _on_bgmode_change(self) -> None:
        self.cfg.overlay.bg_mode = self.var_bgmode.get()
        self.apply_overlay_now()

    def set_bgcolor(self, color: str) -> None:
        self.cfg.overlay.bg_color = color
        try:
            self.btn_bgcolor.configure(bg=color)
        except Exception:
            pass
        self.apply_overlay_now()

    def on_pick_bgcolor(self) -> None:
        c = colorchooser.askcolor(color=self.cfg.overlay.bg_color,
                                  parent=self.root, title="选择背景色")
        if c and c[1]:
            self.set_bgcolor(c[1])

    def _pick_color(self, var: tk.StringVar, key: str) -> None:
        c = colorchooser.askcolor(color=var.get(), parent=self.root,
                                  title="选择颜色")
        if c and c[1]:
            var.set(c[1])
            setattr(self.cfg.overlay, key, c[1])
            self.apply_overlay_now()

    def on_pick_image(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="选择背景图片",
            filetypes=[("图片", "*.png *.gif *.jpg *.jpeg *.bmp *.webp"),
                       ("所有文件", "*.*")])
        if not path:
            return
        self.var_imgpath.set(path)
        self.cfg.overlay.image_path = path
        self.var_bgmode.set(BgMode.IMAGE)
        self.cfg.overlay.bg_mode = BgMode.IMAGE
        self.apply_overlay_now()

    def _replan_windows(self) -> None:
        """按当前屏幕重新安排两个捕获窗口的位置。

        以前这两个按钮各自写死一个坐标，其中名单窗那个用的正是
        ``+880+80`` —— 和展示窗的旧默认值一模一样，点一下两个窗口就叠住了。
        现在统一走 layout 模块，保证错开、且都在屏幕里。
        """
        from .layout import plan
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            return
        plan(self.cfg, sw, sh, force=True)
        self.session.bus.emit(B.CONFIG_CHANGED, None)
        # 无边框窗口改完几何要再摆一次，否则会被窗口管理器改回去
        if self.app.overlay is not None:
            self.app.overlay.place_again()
        if self.app.board is not None:
            self.app.board.place_again()
        self.mark_dirty()

    def on_reset_geom(self) -> None:
        """重新摆一遍两个捕获窗口（保证不重叠、不挡控制台）。"""
        self._replan_windows()
        self._on_status("两个捕获窗口的位置已重新安排")

    # ---- 采集助手 ------------------------------------------------------ #

    def _need_overlay(self) -> bool:
        if self.app.overlay is None:
            self.app.toggle_overlay()
            self._on_status("展示窗刚被创建，再点一次这个按钮")
            return False
        return True

    def on_capture_check(self) -> None:
        if not self._need_overlay():
            return
        self.apply_overlay_now()
        report = self.app.overlay.capture_report()
        self._show_report("展示窗采集体检", report)
        if "能不能列到它：能" in report:
            self.var_capstate.set("上次体检：能 ✅ 直播伴侣的窗口列表里应该能看到它")
        else:
            self.var_capstate.set("上次体检：不能 ❌ 点一下「一键修复采集样式」")

    def on_capture_fix(self) -> None:
        if not self._need_overlay():
            return
        info = self.app.overlay.ensure_capturable(verbose=True)
        report = self.app.overlay.capture_report()
        tip = ("已经补好样式了。\n\n"
               "现在到直播伴侣里：删掉原来的「窗口捕获」素材，"
               "重新「添加素材 → 窗口捕获」，在下拉列表里选：\n"
               f"    {self.cfg.overlay.window_title}\n\n"
               "如果还是看不到，把「无边框（推荐）」的勾去掉，再点"
               "「应用外观到展示窗」—— 有标题栏的窗口一定能被列出来。"
               "（代价是采集时会把标题栏也拍进去）\n\n"
               "另外记得在直播伴侣里刷新一下素材列表，"
               "它的窗口下拉框有时候是打开时才扫描一次的。")
        self._show_report("采集样式已修复", tip + "\n\n" + "=" * 50 + "\n" + report)
        self.var_capstate.set(
            f"已补样式：{'、'.join(info.get('exstyle_names') or []) or '（无）'}  "
            f"可被采集={'是' if info.get('capturable') else '否'}")

    def on_list_capturable(self) -> None:
        from . import winutil
        wins = winutil.list_capturable_windows()
        mine = ""
        if self.app.overlay is not None:
            h = getattr(self.app.overlay, "hwnd", 0)
            mine = next((w["title"] for w in wins if w["hwnd"] == h), "")
        lines = [
            f"采集软件（直播伴侣 / OBS 这类）会列出下面这 {len(wins)} 个窗口。",
            "如果你的展示窗不在里面，就是样式被过滤了，点「一键修复采集样式」。",
            "",
        ]
        for i, w in enumerate(wins, 1):
            mark = "   ← 我们的展示窗" if w["title"] == mine and mine else ""
            lines.append(f"{i:3}. {w['title']}{mark}")
        self._show_report("采集软件能看到的窗口", "\n".join(lines))

    def _show_report(self, title: str, text: str) -> None:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("760x480+120+120")
        win.transient(self.root)
        box = tk.Text(win, wrap="word", font=(self.ui_family, 10),
                      padx=10, pady=8)
        vs = ttk.Scrollbar(win, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        box.pack(fill="both", expand=True, padx=(8, 0), pady=(8, 0))
        box.insert("1.0", text)
        box.configure(state="disabled")

        bar = ttk.Frame(win, padding=(8, 6))
        bar.pack(fill="x", side="bottom")
        ttk.Button(bar, text="复制全文",
                   command=lambda: self._copy_to_clipboard(text)).pack(side="left")
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="left", padx=6)

    def _copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._on_status("已复制到剪贴板")

    def sync_overlay_config(self) -> None:
        oc = self.cfg.overlay
        oc.bg_mode = self.var_bgmode.get()
        oc.image_path = self.var_imgpath.get().strip()
        oc.font_family = self.var_font.get().strip() or oc.font_family
        try:
            oc.uid_size = int(self.var_uidsize.get())
            oc.title_size = int(self.var_titlesize.get())
            oc.count_size = int(self.var_countsize.get())
            oc.outline_width = max(0, int(self.var_fontsize_hint.get()))
        except Exception:
            pass
        oc.uid_color = self.var_uidcolor.get().strip() or oc.uid_color
        oc.title_color = self.var_titlecolor.get().strip() or oc.title_color
        oc.count_color = self.var_countcolor.get().strip() or oc.count_color
        oc.outline_color = self.var_outlinecolor.get().strip() or oc.outline_color
        oc.title_text = self.var_titletext.get()
        oc.idle_text = self.var_idletext.get() or oc.idle_text
        oc.count_prefix = self.var_countprefix.get() or oc.count_prefix
        oc.show_title = self.var_showtitle.get()
        oc.show_count = self.var_showcount.get()
        oc.show_feed = self.var_showfeed.get()
        oc.borderless = self.var_borderless.get()
        oc.always_on_top = self.var_topmost.get()
        oc.window_title = self.var_wintitle.get() or oc.window_title

    def apply_overlay_now(self) -> None:
        self.sync_overlay_config()
        self.session.bus.emit(B.CONFIG_CHANGED, None)
        self.mark_dirty()

    def _apply_soon(self) -> None:
        """延迟一拍再应用外观。

        构建界面时有些变量还没建好，直接调 apply_overlay_now 会 AttributeError；
        等这一轮界面搭建跑完再执行就安全了。
        """
        try:
            self.root.after(1, self.apply_overlay_now)
        except Exception:
            pass

    # ---- 参与名单窗 ---------------------------------------------------- #

    def sync_board_config(self) -> None:
        bc = self.cfg.board
        if not hasattr(self, "var_bsize"):
            return
        try:
            bc.uid_size = max(8, int(self.var_bsize.get()))
            bc.speed = max(40, int(self.var_bspeed.get()))
            bc.track_count = max(1, int(self.var_btracks.get()))
            bc.track_gap = max(20, int(self.var_btgap.get()))
        except Exception:
            pass
        bc.show_title = self.var_btitle.get()
        bc.title_text = self.var_btitletext.get() or bc.title_text
        bc.show_footer = self.var_bfooter.get()
        bc.show_suffix = self.var_bsuffix.get()
        bc.suffix = self.var_bsuffixtext.get() or bc.suffix
        bc.uid_color = self.var_bcolor.get().strip() or bc.uid_color
        bc.suffix_color = self.var_bsuffixcolor.get().strip() or bc.suffix_color
        bc.always_on_top = self.var_btopmost.get()

    def on_reset_board_geom(self) -> None:
        """按当前屏幕重新摆两个窗口（名单窗会回到错开的标准位置）。"""
        self._replan_windows()
        self._on_status("参与名单窗位置已重置")

    def on_board_flip(self) -> None:
        # 弹幕式名单窗没有「翻页」概念了，这里保留按钮兼容，
        # 实际作用改为：临时发一条示例弹幕看看效果。
        if self.app.board is None:
            self.app.toggle_board()
            self._on_status("名单窗刚创建，再点一次")
            return
        self.app.board._spawn("示例 UID")

    def on_capture_check_board(self) -> None:
        if self.app.board is None:
            self.app.toggle_board()
            self._on_status("名单窗刚创建，再点一次这个按钮")
            return
        self.apply_overlay_now()
        report = self.app.overlay.capture_report() + "\n\n" + \
            self.app.board.capture_report()
        self._show_report("展示窗 / 名单窗 采集体检", report)

    # ---- 参与池 -------------------------------------------------------- #

    def schedule_pool_refresh(self, delay: int = 800) -> None:
        if self._pool_job is not None:
            try:
                self.root.after_cancel(self._pool_job)
            except Exception:
                pass
        self._pool_job = self.root.after(delay, self.refresh_pool)

    def refresh_pool(self, force: bool = False) -> None:
        self._pool_job = None
        try:
            idx = self.nb.index(self.nb.select())
        except Exception:
            idx = -1
        if idx != 4 and not force:
            self.schedule_pool_refresh(1500)
            return

        kw = self.var_search.get().strip().lower()
        entries = self.session.pool.all_entries()
        if kw:
            entries = [e for e in entries
                       if kw in e.uid or kw in (e.nick or "").lower()
                       or kw in (e.raw or "").lower()]
        entries.sort(key=lambda e: -e.first_ts)

        LIMIT = 3000
        shown = entries[:LIMIT]
        self._pool_rows = shown

        # ---- 先算出每个人的权重和中奖概率 -------------------------------- #
        # 中奖概率 = 这个人的权重 / 所有有资格的人权重之和。
        # 注意分母用的是「有资格参与抽奖的人」（中过奖的会按配置排除），
        # 这样表里显示的百分比和实际抽奖时的概率是一致的。
        from .draw import compute_weights
        资格 = self.session.pool.eligible(self.cfg.no_repeat_winner)
        概率 = {}
        try:
            现在 = time.time()
            opt = self.session.weight_options()
            ws = compute_weights(资格, 现在, opt)
            总和 = sum(ws)
            if 总和 > 0:
                for e, w in zip(资格, ws):
                    概率[e.uid] = w / 总和 * 100.0
        except Exception:
            traceback.print_exc()

        self.tv.delete(*self.tv.get_children())
        # ★ 显式给 iid 编号，让它等于 self._pool_rows 里的下标。
        #   不指定的话 Tk 会自己生成 "I001"/"I002" 这种 iid，
        #   右键菜单就没法从选中的行翻回是哪个人了。
        现在 = time.time()
        for i, e in enumerate(shown):
            # 观看时长：从首次出现到现在
            秒 = max(0.0, 现在 - e.first_ts) if e.first_ts else 0.0
            if 秒 >= 3600:
                时长 = f"{秒/3600:.1f} 小时"
            elif 秒 >= 60:
                时长 = f"{秒/60:.0f} 分钟"
            else:
                时长 = f"{秒:.0f} 秒"
            p = 概率.get(e.uid)
            概率文本 = f"{p:.2f}%" if p is not None else "—（已中奖）"
            倍 = float(getattr(e, "weight_mult", 0.0) or 0.0)
            倍率文本 = "—" if 倍 <= 0 else (f"{倍:g}×" if abs(倍 - 1) > 1e-9
                                          else "正常")
            self.tv.insert("", "end", iid=str(i), values=(
                e.uid, e.nick or "-", e.count, 时长, 概率文本, 倍率文本,
                time.strftime("%H:%M:%S", time.localtime(e.first_ts)),
                e.source or "-", e.raw or "",
            ))

        total = len(self.session.pool)
        hist = self.session.pool.uid_length_histogram()
        info = f"共 {total} 人"
        if kw:
            info += f"，匹配 {len(entries)} 人"
        if len(entries) > LIMIT:
            info += f"（只显示前 {LIMIT} 条）"
        if hist:
            info += "    位数分布：" + "、".join(
                f"{k}位×{v}" for k, v in sorted(hist.items())[:8])
        self.var_poolinfo.set(info)

        if not force:
            self.schedule_pool_refresh(2000)

    def _selected_uids(self) -> List[str]:
        out = []
        for item in self.tv.selection():
            vals = self.tv.item(item, "values")
            if vals:
                out.append(str(vals[0]))
        return out

    def on_remove_selected(self) -> None:
        uids = self._selected_uids()
        if not uids:
            return
        if not messagebox.askyesno("删除",
                                   f"从参与池里删掉这 {len(uids)} 个人？",
                                   parent=self.root):
            return
        for uid in uids:
            self.session.remove_uid(uid)
        self.refresh_pool(force=True)

    def on_delete_by_uid(self) -> None:
        """按 UID 文本框手动删掉一个人。

        比先搜索再选中再点删除快得多 —— 主播手上有一串要清掉的号时特别有用。
        支持一次贴多个（空格/逗号/换行分隔）。
        """
        import re
        原文 = self.var_deluid.get().strip()
        if not 原文:
            self._on_status("先在左边的框里填 UID")
            return
        # 一行一个、用空格或逗号隔开都行
        候选 = re.split(r"[\s,，;；]+", 原文)
        uids = [u for u in 候选 if u]
        删掉的 = []
        没找到 = []
        for u in uids:
            # 顺手做一次归一化（去掉横线、点之类）
            try:
                from .normalize import normalize_uid
                u = normalize_uid(u) or u
            except Exception:
                pass
            if self.session.remove_uid(u):
                删掉的.append(u)
            else:
                没找到.append(u)
        self.var_deluid.set("")
        self.refresh_pool(force=True)
        提示 = f"已从参与池删除 {len(删掉的)} 个 UID"
        if 删掉的:
            提示 += "：" + "、".join(删掉的[:6])
            if len(删掉的) > 6:
                提示 += " 等"
        if 没找到:
            提示 += f"；{len(没找到)} 个不在池里"
        self._on_status(提示)

    def on_clear_watch(self) -> None:
        """清空「观看时长」记录。

        清空参与池**不会**清这个（老观众的时长优势要保留），
        所以开一场全新的直播、想让所有人重新计时时，用这个按钮。
        """
        n = len(self.session.first_seen)
        if not n:
            self._on_status("目前没有观看时长记录")
            return
        if not messagebox.askyesno(
                "清空观看时长",
                f"会清掉 {n} 个人的「首次出现时间」。\n\n"
                f"清掉之后，他们下次发弹幕会当成「刚刚才来」，\n"
                f"观看时长加权要从 0 重新累计。\n\n确定吗？",
                parent=self.root):
            return
        n = self.session.forget_first_seen()
        # 池里那批人的 first_ts 也要跟着重置，否则表里还显示旧时长
        try:
            for e in self.session.pool.all_entries():
                e.first_ts = 0.0
        except Exception:
            pass
        self.refresh_pool(force=True)
        self._refresh_watch_preview()
        self._on_status(f"已清空 {n} 条观看时长记录，所有人从现在重新计时")

    def on_set_weight_mult(self, 选中, 倍率: float) -> None:
        """给选中的人设中奖倍率。

        倍率乘在「观看时长/发言次数」算出来的权重上：
          1.0（或 0）= 正常，不干预
          2.0        = 中奖率翻倍
          0.5        = 减半
        用倍率而不是直接指定百分比 —— 别人权重变了，他还能保持相对关系。
        """
        if not 选中:
            return
        值 = float(倍率)
        # ★ 走 session 的方法：它会把倍率写进独立的倍率表，
        #   清空参与池时不会丢。
        self.session.set_manual_mult([e.uid for e in 选中], 值)
        self.session.pool.save()
        self.refresh_pool(force=True)
        self._refresh_watch_preview()
        if abs(值 - 1.0) < 1e-9:
            self._on_status(f"已把 {len(选中)} 人的倍率恢复为正常")
        else:
            self._on_status(f"已把 {len(选中)} 人的中奖倍率设为 {值:g}×")

    def on_set_weight_mult_custom(self, 选中) -> None:
        """弹出输入框，自定义倍率。"""
        from tkinter import simpledialog
        if not 选中:
            return
        名 = 选中[0].nick or 选中[0].uid
        更多 = f"（共 {len(选中)} 人）" if len(选中) > 1 else ""
        值 = simpledialog.askfloat(
            "调整中奖倍率",
            f"给「{名}」{更多} 设置中奖倍率：\n\n"
            f"1 = 正常，2 = 中奖率翻倍，0.5 = 减半\n"
            f"直接填 1 就是恢复默认。",
            initialvalue=float(getattr(选中[0], "weight_mult", 0) or 1.0),
            minvalue=0.01, maxvalue=999.0, parent=self.root)
        if 值 is None:
            return
        self.on_set_weight_mult(选中, 值)

    def on_blacklist_selected(self) -> None:
        uids = self._selected_uids()
        if not uids:
            return
        for uid in uids:
            self.session.blacklist_uid(uid)
        self.refresh_pool(force=True)

    # ---- 日志 ---------------------------------------------------------- #

    def on_clear_log(self) -> None:
        self.tv_log.delete(*self.tv_log.get_children())
        self._log_count = 0

    def on_open_data_dir(self) -> None:
        try:
            os.startfile(data_dir())  # noqa: S606
        except Exception as exc:
            messagebox.showerror("打不开", str(exc), parent=self.root)

    def _on_danmaku(self, rec: DanmakuRecord) -> None:
        if rec.source == "抖音直连" and hasattr(self, "var_dystate"):
            self.var_dystate.set(f"运行中 · 最近一条 {rec.clock}")
        if self.var_onlyok.get() and not rec.uid:
            return
        status = STATUS_LABELS.get(rec.status, rec.status)
        tag = "ok" if rec.uid else "bad"
        self.tv_log.insert("", "end", values=(
            rec.clock, rec.source or "-", rec.raw, rec.uid or "-",
            status, rec.reason or "",
        ), tags=(tag,))
        self._log_count += 1
        if self._log_count > 2000:
            kids = self.tv_log.get_children()
            for k in kids[:500]:
                self.tv_log.delete(k)
            self._log_count -= 500
        try:
            self.tv_log.yview_moveto(1.0)
        except Exception:
            pass

    # ---- 事件 ---------------------------------------------------------- #

    def _on_log(self, msg: str) -> None:
        self._on_status(str(msg))

    def _on_error(self, msg: str) -> None:
        self._on_status("[!] " + str(msg))

    def _on_status(self, msg: str) -> None:
        self.var_status.set(str(msg))

    def _on_source_state(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("source") == "file":
            self.var_filestate.set(str(payload.get("text", "")))

    def _on_draw_done(self, result) -> None:
        if result is None:
            return
        self.var_proof.set("本轮凭证（可发到公屏自证）：" + result.proof())
        self.refresh_winners()
        self._refresh_draw_tab()

        # ★ 把刚抽中的人自动填进「手动投票对象」。
        #   必须**覆盖**旧内容：如果留着上次手动填的号码，
        #   点「开始投票」就会拿旧号开票，看起来就像抽中的号没加进去。
        if result.winners and hasattr(self, "var_voteuid"):
            self.var_voteuid.set(result.winners[0].uid)
        # 开了「抽中后自动复制」就顺手把中奖 UID 放进剪贴板
        if self.cfg.copy_winner_after_draw and result.winners:
            ws = result.winners
            # ★ 不再写死 带昵称=False：那样就算用户勾了「复制时带昵称」，
            #   自动复制也不生效。传 None 让它按当前设置走。
            文本 = "\n".join(self._winner_text(w) for w in ws)
            self.copy_text(
                文本,
                "已自动复制中奖 UID：" + "、".join(w.uid for w in ws)
                + "（Ctrl+V 即可粘贴）")

    def refresh_winners(self) -> None:
        self.lb_winners.delete(0, "end")
        # 列表和 self._winner_rows 必须一一对应，点行才能翻回 UID
        self._winner_rows = list(reversed(self.session.pool.winners[-200:]))
        for w in self._winner_rows:
            t = time.strftime("%H:%M:%S", time.localtime(w.ts))
            nick = f"  {w.nick}" if w.nick else ""
            self.lb_winners.insert(
                "end", f"第{w.round_no}轮  {t}   {w.uid}{nick}")

    # ---- 复制中奖 UID（本版新增）--------------------------------------- #

    def copy_text(self, 内容: str, 提示: str = "") -> bool:
        """把文本放进系统剪贴板。

        ``update()`` 是必须的：Tk 设完剪贴板要把所有权交给系统，
        不调一次的话程序退出后剪贴板会空掉。
        """
        if not 内容:
            self._on_status("没有可复制的内容")
            return False
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(内容)
            self.root.update()          # 让 Windows 真正接过剪贴板内容
        except Exception as exc:
            self._on_status(f"复制失败：{exc}")
            return False
        self._on_status(提示 or f"已复制：{内容}")
        return True

    def _winner_text(self, w, 带昵称: Optional[bool] = None) -> str:
        """按配置把一条中奖记录变成要复制的文本。"""
        if 带昵称 is None:
            带昵称 = bool(self.var_copynick.get()) if hasattr(
                self, "var_copynick") else bool(self.cfg.copy_with_nick)
        if 带昵称 and getattr(w, "nick", ""):
            return f"{w.nick}({w.uid})"
        return w.uid

    def _on_winner_click(self, _e=None) -> None:
        """点中奖名单里的一行 —— 直接复制那个 UID。

        这是这个功能最主要的使用方式：抽完想搜人，鼠标点一下就能去粘贴，
        不用手打 9 位数字（很容易看错一位）。
        """
        idx = self._选中行()
        if idx is None or idx >= len(self._winner_rows):
            return
        w = self._winner_rows[idx]
        self.copy_text(self._winner_text(w), f"已复制中奖 UID：{w.uid}")

    def _on_winner_double(self, _e=None) -> None:
        """双击 —— 复制「昵称(UID)」，方便发出去给别人看。"""
        idx = self._选中行()
        if idx is None or idx >= len(self._winner_rows):
            return
        w = self._winner_rows[idx]
        self.copy_text(self._winner_text(w, 带昵称=True),
                       f"已复制：{w.nick}({w.uid})")

    def _选中行(self):
        """当前选中的行号；没选中返回 None。"""
        try:
            sel = self.lb_winners.curselection()
            return int(sel[0]) if sel else None
        except Exception:
            return None

    def _on_winner_menu(self, ev) -> None:
        """右键菜单：几种常用复制格式。"""
        # 先把右键点到的那一行选中，菜单里的操作才有对象
        try:
            idx = self.lb_winners.nearest(ev.y)
            self.lb_winners.selection_clear(0, "end")
            self.lb_winners.selection_set(idx)
            self.lb_winners.activate(idx)
        except Exception:
            return
        if idx >= len(self._winner_rows):
            return
        w = self._winner_rows[idx]

        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=f"复制 UID（{w.uid}）",
                      command=lambda: self.copy_text(
                          w.uid, f"已复制中奖 UID：{w.uid}"))
        m.add_command(label=f"复制 昵称(UID)（{w.nick}）",
                      command=lambda: self.copy_text(
                          f"{w.nick}({w.uid})" if w.nick else w.uid,
                          f"已复制：{w.nick}({w.uid})"))
        if w.nick:
            m.add_command(label=f"只复制昵称（{w.nick}）",
                          command=lambda: self.copy_text(
                              w.nick, f"已复制昵称：{w.nick}"))
        m.add_separator()
        m.add_command(label="复制这一轮的凭证",
                      command=lambda: self.copy_text(
                          self.var_proof.get().replace(
                              "本轮凭证（可发到公屏自证）：", ""),
                          "已复制本轮凭证"))
        m.add_command(label="复制全部中奖 UID",
                      command=self.on_copy_all_winners)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def on_copy_last_winner(self) -> None:
        """复制最近一次中奖的 UID（一次抽多个就全复制，换行分隔）。"""
        ws = self.session.pool.winners
        if not ws:
            self._on_status("还没有人中奖")
            return
        if self.session.last_result and self.session.last_result.winners:
            本轮 = self.session.last_result.winners
        else:
            最近轮次 = ws[-1].round_no
            本轮 = [w for w in ws if w.round_no == 最近轮次]
        文本 = "\n".join(self._winner_text(w) for w in 本轮)
        self.copy_text(文本, f"已复制最近一轮中奖 UID（{len(本轮)} 个）："
                             + "、".join(w.uid for w in 本轮))

    def on_copy_all_winners(self) -> None:
        """复制全部中奖 UID，一行一个（方便粘到搜索框/表格里批量用）。"""
        ws = self.session.pool.winners
        if not ws:
            self._on_status("还没有人中奖")
            return
        文本 = "\n".join(self._winner_text(w) for w in ws)
        self.copy_text(文本, f"已复制全部中奖 UID，共 {len(ws)} 个")

    def _on_autocopy_change(self) -> None:
        self.cfg.copy_winner_after_draw = self.var_autocopy.get()
        self.mark_dirty()
        if self.cfg.copy_winner_after_draw:
            self._on_status("已开启：每次抽完自动把中奖 UID 放进剪贴板")

    def _on_copynick_change(self) -> None:
        self.cfg.copy_with_nick = self.var_copynick.get()
        self.mark_dirty()

    # ---- 参与池的复制（右键菜单）--------------------------------------- #

    def _on_pool_menu(self, ev) -> None:
        """参与池列表右键：复制选中的人。"""
        try:
            items = self.tv.selection()
        except Exception:
            items = ()
        if not items:
            # 没选中就自动选右键点到的那一行
            try:
                row = self.tv.identify_row(ev.y)
                if row:
                    self.tv.selection_set(row)
                    items = (row,)
            except Exception:
                pass
        if not items:
            return

        # Treeview 的 iid 就是行号，拿去索引 self._pool_rows
        选中 = []
        for iid in items:
            try:
                i = int(iid)
                if 0 <= i < len(self._pool_rows):
                    选中.append(self._pool_rows[i])
            except (ValueError, TypeError):
                continue
        if not 选中:
            return

        m = tk.Menu(self.root, tearoff=0)
        m.add_command(
            label=f"复制 UID（{len(选中)} 个）",
            command=lambda: self.copy_text(
                "\n".join(e.uid for e in 选中),
                f"已复制 {len(选中)} 个 UID"))
        # ---- 手动调整中奖倍率 ----
        倍率菜单 = tk.Menu(m, tearoff=0)
        for 值, 名 in ((1.0, "正常（1 倍）"), (2.0, "2 倍"),
                       (3.0, "3 倍"), (5.0, "5 倍"),
                       (10.0, "10 倍"), (0.5, "减半（0.5 倍）"),
                       (0.2, "降到 20%（0.2 倍）")):
            倍率菜单.add_command(
                label=名,
                command=lambda v=值: self.on_set_weight_mult(选中, v))
        倍率菜单.add_separator()
        倍率菜单.add_command(
            label="自定义…",
            command=lambda: self.on_set_weight_mult_custom(选中))
        m.add_cascade(label="调整中奖倍率", menu=倍率菜单)

        m.add_command(
            label=f"加入投票（{选中[0].nick or 选中[0].uid}）",
            command=lambda: self.on_vote_uid_from_pool(选中))

        m.add_command(
            label="复制 昵称(UID)",
            command=lambda: self.copy_text(
                "\n".join(f"{e.nick}({e.uid})" if e.nick else e.uid
                          for e in 选中),
                f"已复制 {len(选中)} 个人的昵称+UID"))
        if len(选中) == 1:
            m.add_separator()
            m.add_command(
                label=f"复制原始弹幕（{选中[0].raw}）",
                command=lambda: self.copy_text(
                    选中[0].raw, "已复制原始弹幕"))
            m.add_command(
                label="复制首次出现时间",
                command=lambda: self.copy_text(
                    time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(选中[0].first_ts)),
                    "已复制首次出现时间"))
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def _set_if_changed(self, var: tk.StringVar, 新值: str) -> None:
        """值没变就不碰这个 StringVar。

        ★ 这是主循环里最值得省的一处：Tk 的 ``set()`` 即使写进去的字符串
        一模一样，也会把标签标脏、排队重绘一次。原来每 60 毫秒无条件 set 五个
        变量（每秒 80 多次重绘），占了不小的 CPU —— 而实际上大部分时候
        数字根本没变。先比较再 set，重绘次数立刻降下来。
        """
        try:
            if var.get() != 新值:
                var.set(新值)
        except Exception:
            pass

    def _refresh_draw_tab(self) -> None:
        st = self.session.pool.stats()
        self._set_if_changed(self.var_participants, str(st["participants"]))
        self._set_if_changed(self.var_eligible, str(len(
            self.session.pool.eligible(self.cfg.no_repeat_winner))))
        self._set_if_changed(self.var_received, str(self.session.feed_total))
        self._set_if_changed(
            self.var_stats,
            f"参与 {st['participants']} · 中奖 {st['winners']} · "
            f"识别 {self.session.feed_matched} / 忽略 {self.session.feed_ignored}")

    # ---- 配置同步 ------------------------------------------------------ #

    def _on_count_change(self) -> None:
        try:
            self.cfg.draw_count = max(1, int(self.var_count.get()))
        except Exception:
            pass
        self.mark_dirty()

    def _on_norepeat_change(self) -> None:
        self.cfg.no_repeat_winner = self.var_norepeat.get()
        self._refresh_draw_tab()
        self.mark_dirty()

    def _on_weight_change(self) -> None:
        self.cfg.weighted_by_count = self.var_weight.get()
        if self.cfg.weighted_by_count:
            self._on_status("已开启按发言次数加权：刷屏的人中奖概率更高，"
                            "一般不建议开")
        self.mark_dirty()
        self._refresh_watch_preview()

    def _on_pity_change(self) -> None:
        """保底设置变了。"""
        try:
            self.cfg.pity_enabled = self.var_pity.get()
            self.cfg.pity_rounds = max(1, int(self.var_pityrounds.get()))
            self.cfg.pity_mode = self.var_pitymode.get()
        except (ValueError, tk.TclError):
            return
        self.mark_dirty()
        self._refresh_watch_preview()

    def _on_watch_change(self) -> None:
        """「观看时长加权」的参数变了。"""
        try:
            self.cfg.watch_full_minutes = max(1.0, float(self.var_watchfull.get()))
            self.cfg.watch_bonus = max(0.0, float(self.var_watchbonus.get()))
        except Exception:
            pass
        self.cfg.weight_by_watch = self.var_watch.get()
        self.cfg.weight_watch_unlimited = self.var_unlimited.get()
        self.mark_dirty()
        self._refresh_watch_preview()

    def _refresh_watch_preview(self) -> None:
        """把当前权重设置的**实际效果**显示出来，别让主播靠猜。

        这是「加权重」这件事最需要的反馈：调完参数立刻看到
        老观众和新观众差多少倍，而不是抽完才发现概率不对。
        """
        if not hasattr(self, "var_watchinfo"):
            return
        if not self.cfg.weight_by_watch:
            self.var_watchinfo.set("当前：等概率抽取（每个人中奖概率一样）")
            return
        try:
            from .draw import weight_report
            entries = self.session.pool.eligible(self.cfg.no_repeat_winner)
            if not entries:
                self.var_watchinfo.set("当前池里没人，抽一次就能看到权重分布了")
                return
            rep = weight_report(entries, time.time(), self.session.weight_options())
            # 把权重换算成「中奖概率百分比」显示 —— 比权重数字直观得多：
            # 权重 1.0/3.0 看不出什么，换成 2.1% / 6.3% 就一目了然了。
            from .draw import compute_weights
            ws = compute_weights(entries, time.time(),
                                 self.session.weight_options())
            总和 = sum(ws) or 1.0
            最久_p = rep['max'] / 总和 * 100.0
            最新_p = rep['min'] / 总和 * 100.0
            self.var_watchinfo.set(
                f"当前 {rep['count']} 人有资格　·　"
                f"看得最久的 {rep['oldest_watched']/60:.0f} 分钟 → 中奖概率 {最久_p:.2f}%　·　"
                f"最新来的 → {最新_p:.2f}%（{rep['oldest_times']:.1f} 倍）")
        except Exception:
            self.var_watchinfo.set("")

    def on_hide_console(self) -> None:
        self.root.iconify()

    def mark_dirty(self) -> None:
        """配置变了，稍后统一保存（避免狂写磁盘）。"""
        self._dirty = True
        if self._save_job is not None:
            return
        self._save_job = self.root.after(600, self._flush_save)

    def _flush_save(self) -> None:
        self._save_job = None
        if not self._dirty:
            return
        self._dirty = False
        self.sync_extract_config()
        self.sync_overlay_config()
        # 名单窗的设置也要一起同步 ——
        # 漏掉它的话，在「展示窗口」页改的名单窗参数（列数/翻页秒数等）
        # 只会留在界面上，不会进配置，重启就回到旧值。
        self.sync_board_config()
        self.app.save_config()

    # ---- 主循环 -------------------------------------------------------- #

    def tick(self, 计数: int = 0) -> None:
        """由 main 的定时器定时调用：刷新界面。

        ★ 这里**不再调用** ``bus.pump()`` —— 主循环 ``App._tick`` 已经调过了，
        原来每轮派发两遍事件，纯属白做（pump 是安全但没必要重复调用）。
        参数 ``计数`` 是主循环传进来的轮次号，用来做"每 N 轮才做一次"的降频。
        """
        self._refresh_draw_tab()
        # 倒计时：每 6 轮（约 0.9 秒）刷一次，看着像在走秒
        if 计数 % 6 == 0:
            self._refresh_countdowns()
        # 权重预览涉及遍历整个池子，没必要每轮都算 —— 每 20 轮（默认约 3 秒）刷一次
        if 计数 % 20 == 0:
            self._refresh_watch_preview()
