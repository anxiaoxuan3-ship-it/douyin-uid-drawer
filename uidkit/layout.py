# -*- coding: utf-8 -*-
"""窗口位置安排（展示窗 / 参与名单窗 / 控制台）。

单独放一个模块的原因：``main.py`` 和 ``console.py`` 都要用它，
而 ``console.py`` 不能反过来 import ``main.py``（会循环导入）。

设计要点
--------
* 位置必须在**创建窗口之前**算好。窗口一建出来就按配置里的坐标显示，
  之后再改坐标，用户会看到「两个窗口先叠在一起、一两秒后才跳开」。
* 用户自己摆好的位置要**保留**，不能每次启动都被程序覆盖 ——
  所以只在「没位置 / 位置不合法 / 两个窗口叠在同一个点」时才重新安排。
* 换过显示器或改过分辨率之后，旧坐标可能跑到屏幕外面，
  窗口就等于「看不见」了，这种也要重新安排。
"""

from __future__ import annotations

import re
from typing import Tuple

#: 位置字符串：``宽x高+左+上``，例如 ``1100x470+1120+40``
_GEOM_RE = re.compile(r"^(\d{2,5})x(\d{2,5})\+(-?\d{1,6})\+(-?\d{1,6})$")

# 控制台的位置和大小（main() 里就是这么摆的）
CONSOLE = (40, 40, 1060, 760)

# 两路捕获窗口「想要」的尺寸
OVERLAY_SIZE = (1100, 470)
BOARD_WIDTH = 1600
BOARD_HEIGHT_MAX = 900


def parse_geom(geom: str) -> Tuple[int, int, int, int] | None:
    """把位置字符串拆成 (宽, 高, 左, 上)；格式不对返回 None。"""
    m = _GEOM_RE.match((geom or "").strip())
    if not m:
        return None
    return tuple(int(v) for v in m.groups())  # type: ignore[return-value]


def geom_ok(geom: str, sw: int, sh: int) -> bool:
    """存下来的位置还能不能用。

    检查三件事：格式对不对、尺寸是不是小得离谱、
    以及**是不是还落在当前屏幕里**（换显示器后旧坐标可能跑到屏幕外）。
    """
    parsed = parse_geom(geom)
    if parsed is None:
        return False
    w, h, x, y = parsed
    if w < 240 or h < 120:
        return False
    # 至少要有一块露在屏幕里，否则用户根本没法把它拖回来
    return (x + w > 80 and y + h > 40 and x < sw - 80 and y < sh - 40)


def same_origin(a: str, b: str) -> bool:
    """两个位置的左上角是不是同一个点（宽高可以不同）。

    旧版本展示窗和名单窗的默认坐标都是 ``+880+80``，
    就是靠这个把它们认出来的。
    """
    pa, pb = parse_geom(a), parse_geom(b)
    if pa is None or pb is None:
        return False
    return (pa[2], pa[3]) == (pb[2], pb[3])


def suggest(sw: int, sh: int) -> Tuple[str, str]:
    """按当前屏幕算一套「标准摆放」，返回 (展示窗位置, 名单窗位置)。

    名单窗是 16:9 的 1600x900，属于大窗口，独立于展示窗单独摆放：
    优先居中靠上（直播时主播会把展示窗抓给绿幕，名单窗另开一路），
    屏幕装不下就等比缩小。
    """
    cx, cy, cw, ch = CONSOLE
    ow, oh = OVERLAY_SIZE
    bw = BOARD_WIDTH

    # ---- 展示窗：优先排控制台右边 ----
    col_x = cx + cw + 20
    if col_x + ow <= sw - 20:
        ox, oy = col_x, cy
    else:
        ox, oy = cx, cy + ch + 16
        if oy + oh > sh - 40:
            ox, oy = max(20, sw - ow - 40), 20

    # ---- 名单窗：16:9 大窗口，居中靠上；装不下就等比缩小 ----
    bw, bh = BOARD_WIDTH, BOARD_HEIGHT_MAX
    # 保留一点边距，避免窗口顶到屏幕边缘
    max_w = sw - 80
    max_h = sh - 120
    if bw > max_w or bh > max_h:
        scale = min(max_w / bw, max_h / bh, 1.0)
        bw = max(480, int(bw * scale))
        bh = max(270, int(bh * scale))
    bx = max(20, (sw - bw) // 2)
    by = max(20, (sh - bh) // 2 - 40)  # 略偏上，给底部留出空间
    if by < 20:
        by = 20

    return (f"{ow}x{oh}+{ox}+{oy}", f"{bw}x{bh}+{bx}+{by}")


def 投票窗位置(sw: int, sh: int) -> str:
    """给投票窗算一个位置：左下角，不和另外两个窗口抢地方。"""
    w = min(760, max(420, int(sw * 0.30)))
    h = min(600, max(320, int(sh * 0.42)))
    x = max(20, int(sw * 0.02))
    y = max(40, sh - h - int(sh * 0.05))
    return f"{w}x{h}+{x}+{y}"


def plan(cfg, sw: int, sh: int, force: bool = False) -> bool:
    """安排两个捕获窗口的位置，返回「配置有没有被改动」。

    ``sw`` / ``sh`` 是屏幕尺寸。调用方负责在**创建窗口之前**调用它。
    """
    if sw <= 0 or sh <= 0:
        return False

    o_geom, b_geom = suggest(sw, sh)
    changed = False

    # ---- 展示窗 ----
    if force or not geom_ok(cfg.overlay.geom, sw, sh):
        cfg.overlay.geom = o_geom
        changed = True

    # ---- 投票窗 ----
    # 放在左下角，避开右边那两列
    if hasattr(cfg, "vote"):
        v_geom = 投票窗位置(sw, sh)
        if (force
                or not geom_ok(cfg.vote.geom, sw, sh)
                or same_origin(cfg.vote.geom, cfg.overlay.geom)
                or same_origin(cfg.vote.geom, cfg.board.geom)):
            cfg.vote.geom = v_geom
            changed = True

    # ---- 参与名单窗 ----
    # 除了「位置可用」，还要排除「和展示窗叠在同一个点」：
    # 那是旧版本的默认值，留着的话两个窗口会互相盖住。
    if (force
            or not geom_ok(cfg.board.geom, sw, sh)
            or same_origin(cfg.overlay.geom, cfg.board.geom)):
        cfg.board.geom = b_geom
        changed = True

    return changed
