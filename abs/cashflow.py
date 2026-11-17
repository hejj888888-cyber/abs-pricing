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

## 第 7 周：账户内划转的两面记账（P0-D 修复）

`available` 口径下收入分账户与本金分账户之间有两笔划转，早期版本只记了一边：

1. 收入不足时 `topup` 从本金账户划给收入账户，本金账户已扣，但交给瀑布的
   `total_collection` 没加回来——现金同时从模型流入端和证券支付端消失，瀑布
   还会把「账上有钱却被划走」误判成优先档利息未足额偿付。
2. 收入有剩余时 `income_transfer` 转入本金账户买了资产，但完整 `interest`
   仍进入瀑布形成 `reserve`——同一笔剩余收入被用了两次。

现在两笔划转在两个账户里同时记账：

    principal_cash  = prin_acct(上期留存) + 计划本金 + 回收款
    income_required = 服务费 + 税费 + 证券利息
    topup           = min(max(income_required − interest, 0), principal_cash)
    income_transfer = max(interest − income_required, 0)

    购买基数        = principal_cash − topup + income_transfer
    交给瀑布        = interest + topup − income_transfer + 分摊本金

由此收入账户净额恰好等于 `income_required`（本金足够时），既不会凭空消失也不会
重复计算；本金账户恒等式
`principal_cash + income_transfer == topup + purchase + prin_acct(月末)` 始终成立。
补足款受本金账户余额约束，因此本金真的不够时仍会留下**真实**的利息缺口，不再是
账户划转造成的假缺口。

`principal` 口径不启用这两条划转，`waterfall_cash` 恒等于 `interest`，故该路径
逐行等价（见 `baseline/p0d/diff-report.md`）。

`income_required` 里的证券利息按 `pool.balance × security_cost_rate / 12` 计提：
`security_cost_rate = Σ(票面 × 分层占比)`，而各档初始本金 = 占比 × 初始资产池，
故这个式子与瀑布按「未偿本金 × 票面」算出的应付利息精确相等。早期用当期
`live` 近似，队列摊还后会偏小，正是修复后第 2 月冒出欠息误报的原因。

## 第 8 周：次级期间收益（P1-4）

上一版把次级期间收益完全交给瀑布、且在**本金分配之后**支付，顺序与条款不符；
`available` 口径下又允许收入剩余全额转去购买，等于先把次级该拿的钱买成了资产。

现在按两单合同拆分（鼎柚 p.47 (j)、p.48 (a)；够花 p.44 (9)、p.45 (1)）：

    senior_required     = 服务费 + 税费 + 优先档利息（+ 流动性储备，两单均为 0）
    topup               = min(max(senior_required − interest, 0), principal_cash)
    income_after_senior = max(interest + topup − senior_required, 0)
    sub_due             = 初始池 × 次级占比 × 3%/12 + 上期顺延
    sub_paid            = min(sub_due, income_after_senior)
    income_transfer     = income_after_senior − sub_paid

关键点：**本金账户的补足范围只到优先档**，不能替次级付收益，所以 `topup` 只覆盖
`senior_required`；次级收益只能从收入账户自身的剩余里出。交给瀑布的现金
`= senior_required + sub_paid`，瀑布再把次级期间收益排在本金之前支付，两边金额一致。

`sub_income_carry` 按条款配置：鼎柚一般分配条款允许不足额顺延（除 (k) 外）取
True，够花「不足部分下一期不予补足」取 False。
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
    prin_acct = 0.0           # 本金分账户余额（跨月结转）
    sub_arrears = 0.0         # 次级期间收益未付额（仅 sub_income_carry=True 时上滚）
    cum_gross_principal = 0.0
    cum_recovery = 0.0
    first_amort_month = rev + 1
    m = 1
    # 第 9 周 P2-2：可证明的安全上界，取代原来的 amort_months + 24 魔数。
    #   · 最晚的新队列在 rev+1 月成立（第 rev 月购买、次月起息）；
    #   · 单个队列最长运行 _periods 月，最后一笔资产现金流最晚在 rev+_periods；
    #   · 回收分布的最大偏移是 len(weights)-1；
    #   · 再留 1 个月用于账户清扫和终止检查（摊还期首个只付息日需要）。
    # 因此 rev + _periods + len(weights) 之后仍不满足终止条件，说明引擎有 bug，
    # 应当抛异常而不是静默截断——静默截断会悄悄丢掉尾部现金。
    hard_end = rev + _periods + len(rec_weights)
    end = hard_end

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

        # ---- 持续购买的可用资金（第 6 周按说明书 p.48 重写）----
        # 本金分账户是一个显式余额，跨月结转：
        #   principal 口径：只把当期本金回收 + 回收款当作购买基数
        #   available 口径：再加上「收入分账户剩余转入」（11.5.1(m)），
        #                    以及上期留存在本金分账户的余额（p.48「下一期持续购买」）
        #
        # 摊还期的第一件事是把「什么时候能分本金」定下来：
        # 鼎柚：摊还期第一个支付日只付息不还本（联合资信报告 p.6、p.13）。
        # 那一期收到的本金不能丢，必须留在本金分账户、下一期起一并过手，
        # 否则会被瀑布当成「未分配余额」流向次级——方向刚好相反。
        distribute = not (pool.revolving_first_pay_interest_only
                          and m == first_amort_month)

        purchase = 0.0
        income_transfer = 0.0
        topup = 0.0
        income_required = 0.0
        sub_due = 0.0
        sub_paid = 0.0
        sweep = 0.0
        net_principal = 0.0
        # 交给瀑布的现金。principal 口径下就是完整利息 + 本金（历史行为，逐行等价）；
        # available 口径下是收入分账户「扣掉转出、加上补足」后的净额 + 本金。
        waterfall_cash = interest
        if m <= rev:
            if pool.purchase_base == "available":
                # 证券利息按各档「未偿本金 × 票面」计提。循环期不分本金，故证券
                # 未偿本金恒等于初始发行规模，用 pool.balance 而非当期资产池余额
                # live：队列摊还后 live 会小于初始池，旧近似会让交给瀑布的钱少于
                # 应付利息，凭空产生「优先档利息欠付」误报（P0-D 修复后暴露）。
                cost = pool.balance * pool.security_cost_rate / 12.0
                principal_cash = max(prin_acct + sched + recovery, 0.0)
                income_required = fee + tax + cost

                # 第 7 周 P0-D 修复：账户内划转必须两边同时记账。
                # 旧实现把 topup 从本金账户扣掉却没交给瀑布（现金凭空消失，瀑布
                # 还会误判优先档利息欠付）；又把 income_transfer 拿去买资产的同时
                # 让完整利息进入瀑布形成 reserve（同一笔剩余收入被使用两次）。
                # 现在补足款受本金账户余额约束，收入账户净额原样交给瀑布。
                topup = min(max(income_required - interest, 0.0), principal_cash)
                income_after_senior = max(interest + topup - income_required, 0.0)

                # 第 8 周 P1-4：次级期间收益排在优先档之后、剩余转入本金之前。
                # 本金账户的补足范围只到优先档（鼎柚 p.47 (a)–(h)、够花 p.43–44
                # (1)–(5)），不能替次级付收益，所以 sub_paid 只能来自
                # income_after_senior。不足额是否顺延按两单条款配置（carry）。
                sub_due = (pool.balance * pool.sub_share * pool.sub_income_rate / 12.0
                           + sub_arrears)
                sub_paid = min(max(sub_due, 0.0), income_after_senior)
                sub_arrears = (sub_due - sub_paid) if pool.sub_income_carry else 0.0

                income_transfer = income_after_senior - sub_paid
                purchase_cash = principal_cash - topup + income_transfer
                available = purchase_cash
                waterfall_cash = interest + topup - income_transfer
                purchase = min(reinvest * purchase_cash, purchase_cash)
            else:
                available = max(prin_acct + sched + recovery, 0.0)
                purchase = min(reinvest * (sched + recovery), available)
            cum_purchased += purchase
            prin_acct = available - purchase
            net_principal = 0.0        # 循环期不分本金，全部留存或再投资
        else:
            # 摊还期不再购买。未到可分配本金的月份，回款与存量现金继续留在
            # 本金分账户；可分配月份一次性清扫（压力情景下购买率不足 100% 时
            # 累积的现金，早期版本在这里凭空消失，是漏损）。
            prin_acct += sched + recovery
            if distribute:
                sweep = prin_acct
                net_principal = prin_acct
                prin_acct = 0.0
            else:
                net_principal = 0.0

        cum_co += co
        cum_gross_principal += sched
        cum_recovery += recovery

        phase = "revolving" if m <= rev else ("amortizing" if live > 1e-6 else "tail")

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
            "income_required": income_required,   # 收入账户应付（费+税+证券利息），仅 available 口径
            "sub_income_due": sub_due,            # 次级期间收益应付（含上期顺延），仅 available 口径
            "sub_income_paid": sub_paid,          # 次级期间收益实付，仅 available 口径；principal 由瀑布直接付
            "income_transfer": income_transfer,   # 收入分账户剩余转入本金分账户
            "prin_topup": topup,                  # 本金分账户实际补足收入分账户缺口（受余额约束）
            "prin_sweep": sweep,                  # 摊还期初始清扫的留存现金
            "prin_acct": prin_acct,               # 月末本金分账户余额
            "total_collection": waterfall_cash + net_principal,
            "bal_end": live - sched - co + purchase,
            "distribute_principal": distribute,
            "cum_charge_off_rate": cum_co / pool.balance,
            "cum_purchased": cum_purchased,
            "cum_default_rate": cum_co / (pool.balance + cum_purchased),
        })

        # 第 9 周 P2-2：可证明终止条件。四个条件缺一不可——
        #   队列全部清空 + 回收事件表为空 + 本金账户清扫完毕 + 已进入摊还期。
        # 旧条件只看 live 和 recovery_events，会在「摊还首个只付息月」或
        # 「尾部回收恰好落在该月」时提前退出，丢掉 prin_acct 里的留存。
        if (m > rev and live <= 1e-6 and not recovery_events
                and prin_acct <= 1e-6):
            break

        if purchase > 0:
            add_cohort(purchase, m + 1)
        m += 1
    else:
        # while 走到上界仍未满足终止条件：这是引擎错误，不能静默返回。
        raise RuntimeError(
            "cashflow 未在上界内收敛：month={}, rev={}, periods={}, "
            "live={:.2f}, prin_acct={:.2f}, pending_recovery={}".format(
                end, rev, _periods,
                sum(c["face"] - c["prin_paid"] - c["loss_rec"] for c in cohorts),
                prin_acct, len(recovery_events)))

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
