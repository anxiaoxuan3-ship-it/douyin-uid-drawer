# -*- coding: utf-8 -*-
"""参与池：收录 UID、去重、黑名单、中奖记录、持久化。"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Set

from .config import data_path


@dataclass
class Entry:
    """一个参与者。"""

    uid: str
    nick: str = ""
    raw: str = ""
    """第一次识别到该 UID 时的原始弹幕，方便主播核对。"""

    count: int = 1
    """该 UID 在公屏上出现的次数。"""

    first_ts: float = 0.0
    last_ts: float = 0.0
    source: str = ""

    #: 主播手动调的中奖倍率。1.0（或 0）表示不调，2.0 表示中奖率翻倍，
    #: 0.5 表示减半。乘在观看时长/发言次数算出来的权重上，
    #: 用倍率而不是直接指定百分比 —— 别人权重变了它还能保持相对关系。
    weight_mult: float = 0.0

    #: 连续多少轮抽奖没中。中一次就清零。保底机制靠它判断。
    miss_rounds: int = 0

    #: 跨场次补偿倍率。
    #: 上一场参与了但没中的人，这一场自动带上这个倍率（默认 2.0）。
    #: 和 weight_mult 是两条独立的通道：
    #:   weight_mult 是主播手动调的，carry_mult 是系统按规则给的。
    #: 两者都 > 0 时会**相乘**。
    carry_mult: float = 0.0

    #: 这个补偿倍率是从哪一场带过来的（时间戳，仅用于界面显示）。
    carry_from: float = 0.0

    @property
    def display(self) -> str:
        if self.nick:
            return f"{self.nick}({self.uid})"
        return self.uid


@dataclass
class Winner:
    """一次中奖记录。"""

    uid: str
    nick: str = ""
    ts: float = 0.0
    round_no: int = 0
    seed: str = ""

    @property
    def display(self) -> str:
        if self.nick:
            return f"{self.nick}({self.uid})"
        return self.uid


class Pool:
    """参与池。所有公开方法都加了锁，可以从任意线程调用。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[str, Entry] = {}
        self._blacklist: Set[str] = set()
        self._winners: List[Winner] = []
        self._round = 0

        # 统计
        self.total_danmaku = 0
        self.total_matched = 0
        self.total_duplicate = 0
        self.total_rejected = 0
        self.started_ts = time.time()

    # ------------------------------------------------------------------ #
    # 基础访问
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, uid: str) -> bool:
        with self._lock:
            return uid in self._entries

    def all_entries(self) -> List[Entry]:
        with self._lock:
            return list(self._entries.values())

    def recent(self, n: int = 50) -> List[Entry]:
        with self._lock:
            items = sorted(self._entries.values(), key=lambda e: -e.last_ts)
            return items[:n]

    @property
    def round_no(self) -> int:
        with self._lock:
            return self._round

    @property
    def winners(self) -> List[Winner]:
        with self._lock:
            return list(self._winners)

    @property
    def blacklist(self) -> Set[str]:
        with self._lock:
            return set(self._blacklist)

    # ------------------------------------------------------------------ #
    # 收录
    # ------------------------------------------------------------------ #

    def get(self, uid: str) -> Optional[Entry]:
        """按 UID 取一个参与者；没有返回 None。"""
        with self._lock:
            return self._entries.get(uid.strip())

    def add(self, uid: str, nick: str = "", raw: str = "",
            source: str = "", ts: Optional[float] = None) -> str:
        """收录一个 UID。

        返回 ``"new"`` / ``"dup"`` / ``"blocked"`` / ``"invalid"``。
        """
        uid = uid.strip()
        if not uid:
            return "invalid"
        ts = ts if ts is not None else time.time()

        with self._lock:
            if uid in self._blacklist:
                return "blocked"
            e = self._entries.get(uid)
            if e is None:
                self._entries[uid] = Entry(
                    uid=uid, nick=nick or "", raw=raw or "",
                    count=1, first_ts=ts, last_ts=ts, source=source,
                )
                self.total_matched += 1
                return "new"
            e.count += 1
            e.last_ts = ts
            if nick and not e.nick:
                e.nick = nick
            if raw and not e.raw:
                e.raw = raw
            self.total_duplicate += 1
            return "dup"

    def note_danmaku(self) -> None:
        with self._lock:
            self.total_danmaku += 1

    def note_rejected(self, n: int = 1) -> None:
        with self._lock:
            self.total_rejected += n

    def remove(self, uid: str) -> bool:
        with self._lock:
            return self._entries.pop(uid, None) is not None

    def add_blacklist(self, uid: str) -> None:
        with self._lock:
            self._blacklist.add(uid)
            self._entries.pop(uid, None)

    def remove_blacklist(self, uid: str) -> None:
        with self._lock:
            self._blacklist.discard(uid)

    def clear_entries(self) -> None:
        """清空参与池（中奖记录保留）。"""
        with self._lock:
            self._entries.clear()
            self.total_danmaku = 0
            self.total_matched = 0
            self.total_duplicate = 0
            self.total_rejected = 0

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()
            self._winners.clear()
            self._blacklist.clear()
            self._round = 0
            self.total_danmaku = 0
            self.total_matched = 0
            self.total_duplicate = 0
            self.total_rejected = 0
            self.started_ts = time.time()

    # ------------------------------------------------------------------ #
    # 抽取支持
    # ------------------------------------------------------------------ #

    def eligible(self, exclude_winners: bool = True) -> List[Entry]:
        """当前有资格参与抽取的人。"""
        with self._lock:
            done = {w.uid for w in self._winners} if exclude_winners else set()
            return [e for e in self._entries.values() if e.uid not in done]

    def commit_winners(self, ws: Sequence[Winner]) -> int:
        """把一轮中奖结果写进记录，返回新的轮次号。"""
        with self._lock:
            self._round += 1
            for w in ws:
                w.round_no = self._round
                self._winners.append(w)
            return self._round

    def undraw(self, n: int = 1) -> List[Winner]:
        """撤销最近 n 条中奖记录（抽错了/主播手滑）。"""
        with self._lock:
            popped = []
            for _ in range(min(n, len(self._winners))):
                popped.append(self._winners.pop())
            return popped

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #

    def stats(self) -> Dict[str, float]:
        with self._lock:
            counts = [e.count for e in self._entries.values()]
            return {
                "participants": len(self._entries),
                "danmaku_total": self.total_danmaku,
                "matched": self.total_matched,
                "duplicate": self.total_duplicate,
                "rejected": self.total_rejected,
                "winners": len(self._winners),
                "round": self._round,
                "blacklist": len(self._blacklist),
                "max_count": max(counts) if counts else 0,
                "elapsed": time.time() - self.started_ts,
            }

    def uid_length_histogram(self) -> Dict[int, int]:
        """UID 位数分布 —— 用来提醒主播把「位数范围」设对。"""
        with self._lock:
            hist: Dict[int, int] = {}
            for uid in self._entries:
                hist[len(uid)] = hist.get(len(uid), 0) + 1
            return dict(sorted(hist.items()))

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "saved_ts": time.time(),
                "round": self._round,
                "entries": [asdict(e) for e in self._entries.values()],
                "winners": [asdict(w) for w in self._winners],
                "blacklist": sorted(self._blacklist),
            }

    def load_dict(self, d: dict) -> None:
        with self._lock:
            self._entries.clear()
            self._winners.clear()
            self._blacklist.clear()
            for raw in d.get("entries", []):
                try:
                    e = Entry(**{k: raw[k] for k in raw if k in Entry.__dataclass_fields__})
                except Exception:
                    continue
                self._entries[e.uid] = e
            for raw in d.get("winners", []):
                try:
                    w = Winner(**{k: raw[k] for k in raw if k in Winner.__dataclass_fields__})
                except Exception:
                    continue
                self._winners.append(w)
            self._blacklist = set(d.get("blacklist", []))
            self._round = int(d.get("round", len(self._winners)))

    def save(self, path: Optional[str] = None) -> str:
        path = path or data_path("参与池.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, path)
        return path

    def load(self, path: Optional[str] = None) -> bool:
        path = path or data_path("参与池.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.load_dict(json.load(f))
            return True
        except Exception:
            return False

    # ---- 导出 ---------------------------------------------------------- #

    def export_uids(self, path: str) -> str:
        """导出全部 UID，每行一个（带昵称时写成 昵称<TAB>UID）。"""
        with self._lock:
            rows = sorted(self._entries.values(), key=lambda e: e.first_ts)
        with open(path, "w", encoding="utf-8-sig") as f:
            for e in rows:
                if e.nick:
                    f.write(f"{e.nick}\t{e.uid}\n")
                else:
                    f.write(f"{e.uid}\n")
        return path

    def export_winners(self, path: str) -> str:
        with self._lock:
            rows = list(self._winners)
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("轮次\t中奖UID\t昵称\t时间\n")
            for w in rows:
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(w.ts))
                f.write(f"{w.round_no}\t{w.uid}\t{w.nick}\t{t}\n")
        return path
