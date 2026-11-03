"""加速清偿事件与违约事件判定。

第 3 周新增（原第 4 周任务提前）。

## 为什么换掉单一阈值

第 1 周用「累计核销率突破 6%」当加速清偿触发，是个占位实现。核对发行说明书后，
真实条款是多条件的，而且量化指标用的是**累计违约率**而不是核销率。

鼎柚三期（说明书加速清偿事件条款）自动生效的条件：

1. 发起机构发生任何丧失清偿能力事件
2. 发生任何贷款服务机构解任事件
3. 贷款服务机构未按时、足额付款或划转资金
4. 需更换受托人/贷款服务机构/后备贷款服务机构，但 90 日内未找到合格继任者
5. 累计违约率：自信托生效日起一年内（含）超过 5%；满一年后超过 8%
6. 优先A/B/C 预期到期日前一个信托分配日，无法按约定顺序足额分配相应档次未偿本金

另有需经宣布生效的四条（义务未履行且 30 日未补救、陈述保证虚假、重大不利事件、
交易文件终止或无效）。

够花二期的结构类似，只有第 5 条阈值不同：第一年度 5%，第二年及以后 10%；
且第 6 条被替换为后备服务机构任命相关的条件。

## 加速清偿事件 ≠ 违约事件

这两类条件的**后果完全不同**，不能混在一个「触发」标签下：

    加速清偿事件（accel）
        后果：持续购买终止、提前进入摊还期、分配顺序转为优先级顺序。
        本模型里可量化的只有累计违约率一条，其余为情景开关。

    违约事件（eod）
        后果：全部证券本息立即到期应付，信托提前终止清算。
        本模型里可量化的是优先档利息未足额偿付、优先档本金未按期足额分配。

早期版本把所有自动生效条件都当作加速清偿，结果「优先A本金逾期」也去截断循环期，
把已经进入摊还期的交易又"加速"一次，属于把两类条款用错了地方。本版按后果分类。

## 累计违约率的口径（第 3 周按说明书原文重写）

鼎柚第 142 项「累计违约率」（够花第 69 项同）：

    A = 各收款期间内所有「违约贷款」在成为违约贷款时的未偿本金余额之和
    B = 初始起算日资产池余额 + 各「持续购买日」购买的资产在「持续购买基准日」的
        未偿本金余额之和

两处都比第一版想当然的写法更具体：

1. **分子不是核销额，是违约额。** 但两者在消费金融口径下是同一批资产——
   联合资信评级方法 p.5 写明「逾期超过 30 天记为违约资产，逾期超过 90 天记为核销资产」，
   而两单说明书的「违约贷款」定义为逾期超过 90 日。故累计核销额可直接作为分子，
   **不需要再乘一个「违约→核销」的系数**。原 `default_uplift` 承担的那个作用不成立。

2. **分母要加历次持续购买的本金，而且是累加、不扣减。** 这一条的后果极大：
   循环购买期内每月购买额约等于当月本金回收额，分母从初始余额一路膨胀。
   鼎柚 8 个月循环期后，分母由 15.03 亿涨到约 39 亿，累计违约率被稀释到
   占初始余额口径的一半以下。**按合同口径，两单在所有压力情景下都不会触发
   累计违约率条款**——这是循环购买结构里 CDR 触发近乎"死条款"的机制性原因。

本模块因此改为接收**已按合同口径算好的**累计违约率，不再自行用核销率换算。
`default_uplift` 保留，但只作敏感性试算用，不代表任何定义。

## 条款有无比默认值更重要

`principal_overdue_enabled` 不是"要不要严格"的开关，是**这一单有没有这条条款**：

- 鼎柚 (129)(f) 明确列出「在 A/B/C 档预期到期日前一个信托分配日，依分配顺序无法足额
  分配对应档次在预期到期日届至后的未偿本金余额」→ 有，启用。
- 够花的 (130) 加速清偿事件只有四条（委托人丧失清偿能力、服务机构解任、后备机构缺位、
  累计违约率），**没有**预期到期日相关的本金触发条款 → 没有，关闭。

早期版本因为模型的摊还速度比披露的预期到期日慢 2–3 个月而把它整体关闭，是拿
"模型偏差"当理由掩盖了"条款有无"这个事实。摊还口径修正后差异已缩到 ±1 个月，
可以按条款原文逐单开关。
"""

from dataclasses import dataclass, field


@dataclass
class TriggerEvent:
    month: int
    code: str
    label: str
    kind: str = "accel"      # accel=加速清偿事件；eod=违约事件；declare=需经宣布
    detail: str = ""

    @property
    def kind_label(self):
        return KIND_LABELS.get(self.kind, self.kind)

    def as_row(self):
        return [self.month, self.code, self.label, self.kind_label, self.detail]


@dataclass
class TriggerTerms:
    """触发条款。字段名尽量贴近说明书原文的用词。"""

    cdr_first_year: float = 0.05        # 累计违约率阈值：首年
    cdr_after_first_year: float = 0.08  # 累计违约率阈值：满一年后
    first_year_months: int = 12
    default_uplift: float = 1.0         # 累计违约率 = 累计核销率 × 该系数（假设）
    flags: dict = field(default_factory=dict)   # {code: month}，情景开关
    principal_overdue_enabled: bool = False     # 见模块说明
    enabled: bool = True

    def threshold(self, month):
        return self.cdr_first_year if month <= self.first_year_months else self.cdr_after_first_year

    def cum_default_rate(self, cum_charge_off_rate):
        return cum_charge_off_rate * self.default_uplift

    @classmethod
    def from_dict(cls, d):
        """返回 None 表示不启用多条件触发，退回旧的单一阈值逻辑。"""
        if not d or not d.get("enabled", True):
            return None
        return cls(
            cdr_first_year=float(d.get("cdr_first_year", 0.05)),
            cdr_after_first_year=float(d.get("cdr_after_first_year", 0.08)),
            first_year_months=int(d.get("first_year_months", 12)),
            default_uplift=float(d.get("default_uplift", 1.0)),
            flags=dict(d.get("flags", {})),
            principal_overdue_enabled=bool(d.get("principal_overdue_enabled", False)),
            enabled=True,
        )


# 条件代号 → 中文名（与说明书条款一一对应）
CONDITION_LABELS = {
    "CDR": "累计违约率突破阈值",
    "SENIOR_INTEREST_SHORTFALL": "优先档利息未足额偿付",
    "SENIOR_PRINCIPAL_OVERDUE": "优先档本金未按期足额分配",
    "SERVICER_DEFAULT": "贷款服务机构解任事件",
    "SERVICER_FAILURE": "后备贷款服务机构缺位",
    "SPONSOR_INSOLVENCY": "发起机构/委托人丧失清偿能力",
    "DOC_TERMINATION": "交易文件终止、无效或不可主张权利",
}

# 条件后果分类：决定「停止持续购买」还是「证券立即到期」。
# 分类依据是鼎柚说明书 (129) 加速清偿事件 / (130) 违约事件 / (131) 权利完善事件，
# 以及够花 (130)/(131)/(132)。注意 (129)(f) 把「优先档本金在预期到期日前一个分配日
# 无法足额分配」列为**自动生效的加速清偿事件**——早期版本按后果把它归成违约事件，错了。
CONDITION_KINDS = {
    "CDR": "accel",
    "SENIOR_PRINCIPAL_OVERDUE": "accel",
    "SERVICER_DEFAULT": "accel",
    "SERVICER_FAILURE": "accel",
    "SPONSOR_INSOLVENCY": "accel",
    "SENIOR_INTEREST_SHORTFALL": "eod",        # 说明书：支付日后 5 个工作日未付 → 违约事件
    "DOC_TERMINATION": "declare",
}

KIND_LABELS = {
    "accel": "加速清偿事件",
    "eod": "违约事件",
    "declare": "需经宣布",
}

# 触发后必须另起一档的后果说明，供报告引用
KIND_CONSEQUENCES = {
    "accel": "持续购买终止，提前进入摊还期",
    "eod": "全部证券本息立即到期应付",
    "declare": "经受托人宣布后生效",
}


def evaluate_month(month, cum_charge_off_rate, senior_unpaid_interest,
                   overdue_principal_tranches, terms, fired):
    """判当月是否新触发条件。返回新增事件列表。

    month                       当前月份（1 起）
    cum_charge_off_rate         累计核销率（占初始余额）
    senior_unpaid_interest      各优先档累计欠付利息 {档名: 金额}
    overdue_principal_tranches  已过预期到期月但仍有余额的优先档名列表
    terms                       TriggerTerms
    fired                       已触发过的条件代号集合（避免重复记录）
    """
    events = []

    def add(code, detail):
        if code in fired:
            return
        fired.add(code)
        events.append(TriggerEvent(month, code, CONDITION_LABELS[code],
                                   CONDITION_KINDS.get(code, "accel"), detail))

    thr = terms.threshold(month)
    cdr = terms.cum_default_rate(cum_charge_off_rate)
    if cdr > thr:
        add("CDR",
            f"累计违约率 {cdr*100:.2f}% > 阈值 {thr*100:.0f}%"
            f"（合同口径：分母含累计持续购买本金）")

    short = {k: v for k, v in senior_unpaid_interest.items() if v > 1.0}
    if short:
        detail = "；".join(f"{k} 欠付 {v/1e4:.1f} 万元" for k, v in short.items())
        add("SENIOR_INTEREST_SHORTFALL", detail)

    if overdue_principal_tranches:
        add("SENIOR_PRINCIPAL_OVERDUE",
            "已过预期到期月仍有余额：" + "、".join(overdue_principal_tranches))

    for code, m in terms.flags.items():
        if code in CONDITION_LABELS and month >= int(m):
            add(code, f"情景开关：自第 {int(m)} 月起")

    return events
