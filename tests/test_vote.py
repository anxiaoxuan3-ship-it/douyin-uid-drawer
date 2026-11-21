# -*- coding: utf-8 -*-
"""验证投票逻辑：档位识别、一人一票、改票、统计。"""

import sys
import unittest
import time

sys.path.insert(0, r"D:\uid看号")

from uidkit.vote import VoteBox, VoteState, 档位  # noqa: E402

没问题 = True


def 检查(名字, 好, 说明=""):
    global 没问题
    没问题 &= bool(好)
    print(f"  [{'ok' if 好 else 'FAIL'}] {名字}" + (f"  {说明}" if 说明 else ""))


def 主流程():
    print("=" * 70)
    print("1) 五个档位")
    print("=" * 70)
    print(f"  档位：{档位}")
    检查("一共 5 档", len(档位) == 5)
    检查("顺序是 拉完了 < NPC < 人上人 < 顶级 < 夯",
         档位 == ["拉完了", "NPC", "人上人", "顶级", "夯"])

    print()
    print("=" * 70)
    print("2) 弹幕识别档位")
    print("=" * 70)
    box = VoteBox()
    box.open("100000001", "小明")
    用例 = [
        ("拉完了", "拉完了"),
        ("NPC", "NPC"),
        ("人上人", "人上人"),
        ("顶级", "顶级"),
        ("夯", "夯"),
        ("这个顶级吧", "顶级"),          # 带别的字也能认
        ("我觉得人上人", "人上人"),
        ("哈哈哈", None),
        ("100591822", None),
        ("", None),
        ("顶", None),                   # 不完整不算
    ]
    全过 = True
    for 文本, 期望 in 用例:
        得 = box.parse(文本)
        好 = 得 == 期望
        全过 &= 好
        print(f"    {'ok' if 好 else 'FAIL'}  {文本!r:<16} -> {得!r}"
              + ("" if 好 else f"  （期望 {期望!r}）"))
    检查("识别全部正确", 全过)

    print()
    print("=" * 70)
    print("3) 一人一票")
    print("=" * 70)
    box = VoteBox()
    box.open("100000001", "小明")
    结果 = []
    结果.append(box.vote("观众A", "顶级"))
    结果.append(box.vote("观众B", "顶级"))
    结果.append(box.vote("观众C", "NPC"))
    print(f"    三次投票结果：{结果}")
    检查("新投票返回 ok", 结果 == ["ok", "ok", "ok"])
    检查("顶级 2 票", box.state.counts["顶级"] == 2,
         f"实际 {box.state.counts['顶级']}")
    检查("NPC 1 票", box.state.counts["NPC"] == 1)
    检查("总票数 3", box.state.total == 3)

    print()
    print("=" * 70)
    print("4) 重复投同一档不累加")
    print("=" * 70)
    前 = box.state.counts["顶级"]
    结 = box.vote("观众A", "顶级")
    print(f"    观众A 再投顶级 -> {结}")
    检查("返回 same", 结 == "same")
    检查("票数没变", box.state.counts["顶级"] == 前)

    print()
    print("=" * 70)
    print("5) 改票")
    print("=" * 70)
    前顶 = box.state.counts["顶级"]
    前夯 = box.state.counts["夯"]
    结 = box.vote("观众A", "夯")
    print(f"    观众A 改投夯 -> {结}")
    检查("返回 changed", 结 == "changed")
    检查("顶级减一", box.state.counts["顶级"] == 前顶 - 1,
         f"实际 {box.state.counts['顶级']}")
    检查("夯加一", box.state.counts["夯"] == 前夯 + 1)
    检查("总票数不变", box.state.total == 3)

    print()
    print("=" * 70)
    print("6) 投票没开时不计数")
    print("=" * 70)
    box2 = VoteBox()
    结 = box2.vote("观众A", "顶级")
    检查("返回 closed", 结 == "closed")
    检查("票数为 0", box2.state.total == 0)

    print()
    print("=" * 70)
    print("7) 档位不认识")
    print("=" * 70)
    box3 = VoteBox()
    box3.open("100000001")
    结 = box3.vote("观众A", "不存在")
    检查("返回 bad", 结 == "bad")

    print()
    print("=" * 70)
    print("8) 统计：百分比 / 领先档位")
    print("=" * 70)
    box4 = VoteBox()
    box4.open("100000002", "小红")
    for i, 档 in enumerate(["顶级", "顶级", "顶级", "人上人", "NPC"]):
        box4.vote(f"观众{i}", 档)
    状态 = box4.state
    print(f"    票数：{状态.counts}")
    print(f"    顶级 {状态.percent('顶级'):.0f}%")
    检查("顶级占 60%", abs(状态.percent("顶级") - 60) < 0.1)
    检查("领先的是顶级", 状态.leader() == "顶级")

    # 平票时取更高的
    box5 = VoteBox()
    box5.open("100000003")
    box5.vote("A", "NPC")
    box5.vote("B", "顶级")
    检查("平票取更高档", box5.state.leader() == "顶级",
         f"实际 {box5.state.leader()}")

    print()
    print("=" * 70)
    print("9) 存档 / 读档")
    print("=" * 70)
    快照 = box4.snapshot()
    新 = VoteBox()
    新.load(快照)
    检查("票数一致", 新.state.counts == box4.state.counts)
    检查("投票人一致", 新.state.voters == box4.state.voters)
    检查("对象一致", 新.state.target_uid == box4.state.target_uid)

    # 存档被改坏时的容错
    坏 = {"counts": {"顶级": 999}, "voters": {"a": "顶级"}}
    新2 = VoteBox()
    新2.load(坏)
    检查("坏存档用 voters 重算 counts", 新2.state.counts["顶级"] == 1,
         f"实际 {新2.state.counts['顶级']}")

    print()
    print("=" * 70)
    print("10) 开新一轮会清空")
    print("=" * 70)
    box4.open("100000004", "新人")
    检查("票数归零", box4.state.total == 0)
    检查("对象换了", box4.state.target_uid == "100000004")
    检查("投票人清空", len(box4.state.voters) == 0)

    print()
    print("=" * 70)
    print(f"结果：{'全部通过 [OK]' if 没问题 else '有问题 [FAIL]'}")
    print("=" * 70)
    return 0 if 没问题 else 1


class TestNumericVote(unittest.TestCase):
    """数字投票：1=夯 ... 5=拉完了。"""

    def test_mapping(self):
        from uidkit.vote import 数字档位
        assert 数字档位 == {"1": "夯", "2": "顶级", "3": "人上人",
                          "4": "NPC", "5": "拉完了"}

    def test_parse_digits(self):
        from uidkit.vote import VoteBox
        b = VoteBox(); b.open("100000001")
        for 数, 期望 in [("1", "夯"), ("2", "顶级"), ("3", "人上人"),
                        ("4", "NPC"), ("5", "拉完了")]:
            assert b.parse(数) == 期望, f"{数} 应识别为 {期望}"

    def test_parse_text_still_works(self):
        """原来的文字投票必须保留。"""
        from uidkit.vote import VoteBox
        b = VoteBox(); b.open("100000001")
        for 词 in ("拉完了", "NPC", "人上人", "顶级", "夯"):
            assert b.parse(词) == 词
        assert b.parse("这个顶级吧") == "顶级"

    def test_uid_not_treated_as_vote(self):
        """★ 核心安全点：UID 绝不能被当成数字投票吞掉。"""
        from uidkit.vote import VoteBox
        b = VoteBox(); b.open("100000001")
        for uid in ("100591822", "300123456", "123456", "100 591 822"):
            assert b.parse(uid) is None, f"{uid} 不该被当成投票"
            assert b.looks_like_uid(uid), f"{uid} 应识别为 UID"

    def test_out_of_range_digits(self):
        from uidkit.vote import VoteBox
        b = VoteBox(); b.open("100000001")
        for s in ("0", "6", "10", "99"):
            assert b.parse(s) is None, f"{s} 不在 1~5 范围内"


class TestVoteTiming(unittest.TestCase):
    """投票手动开启 / 时长 / 参与池定时清空 的默认值。

    这些默认值决定了主播的使用流程，改动会直接影响直播现场，
    所以锁在测试里防止被无意改回去。
    """

    def test_vote_not_auto_open(self):
        """抽奖后**不能**自动开投票，必须主播手动点。"""
        from uidkit.config import AppConfig
        self.assertFalse(AppConfig().vote.auto_open)

    def test_default_vote_seconds(self):
        """投票默认 10 秒。"""
        from uidkit.config import AppConfig
        self.assertEqual(AppConfig().vote.vote_seconds, 10.0)

    def test_pool_auto_clear_defaults(self):
        """参与池默认每 5 分钟自动清一次。"""
        from uidkit.config import AppConfig
        c = AppConfig()
        self.assertTrue(c.pool_auto_clear)
        self.assertEqual(c.pool_auto_clear_minutes, 5.0)

    def test_vote_seconds_zero_means_no_limit(self):
        """0 表示不限时（语义不能变）。"""
        from uidkit.config import VoteConfig
        self.assertEqual(VoteConfig(vote_seconds=0).vote_seconds, 0)


if __name__ == "__main__":
    raise SystemExit(主流程())
