"""分层瀑布。

第 3 层：把资产池每月收到的钱，按"服务费 → 各档利息 → 各档本金 → 剩余归次级"的顺序分配。

三个真实结构里必须刻画的细节：
  1. 顺序偿付（sequential）：所有本金先还优先A，A 清零后才轮到下一档。
  2. 加速清偿触发：累计违约率突破阈值即提前转为顺序偿付。
  3. 次级期间收益封顶：真实交易（如鼎柚2025三期）约定次级在优先档清偿完毕前
     按不超过 3%/年获得期间收益，超额部分留在信托里，到最后才释放。
     不做这条约束，次级收益率会被算到 50% 以上，与市场不符。

## 循环购买期不分配本金（第 3 周修正）

早期版本在循环期也执行本金分配，把「利息收入 − 服务费 − 各档利息」的余额
当成本金付给了优先A。这是个实质性错误：循环期内本金回收款按购买率全额再投资，
资产池余额不变（见 cashflow.py），此时若再拿收入分账的剩余去还本，等于把
超额利差当作本金支付，凭空缩短优先档期限。

真实结构中，循环期的收入分账剩余有两个去向：
  a) 转入本金分账，用于支付持续购买的价款（即资产池实际规模大于初始规模）；
  b) 留存在信托账户，摊还期用于补足利息缺口，期末释放给次级。
本模型采用 (b)：循环期只付息，剩余留存在 `reserve`；摊还期 `reserve` 优先用于
补足优先档利息缺口（避免把「账上有钱却欠息」算成违约事件），期末余额释放给次级。
"""

from dataclasses import dataclass, field


@dataclass
class Tranche:
    name: str
    share: float
    rating: str = ""
    mode: str = "solve"             # solve=反解票面；fixed=用给定票面；residual=剩余收益
    target_yield: float = 0.0
    coupon: float = 0.0
    reference_yield: float = 0.015
    income_rate: float = 0.0        # residual 档期间收益率上限（年化），0 表示不分期间收益
    expected_maturity_month: int = 0  # 预期到期月（1 起，0 表示不适用）

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=d["name"],
            share=float(d["share"]),
            rating=d.get("rating", ""),
            mode=d.get("mode", "solve"),
            target_yield=float(d.get("target_yield", 0.0)),
            coupon=float(d.get("coupon", 0.0)),
            reference_yield=float(d.get("reference_yield", 0.015)),
            income_rate=float(d.get("income_rate", 0.0)),
            expected_maturity_month=int(d.get("expected_maturity_month", 0)),
        )


@dataclass
class WaterfallResult:
    cashflow: dict = field(default_factory=dict)      # {档名: [每月总现金流]}
    principal: dict = field(default_factory=dict)     # {档名: [每月本金流]}
    notional: dict = field(default_factory=dict)
    unpaid_principal: dict = field(default_factory=dict)
    unpaid_interest: dict = field(default_factory=dict)
    accel_month: int = 0
    reserve: float = 0.0                              # 期末释放给次级的留存超额收益
    excess_servicing: float = 0.0                     # 归服务商的超额服务费（excess_sweep=True 时）
    trigger_events: list = field(default_factory=list)  # 触发事件明细（第 3 周新增）


def run_waterfall(rows, pool_balance, tranches, pay_mode="sequential", trigger_cdr=0.06,
                  terms=None, pre_fired=None, excess_sweep=0.0):
    """terms 不为 None 时启用多条件触发；否则退回单一累计核销率阈值。

    pre_fired      已判定触发的条件代号集合。加速清偿一旦发生不可逆，重跑时不能再
                   把同一个条件当成"新事件"记录，故由调用方传进来。
    excess_sweep   期末剩余中作为**后端服务报酬**归服务商的比例，[0, 1]。

                   鼎柚说明书分配顺序末端：(f) 支付后端服务报酬 → (g) 剩余全部作为
                   次级收益。也就是说次级收益是**扣完后端报酬之后的余额**，而后端
                   报酬的计算方式由《收费文件》约定，该文件不公开。
                   因此 0 与 1 分别给出次级收益率的上界与下界，真实值在区间内。
                   默认 0（上界），与早期版本一致。
    """
    names = [t.name for t in tranches]
    notional = {t.name: t.share * pool_balance for t in tranches}
    balances = dict(notional)
    cf = {n: [] for n in names}
    pr = {n: [] for n in names}
    unpaid_in = {n: 0.0 for n in names}
    reserve = 0.0
    excess_servicing = 0.0
    accel_month = 0
    res = tranches[-1]
    # 次级期间收益按**当期未偿本金余额**计（鼎柚说明书 p.92-93：
    # 最高期间收益 = 该支付日本金兑付前的未偿本金余额 × 3%/年 × 计息期间天数 ÷ 365，
    # 单利；首期以信托生效日面值为基数）。早期版本按发行面值固定计，基数偏大。
    res_income_rate = res.income_rate / 12.0 if res.income_rate else 0.0
    events = []
    fired = set(pre_fired or ())

    for row in rows:
        avail = row["total_collection"] - row["servicing_fee"]
        if avail < 0:
            avail = 0.0

        for n in names:
            pr[n].append(0.0)

        # 1) 利息。先算出当期应付总额，不足时动用留存超额收益补足，
        #    避免"账上有钱却欠息"被误判为优先档利息未足额偿付。
        payable = [(t, t.coupon * balances[t.name] / 12.0)
                   for t in tranches if t.mode != "residual"]
        total_due = sum(d for _, d in payable)
        if avail < total_due and reserve > 0:
            draw = min(reserve, total_due - avail)
            avail += draw
            reserve -= draw

        for t in tranches:
            if t.mode == "residual":
                cf[t.name].append(0.0)
                continue
            due = t.coupon * balances[t.name] / 12.0
            pay = min(due, avail)
            avail -= pay
            unpaid_in[t.name] += due - pay
            cf[t.name].append(pay)

        accelerated = accel_month != 0

        # 2) 本金。循环购买期不分配本金（见模块顶部说明）。
        if row["phase"] == "revolving":
            pass
        elif pay_mode == "sequential" or accelerated:
            for t in tranches:
                principal = min(avail, balances[t.name])
                if principal > 0:
                    avail -= principal
                    balances[t.name] -= principal
                    cf[t.name][-1] += principal
                    pr[t.name][-1] += principal
        else:
            total_bal = sum(balances.values())
            if total_bal > 0:
                pot = avail
                for t in tranches:
                    principal = min(pot * balances[t.name] / total_bal, balances[t.name])
                    avail -= principal
                    balances[t.name] -= principal
                    cf[t.name][-1] += principal
                    pr[t.name][-1] += principal

        # 3) 触发判定（放在本金分配之后）
        #    原因：本金逾期条件要看「分配后是否还有余额」。放在分配前，
        #    优先档在预期到期日当月刚要还清时会被误判为逾期——鼎柚基准情景
        #    就是这样被误报了一条加速清偿。
        #    副作用：触发当月的分配方式不变，从下月起才按加速后执行，
        #    这与「事件发生后次一分配日起生效」的实务处理一致。
        if terms is not None:
            from .trigger import evaluate_month

            if terms.principal_overdue_enabled:
                # 判定点：原件是「预期到期日前一个信托分配日」预判能否足额分配；
                # 模型按「预期到期日当月末仍未清偿」近似，时点晚约 1 个月。
                overdue = [t.name for t in tranches
                           if t.mode != "residual" and t.expected_maturity_month
                           and row["month"] >= t.expected_maturity_month
                           and balances[t.name] > 1.0]
            else:
                overdue = []   # 见 trigger.py 模块说明：模型摊还偏差会误报
            senior_unpaid = {t.name: unpaid_in[t.name] for t in tranches
                             if t.mode != "residual"}
            # 用合同口径的累计违约率：分母含历次持续购买本金
            cdr = row.get("cum_default_rate", row["cum_charge_off_rate"])
            new_events = evaluate_month(row["month"], cdr,
                                        senior_unpaid, overdue, terms, fired)
            if new_events:
                events.extend(new_events)
                # 只有加速清偿事件会终止持续购买；违约事件的后果是证券提前到期，
                # 不改变循环期长度。见 trigger.py「加速清偿事件 ≠ 违约事件」。
                if accel_month == 0 and any(e.kind == "accel" for e in new_events):
                    accel_month = row["month"]
        elif accel_month == 0 and row["cum_charge_off_rate"] > trigger_cdr:
            accel_month = row["month"]

        # 4) 剩余：次级期间收益封顶，超额留存或归服务商
        if avail > 1e-9:
            if res_income_rate:
                # 可分配资金不足时，不足部分下一期不予补足（够花说明书 p.44）
                pay = min(res_income_rate * balances[res.name], avail)
                cf[res.name][-1] += pay
                avail -= pay
            reserve += avail

    if reserve > 0 and cf[res.name]:
        # 期末释放：先支付后端服务报酬，余额才归次级（鼎柚说明书分配顺序 (f)→(g)）
        fee = reserve * max(min(excess_sweep, 1.0), 0.0)
        excess_servicing += fee
        cf[res.name][-1] += reserve - fee

    return WaterfallResult(
        cashflow=cf,
        principal=pr,
        notional=notional,
        unpaid_principal={n: balances[n] for n in names},
        unpaid_interest=unpaid_in,
        accel_month=accel_month,
        reserve=reserve,
        excess_servicing=excess_servicing,
        trigger_events=events,
    )
