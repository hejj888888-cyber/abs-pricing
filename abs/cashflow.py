"""逐月现金流引擎。

第 2 层：在资产池上按"计息 → 计划摊还 → 提前还款 → 违约 → 回收（滞后）"的次序逐月推演，
产出每个月的本息回收与余额路径。

口径说明（写进说明文档的"假设与简化"）：
  1. 利息按月初余额计，当月违约的资产当月仍计息（偏保守的简化）。
  2. 计划摊还额按初始余额的等额本息月供固定，实际余额因早偿/违约下降时不重算月供，
     因此计划本金在后期会略高于真实摊还表——这是池层面的近似，误差在第 3 周用真实摊还表校验。
  3. 违约后回收按固定回收率、固定滞后 N 个月回补，不考虑回收的时间分布。
"""


def build_schedule(pool, cdr_mult=1.0, cpr_mult=1.0, recovery_mult=1.0):
    """返回月度明细列表。每个元素是一行 dict。"""
    r = pool.monthly_rate()
    cdr = min(pool.cdr * cdr_mult, 0.95)
    cpr = min(pool.cpr * cpr_mult, 0.95)
    smm = 1 - (1 - cpr) ** (1.0 / 12)
    mdr = 1 - (1 - cdr) ** (1.0 / 12)
    rec_rate = min(pool.recovery_rate * recovery_mult, 1.0)
    lag = pool.recovery_lag_months
    annuity = pool.annuity_payment()

    rows = []
    defaults = []          # [(month, amount)]
    bal = pool.balance
    cum_default = 0.0
    m = 1

    while bal > 1.0 and m <= pool.term_months + 1:
        interest = bal * r
        scheduled = max(0.0, min(annuity - interest, bal))
        base = bal - scheduled
        prepay = base * smm
        default = (base - prepay) * mdr
        bal_end = base - prepay - default

        recovery = sum(a for (mm, a) in defaults if m - mm == lag) * rec_rate
        fee = bal * pool.servicing_fee_rate / 12.0

        cum_default += default
        rows.append({
            "month": m,
            "bal_begin": bal,
            "interest": interest,
            "sched_prin": scheduled,
            "prepay": prepay,
            "default": default,
            "recovery": recovery,
            "servicing_fee": fee,
            "total_collection": interest + scheduled + prepay + recovery,
            "bal_end": bal_end,
            "cum_default_rate": cum_default / pool.balance,
        })
        if default > 0:
            defaults.append((m, default))
        bal = bal_end
        m += 1

    # 余额归零后，仍在回收期内的月份继续入账（只有 recovery）
    last_def_m = defaults[-1][0] if defaults else 0
    tail_end = last_def_m + lag
    while m <= tail_end:
        recovery = sum(a for (mm, a) in defaults if m - mm == lag) * rec_rate
        rows.append({
            "month": m,
            "bal_begin": 0.0,
            "interest": 0.0,
            "sched_prin": 0.0,
            "prepay": 0.0,
            "default": 0.0,
            "recovery": recovery,
            "servicing_fee": 0.0,
            "total_collection": recovery,
            "bal_end": 0.0,
            "cum_default_rate": cum_default / pool.balance,
        })
        m += 1

    return rows


def summarize_pool(rows, pool):
    """池层面的几个关键数：累计违约率、累计净损失率、加权平均期限。"""
    gross_default = sum(r["default"] for r in rows)
    recovery = sum(r["recovery"] for r in rows)
    principal = sum(r["sched_prin"] + r["prepay"] for r in rows)
    wal = sum(r["month"] / 12.0 * (r["sched_prin"] + r["prepay"] + r["default"])
              for r in rows) / pool.balance
    return {
        "累计违约率": gross_default / pool.balance,
        "累计回收率(占违约)": (recovery / gross_default) if gross_default else 0.0,
        "净损失率": (gross_default - recovery) / pool.balance,
        "本金回收率": principal / pool.balance,
        "资产池加权平均期限(年)": wal,
        "存续月数": len(rows),
    }
