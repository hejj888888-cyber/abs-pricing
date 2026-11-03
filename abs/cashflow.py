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

队列寿命由还款与核销共同决定：本金每月偿还 `face × r`，需要还完的本金是
`face × (1 − c)`，故寿命 N = (1 − c) / r；核销总量 `c × face` 在 N 个月内按
时序权重分配（第 5 周修正，见下）。

### 3. 购买率：循环期内到账本金再投资的比例

    购买率 = 当月新增资产规模 / 上月可用于购买的资金

本模型按「当月到账本金（计划本金 + 回收款）× 购买率」购买，并以本金分账户
可用余额封顶（现金守恒：累计购买额不可能超过累计到账本金）。

## 为什么要按队列（cohort）推演

旧实现只有一个"资产池余额"标量，循环期买进的资产**不产生任何现金流**——买入即
消失，只是把余额撑住。真实结构里持续买进的资产会继续生息、继续摊还、继续违约，
并在下一个分配日把本金再次用于购买，形成周转。

按队列推演后，鼎柚 AAAsf 情景下累计放款达到初始池的 1.5 倍（披露购买率 55%）
到 2.1 倍（购买率 100%、现金守恒上限），与评级报告图表20 披露的"持续购买支出
20.17 亿元"同一量级；旧实现只有 0.6 倍。

## 第 5 周：总量 / 时序分离

早期实现把三件事混在一起，导致累计核销比 `c × 累计放款` 少 2%（基准）到 22%
（AAAsf）：

1. **总量**——`co_final`，情景下的累计终身损失率；
2. **日历时序**——`ramp`，从基准核销率过渡到最终压力水平的月数；
3. **账龄时序**——vintage 权重，损失在队列账龄上的分布。

错在「用出生月的核销率定队列寿命」又「用当月的核销率算月度核销」，两个口径混用；
再加上 `p + d > rem` 的同比例 capping 会把后置的损失裁掉。

现在每个队列显式锁定两个目标：

    loss_target = face × co_final          # 终身核销总量，锁死
    prin_target = face − loss_target       # 应还本金目标

月度处理改成各自封顶，不再联合裁剪（因为 `prin_target + loss_target == face`，
两者之和本就不可能超过 face）。ramp 只作为**时序调节因子**叠加到基础权重上再
归一化，因此只改「哪几个月确认损失」，不改总量。见 `docs/loss-conservation.md`。

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

## 回收事件为什么显式排程（第 3 周）

旧实现用 `pending = [(核销月, 核销额)]` 全局聚合，到「核销月 + lag」一次性回收，
三个问题：① 不能表达月度分布；② 不按队列追踪；③ 余额归零后的尾部回收靠一段
单独的补记循环，且 break 只看最后一条事件，分布一长就会漏记或提前退出。

现在改为 `recovery_events: {到期月: 到账额}`：当期核销按 `pool.recovery_weights()`
把 `co × rec_rate × w[k]` 排入 `m + k` 月，主循环每月 pop 当月应到账额。主循环
一直跑到事件表清空才退出，故余额归零后的回收款完整入账，不再有 capping 截断。
默认 one-hot 分布与旧行为逐行等价（见 `tests/test_recovery.py` 与基线 diff）。

**注意回收时序不是中性的**：回收款进入循环期可购买资金（`cash` 含 `cum_recovery`
与当月 `recovery`），改到账月份会改变当月购买额、队列规模、后续利息与核销，
也就是会改变总量。这与第 2 周账龄曲线「只改时序、总量不变」不同，不能套用
两遍跑法；分布只能作为敏感性开关，基准仍用与披露口径一致的定点滞后。
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
    rec_weights = pool.recovery_weights()

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
        """日历月 m 的核销率（ramp 期内为过渡值，之后等于 co_final）。

        **它只用来调整损失确认的时序，不再决定队列的终身损失总量。**
        早期实现用 co_of(born_month) 定队列寿命、又用 co_of(m) 算月度核销，
        两个口径混用导致累计核销比「最终压力率 × 累计放款」少 2%（基准）
        到 22%（AAAsf）——已修正，见 docs/loss-conservation.md。
        """
        return pool.charge_off_rate + (co_final - pool.charge_off_rate) * ramp(m, co_ramp)

    # ---- 队列的「总量 / 时序」分离（第 4 周修正）----
    # 联合资信《消费金融类资产支持证券评级方法》p.7：
    #   1) 「基准情景核销率与压力乘数的乘积就是最大核销率」——这是**总量**；
    #   2) 「设定初始核销率为基准情景核销率，经过 4 个月逐渐升至压力水平……
    #      在达到压力水平之后均维持该压力直至交易结束」——这是**时序**。
    # 中债资信方法 p.21–22 把这条原则说透：「违约时间分布表征了……每年的违约额
    # 在总违约额中的占比」，且「新增违约时间分布 Z(n) = 新增违约率 / 总累计违约率」
    # ——时间分布是总量的分配，不改变总量。
    #
    # 因此每个队列：终身核销总量锁死为 co_final × face；ramp 与 vintage 只决定
    # 分摊到哪几个月，两个自由度互不干扰。这也是「Σcharge_off = co_final × Σface」
    # 能精确成立的原因。

    def cohort_periods():
        """队列期数：本金目标（face − 终身损失）按水平偿还摊完所需月数。"""
        return max(int(math.ceil((1.0 - min(co_final, 0.95)) / mrr)), 1)

    def base_timing(n):
        """基础时序权重（和为 1），只由 vintage 曲线决定。"""
        if vintage is None:
            return [1.0 / n] * n
        ws = vintage.weights(n, shift=int(pool.seasoning_months))
        return ws if ws else [1.0 / n] * n

    _periods = cohort_periods()
    _base_w = base_timing(_periods)

    def timing_weights(born):
        """把 ramp 作为日历时间调节因子叠加到基础权重上，再归一化。

        ramp 期内压力尚未加到顶，损失确认应被压低，故乘以「当月核销率 / 最终
        核销率」。归一化后权重和仍为 1：**只改时序，不改总量**。
        公开文件只写「逐渐」两个字，没有给过渡路径，线性插值本身是显式假设。
        """
        if co_final <= 0:
            return list(_base_w)
        raw = []
        for k in range(_periods):
            level = co_of(born + k) / co_final
            raw.append(_base_w[k] * max(level, 0.0))
        total = sum(raw)
        return [x / total for x in raw] if total > 0 else list(_base_w)

    # ---- 队列状态 ----
    # 每个队列记录：face（初始放款金额）、loss_target（终身核销总量）、
    # loss_rec（已确认核销）、prin_target（应还本金）、prin_paid（已还本金）、
    # born（成立的日历月）、w（时序权重，和为 1）
    cohorts = []

    def add_cohort(face, m):
        if face <= 1e-6:
            return
        loss_target = face * co_final
        cohorts.append({
            "face": face,
            "loss_target": loss_target,
            "loss_rec": 0.0,
            "prin_target": face - loss_target,
            "prin_paid": 0.0,
            "born": m,
            "w": timing_weights(m),
        })

    add_cohort(pool.balance, 1)

    rows = []
    recovery_events = {}      # 到期月 -> 当月应到账的回收额（已乘 rec_rate）
    cum_co = 0.0
    cum_purchased = 0.0       # 持续购买累计本金，累计违约率分母的组成部分
    cum_gross_principal = 0.0
    cum_recovery = 0.0
    first_amort_month = rev + 1
    m = 1
    # 多留队列期数与回收分布长度，保证最后一笔核销的分布尾部也落进主循环
    end = rev + pool.amort_months + 24 + len(rec_weights) + _periods

    while m <= end:
        interest = 0.0
        sched = 0.0            # 计划本金回收
        co = 0.0
        live = 0.0

        for c in cohorts:
            rem = c["face"] - c["prin_paid"] - c["loss_rec"]
            if rem <= 1e-6:
                continue
            live += rem
            interest += rem * rm_of(m)
            age = m - c["born"] + 1
            w = c["w"][age - 1] if 0 < age <= len(c["w"]) else 0.0
            # 本金与核销各自封顶，不再做 p+d 同比例 capping：
            # 同比例裁剪会让「损失后置」的队列凭空少确认损失，正是守恒缺口的来源。
            p = min(c["face"] * mrr, c["prin_target"] - c["prin_paid"])
            if p < 0.0:
                p = 0.0
            d = min(c["loss_target"] * w, c["loss_target"] - c["loss_rec"])
            if d < 0.0:
                d = 0.0
            c["prin_paid"] += p
            c["loss_rec"] += d
            sched += p
            co += d

        # 当期核销按分布排入未来各月的回收事件。排在 pop 之前，是为了让
        # k=0（核销当月即回收）也能计入本月；one-hot 时只有 k=lag 一项。
        if co > 0:
            for k, w in enumerate(rec_weights):
                if w > 0.0:
                    t = m + k
                    recovery_events[t] = (recovery_events.get(t, 0.0)
                                          + co * rec_rate * w)

        # 本月应到账的回收款，可来自多个不同月份核销的叠加
        recovery = recovery_events.pop(m, 0.0)

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

        # 余额归零后仍可能有未到期的回收事件，主循环继续跑到事件表清空，
        # 因此不再需要单独的尾部补记循环，也不会被 capping 截断。
        if m > rev and live <= 1e-6 and not recovery_events:
            break

        if purchase > 0:
            add_cohort(purchase, m + 1)
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
