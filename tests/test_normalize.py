# -*- coding: utf-8 -*-
"""UID 识别引擎测试。直接用 python 运行本文件即可（不依赖 pytest）。

    python tests\test_normalize.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uidkit.normalize import (  # noqa: E402
    ExtractConfig, Mode, extract, is_uid, normalize_uid, pick, preprocess,
)

TARGET = "100591822"


def cfg(**kw) -> ExtractConfig:
    """测试用的识别参数。

    ★ 默认**关掉** UID 位数/范围限制（uid_digits=0 等）：
      这个文件测的是「识别算法本身」—— 分隔符、混淆字母、中文数字这些，
      用 6~8 位的短号码测更省事。范围限制有单独的测试类覆盖。
      程序实际运行时是**开着**的（9 位、1亿~3.99亿）。
    """
    base = dict(min_len=6, max_len=15, mode=Mode.STRICT,
                uid_digits=0, uid_min_value=0, uid_max_value=0)
    base.update(kw)
    return ExtractConfig(**base)


class TestObfuscation(unittest.TestCase):
    """观众用来绕过抖音屏蔽的各种写法，都必须还原成 100591822。"""

    CASES = [
        # ---- 基本 ----
        ("100591822", "正常写法"),
        ("100 591 822", "半角空格"),
        ("100  591  822", "连续空格"),
        ("100\t591\t822", "制表符"),
        ("100　591　822", "全角空格 U+3000"),
        ("100\u00a0591\u00a0822", "不间断空格"),
        ("  100 591 822  ", "首尾空格"),

        # ---- 标点 ----
        ("100.591.822", "英文句点"),
        ("100。591。822", "中文句号"),
        ("100-591-822", "连字符"),
        ("100_591_822", "下划线"),
        ("100*591*822", "星号"),
        ("100,591,822", "逗号"),
        ("100，591，822", "中文逗号"),
        ("100/591/822", "斜杠"),
        ("100\\591\\822", "反斜杠"),
        ("100|591|822", "竖线"),
        ("100·591·822", "间隔号"),
        ("100:591:822", "冒号"),
        ("100~591~822", "波浪号"),
        ("100#591#822", "井号"),
        ("100+591+822", "加号"),
        ("100=591=822", "等号"),
        ("100(591)822", "括号"),
        ("100【591】822", "中文方括号"),

        # ---- 逐位拆开 ----
        ("1 0 0 5 9 1 8 2 2", "逐位空格"),
        ("1-0-0-5-9-1-8-2-2", "逐位横线"),
        ("1 0 0 5 9 1 8 2 2", "逐位空格"),

        # ---- 全角 / 其它数字形式 ----
        ("１００５９１８２２", "全角数字"),
        ("１００ ５９１ ８２２", "全角数字+全角空格"),
        ("𝟏𝟎𝟎𝟓𝟗𝟏𝟖𝟐𝟐", "数学粗体数字"),
        ("①⓪⓪⑤⑨①⑧②②", "圈号数字"),

        # ---- 零宽 / 不可见字符 ----
        ("100\u200b591\u200b822", "零宽空格"),
        ("100\u200c591\u200c822", "零宽非连接符"),
        ("100\u200d591\u200d822", "零宽连接符"),
        ("100\ufeff591\ufeff822", "BOM"),
        ("100\u2060591\u2060822", "词连接符"),
        ("100\u00ad591\u00ad822", "软连字符"),
        ("100\u202e591\u202c822", "双向控制符"),

        # ---- 表情 / 符号隔断 ----
        ("100😀591😀822", "emoji"),
        ("100🎉591🎉822", "emoji2"),
        ("100✨591✨822", "闪光符号"),
        ("100❤591❤822", "心形"),
        ("100🔥591🔥822", "火"),
        ("100👍591👍822", "点赞"),
        ("100⭐591⭐822", "星星"),

        # ---- 混在文本里 ----
        ("我 100 591 822 求抽", "夹杂中文"),
        ("主播看我 100 591 822", "夹杂中文2"),
        ("UID：100 591 822", "带UID标签"),
        ("uid:100 591 822", "小写uid"),
        ("抖音号 100 591 822", "抖音号标签"),
        ("@100 591 822", "@前缀"),
        ("100 591 822 求求了", "带后缀"),
        ("我的号是100 591 822，抽我", "带逗号"),
        ("100 591 822\n", "带换行"),

        # ---- 换行/多行 ----
        ("100 591\n822", "中间换行"),
    ]

    def test_obfuscation(self):
        for text, why in self.CASES:
            with self.subTest(why=why, text=text):
                got = extract(text, cfg(mode=Mode.LOOSE))
                self.assertTrue(got, f"{why}: 没识别出任何东西 → {text!r}")
                self.assertEqual(got[0].uid, TARGET,
                                 f"{why}: 识别成 {got[0].uid} → {text!r}")

    def test_strict_mode_accepts_obfuscated(self):
        """严格模式下，「100 591 822」这种整条即 UID 的写法必须被接受。"""
        for text in ["100 591 822", "100.591.822", "１００ ５９１ ８２２",
                     "100😀591😀822", "100\u200b591\u200b822"]:
            with self.subTest(text=text):
                got = extract(text, cfg(mode=Mode.STRICT))
                self.assertTrue(got, f"严格模式漏掉了 {text!r}")
                self.assertEqual(got[0].uid, TARGET)
                self.assertTrue(got[0].whole)


class TestNoise(unittest.TestCase):
    """不该被识别成 UID 的东西。"""

    def test_plain_chat(self):
        for text in ["哈哈哈", "主播好帅", "666", "888", "233",
                     "来了来了", "抽我抽我", "😀😀😀", ""]:
            with self.subTest(text=text):
                self.assertEqual(extract(text, cfg(mode=Mode.SMART)), [],
                                 f"{text!r} 不该被识别成 UID")

    def test_short_numbers_rejected(self):
        c = cfg(min_len=6, mode=Mode.STRICT)
        for text in ["123", "12345", "66666"]:
            with self.subTest(text=text):
                self.assertEqual(extract(text, c), [])

    def test_common_numbers_blocked(self):
        c = cfg(min_len=6, mode=Mode.STRICT, block_common_numbers=True)
        for text in ["123456", "666666", "888888", "123456789", "5201314",
                     "111111", "9999999", "121212"]:
            with self.subTest(text=text):
                self.assertEqual(extract(text, c), [],
                                 f"{text!r} 是常见数字，应该被挡掉")

    def test_common_numbers_can_be_allowed(self):
        c = cfg(min_len=6, mode=Mode.STRICT, block_common_numbers=False)
        self.assertTrue(extract("123456789", c))

    def test_length_bounds(self):
        c = cfg(min_len=8, max_len=10, mode=Mode.STRICT, block_common_numbers=False)
        self.assertTrue(extract("12345678", c))
        self.assertFalse(extract("1234567", c), "7位应该被 min_len=8 挡掉")
        self.assertFalse(extract("12345678901", c), "11位应该被 max_len=10 挡掉")

    def test_gap_limit(self):
        """数字之间夹太多无关符号就不再认为是同一个 UID。"""
        c = cfg(mode=Mode.LOOSE, max_gap=2)
        got = extract("100 .......... 591", c)
        self.assertFalse(got and got[0].uid == "100591",
                         "间距超过 max_gap 不该被粘起来")

    def test_hard_break(self):
        """汉字/字母一定要把数字断开。"""
        got = extract("100求591", cfg(mode=Mode.LOOSE, min_len=2, max_len=15))
        uids = {c.uid for c in got}
        self.assertNotIn("100591", uids, "汉字两侧的数字不该被粘起来")


class TestModes(unittest.TestCase):
    def test_strict_rejects_embedded(self):
        c = cfg(mode=Mode.STRICT)
        self.assertEqual(extract("主播抽我 100 591 822", c), [])

    def test_smart_accepts_embedded_long(self):
        c = cfg(mode=Mode.SMART)
        got = extract("主播抽我 100 591 822", c)
        self.assertTrue(got)
        self.assertEqual(got[0].uid, TARGET)

    def test_smart_prefers_whole(self):
        c = cfg(mode=Mode.SMART)
        got = extract("100 591 822", c)
        self.assertTrue(got[0].whole)

    def test_smart_ignores_short_embedded(self):
        """文本里的短数字，智能模式不收。"""
        c = cfg(mode=Mode.SMART, min_len=6)
        self.assertEqual(extract("我今天抽了 3 次", c), [])

    def test_one_uid_per_danmaku(self):
        """一条弹幕只取最像 UID 的那一个（防止一个人刷多个号）。"""
        c = cfg(mode=Mode.LOOSE)
        got = extract("100 591 822 和 100 591 823", c)
        self.assertEqual(len({g.uid for g in got}), 2)  # extract 会给全部
        # 但 Session 只用第一个，所以两个 UID 必须都能算出来
        self.assertEqual({g.uid for g in got}, {"100591822", "100591823"})


class TestLetterFix(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(extract("l0059l822", cfg(mode=Mode.LOOSE))[:1] and
                         pick("l0059l822", cfg(mode=Mode.LOOSE)).uid == TARGET)

    def test_on(self):
        c = cfg(mode=Mode.STRICT, letter_fix=True)
        self.assertEqual(pick("l0059l822", c).uid, "100591822")
        self.assertEqual(pick("1OO591822", c).uid, "100591822")
        self.assertEqual(pick("IOO59I822", c).uid, "100591822")

    def test_on_with_spaces(self):
        c = cfg(mode=Mode.STRICT, letter_fix=True)
        self.assertEqual(pick("1OO 59l 822", c).uid, "100591822")


class TestChineseDigits(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(extract("一零零五九一八二二", cfg(mode=Mode.LOOSE)))

    def test_on(self):
        c = cfg(mode=Mode.STRICT, chinese_digits=True)
        self.assertEqual(pick("一零零五九一八二二", c).uid, TARGET)
        self.assertEqual(pick("一 零 零 五 九 一 八 二 二", c).uid, TARGET)


class TestKeywords(unittest.TestCase):
    def test_require(self):
        c = cfg(mode=Mode.SMART, require_keywords=("抽",))
        self.assertFalse(extract("100 591 822", c))
        got = extract("抽 100 591 822", c)
        self.assertTrue(got)
        self.assertEqual(got[0].uid, TARGET)

    def test_block(self):
        c = cfg(mode=Mode.STRICT, block_keywords=("广告",))
        self.assertFalse(extract("100 591 822 广告", c))
        self.assertTrue(extract("100 591 822", c))


class TestLabelStripping(unittest.TestCase):
    """标签必须被抹掉，且不能和易混字母纠正互相干扰。

    回归用例：曾经 "UID:100 591 824" 在开启字母纠正后被识别成 10100591824
    （"ID" 里的 I→1、D→0，跟真 UID 粘在一起了）。
    """

    def test_label_not_glued(self):
        """标签紧贴 UID 的写法，严格模式下也必须还原。"""
        whole_forms = ["UID:100 591 824", "uid:100591824", "UID 100 591 824",
                       "ID:100 591 824", "抖音号 100 591 824",
                       "我的UID是100 591 824".replace("是", "")]
        for fix in (False, True):
            c = cfg(mode=Mode.STRICT, letter_fix=fix)
            for text in whole_forms:
                with self.subTest(letter_fix=fix, text=text):
                    got = extract(text, c)
                    self.assertTrue(got, f"{text!r} 没认出来")
                    self.assertEqual(got[0].uid, "100591824",
                                     f"{text!r} → {got[0].uid}")

    def test_label_embedded_smart(self):
        """夹在句子中间、带标签的写法，智能模式要认出来。"""
        for fix in (False, True):
            c = cfg(mode=Mode.SMART, letter_fix=fix)
            for text in ["我的UID是100 591 824", "主播我的UID是100 591 824 抽我"]:
                with self.subTest(letter_fix=fix, text=text):
                    got = extract(text, c)
                    self.assertTrue(got, f"{text!r} 没认出来")
                    self.assertEqual(got[0].uid, "100591824",
                                     f"{text!r} → {got[0].uid}")

    def test_label_gives_bonus(self):
        c = cfg(mode=Mode.SMART)
        with_label = pick("UID:100 591 822", c)
        self.assertIsNotNone(with_label)
        self.assertIn("带UID标签", with_label.reason)

    def test_label_allows_whole_match(self):
        c = cfg(mode=Mode.STRICT)
        got = pick("UID：100 591 822", c)
        self.assertTrue(got.whole, "抹掉标签后应当算「整条即UID」")

    def test_label_breaks_digits_on_both_sides(self):
        """★ 回归：标签两侧都有数字时，不能被粘成一个号码。

        标签是用「等长空格」抹掉的，而空格属于「软分隔」、允许被跨越，
        于是 "1号100591822" 曾经会粘成 "1100591822" —— 凭空多出来的号码，
        还会被静默加进参与池。标签位置现在按硬断开处理。
        """
        c = cfg(mode=Mode.SMART)
        for text in ["1号100591822", "1 号 100591822", "1、号、100591822"]:
            with self.subTest(text=text):
                got = extract(text, c)
                self.assertTrue(got, f"{text!r} 没认出来")
                self.assertEqual(got[0].uid, "100591822",
                                 f"{text!r} 被粘成了 {got[0].uid}")

    def test_chinese_digit_before_label(self):
        """★ 回归：开启中文数字后，"一号100591822" 里的「一」不能粘进号码。"""
        c = cfg(mode=Mode.SMART, chinese_digits=True)
        for text in ["一号100591822", "1号100591822", "一号 100591822",
                     "号100591822"]:
            with self.subTest(text=text):
                got = extract(text, c)
                self.assertTrue(got, f"{text!r} 没认出来")
                self.assertEqual(got[0].uid, "100591822",
                                 f"{text!r} 被粘成了 {got[0].uid}")

    def test_normal_labels_still_work(self):
        """上面的改动不能把正常标签写法弄坏。"""
        c = cfg(mode=Mode.STRICT)
        for text in ["UID:100591822", "抖音号100591822", "号码100591822",
                     "100591822号", "编号：100591822"]:
            with self.subTest(text=text):
                got = extract(text, c)
                self.assertTrue(got, f"{text!r} 没认出来")
                self.assertEqual(got[0].uid, "100591822",
                                 f"{text!r} → {got[0].uid}")


class TestChineseDigitsMixed(unittest.TestCase):
    """中文数字 + 阿拉伯数字混写。

    这是「中文数字」开关打开之后的主要价值：观众把 UID 写成
    "一00591822" / "100五九一八二二" / "1〇〇591822" 这种中西混排。
    """

    MIXED = [
        ("一00591822", "中文当首位"),
        ("100五九一八二二", "中文在后半"),
        ("1〇〇591822", "中文在中间"),
        ("10〇591八二二", "多处混排"),
        ("一00五九1八2二", "逐位混排"),
        ("一〇〇 五九一 八二二", "中文+空格分隔"),
        ("一零零.五九一.八二二", "中文+句点分隔"),
    ]

    def test_off_by_default(self):
        """默认关闭：这些写法本来认不出来（所以要知道有这个开关）。"""
        c = cfg(mode=Mode.SMART)
        self.assertFalse(c.chinese_digits, "中文数字应当默认关闭")
        for text, _ in self.MIXED:
            with self.subTest(text=text):
                got = extract(text, c)
                self.assertTrue(not got or got[0].uid != TARGET,
                                f"{text!r} 在关闭状态下不该被正确识别")

    def test_on_recognizes_mixed(self):
        """打开之后，中西混排必须全部还原成 100591822。"""
        c = cfg(mode=Mode.SMART, chinese_digits=True)
        for text, why in self.MIXED:
            with self.subTest(text=text, why=why):
                got = extract(text, c)
                self.assertTrue(got, f"{text!r}（{why}）没认出来")
                self.assertEqual(got[0].uid, TARGET,
                                 f"{text!r}（{why}）→ {got[0].uid}")

    def test_pure_chinese(self):
        c = cfg(mode=Mode.SMART, chinese_digits=True)
        for text in ["一零零五九一八二二", "一〇〇五九一八二二",
                     "壹零零伍玖壹捌贰贰", "幺零零五九一八二二"]:
            with self.subTest(text=text):
                got = extract(text, c)
                self.assertTrue(got, f"{text!r} 没认出来")
                self.assertEqual(got[0].uid, TARGET)

    def test_no_false_positive_on_chat(self):
        """打开中文数字后，普通聊天不能被误判成 UID。"""
        c = cfg(mode=Mode.SMART, chinese_digits=True)
        chat = ["我二点三十分四号桌五个人六点走", "一二三四五六七",
                "七八九十", "三号链接四号链接五号链接六号链接",
                "第一次第二次第三次第四次第五次第六次",
                "从一到十我都认识", "五一劳动节十一国庆节",
                "二零二四年三月十五日", "六六六六六六"]
        for text in chat:
            with self.subTest(text=text):
                self.assertFalse(extract(text, c),
                                 f"{text!r} 被误判成了 UID")

    def test_non_positional_reading_rejected(self):
        """「一亿零五十九万」是数值读法，不是逐位念法，不该被拼成号码。"""
        c = cfg(mode=Mode.SMART, chinese_digits=True)
        for text in ["一亿零五十九万一千八百二十二", "十万零五百九十一"]:
            with self.subTest(text=text):
                self.assertFalse(extract(text, c),
                                 f"{text!r} 不该被识别")

    def test_leading_zero_preserved(self):
        """前导零要如实保留，不能自作聪明去掉。"""
        c = cfg(mode=Mode.SMART, chinese_digits=True)
        got = extract("零一零零五九一八二二", c)
        self.assertTrue(got)
        self.assertEqual(got[0].uid, "0100591822")


class TestUidRange(unittest.TestCase):
    """UID 位数 / 数值范围限制（程序实际运行时开启）。"""

    def _c(self, **kw):
        # 这里显式开启范围限制，模拟程序实际运行的默认值
        base = dict(mode=Mode.SMART, uid_digits=9,
                    uid_min_value=100000000, uid_max_value=399999999,
                    block_common_numbers=True)
        base.update(kw)
        return ExtractConfig(**base)

    def test_defaults_are_strict(self):
        """ExtractConfig 的默认值就是 9 位、1亿~3.99亿。"""
        c = ExtractConfig()
        self.assertEqual(c.uid_digits, 9)
        self.assertEqual(c.uid_min_value, 100000000)
        self.assertEqual(c.uid_max_value, 399999999)

    def test_accepts_in_range(self):
        c = self._c()
        for text, want in [("100 591 822", "100591822"),
                           ("200 591 822", "200591822"),
                           ("399 999 999", "399999999"),
                           ("100000000", "100000000")]:
            with self.subTest(text=text):
                got = extract(text, c)
                self.assertTrue(got, f"{text!r} 应该认出来")
                self.assertEqual(got[0].uid, want)

    def test_rejects_out_of_range(self):
        c = self._c()
        for text in ["400 000 000", "099 999 999", "900 000 000"]:
            with self.subTest(text=text):
                self.assertFalse(extract(text, c), f"{text!r} 超出范围应拒绝")

    def test_rejects_wrong_length(self):
        c = self._c()
        for text in ["12345678", "1005918220", "138 0013 8000"]:
            with self.subTest(text=text):
                self.assertFalse(extract(text, c), f"{text!r} 位数不对应拒绝")

    def test_wide_spaces_still_work(self):
        """范围限制不能把「宽空格」那个修复弄坏。"""
        c = self._c()
        got = extract("280   701     003", c)
        self.assertTrue(got)
        self.assertEqual(got[0].uid, "280701003")

    def test_can_be_disabled(self):
        """两个都设 0 -> 完全不限制。"""
        c = self._c(uid_digits=0, uid_min_value=0, uid_max_value=0,
                    block_common_numbers=False)
        self.assertTrue(extract("12345678", c))
        self.assertTrue(extract("400 000 000", c))

    def test_uid_in_range_helper(self):
        from uidkit.normalize import uid_in_range
        c = self._c()
        self.assertTrue(uid_in_range("100591822", c))
        self.assertFalse(uid_in_range("400000000", c))
        self.assertFalse(uid_in_range("12345678", c))
        self.assertFalse(uid_in_range("abcdefghi", c))


class TestHelpers(unittest.TestCase):
    def test_preprocess(self):
        self.assertEqual(preprocess("１００"), "100")
        self.assertEqual(preprocess("100\u200b591"), "100591")
        self.assertEqual(preprocess("①⓪⓪"), "100")

    def test_normalize_uid(self):
        self.assertEqual(normalize_uid("100 591 822"), "100591822")
        self.assertEqual(normalize_uid("１００-５９１"), "100591")

    def test_is_uid(self):
        self.assertTrue(is_uid("100 591 822", cfg(mode=Mode.STRICT)))
        self.assertFalse(is_uid("哈哈哈", cfg(mode=Mode.STRICT)))

    def test_whole_flag_and_score(self):
        got = pick("100 591 822", cfg(mode=Mode.SMART))
        self.assertTrue(got.whole)
        self.assertIn("整条即UID", got.reason)

    def test_raw_preserved(self):
        """raw 里要能看到原文，方便主播核对。"""
        got = pick("100 591 822", cfg(mode=Mode.SMART))
        self.assertIn("100", got.raw)


class TestRealWorldDanmaku(unittest.TestCase):
    """模拟一场真实直播里的公屏，检查抽取效果。"""

    DANMAKU = [
        "主播好帅",
        "哈哈哈",
        "666",
        "来了",
        "100 591 822",
        "抽我 100 591 823",
        "UID:100 591 824",
        "１００５９１８２５",
        "100.591.826",
        "100😀591😀827",
        "100\u200b591\u200b828",
        "我 100 591 829 求抽",
        "888",
        "233333",
        "抽奖抽奖",
        "123456789",
        "l0059l830",
        "1OO 591 831",
        "100 591 832 号",
        "关注了 100 591 833",
    ]

    def test_smart_mode(self):
        c = cfg(mode=Mode.SMART, letter_fix=True)
        got = [pick(t, c) for t in self.DANMAKU]
        uids = [g.uid for g in got if g]
        for expect in ["100591822", "100591823", "100591824", "100591825",
                       "100591826", "100591827", "100591828", "100591829",
                       "100591830", "100591831", "100591832", "100591833"]:
            with self.subTest(expect=expect):
                self.assertIn(expect, uids)
        # 噪声不该混进来
        for bad in ["666", "888", "233333", "123456789"]:
            with self.subTest(bad=bad):
                self.assertNotIn(bad, uids)

    def test_strict_mode(self):
        c = cfg(mode=Mode.STRICT)
        got = [pick(t, c) for t in self.DANMAKU]
        uids = [g.uid for g in got if g]
        self.assertIn("100591822", uids)
        self.assertIn("100591825", uids)
        self.assertIn("100591826", uids)
        self.assertNotIn("666", uids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
