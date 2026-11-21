# -*- coding: utf-8 -*-
"""
UID 识别 / 归一化引擎
=====================

这是本程序的心脏。抖音会屏蔽公屏上的纯数字 UID，所以观众会用各种方式把 UID
"拆开"再发出来。本模块负责把千奇百怪的写法还原成一个干净的 UID。

已经覆盖的真实规避写法（全部在 tests/test_normalize.py 里有用例）
------------------------------------------------------------------
    100591822            正常
    100 591 822          半角空格（最常见）
    100　591　822         全角空格 U+3000
    100.591.822          英文句点
    100。591。822         中文句号
    100-591-822          横线 / 减号
    100*591*822          星号
    100_591_822          下划线
    100,591,822          逗号
    100/591/822          斜杠
    100·591·822          间隔号
    100|591|822          竖线
    100\\591\\822         反斜杠
    100 591 822↵100 591 822  多行
    1 0 0 5 9 1 8 2 2    逐位拆开（每个数字之间都有空格）
    １００５９１８２２      全角数字
    𝟏𝟎𝟎𝟓𝟗𝟏𝟖𝟐𝟐         数学粗体数字
    100\u200b591\u200b822 零宽空格（肉眼看不见）
    100⁠591⁠822          词连接符
    100😀591😀822         表情隔断
    100✨591✨822         符号隔断
    ①⓪⓪⑤⑨①⑧②②         圈号数字
    l0059l822            字母 l / I 冒充 1
    1OO591822            字母 O 冒充 0
    UID：100 591 822      带标签前缀
    我 100 591 822 求抽    混杂在正常聊天文本里
    @100 591 822          @ 前缀
    一零零五九一八二二      中文数字（可选功能）

设计要点
--------
* 三段式：NFKC 规范化 → 字符分类（数字 / 软分隔 / 硬断开）→ 跨软分隔合并数字段。
* "软分隔"= 空白、标点、零宽字符、表情符号：允许跨越，但有长度上限，
  避免把 "100 .......... 200" 这种不相干的东西粘在一起。
* "硬断开"= 汉字、字母（未开启易混字母纠正时）、其它文字：一定断开。
* 输出 Candidate 带 score / reason，界面上可以把"原始弹幕 → 识别结果"显示出来，
  主播一眼就能确认没认错。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple

__all__ = [
    "ExtractConfig",
    "Candidate",
    "preprocess",
    "extract",
    "pick",
    "is_uid",
    "COMMON_NUMBERS",
    "LETTER_FIX_MAP",
]


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

class Mode:
    """识别模式。"""

    STRICT = "strict"   # 严格：整条弹幕必须就是一个 UID
    SMART = "smart"     # 智能：优先整条即 UID，否则在文本里找最像 UID 的（默认）
    LOOSE = "loose"     # 宽松：文本里所有符合长度范围的数字都收进来

    ALL = (STRICT, SMART, LOOSE)
    LABELS = {
        STRICT: "严格（整条即UID）",
        SMART: "智能（推荐）",
        LOOSE: "宽松（文本内提取）",
    }
    HINTS = {
        STRICT: "只认「整条弹幕就是一个UID」。最干净。\n"
                "「100 591 822」可以   「主播抽我 100591822」不行",
        SMART: "整条是UID就收；否则只在聊天文本里找明显像UID的\n"
               "（8位以上，或带「UID:」标签）。兼顾干净与不漏。",
        LOOSE: "整条是UID就收；否则把文本里最像UID的那段数字也收下，\n"
               "只要位数在范围内。参与人数更多，但可能混进无关数字。",
    }


@dataclass
class ExtractConfig:
    """UID 识别参数。界面上「识别设置」页对应这里每一项。"""

    mode: str = Mode.SMART

    min_len: int = 6
    """UID 最少位数。低于这个长度一律不认（用来挡掉 666 / 888 / 233 这类弹幕）。"""

    max_len: int = 15
    """UID 最多位数。抖音数字 UID 一般不超过 19 位，默认 15 足够。"""

    letter_fix: bool = False
    """是否把易混字母还原成数字（O→0、l/I→1、Z→2、S→5、B→8、g→9…）。

    观众有时用字母冒充数字来绕过屏蔽。开启后识别率更高，但昵称里的字母
    （例如 "xiaomi"）也可能被拼成数字，所以默认关闭。建议先看「识别测试」再决定。
    """

    chinese_digits: bool = False
    """是否把中文数字按逐位念法还原（一零零 → 100）。默认关闭。"""

    max_gap: int = 4
    """两个数字之间最多允许夹几个**符号**（点、横线、星号…）。超过就断开。"""

    uid_digits: int = 9
    """UID 必须正好多少位。0 = 不限制。

    抖音的数字 ID 固定 9 位，所以默认 9。设成 0 就回到「只按 min_len/max_len」
    的宽松模式。
    """

    uid_min_value: int = 100000000
    uid_max_value: int = 399999999
    """UID 的数值范围（闭区间）。两个都设 0 表示不限制。

    默认 1 亿 ~ 3.99 亿，也就是 **9 位数里首位是 1 / 2 / 3** 的那种。
    观众的 UID 一般就在这个区间里；区间外的基本是别的数字（订单号、手机号、
    游戏战力之类）被误认进来的，挡掉能明显减少噪音。
    """

    max_gap_space: int = 12
    """两个数字之间最多允许夹几个**空白**（空格、制表符、全角空格…）。

    比 ``max_gap`` 宽松得多，因为观众为了对齐数字会一口气敲好几个空格：
    ``280   701     003`` 中间有 5 个空格，``205       531      179`` 有 7 个。
    以前和符号共用一个上限 4，这两种写法都会被拆断丢掉 ——
    观众以为参与了，实际没进池。

    但如果夹的是**符号**（``100 .......... 200``），仍然按 ``max_gap`` 严格处理。
    """

    require_keywords: Tuple[str, ...] = ()
    """弹幕必须包含其中任意一个关键词才算有效（留空 = 不限制）。"""

    block_keywords: Tuple[str, ...] = ()
    """弹幕包含其中任意一个关键词就丢弃（优先级高于 require_keywords）。"""

    block_common_numbers: bool = True
    """是否拦截 666 / 123456 / 5201314 这类常见"非 UID"数字。"""

    strip_label: bool = True
    """是否把 "UID:" / "抖音号:" 这类标签当成噪声去掉（只是影响打分，不影响结果）。"""


# --------------------------------------------------------------------------- #
# 字符表
# --------------------------------------------------------------------------- #

#: 零宽 / 双向控制 / 软连字符 —— 肉眼完全看不见，是最阴的规避手段
ZERO_WIDTH = frozenset(
    "\u200b\u200c\u200d\u200e\u200f\u180e\u2060\u2061\u2062\u2063\u2064"
    "\u2066\u2067\u2068\u2069\u202a\u202b\u202c\u202d\u202e\ufeff\u00ad"
    "\u034f\u17b4\u17b5"
)

#: 排版用的空白（含全角空格、不间断空格）
SPACES = frozenset(" \t\r\n\v\f\u3000\u00a0\u2000\u2001\u2002\u2003\u2004\u2005"
                   "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")

#: 会被当成"软分隔"的可见标点 / 符号
PUNCT = frozenset(
    ".,:;!?/\\|-_~^*#+=<>()[]{}\"'`&%$@"
    "。，、；：！？·—…～《》【】（）〔〕「」『』“”‘’〈〉〖〗"
    "＋－×÷＝／＼｜＜＞［］｛｝＃＆＊＠％＄"
    "•∙‧・°※§¶†‡"
)

#: 常见"观众乱打的数字"，不是 UID
COMMON_NUMBERS = frozenset({
    "000000", "111111", "222222", "333333", "444444", "555555", "666666",
    "777777", "888888", "999999", "123456", "654321", "1111111", "6666666",
    "8888888", "1234567", "12345678", "123456789", "5201314", "1314520",
    "233333", "2333333", "66666666", "88888888", "10086", "10010", "12315",
    "2024", "2025", "2026", "0000000", "9999999", "7777777", "5555555",
})

#: 易混字母 → 数字（仅在 letter_fix=True 时启用）
LETTER_FIX_MAP = {
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "l": "1", "I": "1", "i": "1", "|": "1", "!": "1",
    "Z": "2", "z": "2",
    "A": "4", "a": "4",
    "S": "5", "s": "5",
    "b": "6", "G": "6",
    "T": "7",
    "B": "8",
    "g": "9", "q": "9",
}

#: 圈号数字 ①..⑳ / ⓪
_CIRCLED = {
    "\u24ea": "0",
    **{chr(0x2460 + i): str(i + 1) for i in range(9)},       # ①-⑨
    **{chr(0x2474 + i): str(i + 1) for i in range(9)},       # ⑴-⑼
    **{chr(0x2488 + i): str(i + 1) for i in range(9)},       # ⒈-⒐
    **{chr(0x249c + i): chr(ord("a") + i) for i in range(26)},
    "\u24ff": "0",
}

#: 中文数字（逐位念法）
_CHINESE_DIGITS = {
    "零": "0", "〇": "0", "○": "0", "洞": "0",
    "一": "1", "壹": "1", "幺": "1", "么": "1",
    "二": "2", "贰": "2", "两": "2",
    "三": "3", "叁": "3",
    "四": "4", "肆": "4",
    "五": "5", "伍": "5",
    "六": "6", "陆": "6",
    "七": "7", "柒": "7",
    "八": "8", "捌": "8",
    "九": "9", "玖": "9",
}

#: "UID:" / "抖音号:" 之类的标签。它们不是 UID 的一部分，识别前要"抹掉"。
#: 关键：抹掉时**用等长空格替换**，这样下标不变，打分时还能查到"这里原来有个标签"。
_LABEL_STRIP_RE = re.compile(
    r"(?i)"
    r"(?:(?<![A-Za-z0-9])(?:u\s*id|uid|id)(?![A-Za-z]))"
    r"|(?:抖音号|抖音|号码|编号|账号|帐号|用户名|用户|我的|号)"
)

#: 会被当成"软分隔"的 Unicode 大类
_SOFT_CATEGORIES = frozenset({
    "Cf", "Cc", "Cs", "Co", "Cn",   # 控制 / 格式 / 私有区
    "Zs", "Zl", "Zp",               # 各种空白
    "Mn", "Me",                     # 组合符号 / 变体选择符
    "Sk", "So",                     # 修饰符号 / 表情
    "Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po",  # 标点
    "Sm", "Sc",                     # 数学符号 / 货币
})

#: 会被当成"软分隔"的 Emoji 区段（有些 emoji 的类别是 Lo，靠类别判断不出来）
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x1F300, 0x1F5FF), (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF), (0x1F900, 0x1F9FF), (0x2600, 0x27BF),
    (0x2B00, 0x2BFF), (0x1F1E6, 0x1F1FF), (0xFE00, 0xFE0F),
    (0x1F3FB, 0x1F3FF), (0x2190, 0x21FF), (0x2900, 0x297F),
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3299),
)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _EMOJI_RANGES:
        if lo <= cp <= hi:
            return True
    return False


# --------------------------------------------------------------------------- #
# 三种字符类别
# --------------------------------------------------------------------------- #

DIGIT = "D"
SOFT = "S"
HARD = "H"


def classify(ch: str) -> str:
    """把一个字符分成 数字 / 软分隔 / 硬断开。"""
    if ch in ZERO_WIDTH or ch in SPACES or ch in PUNCT:
        return SOFT
    cat = unicodedata.category(ch)
    if cat == "Nd":
        return DIGIT
    if cat in _SOFT_CATEGORIES:
        return SOFT
    if _is_emoji(ch):
        return SOFT
    return HARD


def _digit_value(ch: str) -> Optional[str]:
    """取字符的 0-9 值；不是数字返回 None。"""
    cat = unicodedata.category(ch)
    if cat == "Nd":
        try:
            return str(unicodedata.decimal(ch))
        except (TypeError, ValueError):
            return None
    if cat == "No":
        # 上标 ⁰¹²³、分数 ½ 之类，NFKC 之后大多已经变成普通数字了
        try:
            val = unicodedata.numeric(ch)
        except (TypeError, ValueError):
            return None
        if float(val).is_integer() and 0 <= val <= 9:
            return str(int(val))
    return None


# --------------------------------------------------------------------------- #
# 预处理
# --------------------------------------------------------------------------- #

def _normalize_chars(text: str, cfg: "ExtractConfig") -> str:
    """字符级规范化（不含易混字母纠正，也不抹标签）。"""
    # 1) NFKC：全角数字 ０-９ → 0-9，全角空格 → 普通空格，数学粗体数字 → 0-9
    out = unicodedata.normalize("NFKC", text)

    # 2) 零宽字符直接删掉（它们是"隐形分隔"，删掉后数字自然连起来）
    if any(c in ZERO_WIDTH for c in out):
        out = "".join(c for c in out if c not in ZERO_WIDTH)

    # 3) 圈号数字等
    if any(c in _CIRCLED for c in out):
        out = "".join(_CIRCLED.get(c, c) for c in out)

    # 4) 中文数字
    if cfg.chinese_digits:
        out = "".join(_CHINESE_DIGITS.get(c, c) for c in out)

    return out


def _blank_labels(s: str) -> Tuple[str, List[bool]]:
    """把 "UID:" / "抖音号" 之类的标签替换成等长空格。

    返回 (新字符串, 每个位置是否为标签字符)。等长替换保证下标不漂移。
    """
    flags = [False] * len(s)
    chars = list(s)
    hit = False
    for m in _LABEL_STRIP_RE.finditer(s):
        hit = True
        for i in range(m.start(), m.end()):
            chars[i] = " "
            flags[i] = True
    return ("".join(chars) if hit else s), flags


def _prepare(text: str, cfg: "ExtractConfig") -> Tuple[str, List[bool]]:
    """完整的预处理流水线，返回 (可扫描文本, 标签位置标记)。"""
    s = _normalize_chars(text, cfg)
    flags: List[bool] = [False] * len(s)

    # 抹标签必须放在"易混字母纠正"之前 —— 否则 UID 里的 I/D 会先变成 1/0，
    # 标签就认不出来了，还会跟真正的 UID 粘在一起（"UID:100591824" → "10100591824"）。
    if cfg.strip_label:
        s, flags = _blank_labels(s)

    if cfg.letter_fix:
        s = "".join(LETTER_FIX_MAP.get(c, c) for c in s)

    return s, flags


def preprocess(text: str, cfg: Optional[ExtractConfig] = None) -> str:
    """把原始弹幕规范成"可扫描"的形式。

    做了这些事：NFKC 规范化（全角→半角、数学字母→普通数字）、圈号数字转普通数字、
    去掉零宽字符、抹掉 UID 标签、可选地把易混字母和中文数字换成数字。
    """
    if not text:
        return ""
    return _prepare(text, cfg or ExtractConfig())[0]


# --------------------------------------------------------------------------- #
# 候选
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Candidate:
    """一个被识别出来的 UID 候选。"""

    uid: str
    """归一化之后的纯数字 UID。"""

    raw: str
    """在弹幕里对应的原始片段（方便主播核对）。"""

    score: int = 0
    """可信度打分，越高越像真 UID。"""

    reason: str = ""
    """打分理由，界面上直接显示给主播看。"""

    whole: bool = False
    """是否"整条弹幕就是一个 UID"。"""

    index: int = 0
    """在弹幕文本中出现的位置。"""

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"<{self.uid} score={self.score} {self.reason}>"


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #

def _scan_runs(proc: str, cfg: ExtractConfig,
               label_flags: Optional[List[bool]] = None
               ) -> List[Tuple[str, int, int]]:
    """扫描出所有"数字段"，返回 (uid, 起始下标, 结束下标)。

    数字段 = 一串数字，中间可以夹杂若干个软分隔字符。**空白和符号分开计数**：

    * **空白**（空格、制表符、全角空格…）上限是 ``cfg.max_gap_space``（默认 12）。
      观众为了对齐数字，经常一口气敲好几个空格 —— 真实直播里出现过
      ``"280   701     003"``（中间 5 个空格）和 ``"205       531      179"``
      （中间 7 个空格）。以前和符号共用一个上限 4，这两条都会被拆断丢掉，
      观众以为自己参与了，其实没进池。
    * **符号**（点、横线、星号…）上限仍是 ``cfg.max_gap``（默认 4）。
      一串点号往往是故意的分隔符，限制严一点不会误伤。

    ``label_flags`` 标出**被抹掉的标签位置**（"UID:" / "抖音号" 之类）。

    这些位置必须当成**硬断开**，原因是个很隐蔽的坑：
    抹标签用的是「等长空格」，而空格是**软分隔**、允许被跨越，
    于是 ``1号100591822`` 会被粘成 ``1100591822`` —— 一个凭空多出来的号码，
    还会被静默地加进参与池。标签两侧的数字在语义上本来就属于不同的东西，
    不能合并。所以这里对标签位置直接 flush。
    """
    runs: List[Tuple[str, int, int]] = []
    buf: List[str] = []
    start = -1
    last_digit_idx = -1
    sym_gap = 0        # 已经跨过的「符号」个数
    space_gap = 0      # 已经跨过的「空白」个数

    def flush() -> None:
        nonlocal buf, start, last_digit_idx, sym_gap, space_gap
        if buf:
            runs.append(("".join(buf), start, last_digit_idx + 1))
        buf = []
        start = -1
        last_digit_idx = -1
        sym_gap = 0
        space_gap = 0

    for i, ch in enumerate(proc):
        kind = classify(ch)
        if kind == DIGIT:
            val = _digit_value(ch)
            if val is None:
                flush()
                continue
            if start < 0:
                start = i
            buf.append(val)
            last_digit_idx = i
            sym_gap = 0
            space_gap = 0
        elif kind == SOFT:
            # 标签抹掉后留下的空格：当硬断开，不许跨越（见上面的说明）
            if label_flags is not None and label_flags[i]:
                flush()
            elif buf:
                if ch in SPACES:
                    space_gap += 1
                    if space_gap > cfg.max_gap_space:
                        flush()
                else:
                    sym_gap += 1
                    if sym_gap > cfg.max_gap:
                        flush()
        else:  # HARD
            flush()
    flush()
    return runs


def uid_in_range(uid: str, cfg: "ExtractConfig") -> bool:
    """UID 的位数和数值范围对不对。

    默认要求 **9 位、1 亿 ~ 3.99 亿**（也就是首位是 1/2/3 的 9 位数）——
    抖音的数字 ID 就在这个区间。区间外的数字（订单号、手机号、
    游戏战力之类）挡掉能明显减少噪音。

    两个都设 0 就完全不限制。
    """
    if not uid or not uid.isdigit():
        return False
    if cfg.uid_digits and len(uid) != cfg.uid_digits:
        return False
    lo, hi = int(cfg.uid_min_value or 0), int(cfg.uid_max_value or 0)
    if lo or hi:
        try:
            v = int(uid)
        except ValueError:
            return False
        if lo and v < lo:
            return False
        if hi and v > hi:
            return False
    return True


def _looks_like_junk(uid: str) -> Optional[str]:
    """判断这个数字是不是"一看就不是 UID"的。返回理由，不像则返回 None。"""
    if uid in COMMON_NUMBERS:
        return "常见数字"

    n = len(uid)

    # 全部同一位数字
    if len(set(uid)) == 1:
        return "全是同一位数字"

    # 123456 / 987654 这种连续递增递减
    if n >= 4:
        deltas = {int(uid[i + 1]) - int(uid[i]) for i in range(n - 1)}
        if deltas in ({1}, {-1}):
            return "连续递增/递减"

    # ABABAB / ABCABC 这种重复块
    for block in (1, 2, 3):
        if n >= block * 2 and n % block == 0:
            if len(set(uid[i:i + block] for i in range(0, n, block))) == 1:
                return "重复片段"

    return None


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def extract(text: str, cfg: Optional[ExtractConfig] = None) -> List[Candidate]:
    """从一条弹幕里提取 UID 候选，按可信度从高到低排序。

    返回空列表表示这条弹幕里没有 UID。
    """
    cfg = cfg or ExtractConfig()
    if not text:
        return []

    # --- 关键词过滤 ------------------------------------------------------ #
    if cfg.block_keywords:
        low = text.lower()
        for kw in cfg.block_keywords:
            if kw and kw.lower() in low:
                return []
    if cfg.require_keywords:
        low = text.lower()
        if not any(kw and kw.lower() in low for kw in cfg.require_keywords):
            return []

    proc, label_flags = _prepare(text, cfg)
    if not proc:
        return []

    runs = _scan_runs(proc, cfg, label_flags)
    if not runs:
        return []

    # --- UID 长度 / 数值范围过滤 ---------------------------------------- #
    # 放在打分之前：明显不是 UID 的（位数不对、超出数值区间）直接扔掉，
    # 不给它们参与打分的机会，既快又不会误伤真正的那一个。
    runs = [r for r in runs if uid_in_range(r[0], cfg)]
    if not runs:
        return []

    # --- 判断"整条即 UID" ------------------------------------------------ #
    whole_uid: Optional[str] = None
    if len(runs) == 1:
        uid, s, e = runs[0]
        # 数字段之外只剩软分隔 → 整条就是它
        before = proc[:s]
        after = proc[e:]
        if all(classify(c) == SOFT for c in before) and \
           all(classify(c) == SOFT for c in after):
            if cfg.min_len <= len(uid) <= cfg.max_len:
                whole_uid = uid

    cands: List[Candidate] = []

    def add(uid: str, raw: str, idx: int, whole: bool) -> None:
        if not (cfg.min_len <= len(uid) <= cfg.max_len):
            return
        score, reason = _score(uid, raw, proc, idx, whole, cfg, label_flags)
        if score <= 0:
            return
        cands.append(Candidate(uid=uid, raw=raw, score=score,
                               reason=reason, whole=whole, index=idx))

    if whole_uid is not None:
        uid, s, e = runs[0]
        add(uid, proc.strip(), s, True)

    for uid, s, e in runs:
        if whole_uid is not None and uid == whole_uid:
            continue
        add(uid, proc[s:e], s, False)

    # --- 去重（同一个 UID 只留分最高的） -------------------------------- #
    best: dict = {}
    for c in cands:
        old = best.get(c.uid)
        if old is None or c.score > old.score:
            best[c.uid] = c
    out = sorted(best.values(), key=lambda c: (-c.score, c.index))

    # --- 按模式筛选 ------------------------------------------------------ #
    if cfg.mode == Mode.STRICT:
        out = [c for c in out if c.whole]
    elif cfg.mode == Mode.SMART:
        if whole_uid is not None:
            out = [c for c in out if c.whole]
        else:
            out = [c for c in out if _is_strong(c, cfg)][:1]
    elif cfg.mode == Mode.LOOSE:
        pass
    else:
        raise ValueError(f"未知识别模式: {cfg.mode!r}")

    return out


def _is_strong(c: Candidate, cfg: ExtractConfig) -> bool:
    """智能模式下，非「整条即UID」的候选要够"像 UID"才收。

    判据：位数够长（至少 8 位），或者弹幕里带了 "UID:" 之类的标签。
    这样 "主播抽我 100 591 822" 能收，"我 666 了你" 收不进来。
    """
    if len(c.uid) >= max(cfg.min_len, 8):
        return True
    return "带UID标签" in c.reason


def _score(uid: str, raw: str, proc: str, idx: int,
           whole: bool, cfg: ExtractConfig,
           label_flags: Optional[List[bool]] = None) -> Tuple[int, str]:
    """给一个候选打分。返回 (分数, 理由)。分数 <= 0 表示直接淘汰。"""
    if len(uid) < cfg.min_len:
        return 0, "位数不足"
    if len(uid) > cfg.max_len:
        return 0, "位数过长"

    if cfg.block_common_numbers:
        junk = _looks_like_junk(uid)
        if junk:
            return 0, f"疑似{junk}"

    score = 0
    reasons: List[str] = []

    score += len(uid) * 2
    reasons.append(f"{len(uid)}位")

    if whole:
        score += 60
        reasons.append("整条即UID")
    else:
        score += 10

    # 数字越长越像真 UID（抖音数字 UID 通常 8 位以上）
    if len(uid) >= 8:
        score += 15
        reasons.append("长度像UID")

    # 前面带 "UID:" / "抖音号:" 之类标签（标签已被抹成空格，靠标记回查）
    if cfg.strip_label and label_flags:
        lo = max(0, idx - 16)
        if any(label_flags[lo:idx]):
            score += 25
            reasons.append("带UID标签")

    # 被硬断开包围（说明是独立的一段，不是长单词的一部分）
    left_hard = idx > 0 and classify(proc[idx - 1]) == HARD
    right_idx = idx + len(raw)
    right_hard = right_idx < len(proc) and classify(proc[right_idx]) == HARD
    if not left_hard and not right_hard:
        score += 8

    return score, "·".join(reasons)


def pick(text: str, cfg: Optional[ExtractConfig] = None) -> Optional[Candidate]:
    """只取最好的那一个候选；没有就返回 None。"""
    got = extract(text, cfg)
    return got[0] if got else None


def is_uid(text: str, cfg: Optional[ExtractConfig] = None) -> bool:
    """这条弹幕里是否含有 UID。"""
    return bool(extract(text, cfg))


# --------------------------------------------------------------------------- #
# 便捷工具
# --------------------------------------------------------------------------- #

def normalize_uid(uid: str) -> str:
    """把一个已知的 UID 字符串归一化（用于手工录入 / 去重）。"""
    cfg = ExtractConfig(min_len=1, max_len=64, mode=Mode.LOOSE,
                        block_common_numbers=False, letter_fix=False)
    proc = preprocess(uid, cfg)
    return "".join(filter(str.isdigit, proc))


def mask_uid(uid: str, keep_head: int = 3, keep_tail: int = 3) -> str:
    """打码显示，例如 100591822 → 100***822。"""
    if len(uid) <= keep_head + keep_tail:
        return uid
    return uid[:keep_head] + "*" * (len(uid) - keep_head - keep_tail) + uid[-keep_tail:]
