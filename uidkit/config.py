# -*- coding: utf-8 -*-
"""程序路径与配置文件读写。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List

from .normalize import ExtractConfig, Mode

APP_NAME = "抖音UID抽号器"


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #

def install_dir() -> str:
    """程序所在目录（打包成 exe 后是 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


_DATA_DIR_CACHE: List[str] = []


def data_dir() -> str:
    """数据目录：优先放在程序目录下的「数据」文件夹，不可写则退回用户目录。"""
    if _DATA_DIR_CACHE:
        return _DATA_DIR_CACHE[0]

    local = os.path.join(install_dir(), "数据")
    if _writable(local):
        _DATA_DIR_CACHE.append(local)
        return local

    fallback = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME
    )
    os.makedirs(fallback, exist_ok=True)
    _DATA_DIR_CACHE.append(fallback)
    return fallback


def data_path(*parts: str) -> str:
    p = os.path.join(data_dir(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def asset_dir() -> str:
    """静态资源目录（打包后会被解到 _MEIPASS）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "assets")
    return os.path.join(install_dir(), "assets")


def bridge_dir() -> str:
    """抖音直连桥接脚本所在目录（跟 exe / main.py 同层的 tools 下）。"""
    return os.path.join(install_dir(), "tools", "抖音弹幕直连")


def find_python_for_scripts() -> str | None:
    """找一个能跑 .py 的 Python 解释器，给抖音直连桥接用。

    优先级：
      1. 系统里装的 Python（最"正常"的执行环境，兼容性最好）；
      2. 随程序分发的便携 Python（tools\\python\\python.exe）—— 没装 Python 时的兜底；
      3. 源码方式运行时，当前解释器。

    注意两点：
      * 打包成 exe 之后**绝对不能用 sys.executable**，它指向 exe 自己，
        拿它跑脚本等于又开一个抽号器；
      * 便携版**不含 tkinter**，只能跑桥接这种纯标准库脚本，
        所以它不能拿来启动本程序本体（源码方式仍需要完整 Python）。
    """
    import glob
    import shutil

    if not getattr(sys, "frozen", False):
        return sys.executable

    for name in ("python.exe", "py.exe"):
        found = shutil.which(name)
        if found:
            return found

    roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python"),
        "C:\\",
        os.path.expandvars(r"%ProgramFiles%"),
        os.path.expandvars(r"%ProgramFiles(x86)%"),
    ]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for exe in ("python.exe", "pythonw.exe"):
            hits = glob.glob(os.path.join(root, "Python3*", exe))
            if hits:
                return sorted(hits)[-1]

    bundled = os.path.join(install_dir(), "tools", "python", "python.exe")
    if os.path.isfile(bundled):
        return bundled
    return None


# --------------------------------------------------------------------------- #
# 展示窗配置
# --------------------------------------------------------------------------- #

class BgMode:
    CHROMA = "chroma"          # 纯色背景（给直播伴侣「绿幕抠图」用）—— 推荐
    TRANSPARENT = "transparent"  # 真透明（自己桌面上看穿；采集时不可靠）
    IMAGE = "image"            # 自定义背景图

    ALL = (CHROMA, TRANSPARENT, IMAGE)
    LABELS = {
        CHROMA: "纯色背景（推荐 · 配合直播伴侣绿幕抠图）",
        TRANSPARENT: "真透明（桌面看穿；直播伴侣采集不可靠）",
        IMAGE: "自定义图片背景",
    }
    HINTS = {
        CHROMA: "窗口铺满一个纯色，直播伴侣里用「绿幕抠图」吸管点一下这个颜色就透明了。\n"
                "实测最稳的方案。注意：文字别用接近底色的颜色。",
        TRANSPARENT: "窗口本身透明，自己屏幕上直接看穿。\n"
                     "但窗口采集（BitBlt/WGC）拿不到 alpha，直播伴侣里多半会变成黑块或色块，\n"
                     "只有配合抠像才可用 —— 那还不如直接用「纯色背景」。",
        IMAGE: "用一张图片当背景（相当于带底图的叠加层）。",
    }


@dataclass
class OverlayConfig:
    """展示窗口的外观。"""

    bg_mode: str = BgMode.CHROMA
    bg_color: str = "#00AA00"
    """纯色背景色。默认纯绿 —— 直播伴侣的「绿幕抠图」有专门的「去泛绿」参数，
    对绿色处理最好。也可以用右侧的预设按钮换成品红/蓝幕。"""

    image_path: str = ""

    font_family: str = "Microsoft YaHei UI"
    font_weight: str = "bold"

    title_text: str = "UID 抽号"
    title_size: int = 40
    title_color: str = "#FFD54A"
    show_title: bool = True

    uid_size: int = 150
    """中奖 UID 的字号。"""

    uid_color: str = "#FFFFFF"
    outline_color: str = "#101010"
    outline_width: int = 5
    """文字描边宽度（像素），0 = 不描边。有描边时在任何背景上都看得清。"""

    reel_size: int = 110
    """滚动过程中的字号。"""

    show_count: bool = True
    count_color: str = "#7FE7FF"
    count_size: int = 60
    """「参与人数」那行的字号。

    原来 30。展示窗里删掉中奖名单之后，下半部分空出来了，
    把人数放大到 60 更醒目 —— 观众一眼就能看到现在有多少人参与。
    """
    count_prefix: str = "参与人数"

    show_feed: bool = False
    feed_limit: int = 8
    feed_size: int = 20
    feed_color: str = "#CFCFCF"

    idle_text: str = "等待抽取…"
    idle_color: str = "#9AA0A6"
    idle_size: int = 56

    geom: str = ""
    """窗口位置，形如 ``1100x470+1120+40``。

    **留空表示「自动安排」**：程序启动时会在创建窗口之前算好一个
    不挡住控制台、也不和名单窗重合的位置，然后立刻存进配置里。

    以前这里写死了 ``+880+80``，而名单窗也写死了同一个坐标，
    结果全新安装启动时两个窗口会**叠在一起两秒**才跳开 ——
    所以改成留空由程序自己算。
    """
    always_on_top: bool = False
    """是否总在最前。

    默认**关**：窗口捕获是按窗口内容抓的，跟它有没有被别的窗口盖住无关，
    所以置顶对直播毫无帮助，反而会一直挡着你操作别的软件。
    想让它一直在上面，在「展示窗口」页勾上「总在最前」即可（勾了立刻生效）。
    """
    locked: bool = False
    """锁定后不可拖动（防止直播中误碰）。"""

    window_title: str = "★ UID 抽号展示窗口 ★"
    """窗口标题。直播伴侣的「窗口捕获」下拉列表里就是靠它认人的。"""

    borderless: bool = True
    """无边框（推荐）：采集时不会把标题栏也拍进去。
    如果直播伴侣的窗口列表里找不到本窗口，把它关掉就会出现标题栏。"""

    def font(self, size: int) -> tuple:
        return (self.font_family, size, self.font_weight)


@dataclass
class BoardConfig:
    """参与名单窗（给观众看「谁参与成功了」）。

    背景色 / 字体 / 描边沿用 ``OverlayConfig``，这样两路素材只需抠同一种颜色；
    这里只放自己独有的位置和排版。

    显示方式：**B 站弹幕式**——有人参与成功时，一条「UID + 参与成功」
    从右往左飘过，和视频弹幕一样。
    """

    enabled: bool = True
    geom: str = "1600x900"
    """窗口位置。**留空表示「自动安排」**，和展示窗一样。

    名单窗固定 16:9 的 1600x900，只在这里提供默认值；
    具体位置由 layout 按屏幕算出来。
    """
    window_title: str = "★ 参与名单窗口 ★"
    borderless: bool = True
    always_on_top: bool = False
    locked: bool = False

    title_text: str = "参与名单"
    title_size: int = 34
    title_color: str = "#FFD54A"
    show_title: bool = False
    """是否显示顶部标题。16:9 弹幕模式下默认关闭，纯弹幕画面更干净。"""

    # ---- 弹幕式显示参数 ----
    uid_size: int = 44
    """弹幕文字的字号。"""
    uid_color: str = "#FFFFFF"
    """弹幕主文字颜色。"""
    suffix: str = "参与成功"
    """显示在 UID 后面的文字。"""
    suffix_color: str = "#7FE7FF"
    """后缀文字的颜色。"""
    show_suffix: bool = True
    """是否在 UID 后面显示「参与成功」。"""

    track_count: int = 8
    """同时最多几条弹幕轨道（垂直方向错开，避免重叠）。"""
    track_gap: int = 58
    """相邻两条轨道的垂直间距（像素）。"""
    speed: int = 220
    """弹幕滚动速度（像素/秒）。"""
    stay_ms: int = 9000
    """每条弹幕从出现到完全滚出窗口的时长（毫秒）。"""

    footer_size: int = 22
    footer_color: str = "#7FE7FF"
    show_footer: bool = False
    """是否在底部显示「已参与 N 人」计数。16:9 弹幕模式下默认关闭。"""


@dataclass
class VoteConfig:
    """投票捕捉窗口：让观众给刚抽中的 UID 评好坏。"""

    enabled: bool = True
    geom: str = ""
    """窗口位置。留空由程序自动安排。"""
    window_title: str = "★ 观众投票窗口 ★"
    borderless: bool = True
    always_on_top: bool = False
    locked: bool = False

    auto_show_on_draw: bool = True
    """抽奖后自动把投票窗显示出来（只是显示窗口，**不会**自动开始投票）。

    这样抽中的 UID 会立刻出现在捕捉窗口里，主播点一下「开始投票」即可。
    """

    auto_open: bool = False
    """抽奖后是否自动开始一轮新投票。

    ★ 默认**关闭**：抽到人之后不会自动进入投票，
      必须由主播点「开始投票」才开放，观众才能投。
    """

    one_vote: bool = True
    """一人一票。开启后同一个人再发别的档位算**改票**，不累加。"""

    vote_seconds: float = 10.0
    """投票持续多少秒后自动结束。0 = 不自动结束。

    默认 10 秒：点「开始投票」后倒数 10 秒，到点自动关票。
    """

    # ---- 外观 ----
    title_text: str = "观众投票"
    title_size: int = 30
    title_color: str = "#FFD54A"
    show_title: bool = True

    target_size: int = 24
    target_color: str = "#FFFFFF"

    style_size: int = 26
    style_color: str = "#E8EEFF"
    count_size: int = 30
    count_color: str = "#FFFFFF"
    pct_size: int = 18
    pct_color: str = "#9FB0CC"

    footer_size: int = 20
    footer_color: str = "#7FE7FF"
    show_footer: bool = True

    idle_text: str = "等待主播开启投票"


@dataclass
class RankConfig:
    """在线榜窗口（给观众看「在线前五」+ 贡献分）。

    背景色 / 字体 / 描边沿用 ``OverlayConfig``，跟展示窗、名单窗共用一套底色，
    这样三路素材只需抠同一种颜色。
    """

    enabled: bool = False
    """是否显示榜窗口。默认关 —— 榜数据来自抖音直连，没开直连时窗口是空的。"""

    geom: str = ""
    """窗口位置。留空由程序自动安排。"""

    window_title: str = "★ 在线榜窗口 ★"
    borderless: bool = True
    always_on_top: bool = False
    locked: bool = False

    title_text: str = "在线榜 TOP5"
    title_size: int = 34
    title_color: str = "#FFD54A"
    show_title: bool = True

    show_total: bool = True
    """是否在标题下方显示「在线 N 人」总人数行。"""

    rank_size: int = 28
    """每条榜的行字号。"""
    rank_color: str = "#FFFFFF"
    rank_num_color: str = "#7FE7FF"
    """排名数字（1~5）的颜色。"""

    score_color: str = "#FFD54A"
    """贡献分（礼物金额）的颜色。"""

    show_score: bool = True
    """是否显示贡献分。用户要求：只要一个数字，不要单位。"""

    max_rows: int = 5
    """最多显示几行（默认 5）。"""

    footer_size: int = 20
    footer_color: str = "#9FB0CC"
    show_footer: bool = False


# --------------------------------------------------------------------------- #
# 数据源配置
# --------------------------------------------------------------------------- #

@dataclass
class SourceConfig:
    """弹幕来源。四个通道可以同时开。"""

    http_enabled: bool = True
    http_host: str = "127.0.0.1"
    http_port: int = 8787
    http_token: str = ""
    """可选。填了就要求推送方带 ?token=xxx，防止局域网里别人乱推。"""

    file_enabled: bool = False
    file_path: str = ""
    file_encoding: str = "utf-8"
    file_format: str = "auto"
    """auto | text | nick_tab_text | jsonl"""

    clipboard_enabled: bool = False
    clipboard_interval_ms: int = 400

    demo_enabled: bool = False
    demo_interval_ms: int = 900

    sandbox: bool = False
    """沙箱/演练模式：不写日志、不落盘，随便试。"""


# --------------------------------------------------------------------------- #
# 总配置
# --------------------------------------------------------------------------- #

@dataclass
class AppConfig:
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    board: BoardConfig = field(default_factory=BoardConfig)
    source: SourceConfig = field(default_factory=SourceConfig)

    draw_count: int = 1
    no_repeat_winner: bool = True
    """已中奖的不再参与下一轮。"""

    weighted_by_count: bool = False
    """按发言次数加权（发得多中奖概率高）。默认关闭。"""

    # ---- 观看时长加权（新增）-------------------------------------------- #

    weight_by_watch: bool = True
    """★ 按「观看时长」加权：在池子里待得越久，中奖概率越高。

    「看了多久」用该 UID **第一次出现在公屏上的时间**到抽奖那一刻的间隔来衡量。
    默认**开启**：这是「随机 UID 看号」直播间的常见玩法 ——
    观众挂着看久了应该有点回报，否则来晚一秒的人和中途进来的人没区别。

    想回到纯等概率（每个人一样），把这个关掉即可。
    """

    watch_full_minutes: float = 30.0
    """看满多少分钟拿满时长加成。"""

    watch_bonus: float = 2.0
    """时长加成的倍数。2.0 表示看满的人是刚来的人的 3 倍权重。

    不建议调太高：超过 5 倍的话新观众基本没机会，公屏会冷下来。
    """

    watch_memory_hours: float = 12.0
    """观看时长记录保留多少小时。

    清空参与池**不会**清掉观看时长，所以需要一个过期时间 ——
    不然昨天看了 3 小时的人今天一来就是满权重，对新观众不公平。
    默认 12 小时，足够覆盖一整场直播。设 0 = 永不过期。
    """

    # ---- 观看时长权重是否无限叠加 ----
    weight_watch_unlimited: bool = False
    """观看时长权重要不要**无限叠加**。

    关掉：看满 watch_full_minutes 分钟就吃满加成，不再涨（旧行为）。
    打开：一直线性上涨，看越久权重越高，**不封顶**。
    """

    # ---- 参与池定时自动清空 ----
    pool_auto_clear: bool = True
    """是否每隔一段时间自动清空参与池（中奖记录和观看时长保留）。"""

    pool_auto_clear_minutes: float = 5.0
    """多少分钟清一次。默认 5 分钟。"""

    # ---- 投票窗口 ----
    vote: VoteConfig = field(default_factory=VoteConfig)

    # ---- 在线榜窗口 ----
    rank: RankConfig = field(default_factory=RankConfig)

    # ---- 下播 / 跨场次补偿 ----
    carry_enabled: bool = True
    """上一场没中的人，这一场要不要给补偿加权。"""

    carry_mult: float = 2.0
    """补偿倍率。2.0 = 中奖率翻倍。"""

    carry_expire_days: float = 7.0
    """补偿记录保留几天。

    太老的记录没意义（一周前没中的人这次未必还在），
    过期自动丢弃。设 0 = 永不过期。
    """

    # ---- 保底 ----
    pity_enabled: bool = False
    """是否开启保底。"""

    pity_rounds: int = 5
    """连续多少轮没中就触发保底。"""

    pity_mode: str = "guarantee"
    """保底方式：guarantee=必中，multiply=权重递增。"""

    pity_max_mult: float = 64.0
    """multiply 模式下最多翻到几倍。"""

    weight_cap: float = 0.0
    """权重上限，0 = 不限制。设了可以防止极端老观众权重过大。"""

    # ---- 中奖 UID 复制 --------------------------------------------------- #

    copy_winner_after_draw: bool = True
    """抽完是否**自动**把中奖 UID 放进剪贴板。

    默认关闭：不打招呼就占用剪贴板不太礼貌（主播可能正在复制别的东西）。
    但连续抽号时开着很省事 —— 抽完直接 Ctrl+V 就能去搜人。
    """

    copy_with_nick: bool = False
    """复制时带不带昵称。关 = 只复制 9 位纯数字；开 = 复制成「昵称(UID)」。

    搜索/私信一般只要纯数字，所以默认关。
    """

    animation_ms: int = 4500
    """动画总时长（毫秒）。关掉动画时这个值不起作用。"""
    save_danmaku_log: bool = True
    auto_save_ms: int = 15000

    # ---- 读写 ---------------------------------------------------------- #

    @staticmethod
    def path() -> str:
        return os.path.join(data_dir(), "config.json")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AppConfig":
        import dataclasses

        cfg = cls()
        # ★ 自动识别所有嵌套 dataclass 字段，而不是硬编码一份名单。
        #   以前名单里只有 extract/overlay/board/source，
        #   后来加的 vote 没进名单，结果配置文件里的 "vote" 被当成
        #   普通值直接塞成 dict，访问 .geom 就 AttributeError。
        嵌套 = set()
        for f in fields(cls):
            默认值 = getattr(cfg, f.name, None)
            if dataclasses.is_dataclass(默认值):
                嵌套.add(f.name)
                setattr(cfg, f.name,
                        _fill(type(默认值), d.get(f.name) or {}, 默认值))

        for f in fields(cls):
            if f.name in 嵌套:
                continue
            if f.name in d and d[f.name] is not None:
                try:
                    setattr(cfg, f.name, d[f.name])
                except Exception:
                    pass
        # 元组字段从 JSON 读回来是 list
        cfg.extract.require_keywords = tuple(cfg.extract.require_keywords or ())
        cfg.extract.block_keywords = tuple(cfg.extract.block_keywords or ())
        if cfg.extract.mode not in Mode.ALL:
            cfg.extract.mode = Mode.SMART
        if cfg.overlay.bg_mode not in BgMode.ALL:
            cfg.overlay.bg_mode = BgMode.CHROMA
        return cfg

    @classmethod
    def load(cls) -> "AppConfig":
        p = cls.path()
        if not os.path.exists(p):
            return cls()
        try:
            with open(p, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except Exception:
            return cls()

    def save(self) -> None:
        p = self.path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)


def _fill(cls: type, d: Dict[str, Any], default: Any) -> Any:
    """把 JSON 字典填进 dataclass。

    ★ 嵌套的 dataclass 字段（overlay / board / source / vote …）必须
      **递归构造成对象**，不能把 dict 直接塞进去。
      直接塞的话，配置文件里少写几个键就会得到一个 dict，
      后面访问 .geom 之类的属性就会 AttributeError。
      （以前能正常工作纯属巧合：默认值兜住了类型。）
    """
    import dataclasses

    obj = cls()
    for f in fields(cls):
        if f.name not in d or d[f.name] is None:
            continue
        值 = d[f.name]
        默认值 = getattr(default, f.name, None)

        # 嵌套 dataclass：递归填充
        if dataclasses.is_dataclass(默认值) and isinstance(值, dict):
            try:
                setattr(obj, f.name, _fill(type(默认值), 值, 默认值))
                continue
            except Exception:
                setattr(obj, f.name, 默认值)
                continue

        # 类型对不上就用默认值（比如该是数字却写成了字符串）
        try:
            setattr(obj, f.name, 值)
        except Exception:
            setattr(obj, f.name, 默认值)
    return obj
