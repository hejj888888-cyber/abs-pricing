"""冒烟测试：模型里最容易错的三件事——年金公式、现金流守恒、反解收敛。

运行：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abs import Pool, Tranche, build_schedule, run_waterfall, solve_coupons, tranche_metrics
from abs.structure import min_subordination


def sample_pool(**kw):
    base = dict(balance=1e9, wac=0.18, term_months=24, cpr=0.15, cdr=0.06,
                recovery_rate=0.25, recovery_lag_months=6, servicing_fee_rate=0.015)
    base.update(kw)
    return Pool(**base)


def sample_tranches():
    return [
        Tranche("优先A", 0.75, "AAA", "solve", 0.032, reference_yield=0.02),
        Tranche("夹层", 0.13, "AA", "solve", 0.055, reference_yield=0.02),
        Tranche("次级", 0.12, "未评级", "residual", reference_yield=0.02),
    ]


class TestPool(unittest.TestCase):
    def test_annuity_zero_rate(self):
        p = sample_pool(wac=0.0)
        self.assertAlmostEqual(p.annuity_payment(), p.balance / p.term_months)

    def test_smm_zero_cpr(self):
        p = sample_pool(cpr=0.0)
        self.assertEqual(p.smm(), 0.0)

    def test_smm_cpr_consistency(self):
        p = sample_pool(cpr=0.15)
        self.assertAlmostEqual((1 - p.smm()) ** 12, 1 - 0.15, places=10)


class TestCashflow(unittest.TestCase):
    def test_no_prepay_no_default(self):
        """CPR=CDR=0 时，本金应全部收回且余额归零。"""
        p = sample_pool(cpr=0.0, cdr=0.0)
        rows = build_schedule(p)
        principal = sum(r["sched_prin"] for r in rows)
        self.assertAlmostEqual(principal, p.balance, delta=1.0)
        self.assertAlmostEqual(rows[-1]["bal_end"], 0.0, delta=1.0)

    def test_balance_monotonic(self):
        rows = build_schedule(sample_pool())
        for prev, cur in zip(rows, rows[1:]):
            self.assertLessEqual(cur["bal_begin"], prev["bal_begin"] + 1e-6)


class TestWaterfall(unittest.TestCase):
    def test_conservation(self):
        """池收回总额 = 服务费 + 各档分得现金。"""
        pool = sample_pool()
        rows = build_schedule(pool)
        trs = sample_tranches()
        res = run_waterfall(rows, pool.balance, trs)
        inflow = sum(r["total_collection"] for r in rows)
        fee = sum(r["servicing_fee"] for r in rows)
        paid = sum(sum(v) for v in res.cashflow.values())
        self.assertAlmostEqual(inflow - fee, paid, delta=max(1.0, inflow * 1e-9))

    def test_senior_gets_principal_first(self):
        pool = sample_pool()
        rows = build_schedule(pool)
        trs = sample_tranches()
        res = run_waterfall(rows, pool.balance, trs, pay_mode="sequential")
        self.assertAlmostEqual(res.unpaid_principal["优先A"], 0.0, delta=1.0)

    def test_totals_match_notional(self):
        pool = sample_pool()
        rows = build_schedule(pool)
        trs = sample_tranches()
        res = run_waterfall(rows, pool.balance, trs)
        total = sum(res.notional.values())
        self.assertAlmostEqual(total, pool.balance, delta=1.0)


class TestPricing(unittest.TestCase):
    def test_coupon_solve_hits_target(self):
        pool = sample_pool()
        trs = sample_tranches()
        rows = build_schedule(pool)
        solve_coupons(rows, pool.balance, trs)
        res = run_waterfall(rows, pool.balance, trs)
        for t in trs:
            if t.mode != "solve":
                continue
            m = tranche_metrics(res, t)
            self.assertAlmostEqual(m["实现收益率"], t.target_yield, places=3,
                                   msg=f"{t.name} 反解未命中目标收益率")

    def test_more_subordination_never_hurts(self):
        """次级越厚，夹层的损失不应变大。"""
        pool = sample_pool(cdr=0.18, recovery_rate=0.10)
        rows = build_schedule(pool)
        losses = []
        for sub_share in (0.04, 0.08, 0.16):
            trs = sample_tranches()
            trs[0].share = 1 - sub_share - trs[1].share
            trs[2].share = sub_share
            solve_coupons(rows, pool.balance, trs)
            res = run_waterfall(rows, pool.balance, trs)
            losses.append(res.unpaid_principal["夹层"] / res.notional["夹层"])
        self.assertGreaterEqual(losses[0], losses[1] - 1e-9)
        self.assertGreaterEqual(losses[1], losses[2] - 1e-9)


class TestStructure(unittest.TestCase):
    def test_min_subordination_feasible(self):
        pool = sample_pool(cdr=0.20, recovery_rate=0.10)
        rows = build_schedule(pool)
        trs = sample_tranches()
        solve_coupons(rows, pool.balance, trs)
        s, _ = min_subordination(rows, pool.balance, trs)
        self.assertIsNotNone(s)
        self.assertGreater(s, 0.0)
        self.assertLess(s, 0.40)


if __name__ == "__main__":
    unittest.main()
