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

## 回收分布（第 3 周新增，显式建模假设）

评级报告《表15》只给出「回收周期 6 个月」和「核销后回收率」，**没有披露回收款
在 6 个月内如何分布**。本模型默认把全部回收款放在第 6 月一次性到账（与原件
「回收周期」的语义映射一致，见 cases/README.md）；`recovery_distribution` 允许
把同一笔核销的回收款按核销后月序 k=0,1,2,... 铺开，用于敏感性分析。

任何非默认分布都是**自己引入的、原件没有的假设**，不得写成披露值。

## 可购买资金的口径（第 6 周新增，`purchase_base`）

评级方法 p.6 把购买率定义为：

    购买率 = 当月新增资产规模 / **上月可用于购买的资金**

「上月可用于购买的资金」不只是当月本金回收。鼎柚说明书 p.48 写明，持续购买期内
本金分账户的资金来源包括《信托合同》11.5.1 的 (i)、(k)、(m) 三项，其中

    (m)「收入分账户」项下剩余资金记入「本金分账户」

同页还写「持续购买后的剩余资金（如有）将留存于『本金分账户』……在**下一期**持续
购买新的资产」——即未用完的部分会滚入下一期。

因此有两种口径：

    principal   当期本金回收 + 当期回收款，再乘购买率（**旧口径，默认**）
                —— 只看当期流量，忽略收入账户转入与上期留存
    available   本金分账户余额（含上期留存）+ 当期本金回收 + 当期回收款
                + 收入账户剩余转入，再乘购买率
                —— 与 p.6 的「可用于购买的资金」字面一致

两种口径给出的累计购买额差别很大，而公开披露只给了购买率的**数值**、没给它的
**分母**。哪一种是评级机构实际用的，无法从公开文件判定，故保留双口径并做对照
（见报告「二之四」节），默认值不擅自更改。

`security_cost_rate` 只在 `available` 口径下用到：算收入账户剩余时要先扣掉证券
利息，它等于 `Σ(各档票面 × 分层占比)`。现金流引擎按**初始资产池余额**计提
（循环期证券未偿本金不分摊、恒等于初始发行规模），因此与瀑布按「各档未偿本金
× 票面」算出的应付利息精确相等；早期按当期资产池余额 `live` 近似，队列摊还后
会偏小并误报优先档利息欠付（第 7 周 P0-D 修复，见 `docs/purchase-base.md`）。
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
    recovery_distribution: tuple = ()  # 回收按月分布权重；空 = 用 recovery_lag_months 定点滞后
    purchase_base: str = "principal"   # 可购买资金口径：principal / available（见下）
    security_cost_rate: float = 0.0    # 证券利息成本（年化，按初始资产池余额计提），仅 available 模式用

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
            recovery_distribution=tuple(
                float(x) for x in (d.get("recovery_distribution") or [])),
            purchase_base=str(d.get("purchase_base", "principal")),
            security_cost_rate=float(d.get("security_cost_rate", 0.0)),
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

    def recovery_weights(self):
        """回收款在核销后各月的到账权重，和为 1。

        索引 k 表示「核销发生月 + k」。未配置分布时退化为
        `[0]*lag + [1]` 的 one-hot 向量，与旧版定点滞后逐行等价；
        这是回归测试的基准，也是两单 case 的默认口径。

        配置分布时按非负权重归一化；全零或空则同样退回 one-hot。
        公开文件未披露分布形状，非默认值一律属显式建模假设。
        """
        ws = [max(float(x), 0.0) for x in (self.recovery_distribution or ())]
        total = sum(ws)
        if ws and total > 0:
            return [x / total for x in ws]
        lag = max(int(self.recovery_lag_months), 0)
        return [0.0] * lag + [1.0]
