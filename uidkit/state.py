# -*- coding: utf-8 -*-
"""会话状态：把配置、参与池、识别引擎、抽取引擎串起来。

所有后台线程（HTTP / 文件 / 剪贴板 / 模拟）都只调用 ``Session.feed()``，
所有界面都只读 ``Session`` 的状态 + 订阅 ``Session.bus`` 的事件。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional

from . import bus as B
from .config import AppConfig, data_path
from .draw import DrawResult, WeightOptions, draw as do_draw
from .normalize import Candidate, extract
from .pool import Entry, Pool, Winner


@dataclass
class DanmakuRecord:
    """一条弹幕的原始内容和识别结果（用于界面上的「识别详情」）。"""

    ts: float
    raw: str
    nick: str = ""
    source: str = ""
    uid: str = ""
    status: str = "ignored"
    """new / dup / blocked / ignored"""
    reason: str = ""

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.ts))


STATUS_LABELS = {
    "new": "新增",
    "dup": "重复",
    "blocked": "黑名单",
    "ignored": "未识别",
}


class Session:
    def __init__(self, cfg: Optional[AppConfig] = None) -> None:
        self.cfg = cfg or AppConfig.load()
        self.pool = Pool()
        self.bus = B.EventBus()
        self.recent: Deque[DanmakuRecord] = deque(maxlen=500)

        self.last_result: Optional[DrawResult] = None
        self.animating = False
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._log_fp = None
        self._log_date = ""
        self._done_timer: Optional[threading.Timer] = None

        # 实时统计
        self.feed_total = 0
        self.feed_matched = 0
        self.feed_ignored = 0

        #: 观众投票盒子（对刚抽中的 UID 评好坏）
        from .vote import VoteBox
        self.votes = VoteBox()

        #: 最近一次抽奖抽中的 UID（用来区分投票对象是「抽中的」还是「手动指定」的）
        self.last_draw_uid: str = ""

        #: 本轮「待开启投票」的对象。
        #: 抽奖结束后自动填成中奖者，等主播点「开始投票」才真正开放投票。
        self.pending_vote_uid: str = ""
        self.pending_vote_nick: str = ""

        #: 投票的截止时间戳（None = 没设时长或不限时）
        self._vote_deadline: Optional[float] = None

        #: 下次自动清空参与池的时间戳（None = 还没开始计时）
        self._pool_clear_at: Optional[float] = None

        #: 主播手动调的中奖倍率：{uid: 倍数}
        #: ★ 单独存一份，和参与池解耦 —— 清空参与池不会清掉它。
        #:   人再进池时自动套用回 Entry.weight_mult 上。
        self.manual_mult: Dict[str, float] = {}

        #: 上一场没中的人：{uid: {"nick": 昵称, "ts": 那一场的时间}}
        #: 下播时把「参与了但没中」的人写进来，开播时给这些人补偿加权。
        self.carry_over: Dict[str, Dict[str, Any]] = {}

        #: UID -> 第一次出现在公屏上的时间。
        #: ★ 这个**单独存**，清空参与池不会清它 ——
        #:   主播开新一轮时参与者会被清掉，但老观众不该因此丢掉
        #:   "看了多久"的优势。同一个人再发弹幕时会用回这里记的最早时间。
        self.first_seen: Dict[str, float] = {}

        #: 在线/贡献榜快照（抖音直连推送）。结构：
        #:   {"ranks": [{"uid","nick","rank","score","score_desc"}, ...],
        #:    "total": int, "popularity": int, "ts": float}
        #: 只保留最新一份，榜窗口读这个显示「在线前五」。
        self.rank: Dict[str, Any] = {}

    # ================================================================== #
    # 弹幕摄入
    # ================================================================== #

    def feed(self, text: str, nick: str = "", source: str = "manual",
             ts: Optional[float] = None) -> Optional[DanmakuRecord]:
        """处理一条弹幕。返回识别记录（未识别也会返回，方便界面显示）。"""
        text = (text or "").replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return None
        ts = ts if ts is not None else time.time()

        with self._lock:
            self.feed_total += 1
            self.pool.note_danmaku()

            # ---- 先看是不是投票弹幕 ----
            # ★ 顺序很重要：必须先用 looks_like_uid() 挡一道。
            #   投票支持数字（1=夯）之后，如果直接 parse，
            #   观众发的 9 位 UID 会被当成「1」号票吞掉，
            #   人就再也进不了参与池了。
            if self.votes.is_open and not self.votes.looks_like_uid(text):
                档 = self.votes.parse(text)
                if 档:
                    谁 = nick or "匿名"
                    # 一人一票靠 nick 或 raw 里的身份；这里用昵称+来源做键，
                    # 拿不到昵称时退回「匿名」——同一场里同名的算一个人。
                    键 = (nick or "").strip() or f"_匿名_{source}"
                    结果 = self.votes.vote(键, 档)
                    if 结果 in ("ok", "changed"):
                        self.bus.emit(B.VOTE_CHANGED, {"style": 档,
                                                       "result": 结果,
                                                       "nick": 谁})
                    elif 结果 == "same":
                        pass        # 重复投同一档，静默忽略
                    rec0 = DanmakuRecord(ts=ts, raw=text, nick=nick or "",
                                         source=source)
                    rec0.uid = ""
                    # ★ 只有真的计上票才算 vote。
                    #   投票没开（或已结束）时票没记上，
                    #   日志里却显示"投票"会误导排查。
                    if 结果 in ("ok", "changed", "same"):
                        rec0.status = "vote"
                        rec0.reason = f"投票：{档}"
                    else:
                        rec0.status = "ignored"
                        rec0.reason = f"投票未开启（{档}）"
                    self.recent.append(rec0)
                    self._write_log(rec0)
                    return rec0

            cands: List[Candidate] = extract(text, self.cfg.extract)

            rec = DanmakuRecord(ts=ts, raw=text, nick=nick or "", source=source)
            if not cands:
                self.feed_ignored += 1
                self.pool.note_rejected()
                rec.status = "ignored"
                rec.reason = "没有识别到 UID"
            else:
                # 一条弹幕只取一个 UID —— 防止有人一次刷好几个号来抬高概率
                best = cands[0]
                rec.uid = best.uid
                rec.reason = best.reason
                status = self.pool.add(best.uid, nick=nick, raw=text,
                                       source=source, ts=ts)
                rec.status = status
                if status in ("new", "dup"):
                    self.feed_matched += 1
                if status == "new":
                    self._remember_first_seen(best.uid, ts)
                    self._apply_manual_mult(best.uid)
                    self._apply_carry_over(best.uid)

            self.recent.append(rec)

        self.bus.emit(B.DANMAKU, rec)
        if rec.status == "new":
            self.bus.emit(B.POOL_CHANGED, {"reason": "new", "uid": rec.uid})
        self._write_log(rec)
        return rec

    def feed_many(self, lines: List[str], nick: str = "", source: str = "paste") -> int:
        """批量导入（粘贴一堆弹幕时用）。返回识别出的条数。"""
        n = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            rec = self.feed(line, nick=nick, source=source)
            if rec and rec.uid:
                n += 1
        return n

    # ================================================================== #
    # 在线/贡献榜
    # ================================================================== #

    def apply_rank(self, rank: Dict[str, Any]) -> None:
        """接收一份榜快照（来自抖音直连桥接的 /rank 推送），更新并广播。

        只保留最新一份。榜数据比弹幕稀疏，这里不落盘 —— 关掉就没了，
        下次直播重新抓即可。
        """
        if not isinstance(rank, dict):
            return
        ranks = rank.get("ranks")
        if not isinstance(ranks, list) or not ranks:
            return
        with self._lock:
            self.rank = {
                "ranks": ranks,
                "total": int(rank.get("total") or 0),
                "popularity": int(rank.get("popularity") or 0),
                "ts": time.time(),
            }
        self.bus.emit(B.RANK_CHANGED, self.rank)

    # ================================================================== #
    # 抽取
    # ================================================================== #

    def weight_options(self) -> WeightOptions:
        """把配置里的加权设置组装成引擎要的参数。

        抽奖和事后复算都走这一个方法，保证两边用的是**同一套**参数。
        """
        c = self.cfg
        return WeightOptions(
            by_watch=bool(c.weight_by_watch),
            full_minutes=float(c.watch_full_minutes),
            watch_bonus=float(c.watch_bonus),
            by_count=bool(c.weighted_by_count),
            count_bonus=0.5,
            base=1.0,
            cap=float(c.weight_cap or 0.0),
            watch_unlimited=bool(getattr(c, "weight_watch_unlimited", False)),
            pity_enabled=bool(getattr(c, "pity_enabled", False)),
            pity_rounds=int(getattr(c, "pity_rounds", 5) or 5),
            pity_mode=str(getattr(c, "pity_mode", "guarantee") or "guarantee"),
            pity_max_mult=float(getattr(c, "pity_max_mult", 64.0) or 64.0),
        )

    def do_draw(self, count: Optional[int] = None) -> Optional[DrawResult]:
        """执行一次抽取。中奖结果立刻落库，动画只是表演。"""
        with self._lock:
            if self.animating:
                self.bus.emit(B.STATUS, "上一轮还在滚动中…")
                return None

            entries = self.pool.eligible(exclude_winners=self.cfg.no_repeat_winner)
            if not entries:
                reason = ("所有人都已经中过奖了" if self.cfg.no_repeat_winner
                          else "参与池是空的")
                self.bus.emit(B.ERROR, f"没法抽：{reason}")
                return None

            n = count if count is not None else self.cfg.draw_count
            n = max(1, min(int(n), len(entries)))

            # 按当前配置组装一套权重参数，交给抽取引擎。
            # ★ 这套参数会被一并写进结果的指纹里，所以事后能用同样的参数复算。
            result = do_draw(entries, count=n,
                             options=self.weight_options())
            round_no = self.pool.commit_winners(
                [Winner(uid=e.uid, nick=e.nick, ts=result.ts, seed=result.seed)
                 for e in result.winners]
            )
            result.round_no = round_no
            self.last_result = result
            self.animating = True

            # ★ 抽完**不再**自动进入投票。
            #   投票由主播点「开始投票」手动开启，观众才能投。
            #   上一轮如果还开着，先关掉 —— 免得观众把票投到上一个人身上。
            if self.votes.is_open:
                self.votes.close()
                self._vote_deadline = None
                self.bus.emit(B.VOTE_CHANGED, {"closed": True})

            # 把中奖者记成「待开启投票」的对象 ——
            # 这样点「开始投票」时留空就能直接用他，界面也有东西显示。
            self.last_draw_uid = (result.winners[0].uid
                                  if result.winners else "")
            if result.winners:
                self.pending_vote_uid = result.winners[0].uid
                self.pending_vote_nick = result.winners[0].nick or ""
            else:
                self.pending_vote_uid = ""
                self.pending_vote_nick = ""
            self.bus.emit(B.VOTE_CHANGED,
                          {"pending": self.pending_vote_uid})

            # ★ 更新「连续没中轮数」——保底机制的依据。
            #   中了的清零，没中的加一。这里必须覆盖**整池**，
            #   不只是有资格参与的那部分（中过奖被排除的人也该累加）。
            中奖的 = {e.uid for e in result.winners}
            for e in self.pool.all_entries():
                if e.uid in 中奖的:
                    e.miss_rounds = 0
                else:
                    e.miss_rounds = int(getattr(e, "miss_rounds", 0) or 0) + 1

        self.bus.emit(B.DRAW_START, result)
        self.bus.emit(B.POOL_CHANGED, {"reason": "draw"})

        # 动画时长结束后广播 DRAW_DONE（不管展示窗在不在）
        delay = max(0.3, self.cfg.animation_ms / 1000.0)
        self._done_timer = threading.Timer(delay, self._finish_draw, args=(result,))
        self._done_timer.daemon = True
        self._done_timer.start()
        return result

    def _finish_draw(self, result: DrawResult) -> None:
        with self._lock:
            self.animating = False
        self.log(f"第{result.round_no}轮中奖：" + "、".join(
            w.display for w in result.winners) + f"（参与 {result.pool_size} 人）")
        self.bus.emit(B.DRAW_DONE, result)
        self.bus.emit(B.POOL_CHANGED, {"reason": "draw_done"})

    def undo_draw(self, n: int = 1) -> int:
        """撤销最近 n 条中奖记录。"""
        popped = self.pool.undraw(n)
        if popped:
            self.log("已撤销中奖记录：" + "、".join(w.uid for w in popped))
            self.bus.emit(B.POOL_CHANGED, {"reason": "undo"})
            self.bus.emit(B.STATUS, f"已撤销 {len(popped)} 条中奖记录")
        return len(popped)

    # ================================================================== #
    # 池操作
    # ================================================================== #

    def remove_uid(self, uid: str) -> bool:
        ok = self.pool.remove(uid)
        if ok:
            self.bus.emit(B.POOL_CHANGED, {"reason": "remove", "uid": uid})
        return ok

    def blacklist_uid(self, uid: str) -> None:
        self.pool.add_blacklist(uid)
        self.log(f"已把 {uid} 加入黑名单")
        self.bus.emit(B.POOL_CHANGED, {"reason": "blacklist"})

    def reset_pool(self, keep_winners: bool = True) -> None:
        """开新一轮：清空参与池。"""
        if keep_winners:
            self.pool.clear_entries()
        else:
            self.pool.clear_all()
        self.recent.clear()
        self.feed_total = self.feed_matched = self.feed_ignored = 0
        # 注意：观看时长记录（first_seen）**不清** —— 见 _remember_first_seen 的说明
        self.log("参与池已清空" + ("（中奖记录保留）" if keep_winners else "（含中奖记录）")
                 + "；观看时长记录保留")
        self.bus.emit(B.POOL_CHANGED, {"reason": "reset"})
        self.bus.emit(B.STATUS, "参与池已清空")

    # ================================================================== #
    # 手动倍率（清池不影响）
    # ================================================================== #

    def vote_path(self) -> str:
        return data_path("投票.json")

    def open_vote_manual(self, uid: str, nick: str = "") -> bool:
        """手动把某个 UID 设为投票对象（不经过抽奖）。

        用途：主播想单独让观众评价某个人时用。
        返回是否成功。
        """
        uid = (uid or "").strip()
        if not uid:
            return False
        # 从池里找昵称，找不到就用传进来的
        池里 = self.pool.get(uid)
        if 池里 is not None and not nick:
            nick = 池里.nick or ""
        秒 = float(getattr(self.cfg.vote, "vote_seconds", 0) or 0)
        # 抽中的那个人 -> 显示「评价对象」；换了别的号 -> 显示「手动指定」
        手动 = bool(self.last_draw_uid and uid != self.last_draw_uid)
        with self._lock:
            # 开了谁就把待开启对象同步成谁，界面上显示的始终是"当前投的人"
            self.pending_vote_uid = uid
            self.pending_vote_nick = nick or ""
            self.votes.open(uid, nick, manual=手动)
            # 设了时长就记个截止时间，由 tick_timers() 到点自动关票
            self._vote_deadline = (time.time() + 秒) if 秒 > 0 else None
        self.log(f"手动开启投票：{nick}({uid})" if nick
                 else f"手动开启投票：{uid}")
        self.bus.emit(B.VOTE_CHANGED, {"manual": True, "uid": uid})
        self.bus.emit(B.STATUS,
                      f"投票已开启：{uid}" +
                      (f"，{秒:g} 秒后自动结束" if 秒 > 0 else ""))
        return True

    def close_vote(self) -> None:
        """结束当前投票。"""
        self._vote_deadline = None
        if not self.votes.is_open:
            return
        self.votes.close()
        self.bus.emit(B.VOTE_CHANGED, {"closed": True})
        self.log("投票已结束")

    # ---- 倒计时 ---------------------------------------------------- #

    def rearm_vote(self) -> None:
        """按当前 vote_seconds 重新计算截止时间。

        主播在投票进行中改了秒数（比如 10 秒改成 30 秒），立刻生效，
        不用等这一轮结束。
        """
        if not self.votes.is_open:
            self._vote_deadline = None
            return
        秒 = float(getattr(self.cfg.vote, "vote_seconds", 0) or 0)
        if 秒 <= 0:
            self._vote_deadline = None          # 不限时
            return
        起 = float(self.votes.state.opened_ts or time.time())
        self._vote_deadline = 起 + 秒

    def vote_remaining(self) -> float:
        """投票还剩几秒。没开投票、或不限时，返回 0。"""
        d = self._vote_deadline
        if d is None or not self.votes.is_open:
            return 0.0
        return max(0.0, d - time.time())

    def pool_clear_remaining(self) -> float:
        """距离下次自动清空参与池还有几秒。没开启返回 0。"""
        if not bool(getattr(self.cfg, "pool_auto_clear", False)):
            return 0.0
        d = self._pool_clear_at
        if d is None:
            分钟 = float(getattr(self.cfg, "pool_auto_clear_minutes", 5) or 0)
            return max(0.0, 分钟 * 60) if 分钟 > 0 else 0.0
        return max(0.0, d - time.time())

    def reset_pool_timer(self) -> None:
        """重新开始计算下次自动清池的时间（改设置或手动清池后调用）。"""
        分钟 = float(getattr(self.cfg, "pool_auto_clear_minutes", 5) or 0)
        if not bool(getattr(self.cfg, "pool_auto_clear", False)) or 分钟 <= 0:
            self._pool_clear_at = None
        else:
            self._pool_clear_at = time.time() + 分钟 * 60

    def tick_timers(self) -> None:
        """主循环每轮调一次：处理投票倒计时和参与池自动清空。

        放在这里而不是开线程，是为了所有状态变更都在同一个线程里，
        不用考虑并发 —— 定时器只做「到点了吗」这一件事。
        """
        try:
            self._tick_vote_deadline()
        except Exception:
            pass
        try:
            self._tick_pool_clear()
        except Exception:
            pass

    def _tick_vote_deadline(self) -> None:
        d = self._vote_deadline
        if d is None or not self.votes.is_open:
            return
        if time.time() >= d:
            self.close_vote()
            self.bus.emit(B.STATUS, "投票时间到，已自动结束")

    def _tick_pool_clear(self) -> None:
        if not bool(getattr(self.cfg, "pool_auto_clear", False)):
            self._pool_clear_at = None
            return
        分钟 = float(getattr(self.cfg, "pool_auto_clear_minutes", 5) or 0)
        if 分钟 <= 0:
            self._pool_clear_at = None
            return
        现在 = time.time()
        if self._pool_clear_at is None:
            self._pool_clear_at = 现在 + 分钟 * 60
            return
        if 现在 < self._pool_clear_at:
            return
        # 到点了：先排下一次，再清池
        self._pool_clear_at = 现在 + 分钟 * 60
        if len(self.pool) <= 0:
            return                      # 池子本来就是空的，不刷日志
        self.reset_pool(keep_winners=True)
        self.bus.emit(B.STATUS, f"已自动清空参与池（每 {分钟:g} 分钟一次）")

    def manual_path(self) -> str:
        return data_path("手动倍率.json")

    def _load_manual(self) -> None:
        """读手动倍率表。"""
        try:
            with open(self.manual_path(), encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.manual_mult = {}
                for k, v in raw.items():
                    try:
                        倍 = float(v)
                    except (TypeError, ValueError):
                        continue
                    if 倍 > 0:
                        self.manual_mult[str(k)] = 倍
        except (FileNotFoundError, ValueError, OSError):
            self.manual_mult = {}

    def _save_manual(self) -> None:
        try:
            p = self.manual_path()
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.manual_mult, f, ensure_ascii=False)
            os.replace(tmp, p)
        except OSError as exc:
            self.bus.emit(B.ERROR, f"保存手动倍率失败：{exc}")

    def _apply_manual_mult(self, uid: str) -> None:
        """人进池时，如果倍率表里有他，套用上去。"""
        倍 = self.manual_mult.get(uid)
        if not 倍:
            return
        e = self.pool.get(uid)
        if e is not None:
            e.weight_mult = 倍

    def set_manual_mult(self, uids, 倍率: float) -> int:
        """设置若干人的手动倍率。倍率 <= 0 或 == 1 表示恢复正常。

        返回实际改动的人数。
        """
        值 = float(倍率)
        规范 = 0.0 if (值 <= 0 or abs(值 - 1.0) < 1e-9) else max(0.01, 值)
        改 = 0
        with self._lock:
            for uid in uids:
                uid = str(uid).strip()
                if not uid:
                    continue
                # 写进倍率表（持久化，清池不影响）
                if 规范 <= 0:
                    self.manual_mult.pop(uid, None)
                else:
                    self.manual_mult[uid] = 规范
                # 同步到当前池里的那个 Entry
                e = self.pool.get(uid)
                if e is not None:
                    e.weight_mult = 规范
                改 += 1
            self._save_manual()
        return 改

    def get_manual_mult(self, uid: str) -> float:
        return float(self.manual_mult.get(str(uid).strip(), 0.0) or 0.0)

    def manual_mult_count(self) -> int:
        return len(self.manual_mult)

    def clear_manual_mult(self) -> int:
        """清空所有手动倍率。返回清掉了多少条。"""
        with self._lock:
            n = len(self.manual_mult)
            self.manual_mult.clear()
            for e in self.pool.all_entries():
                e.weight_mult = 0.0
            self._save_manual()
        if n:
            self.log(f"已清空手动倍率（{n} 条）")
            self.bus.emit(B.POOL_CHANGED, {"reason": "manual_clear"})
        return n

    # ================================================================== #
    # 场次：下播归档 / 开播补偿
    # ================================================================== #

    def carry_path(self) -> str:
        """待补偿名单存在哪。"""
        return data_path("上期未中.json")

    def _load_carry(self) -> None:
        """读上次下播归档的「没中名单」。"""
        try:
            with open(self.carry_path(), encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.carry_over = {
                    str(k): (v if isinstance(v, dict) else {"ts": 0})
                    for k, v in raw.items()
                }
        except (FileNotFoundError, ValueError, OSError):
            self.carry_over = {}
        self._prune_carry()

    def _save_carry(self) -> None:
        try:
            p = self.carry_path()
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.carry_over, f, ensure_ascii=False)
            os.replace(tmp, p)
        except OSError as exc:
            self.bus.emit(B.ERROR, f"保存待补偿名单失败：{exc}")

    def _prune_carry(self) -> int:
        """丢掉过期的补偿记录。"""
        天 = float(getattr(self.cfg, "carry_expire_days", 7) or 0)
        if 天 <= 0:
            return 0
        截止 = time.time() - 天 * 86400
        旧的 = [k for k, v in self.carry_over.items()
                if float(v.get("ts", 0) or 0) < 截止]
        for k in 旧的:
            self.carry_over.pop(k, None)
        return len(旧的)

    def _apply_carry_over(self, uid: str) -> bool:
        """如果这个人在上期没中名单里，给他带上补偿倍率。"""
        if not bool(getattr(self.cfg, "carry_enabled", True)):
            return False
        info = self.carry_over.get(uid)
        if not info:
            return False
        e = self.pool.get(uid)
        if e is None:
            return False
        倍 = max(1.0, float(getattr(self.cfg, "carry_mult", 2.0) or 2.0))
        # 补偿倍率单独记，不覆盖主播手动调的 weight_mult
        e.carry_mult = 倍
        e.carry_from = float(info.get("ts", 0) or 0)
        return True

    def offline(self) -> Dict[str, Any]:
        """下播：归档本场没中的人，然后清空本场数据。

        清理内容（按你的要求「完全重开」）：
          · 参与池（所有参与者）
          · 观看时长记录（first_seen）
          · 中奖记录
        保留：
          · 本场参与过但没中的 UID -> 写进待补偿名单，下次开播自动加权
        """
        with self._lock:
            参与者 = list(self.pool.all_entries())
            中奖的 = {w.uid for w in self.pool.winners}
            没中的 = [e for e in 参与者 if e.uid not in 中奖的]
            现在 = time.time()

            # 写进待补偿名单。同一批人如果之前就在名单里，
            # 刷新时间戳（表示「最近这一场也没中」）。
            for e in 没中的:
                self.carry_over[e.uid] = {
                    "nick": e.nick or "",
                    "ts": 现在,
                }
            self._prune_carry()
            self._save_carry()

            # 清空本场
            n_pool = len(参与者)
            self.pool.clear_all()
            self.recent.clear()
            self.first_seen.clear()
            self._save_first_seen()
            self.feed_total = self.feed_matched = self.feed_ignored = 0
            self.last_result = None

            摘要 = {
                "participants": n_pool,
                "winners": len(中奖的),
                "carried": len(没中的),
                "carry_total": len(self.carry_over),
            }

        self.log(f"本场结束：{n_pool} 人参与、{len(中奖的)} 人中奖，"
                 f"{len(没中的)} 人转入下期补偿名单")
        self.bus.emit(B.POOL_CHANGED, {"reason": "offline"})
        self.bus.emit(B.STATUS, f"已下播：清空本场数据，"
                                f"{摘要['carried']} 人将获得下期补偿")
        return 摘要

    def clear_carry(self) -> int:
        """手动清空待补偿名单。返回清掉了多少条。"""
        with self._lock:
            n = len(self.carry_over)
            self.carry_over.clear()
            self._save_carry()
        if n:
            self.log(f"已清空待补偿名单（{n} 人）")
            self.bus.emit(B.POOL_CHANGED, {"reason": "carry_clear"})
        return n

    def carry_count(self) -> int:
        return len(self.carry_over)

    # ================================================================== #
    # 首次出现时间（观看时长的起点）
    # ================================================================== #

    def _remember_first_seen(self, uid: str, ts: float) -> None:
        """记下/更新某人的首次出现时间，并把池里那条记录的 first_ts 校准过来。

        ★ 关键点：如果这个人**以前来过**（参与池被清过一次），
          就沿用之前记的最早时间，而不是从这次重新算。
          这样"清空参与池"只清参与者，不会把老观众的观看时长清零。
        """
        prev = self.first_seen.get(uid)
        earliest = ts if prev is None else min(prev, ts)
        self.first_seen[uid] = earliest

        e = self.pool.get(uid)
        if e is not None and earliest < e.first_ts:
            # 池里这条是本次新建的（first_ts = 这次的时间），
            # 往前挪到真正的首次出现时间
            e.first_ts = earliest

    def forget_first_seen(self, uid: Optional[str] = None) -> int:
        """忘掉观看时长记录。uid 留空 = 全部清空。返回清掉了多少条。"""
        with self._lock:
            if uid is None:
                n = len(self.first_seen)
                self.first_seen.clear()
            else:
                n = 1 if self.first_seen.pop(uid, None) is not None else 0
        if n:
            self._save_first_seen()
            self.bus.emit(B.POOL_CHANGED, {"reason": "watch_reset"})
        return n

    def first_seen_path(self) -> str:
        return data_path("首次出现.json")

    def _load_first_seen(self) -> None:
        """从磁盘恢复观看时长记录，顺便丢掉太旧的。"""
        路径 = self.first_seen_path()
        try:
            with open(路径, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.first_seen = {str(k): float(v)
                                   for k, v in raw.items() if v}
        except (FileNotFoundError, ValueError, OSError, TypeError):
            self.first_seen = {}
        self.prune_first_seen()

    def _save_first_seen(self) -> None:
        路径 = self.first_seen_path()
        try:
            tmp = 路径 + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.first_seen, f, ensure_ascii=False)
            os.replace(tmp, 路径)
        except OSError as exc:
            self.bus.emit(B.ERROR, f"保存观看时长记录失败：{exc}")

    def prune_first_seen(self) -> int:
        """丢掉超过保留时长的记录。

        不清理的话，昨天看了 3 小时的人今天一来就是满权重，
        对新观众不公平。默认保留 12 小时（够覆盖一整场直播）。
        """
        小时 = max(0.0, float(getattr(self.cfg, "watch_memory_hours", 12) or 0))
        if 小时 <= 0:
            return 0
        截止 = time.time() - 小时 * 3600
        旧的 = [k for k, v in self.first_seen.items() if v < 截止]
        for k in 旧的:
            self.first_seen.pop(k, None)
        return len(旧的)

    # ================================================================== #
    # 日志
    # ================================================================== #

    def log(self, msg: str) -> None:
        self.bus.emit(B.LOG, msg)

    def _write_log(self, rec: DanmakuRecord) -> None:
        if not self.cfg.save_danmaku_log or self.cfg.source.sandbox:
            return
        try:
            with self._log_lock:
                day = time.strftime("%Y%m%d", time.localtime(rec.ts))
                if self._log_fp is None or self._log_date != day:
                    if self._log_fp is not None:
                        try:
                            self._log_fp.close()
                        except Exception:
                            pass
                    path = data_path("弹幕记录", f"弹幕-{day}.jsonl")
                    self._log_fp = open(path, "a", encoding="utf-8")
                    self._log_date = day
                self._log_fp.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
                self._log_fp.flush()
        except Exception:
            pass

    # ================================================================== #
    # 持久化
    # ================================================================== #

    def save(self, with_config: bool = True) -> None:
        try:
            self.votes.save(self.vote_path())
        except Exception:
            pass
        try:
            self._save_manual()
        except Exception as exc:
            self.bus.emit(B.ERROR, f"保存手动倍率失败：{exc}")
        try:
            self.prune_first_seen()
            self._save_first_seen()
        except Exception as exc:
            self.bus.emit(B.ERROR, f"保存观看时长记录失败：{exc}")
        try:
            self.pool.save()
        except Exception as exc:
            self.bus.emit(B.ERROR, f"保存参与池失败：{exc}")
        if with_config:
            try:
                self.cfg.save()
            except Exception as exc:
                self.bus.emit(B.ERROR, f"保存设置失败：{exc}")

    def load(self) -> None:
        # 投票数据
        try:
            self.votes.load_file(self.vote_path())
        except Exception:
            pass
        # 手动倍率先读 —— 恢复参与池时要用它套回去
        self._load_manual()
        if self.manual_mult:
            self.log(f"已恢复 {len(self.manual_mult)} 条手动倍率设置")
        # 待补偿名单先读，新进池的人要用它判断补偿
        self._load_carry()
        if self.carry_over:
            self.log(f"有 {len(self.carry_over)} 人上期没中，本期可获补偿加权")
        # 观看时长记录先加载 —— 参与池恢复时要用它校准 first_ts
        self._load_first_seen()
        if self.first_seen:
            self.log(f"已恢复 {len(self.first_seen)} 个人的观看时长记录")
        if self.pool.load():
            # 把手动倍率套回已经恢复的条目上
            for e in self.pool.all_entries():
                倍 = self.manual_mult.get(e.uid)
                if 倍:
                    e.weight_mult = 倍
            self.log(f"已恢复上次的参与池：{len(self.pool)} 人、"
                     f"{len(self.pool.winners)} 条中奖记录")

    # ================================================================== #
    # 给界面 / 浏览器页面的快照
    # ================================================================== #

    def snapshot(self) -> Dict[str, Any]:
        st = self.pool.stats()
        lr = self.last_result
        return {
            "pool_size": st["participants"],
            "eligible": len(self.pool.eligible(self.cfg.no_repeat_winner)),
            "danmaku_total": self.feed_total,
            "matched": self.feed_matched,
            "ignored": self.feed_ignored,
            "winners_total": st["winners"],
            "round": st["round"],
            "animating": self.animating,
            "last": None if lr is None else {
                "uids": lr.winner_uids,
                "nicks": [w.nick for w in lr.winners],
                "round": lr.round_no,
                "pool": lr.pool_size,
                "seed": lr.seed,
                "fingerprint": lr.fingerprint,
                "reel": lr.reel,
            },
            "winner_list": [
                {"uid": w.uid, "nick": w.nick, "round": w.round_no}
                for w in self.pool.winners[-20:]
            ],
            # 投票状态（给 /status 查，方便排查）
            "vote": {
                "target_uid": self.votes.state.target_uid,
                "pending_uid": self.pending_vote_uid,
                "pending_nick": self.pending_vote_nick,
                "vote_seconds": float(getattr(self.cfg.vote, "vote_seconds", 0) or 0),
                "vote_remaining": round(self.vote_remaining(), 1),
                "pool_clear_in": round(self.pool_clear_remaining(), 1),
                "target_nick": self.votes.state.target_nick,
                "manual": self.votes.state.manual,
                "open": self.votes.is_open,
                "total": self.votes.state.total,
                "counts": dict(self.votes.state.counts),
                "leader": self.votes.state.leader(),
            },
            "recent": [
                {"uid": r.uid, "raw": r.raw, "nick": r.nick,
                 "status": r.status, "clock": r.clock,
                 "reason": r.reason}
                for r in list(self.recent)[-20:]
            ],
            # 在线/贡献榜快照（抖音直连推送的最新一份）
            "rank": {
                "total": int((self.rank.get("total") or 0)),
                "popularity": int((self.rank.get("popularity") or 0)),
                "ranks": (self.rank.get("ranks") or [])[:5],
                "ts": self.rank.get("ts"),
            },
        }
