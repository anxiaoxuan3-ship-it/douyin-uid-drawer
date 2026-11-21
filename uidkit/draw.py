# -*- coding: utf-8 -*-
"""抽取引擎（含「观看时长加权」）。

公平性设计
----------
* 用 ``secrets`` 产生随机种子（操作系统级密码学随机源），再用该种子驱动一个
  独立的 ``random.Random``。种子不可预测，事后可复算。
* 抽之前对"参与名单 **及其权重**"算 SHA-256 指纹。
  ★ 这一点很关键：老版本只哈希 UID，所以**加权模式根本没法复算验证** ——
    别人不知道你用的是哪套权重。现在权重一起进指纹，
    种子 + 指纹 + 抽奖时刻 三者公示后，谁都能复算出同一结果。
* 按需洗牌 / 按权重不放回抽取，同一轮不会重复抽到同一个人。

权重公式
--------
::

    权重 = 保底 + 时长加成 + 发言加成

    时长加成 = 时长系数 × min(1, 已观看秒数 / 看满秒数)
    发言加成 = 发言系数 × (发言次数 - 1)

「已观看秒数」用该 UID **第一次出现在公屏上的时间**到**抽奖那一刻**的间隔来衡量：
在池子里待得越久，说明看得越久，权重越高，中奖概率越大。

默认参数下的效果：刚来的人权重 1.0，看满 30 分钟的人权重 3.0 ——
老观众有 3 倍优势，但新来的也不是没有机会（这对直播间气氛是好事）。
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .pool import Entry

# --------------------------------------------------------------------------- #
# 权重参数
# --------------------------------------------------------------------------- #


@dataclass
class WeightOptions:
    """一套完整的权重参数。抽奖和复算必须用**完全相同**的一套。"""

    by_watch: bool = False
    """是否按观看时长加权。"""

    full_minutes: float = 30.0
    """看满多少分钟拿满时长加成。"""

    watch_bonus: float = 2.0
    """拿满时额外加多少倍权重（2.0 表示看满的人是新来的 3 倍）。"""

    by_count: bool = False
    """是否按发言次数加权。"""

    count_bonus: float = 0.5
    """每多发一条弹幕额外加多少权重。"""

    base: float = 1.0
    """保底权重。所有人都至少有这么多，保证新人也有机会。"""

    cap: float = 0.0
    """权重上限。0 表示不限制。

    ★ 无限叠加模式下这个值仍然有效（作为安全阀），
      默认给一个很大的数防止极端情况把概率算成 inf。
    """

    watch_unlimited: bool = False
    """观看时长权重是否**无限叠加**。

    关掉（默认旧行为）：看满 full_minutes 分钟就吃满 watch_bonus，
    之后再久也不再涨。权重区间是 [base, base + watch_bonus]。

    打开：权重**一直随观看时长线性上涨，不封顶**。
    每看满 full_minutes 分钟就多拿到一份 watch_bonus。
    例如 full_minutes=30、watch_bonus=1.0 时：
      看了 30 分钟 -> +1.0
      看了 2 小时   -> +4.0
      看了 6 小时   -> +12.0
    公式：w = base + watch_bonus * (观看分钟 / full_minutes)
    """

    # ---- 保底 ----
    pity_enabled: bool = False
    """是否开启保底。"""

    pity_rounds: int = 5
    """连续多少轮没中，就触发保底。"""

    pity_mode: str = "guarantee"
    """保底方式：

    ``"guarantee"``  —— 到点直接必中（本轮必抽到他）
    ``"multiply"``   —— 到点后权重翻倍，每多一轮再翻一倍（温和版）
    """

    pity_max_mult: float = 64.0
    """``multiply`` 模式下权重最多翻到几倍，防止数值爆炸。"""

    @property
    def active(self) -> bool:
        """有没有开启任何加权（没开就是纯等概率）。

        ★ 注意：这里只能判断「配置层面」有没有开加权。
          某个人身上带的 weight_mult / carry_mult 属于**数据层面**，
          这个属性看不到 —— 所以 draw() 里还要额外检查一次，
          否则「只调了倍率、没开别的加权」时倍率会完全失效。
        """
        return self.by_watch or self.by_count or self.pity_enabled

    def to_dict(self) -> Dict[str, object]:
        return {
            "by_watch": self.by_watch,
            "full_minutes": self.full_minutes,
            "watch_bonus": self.watch_bonus,
            "by_count": self.by_count,
            "count_bonus": self.count_bonus,
            "base": self.base,
            "cap": self.cap,
            "watch_unlimited": self.watch_unlimited,
            "pity_enabled": self.pity_enabled,
            "pity_rounds": self.pity_rounds,
            "pity_mode": self.pity_mode,
            "pity_max_mult": self.pity_max_mult,
        }

    def describe(self) -> str:
        """给公屏/日志用的一句话说明。"""
        parts = []
        if self.by_watch:
            parts.append(
                f"观看时长加权（看满{self.full_minutes:g}分钟 ×"
                f"{1 + self.watch_bonus:g}）")
        if self.by_count:
            parts.append(f"发言加权（每条 +{self.count_bonus:g}）")
        return " + ".join(parts) if parts else "等概率"


def compute_weights(entries: Sequence[Entry], now: float,
                    opt: WeightOptions) -> List[float]:
    """算出每个人的中奖权重。

    ``now`` 是**抽奖那一刻**的时间戳。复算时必须传同一个值，
    否则时长不同、权重不同、结果自然对不上。
    """
    out: List[float] = []
    full_secs = max(1.0, float(opt.full_minutes) * 60.0)

    for e in entries:
        w = float(opt.base)

        if opt.by_watch:
            # 已经看了多久：从「第一次出现在公屏」到现在。
            # first_ts 为 0 说明是导入进来的、没有时间信息 —— 不给加成，
            # 免得有人靠导入拿到满权重。
            watched = max(0.0, now - float(e.first_ts)) if e.first_ts else 0.0
            if opt.watch_unlimited:
                # ★ 无限叠加：每看满 full_minutes 就多拿一份 watch_bonus，
                #   不设上限。看了越久权重越高，没有封顶。
                multiple = watched / full_secs
                w += float(opt.watch_bonus) * multiple
            else:
                ratio = min(1.0, watched / full_secs)
                w += float(opt.watch_bonus) * ratio

        if opt.by_count:
            # count 至少是 1，所以 (count-1) 表示"多发了几条"
            extra = max(0, int(e.count) - 1)
            w += float(opt.count_bonus) * extra

        # ---- 手动调整的倍率（主播给某个人单独加/减权重）----
        override = float(getattr(e, "weight_mult", 0.0) or 0.0)
        if override > 0:
            w *= override

        # ---- 跨场次补偿：上一场参与了但没中，这一场给点加权 ----
        # 和手动倍率是两条独立通道，同时存在就相乘。
        carry = float(getattr(e, "carry_mult", 0.0) or 0.0)
        if carry > 0:
            w *= carry

        # ---- 保底：连续没中的轮数越多，权重越高 ----
        if opt.pity_enabled:
            miss = int(getattr(e, "miss_rounds", 0) or 0)
            if miss >= max(1, int(opt.pity_rounds)):
                if opt.pity_mode == "multiply":
                    # 超过保底线后，每多一轮再翻一倍
                    extra_rounds = miss - int(opt.pity_rounds)
                    w *= min(float(opt.pity_max_mult),
                             2.0 ** min(20, extra_rounds))
                # guarantee 模式不在这里加权，由 draw() 直接指定

        if opt.cap and opt.cap > 0:
            w = min(w, float(opt.cap))

        out.append(max(0.0, w))

    return out


def pity_guaranteed(entries: Sequence[Entry], opt: WeightOptions):
    """保底模式是 guarantee 时，找出该「必中」的那个人。

    返回 (下标, 该人连续没中轮数)；没人触发保底就返回 (None, 0)。
    多人同时触发时，挑**连续没中轮数最多**的那个。
    """
    if not opt.pity_enabled or opt.pity_mode != "guarantee":
        return (None, 0)
    need = max(1, int(opt.pity_rounds))
    best_i, best_miss = None, 0
    for i, e in enumerate(entries):
        miss = int(getattr(e, "miss_rounds", 0) or 0)
        if miss >= need and miss > best_miss:
            best_i, best_miss = i, miss
    return (best_i, best_miss)


def watch_seconds(entry: Entry, now: float) -> float:
    """某个人已经看了多久（秒）。没有首次出现时间的返回 0。"""
    if not entry.first_ts:
        return 0.0
    return max(0.0, now - entry.first_ts)


# --------------------------------------------------------------------------- #
# 抽取结果
# --------------------------------------------------------------------------- #


@dataclass
class DrawResult:
    """一次抽取的完整结果。"""

    winners: List[Entry]
    seed: str
    fingerprint: str
    pool_size: int
    ts: float
    round_no: int = 0
    reel: List[str] = field(default_factory=list)
    """滚动动画用的候选 UID 序列（最后一个是中奖者）。"""

    weights: List[float] = field(default_factory=list)
    """参与名单每个人的权重（和 winners 对应的 entries 同序）。"""

    options: WeightOptions = field(default_factory=WeightOptions)

    # ---- 兼容老接口 ---- #
    weight_by_count: bool = False

    @property
    def winner_uids(self) -> List[str]:
        return [w.uid for w in self.winners]

    @property
    def weighted(self) -> bool:
        return self.options.active

    def weight_range(self) -> Tuple[float, float]:
        if not self.weights:
            return (0.0, 0.0)
        return (min(self.weights), max(self.weights))

    def proof(self) -> str:
        """一行公平性凭证，可以直接发到公屏。"""
        ids = ",".join(self.winner_uids)
        tail = ""
        if self.weighted:
            lo, hi = self.weight_range()
            tail = f" 权重{lo:.2f}~{hi:.2f}"
        return (f"第{self.round_no}轮 参与{self.pool_size}人{tail} "
                f"种子{self.seed} 指纹{self.fingerprint} 结果[{ids}]")

    def to_dict(self) -> dict:
        """完整存档。权重参数和抽奖时刻都存下来，方便日后复算。"""
        return {
            "winners": [{"uid": w.uid, "nick": w.nick} for w in self.winners],
            "seed": self.seed,
            "fingerprint": self.fingerprint,
            "pool_size": self.pool_size,
            "ts": self.ts,
            "round_no": self.round_no,
            "reel": self.reel,
            "weights": [round(w, 6) for w in self.weights],
            "options": self.options.to_dict(),
        }

    #: 本次抽中的人里，有几个是走保底直接进的（0 表示没触发）
    pity_hit: int = 0



# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #


def pool_fingerprint(entries: Sequence[Entry],
                     weights: Optional[Sequence[float]] = None) -> str:
    """参与名单指纹。

    ★ 传了 ``weights`` 就把权重一起算进去。不这么做的话，加权模式没法验证 ——
    别人只看得到 UID 名单，不知道每个人被赋了多少权重，算不出同样的结果。
    """
    if weights is None:
        payload = sorted(e.uid for e in entries)
    else:
        payload = sorted(f"{e.uid}:{w:.6f}"
                         for e, w in zip(entries, weights))
    blob = json.dumps(payload, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8].upper()


# --------------------------------------------------------------------------- #
# 抽取算法
# --------------------------------------------------------------------------- #


def _pick_indices(n: int, k: int, rng) -> List[int]:
    """Fisher–Yates 部分洗牌：从 n 个里等概率取 k 个不重复的下标。"""
    idx = list(range(n))
    k = min(k, n)
    for i in range(k):
        j = rng.randrange(i, n)
        idx[i], idx[j] = idx[j], idx[i]
    return idx[:k]


def _weighted_pick(weights: Sequence[float], k: int, rng) -> List[int]:
    """按权重不放回抽取 k 个下标。

    比老版本快一点：总数只在开头算一次，每抽走一个人就减掉他的权重，
    不用每次都 ``sum()`` 扫一遍（老版本是 O(n·k) 的重复求和）。
    """
    n = len(weights)
    w = list(weights)
    order = list(range(n))
    out: List[int] = []
    total = sum(w)

    # ★ 权重全是 0（比如把保底权重和加成全设成 0）时，不能一个人都不抽。
    #   原来这里 total <= 0 直接 break，结果是「点了抽取什么都没出来」，
    #   界面上看像卡死了。这里退化成等概率，保证总能抽够人。
    if total <= 0:
        w = [1.0] * n
        total = float(n)

    for _ in range(min(k, n)):
        if total <= 0:
            # 抽到后面总权重可能归零（都被抽走了），剩下的按顺序补
            for i in order:
                if len(out) >= min(k, n):
                    break
                out.append(i)
            break
        r = rng.random() * total
        acc = 0.0
        chosen = -1
        for i in range(len(order)):
            acc += w[order[i]]
            if r < acc:
                chosen = i
                break
        if chosen < 0:                     # 浮点误差兜底
            chosen = len(order) - 1
        idx = order.pop(chosen)
        total -= w[idx]
        out.append(idx)

    return out


def draw(entries: Sequence[Entry], count: int = 1,
         weight_by_count: bool = False,
         reel_len: int = 34,
         seed: Optional[str] = None,
         options: Optional[WeightOptions] = None,
         now: Optional[float] = None) -> DrawResult:
    """从参与名单里抽取 ``count`` 个中奖者。

    ``seed`` 留空时自动用密码学随机源生成；传入固定值可复现。
    ``now`` 留空时取当前时间；复算时**必须**传当时的抽奖时刻。
    ``options`` 留空则用老参数 ``weight_by_count`` 构造一套（保持兼容）。
    """
    entries = list(entries)
    now = time.time() if now is None else float(now)
    ts = now

    if options is None:
        options = WeightOptions(by_count=bool(weight_by_count))
        if weight_by_count:
            # 老版本是 max(1.0, count)，即「发 9 条 = 9 倍权重」。
            # 太激进了，这里换成等效但温和的写法：每条额外 +1.0。
            options.count_bonus = 1.0

    # ---- 权重 & 指纹 ---------------------------------------------------- #
    # ★ 不能只看 options.active：它只反映「配置里开了什么加权」，
    #   看不到某个人身上的手动倍率 / 补偿倍率。
    #   只调倍率、不勾任何加权时 active 是 False，倍率会被整个忽略 ——
    #   这个 bug 导致「调到 99 倍还是抽不中」。
    有人带倍率 = any(
        float(getattr(e, "weight_mult", 0.0) or 0.0) > 0
        or float(getattr(e, "carry_mult", 0.0) or 0.0) > 0
        for e in entries
    )
    需要加权 = bool(options.active) or 有人带倍率

    if 需要加权 and entries:
        weights = compute_weights(entries, ts, options)
    else:
        weights = [1.0] * len(entries)

    # 指纹也要跟着带上权重，否则复算时对不上
    fp = pool_fingerprint(entries, weights if 需要加权 else None)
    if seed is None:
        seed = secrets.token_hex(8).upper()
    rng = random.Random(f"{seed}|{fp}")

    # ---- 抽人 ----------------------------------------------------------- #
    winners: List[Entry] = []
    if entries:
        picked: List[int] = []

        # ★ 保底（guarantee 模式）：先按保底把人定下来，剩下的名额再按权重抽。
        #   排序用 (miss_rounds, uid) —— 必须完全确定，
        #   否则事后拿种子+指纹复算时，同分的人可能换成另一个，复算就对不上。
        if options.pity_enabled and options.pity_mode == "guarantee":
            need = max(1, int(options.pity_rounds))
            候选 = [(int(getattr(e, "miss_rounds", 0) or 0), e.uid, i)
                    for i, e in enumerate(entries)
                    if int(getattr(e, "miss_rounds", 0) or 0) >= need]
            候选.sort(key=lambda x: (-x[0], x[1]))
            picked = [i for _, _, i in 候选[:count]]

        # 剩下的名额正常抽（排除已经保证入选的）
        剩余 = count - len(picked)
        if 剩余 > 0:
            可抽 = [i for i in range(len(entries)) if i not in picked]
            if 可抽:
                if 需要加权:
                    子权重 = [weights[i] for i in 可抽]
                    子选 = _weighted_pick(子权重, 剩余, rng)
                else:
                    子选 = _pick_indices(len(可抽), 剩余, rng)
                picked.extend(可抽[j] for j in 子选)

        winners = [entries[i] for i in picked]

    # ---- 滚动动画素材 ---------------------------------------------------- #
    # 注意：陪跑序列**故意用等概率**生成。
    # 如果按权重生成，观众能从滚动过程里看出谁更容易中奖，等于提前泄底。
    reel: List[str] = []
    if entries:
        pool_uids = [e.uid for e in entries]
        tail: List[str] = []
        per = max(1, reel_len // max(1, len(winners)))
        for w in winners:
            for _ in range(per):
                tail.append(pool_uids[rng.randrange(len(pool_uids))])
            tail.append(w.uid)
        reel = tail or [pool_uids[rng.randrange(len(pool_uids))]
                        for _ in range(max(0, reel_len))]

    # 本次有几个是走保底进来的（用来在界面上提示「保底触发」）
    _保底 = 0
    if options.pity_enabled and options.pity_mode == "guarantee":
        need = max(1, int(options.pity_rounds))
        _保底 = sum(1 for w in winners
                    if int(getattr(w, "miss_rounds", 0) or 0) >= need)

    return DrawResult(
        winners=winners,
        seed=seed,
        fingerprint=fp,
        pity_hit=_保底,
        pool_size=len(entries),
        ts=ts,
        reel=reel,
        weights=weights,
        options=options,
        weight_by_count=options.by_count,
    )


def verify(entries: Sequence[Entry], count: int, seed: str,
           fingerprint: str, expected: Sequence[str],
           options: Optional[WeightOptions] = None,
           now: Optional[float] = None,
           weight_by_count: bool = False) -> bool:
    """复算校验：用公示的种子 / 名单 / 抽奖时刻，验证结果是否一致。

    等概率模式下 ``now`` 和 ``options`` 都不用传；
    加权模式下**必须**传 —— 因为权重取决于「抽奖那一刻」每个人的观看时长。
    """
    entries = list(entries)

    if options is None:
        options = WeightOptions(by_count=bool(weight_by_count))
        if weight_by_count:
            options.count_bonus = 1.0

    if options.active:
        if now is None:
            return False               # 没给时刻，权重算不出来
        weights = compute_weights(entries, float(now), options)
        if pool_fingerprint(entries, weights) != fingerprint:
            return False
    else:
        weights = [1.0] * len(entries)
        if pool_fingerprint(entries) != fingerprint:
            return False

    rng = random.Random(f"{seed}|{fingerprint}")
    if not entries:
        return not expected
    if options.active:
        picked = _weighted_pick(weights, count, rng)
    else:
        picked = _pick_indices(len(entries), count, rng)
    return [entries[i].uid for i in picked] == list(expected)


# --------------------------------------------------------------------------- #
# 给界面用的权重报告
# --------------------------------------------------------------------------- #


def weight_report(entries: Sequence[Entry], now: float,
                  opt: WeightOptions) -> dict:
    """算一份权重分布摘要，给主播在界面上预览「改成这样会是什么效果」。"""
    if not entries:
        return {"count": 0}
    ws = compute_weights(entries, now, opt)
    pairs = list(zip(entries, ws))
    lo = min(pairs, key=lambda kv: kv[1])
    hi = max(pairs, key=lambda kv: kv[1])
    total = sum(ws)
    return {
        "count": len(entries),
        "min": lo[1], "max": hi[1],
        "avg": total / len(ws),
        "newest": lo[0].uid, "oldest": hi[0].uid,
        "oldest_watched": watch_seconds(hi[0], now),
        "newest_watched": watch_seconds(lo[0], now),
        # 老观众相对新人的中奖概率倍数
        "oldest_times": (hi[1] / lo[1]) if lo[1] > 0 else 0.0,
    }
