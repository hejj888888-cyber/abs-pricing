"""账龄曲线的单元测试（第 2 周新增）。"""

import unittest

from abs import Pool
from abs.cashflow import build_schedule, summarize_pool
from abs.vintage import VintageCurve, from_dict


class TestVintageCurve(unittest.TestCase):
    def setUp(self):
        self.vc = VintageCurve(tau=8.0, shape=2.0)

    def test_cum_is_monotone_increasing(self):
        prev = -1.0
        for t in range(0, 40):
            cur = self.vc.cum(t)
            self.assertGreaterEqual(cur, prev, f"累计曲线在 t={t} 处下降")
            prev = cur

    def test_cum_starts_at_zero_and_saturates(self):
        self.assertEqual(self.vc.cum(0), 0.0)
        self.assertLess(self.vc.cum(-1), 0.0 or True)  # 负账龄返回 0
        self.assertEqual(self.vc.cum(-1), 0.0)
        self.assertGreater(self.vc.cum(60), 0.999)
        self.assertLessEqual(self.vc.cum(60), 1.0)

    def test_marginal_peaks_then_declines(self):
        """边际违约率先升后降——'衰减'发生在边际上，不在累计上。"""
        peak = int(round(self.vc.peak_month()))
        self.assertTrue(3 <= peak <= 8, f"峰值月 {peak} 不在合理区间")
        before = self.vc.marginal(peak - 1)
        at = self.vc.marginal(peak)
        after = self.vc.marginal(peak + 3)
        self.assertGreater(at, before)
        self.assertGreater(at, after)

    def test_weights_sum_to_one(self):
        for n in (12, 29, 52):
            self.assertAlmostEqual(sum(self.vc.weights(n)), 1.0, places=10)

    def test_amounts_preserve_total(self):
        total = 1_234_567.0
        self.assertAlmostEqual(sum(self.vc.amounts(30, total)), total, places=4)

    def test_allocate_pool_start_shifts_by_seasoning(self):
        """入池起算：seasoning 越大，首月核销越接近峰值。"""
        total = 1e6
        young = self.vc.allocate(24, total, revolving_months=6, seasoning_months=0)
        aged = VintageCurve(tau=8.0, shape=2.0, anchor="pool_start").allocate(
            24, total, revolving_months=6, seasoning_months=6)
        self.assertGreater(aged[0], young[0], "已 seasoning 的池首月核销应更高")

    def test_allocate_amort_start_zeros_revolving(self):
        total = 1e6
        amt = VintageCurve(tau=8.0, shape=2.0, anchor="amort_start").allocate(
            24, total, revolving_months=6, seasoning_months=0)
        self.assertEqual(sum(amt[:6]), 0.0, "摊还起算口径下循环期不应计提核销")
        self.assertGreater(sum(amt[6:]), 0.0)

    def test_from_dict_disabled(self):
        self.assertIsNone(from_dict(None))
        self.assertIsNone(from_dict({"enabled": False}))

    def test_from_dict_enabled(self):
        vc = from_dict({"enabled": True, "tau": 6.0, "shape": 3.0, "anchor": "amort_start"})
        self.assertIsNotNone(vc)
        self.assertEqual(vc.tau, 6.0)
        self.assertEqual(vc.shape, 3.0)
        self.assertEqual(vc.anchor, "amort_start")


class TestVintageInCashflow(unittest.TestCase):
    def setUp(self):
        self.pool = Pool(
            balance=1_500_000_000,
            annual_yield=0.2115,
            monthly_repayment_rate=0.1987,
            charge_off_rate=0.0314,
            recovery_rate=0.0331,
            recovery_lag_months=6,
            servicing_fee_rate=0.005,
            revolving_months=8,
            amort_months=13,
            seasoning_months=5,
        )

    def test_total_charge_off_preserved(self):
        """核心性质：曲线只重整时序，不改变损失总量。

        允许 0.01% 的相对误差——两遍跑法下末期余额可能先归零，
        导致当年最后几个月的预算额度被 capping 掉一小部分。
        """
        base = build_schedule(self.pool)
        curve = build_schedule(self.pool, vintage=VintageCurve(tau=8.0, shape=2.0))
        b = sum(r["charge_off"] for r in base)
        c = sum(r["charge_off"] for r in curve)
        self.assertAlmostEqual(b, c, delta=b * 1e-4)

    def test_timing_actually_changes(self):
        """总量相同，但逐月分布必须不同，否则说明曲线没生效。"""
        base = build_schedule(self.pool)
        curve = build_schedule(self.pool, vintage=VintageCurve(tau=8.0, shape=2.0))
        n = min(len(base), len(curve))
        diffs = [abs(base[i]["charge_off"] - curve[i]["charge_off"]) for i in range(n)]
        self.assertGreater(max(diffs), 1e5, "曲线模式下逐月核销应与恒定模式有明显差异")

    def test_balance_never_negative(self):
        rows = build_schedule(self.pool, vintage=VintageCurve(tau=8.0, shape=2.0))
        for r in rows:
            self.assertGreaterEqual(r["bal_end"], -1e-6, f"第 {r['month']} 月余额为负")

    def test_summary_runs(self):
        rows = build_schedule(self.pool, vintage=VintageCurve(tau=8.0, shape=2.0))
        s = summarize_pool(rows, self.pool)
        self.assertGreater(s["累计核销率"], 0.0)
        self.assertLess(s["累计核销率"], 0.2)


if __name__ == "__main__":
    unittest.main()
