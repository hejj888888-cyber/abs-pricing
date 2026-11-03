"""回收时间分布测试（第 3 周）：退化性、守恒性、尾部完整性与反馈环。

对应 `docs/recovery-timing.md`。最关键的一条是 `test_default_matches_explicit_one_hot_rowwise`：
默认定点滞后与显式 one-hot 分布必须逐行逐字段相等，否则评级机构对表口径会被悄悄改掉。

运行：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abs import Pool, build_schedule  # noqa: E402


def make_pool(**kw):
    base = dict(balance=1.5e9, annual_yield=0.2115, monthly_repayment_rate=0.1987,
                charge_off_rate=0.0314, recovery_rate=0.10, recovery_lag_months=6,
                servicing_fee_rate=0.005, revolving_months=6, amort_months=12)
    base.update(kw)
    return Pool(**base)


class TestRecoveryWeights(unittest.TestCase):
    def test_default_is_one_hot_at_lag(self):
        p = make_pool(recovery_lag_months=6)
        w = p.recovery_weights()
        self.assertEqual(len(w), 7)
        self.assertEqual(w[6], 1.0)
        self.assertEqual(sum(w[:6]), 0.0)
        self.assertAlmostEqual(sum(w), 1.0)

    def test_custom_distribution_is_normalized(self):
        p = replace(make_pool(), recovery_distribution=(0, 2.0, 1.0, 1.0))
        w = p.recovery_weights()
        self.assertAlmostEqual(sum(w), 1.0)
        self.assertAlmostEqual(w[1], 0.5)

    def test_negative_weights_are_clipped(self):
        p = replace(make_pool(), recovery_distribution=(-1.0, 2.0, 2.0))
        w = p.recovery_weights()
        self.assertEqual(w[0], 0.0)
        self.assertAlmostEqual(sum(w), 1.0)

    def test_all_zero_falls_back_to_lag(self):
        p = replace(make_pool(), recovery_distribution=(0.0, 0.0))
        self.assertEqual(p.recovery_weights(), make_pool().recovery_weights())


class TestRowwiseDegeneration(unittest.TestCase):
    def test_default_matches_explicit_one_hot_rowwise(self):
        """默认定点滞后 ≡ 显式 one-hot 分布，逐月逐字段完全相等。"""
        base = make_pool()
        onehot = replace(base, recovery_distribution=tuple([0.0] * 6 + [1.0]))
        a, b = build_schedule(base), build_schedule(onehot)
        self.assertEqual(len(a), len(b))
        for ra, rb in zip(a, b):
            self.assertEqual(ra.keys(), rb.keys())
            for key, val in ra.items():
                if isinstance(val, float):
                    self.assertAlmostEqual(
                        val, rb[key], delta=1e-9,
                        msg="month {} field {}".format(ra["month"], key))
                else:
                    self.assertEqual(
                        val, rb[key],
                        msg="month {} field {}".format(ra["month"], key))


class TestRecoveryConservation(unittest.TestCase):
    def test_total_recovery_equals_rate_times_charge_off(self):
        """任何分布下累计回收款 = 回收率 × 累计核销，一分不丢。"""
        dists = [
            (0, 1/6, 1/6, 1/6, 1/6, 1/6, 1/6),
            (0, 0.30, 0.25, 0.20, 0.12, 0.08, 0.05),
            (1.0,),
            (0.2, 0.8),
        ]
        for dist in dists:
            with self.subTest(dist=dist):
                p = replace(make_pool(), recovery_distribution=dist)
                rows = build_schedule(p)
                total_co = sum(r["charge_off"] for r in rows)
                total_rec = sum(r["recovery"] for r in rows)
                self.assertAlmostEqual(
                    total_rec, p.recovery_rate * total_co,
                    delta=max(1.0, total_rec * 1e-9))

    def test_tail_recovery_is_not_truncated(self):
        """余额归零之后到期的回收款必须完整入账，不能被 capping 截掉。"""
        p = make_pool(revolving_months=0, monthly_repayment_rate=0.30,
                      charge_off_rate=0.10, recovery_rate=0.50,
                      recovery_lag_months=20, amort_months=12)
        rows = build_schedule(p)
        tail = [r for r in rows if r["phase"] == "tail" and r["recovery"] > 0]
        self.assertTrue(tail, "余额归零后应仍有回收款入账")
        self.assertGreaterEqual(max(r["month"] for r in tail), 20)
        total_co = sum(r["charge_off"] for r in rows)
        total_rec = sum(r["recovery"] for r in rows)
        self.assertAlmostEqual(total_rec, 0.50 * total_co,
                               delta=max(1.0, total_rec * 1e-9))

    def test_k0_recovery_lands_in_same_month(self):
        """k=0 的回收应计入核销当月，不能被 pop 顺序漏掉。"""
        p = replace(make_pool(), recovery_distribution=(1.0,))
        rows = build_schedule(p)
        first = rows[0]
        self.assertGreater(first["recovery"], 0.0)
        self.assertAlmostEqual(first["recovery"],
                               first["charge_off"] * p.recovery_rate,
                               delta=1e-6)


class TestFeedbackLoop(unittest.TestCase):
    def test_distribution_changes_total_purchase(self):
        """回收时序会改变循环期购买额与总量——不能套用「只改时序」的两遍跑法。"""
        base = make_pool()
        front = replace(base, recovery_distribution=(0, 0.30, 0.25, 0.20,
                                                     0.12, 0.08, 0.05))
        a = sum(r["purchase"] for r in build_schedule(base))
        b = sum(r["purchase"] for r in build_schedule(front))
        self.assertNotEqual(a, b)
        self.assertGreater(abs(a - b), 1.0)


if __name__ == "__main__":
    unittest.main()
