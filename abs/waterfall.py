"""分层瀑布。

第 3 层：把资产池每月收到的钱，按"服务费 → 各档利息 → 各档本金 → 剩余归次级"的顺序分配。

三个真实结构里必须刻画的细节：
  1. 顺序偿付（sequential）：所有本金先还优先A，A 清零后才轮到下一档。
  2. 加速清偿触发：累计核销率突破阈值即提前转为顺序偿付。
  3. 次级期间收益封顶：真实交易（如鼎柚2025三期）约定次级在优先档清偿完毕前
     按不超过 3%/年获得期间收益，超额部分留在信托里，到最后才释放。
     不做这条约束，次级收益率会被算到 50% 以上，与市场不符。
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


def run_waterfall(rows, pool_balance, tranches, pay_mode="sequential", trigger_cdr=0.06):
    names = [t.name for t in tranches]
    notional = {t.name: t.share * pool_balance for t in tranches}
    balances = dict(notional)
    cf = {n: [] for n in names}
    pr = {n: [] for n in names}
    unpaid_in = {n: 0.0 for n in names}
    reserve = 0.0
    accel_month = 0
    res = tranches[-1]
    res_income = (res.income_rate * notional[res.name] / 12.0) if res.income_rate else 0.0

    for row in rows:
        avail = row["total_collection"] - row["servicing_fee"]
        if avail < 0:
            avail = 0.0

        for n in names:
            pr[n].append(0.0)

        # 1) 利息
        for t in tranches:
            if t.mode == "residual":
                cf[t.name].append(0.0)
                continue
            due = t.coupon * balances[t.name] / 12.0
            pay = min(due, avail)
            avail -= pay
            unpaid_in[t.name] += due - pay
            cf[t.name].append(pay)

        # 2) 触发
        accelerated = accel_month != 0 or row["cum_charge_off_rate"] > trigger_cdr
        if accelerated and accel_month == 0:
            accel_month = row["month"]

        # 3) 本金
        if pay_mode == "sequential" or accelerated:
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

        # 4) 剩余：次级期间收益封顶，超额留存
        if avail > 1e-9:
            if res_income > 0:
                pay = min(res_income, avail)
                cf[res.name][-1] += pay
                avail -= pay
            reserve += avail

    if reserve > 0 and cf[res.name]:
        cf[res.name][-1] += reserve

    return WaterfallResult(
        cashflow=cf,
        principal=pr,
        notional=notional,
        unpaid_principal={n: balances[n] for n in names},
        unpaid_interest=unpaid_in,
        accel_month=accel_month,
        reserve=reserve,
    )
