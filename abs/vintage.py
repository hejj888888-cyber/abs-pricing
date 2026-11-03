"""账龄违约曲线（vintage curve）。

第 2 周新增。把"恒定核销率"换成"随账龄变化"的损失节奏。

## 为什么需要这一层

恒定核销率假设每月按同一比例计提核销，等于把损失平铺到全周期。而评级报告披露的
静态样本池数据显示：放款后前几个月几乎不违约，第 6–9 个月爬坡到峰值，账龄 15–18
个月后累计违约率收敛到平台，之后基本走平。

## 曲线形态

采用 Weibull 型累计曲线：

    S(t) = S_inf * (1 - exp(-(t / tau)^k))

    t      账龄（月），t = 0 表示刚入池
    S_inf  平台水平（累计违约率上限，占初始余额）
    tau    尺度参数，控制"多快爬到平台"
    k      形状参数，k > 1 时呈 S 型（前期慢、中期快、后期收敛）

对应地，**边际违约率**（每月新增违约占初始余额的比例）是 S 的差分：

    m(t) = S(t) - S(t-1)

边际曲线呈驼峰型：约在 t = tau * ((k-1)/k)^(1/k) 处达到峰值。注意这与累计曲线
单调上升并不矛盾——"衰减"发生在边际上，不在累计上。第一版设计曾把两者混淆。

## 从评级报告读到的标定依据

鼎柚与够花的评级报告第 13 页各有一张「静态样本池累计违约率」图（鼎柚 68 个池、
够花 67 个池）。曲线群的形态特征：

- 账龄 0–3 个月：累计违约率 0–1%
- 账龄 6 个月：约平台的 40%–60%
- 账龄 12 个月：约平台的 85%–95%
- 账龄 15–18 个月：基本收敛到平台，之后仅有极轻微上升
- **不存在下降段**（累计量按定义单调）

据此取 k = 2、tau = 8 个月作为默认参数：S(6) ≈ 43%、S(12) ≈ 89%、S(18) ≈ 99%，
与图上读数一致。

够花评级报告正文另有一句可直接引用的定量锚点：
「本期静态样本池推出的累计违约率均值为 3.46%」。

## 重要口径提醒

1. **累计违约率 ≠ 累计核销率**。核销是违约后更晚的处置环节，违约率通常高于核销率。
   本引擎只有核销指标，故曲线只用来重整时序，不用来独立决定损失总量。
2. **单池累计违约率 ≠ 循环购买池的累计核销率**。循环购买期内本金被反复投放，
   同一笔钱可能多次暴露于违约，因此循环池的累计核销量相对初始余额可以高于
   单池的累计违约率。两者不可直接比较。
"""

import math
from dataclasses import dataclass


@dataclass
class VintageCurve:
    """Weibull 型累计违约曲线。"""

    tau: float = 8.0          # 尺度参数（月）
    shape: float = 2.0        # 形状参数 k
    plateau: float = 1.0      # 平台占初始余额比例；仅用于独立试算，重整时序时不使用
    anchor: str = "pool_start"  # pool_start=从信托起算并叠加 seasoning；amort_start=从摊还期起算

    def cum(self, t):
        """账龄 t 月末的累计违约率（占初始余额，未乘 plateau）。"""
        if t <= 0:
            return 0.0
        return 1.0 - math.exp(-((t / self.tau) ** self.shape))

    def marginal(self, t):
        """账龄第 t 月的新增违约率。t 从 1 开始。"""
        return self.cum(t) - self.cum(t - 1)

    def peak_month(self):
        """边际违约率峰值所在月份（连续近似）。"""
        k = self.shape
        if k <= 1.0:
            return 1.0
        return self.tau * (((k - 1.0) / k) ** (1.0 / k))

    def weights(self, n_months, shift=0):
        """前 n_months 个月的边际权重，和为 1。shift 为起算账龄偏移（已 seasoning 的月数）。"""
        if n_months <= 0:
            return []
        ws = [self.marginal(t + shift) for t in range(1, n_months + 1)]
        total = sum(ws)
        if total <= 0:
            return [1.0 / n_months] * n_months
        return [w / total for w in ws]

    def amounts(self, n_months, total_loss, shift=0):
        """把总量 total_loss 按曲线分配到各月。"""
        return [w * total_loss for w in self.weights(n_months, shift)]

    def allocate(self, n_months, total_loss, revolving_months=0, seasoning_months=0):
        """按 anchor 口径把总量分配到各月。

        pool_start  ：曲线自信托起算，首月账龄即 seasoning_months。适合"入池时资产已有一
                      定账龄、后续不再买入"的静态池。
        amort_start ：曲线自摊还期起算，循环期内不计提核销。适合"循环期内不断买入新资产、
                      老资产占比被稀释"的循环购买池。
        """
        if self.anchor == "amort_start":
            start = max(int(revolving_months), 0)
            tail = max(n_months - start, 1)
            return [0.0] * start + self.amounts(tail, total_loss, shift=0)
        return self.amounts(n_months, total_loss, shift=int(seasoning_months))

    def summary(self):
        return {
            "tau(月)": self.tau,
            "shape": self.shape,
            "anchor": self.anchor,
            "边际峰值月": round(self.peak_month(), 2),
            "S(6)": self.cum(6),
            "S(12)": self.cum(12),
            "S(18)": self.cum(18),
            "S(24)": self.cum(24),
        }


def from_dict(d):
    """从 case JSON 的 vintage 字段构造；返回 None 表示不启用。"""
    if not d or not d.get("enabled", False):
        return None
    return VintageCurve(
        tau=float(d.get("tau", 8.0)),
        shape=float(d.get("shape", 2.0)),
        plateau=float(d.get("plateau", 1.0)),
        anchor=d.get("anchor", "pool_start"),
    )
