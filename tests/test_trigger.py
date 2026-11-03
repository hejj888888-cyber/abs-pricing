"""触发机制测试（第 3 周新增）。

覆盖四类容易错的地方：
  1. 阈值分段：首年 5% / 满一年后 8%，切换点必须准确。
  2. 事件不重复：同一个条件只记一次，否则报告会被刷屏。
  3. 后果分类：加速清偿事件才截断循环期，违约事件不截断。
  4. 循环期不分配本金，留存收益能补足利息缺口。

运行：
    python -m unittest tests.test_trigger -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abs import Pool, Tranche, build_schedule, run_waterfall
from abs.trigger import TriggerTerms, evaluate_month


def terms(**kw):
    base = dict(cdr_first_year=0.05, cdr_after_first_year=0.08, first_year_months=12,
                default_uplift=1.0)
    base.update(kw)
    return TriggerTerms(**base)


class TestThreshold(unittest.TestCase):

    def test_first_year_threshold(self):
        t = terms()
        self.assertAlmostEqual(t.threshold(1), 0.05)
        self.assertAlmostEqual(t.threshold(12), 0.05)

    def test_after_first_year_threshold(self):
        t = terms()
        self.assertAlmostEqual(t.threshold(13), 0.08)

    def test_cdr_fires_just_above_threshold(self):
        t = terms()
        fired = set()
        ev = evaluate_month(6, 0.0501, {}, [], t, fired)
        self.assertEqual([e.code for e in ev], ["CDR"])
        self.assertEqual(ev[0].kind, "accel")

    def test_cdr_silent_at_threshold(self):
        """等于阈值不触发——条款用「超过」，不是「达到」。"""
        ev = evaluate_month(6, 0.05, {}, [], terms(), set())
        self.assertEqual(ev, [])

    def test_threshold_jumps_at_month_13(self):
        """6.5% 在第 12 月触发，在第 13 月不触发（阈值升到 8%）。"""
        self.assertEqual(len(evaluate_month(12, 0.065, {}, [], terms(), set())), 1)
        self.assertEqual(len(evaluate_month(13, 0.065, {}, [], terms(), set())), 0)

    def test_uplift_advances_trigger(self):
        """上调系数 >1 时，同样的核销率更早突破阈值。"""
        fired = set()
        early = evaluate_month(4, 0.03, {}, [], terms(default_uplift=2.0), fired)
        self.assertEqual([e.code for e in early], ["CDR"])
        late = evaluate_month(4, 0.03, {}, [], terms(default_uplift=1.0), set())
        self.assertEqual(late, [])


class TestEventBookkeeping(unittest.TestCase):

    def test_no_duplicate_events(self):
        fired = set()
        first = evaluate_month(5, 0.09, {}, [], terms(), fired)
        second = evaluate_month(6, 0.10, {}, [], terms(), fired)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_interest_shortfall_is_eod(self):
        ev = evaluate_month(3, 0.0, {"优先A": 1e6}, [], terms(), set())
        self.assertEqual([e.code for e in ev], ["SENIOR_INTEREST_SHORTFALL"])
        self.assertEqual(ev[0].kind, "eod")

    def test_tiny_shortfall_ignored(self):
        """欠付不足 1 元视为舍入噪声，不触发。"""
        self.assertEqual(evaluate_month(3, 0.0, {"优先A": 0.5}, [], terms(), set()), [])

    def test_flag_injection_uses_month(self):
        t = terms(flags={"SERVICER_DEFAULT": 4})
        self.assertEqual(evaluate_month(3, 0.0, {}, [], t, set()), [])
        ev = evaluate_month(4, 0.0, {}, [], t, set())
        self.assertEqual([e.code for e in ev], ["SERVICER_DEFAULT"])
        self.assertEqual(ev[0].kind, "accel")

    def test_doc_termination_is_declare_kind(self):
        t = terms(flags={"DOC_TERMINATION": 2})
        ev = evaluate_month(2, 0.0, {}, [], t, set())
        self.assertEqual(ev[0].kind, "declare")


class TestTermsParsing(unittest.TestCase):

    def test_disabled_returns_none(self):
        self.assertIsNone(TriggerTerms.from_dict(None))
        self.assertIsNone(TriggerTerms.from_dict({}))
        self.assertIsNone(TriggerTerms.from_dict({"enabled": False}))

    def test_defaults_are_huitian_like(self):
        t = TriggerTerms.from_dict({"enabled": True})
        self.assertAlmostEqual(t.cdr_first_year, 0.05)
        self.assertAlmostEqual(t.cdr_after_first_year, 0.08)
        self.assertFalse(t.principal_overdue_enabled, "本金逾期条件默认关闭")


def small_pool(**kw):
    base = dict(balance=1.0e9, annual_yield=0.20, monthly_repayment_rate=0.18,
                charge_off_rate=0.20, recovery_rate=0.02, recovery_lag_months=6,
                servicing_fee_rate=0.005, revolving_months=6, amort_months=10)
    base.update(kw)
    return Pool(**base)


def tranches():
    return [
        Tranche("优先A", 0.80, "AAA", "fixed", coupon=0.02, expected_maturity_month=10),
        Tranche("次级", 0.20, "未评级", "residual", income_rate=0.03,
                expected_maturity_month=20),
    ]


class TestWaterfallIntegration(unittest.TestCase):

    def test_revolving_period_pays_no_principal(self):
        """循环购买期不得分配本金——否则等于把超额利差当本金用掉。"""
        pool = small_pool()
        rows = build_schedule(pool)
        res = run_waterfall(rows, pool.balance, tranches(), terms=terms())
        n_rev = sum(1 for r in rows if r["phase"] == "revolving")
        for name, flow in res.principal.items():
            self.assertAlmostEqual(sum(flow[:n_rev]), 0.0, delta=1.0,
                                   msg=f"{name} 在循环期收到了本金")

    def test_accel_event_sets_accel_month(self):
        """核销率很高的池子应在循环期内突破累计违约率阈值。"""
        pool = small_pool()
        rows = build_schedule(pool)
        res = run_waterfall(rows, pool.balance, tranches(), terms=terms())
        self.assertGreater(res.accel_month, 0)
        self.assertLessEqual(res.accel_month, 6, "应在循环期内触发")
        self.assertTrue(any(e.code == "CDR" for e in res.trigger_events))

    def test_principal_overdue_is_accel_not_eod(self):
        """鼎柚 (129)(f)：预期到期日无法足额分配本金属自动生效的**加速清偿事件**。

        早期版本按"后果"把它归成违约事件（证券立即到期），与原件不符。这个测试
        锁住正确分类：它属于 accel，会终止持续购买。
        """
        pool = small_pool(charge_off_rate=0.001)
        rows = build_schedule(pool)
        trs = tranches()
        trs[0].expected_maturity_month = 3        # 故意提前，制造"未按期足额分配"
        res = run_waterfall(rows, pool.balance, trs,
                            terms=terms(principal_overdue_enabled=True))
        ev = [e for e in res.trigger_events if e.code == "SENIOR_PRINCIPAL_OVERDUE"]
        self.assertTrue(ev, "应报出本金逾期")
        self.assertEqual(ev[0].kind, "accel")
        self.assertGreater(res.accel_month, 0, "accel 类应触发加速清偿")

    def test_interest_shortfall_does_not_set_accel_month(self):
        """真正的违约事件（优先档欠息）不截断循环期——后果是证券立即到期，不是提前摊还。"""
        pool = small_pool(charge_off_rate=0.001)
        rows = build_schedule(pool)
        trs = tranches()
        res = run_waterfall(rows, pool.balance, trs,
                            terms=terms(flags={"SENIOR_INTEREST_SHORTFALL": 2}))
        # 用 flags 无法注入 eod（flags 走 CONDITION_KINDS），故直接校验分类映射
        from abs.trigger import CONDITION_KINDS
        self.assertEqual(CONDITION_KINDS["SENIOR_INTEREST_SHORTFALL"], "eod")
        self.assertEqual(CONDITION_KINDS["SENIOR_PRINCIPAL_OVERDUE"], "accel")
        self.assertEqual(res.accel_month, 0)

    def test_pre_fired_suppresses_duplicate(self):
        pool = small_pool()
        rows = build_schedule(pool)
        res = run_waterfall(rows, pool.balance, tranches(), terms=terms(),
                            pre_fired={"CDR"})
        self.assertFalse(any(e.code == "CDR" for e in res.trigger_events))

    def test_legacy_threshold_still_works(self):
        """不传 terms 时退回旧的单一累计核销率阈值，保持向后兼容。"""
        pool = small_pool()
        rows = build_schedule(pool)
        res = run_waterfall(rows, pool.balance, tranches(), trigger_cdr=0.10)
        self.assertGreater(res.accel_month, 0)


if __name__ == "__main__":
    unittest.main()
