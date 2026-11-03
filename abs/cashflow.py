"""逐月现金流引擎（队列法 + 循环购买）。

第 2 层：在资产池上按月推演，产出每月可分配给证券的现金。

## 三个口径（按《联合资信 消费金融类资产支持证券评级方法》p.5–p.6 原文修正）

### 1. 月还款率：分母是初始放款金额，不是当期余额

    月还款率 = 静态资产池当月收到的本金 / 静态资产池初始放款金额

推论：一个队列每月偿还的本金是常数 `face × 月还款率`，而不是余额的等比衰减。
按初始放款金额 F、月还款率 r，队列在 1/r 个月左右摊完。

### 2. 核销率：累计终身口径，不是年化率  ← 本版最重要的修正

    核销率 = 正常还款周期 90 天后仍逾期的未偿本金余额 / 静态资产池初始放款金额

分子是「最终核销掉的本金」，分母是初始放款金额。所以核销率是**一个队列在整个
生命周期内的累计损失率**，不是年化率。此前版本按 `1-(1-年化率)^(1/12)` 拆月、
再乘到逐月递减的余额上，把损失系统性低估了一半以上——这是模型偏乐观的主要来源。

队列寿命由还款与核销共同决定：每月流出占 face 的比例为 `r + c/(1-c)·r`，
故寿命 N = (1 − c) / r，期间累计核销恰为 `c × face`。

### 3. 购买率：循环期内到账本金再投资的比例

    购买率 = 当月新增资产规模 / 上月可用于购买的资金

本模型按「当月到账本金（计划本金 + 回收款）× 购买率」购买，并以本金分账户
可用余额封顶（现金守恒：累计购买额不可能超过累计到账本金）。

## 为什么要按队列（cohort）推演

旧实现只有一个"资产池余额"标量，循环期买进的资产**不产生任何现金流**——买入即
消失，只是把余额撑住。真实结构里持续买进的资产会继续生息、继续摊还、继续违约，
并在下一个分配日把本金再次用于购买，形成周转。

按队列推演后，鼎柚 AAAsf 情景下累计放款达到初始池的 2.3 倍，与评级报告图表20
披露的"持续购买支出 20.17 亿元"同一量级；旧实现只有 0.6 倍。

## 累计违约率的分母（按两单说明书「累计违约率」定义）

    A = 各收款期间内所有违约贷款在成为违约贷款时的未偿本金余额之和
    B = 初始起算日资产池余额 + 各持续购买日购买的资产在持续购买基准日的未偿本金余额之和

分母 B 要**加上历次持续购买的本金**。循环购买结构下分母快速膨胀，累计违约率被
显著稀释——这是循环结构里加速清偿不容易触发的机制性原因。
本模块同时输出两个口径：`cum_charge_off_rate`（占初始余额）与
`cum_default_rate`（占初始余额 + 累计购买本金），后者供触发判定使用。

分子口径的一致性：方法文件 p.5 写明「逾期超过 30 天记为违约资产，逾期超过 90 天
记为核销资产」，而两单说明书的「违约贷款」定义为逾期超过 90 日——**两者是同一批
资产**。因此累计核销额可以直接作为累计违约率的分子。
"""

import math


def build_schedule(pool, co_mult=1.0, mrr_mult=1.0, recovery_mult=1.0, yield_mult=1.0,
                   vintage=None, revolving_cap=None, ramp_months=0):
    """返回月度明细列表。mult 参数用于压力情景。

    vintage 不为 None 时启用账龄曲线：损失总量仍等于「核销率 × 各队列放款额」，
    但按 Weibull 边际曲线在队列寿命内重新分配节奏（总量不变，只改时序）。

    revolving_cap 用于模拟「加速清偿 → 停止持续购买 → 提前进入摊还期」：
    给出则覆盖 pool.revolving_months 作为循环期的实际上限。
    """
    rev = pool.revolving_months if revolving_cap is None else max(int(revolving_cap), 0)
    return _build(pool, co_mult, mrr_mult, recovery_mult, yield_mult,
                  vintage=vintage, revolving_months=rev, ramp_months=ramp_months)


def _build(pool, co_mult, mrr_mult, recovery_mult, yield_mult, vintage=None,
           revolving_months=None, ramp_months=0):
    rev = pool.revolving_months if revolving_months is None else int(revolving_months)

    mrr = min(max(pool.monthly_repayment_rate * mrr_mult, 0.001), 0.95)
    co_final = min(pool.charge_off_rate * co_mult, 0.95)
    yield_final = pool.annual_yield * yield_mult
    reinvest = min(max(pool.reinvest_rate, 0.0), 1.0)
    rec_rate = min(pool.recovery_rate * recovery_mult, 1.0)
    lag = max(int(pool.recovery_lag_months), 0)

    # 收益率与核销率的过渡期长度可以不同：够花 AAAsf 情景下收益率不设过渡
    # （条款下限 20% 直接作为压力参数），核销率仍设 4 个月过渡。
    co_ramp = int(ramp_months)
    y_ramp = int(getattr(pool, "yield_ramp_months", None) if
                 getattr(pool, "yield_ramp_months", None) is not None else ramp_months)

    def ramp(m, months):
        """施压速度：从基准值经 months 个月线性达到压力值。

        联合资信评级方法 p.7：AAA 级过渡时间 4 个月（AA+/AA-/A+ 为 6 个月），
        月还款率与购买率不设过渡、直接达到压力值。
        公开文件**没有披露过渡曲线的形态**，线性插值是我自己的建模假设。
        """
        if months <= 0:
            return 1.0
        return min(1.0, (m - 1) / float(months))

    def rm_of(m):
        return (pool.annual_yield + (yield_final - pool.annual_yield) * ramp(m, y_ramp)) / 12.0

    def co_of(m):
        return pool.charge_off_rate + (co_final - pool.charge_off_rate) * ramp(m, co_ramp)

    def life_months(c):
        """队列寿命（月）：还款与核销共同把余额耗尽的时点。"""
        c = min(max(c, 0.0), 0.9)
        return max(int(round((1.0 - c) / mrr)), 1)

    def loss_weights(c, born_month):
        """队列寿命内各账龄月的核销权重，和为 1。"""
        n = life_months(c)
        if vintage is None:
            return [1.0 / n] * n
        ws = vintage.weights(n, shift=int(pool.seasoning_months))
        return ws if ws else [1.0 / n] * n

    # ---- 队列状态 ----
    # 每个队列记录：face（初始放款金额）、paid（已还本金）、co（已核销）、
    # born（成立的日历月）、w（核销权重）
    cohorts = []

    def add_cohort(face, m):
        if face <= 1e-6:
            return
        c = co_of(m)
        cohorts.append({"face": face, "paid": 0.0, "co": 0.0, "born": m,
                        "w": loss_weights(c, m)})

    add_cohort(pool.balance, 1)

    rows = []
    pending = []              # (发生月, 核销额)，用于计算滞后回收
    cum_co = 0.0
    cum_purchased = 0.0       # 持续购买累计本金，累计违约率分母的组成部分
    cum_gross_principal = 0.0
    cum_recovery = 0.0
    first_amort_month = rev + 1
    m = 1
    end = rev + pool.amort_months + 24

    while m <= end:
        interest = 0.0
        sched = 0.0            # 计划本金回收
        co = 0.0
        live = 0.0

        c_m = co_of(m)
        for c in cohorts:
            rem = c["face"] - c["paid"] - c["co"]
            if rem <= 1e-6:
                continue
            live += rem
            interest += rem * rm_of(m)
            age = m - c["born"] + 1
            w = c["w"][age - 1] if 0 < age <= len(c["w"]) else 0.0
            p = c["face"] * mrr
            d = c["face"] * c_m * w
            if p + d > rem:
                s = rem / (p + d)
                p *= s
                d *= s
            c["paid"] += p
            c["co"] += d
            sched += p
            co += d

        recovery = sum(a for (mm, a) in pending if m - mm == lag) * rec_rate
        pending = [(mm, a) for (mm, a) in pending if m - mm != lag]
        if co > 0:
            pending.append((m, co))

        fee = live * pool.servicing_fee_rate / 12.0
        tax = interest * pool.tax_rate

        purchase = 0.0
        if m <= rev:
            # 现金守恒：可用于购买的上限是本金分账户余额（累计到账本金 − 累计已购买）
            cash = cum_gross_principal + cum_recovery + sched + recovery - cum_purchased
            purchase = min(reinvest * (sched + recovery), max(cash, 0.0))
            cum_purchased += purchase

        cum_co += co
        cum_gross_principal += sched
        cum_recovery += recovery

        phase = "revolving" if m <= rev else ("amortizing" if live > 1e-6 else "tail")
        net_principal = sched + recovery - purchase
        # 鼎柚：摊还期第一个支付日只付息不还本（联合资信报告 p.6、p.13）
        distribute = not (pool.revolving_first_pay_interest_only and m == first_amort_month)

        rows.append({
            "month": m,
            "phase": phase,
            "bal_begin": live,
            "interest": interest,
            "principal": net_principal,
            "gross_principal": sched,
            "recovery": recovery,
            "purchase": purchase,
            "charge_off": co,
            "servicing_fee": fee,
            "tax": tax,
            "total_collection": interest + net_principal,
            "bal_end": live - sched - co + purchase,
            "distribute_principal": distribute,
            "cum_charge_off_rate": cum_co / pool.balance,
            "cum_purchased": cum_purchased,
            "cum_default_rate": cum_co / (pool.balance + cum_purchased),
        })

        if m > rev and live <= 1e-6 and not pending:
            break
        if m > rev and live <= 1e-6:
            # 余额归零但仍有未到期回收款，继续把回收款记入
            if m >= (pending[-1][0] + lag if pending else m):
                break

        if purchase > 0:
            add_cohort(purchase, m + 1)
        m += 1

    # 余额归零后仍在回收期内的月份继续入账
    last_def = pending[-1][0] if pending else 0
    while m <= last_def + lag and m <= end:
        recovery = sum(a for (mm, a) in pending if m - mm == lag) * rec_rate
        pending = [(mm, a) for (mm, a) in pending if m - mm != lag]
        cum_recovery += recovery
        rows.append({
            "month": m,
            "phase": "tail",
            "bal_begin": 0.0,
            "interest": 0.0,
            "principal": recovery,
            "gross_principal": 0.0,
            "recovery": recovery,
            "purchase": 0.0,
            "charge_off": 0.0,
            "servicing_fee": 0.0,
            "tax": 0.0,
            "total_collection": recovery,
            "bal_end": 0.0,
            "distribute_principal": True,
            "cum_charge_off_rate": cum_co / pool.balance,
            "cum_purchased": cum_purchased,
            "cum_default_rate": cum_co / (pool.balance + cum_purchased),
        })
        m += 1

    return rows


def summarize_pool(rows, pool):
    gross_co = sum(r["charge_off"] for r in rows)
    recovery = sum(r["recovery"] for r in rows)
    principal = sum(r["gross_principal"] for r in rows)
    interest = sum(r["interest"] for r in rows)
    purchased = sum(r["purchase"] for r in rows)
    wal = sum(r["month"] / 12.0 * r["gross_principal"] for r in rows) / pool.balance
    return {
        "资产池加权平均期限(年)": wal,
        "累计核销率": gross_co / pool.balance,
        "累计回收率(占核销)": (recovery / gross_co) if gross_co else 0.0,
        "净损失率": (gross_co - recovery) / pool.balance,
        "本金回收率": principal / pool.balance,
        "池利息收入(亿元)": interest / 1e8,
        "存续月数": len(rows),
        "累计放款倍数": (pool.balance + purchased) / pool.balance,
        "累计持续购买(亿元)": purchased / 1e8,
        "累计回收本金(亿元)": (principal + recovery) / 1e8,
        "净损失(亿元)": (gross_co - recovery) / 1e8,
    }
