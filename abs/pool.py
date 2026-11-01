"""资产池参数。

第 1 层：把一只 ABS 的基础资产池抽象成 8 个参数。
这一层不做任何证券端假设，只回答一个问题——如果只有这个池子，它每月能收回多少钱。
"""

from dataclasses import dataclass


@dataclass
class Pool:
    balance: float                  # 初始本金余额（元）
    wac: float                      # 加权平均票面利率（年化，小数）
    term_months: int                # 剩余期限（月）
    cpr: float                      # 条件提前还款率（年化，小数）
    cdr: float                      # 条件违约率（年化，小数）
    recovery_rate: float            # 违约后回收率
    recovery_lag_months: int        # 回收滞后（月）
    servicing_fee_rate: float       # 服务费（年化，按月初余额计）
    seasoning_months: int = 0       # 账龄（月），仅用于说明，不改变摊还计算

    @classmethod
    def from_dict(cls, d):
        return cls(
            balance=float(d["balance"]),
            wac=float(d["wac"]),
            term_months=int(d["term_months"]),
            cpr=float(d["cpr"]),
            cdr=float(d["cdr"]),
            recovery_rate=float(d["recovery_rate"]),
            recovery_lag_months=int(d["recovery_lag_months"]),
            servicing_fee_rate=float(d["servicing_fee_rate"]),
            seasoning_months=int(d.get("seasoning_months", 0)),
        )

    def monthly_rate(self):
        return self.wac / 12.0

    def annuity_payment(self):
        """等额本息月供。消费金融类基础资产以等额本息分期为主。"""
        r = self.monthly_rate()
        n = self.term_months
        if r == 0:
            return self.balance / n
        return self.balance * r / (1 - (1 + r) ** (-n))

    def smm(self):
        """CPR → SMM（单月提前还款率）。"""
        return 1 - (1 - self.cpr) ** (1.0 / 12)

    def mdr(self):
        """CDR → 单月违约率。"""
        return 1 - (1 - self.cdr) ** (1.0 / 12)
