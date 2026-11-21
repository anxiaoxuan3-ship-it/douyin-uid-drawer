# -*- coding: utf-8 -*-
"""弹幕来源：文件监听 / 剪贴板监听 / 模拟弹幕。

（HTTP 推送在 netserver.py 里。）

**文件监听**是最容易和第三方工具对接的方式：任何"抖音弹幕助手"只要能把弹幕
写进一个 txt，本程序就能实时读进来。支持这几种行格式：

    纯文本                  100 591 822
    昵称 + Tab + 内容        小明<TAB>100 591 822
    JSON Lines              {"text":"100 591 822","nick":"小明"}
    聊天记录                [12:00:01] 小明: 100 591 822
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from typing import Callable, Optional, Tuple

from .state import Session

# --------------------------------------------------------------------------- #
# 行解析
# --------------------------------------------------------------------------- #

_CHAT_RE = re.compile(
    r"^\s*(?:\[(?P<t>[\d:.\-/\s]+)\]\s*)?"
    r"(?P<nick>[^:：]{1,24}?)\s*[:：]\s*(?P<text>.+?)\s*$"
)


def parse_line(line: str, fmt: str = "auto") -> Tuple[str, str]:
    """把文件里的一行解析成 (弹幕文本, 昵称)。"""
    line = line.rstrip("\r\n")
    if not line.strip():
        return "", ""

    def from_json(s: str) -> Optional[Tuple[str, str]]:
        try:
            obj = json.loads(s)
        except Exception:
            return None
        if isinstance(obj, dict):
            text = (obj.get("text") or obj.get("content") or obj.get("msg")
                    or obj.get("danmaku") or obj.get("message") or "")
            nick = (obj.get("nick") or obj.get("nickname") or obj.get("user")
                    or obj.get("name") or "")
            return str(text), str(nick)
        if isinstance(obj, str):
            return obj, ""
        return None

    if fmt == "jsonl":
        got = from_json(line)
        return got if got else ("", "")

    if fmt == "nick_tab_text":
        parts = line.split("\t")
        if len(parts) >= 2:
            return parts[-1].strip(), parts[0].strip()
        return line.strip(), ""

    if fmt == "chat":
        m = _CHAT_RE.match(line)
        if m:
            return m.group("text").strip(), m.group("nick").strip()
        return line.strip(), ""

    if fmt == "text":
        return line.strip(), ""

    # ---- auto ----
    stripped = line.strip()
    if stripped[:1] in "{[":
        got = from_json(stripped)
        if got:
            return got
    if "\t" in line:
        parts = line.split("\t")
        if len(parts) >= 2:
            return parts[-1].strip(), parts[0].strip()
    return stripped, ""


# --------------------------------------------------------------------------- #
# 文件监听
# --------------------------------------------------------------------------- #

class FileTailSource:
    """监听一个文本文件，把新增的行当成弹幕。"""

    def __init__(self, session: Session, on_state: Optional[Callable[[str], None]] = None):
        self.session = session
        self.on_state = on_state
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pos = 0
        self._inode = None
        self._path = ""
        self._warned = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, path: str) -> bool:
        self.stop()
        path = (path or "").strip().strip('"')
        if not path:
            return False
        self._path = os.path.abspath(path)
        self._pos = 0
        self._inode = None
        self._warned = False
        self._stop.clear()

        # 已经存在的文件：跳过老内容，只读新追加的
        if os.path.exists(self._path):
            try:
                self._pos = os.path.getsize(self._path)
                self._inode = os.stat(self._path).st_ino
            except OSError:
                self._pos = 0
        else:
            self.session.log(f"文件监听：{self._path} 还不存在，等它出现…")

        self._thread = threading.Thread(target=self._loop, name="file-tail",
                                        daemon=True)
        self._thread.start()
        self._state(f"监听中：{os.path.basename(self._path)}")
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _state(self, msg: str) -> None:
        if self.on_state:
            try:
                self.on_state(msg)
            except Exception:
                pass
        self.session.bus.emit("source_state", {"source": "file", "text": msg})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as exc:
                if not self._warned:
                    self.session.log(f"文件监听出错：{exc}")
                    self._warned = True
            self._stop.wait(0.4)

    def _poll(self) -> None:
        path = self._path
        if not os.path.exists(path):
            if self._inode is not None:
                self._state("文件被删除了，等它回来…")
                self._inode = None
                self._pos = 0
            return

        try:
            st = os.stat(path)
        except OSError:
            return

        # 文件被替换 / 被截断 → 从头读
        if self._inode is not None and st.st_ino != self._inode:
            self._pos = 0
            self._state("检测到文件被替换，从头开始读")
        if st.st_size < self._pos:
            self._pos = 0
            self._state("检测到文件被清空，从头开始读")
        self._inode = st.st_ino
        if st.st_size == self._pos:
            return

        fmt = self.session.cfg.source.file_format
        enc = self.session.cfg.source.file_encoding or "utf-8"

        with open(path, "r", encoding=enc, errors="replace") as f:
            f.seek(self._pos)
            data = f.read()
            self._pos = f.tell()

        for line in data.splitlines():
            text, nick = parse_line(line, fmt)
            if text:
                self.session.feed(text, nick=nick, source="file")


# --------------------------------------------------------------------------- #
# 剪贴板监听（必须在主线程轮询 —— Tk 的剪贴板只能主线程访问）
# --------------------------------------------------------------------------- #

class ClipboardSource:
    """轮询系统剪贴板，内容一变就当成弹幕批次收进来。

    适用场景：主播用别的弹幕工具/手机，把弹幕复制过来。
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self._last = ""
        self._last_ts = 0.0
        self._recent = []

    def poll(self, root) -> None:
        """由主循环定时调用。root 是 tkinter 的任意 widget。"""
        if not self.session.cfg.source.clipboard_enabled:
            return
        interval = max(120, int(self.session.cfg.source.clipboard_interval_ms)) / 1000.0
        now = time.time()
        if now - self._last_ts < interval:
            return
        self._last_ts = now

        try:
            data = root.clipboard_get()
        except Exception:
            return
        if not isinstance(data, str) or not data.strip():
            return
        if data == self._last:
            return
        self._last = data

        # 防止把刚复制过的同一批内容反复收
        key = hash(data)
        if key in self._recent:
            return
        self._recent.append(key)
        if len(self._recent) > 60:
            self._recent.pop(0)

        lines = [ln for ln in data.splitlines() if ln.strip()]
        if not lines:
            return
        n = 0
        for ln in lines[:500]:
            text, nick = parse_line(ln, "auto")
            if text:
                rec = self.session.feed(text, nick=nick, source="剪贴板")
                if rec and rec.uid:
                    n += 1
        if n:
            self.session.log(f"剪贴板收到 {n} 个 UID")


# --------------------------------------------------------------------------- #
# 模拟弹幕（彩排用，不需要真直播）
# --------------------------------------------------------------------------- #

_NICKS = ["小明", "阿强", "夜风", "糖豆", "老张", "小仙女", "打工人", "路过的",
          "爱吃辣", "不吃香菜", "一只猫", "蓝色的海", "摸鱼中", "886", "小可爱"]
_CHAT = ["主播好帅", "来了来了", "哈哈哈", "666", "关注了",
         "抽我抽我", "第一次来", "这个怎么玩", "支持一下", "厉害"]
_SEPS = [" ", "  ", "　", ".", "-", "*", "_", ",", "/", "|", "·", "😀", "✨", ""]


class DemoSource:
    """生成一堆仿真的弹幕（含各种规避写法）来彩排。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._job = None
        self._rng = random.Random()

    def start(self, root) -> None:
        self.stop()
        self._schedule(root)

    def stop(self) -> None:
        if self._job is not None:
            try:
                self._job.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _schedule(self, root) -> None:
        if not self.session.cfg.source.demo_enabled:
            self._job = None
            return
        delay = max(120, int(self.session.cfg.source.demo_interval_ms))
        self._job = root.after(delay, lambda: self._fire(root))

    def _fire(self, root) -> None:
        self.session.feed(self.make_danmaku(), nick=self._rng.choice(_NICKS),
                          source="模拟")
        self._schedule(root)

    def make_danmaku(self) -> str:
        """随机造一条弹幕，写法模仿真实观众。"""
        if self._rng.random() < 0.42:
            return self._rng.choice(_CHAT)

        uid = "".join(self._rng.choice("0123456789")
                      for _ in range(self._rng.randint(9, 10)))
        style = self._rng.randint(0, 6)
        if style == 0:
            return uid
        if style == 1:  # 空格分组（最常见的规避写法）
            return " ".join(uid[i:i + 3] for i in range(0, len(uid), 3))
        if style == 2:  # 随机分隔符
            parts = [uid[i:i + 3] for i in range(0, len(uid), 3)]
            return self._rng.choice(_SEPS).join(parts)
        if style == 3:  # 逐位拆开
            return " ".join(uid)
        if style == 4:  # 夹在文本里
            return self._rng.choice(["抽我 ", "我的 ", "UID:", "主播看我 ",
                                     "关注了 "]) + " ".join(
                uid[i:i + 3] for i in range(0, len(uid), 3))
        if style == 5:  # 带零宽字符
            return "\u200b".join(uid[i:i + 3] for i in range(0, len(uid), 3))
        return self._rng.choice(["我 " + uid + " 求抽", uid + " 号", "@" + uid])
