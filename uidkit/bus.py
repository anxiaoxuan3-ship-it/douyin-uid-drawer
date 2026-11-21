# -*- coding: utf-8 -*-
"""线程安全事件总线。

后台线程（HTTP 服务、文件监听、剪贴板轮询）只负责 ``emit``；
tkinter 主线程用 ``after()`` 定时 ``pump``，在主线程序里派发给订阅者。
这样所有界面更新都发生在主线程，不会出现 tkinter 跨线程崩溃。
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Tuple

# 事件主题
DANMAKU = "danmaku"            # 收到一条弹幕（含识别结果）
POOL_CHANGED = "pool_changed"  # 参与池有变化
DRAW_START = "draw_start"      # 开始抽取（滚动动画开始）
DRAW_DONE = "draw_done"        # 抽取结束（已定格）
STATUS = "status"              # 状态栏文字
LOG = "log"                    # 日志行
ERROR = "error"                # 出错
CONFIG_CHANGED = "config_changed"   # 配置改了，且需要重新应用到窗口上
CONFIG_DIRTY = "config_dirty"

#: 投票数据变了（新票 / 改票 / 清空 / 开新一轮）
VOTE_CHANGED = "vote_changed"       # 配置改了，只需存盘（不要重算外观）
SOURCE_STATE = "source_state"  # 某个数据源连上/断开

#: 在线/贡献榜快照变了（抖音直连推送）
RANK_CHANGED = "rank_changed"


class EventBus:
    def __init__(self, backlog: int = 4000) -> None:
        self._q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._subs: Dict[str, List[Callable[[Any], None]]] = {}
        self._lock = threading.RLock()
        self._history: Deque[Tuple[str, Any]] = deque(maxlen=backlog)

    # ---- 订阅 ---------------------------------------------------------- #

    def on(self, topic: str, fn: Callable[[Any], None]) -> Callable[[Any], None]:
        with self._lock:
            self._subs.setdefault(topic, []).append(fn)
        return fn

    def off(self, topic: str, fn: Callable[[Any], None]) -> None:
        with self._lock:
            if fn in self._subs.get(topic, []):
                self._subs[topic].remove(fn)

    # ---- 发送（任意线程） ---------------------------------------------- #

    def emit(self, topic: str, payload: Any = None) -> None:
        self._q.put((topic, payload))

    # ---- 派发（仅主线程调用） ------------------------------------------ #

    def pump(self, limit: int = 200) -> int:
        """把队列里的事件派发给订阅者。返回处理条数。"""
        n = 0
        while n < limit:
            try:
                topic, payload = self._q.get_nowait()
            except queue.Empty:
                break
            n += 1
            self._history.append((topic, payload))
            with self._lock:
                subs = list(self._subs.get(topic, ()))
            for fn in subs:
                try:
                    fn(payload)
                except Exception as exc:  # 单个订阅者出错不能拖垮总线
                    import traceback
                    traceback.print_exc()
                    del exc
        return n

    def drain(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    @property
    def history(self) -> List[Tuple[str, Any]]:
        return list(self._history)
