"""逐月现金流引擎（含循环购买期）。

第 2 层：在资产池上按月推演，产出每月可分配给证券的现金。

两个阶段：
  循环购买期（1 ~ revolving_months）
      本金回收按购买率再投资，资产池余额维持不变；证券端只收到利息与回收款，
      优先档在这一阶段只付息、不还本（与真实交易"循环期按季付息"一致）。
  摊还期（之后）
      资产池按"月还款率"摊还，回收款不再投资，全额过手给证券端。

口径说明：
  1. 利息按月初余额计。
  2. 核销有两种模式：
     - 恒定模式（默认）：年化核销率折算为月度核销率，逐月按余额计提。
     - 账龄曲线模式（vintage）：保持核销总量不变，按 Weibull 累计曲线重新分配
       到各月。目的是把"总量估得准不准"和"时间分布对不对"两个问题分开。
  3. 核销后回收按固定回收率、固定滞后处理，未刻画回收的时间分布。
  4. 循环期内假设购买率 100%、资产池余额严格维持不变（评级报告披露的量化参数假设）。
"""


def build_schedule(pool, co_mult=1.0, mrr_mult=1.0, recovery_mult=1.0, yield_mult=1.0,
                   vintage=None, revolving_cap=None):
    """返回月度明细列表。mult 参数用于压力情景。

    vintage 不为 None 时启用账龄曲线：先按恒定核销率跑一遍得到损失总量与期限，
    再按曲线把同样的总量重新分配到各月。两遍结果的总核销量一致，差异纯粹来自时序。

    revolving_cap 用于模拟「加速清偿 → 停止持续购买 → 提前进入摊还期」：
    给出则覆盖 pool.revolving_months 作为循环期的实际上限。
    """
    rev = pool.revolving_months if revolving_cap is None else max(int(revolving_cap), 0)

    if vintage is not None:
        base = build_schedule(pool, co_mult, mrr_mult, recovery_mult, yield_mult,
                              revolving_cap=revolving_cap)
        total_loss = sum(r["charge_off"] for r in base)
        n = len(base)
        amounts = vintage.allocate(n, total_loss,
                                   revolving_months=rev,
                                   seasoning_months=pool.seasoning_months)
        return _build(pool, co_mult, mrr_mult, recovery_mult, yield_mult,
                      co_amounts=amounts, revolving_months=rev)

    return _build(pool, co_mult, mrr_mult, recovery_mult, yield_mult,
                  revolving_months=rev)


def _build(pool, co_mult, mrr_mult, recovery_mult, yield_mult, co_amounts=None,
           revolving_months=None):
    rev = pool.revolving_months if revolving_months is None else int(revolving_months)
    rm = pool.monthly_rate() * yield_mult
    mrr = min(max(pool.monthly_repayment_rate * mrr_mult, 0.001), 0.95)
    co_rate = min(pool.charge_off_rate * co_mult, 0.95)
    mdr = 1 - (1 - co_rate) ** (1.0 / 12)
    rec_rate = min(pool.recovery_rate * recovery_mult, 1.0)
    lag = pool.recovery_lag_months

    def charge_off_of(m, bal):
        """第 m 月的核销量。曲线模式下取预算额度，且不超过当期余额。"""
        if co_amounts is None:
            return bal * mdr
        if m - 1 >= len(co_amounts):
            return 0.0
        return min(co_amounts[m - 1], bal)

    rows = []
    defaults = []
    bal = pool.balance
    cum_co = 0.0
    m = 1

    # ---------- 循环购买期 ----------
    while m <= rev and bal > 1.0:
        interest = bal * rm
        charge_off = charge_off_of(m, bal)
        recovery = sum(a for (mm, a) in defaults if m - mm == lag) * rec_rate
        fee = bal * pool.servicing_fee_rate / 12.0

        cum_co += charge_off
        if charge_off > 0:
            defaults.append((m, charge_off))

        # 本金回收（含核销）按购买率再投资，余额不变
        principal_recovered = bal * mrr + charge_off
        reinvested = principal_recovered * pool.reinvest_rate
        principal_to_trust = principal_recovered - reinvested

        rows.append({
            "month": m,
            "phase": "revolving",
            "bal_begin": bal,
            "interest": interest,
            "principal": principal_to_trust,
            "charge_off": charge_off,
            "recovery": recovery,
            "servicing_fee": fee,
            "total_collection": interest + principal_to_trust + recovery,
            "bal_end": bal - principal_to_trust - charge_off * (1 - pool.reinvest_rate),
            "cum_charge_off_rate": cum_co / pool.balance,
        })
        bal = rows[-1]["bal_end"]
        if bal < 1.0:
            bal = pool.balance if pool.reinvest_rate >= 1.0 else bal
        m += 1

    # ---------- 摊还期 ----------
    end = rev + pool.amort_months + 2
    while bal > 1.0 and m <= end:
        interest = bal * rm
        principal = bal * mrr
        charge_off = charge_off_of(m, bal)
        if principal + charge_off > bal:
            scale = bal / (principal + charge_off)
            principal *= scale
            charge_off *= scale
        recovery = sum(a for (mm, a) in defaults if m - mm == lag) * rec_rate
        fee = bal * pool.servicing_fee_rate / 12.0

        cum_co += charge_off
        if charge_off > 0:
            defaults.append((m, charge_off))

        rows.append({
            "month": m,
            "phase": "amortizing",
            "bal_begin": bal,
            "interest": interest,
            "principal": principal,
            "charge_off": charge_off,
            "recovery": recovery,
            "servicing_fee": fee,
            "total_collection": interest + principal + recovery,
            "bal_end": bal - principal - charge_off,
            "cum_charge_off_rate": cum_co / pool.balance,
        })
        bal = rows[-1]["bal_end"]
        m += 1

    # 余额归零后，仍在回收期内的月份继续入账
    last_def = defaults[-1][0] if defaults else 0
    while m <= last_def + lag:
        recovery = sum(a for (mm, a) in defaults if m - mm == lag) * rec_rate
        rows.append({
            "month": m,
            "phase": "tail",
            "bal_begin": 0.0,
            "interest": 0.0,
            "principal": 0.0,
            "charge_off": 0.0,
            "recovery": recovery,
            "servicing_fee": 0.0,
            "total_collection": recovery,
            "bal_end": 0.0,
            "cum_charge_off_rate": cum_co / pool.balance,
        })
        m += 1

    return rows


def summarize_pool(rows, pool):
    gross_co = sum(r["charge_off"] for r in rows)
    recovery = sum(r["recovery"] for r in rows)
    principal = sum(r["principal"] for r in rows)
    interest = sum(r["interest"] for r in rows)
    wal = sum(r["month"] / 12.0 * (r["principal"] + r["charge_off"]) for r in rows) / pool.balance
    return {
        "资产池加权平均期限(年)": wal,
        "累计核销率": gross_co / pool.balance,
        "累计回收率(占核销)": (recovery / gross_co) if gross_co else 0.0,
        "净损失率": (gross_co - recovery) / pool.balance,
        "本金回收率": principal / pool.balance,
        "池利息收入(亿元)": interest / 1e8,
        "存续月数": len(rows),
    }
