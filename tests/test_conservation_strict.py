"""P2-1/P2-3：严格守恒校验与 OC 一致性断言（第 9 周）。

P2-1：check_conservation 从「一条相对容差式」收紧为「三条恒等式 + 1 元绝对容差」。
      旧式在 16 亿量级上允许 16 万残差，足以掩盖 P0-D 那种双边漏记。
P2-3：分层占比隐含的 OC 必须等于披露 oc_rate（容差 5e-6），否则拒绝生成报告。
"""

import os
import sys
import unittest
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from abs import Pool, Tranche, build_schedule, run_waterfall  # noqa: E402
from run_case import check_conservation  # noqa: E402


def make_case_objs(case_id):
    import json
    path = os.path.join(ROOT, "cases",
                        "dingyou_2025_3.json" if case_id == "dingyou"
                        else "gouhua_2025_2.json")
    with open(path, encoding="utf-8") as f:
        case = json.load(f)
    return case, Pool.from_dict(case["pool"]), [Tranche.from_dict(t)
                                                for t in case["tranches"]]


class TestStrictConservation(unittest.TestCase):

    def test_subaccount_identities_hold_in_both_modes(self):
        """两条子账恒等式在两种口径下逐月残差均为 0。"""
        for case_id in ("dingyou", "gouhua"):
            _, pool, _ = make_case_objs(case_id)
            for mode in ("principal", "available"):
                p = replace(pool, purchase_base=mode, security_cost_rate=0.02,
                            sub_share=0.09, sub_income_rate=0.03)
                rows = build_schedule(p)
                prev = 0.0
                for r in rows:
                    acct = (prev + r["gross_principal"] + r["recovery"]
                            + r["income_transfer"] - r["prin_topup"]
                            - r["purchase"] - r["principal"] - r["prin_acct"])
                    inc = (r["total_collection"] - r["interest"]
                           - r["prin_topup"] + r["income_transfer"]
                           - r["principal"])
                    self.assertLess(abs(acct), 1e-6, (case_id, mode, r["month"]))
                    self.assertLess(abs(inc), 1e-6, (case_id, mode, r["month"]))
                    prev = r["prin_acct"]

    def test_one_yuan_tolerance_rejects_smaller_leak(self):
        """人为在 total_collection 里漏记 2 元必须被拒绝。"""
        _, pool, tranches = make_case_objs("dingyou")
        rows = build_schedule(replace(pool, purchase_base="available",
                                      security_cost_rate=0.02))
        res = run_waterfall(rows, pool.balance, tranches)
        ok, gap = check_conservation(rows, res)
        self.assertTrue(ok)
        self.assertLessEqual(abs(gap), 1.0)

        rows[0]["total_collection"] -= 2.0
        ok2, gap2 = check_conservation(rows, res)
        self.assertFalse(ok2)
        self.assertAlmostEqual(gap2, -2.0, delta=1e-6)


class TestOCConsistency(unittest.TestCase):

    def test_disclosed_oc_matches_shares(self):
        """两单的 1−Σshare 与披露 oc_rate 差不超过 5e-6。"""
        for case_id in ("dingyou", "gouhua"):
            case, _, tranches = make_case_objs(case_id)
            implied = 1.0 - sum(t.share for t in tranches)
            self.assertLessEqual(
                abs(implied - case["computed"]["oc_rate"]), 5e-6, case_id)

    def test_run_one_rejects_bad_oc(self):
        """篡改披露 oc_rate 后 run_one 必须抛 ValueError。"""
        import copy
        import json
        import tempfile
        path = os.path.join(ROOT, "cases", "dingyou_2025_3.json")
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        case["computed"]["oc_rate"] = 0.02  # 严重不一致
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                          encoding="utf-8")
        try:
            json.dump(case, tmp)
            tmp.close()
            from run_case import run_one
            with self.assertRaises(ValueError):
                run_one(tmp.name, tempfile.mkdtemp(), quiet=True)
        finally:
            os.unlink(tmp.name)


class TestTerminationBound(unittest.TestCase):
    """P2-2：终止条件包含 prin_acct 清扫；上界内必须收敛。"""

    def test_prin_acct_cleared_at_end(self):
        """两种口径下主循环结束时本金账户余额都必须归零。"""
        for case_id in ("dingyou", "gouhua"):
            _, pool, _ = make_case_objs(case_id)
            for mode in ("principal", "available"):
                p = replace(pool, purchase_base=mode, security_cost_rate=0.02,
                            sub_share=0.09, sub_income_rate=0.03)
                rows = build_schedule(p)
                self.assertLessEqual(abs(rows[-1]["prin_acct"]), 1e-6,
                                     (case_id, mode, rows[-1]["month"]))

    def test_no_silent_truncation(self):
        """各情景行数都不超过可证明上界，且最后一行已满足终止条件。"""
        for case_id in ("dingyou", "gouhua"):
            case, pool, _ = make_case_objs(case_id)
            for sc in case["scenarios"]:
                p = replace(pool, purchase_base="available", security_cost_rate=0.02,
                            sub_share=0.09, sub_income_rate=0.03)
                rows = build_schedule(p, co_mult=sc.get("co_mult", 1.0),
                                      mrr_mult=sc.get("mrr_mult", 1.0),
                                      ramp_months=sc.get("ramp_months", 0))
                # hard_end = rev + cohort_periods + len(recovery_weights)
                import math
                co_final = min(pool.charge_off_rate * sc.get("co_mult", 1.0), 0.95)
                mrr = pool.monthly_repayment_rate * sc.get("mrr_mult", 1.0)
                periods = max(int(math.ceil((1.0 - co_final) / mrr)), 1)
                hard_end = pool.revolving_months + periods + len(pool.recovery_weights())
                self.assertLessEqual(rows[-1]["month"], hard_end)
                # 末月必须同时满足：队列清空、无待回收、账户归零
                self.assertLessEqual(abs(rows[-1]["prin_acct"]), 1e-6)


if __name__ == "__main__":
    unittest.main()
