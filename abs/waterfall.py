"""分层瀑布。

第 3 层：把资产池每月收到的钱，按"服务费 → 各档利息 → 各档本金 → 剩余归次级"的顺序分配。

支持两种分配方式，这是真实 ABS 结构差异的核心：
  sequential              顺序偿付：所有本金先还优先 A，A 清零后才还夹层
  pro_rata_with_trigger   按比例偿付，一旦累计违约率突破触发值即"加速清偿"转为顺序偿付
"""

from dataclasses import dataclass, field


@dataclass
class Tranche:
    name: str
    share: float                    # 占初始资产池的比例
    rating: str = ""
    mode: str = "solve"             # solve=按目标收益率反解票面；residual=不计息，拿剩余
    target_yield: float = 0.0       # 目标收益率（年化）
    coupon: float = 0.0             # 反解得到的票面利率（年化）
    reference_yield: float = 0.03   # 定价基准（同期限无风险利率），用于算利差

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=d["name"],
            share=float(d["share"]),
            rating=d.get("rating", ""),
            mode=d.get("mode", "solve"),
            target_yield=float(d.get("target_yield", 0.0)),
            coupon=float(d.get("coupon", 0.0)),
            reference_yield=float(d.get("reference_yield", 0.03)),
        )


@dataclass
class WaterfallResult:
    cashflow: dict = field(default_factory=dict)     # {档名: [每月现金流]}
    notional: dict = field(default_factory=dict)     # {档名: 发行规模}
    unpaid_principal: dict = field(default_factory=dict)
    unpaid_interest: dict = field(default_factory=dict)
    accel_month: int = 0                             # 触发加速清偿的月份，0 表示未触发
    reserve: float = 0.0                             # 期末超额收益账户余额


def run_waterfall(rows, pool_balance, tranches, pay_mode="sequential", trigger_cdr=0.06):
    names = [t.name for t in tranches]
    notional = {t.name: t.share * pool_balance for t in tranches}
    balances = dict(notional)
    cf = {n: [] for n in names}
    unpaid_pr = {n: 0.0 for n in names}
    unpaid_in = {n: 0.0 for n in names}
    reserve = 0.0
    accel_month = 0
    residual_name = tranches[-1].name

    for row in rows:
        avail = row["total_collection"] - row["servicing_fee"]
        if avail < 0:
            avail = 0.0

        # 1) 利息：按档位顺序支付
        for t in tranches:
            if t.mode == "residual":
                cf[t.name].append(0.0)
                continue
            due = t.coupon * balances[t.name] / 12.0
            pay = min(due, avail)
            avail -= pay
            unpaid_in[t.name] += due - pay
            cf[t.name].append(pay)

        # 2) 触发判断
        accelerated = accel_month != 0 or row["cum_default_rate"] > trigger_cdr
        if accelerated and accel_month == 0:
            accel_month = row["month"]

        # 3) 本金：顺序 or 按比例
        use_sequential = (pay_mode == "sequential") or accelerated
        if use_sequential:
            for t in tranches:
                principal = min(avail, balances[t.name])
                if principal > 0:
                    avail -= principal
                    balances[t.name] -= principal
                    cf[t.name][-1] += principal
        else:
            total_bal = sum(balances.values())
            if total_bal > 0:
                pool_avail = avail
                for t in tranches:
                    principal = pool_avail * balances[t.name] / total_bal
                    principal = min(principal, balances[t.name])
                    avail -= principal
                    balances[t.name] -= principal
                    cf[t.name][-1] += principal

        # 4) 剩余现金流归次级（超额收益）
        if avail > 1e-9:
            reserve += avail
            cf[residual_name][-1] += avail
            avail = 0.0

    for n in names:
        unpaid_pr[n] = balances[n]

    return WaterfallResult(
        cashflow=cf,
        notional=notional,
        unpaid_principal=unpaid_pr,
        unpaid_interest=unpaid_in,
        accel_month=accel_month,
        reserve=reserve,
    )
