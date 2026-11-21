# -*- coding: utf-8 -*-
"""抖音随机 UID 抽号器 —— 程序入口。

用法
----
    python main.py            正常启动（控制台 + 透明展示窗）
    python main.py --check    自检：不开界面，跑一遍识别/抽取/HTTP 接口
    python main.py --no-overlay   只开控制台，不显示展示窗
"""

from __future__ import annotations

import os
import sys
import time
import traceback

# 让 `python main.py` 和打包后的 exe 都能 import uidkit
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from uidkit import bus as B                                       # noqa: E402
from uidkit.config import APP_NAME, AppConfig, data_dir           # noqa: E402
from uidkit.state import Session                                  # noqa: E402
from uidkit.votewin import VoteWindow           # noqa: E402


# --------------------------------------------------------------------------- #
# Windows 高 DPI
# --------------------------------------------------------------------------- #

def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class App:
    """把各个部件拼起来，并负责启停数据源。"""

    def __init__(self, root, show_overlay: bool = True) -> None:
        from uidkit.board import BoardWindow
        from uidkit.console import Console
        from uidkit.netserver import NetServer
        from uidkit.overlay import OverlayWindow
        from uidkit.rankwin import RankWindow
        from uidkit.sources import ClipboardSource, DemoSource, FileTailSource

        self.root = root
        self.session = Session(AppConfig.load())
        self.session.load()
        self.cfg = self.session.cfg
        # 第一次运行（没有配置文件）时自动安排窗口位置
        self.first_run = not os.path.exists(AppConfig.path())
        self._save_job = None

        # ★ 必须先算好两个捕获窗口的位置，再创建它们。
        #   窗口一建出来就会按配置里的坐标显示；如果之后再改坐标，
        #   用户就会看到「两个窗口先叠在一起、一两秒后才跳开」。
        #   （旧版本两个窗口的默认坐标都是 +880+80，叠得严严实实。）
        if show_overlay and plan_window_geometry(root, self.cfg):
            self.cfg.save()

        self._overlay_wanted = show_overlay
        self.overlay = OverlayWindow(root, self.session) if show_overlay else None
        self.vote_win = (VoteWindow(root, self.session)
                         if self.cfg.vote.enabled else None)
        self.board = (BoardWindow(root, self.session)
                      if (show_overlay and self.cfg.board.enabled) else None)
        self.rank_win = (RankWindow(root, self.session)
                         if (show_overlay and self.cfg.rank.enabled) else None)

        self.net = NetServer(self.session, self.cfg)
        self.file_source = FileTailSource(self.session,
                                          on_state=self._on_file_state)
        self.clipboard = ClipboardSource(self.session)
        self.demo = DemoSource(self.session)

        self.console = Console(self, root, self.session)

        self._last_autosave = time.time()
        self._closing = False
        #: 上一次看到的显示器配置（用来发现副屏被关掉/拔掉）
        try:
            from uidkit.winutil import display_signature
            self._last_display = display_signature()
        except Exception:
            self._last_display = None

        # 窗口被拖动 / 大小变了 —— 只需存盘，不要重算外观（否则会打架）
        self.session.bus.on(B.CONFIG_DIRTY, self._on_config_dirty)

        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.report_callback_exception = self._on_tk_error

        self._boot_sources()
        root.after(60, self._tick)
        self.session.log(f"{APP_NAME} 已启动。数据目录：{data_dir()}")

    # ------------------------------------------------------------------ #
    # 数据源
    # ------------------------------------------------------------------ #

    def _boot_sources(self) -> None:
        if self.cfg.source.http_enabled:
            self.net.start()
            self.console._refresh_source_tab()
            if self.net.conflict:
                # 端口被占用是「识别成功但参与池不涨」的头号原因，必须弹窗说清楚
                self.root.after(700, self._warn_port_conflict)
        if self.cfg.source.file_enabled and self.cfg.source.file_path:
            self.file_source.start(self.cfg.source.file_path)
        if self.cfg.source.demo_enabled:
            self.demo.start(self.root)

    def _warn_port_conflict(self) -> None:
        from tkinter import messagebox
        want = self.net.requested_port
        got = self.net.port
        info = getattr(self.net, "existing", None) or {}
        if info:
            other = (f"占用它的是另一个抽号器：进程号 {info.get('pid', '?')}，"
                     f"里面已经有 {info.get('pool_size', '?')} 人")
        else:
            other = "占用它的可能是另一个抽号器，也可能是别的程序"

        messagebox.showwarning(
            "端口被占用 —— 弹幕会跑进另一个抽号器",
            f"本程序想用的端口 {want} 已经被占用。\n{other}。\n\n"
            f"本程序已经临时改用端口 {got}。\n\n"
            f"⚠ 关键问题：\n"
            f"抖音直连、以及其它推送工具，默认还是往 {want} 发。\n"
            f"弹幕会全部跑进那个旧实例里 ——\n"
            f"你会看到「日志里识别成功，但这边的参与人数不涨」。\n\n"
            f"正确的做法：\n"
            f"  1. 打开任务管理器，结束所有「UID抽号器」进程\n"
            f"     （结束不掉就点「以管理员身份重新打开」任务管理器）\n"
            f"  2. 重新打开本程序\n\n"
            f"或者：确保推送地址用的是  http://127.0.0.1:{got}\n"
            f"（「弹幕来源」页有复制按钮；点界面上的「直接启动桥接」会自动用对）",
            parent=self.root)
        self.session.log(f"端口 {want} 被占用，已改用 {got}；"
                         f"请检查推送地址是否为 {got}")

    def _on_file_state(self, msg: str) -> None:
        self.session.bus.emit("source_state", {"source": "file", "text": msg})

    def restart_http(self) -> None:
        self.net.stop()
        if self.cfg.source.http_enabled:
            self.net.start()

    def restart_file_source(self) -> None:
        self.file_source.stop()
        if self.cfg.source.file_enabled and self.cfg.source.file_path:
            self.file_source.start(self.cfg.source.file_path)

    def restart_demo(self) -> None:
        self.demo.stop()
        if self.cfg.source.demo_enabled:
            self.demo.start(self.root)

    def base_url(self) -> str:
        if self.net.running:
            return self.net.url("")
        host = self.cfg.source.http_host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.cfg.source.http_port}"

    def push_url(self) -> str:
        url = self.base_url().rstrip("/") + "/push"
        if self.cfg.source.http_token:
            url += f"?token={self.cfg.source.http_token}"
        return url

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #

    def toggle_overlay(self) -> None:
        if self.overlay is None:
            from uidkit.overlay import OverlayWindow
            self.overlay = OverlayWindow(self.root, self.session)
            self.overlay.show()
            self.session.log("展示窗口已创建")
            return
        self.overlay.toggle_visible()

    def toggle_board(self) -> None:
        """显示/隐藏参与名单窗（F5）。

        ★ 同步写 cfg.board.enabled —— 开关状态要能存进配置，
          下次启动才知道该不该自动打开这个窗口。
        """
        if self.board is None:
            from uidkit.board import BoardWindow
            self.cfg.board.enabled = True
            self.board = BoardWindow(self.root, self.session)
            self.board.place_again()
            self.board.show()
            self.session.log("参与名单窗已创建")
            self.save_config()
            return
        self.board.toggle_visible()
        self.cfg.board.enabled = bool(self.board.visible)
        self.save_config()
        self.session.log("参与名单窗已" +
                         ("显示" if self.board.visible else "隐藏"))

    def set_board_visible(self, 显示: bool) -> None:
        """明确指定名单窗显示/隐藏（给界面开关用）。"""
        if self.board is None:
            if not 显示:
                self.cfg.board.enabled = False
                self.save_config()
                return
            from uidkit.board import BoardWindow
            self.cfg.board.enabled = True
            self.board = BoardWindow(self.root, self.session)
            self.board.place_again()
            self.board.show()
            self.session.log("参与名单窗已创建")
            self.save_config()
            return
        if 显示 and not self.board.visible:
            self.board.show()
        elif not 显示 and self.board.visible:
            self.board.hide()
        self.cfg.board.enabled = bool(self.board.visible)
        self.save_config()

    def board_visible(self) -> bool:
        """名单窗现在是不是显示着（没创建过算「不显示」）。"""
        return bool(self.board is not None and self.board.visible)

    def toggle_vote(self) -> None:
        """显示/隐藏投票窗（F6）。"""
        if getattr(self, "vote_win", None) is None:
            from uidkit.votewin import VoteWindow
            self.cfg.vote.enabled = True
            self.vote_win = VoteWindow(self.root, self.session)
            self.vote_win.place_again()
            self.vote_win.show()
            self.session.log("投票窗口已创建")
            return
        self.vote_win.toggle_visible()

    def toggle_rank(self) -> None:
        """显示/隐藏在线榜窗口（F7）。"""
        if getattr(self, "rank_win", None) is None:
            from uidkit.rankwin import RankWindow
            self.cfg.rank.enabled = True
            self.rank_win = RankWindow(self.root, self.session)
            self.rank_win.place_again()
            self.rank_win.show()
            self.save_config()
            self.session.log("在线榜窗口已创建")
            return
        self.rank_win.toggle_visible()
        self.cfg.rank.enabled = bool(self.rank_win.visible)
        self.save_config()

    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.console.sync_extract_config()
            self.console.sync_overlay_config()
            self.console.sync_board_config()
            self.session.save()
        except Exception:
            traceback.print_exc()
        try:
            self.net.stop()
            self.file_source.stop()
            self.demo.stop()
            if self.overlay is not None:
                self.overlay.destroy()
            if self.board is not None:
                self.board.destroy()
            if getattr(self, "vote_win", None) is not None:
                self.vote_win.destroy()
            if getattr(self, "rank_win", None) is not None:
                self.rank_win.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _on_tk_error(self, exc, val, tb) -> None:
        text = "".join(traceback.format_exception(exc, val, tb))
        sys.stderr.write(text)
        try:
            self.session.bus.emit("error", f"界面异常：{val}")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    #: 主循环间隔（毫秒）。原来是 60ms（每秒 16.7 次界面刷新）。
    #: 参与人数、中奖数这些数字**没必要刷那么快**，肉眼 150ms 完全看不出差别，
    #: 但重绘次数降到原来的 40%，CPU 明显下降。
    TICK_MS = 150

    #: 多少轮主循环检查一次显示器配置（150ms × 14 ≈ 2 秒）。
    #: 显示器变化不是高频事件，2 秒的延迟完全够用，又不会白白占 CPU。
    DISPLAY_CHECK_EVERY = 14

    def _tick(self) -> None:
        if self._closing:
            return
        self._tick_no = getattr(self, "_tick_no", 0) + 1
        try:
            # 事件派发只在这里做一次（原来 Console.tick() 里又做了一遍，纯属重复）
            self.session.bus.pump()
            # 投票倒计时 / 参与池定时清空（都在主线程里做，不另起线程）
            self.session.tick_timers()
            self.console.tick(self._tick_no)
            self.clipboard.poll(self.root)

            if self._tick_no % self.DISPLAY_CHECK_EVERY == 0:
                self._check_capture_windows()

            # 名单窗开关和窗口实际状态对齐（F5 切过之后勾选框要跟着变）
            if self._tick_no % 20 == 0:
                try:
                    self.console.sync_board_toggle()
                except Exception:
                    pass

            now = time.time()
            if now - self._last_autosave > max(5, self.cfg.auto_save_ms / 1000.0):
                self._last_autosave = now
                try:
                    self.session.pool.save()
                except Exception:
                    pass
                # ★ 观看时长记录也要定期存。
                #   原来只在正常退出时存，程序被强杀/崩溃就白看了 ——
                #   而这个记录恰恰是「清空参与池也要保留」的东西。
                try:
                    self.session._save_first_seen()
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()
        finally:
            if not self._closing:
                self.root.after(self.TICK_MS, self._tick)

    # ------------------------------------------------------------------ #
    # 两个捕获窗口的位置自愈
    # ------------------------------------------------------------------ #

    def _check_capture_windows(self) -> None:
        """定期检查两个捕获窗口是不是还摆在合理位置，不对就自动摆回来。

        ★ 修的 bug：把两个窗口拖到副屏、然后**把副屏关掉**，
          Windows 会把它们挪回主屏，而且经常**堆在同一个坐标上**
          （用户看到的就是「两个窗口黏在一起」）。
          程序原来只在启动时安排过一次位置，之后不管，于是一直叠着。

        触发条件有三个，任意一个成立就重新安排：
          1. 显示器配置变了（副屏被关/拔、分辨率改了）——
             这必须用 Win32 的虚拟桌面来判断，tkinter 的
             winfo_screenwidth() 只报主屏，副屏关掉时它察觉不到；
          2. 某个窗口整个跑到「已经不存在的显示器」上去了；
          3. 两个窗口重叠在一起了。
        """
        # 一个捕获窗口都没开就不管
        if self.overlay is None and self.board is None:
            return

        from uidkit import winutil as W

        sig = W.display_signature()
        显示器变了 = bool(sig) and self._last_display is not None \
            and sig != self._last_display
        self._last_display = sig

        # ★ 注意：显示器变化只是「值得重新检查一遍」的理由，
        #   不等于「必须动窗口」。
        #   比如用户把副屏**重新打开**，配置也变了，但窗口本来就摆得好好的 ——
        #   这时去强制重摆反而会把他自己摆好的位置冲掉。
        #   所以下面统一按「窗口实际状态」来决定动不动。
        原因 = ""
        位置 = []
        for 名, 窗 in (("展示窗", self.overlay), ("名单窗", self.board)):
            if 窗 is None:
                continue
            try:
                hwnd = W.root_hwnd(窗.win)
                l, t, r, b = W.window_rect(hwnd)
                位置.append((名, l, t, r - l, b - t))
            except Exception:
                continue

        for 名, l, t, w, h in 位置:
            if w > 0 and h > 0 and not W.on_screen(l, t, w, h):
                原因 = f"{名}跑到屏幕外了"
                break
        if not 原因 and len(位置) == 2:
            (_, l1, t1, w1, h1), (_, l2, t2, w2, h2) = 位置
            # 左上角完全相同 = 叠在一起（宽高不同也算）
            if (l1, t1) == (l2, t2):
                原因 = "两个捕获窗口叠在一起了"
        if not 原因 and len(位置) < 2:
            return                      # 只开了一个窗口，没什么好比的

        if not 原因:
            return
        if 显示器变了:
            原因 += "（显示器配置刚变过）"
        self.relayout_capture_windows(原因)

    def relayout_capture_windows(self, 原因: str = "") -> None:
        """把两个捕获窗口重新摆到互不重叠、且在屏幕内的位置。"""
        from uidkit.layout import plan
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            if plan(self.cfg, sw, sh, force=True):
                self.save_config()
            for 窗 in (self.overlay, self.board):
                if 窗 is not None:
                    窗.place_again()
        except Exception:
            traceback.print_exc()
            return
        if 原因:
            self.session.log(f"已重新安排两个捕获窗口（原因：{原因}）")
            self.session.bus.emit(
                B.STATUS, f"检测到{原因}，已自动把两个捕获窗口摆好")

    def save_config(self) -> None:
        try:
            self.cfg.save()
        except Exception as exc:
            sys.stderr.write(f"保存配置失败：{exc}\n")

    def _on_config_dirty(self, _payload=None) -> None:
        """配置有小改动（比如拖动了窗口）—— 攒一下再存，避免频繁写盘。"""
        if self._closing:
            return
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        try:
            self._save_job = self.root.after(800, self._flush_config)
        except Exception:
            self._save_job = None

    def _flush_config(self) -> None:
        self._save_job = None
        self.save_config()


# --------------------------------------------------------------------------- #
# 自检（不需要图形界面）
# --------------------------------------------------------------------------- #

def run_check() -> int:
    """跑一遍核心链路，确认程序没坏。"""
    from uidkit.draw import draw, verify
    from uidkit.normalize import ExtractConfig, Mode, extract
    from uidkit.netserver import NetServer

    # 打包成 exe 后控制台默认是 GBK，直接 print emoji 会抛 UnicodeEncodeError。
    # 这里把输出强制成 UTF-8 并且遇到编不出的字符就替换，保证自检不会被编码问题搞挂。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("=" * 64)
    print("抖音UID抽号器 · 自检")
    print("=" * 64)

    ok = True

    # 1) 识别
    print("\n[1] UID 识别（模拟真实弹幕）")
    cfg = ExtractConfig(mode=Mode.SMART, min_len=6, max_len=15, letter_fix=True)
    samples = [
        "100 591 822", "100.591.822", "１００ ５９１ ８２２",
        "100😀591😀822", "100\u200b591\u200b822",
        "UID:100 591 823", "主播抽我 100 591 824",
        "1 0 0 5 9 1 8 2 5", "l0059l826",
        "666", "哈哈哈", "123456",
    ]
    for s in samples:
        c = extract(s, cfg)
        got = c[0].uid if c else "—"
        print(f"    {s!r:34} → {got}")

    cases = {"100 591 822": "100591822", "100.591.822": "100591822",
             "１００ ５９１ ８２２": "100591822", "100😀591😀822": "100591822",
             "UID:100 591 823": "100591823", "l0059l826": "100591826"}
    for src, want in cases.items():
        c = extract(src, cfg)
        if not c or c[0].uid != want:
            print(f"    !! 失败：{src!r} 应该是 {want}")
            ok = False
    for src in ("666", "哈哈哈", "123456"):
        if extract(src, cfg):
            print(f"    !! 失败：{src!r} 不该被识别")
            ok = False

    # 2) 会话 + 抽取
    print("\n[2] 参与池与抽取")
    sess = Session(AppConfig())
    sess.cfg.extract = cfg
    sess.cfg.source.http_enabled = False
    import random as _r
    rng = _r.Random(7)
    for i in range(200):
        # ★ 用**落在程序允许范围内**的 UID：9 位、首位 1/2/3
        #   （默认只收 100000000~399999999）。随便生成 9 位数的话，
        #   一多半会被范围过滤挡掉，自检就会误报「识别率太低」。
        n = rng.choice("123") + "".join(
            rng.choice("0123456789") for _ in range(8))
        # 注意：不要再在前面拼 "100" —— 那样会变成 12 位，
        # 被 9 位的限制直接挡掉（这个坑我自己踩过一次）。
        text = (f"{n[:3]} {n[3:6]} {n[6:]}" if i % 3 == 0
                else (f"UID:{n}" if i % 3 == 1 else n))
        sess.feed(text, nick=f"观众{i}", source="自检")
    print(f"    收到 {sess.feed_total} 条，识别出 {len(sess.pool)} 人")
    if len(sess.pool) < 150:
        print("    !! 失败：识别率太低")
        ok = False

    # 2b) 人为把「首次出现时间」铺开，模拟真实直播里有人早就来了、有人刚来。
    #     不这么做的话 200 条弹幕是同一瞬间灌进去的，所有人的观看时长都是 0，
    #     加权逻辑就完全没被验证到（区间会显示 1.00~1.00）。
    _now = time.time()
    _all = sess.pool.all_entries()
    for _i, _e in enumerate(_all):
        # 均匀分布在 0 ~ 60 分钟之前
        _e.first_ts = _now - (_i / max(1, len(_all) - 1)) * 3600.0

    res = sess.do_draw(3)
    if res is None:
        print("    !! 失败：抽不出来")
        ok = False
    else:
        print(f"    第 {res.round_no} 轮抽中：{res.winner_uids}")
        print(f"    凭证：{res.proof()}")
        if len(res.winner_uids) != 3 or len(set(res.winner_uids)) != 3:
            print("    !! 失败：一次抽 3 个却出现重复")
            ok = False

        # 3) 公平性复算
        print("\n[3] 公平性复算")
        entries = sess.pool.eligible(exclude_winners=False)
        # ★ 加权模式下复算必须带上「当时的权重参数」和「抽奖那一刻的时间」——
        #   因为权重取决于每个人在那一刻已经看了多久，不给这两样就算不出同样的权重。
        good = verify(entries, 3, res.seed, res.fingerprint, res.winner_uids,
                      options=res.options, now=res.ts)
        print(f"    用公示的种子+指纹复算：{'一致 [OK]' if good else '不一致 [FAIL]'}")
        ok = ok and good

        # 3b) 反向验证：换一个时间点就应该算不出同样的结果。
        #     这证明「权重确实进了指纹」，不是摆设。
        wrong = verify(entries, 3, res.seed, res.fingerprint, res.winner_uids,
                       options=res.options, now=res.ts + 7200)
        print(f"    换个时间点复算（应当不一致）："
              f"{'不一致 [OK]' if not wrong else '居然一致 [FAIL]'}")
        ok = ok and (not wrong)
        print(f"    本轮权重设置：{res.options.describe()}")
        if res.weighted:
            lo, hi = res.weight_range()
            print(f"    参与 {res.pool_size} 人，权重区间 {lo:.2f} ~ {hi:.2f}")

    # 4) HTTP 服务
    print("\n[4] HTTP 弹幕接口")
    sess2 = Session(AppConfig())
    sess2.cfg.source.http_enabled = True
    sess2.cfg.source.http_host = "127.0.0.1"
    sess2.cfg.source.http_port = 0
    net = NetServer(sess2, sess2.cfg)
    if not net.start():
        print(f"    !! 失败：{net.error}")
        ok = False
    else:
        import json
        import urllib.parse
        import urllib.request
        base = net.url("")
        print(f"    服务地址：{base}")

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read().decode("utf-8")

        def post(path, body=""):
            req = urllib.request.Request(base + path, data=body.encode(),
                                         method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")

        try:
            st, body = get("/push?text=" + urllib.parse.quote("100 591 822")
                           + "&nick=" + urllib.parse.quote("小明"))
            j = json.loads(body)
            print(f"    GET /push        → {j}")
            if j.get("uid") != "100591822":
                print("    !! 失败：GET 推送没识别对")
                ok = False

            st, body = post("/push", json.dumps({"text": "100.591.823",
                                                 "nick": "小红"}))
            j = json.loads(body)
            print(f"    POST /push       → {j}")
            if j.get("uid") != "100591823":
                print("    !! 失败：POST 推送没识别对")
                ok = False

            st, body = post("/push/batch", json.dumps(
                ["100 591 824", "100 591 825", "哈哈哈"]))
            print(f"    POST /push/batch → {json.loads(body)}")

            st, body = get("/status")
            snap = json.loads(body)
            print(f"    GET /status      → 参与 {snap['pool_size']} 人，"
                  f"收到 {snap['danmaku_total']} 条")
            if snap["pool_size"] != 4:
                print(f"    !! 失败：应该是 4 个人，实际 {snap['pool_size']}")
                ok = False

            st, body = post("/draw?count=1")
            print(f"    POST /draw       → {json.loads(body)}")

            st, body = get("/overlay")
            print(f"    GET /overlay     → HTTP {st}，{len(body)} 字节 HTML")
            if "EventSource" not in body:
                print("    !! 失败：叠加页内容不对")
                ok = False

            st, body = get("/")
            print(f"    GET /            → HTTP {st}，{len(body)} 字节 HTML")
        except Exception as exc:
            print(f"    !! 失败：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            ok = False
        finally:
            net.stop()

    # 5) 打包依赖检查
    print("\n[5] 环境")
    print(f"    Python {sys.version.split()[0]}   {sys.executable}")
    print(f"    运行方式：{'打包 exe' if getattr(sys, 'frozen', False) else '源码'}")
    try:
        import tkinter
        print(f"    tkinter OK (Tk {tkinter.TkVersion})")
    except Exception as exc:
        print(f"    !! tkinter 不可用：{exc}")
        ok = False

    # 桥接（抖音直连）依赖检查 —— 这一项不影响抽号器本体
    from uidkit.console import bridge_dir, find_python_for_scripts
    bdir = bridge_dir()
    script = os.path.join(bdir, "douyin_bridge.py")
    print(f"    抖音直连桥接：{bdir}")
    if os.path.isfile(script):
        print("      - 桥接脚本：找到")
    else:
        print("      - 桥接脚本：没找到（把 tools 文件夹放到程序同一层即可）")
    py = find_python_for_scripts()
    if py:
        print(f"      - 可用 Python：{py}")
    else:
        print("      - 可用 Python：没找到 —— 抖音直连需要单独装一个 Python；"
              "其它接入方式不受影响")

    print("\n" + "=" * 64)
    print("自检结果：" + ("全部通过 [OK]" if ok else "有问题 [FAIL]"))
    print("=" * 64)
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def heal_runasadmin_flag() -> bool:
    """去掉 Windows 给我们加的「以管理员身份运行」兼容性标记。

    这是**程序兼容性助手**干的：一个 exe 反复启动失败若干次之后，Windows 会
    悄悄在注册表里给它打上 ``RUNASADMIN``。后果是以后每次双击都会弹 UAC，
    不点「是」就打不开 —— 用户完全不知道为什么。

    这个标记在 ``HKCU`` 下，删它不需要管理员权限，所以程序可以自己修。
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        import winreg
        exe = os.path.abspath(sys.executable)
        path = (r"Software\Microsoft\Windows NT\CurrentVersion"
                r"\AppCompatFlags\Layers")
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                 winreg.KEY_READ | winreg.KEY_WRITE)
        except OSError:
            return False
        try:
            val, _ = winreg.QueryValueEx(key, exe)
        except OSError:
            winreg.CloseKey(key)
            return False
        healed = bool(val) and "RUNASADMIN" in str(val).upper()
        if healed:
            try:
                winreg.DeleteValue(key, exe)
            except OSError:
                healed = False
        winreg.CloseKey(key)
        return healed
    except Exception:
        return False


def app_icon_path():
    """找 assets/app.ico 的真实路径（源码运行 / 打包成 exe 都要能找到）。

    ★ 打包成 onefile 之后，assets 会被解压到 sys._MEIPASS 指向的临时目录，
      不能直接按源码目录拼路径，否则运行时图标就是空的。
    """
    import sys as _sys
    候选 = []
    基准 = getattr(_sys, "_MEIPASS", None)
    if 基准:
        候选.append(os.path.join(基准, "assets", "app.ico"))
    # 源码运行、或安装版（exe 旁边就是 assets）
    候选.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "app.ico"))
    候选.append(os.path.join(os.getcwd(), "assets", "app.ico"))
    for p in 候选:
        if os.path.exists(p):
            return p
    return None


def set_window_icon(root) -> None:
    """给窗口设图标：标题栏、任务栏、Alt+Tab 都会跟着变。

    ★ 为什么必须单独设：
      exe 里嵌的图标只决定「资源管理器里这个文件长什么样」。
      运行中的窗口图标是另一套东西（WM_SETICON），
      不显式调 iconbitmap() 的话，标题栏会一直是 tkinter 默认那个羽毛图标。

      default=True 会把图标设成**默认值**，后面新建的 Toplevel 也自动继承，
      所以展示窗、参与名单窗不用再单独设一遍。
    """
    路径 = app_icon_path()
    if not 路径:
        return
    try:
        root.iconbitmap(default=路径)
    except Exception:
        try:
            root.iconbitmap(路径)
        except Exception:
            pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--check" in argv or "--selftest" in argv:
        return run_check()

    show_overlay = "--no-overlay" not in argv

    healed = heal_runasadmin_flag()

    enable_dpi_awareness()

    try:
        import tkinter as tk
    except Exception as exc:
        print(f"缺少 tkinter，无法启动界面：{exc}")
        return 2

    root = tk.Tk()
    # 图标要在窗口显示之前设好，否则任务栏会先闪一下默认图标
    set_window_icon(root)
    root.withdraw()

    try:
        app = App(root, show_overlay=show_overlay)
    except Exception:
        traceback.print_exc()
        try:
            from tkinter import messagebox
            messagebox.showerror("启动失败",
                                 "程序启动出错：\n\n" + traceback.format_exc())
        except Exception:
            pass
        return 3

    # 控制台放左上角，展示窗按配置摆在右边
    root.update_idletasks()
    try:
        w, h = 1060, 760
        root.geometry(f"{w}x{h}+40+40")
    except Exception:
        pass
    root.deiconify()
    root.lift()
    root.focus_force()
    root.update_idletasks()

    # 窗口的几何必须等主窗口真正映射之后再设一次，
    # 否则（尤其是无边框窗口）会被窗口管理器改回去。
    # 注意：位置本身已经在 App.__init__ 里、创建窗口之前就算好了，
    # 这里只是「再摆一次」，不会再发生位置跳变。
    if app.overlay is not None:
        app.overlay.place_again()
        app.overlay.show()
        app.overlay.relayout()
    if app.board is not None:
        app.board.place_again()
        app.board.show()
        app.board.relayout()
    if getattr(app, "rank_win", None) is not None:
        app.rank_win.place_again()
        app.rank_win.show()
        app.rank_win.relayout()

    if healed:
        app.session.log("已自动清除 Windows 给本程序加的「以管理员身份运行」标记"
                        "（这个标记会让以后每次启动都弹 UAC）")

    root.mainloop()
    return 0


def _auto_place_windows(root, app) -> None:
    """（旧接口，保留兼容）按当前屏幕把两个捕获窗口重新摆一遍。"""
    plan_window_geometry(root, app.cfg, 强制=True)


def plan_window_geometry(root, cfg, 强制: bool = False) -> bool:
    """安排展示窗 / 参与名单窗的位置，返回「配置有没有被改动」。

    ★ 必须在**创建窗口之前**调用。
      窗口一旦建出来就按配置里的坐标显示，之后再改就会出现
      「两个窗口先叠在一起、一两秒后才跳开」的现象。

    真正的算法在 ``uidkit/layout.py``（界面上的「重置位置」按钮也要用，
    放这里会循环导入）。
    """
    from uidkit.layout import plan
    try:
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
    except Exception:
        return False
    return plan(cfg, sw, sh, force=强制)


if __name__ == "__main__":
    sys.exit(main())
