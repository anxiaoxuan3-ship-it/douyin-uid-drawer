# -*- coding: utf-8 -*-
"""抽取引擎测试（重点是「观看时长加权」）。

直接运行：  python tests\\test_draw.py
不依赖 pytest。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uidkit.draw import (  # noqa: E402
    WeightOptions, compute_weights, draw, pool_fingerprint, verify,
    watch_seconds, weight_report,
)
from uidkit.pool import Entry  # noqa: E402


#: ★ 固定的「现在」。统计类测试必须锁死这个值，否则每次跑 today 都不一样：
#:   first_ts 变 -> 权重变 -> 指纹变 -> 抽出来的结果变 -> 测试时过时不过。
#:   之前这个值取 time.time()，导致 test_longer_watchers_win_more 约 1/4 概率
#:   卡在 2.0 的边界上误报失败。
固定现在 = 1_800_000_000.0


def 造池(n: int, 跨度分钟: float = 60.0, 现在: float = None):
    """造 n 个人：第 0 个「刚来」，第 n-1 个「已经看了跨度分钟」。

    first_ts 是「第一次出现在公屏上的时间」，也就是观看时长的起点。
    """
    现在 = 固定现在 if 现在 is None else 现在
    out = []
    for i in range(n):
        看了 = 跨度分钟 * 60.0 * (i / max(1, n - 1))
        out.append(Entry(uid=f"{300000000 + i}", nick=f"观众{i}",
                         raw=f"{300000000 + i}",
                         first_ts=现在 - 看了, count=1))
    return out


class TestWeightFormula(unittest.TestCase):
    """权重公式本身。"""

    def test_new_visitor_gets_base_only(self):
        """刚来的人只有保底权重。"""
        now = time.time()
        es = 造池(3, 60, now)
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        ws = compute_weights(es, now, opt)
        self.assertAlmostEqual(ws[0], 1.0, places=6)

    def test_full_watch_gets_full_bonus(self):
        """看满 30 分钟拿到满加成（1 + 2 = 3）。"""
        now = time.time()
        es = [Entry(uid="1", first_ts=now - 30 * 60)]
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        self.assertAlmostEqual(compute_weights(es, now, opt)[0], 3.0, places=6)

    def test_bonus_is_capped_at_full(self):
        """看 10 小时也不会超过满加成。"""
        now = time.time()
        es = [Entry(uid="1", first_ts=now - 10 * 3600)]
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        self.assertAlmostEqual(compute_weights(es, now, opt)[0], 3.0, places=6)

    def test_half_watch_is_half_bonus(self):
        """看了一半时长 -> 一半加成。"""
        now = time.time()
        es = [Entry(uid="1", first_ts=now - 15 * 60)]
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        self.assertAlmostEqual(compute_weights(es, now, opt)[0], 2.0, places=6)

    def test_monotonic(self):
        """待得越久，权重越大（这是这个功能的核心承诺）。"""
        now = time.time()
        es = 造池(20, 60, now)
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        ws = compute_weights(es, now, opt)
        for a, b in zip(ws, ws[1:]):
            self.assertLessEqual(a, b, "权重必须随观看时长单调不减")

    def test_disabled_is_uniform(self):
        """关掉加权 = 每个人一样。"""
        now = time.time()
        es = 造池(10, 60, now)
        ws = compute_weights(es, now, WeightOptions(by_watch=False))
        self.assertEqual(len({round(w, 9) for w in ws}), 1)

    def test_no_ts_gets_no_bonus(self):
        """导入进来的人没有首次出现时间 -> 不给时长加成，防止刷权重。"""
        now = time.time()
        es = [Entry(uid="1", first_ts=0.0)]
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        self.assertAlmostEqual(compute_weights(es, now, opt)[0], 1.0, places=6)
        self.assertEqual(watch_seconds(es[0], now), 0.0)

    def test_cap(self):
        """权重上限生效。"""
        now = time.time()
        es = [Entry(uid="1", first_ts=now - 3600)]
        opt = WeightOptions(by_watch=True, full_minutes=10, watch_bonus=5.0,
                            cap=2.0)
        self.assertAlmostEqual(compute_weights(es, now, opt)[0], 2.0, places=6)


class TestVerifiability(unittest.TestCase):
    """★ 重点：加权模式也必须能被复算 —— 否则「公平」就是空话。"""

    def setUp(self):
        self.now = time.time()
        self.es = 造池(50, 90, self.now)
        self.opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)

    def test_same_moment_verifies(self):
        r = draw(self.es, count=3, options=self.opt, seed="SEED1")
        self.assertTrue(verify(self.es, 3, r.seed, r.fingerprint,
                               r.winner_uids, options=self.opt, now=r.ts))

    def test_other_moment_does_not_verify(self):
        """换个时间点就算不出同样的结果 —— 证明权重真的进了指纹。"""
        r = draw(self.es, count=3, options=self.opt, seed="SEED1")
        self.assertFalse(verify(self.es, 3, r.seed, r.fingerprint,
                                r.winner_uids, options=self.opt,
                                now=r.ts + 3600))

    def test_missing_moment_refuses(self):
        """加权模式不给时间点，就不该声称验证通过。"""
        r = draw(self.es, count=1, options=self.opt, seed="SEED1")
        self.assertFalse(verify(self.es, 1, r.seed, r.fingerprint,
                                r.winner_uids, options=self.opt))

    def test_fingerprint_covers_weights(self):
        """同样的名单、不同的权重 -> 指纹必须不同。"""
        a = pool_fingerprint(self.es, [1.0] * len(self.es))
        b = pool_fingerprint(self.es, [3.0] * len(self.es))
        self.assertNotEqual(a, b)

    def test_fingerprint_without_weights_is_stable(self):
        """不传权重时，指纹只跟 UID 名单有关（保证老逻辑/等概率模式不变）。"""
        self.assertEqual(pool_fingerprint(self.es), pool_fingerprint(self.es))

    def test_uniform_mode_still_verifies_old_way(self):
        """等概率模式下，老式调用（不传 options/now）仍然可用。"""
        r = draw(self.es, count=2, seed="SEED2")     # 默认不加权
        self.assertTrue(verify(self.es, 2, r.seed, r.fingerprint,
                               r.winner_uids))


class TestDrawBehaviour(unittest.TestCase):
    def test_no_duplicate_winners(self):
        """一次抽多个不会抽到同一个人。"""
        es = 造池(30)
        r = draw(es, count=5, options=WeightOptions(by_watch=True), seed="S")
        self.assertEqual(len(r.winner_uids), 5)
        self.assertEqual(len(set(r.winner_uids)), 5)

    def test_count_clamped_to_pool(self):
        """要抽的人比池子里还多，就抽满为止。"""
        es = 造池(3)
        r = draw(es, count=10, seed="S")
        self.assertEqual(len(r.winner_uids), 3)

    def test_empty_pool(self):
        r = draw([], count=3, seed="S")
        self.assertEqual(r.winner_uids, [])
        self.assertEqual(r.pool_size, 0)

    def test_reel_ends_with_winner(self):
        """滚动动画的最后一个一定是中奖者（否则展示窗会定格在别人身上）。"""
        es = 造池(20)
        r = draw(es, count=1, reel_len=30, seed="S")
        self.assertEqual(r.reel[-1], r.winner_uids[0])

    def test_reel_is_uniform_not_weighted(self):
        """陪跑序列故意用等概率 —— 否则滚动过程会泄露谁更容易中奖。

        造一个极端池子：一个人权重 3，其余权重 1。
        如果按权重生成，那个人在陪跑里会出现得明显更多。
        """
        now = time.time()
        es = [Entry(uid="OLD", first_ts=now - 3600)]
        es += [Entry(uid=f"N{i}", first_ts=now) for i in range(40)]
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        r = draw(es, count=1, reel_len=2000, options=opt, seed="S")
        # 陪跑里每个人的期望出现次数应当接近 2000/41 ≈ 49
        cnt = r.reel.count("OLD")
        self.assertLess(cnt, 90, f"陪跑序列看起来被加权了（OLD 出现 {cnt} 次）")

    def test_proof_mentions_weight(self):
        """凭证里要能看出用了加权，观众才知道规则。"""
        es = 造池(10)
        r = draw(es, count=1, options=WeightOptions(by_watch=True), seed="S")
        self.assertIn("权重", r.proof())

    def test_backward_compatible_weight_by_count(self):
        """老的 weight_by_count=True 调用方式仍然能用。"""
        es = 造池(10)
        r = draw(es, count=1, weight_by_count=True, seed="S")
        self.assertTrue(r.options.by_count)
        self.assertEqual(len(r.winner_uids), 1)


class TestStatistics(unittest.TestCase):
    """统计验证：加权是不是真的起作用了。

    这类测试用固定种子跑几千次，结果稳定、不会偶发失败。
    """

    def test_longer_watchers_win_more(self):
        es = 造池(20, 60)          # 第 19 个看了 60 分钟，第 0 个刚来
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        命中 = {}
        for i in range(2000):
            # ★ now 必须和造池用的是同一个时刻：draw() 不传 now 的话
            #   它会自己取 time.time()，跟 first_ts 对不上，
            #   所有人的已观看时长都会被算成 0（权重全变 1.0，等于没加权）。
            r = draw(es, count=1, options=opt, seed=f"STAT{i}",
                     now=固定现在)
            u = r.winner_uids[0]
            命中[u] = 命中.get(u, 0) + 1
        老 = 命中.get(es[-1].uid, 0)
        新 = 命中.get(es[0].uid, 0)
        self.assertGreater(老, 新, "看得久的人应当中得更多")
        # 理论倍数 3.0（老权重 3.0、新权重 1.0），给足容差
        self.assertGreater(老 / max(1, 新), 2.0,
                           f"倍数 {老/max(1,新):.2f} 明显偏低，加权可能没生效")

    def test_uniform_mode_is_balanced(self):
        """关掉加权后，每个人中奖次数应当接近平均。"""
        es = 造池(10, 60)
        opt = WeightOptions(by_watch=False)
        命中 = {}
        for i in range(2000):
            r = draw(es, count=1, options=opt, seed=f"U{i}", now=固定现在)
            u = r.winner_uids[0]
            命中[u] = 命中.get(u, 0) + 1
        最多 = max(命中.values())
        最少 = min(命中.values())
        self.assertLess(最多 / max(1, 最少), 1.6,
                        f"等概率模式偏差太大：{最少} ~ {最多}")


class TestWeightReport(unittest.TestCase):
    def test_report_shape(self):
        now = time.time()
        es = 造池(30, 60, now)
        opt = WeightOptions(by_watch=True, full_minutes=30, watch_bonus=2.0)
        rep = weight_report(es, now, opt)
        self.assertEqual(rep["count"], 30)
        self.assertAlmostEqual(rep["min"], 1.0, places=5)
        self.assertAlmostEqual(rep["max"], 3.0, places=5)
        self.assertAlmostEqual(rep["oldest_times"], 3.0, places=5)

    def test_empty(self):
        self.assertEqual(weight_report([], time.time(),
                                       WeightOptions())["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
