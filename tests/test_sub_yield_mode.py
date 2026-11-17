"""次级收益口径按交易文件分别处理（第 9 周 P1-6）。

鼎柚的本金分配顺序在偿还次级本金后先支付后端服务报酬，算法由《收费文件》
约定且不公开，故只报 0%/100% 抽取边界的条件区间。够花的公开分配顺序没有
后端服务报酬科目，撤销区间、按 excess_sweep=0 报条件性模型点估计。
两者都不是市场报价。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_case import run_one  # noqa: E402


class TestSubYieldMode(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = cls._tmp.name
        cls.reports = {}
        for cid, stem in (("dingyou", "dingyou_2025_3"),
                          ("gouhua", "gouhua_2025_2")):
            path = os.path.join(ROOT, "cases", stem + ".json")
            case = json.load(open(path, encoding="utf-8"))
            run_one(path, cls.out, quiet=True)
            with open(os.path.join(cls.out,
                                   f"{case['case_id']}_report.md"),
                      encoding="utf-8") as f:
                cls.reports[cid] = f.read()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_case_configuration(self):
        """两单必须显式配置口径，且方向相反。"""
        dy = json.load(open(os.path.join(ROOT, "cases", "dingyou_2025_3.json"),
                            encoding="utf-8"))
        gh = json.load(open(os.path.join(ROOT, "cases", "gouhua_2025_2.json"),
                            encoding="utf-8"))
        self.assertEqual(dy["structure"]["sub_yield_mode"], "interval")
        self.assertEqual(gh["structure"]["sub_yield_mode"], "point")
        self.assertIn("收费文件", dy["structure"]["sub_yield_mode_note"])
        self.assertIn("作废", gh["structure"]["sub_yield_mode_note"])

    def test_dingyou_keeps_interval(self):
        r = self.reports["dingyou"]
        self.assertIn("只能给条件区间，给不出点估计", r)
        self.assertIn("后端报酬", r)
        self.assertIn("都不是市场报价", r)
        self.assertNotIn("区间对本单不适用", r)

    def test_gouhua_drops_interval(self):
        r = self.reports["gouhua"]
        self.assertIn("条件性模型点估计", r)
        self.assertIn("原 0%–100% 后端报酬区间对本单不适用，已撤销", r)
        self.assertIn("不是市场报价", r)
        self.assertNotIn("只能给条件区间，给不出点估计", r)

    def test_limitations_match_case(self):
        """第八节局限第 6 条也按单分别表述，不能两套口径混用。"""
        self.assertIn("仅报告 0% 与 100% 抽取边界形成的条件区间",
                      self.reports["dingyou"])
        self.assertIn("按 `excess_sweep=0` 报告条件性模型点估计",
                      self.reports["gouhua"])


if __name__ == "__main__":
    unittest.main()
