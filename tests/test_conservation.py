"""累计核销守恒测试（第 4 周 P0-1 修复）。

对应 `docs/loss-conservation.md`。核心性质：**终身核销总量与确认时序彻底分离**——
每个队列的累计核销锁死为 `co_final × face`，ramp 与 vintage 只能改变分摊月份。

修复前这两个自由度是混在一起的：队列寿命用 `co_of(born_month)` 定、月度核销用
`co_of(m)` 算，再由 `p + d > rem` 同比例裁剪，导致累计核销比 `co_final × 累计放款`
少 2.4%（基准）到 22.1%（AAAsf）。

运行：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abs import Pool, build_schedule  # noqa: E402


def make_pool(**kw):
    base = dict(balance=1.5e9, annual_yield=0.2115, monthly_repayment_rate=0.10,
                charge_off_rate=0.05, recovery_rate=0.03, recovery_lag_months=6,
                servicing_fee_rate=0.005, revolving_months=0, amort_months=12)
    base.update(kw)
    return Pool(**base)


def cum_charge_off(rows):
    return sum(r["charge_off"] for r in rows)


class TestLossConservation(unittest.TestCase):
    def test_no_revolving_exact(self):
        p = make_pool()
        rows = build_schedule(p)
        self.assertAlmostEqual(cum_charge_off(rows), p.charge_off_rate * p.balance,
                               delta=p.balance * 1e-9)

    def test_stressed_exact(self):
        """压力乘数下累计核销仍精确等于 co_final × 期初余额。"""
        p = make_pool()
        rows = build_schedule(p, co_mult=4.0, mrr_mult=0.5, ramp_months=4)
        self.assertAlmostEqual(cum_charge_off(rows), 0.05 * 4.0 * p.balance,
                               delta=p.balance * 1e-9)

    def test_with_revolving_exact(self):
        """循环购买下累计核销 = co_final × (初始池 + 累计购买)。"""
        p = make_pool(revolving_months=6, reinvest_rate=1.0)
        rows = build_schedule(p, co_mult=3.0, ramp_months=4)
        purchased = sum(r["purchase"] for r in rows)
        expected = 0.05 * 3.0 * (p.balance + purchased)
        self.assertAlmostEqual(cum_charge_off(rows), expected,
                               delta=expected * 1e-9)

    def test_high_loss_rate_still_exact(self):
        """高核销率（余额被损失吃掉大半）时不得出现 capping 型缺口。"""
        p = make_pool(charge_off_rate=0.30, monthly_repayment_rate=0.12)
        rows = build_schedule(p)
        self.assertAlmostEqual(cum_charge_off(rows), 0.30 * p.balance,
                               delta=p.balance * 1e-9)


class TestRampOnlyChangesTiming(unittest.TestCase):
    def test_ramp_preserves_total(self):
        """ramp 只决定何时确认，不改变终身总量。"""
        p = make_pool()
        flat = build_schedule(p, co_mult=3.0, ramp_months=0)
        ramp = build_schedule(p, co_mult=3.0, ramp_months=4)
        self.assertAlmostEqual(cum_charge_off(flat), cum_charge_off(ramp),
                               delta=p.balance * 1e-9)

    def test_ramp_actually_shifts_timing(self):
        """总量相同，但逐月分布必须不同，否则说明 ramp 没生效。"""
        p = make_pool()
        flat = build_schedule(p, co_mult=3.0, ramp_months=0)
        ramp = build_schedule(p, co_mult=3.0, ramp_months=4)
        diffs = [abs(a["charge_off"] - b["charge_off"])
                 for a, b in zip(flat, ramp)]
        self.assertGreater(max(diffs), 0.0)


class TestQueueFullyAmortizes(unittest.TestCase):
    def test_balance_reaches_zero(self):
        """队列本金目标与损失目标都摊完后，余额必须归零，不留残值。"""
        p = make_pool(revolving_months=0)
        rows = build_schedule(p)
        self.assertAlmostEqual(rows[-1]["bal_end"], 0.0, delta=p.balance * 1e-9)

    def test_principal_plus_loss_equals_face(self):
        """期初余额 = 累计本金回收 + 累计核销（无循环购买、无回收款时）。"""
        p = make_pool(revolving_months=0, recovery_rate=0.0)
        rows = build_schedule(p)
        principal = sum(r["gross_principal"] for r in rows)
        loss = cum_charge_off(rows)
        self.assertAlmostEqual(principal + loss, p.balance, delta=p.balance * 1e-9)


if __name__ == "__main__":
    unittest.main()
