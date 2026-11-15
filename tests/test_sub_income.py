"""次级期间收益（第 8 周 P1-4）。

条款依据：
  鼎柚说明书 p.47 (j)、p.92–93：次级按未偿本金 × 3%/年取期间收益，加速清偿或
  违约后不再支付；本金账户补足范围只到 p.47 的 (a)–(h)，不含 (j)。
  够花说明书 p.44 (9)、p.45 (1)：优先级在优先档利息之后、本金之前；不足部分
  下一期不予补足；本金账补足范围只到 p.43–44 的 (1)–(5)，不含 (9)。

核心性质（`available` 口径）：
    topup 只覆盖 senior_required，不能替次级付收益；
    sub_paid 只能来自收入账户自身的剩余；
    income_transfer = income_after_senior − sub_paid。

运行：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abs import Pool, Tranche, build_schedule, run_waterfall  # noqa: E402


def make_pool(**kw):
    base = dict(balance=1.5e9, annual_yield=0.21, monthly_repayment_rate=0.10,
                charge_off_rate=0.0, recovery_rate=0.0, recovery_lag_months=0,
                servicing_fee_rate=0.0, revolving_months=0, amort_months=12)
    base.update(kw)
    return Pool(**base)


def make_row(**kw):
    base = dict(month=1, phase="amortizing", bal_begin=0.0, interest=0.0,
                principal=0.0, gross_principal=0.0, recovery=0.0, purchase=0.0,
                charge_off=0.0, servicing_fee=0.0, tax=0.0,
                income_required=0.0, sub_income_due=0.0, sub_income_paid=0.0,
                income_transfer=0.0, prin_topup=0.0, prin_sweep=0.0,
                prin_acct=0.0, total_collection=0.0, bal_end=0.0,
                distribute_principal=True, cum_charge_off_rate=0.0,
                cum_purchased=0.0, cum_default_rate=0.0)
    base.update(kw)
    return base


class TestSubIncomeInCashflow(unittest.TestCase):
    def test_paid_before_transfer(self):
        """收入充足时，先付次级期间收益，剩下的才转入本金账户。"""
        p = make_pool(balance=1.5e9, annual_yield=0.21, revolving_months=6,
                      reinvest_rate=1.0, purchase_base="available",
                      security_cost_rate=0.02, sub_share=0.2,
                      sub_income_rate=0.03)
        r = build_schedule(p)[0]
        due = p.balance * 0.2 * 0.03 / 12.0
        self.assertAlmostEqual(r["sub_income_due"], due, delta=1e-6)
        self.assertAlmostEqual(r["sub_income_paid"], due, delta=1e-6)
        self.assertAlmostEqual(
            r["income_transfer"],
            r["interest"] + r["prin_topup"] - r["income_required"] - due,
            delta=1e-6)

    def test_topup_does_not_cover_sub_income(self):
        """本金账户只补优先档；收入不够时次级收益为 0，不能由本金垫付。"""
        p = make_pool(balance=1000.0, annual_yield=0.0, monthly_repayment_rate=0.20,
                      revolving_months=1, amort_months=3, reinvest_rate=0.0,
                      purchase_base="available", security_cost_rate=0.12,
                      sub_share=0.6, sub_income_rate=0.03)
        r = build_schedule(p)[0]
        # 证券利息 1000×12%/12 = 10，本金回收 200，利息收入 0
        self.assertAlmostEqual(r["income_required"], 10.0, delta=1e-9)
        self.assertAlmostEqual(r["prin_topup"], 10.0, delta=1e-9)
        self.assertAlmostEqual(r["sub_income_due"], 1.5, delta=1e-9)
        self.assertAlmostEqual(r["sub_income_paid"], 0.0, delta=1e-9)
        self.assertAlmostEqual(r["income_transfer"], 0.0, delta=1e-9)
        # 交给瀑布的只有 senior_required，不含次级
        self.assertAlmostEqual(r["total_collection"], 10.0, delta=1e-9)

    def test_carry_only_when_configured(self):
        """不足额顺延按 case 配置：鼎柚 True 上滚，够花 False 不补。"""
        kw = dict(balance=1000.0, annual_yield=0.0, monthly_repayment_rate=0.20,
                  revolving_months=3, amort_months=6, reinvest_rate=0.0,
                  purchase_base="available", security_cost_rate=0.0,
                  sub_share=0.9, sub_income_rate=0.12)
        carry = build_schedule(make_pool(sub_income_carry=True, **kw))
        nocarry = build_schedule(make_pool(sub_income_carry=False, **kw))
        base_due = 1000.0 * 0.9 * 0.12 / 12.0      # 9.0
        self.assertAlmostEqual(carry[0]["sub_income_due"], base_due, delta=1e-9)
        self.assertAlmostEqual(carry[1]["sub_income_due"], base_due * 2, delta=1e-9)
        self.assertAlmostEqual(nocarry[0]["sub_income_due"], base_due, delta=1e-9)
        self.assertAlmostEqual(nocarry[1]["sub_income_due"], base_due, delta=1e-9)
        self.assertAlmostEqual(carry[0]["sub_income_paid"], 0.0, delta=1e-9)

    def test_income_account_identity(self):
        """收入账户恒等式：利息 + 补足 = 优先应付 + 次级实付 + 转入。"""
        for mode in ("principal", "available"):
            for carry in (False, True):
                p = make_pool(revolving_months=4, annual_yield=0.18,
                              reinvest_rate=0.6, purchase_base=mode,
                              security_cost_rate=0.02, sub_share=0.2,
                              sub_income_rate=0.03, sub_income_carry=carry)
                for r in build_schedule(p):
                    if r["phase"] != "revolving":
                        break
                    # 定义式：两种口径都成立
                    self.assertAlmostEqual(
                        r["total_collection"],
                        r["interest"] + r["prin_topup"] - r["income_transfer"]
                        + r["principal"], delta=1e-6)
                    # 收入账户恒等式只在 available 口径建模
                    if mode == "available":
                        self.assertAlmostEqual(
                            r["interest"] + r["prin_topup"],
                            r["income_required"] + r["sub_income_paid"]
                            + r["income_transfer"], delta=1e-6)


class TestSubIncomeInWaterfall(unittest.TestCase):
    def _tranches(self, income_rate=0.36):
        return [Tranche(name="优先A", share=0.8, mode="fixed", coupon=0.0),
                Tranche(name="次级", share=0.2, mode="residual",
                        income_rate=income_rate)]

    def test_before_principal(self):
        """摊还期：次级期间收益排在优先档利息之后、本金之前。"""
        rows = [make_row(total_collection=200.0)]
        w = run_waterfall(rows, 1000.0, self._tranches())
        # 次级本金 200，月收益率 3% → 应付 6
        self.assertAlmostEqual(w.sub_income[0], 6.0, delta=1e-9)
        # 剩下的 194 才归优先A 本金；若顺序反了会是 200 和 0
        self.assertAlmostEqual(w.principal["优先A"][0], 194.0, delta=1e-9)

    def test_stops_after_acceleration(self):
        """加速清偿发生后不再支付次级期间收益。"""
        rows = [make_row(month=1, cum_charge_off_rate=0.0, total_collection=200.0),
                make_row(month=2, cum_charge_off_rate=0.90, total_collection=200.0),
                make_row(month=3, cum_charge_off_rate=0.90, total_collection=200.0)]
        w = run_waterfall(rows, 1000.0, self._tranches(), trigger_cdr=0.8)
        self.assertEqual(w.accel_month, 2)
        self.assertAlmostEqual(w.sub_income[0], 6.0, delta=1e-9)
        self.assertAlmostEqual(w.sub_income[2], 0.0, delta=1e-9)

    def test_stops_when_senior_fully_repaid(self):
        """优先档本金清偿完毕后不再产生次级期间收益。"""
        rows = [make_row(month=1, total_collection=1000.0),
                make_row(month=2, total_collection=1000.0)]
        w = run_waterfall(rows, 1000.0, self._tranches())
        self.assertAlmostEqual(w.principal["优先A"][0], 800.0, delta=1e-9)
        # 第 1 月付了次级收益，优先A 清零；第 2 月不再有次级期间收益
        self.assertAlmostEqual(w.sub_income[0], 6.0, delta=1e-9)
        self.assertAlmostEqual(w.sub_income[1], 0.0, delta=1e-9)


if __name__ == "__main__":
    unittest.main()
