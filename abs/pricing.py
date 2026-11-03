"""票面利率反解与收益指标。

第 4 层：给定某一档的目标收益率（市场要求的回报），反解它"应该发多少票面"才能在发行价
（面值）上刚好达到这个收益率。债券定价的常规做法是给定利率算价格，这里是反过来的——
分层设计里更常见的提问方式是"我要把这一档卖给投资者，票面得到多少"。

另外提供 WAL、预期损失率、利差三个评价指标。
"""


def _npv(rate, cashflows):
    """cashflows: [(年化时点, 金额)]"""
    return sum(cf / (1.0 + rate) ** t for t, cf in cashflows)


def irr(monthly_cfs, notional):
    """月度现金流（含期初 -notional）的内部收益率，二分法求解。"""
    flows = [(0.0, -notional)]
    for i, cf in enumerate(monthly_cfs):
        if cf:
            flows.append(((i + 1) / 12.0, cf))

    def f(y):
        return _npv(y, flows)

    lo, hi = -0.95, 3.0
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


def solve_coupons(rows, pool_balance, tranches, pay_mode="sequential", trigger_cdr=0.06):
    """逐档反解票面利率：让该档在面值发行下达到目标收益率。"""
    from .waterfall import run_waterfall

    for t in tranches:
        if t.mode != "solve":
            # fixed：沿用给定票面；residual：无票面。两者都不参与反解。
            continue
        if t.target_yield <= 0:
            t.coupon = 0.0
            continue
        lo, hi = 0.0, 0.60
        for _ in range(60):
            mid = (lo + hi) / 2.0
            t.coupon = mid
            res = run_waterfall(rows, pool_balance, tranches, pay_mode, trigger_cdr)
            y = irr(res.cashflow[t.name], res.notional[t.name])
            if y is None or y < t.target_yield:
                lo = mid
            else:
                hi = mid
        t.coupon = (lo + hi) / 2.0


def tranche_metrics(result, tranche):
    """单档指标。本金流单独取自 WaterfallResult.principal，不把利差当本金。"""
    notional = result.notional[tranche.name]
    cfs = result.cashflow[tranche.name]
    prs = result.principal[tranche.name]

    paid_principal = sum(prs)
    unpaid = max(notional - paid_principal, 0.0)
    wal = sum((i + 1) / 12.0 * p for i, p in enumerate(prs)) / notional if notional else 0.0
    realized = irr(cfs, notional)

    return {
        "档位": tranche.name,
        "评级": tranche.rating,
        "发行规模(亿元)": notional / 1e8,
        "占比": tranche.share,
        "票面利率": tranche.coupon,
        "目标收益率": tranche.target_yield if tranche.mode != "residual" else None,
        "实现收益率": realized,
        "定价基准": tranche.reference_yield,
        "利差": (tranche.coupon - tranche.reference_yield) if tranche.mode != "residual" else None,
        "加权平均期限(年)": wal,
        "本金损失率": unpaid / notional if notional else 0.0,
        "未付利息(万元)": result.unpaid_interest[tranche.name] / 1e4,
    }
