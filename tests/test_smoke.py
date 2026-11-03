"""冒烟测试：模型里最容易错的四件事——换算口径、现金流守恒、反解收敛、结构单调性。

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
    base = dict(balance=1.5e9, annual_yield=0.2115, monthly_repayment_rate=0.1987,
                charge_off_rate=0.0314, recovery_rate=0.10, recovery_lag_months=6,
                servicing_fee_rate=0.005, revolving_months=6, amort_months=12)
    base.update(kw)
    return Pool(**base)


def sample_tranches():
    return [
        Tranche("优先A", 0.7444, "AAA", "solve", 0.0205, reference_yield=0.015),
        Tranche("优先B", 0.0579, "AA+", "solve", 0.0230, reference_yield=0.015),
        Tranche("优先C", 0.1031, "A+", "solve", 0.0450, reference_yield=0.015),
        Tranche("次级", 0.0925, "未评级", "residual", reference_yield=0.015, income_rate=0.03),
    ]


class TestPool(unittest.TestCase):
    def test_charge_off_is_lifetime_cumulative(self):
        """核销率是**累计终身**口径，不是年化率。

        联合资信评级方法 p.5：核销率 = 正常还款周期 90 天后仍逾期的未偿本金余额 /
        静态资产池初始放款金额。分子是最终核销掉的本金，故累计核销量应等于
        「核销率 × 初始放款金额」。第一版按年化率拆月，累计损失只有这个数的一半。
        """
        p = sample_pool(charge_off_rate=0.1727, monthly_repayment_rate=0.0994,
                        revolving_months=0, recovery_rate=0.0)
        rows = build_schedule(p)
        total_co = sum(r["charge_off"] for r in rows)
        self.assertAlmostEqual(total_co, 0.1727 * p.balance,
                               delta=0.01 * 0.1727 * p.balance)

    def test_zero_charge_off(self):
        p = sample_pool(charge_off_rate=0.0)
        rows = build_schedule(p)
        self.assertAlmostEqual(sum(r["charge_off"] for r in rows), 0.0, delta=1e-6)


class TestCashflow(unittest.TestCase):
    def test_full_reinvest_keeps_balance_when_no_loss(self):
        """购买率 100% 且无损失时，循环期内资产池余额保持不变、不向证券端过手本金。"""
        p = sample_pool(revolving_months=6, charge_off_rate=0.0, recovery_rate=0.0)
        rows = build_schedule(p)
        rev = [r for r in rows if r["phase"] == "revolving"]
        self.assertEqual(len(rev), 6)
        for r in rev:
            self.assertAlmostEqual(r["bal_end"], p.balance, delta=1.0)
            self.assertAlmostEqual(r["principal"], 0.0, delta=1.0)

    def test_reinvest_replaces_repaid_principal_only(self):
        """持续购买只替换已偿还本金，不补足核销掉的本金——余额按累计核销递减。"""
        p = sample_pool(revolving_months=6, charge_off_rate=0.10, recovery_rate=0.0)
        rows = build_schedule(p)
        rev = [r for r in rows if r["phase"] == "revolving"]
        last = rev[-1]
        cum_co = sum(r["charge_off"] for r in rev)
        self.assertAlmostEqual(last["bal_end"], p.balance - cum_co, delta=1.0)

    def test_amortization_drains_balance(self):
        p = sample_pool()
        rows = build_schedule(p)
        self.assertLess(rows[-1]["bal_end"], 1.0)

    def test_no_revolving_flag(self):
        p = sample_pool(revolving_months=0)
        rows = build_schedule(p)
        self.assertTrue(all(r["phase"] != "revolving" for r in rows))

    def test_balance_monotonic_after_revolving(self):
        rows = build_schedule(sample_pool())
        amort = [r for r in rows if r["phase"] == "amortizing"]
        for prev, cur in zip(amort, amort[1:]):
            self.assertLessEqual(cur["bal_begin"], prev["bal_begin"] + 1e-6)

    def test_repayment_rate_is_on_initial_not_current_balance(self):
        """月还款率的分母是静态池初始放款金额，不是当期余额。

        联合资信评级方法 p.5：月还款率 = 静态池当月收到的本金 / 静态池初始放款金额。
        所以每月本金回款额近似常数（水平偿还），不是几何衰减。这个区别决定了
        优先档清偿月对不对得上披露的预期到期日。
        """
        p = sample_pool(revolving_months=0, charge_off_rate=0.0, recovery_rate=0.0)
        rows = build_schedule(p)
        amort = [r["principal"] for r in rows if r["phase"] == "amortizing"]
        self.assertAlmostEqual(amort[0] / p.balance, p.monthly_repayment_rate, places=9)
        # 水平偿还：只要余额还够，后续月份回款额与首月相同（几何衰减会逐月变小）
        for v in amort[:-1]:
            self.assertAlmostEqual(v, amort[0], delta=1.0)
        # 池子应在约 1/月还款率 个月内摊完，而不是永远摊不完
        self.assertLessEqual(len(amort), int(1.0 / p.monthly_repayment_rate) + 2)

    def test_cum_default_rate_denominator_includes_purchases(self):
        """合同口径的累计违约率分母 = 初始余额 + 累计持续购买本金（累加不扣减）。"""
        p = sample_pool(revolving_months=6)
        rows = build_schedule(p)
        rev = [r for r in rows if r["phase"] == "revolving"]
        last = rev[-1]
        self.assertGreater(last["cum_purchased"], p.balance * 0.5)
        # 分母膨胀 → 合同口径显著低于"占初始余额"口径
        self.assertLess(last["cum_default_rate"], last["cum_charge_off_rate"] * 0.7)


class TestWaterfall(unittest.TestCase):
    def test_conservation(self):
        pool = sample_pool()
        rows = build_schedule(pool)
        res = run_waterfall(rows, pool.balance, sample_tranches())
        inflow = sum(r["total_collection"] for r in rows)
        fee = sum(r["servicing_fee"] for r in rows)
        paid = sum(sum(v) for v in res.cashflow.values())
        self.assertAlmostEqual(inflow - fee, paid, delta=max(1.0, inflow * 1e-8))

    def test_senior_gets_principal_first(self):
        pool = sample_pool()
        rows = build_schedule(pool)
        res = run_waterfall(rows, pool.balance, sample_tranches(), pay_mode="sequential")
        self.assertAlmostEqual(res.unpaid_principal["优先A"], 0.0, delta=1.0)

    def test_notional_sums_to_pool(self):
        pool = sample_pool()
        trs = sample_tranches()
        res = run_waterfall(build_schedule(pool), pool.balance, trs)
        self.assertAlmostEqual(sum(res.notional.values()), pool.balance * sum(t.share for t in trs), delta=1.0)

    def test_residual_income_capped(self):
        """次级期间收益封顶：income_rate>0 时存续期内有利息性现金流；=0 时利息性现金流全部留到最后释放。"""
        pool = sample_pool()
        rows = build_schedule(pool)

        trs_cap = sample_tranches()
        res_cap = run_waterfall(rows, pool.balance, trs_cap)
        interim_cap = sum(res_cap.cashflow["次级"][:-1]) - sum(res_cap.principal["次级"][:-1])

        trs_no = sample_tranches()
        trs_no[-1].income_rate = 0.0
        res_no = run_waterfall(rows, pool.balance, trs_no)
        interim_no = sum(res_no.cashflow["次级"][:-1]) - sum(res_no.principal["次级"][:-1])

        self.assertGreater(interim_cap, 0)
        self.assertAlmostEqual(interim_no, 0.0, delta=1.0)


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

    def test_principal_is_fully_distributed_when_no_loss(self):
        """无核销、无回收时，各档本金合计应等于资产池本金。"""
        pool = sample_pool(charge_off_rate=0.0, recovery_rate=0.0)
        trs = sample_tranches()
        rows = build_schedule(pool)
        res = run_waterfall(rows, pool.balance, trs)
        total_principal = sum(sum(v) for v in res.principal.values())
        expected = pool.balance * sum(t.share for t in trs)
        self.assertAlmostEqual(total_principal, expected, delta=1.0)


class TestStructure(unittest.TestCase):
    def test_min_subordination_feasible(self):
        pool = sample_pool(charge_off_rate=0.10, recovery_rate=0.05)
        rows = build_schedule(pool)
        trs = sample_tranches()
        s, _ = min_subordination(rows, pool.balance, trs)
        self.assertIsNotNone(s)
        self.assertGreater(s, 0.0)
        self.assertLess(s, 0.45)

    def test_more_subordination_never_hurts(self):
        pool = sample_pool(charge_off_rate=0.12, recovery_rate=0.05)
        rows = build_schedule(pool)
        losses = []
        for sub_share in (0.04, 0.10, 0.20):
            trs = sample_tranches()
            mid = sum(t.share for t in trs[1:-1])
            trs[0].share = 1 - sub_share - mid
            trs[-1].share = sub_share
            solve_coupons(rows, pool.balance, trs)
            res = run_waterfall(rows, pool.balance, trs)
            losses.append(res.unpaid_principal["优先A"] / res.notional["优先A"])
        self.assertGreaterEqual(losses[0] + 1e-9, losses[1])
        self.assertGreaterEqual(losses[1] + 1e-9, losses[2])


if __name__ == "__main__":
    unittest.main()
