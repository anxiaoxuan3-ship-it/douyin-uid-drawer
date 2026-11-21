# -*- coding: utf-8 -*-
"""观众投票：给刚抽中的 UID 评好坏。

五个档位（从低到高）：
    拉完了 < NPC < 人上人 < 顶级 < 夯

规则：
  · 观众在公屏发档位词即投票，例如发「顶级」
  · **一人一票，可以改票** —— 同一个人再发别的档位，等于把票改过去
  · 同一个档位重复发不累加（防刷屏）

投票对象是「本轮刚抽中的那个 UID」—— 抽奖后自动开启投票，
所以需要和抽奖流程联动。
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: 五个档位，**从低到高**。顺序就是窗口里从下往上（或从低到高）的排列。
档位 = ["拉完了", "NPC", "人上人", "顶级", "夯"]

#: 每个档位的配色（越好的越亮）
档位颜色 = {
    "拉完了": "#8B6B6B",
    "NPC": "#6B7B8B",
    "人上人": "#5B8FB9",
    "顶级": "#D9A441",
    "夯": "#FF5C3D",
}

#: 数字投票：1 = 最高档（夯），数字越大评价越差。
#: 观众发「1」就等于投「夯」，比打字快得多。
数字档位 = {
    "1": "夯",
    "2": "顶级",
    "3": "人上人",
    "4": "NPC",
    "5": "拉完了",
}

#: 反向映射：档位 -> 数字（窗口上标出来，告诉观众发几）
档位数字 = {v: k for k, v in 数字档位.items()}

#: 每个档位的短说明（窗口里显示，帮观众理解）
档位说明 = {
    "拉完了": "很差",
    "NPC": "一般",
    "人上人": "不错",
    "顶级": "很好",
    "夯": "封神",
}


@dataclass
class VoteState:
    """一轮投票的完整状态。"""

    target_uid: str = ""
    """投的是谁（本轮中奖的 UID）。"""

    target_nick: str = ""
    opened_ts: float = 0.0
    closed: bool = False

    manual: bool = False
    """是不是主播手动指定的对象（而不是抽奖抽出来的）。"""

    counts: Dict[str, int] = field(default_factory=dict)
    """各档位的票数。"""

    voters: Dict[str, str] = field(default_factory=dict)
    """投票人 UID -> 投的档位。用来实现「一人一票可改票」。"""

    def __post_init__(self) -> None:
        for d in 档位:
            self.counts.setdefault(d, 0)

    # ---- 统计 ---------------------------------------------------- #

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def percent(self, 档: str) -> float:
        总 = self.total
        return (self.counts.get(档, 0) / 总 * 100.0) if 总 else 0.0

    def leader(self) -> Optional[str]:
        """票数最高的档位；平票时取更高的那档；没票返回 None。"""
        有票 = [(self.counts.get(d, 0), i, d) for i, d in enumerate(档位)]
        有票 = [x for x in 有票 if x[0] > 0]
        if not 有票:
            return None
        # 票数优先，平票取序号大的（更高档）
        有票.sort(key=lambda x: (x[0], x[1]))
        return 有票[-1][2]

    def to_dict(self) -> dict:
        return {
            "target_uid": self.target_uid,
            "target_nick": self.target_nick,
            "opened_ts": self.opened_ts,
            "closed": self.closed,
            "manual": self.manual,
            "counts": dict(self.counts),
            "voters": dict(self.voters),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VoteState":
        s = cls(
            target_uid=str(d.get("target_uid", "")),
            target_nick=str(d.get("target_nick", "")),
            opened_ts=float(d.get("opened_ts", 0) or 0),
            closed=bool(d.get("closed", False)),
            manual=bool(d.get("manual", False)),
        )
        for k, v in (d.get("counts") or {}).items():
            if k in 档位:
                try:
                    s.counts[k] = max(0, int(v))
                except (TypeError, ValueError):
                    pass
        for k, v in (d.get("voters") or {}).items():
            if v in 档位:
                s.voters[str(k)] = str(v)
        # 用 voters 重算 counts，保证两者一致（防止存档被手改坏）
        重算 = {d2: 0 for d2 in 档位}
        for 档 in s.voters.values():
            重算[档] = 重算.get(档, 0) + 1
        s.counts = 重算
        return s


class VoteBox:
    """投票盒子：管一轮投票的开关和计票。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state = VoteState()
        self._last_change = 0.0
        #: 最近一次变动的档位（给窗口做高亮动画用）
        self.last_style = ""
        #: 每次变动 +1，窗口靠它判断要不要重绘
        self.revision = 0

    # ---- 开 / 关 -------------------------------------------------- #

    def open(self, uid: str, nick: str = "", manual: bool = False) -> VoteState:
        """开始一轮新投票。

        ``manual=True`` 表示这是主播手动指定的对象（不是抽奖抽出来的），
        窗口上会标出来，避免观众误以为这轮抽奖结果变了。
        """
        with self._lock:
            self.state = VoteState(target_uid=str(uid or "").strip(),
                                   target_nick=nick or "",
                                   opened_ts=time.time())
            self.state.manual = bool(manual)
            self.last_style = ""
            self.revision += 1
            return self.state

    def close(self) -> None:
        with self._lock:
            self.state.closed = True
            self.revision += 1

    def reset(self) -> None:
        with self._lock:
            self.state = VoteState()
            self.last_style = ""
            self.revision += 1

    @property
    def is_open(self) -> bool:
        with self._lock:
            return bool(self.state.target_uid) and not self.state.closed

    # ---- 计票 ---------------------------------------------------- #

    def parse(self, text: str) -> Optional[str]:
        """从一条弹幕里认出档位。认不出返回 None。

        支持两种写法：
          1. **文字**：发「顶级」即可。允许带别的字（「这个顶级吧」），
             只认最长的那个匹配，避免「顶级」和「人上人」混淆。
          2. **数字**：发「1」= 夯，「2」= 顶级 …「5」= 拉完了。

        ★ 数字必须**整条就是那一个数字**（允许前后空格）。
          绝不能用「包含」来判断 —— 否则观众发的 9 位 UID
          （如 100591822）会被当成 1 号票吞掉，人也就进不了参与池了。
        """
        if not text:
            return None

        # ---- 先试数字：整条弹幕就是一个 1~5 ----
        s = text.strip()
        if s in 数字档位:
            return 数字档位[s]
        # 也允许「1票」「投1」这种带单位的写法，但仍然要求很短，
        # 防止把长数字串误判成投票
        if len(s) <= 3:
            for 数字, 档 in 数字档位.items():
                if s in (f"{数字}票", f"投{数字}", f"{数字}号"):
                    return 档

        # ---- 再试文字 ----
        # 从长到短匹配，防止短词抢先命中
        for 档 in sorted(档位, key=len, reverse=True):
            if 档 in text:
                return 档
        return None

    def looks_like_uid(self, text: str) -> bool:
        """这条弹幕像不像一个 UID（用来避免把 UID 当投票）。

        只要去掉分隔符后有 6 位以上连续数字，就当成 UID 处理 ——
        宁可少记一票，也不能把参与者吞掉。
        """
        if not text:
            return False
        import re as _re
        纯 = _re.sub(r"[\s\.\-_/|·]+", "", text)
        if not 纯.isdigit():
            # 里面还有别的字符，再看有没有 6 位以上数字串
            return bool(_re.search(r"\d{6,}", 纯))
        return len(纯) >= 6

    def vote(self, voter_uid: str, 档: str) -> str:
        """记一票。返回结果：

        ``"ok"``      新投了一票
        ``"changed"`` 改票成功
        ``"same"``    重复投同一档（不累加）
        ``"closed"``  投票没开
        ``"bad"``     档位不认识
        """
        if 档 not in 档位:
            return "bad"
        with self._lock:
            if not self.state.target_uid or self.state.closed:
                return "closed"
            voter_uid = str(voter_uid or "").strip() or "_匿名"
            旧 = self.state.voters.get(voter_uid)
            if 旧 == 档:
                return "same"
            if 旧:
                # 改票：旧档减一，新档加一
                self.state.counts[旧] = max(0, self.state.counts.get(旧, 0) - 1)
                结果 = "changed"
            else:
                结果 = "ok"
            self.state.voters[voter_uid] = 档
            self.state.counts[档] = self.state.counts.get(档, 0) + 1
            self.last_style = 档
            self._last_change = time.time()
            self.revision += 1
            return 结果

    # ---- 存档 ---------------------------------------------------- #

    def snapshot(self) -> dict:
        with self._lock:
            return self.state.to_dict()

    def load(self, d: dict) -> None:
        with self._lock:
            try:
                self.state = VoteState.from_dict(d or {})
            except Exception:
                self.state = VoteState()
            self.revision += 1

    def save(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.snapshot(), f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

    def load_file(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                self.load(json.load(f))
        except (FileNotFoundError, ValueError, OSError):
            pass

    # ---- 给界面用的摘要 ------------------------------------------ #

    def rows(self) -> List[dict]:
        """返回窗口要画的数据：[{档位, 票数, 百分比, 颜色, 说明}]。"""
        with self._lock:
            out = []
            for 档 in 档位:
                out.append({
                    "档位": 档,
                    "数字": 档位数字.get(档, ""),
                    "票数": self.state.counts.get(档, 0),
                    "百分比": self.state.percent(档),
                    "颜色": 档位颜色.get(档, "#888888"),
                    "说明": 档位说明.get(档, ""),
                })
            return out
