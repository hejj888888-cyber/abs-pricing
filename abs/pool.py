"""资产池参数（评级机构口径）。

第 1 层：把一只消费金融 ABS 的基础资产池抽象成参数。

口径说明（重要，写进说明文档）：
  第一版用的是教科书口径——等额本息月供 + CPR/CDR。改成现在这套，是因为扒了真实产品的
  信用评级报告后发现：评级机构披露的量化参数是"收益率 / 核销率 / 月还款率 / 购买率"四个，
  而不是 CPR/CDR。

  如果我用教科书口径、行业用四参数口径，两边算出来的分层厚度根本没得比——比的是口径差异，
  不是能力差异。所以第 1 周就把参数体系换成对方的口径，这样结果才能放进同一张表。

  四个参数的来源与含义：
    收益率(annual_yield)        初始资产池加权平均贷款利率
    核销率(charge_off_rate)     历史静态池平均核销率（年化）
    月还款率(monthly_repayment) 每月本金回收占期初余额的比例（计划摊还 + 提前还款的合计速度）
    购买率(reinvest_rate)       循环期内本金回收再投资的比例，本期均为 100%
"""

from dataclasses import dataclass


@dataclass
class Pool:
    balance: float                 # 初始资产池未偿本金（元）
    annual_yield: float            # 收益率：加权平均贷款利率（年化）
    monthly_repayment_rate: float  # 月还款率：每月本金回收占期初余额比例
    charge_off_rate: float         # 核销率（年化）
    recovery_rate: float           # 核销后回收率
    recovery_lag_months: int       # 回收滞后（月）
    servicing_fee_rate: float      # 服务费率（年化，按月初余额计提）
    revolving_months: int          # 循环购买期长度（月）
    amort_months: int              # 摊还期长度上限（月）
    reinvest_rate: float = 1.0     # 购买率：循环期内本金再投资比例
    seasoning_months: int = 0      # 加权平均账龄，仅作说明

    @classmethod
    def from_dict(cls, d):
        return cls(
            balance=float(d["balance"]),
            annual_yield=float(d["annual_yield"]),
            monthly_repayment_rate=float(d["monthly_repayment_rate"]),
            charge_off_rate=float(d["charge_off_rate"]),
            recovery_rate=float(d["recovery_rate"]),
            recovery_lag_months=int(d["recovery_lag_months"]),
            servicing_fee_rate=float(d["servicing_fee_rate"]),
            revolving_months=int(d.get("revolving_months", 0)),
            amort_months=int(d.get("amort_months", 18)),
            reinvest_rate=float(d.get("reinvest_rate", 1.0)),
            seasoning_months=int(d.get("seasoning_months", 0)),
        )

    def monthly_rate(self):
        return self.annual_yield / 12.0

    def mdr(self):
        """年化核销率 → 单月核销率。"""
        return 1 - (1 - self.charge_off_rate) ** (1.0 / 12)
