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
    核销率(charge_off_rate)     **累计终身**核销率（分母 = 静态池初始放款金额）
    月还款率(monthly_repayment) 每月本金回收占**初始放款金额**的比例
    购买率(reinvest_rate)       循环期内到账本金再投资的比例

## 核销率是累计终身口径，不是年化率（第 4 周修正）

《联合资信 消费金融类资产支持证券评级方法》p.5 原文：

    核销率 = 正常还款周期 90 天后仍逾期的未偿本金余额 / 静态资产池初始放款金额

分子是最终核销掉的本金，分母是初始放款金额，所以核销率是**一个队列在整个生命
周期内的累计损失率**。第一版把它当年化率，用 `1-(1-r)^(1/12)` 拆月后乘到逐月
递减的余额上，累计损失只有正确口径的一半左右——这是模型相对评级机构偏乐观的
最主要来源。

同理，月还款率的分母也是「静态资产池初始放款金额」，不是当期余额（方法 p.6）。
两个指标同分母，队列寿命因此由二者共同决定，见 cashflow.py 顶部说明。
"""

from dataclasses import dataclass


@dataclass
class Pool:
    balance: float                 # 初始资产池未偿本金（元）
    annual_yield: float            # 收益率：加权平均贷款利率（年化）
    monthly_repayment_rate: float  # 月还款率：每月本金回收占期初余额比例
    charge_off_rate: float         # 核销率：**累计终身**口径（占初始放款金额），非年化
    recovery_rate: float           # 核销后回收率
    recovery_lag_months: int       # 回收滞后（月）
    servicing_fee_rate: float      # 服务费率（年化，按月初余额计提）
    revolving_months: int          # 循环购买期长度（月）
    amort_months: int              # 摊还期长度上限（月）
    reinvest_rate: float = 1.0     # 购买率：循环期内本金再投资比例
    seasoning_months: int = 0      # 加权平均账龄，仅作说明
    tax_rate: float = 0.0          # 证券税率（年化，对利息收入计提）
    revolving_first_pay_interest_only: bool = False  # 摊还期首个支付日只付息不还本
    yield_ramp_months: int = -1    # 收益率加压过渡月数；-1 表示与 ramp_months 相同

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
            tax_rate=float(d.get("tax_rate", 0.0)),
            revolving_first_pay_interest_only=bool(
                d.get("revolving_first_pay_interest_only", False)),
            yield_ramp_months=int(d.get("yield_ramp_months", -1)),
        )

    def monthly_rate(self):
        return self.annual_yield / 12.0

    def lifetime_charge_off(self):
        """累计终身核销率（占初始放款金额）。"""
        return self.charge_off_rate

    def cohort_life_months(self, mrr=None):
        """队列寿命（月）：(1 − 核销率) / 月还款率。见 cashflow.py 顶部说明。"""
        r = self.monthly_repayment_rate if mrr is None else mrr
        return max(int(round((1.0 - min(self.charge_off_rate, 0.9)) / max(r, 1e-6))), 1)
