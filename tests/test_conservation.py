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

from abs import Pool, Tranche, build_schedule, run_waterfall  # noqa: E402


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


class TestPurchaseBase(unittest.TestCase):
    """购买资金口径（第 6 周）：principal vs available。

    依据：评级方法 p.6「购买率 = 当月新增资产 / 上月可用于购买的资金」；两单说明书
    都写明持续购买期内「收入账户剩余转入本金账户」，且未用完的留存滚入下一期。
    见 `docs/purchase-base.md`。
    """

    def test_available_is_not_smaller(self):
        """含收入转入与留存的口径，累计购买不可能更少。"""
        base = make_pool(revolving_months=6, reinvest_rate=0.5,
                         security_cost_rate=0.02)
        from dataclasses import replace
        a = build_schedule(replace(base, purchase_base="principal"))
        b = build_schedule(replace(base, purchase_base="available"))
        buy_a = sum(r["purchase"] for r in a)
        buy_b = sum(r["purchase"] for r in b)
        self.assertGreaterEqual(buy_b, buy_a - 1.0)
        self.assertGreater(buy_b, 0.0)

    def test_available_keeps_loss_conservation(self):
        """换口径不能破坏守恒：累计核销仍等于 co_final × 累计放款。"""
        p = make_pool(revolving_months=6, reinvest_rate=0.6,
                      security_cost_rate=0.02, purchase_base="available")
        rows = build_schedule(p, co_mult=2.0, ramp_months=4)
        purchased = sum(r["purchase"] for r in rows)
        expected = 0.05 * 2.0 * (p.balance + purchased)
        self.assertAlmostEqual(cum_charge_off(rows), expected, delta=expected * 1e-9)

    def test_income_transfer_only_counts_remainder(self):
        """收入账户转入 = 利息 − 服务费 − 税费 − 证券利息，不能为负。"""
        p = make_pool(revolving_months=6, reinvest_rate=0.0,
                      security_cost_rate=0.02, purchase_base="available")
        rows = build_schedule(p)
        for r in rows:
            if r["phase"] != "revolving":
                continue
            cost = r["servicing_fee"] + r["tax"] + p.balance * 0.02 / 12.0
            self.assertGreaterEqual(r["income_transfer"], 0.0)
            self.assertAlmostEqual(r["income_transfer"],
                                   max(r["interest"] - cost, 0.0),
                                   delta=max(r["interest"], 1.0) * 1e-9)

    def test_first_amort_month_defers_principal(self):
        """摊还期首个只付息日不分本金，回款留存在本金分账户、次月一并过手。"""
        p = make_pool(revolving_months=4, reinvest_rate=0.6,
                      revolving_first_pay_interest_only=True)
        rows = build_schedule(p)
        first_amort = next(r for r in rows if r["phase"] == "amortizing")
        self.assertEqual(first_amort["principal"], 0.0)
        self.assertGreater(first_amort["prin_acct"], 0.0)
        self.assertGreater(rows[first_amort["month"]]["principal"], 0.0)


class TestPurchaseAccountTransfers(unittest.TestCase):
    """第 7 周 P0-D：收入分账户 / 本金分账户划转必须两边同时记账。

    修复前有两面错误（见 `docs/purchase-base.md` 第八节）：

    1. `topup` 从本金账户划走却没交给瀑布，现金凭空消失，瀑布把「账上有钱
       却被划走」误判成优先档利息未足额偿付；
    2. `income_transfer` 拿去买资产的同时，完整 `interest` 仍进入瀑布形成
       `reserve`，同一笔剩余收入被使用两次。

    现有 `check_conservation` 抓不到第 1 面，因为等式两边同时漏记 topup。
    下面用 Codex 给出的最小参数（1000 元池、首月回收 200、资产收益 0、
    证券成本 12%/年）复现，并补上账户子账恒等式。
    """

    def _pool(self, **kw):
        base = dict(balance=1000.0, annual_yield=0.0,
                    monthly_repayment_rate=0.20, charge_off_rate=0.0,
                    recovery_rate=0.0, recovery_lag_months=0,
                    servicing_fee_rate=0.0, revolving_months=1,
                    amort_months=3, reinvest_rate=0.0,
                    purchase_base="available", security_cost_rate=0.12)
        base.update(kw)
        return make_pool(**base)

    def _senior(self, coupon):
        return Tranche(name="优先A", share=1.0, mode="fixed", coupon=coupon)

    def test_minimal_repro_topup_reaches_waterfall(self):
        """最小复现：补足 10 元必须出现在交给瀑布的现金里。"""
        p = self._pool()
        r = build_schedule(p)[0]
        self.assertAlmostEqual(r["gross_principal"], 200.0, delta=1e-9)
        self.assertAlmostEqual(r["interest"], 0.0, delta=1e-9)
        self.assertAlmostEqual(r["income_required"], 10.0, delta=1e-9)
        self.assertAlmostEqual(r["prin_topup"], 10.0, delta=1e-9)
        self.assertAlmostEqual(r["income_transfer"], 0.0, delta=1e-9)
        self.assertAlmostEqual(r["total_collection"], 10.0, delta=1e-9)
        self.assertAlmostEqual(r["prin_acct"], 190.0, delta=1e-9)

        # 修复前 total_collection=0，瀑布会累计约 10.33 元的假欠息
        w = run_waterfall([r], p.balance, [self._senior(0.12)])
        self.assertAlmostEqual(w.unpaid_interest["优先A"], 0.0, delta=1e-9)

    def test_principal_sufficient_no_false_shortfall_across_revolving(self):
        """本金账户逐期累积、足够补足时，整个循环期都不该出现假欠息。

        同时验证证券利息按初始发行规模计提：队列摊还后 `live` 递减，若仍用
        `live × security_cost_rate` 近似，交给瀑布的钱会少于应付利息而误报欠息。
        """
        p = self._pool(monthly_repayment_rate=0.05, revolving_months=4,
                       amort_months=24)
        rows = build_schedule(p)
        rev = [r for r in rows if r["phase"] == "revolving"]
        self.assertEqual(len(rev), 4)
        for r in rev:
            self.assertAlmostEqual(r["total_collection"], r["income_required"],
                                   delta=1e-9)
            self.assertAlmostEqual(r["prin_topup"], r["income_required"],
                                   delta=1e-9)
            self.assertAlmostEqual(r["income_transfer"], 0.0, delta=1e-9)
            self.assertGreater(r["prin_acct"], 0.0)
        w = run_waterfall(rev, p.balance, [self._senior(0.12)])
        self.assertAlmostEqual(w.unpaid_interest["优先A"], 0.0, delta=1e-9)

    def test_real_shortfall_still_fires_when_principal_insufficient(self):
        """本金账户真的不够时，缺口必须保留下来，不能被划转抹平。"""
        p = self._pool(monthly_repayment_rate=0.05, security_cost_rate=1.2)
        r = build_schedule(p)[0]
        self.assertAlmostEqual(r["income_required"], 100.0, delta=1e-9)
        self.assertAlmostEqual(r["prin_topup"], 50.0, delta=1e-9)
        self.assertAlmostEqual(r["total_collection"], 50.0, delta=1e-9)
        self.assertLess(r["total_collection"], r["income_required"])
        w = run_waterfall([r], p.balance, [self._senior(1.2)])
        self.assertGreater(w.unpaid_interest["优先A"], 0.0)

    def test_topup_capped_by_principal_cash(self):
        """补足款不得超过本金账户当期可用现金，账户不能垫成负数。"""
        p = self._pool(monthly_repayment_rate=0.05, revolving_months=4,
                       amort_months=24, security_cost_rate=1.2)
        prev = 0.0
        for r in build_schedule(p):
            if r["phase"] != "revolving":
                break
            cash = prev + r["gross_principal"] + r["recovery"]
            self.assertLessEqual(r["prin_topup"], cash + 1e-9)
            self.assertGreaterEqual(r["prin_acct"], -1e-9)
            prev = r["prin_acct"]

    def test_income_transfer_excluded_from_waterfall(self):
        """正方向：收入剩余可买资产，但不能同时留在瀑布里形成 reserve。"""
        p = self._pool(annual_yield=0.24, security_cost_rate=0.0,
                       reinvest_rate=1.0)
        r = build_schedule(p)[0]
        self.assertAlmostEqual(r["interest"], 20.0, delta=1e-9)
        self.assertAlmostEqual(r["income_transfer"], 20.0, delta=1e-9)
        self.assertAlmostEqual(r["purchase"], 220.0, delta=1e-9)
        # 修复前这里是 20（完整利息），与买资产的 20 重复
        self.assertAlmostEqual(r["total_collection"], 0.0, delta=1e-9)
        w = run_waterfall([r], p.balance, [self._senior(0.0)])
        self.assertAlmostEqual(w.reserve, 0.0, delta=1e-9)

    def test_subaccount_identities(self):
        """两个账户的子账恒等式，逐月成立（两种口径都验）。"""
        for mode in ("principal", "available"):
            p = self._pool(annual_yield=0.18, revolving_months=4,
                           amort_months=24, reinvest_rate=0.6,
                           purchase_base=mode)
            prev = 0.0
            for r in build_schedule(p):
                if r["phase"] != "revolving":
                    break
                # 交给瀑布的钱 = 收入账户净额 + 分摊本金
                self.assertAlmostEqual(
                    r["total_collection"],
                    r["interest"] + r["prin_topup"] - r["income_transfer"]
                    + r["principal"], delta=1e-9)
                # 本金账户：流入 = 补足 + 购买 + 月末留存
                cash = prev + r["gross_principal"] + r["recovery"]
                self.assertAlmostEqual(
                    cash + r["income_transfer"],
                    r["prin_topup"] + r["purchase"] + r["prin_acct"],
                    delta=1e-9)
                prev = r["prin_acct"]

    def test_principal_path_rowwise_unchanged(self):
        """默认口径不启用划转，三个字段恒为 0，total_collection 与历史一致。"""
        p = make_pool(revolving_months=6, reinvest_rate=0.6,
                      security_cost_rate=0.02, purchase_base="principal")
        rows = build_schedule(p, co_mult=2.0, ramp_months=4)
        for r in rows:
            self.assertEqual(r["income_required"], 0.0)
            self.assertEqual(r["income_transfer"], 0.0)
            self.assertEqual(r["prin_topup"], 0.0)
            self.assertAlmostEqual(r["total_collection"],
                                   r["interest"] + r["principal"], delta=1e-9)

    def test_cash_conservation_with_waterfall(self):
        """账户划转修复后，全期现金守恒仍精确成立。"""
        p = self._pool()
        rows = build_schedule(p)
        w = run_waterfall(rows, p.balance, [self._senior(0.12)])
        inflow = sum(r["total_collection"] for r in rows)
        fee = sum(r["servicing_fee"] for r in rows)
        tax = sum(r["tax"] for r in rows)
        paid = sum(sum(v) for v in w.cashflow.values())
        self.assertAlmostEqual(inflow - fee - tax - paid, 0.0, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
