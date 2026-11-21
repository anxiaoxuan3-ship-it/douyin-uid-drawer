# -*- coding: utf-8 -*-
"""补测：倍率在各种「没开其它加权」的组合下都要生效。

这是之前测试的盲区 —— 所有测试都开着 by_watch，所以
「只调倍率、不开加权」这条路径从来没被走过。
"""

import sys
import time

sys.path.insert(0, r"D:\uid看号")
sys.path.insert(0, r"D:\uid看号\tests")

from uidkit.draw import WeightOptions, compute_weights, draw  # noqa: E402
from uidkit.pool import Entry  # noqa: E402

现在 = 1_800_000_000.0
没问题 = True


def 检查(名字, 好, 说明=""):
    global 没问题
    没问题 &= bool(好)
    print(f"  [{'ok' if 好 else 'FAIL'}] {名字}" + (f"  {说明}" if 说明 else ""))


def 造池(n, 倍率=None, 补偿=None, 看分钟=None):
    倍率 = 倍率 or {}
    补偿 = 补偿 or {}
    看分钟 = 看分钟 or {}
    out = []
    for i in range(n):
        out.append(Entry(
            uid=f"{300000000 + i}", count=1,
            first_ts=(现在 - 看分钟[i] * 60) if i in 看分钟 else 现在,
            weight_mult=倍率.get(i, 0.0),
            carry_mult=补偿.get(i, 0.0),
        ))
    return out


def 统计(es, opt, 次数=800):
    命中 = {}
    for i in range(次数):
        r = draw(es, count=1, options=opt, seed=f"X{i}", now=现在)
        命中[r.winner_uids[0]] = 命中.get(r.winner_uids[0], 0) + 1
    return 命中


def 主流程():
    print("=" * 72)
    print("A. 只调手动倍率，其它加权全关")
    print("=" * 72)
    for 倍, 期望下限 in ((2.0, 0.07), (10.0, 0.30), (99.0, 0.80)):
        es = 造池(20, 倍率={0: 倍})
        opt = WeightOptions()          # 全关！
        ws = compute_weights(es, 现在, opt)
        理论 = ws[0] / sum(ws)
        命中 = 统计(es, opt, 800)
        实 = 命中.get(es[0].uid, 0) / 800
        检查(f"{倍:g} 倍：理论 {理论*100:.0f}%，实际 {实*100:.0f}%",
             实 >= 期望下限, f"（下限 {期望下限*100:.0f}%）")

    print()
    print("=" * 72)
    print("B. 只有补偿倍率，其它加权全关")
    print("=" * 72)
    es = 造池(10, 补偿={0: 5.0})
    opt = WeightOptions()
    ws = compute_weights(es, 现在, opt)
    print(f"       权重：{ws[0]:.1f} vs 其余 1.0")
    命中 = 统计(es, opt, 600)
    实 = 命中.get(es[0].uid, 0) / 600
    理论 = ws[0] / sum(ws)
    检查(f"补偿 5 倍：理论 {理论*100:.0f}%，实际 {实*100:.0f}%",
          abs(实 - 理论) < 0.12)

    print()
    print("=" * 72)
    print("C. 倍率 + 观看加权同时存在")
    print("=" * 72)
    es = 造池(10, 倍率={0: 3.0}, 看分钟={0: 60, 1: 60})
    opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
    ws = compute_weights(es, 现在, opt)
    print(f"       第0号(60分钟×3倍)={ws[0]:.1f}  "
          f"第1号(60分钟)={ws[1]:.1f}  第2号(刚来)={ws[2]:.1f}")
    检查("倍率与时长叠加", abs(ws[0] - 9.0) < 1e-6, f"应为 3×3=9，实际 {ws[0]}")

    print()
    print("=" * 72)
    print("D. 倍率 + 保底同时存在")
    print("=" * 72)
    es = 造池(5, 倍率={0: 2.0})
    es[1].miss_rounds = 5
    opt = WeightOptions(pity_enabled=True, pity_rounds=3,
                        pity_mode="guarantee")
    r = draw(es, count=1, options=opt, seed="P", now=现在)
    检查("保底优先于倍率（保底的人必中）",
         r.winner_uids[0] == es[1].uid, f"实际抽中 {r.winner_uids}")

    print()
    print("=" * 72)
    print("E. 没有任何加权时仍是等概率")
    print("=" * 72)
    es = 造池(6)          # 无倍率、无时长
    opt = WeightOptions()
    命中 = 统计(es, opt, 1200)
    各 = list(命中.values())
    print(f"       分布：{dict(sorted(命中.items()))}")
    检查("没有加权时分布均匀",
         各 and max(各) < 1200 / 6 * 1.5, f"最大 {max(各) if 各 else 0}")

    print()
    print("=" * 72)
    print("F. 复算仍然一致（指纹带没带权重）")
    print("=" * 72)
    es = 造池(10, 倍率={0: 99.0})
    opt = WeightOptions()
    r1 = draw(es, count=1, options=opt, seed="R", now=现在)
    r2 = draw(es, count=1, options=opt, seed="R", now=现在)
    检查("同种子结果一致", r1.winner_uids == r2.winner_uids)
    检查("指纹一致", r1.fingerprint == r2.fingerprint)
    检查("指纹不为空", bool(r1.fingerprint))

    print()
    print("=" * 72)
    print(f"结果：{'全部通过 [OK]' if 没问题 else '有问题 [FAIL]'}")
    print("=" * 72)
    return 0 if 没问题 else 1


if __name__ == "__main__":
    raise SystemExit(主流程())
