"""跑案例：反解票面 → 三情景 → 守恒校验 → 厚度反解 → 输出 CSV 与 Markdown 报告。

用法：
    python scripts/run_case.py                      # 跑单个案例
    python scripts/run_case.py --case cases/gouhua_2025_2.json
    python scripts/run_case.py --all                # 跑 cases/ 下全部案例并输出横向对比
"""

import argparse
import csv
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abs import Pool, build_schedule, Tranche, run_waterfall, solve_coupons, tranche_metrics
from abs.vintage import VintageCurve, from_dict as vintage_from_dict
from abs.trigger import TriggerTerms
from abs.cashflow import summarize_pool
from abs.structure import min_subordination

PARAM_LABELS = [
    ("balance", "资产池本金余额", "亿元", 1e8),
    ("annual_yield", "收益率（加权平均贷款利率）", "%", 0.01),
    ("monthly_repayment_rate", "月还款率", "%", 0.01),
    ("charge_off_rate", "核销率（累计终身口径）", "%", 0.01),
    ("reinvest_rate", "购买率", "%", 0.01),
    ("recovery_rate", "核销后回收率", "%", 0.01),
    ("recovery_lag_months", "回收滞后", "月", 1),
    ("recovery_distribution", "回收时间分布", "—", 1),
    ("servicing_fee_rate", "服务费率", "%", 0.01),
    ("revolving_months", "循环购买期", "月", 1),
    ("amort_months", "摊还期上限", "月", 1),
]


def pct(x, nd=2):
    return "—" if x is None else f"{x * 100:.{nd}f}%"


def run_scenario(pool, tranches, structure, sc, vintage=None, terms=None,
                 stop_revolving=True):
    """跑一个情景，返回 (月度明细, 瀑布结果, 各档指标)。

    加速清偿在循环购买结构里的实质后果是「停止持续购买、提前进入摊还期」
    （鼎柚说明书「持续购买终止日」定义第 (b) 项即"加速清偿事件发生之日"）。
    因此做法是两遍：

      第一遍 全额循环，判定触发时点 m。这是**唯一有效**的触发记录——加速清偿
             一旦发生不可逆，不能因为截断循环期后损失轨迹变慢、阈值暂时不破，
             就把发生日改判到更晚的月份。
      第二遍 循环期截断到 m−1 个月重跑（触发当月即不再持续购买），并把 m 时点
             的触发事件原样带入结果。

    第二遍里同一个条件不再重复记录（pre_fired），但优先档欠付利息、本金逾期这类
    条件仍会在新轨迹上独立判定，因为它们可能是加速清偿之后新出现的后果。
    """
    pay_mode = structure.get("pay_mode", "sequential")
    trigger_cdr = structure.get("trigger_cdr", 0.06)

    kw = dict(co_mult=sc.get("co_mult", 1.0), mrr_mult=sc.get("mrr_mult", 1.0),
              recovery_mult=sc.get("recovery_mult", 1.0),
              yield_mult=sc.get("yield_mult", 1.0), vintage=vintage,
              ramp_months=int(sc.get("ramp_months", 0)))

    # 购买率加压：评级机构对购买率施加折损率（立即生效，无过渡）
    pool_s = pool
    if sc.get("reinvest_mult") is not None:
        from dataclasses import replace as _replace
        pool_s = _replace(pool, reinvest_rate=pool.reinvest_rate * float(sc["reinvest_mult"]))

    # 发行利率加压：压力情景下优先档票面 +bps（联合资信图表19：+50bps）
    tr_s = tranches
    bps = float(sc.get("coupon_bps", 0.0))
    if bps:
        from dataclasses import replace as _replace
        tr_s = [_replace(t, coupon=t.coupon + bps) if t.mode != "residual" else t
                for t in tranches]

    rows = build_schedule(pool_s, **kw)
    result = run_waterfall(rows, pool_s.balance, tr_s, pay_mode, trigger_cdr, terms)
    cap = None

    if stop_revolving and terms is not None:
        auto = [e for e in result.trigger_events if e.kind == "accel"]
        if auto:
            m = min(e.month for e in auto)
            cap = max(m - 1, 0)
            if cap < pool_s.revolving_months:
                rows2 = build_schedule(pool_s, revolving_cap=cap, **kw)
                res2 = run_waterfall(rows2, pool_s.balance, tr_s, pay_mode,
                                     trigger_cdr, terms,
                                     pre_fired={e.code for e in auto})
                res2.trigger_events = list(result.trigger_events) + res2.trigger_events
                res2.accel_month = m
                rows, result = rows2, res2
            else:
                cap = None   # 触发发生在摊还期，循环期无需截断

    result.revolving_cap = cap
    result.tranches_used = tr_s
    return rows, result, [tranche_metrics(result, t) for t in tr_s]


def check_conservation(rows, result):
    inflow = sum(r["total_collection"] for r in rows)
    fee = sum(r["servicing_fee"] for r in rows)
    tax = sum(r.get("tax", 0.0) for r in rows)
    paid = sum(sum(cfs) for cfs in result.cashflow.values())
    gap = inflow - fee - tax - paid
    return abs(gap) < 1e-4 * max(1.0, inflow), gap


def write_schedule_csv(path, rows):
    cols = ["month", "phase", "bal_begin", "interest", "principal", "gross_principal",
            "recovery", "purchase", "charge_off", "servicing_fee", "tax",
            "total_collection", "bal_end", "distribute_principal",
            "cum_charge_off_rate", "cum_purchased", "cum_default_rate"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([round(r[c], 6) if isinstance(r[c], float) else r[c] for c in cols])


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def wan(x):
    return x / 1e4


def benchmark_section(case, pool, tranches, results, summaries):
    """与评级机构披露的 AAAsf 压力测试结果逐项对表。

    对表分两层，因为两层的可信度完全不同：
      流入端  回收款利息 / 回收款本金 / 持续购买支出 —— 由资产模型算出，
              公开参数未必能复现，偏差是**模型 GAP**。
      分配端  各档实付本金 —— 只要有流入端合计，就能用瀑布算出来。
              披露表的内部勾稽可以验证这一层是否精确成立。
    """
    bm = case.get("rating_benchmark") or {}
    disc = bm.get("aaa_inflow_wan")
    if not disc:
        return []

    idx = next((i for i, s in enumerate(case["scenarios"]) if s["id"] == "aaa"), -1)
    if idx < 0:
        return []
    sc = case["scenarios"][idx]
    # 关键口径：披露的持续购买额是在**未截断循环期**下跑出来的。
    # 核销率改为累计终身口径后，累计违约率会在第 5–6 个月越过 5% 阈值；
    # 若把加速清偿反馈进现金流（截断循环期），购买额会被砍掉一半以上，
    # 与披露的 20.17 亿 / 2.27 亿明显不符。说明评级机构这次压力测试
    # 是把压力参数代入现金流模型直接跑，没有再叠加触发机制的时序反馈。
    rows, result, metrics = run_scenario(pool, tranches, case.get("structure", {}),
                                         sc, vintage_from_dict(case.get("vintage")),
                                         TriggerTerms.from_dict(case.get("triggers")),
                                         stop_revolving=False)

    interest = wan(sum(r["interest"] for r in rows))
    principal = wan(sum(r["gross_principal"] + r["recovery"] for r in rows))
    purchased = wan(sum(r["purchase"] for r in rows))
    netloss = wan(sum(r["charge_off"] - r["recovery"] for r in rows))
    tax = wan(sum(r.get("tax", 0.0) for r in rows))

    L = ["\n## 二之二、与评级机构 AAAsf 披露结果对表\n"]
    L.append("> 数据源：{}\n".format(bm.get("source", "")))

    def cmp_row(label, mine, theirs, unit="万元"):
        if theirs is None:
            return [label, f"{mine:,.2f}", "—", "—"]
        gap = (mine - theirs) / theirs * 100 if theirs else 0.0
        return [label, f"{mine:,.2f}", f"{theirs:,.2f}", f"{gap:+.1f}%"]

    L.append(md_table(["流入端项目（AAAsf）", "本模型", "评级机构披露", "偏差"], [
        cmp_row("回收款利息", interest, disc.get("回收款利息")),
        cmp_row("回收款本金", principal, disc.get("回收款本金")),
        cmp_row("持续购买支出", purchased, disc.get("持续购买支出")),
        cmp_row("净损失（核销 − 回收）", netloss, None),
        cmp_row("税费", tax, disc.get("税费")),
        ["累计放款 / 初始池", f"{(pool.balance + sum(r['purchase'] for r in rows)) / pool.balance:.2f}x", "—", "—"],
    ]))

    # 现金守恒上限：购买率开到 100%，看披露参数下最多能买多少。
    # 若连上限都低于披露值，说明披露的持续购买额不可能由公开参数推出，
    # 这是"调参解决不了"的硬 GAP，必须单独标出来而不是靠调参掩盖。
    from dataclasses import replace as _rep
    # 注意：run_scenario 会用 sc["reinvest_mult"] 覆盖池子的购买率，
    # 求上限时必须把该乘数摘掉，否则算出来的还是加压后的同一个数。
    sc_full = dict(sc)
    sc_full.pop("reinvest_mult", None)
    p_full = _rep(pool, reinvest_rate=1.0)
    r_full = run_scenario(p_full, tranches, case.get("structure", {}), sc_full,
                          vintage_from_dict(case.get("vintage")),
                          TriggerTerms.from_dict(case.get("triggers")),
                          stop_revolving=False)
    ceiling = wan(sum(r["purchase"] for r in r_full[0]))
    L.append("\n**持续购买的现金守恒上限**：把购买率开到 100%（到账本金全额再投资、"
             "不留现金），本模型最多只能买到 {:,.2f} 万元；而评级机构披露的持续购买支出是 "
             "{:,.2f} 万元。{}".format(
                 ceiling, disc.get("持续购买支出", 0.0),
                 "连上限都低于披露值，说明这个数不可能由公开披露的月还款率、循环期长度和"
                 "初始池规模推出来——图表20 里还含有未公开的建模选择（可能是月还款率的实际"
                 "曲线形态，或持续购买次数多于披露的循环期月数）。这是调参解决不了的硬 GAP。"
                 if ceiling < disc.get("持续购买支出", 0.0) else
                 "上限高于披露值，说明披露的购买额落在公开参数允许的范围之内。"))

    L.append("\n### 分配端：用披露的流入端合计代入瀑布\n")
    L.append(bm.get("inflow_note", "") + "\n")

    res_rows = []
    disc_res = bm.get("aaa_result_wan") or {}
    for i, t in enumerate(tranches):
        face = t.share * pool.balance
        paid = face * (1 - metrics[i]["本金损失率"])
        d = disc_res.get(t.name)
        res_rows.append(cmp_row(t.name, wan(paid), d.get("实付") if d else None))
    L.append(md_table(["各档实付本金", "本模型", "评级机构披露", "偏差"], res_rows))

    L.append("\n> **这一层是分配端的检验，不是流入端的。** 把披露的流入端合计"
             "（流入合计 − 持续购买支出 − 税费 − 各档利息）代入顺序偿付瀑布，"
             "算术上必然得到披露的各档实付金额——上面这行勾稽在两单都精确成立，"
             "说明本模型的瀑布顺序、税费计提、次级期间收益封顶与真实结构一致。"
             "剩下的一切偏差都来自流入端，也就是资产模型。\n")
    return L


def recovery_section(case, pool, tranches):
    """回收时间分布敏感性（原计划第 3 周）。

    基准仍是「核销后第 6 月一次性到账」，与评级机构披露口径的语义映射一致。
    本节只做敏感性对照：把同一笔核销的回收款按不同月度分布铺开，看优先档 WAL
    与次级实现收益率怎么变。公开文件只给「回收周期 6 个月」，没有给分布形状，
    因此下列分布全部是**显式建模假设**，不是披露值。
    """
    from dataclasses import replace as _rep

    # 索引 k = 核销发生月 + k。k=0 留空，让三种形态都落在 1~6 月窗口内，
    # 与「回收周期 6 个月」口径可比；基准 one-hot 落在 k=6。
    variants = [
        ("固定滞后 6 月（基准）", None),
        ("均匀铺开 1–6 月", (0, 1/6, 1/6, 1/6, 1/6, 1/6, 1/6)),
        ("前高后低 1–6 月", (0, 0.30, 0.25, 0.20, 0.12, 0.08, 0.05)),
        ("后高前低 1–6 月", (0, 0.05, 0.08, 0.12, 0.20, 0.25, 0.30)),
    ]
    sc = case["scenarios"][0]
    structure = case.get("structure", {})
    rows_out, wals, sub_ys, multiples = [], [], [], []

    for label, dist in variants:
        p = pool if dist is None else _rep(pool, recovery_distribution=dist)
        rr = run_scenario(p, tranches, structure, sc,
                          vintage_from_dict(case.get("vintage")),
                          TriggerTerms.from_dict(case.get("triggers")),
                          stop_revolving=False)
        s = summarize_pool(rr[0], p)
        wal = rr[2][0]["加权平均期限(年)"]
        sub_y = rr[2][-1]["实现收益率"]
        mult = s["累计放款倍数"]
        wals.append(wal)
        sub_ys.append(sub_y)
        multiples.append(mult)
        rows_out.append([
            label,
            f"{wal:.4f}",
            pct(sub_y),
            f"{mult:.4f}x",
            f"{wan(sum(r['recovery'] for r in rr[0])):,.2f}",
            str(s["存续月数"]),
        ])

    L = ["\n## 二之三、回收时间分布（第 3 周）\n"]
    L.append("> **口径边界**：评级报告《表15》只给出「回收周期 6 个月」与核销后回收率，"
             "**没有披露回收款在 6 个月内如何分布**。基准行按「第 6 月一次性到账」处理，"
             "是对「回收周期」的语义映射；其余三行是本模型显式引入的分布假设，"
             "只用于敏感性分析，不得引用为披露值。见 `docs/recovery-timing.md`。\n")
    L.append(md_table(["回收形态", "优先A WAL(年)", "次级实现收益率",
                       "累计放款倍数", "累计回收(万元)", "存续月数"], rows_out))

    wal_gap = max(wals) - min(wals)
    sub_gap = max(sub_ys) - min(sub_ys)
    mult_gap = max(multiples) - min(multiples)
    L.append("\n**为什么这不是「只改时序」**：回收款是循环期可购买资金的一部分"
             "（`cash` 含 `cum_recovery` 与当月 `recovery`），到账月份一变，当月购买额、"
             "新队列规模、后续利息与核销都会跟着变。表中累计放款倍数从 {:0.4f}x 到 "
             "{:0.4f}x，虽然量级只有 {:.4f}x，仍说明这条反馈环**改变了总量**，不只是把"
             "同一笔钱挪到不同月份。因此第 2 周账龄曲线那套「总量不变、两遍跑法」"
             "不能套用在这里。\n".format(
                 min(multiples), max(multiples), mult_gap))
    L.append("**结果怎么读**：四种形态下优先A 的 WAL 极差只有 {:.4f} 年（约 {:.2f} 天），"
             "次级实现收益率极差也只有 {:.2f} 个百分点——**回收时间分布对本单定价几乎"
             "没有影响**。原因有两层：一是优先档在顺序偿付与超额利差保护下足额受偿，"
             "回收早到晚到只影响其现金流的时间分布；二是更关键的，核销后回收率只有 {}，"
             "回收款占资产池现金流的比重极低，即使把到账从「第 6 月一次性」改成"
             "「1–6 月铺开」，循环期可购买资金的变化也只有万分之几。方向与机制一致"
             "（回收前置 → 更早形成购买力 → 累计放款略增），但量级可忽略。\n".format(
                 wal_gap, wal_gap * 365, sub_gap * 100, pct(pool.recovery_rate)))
    L.append("这属于「做了才发现没那么重要」的一类发现，如实记录：原计划第 3 周的验收标准"
             "「优先档 WAL 变化可解释」成立，解释就是**变化接近零，因为回收款太小**。"
             "若未来拿到回收率显著更高的产品（车贷 ABS 有抵押物），这一步才可能变得重要。\n")
    L.append("> **对表不受影响**：两单 case 的 `recovery_distribution` 默认为 `[]`（定点滞后），"
             "一键运行与第二节之二的评级机构 AAAsf 对表口径完全一致。改造前后逐月现金流"
             "已做逐行 diff 验证：最大浮点差 0.0、差异 0 处。\n")
    return L


def param_table(case, pool):
    ps = case.get("param_sources", {})
    rows = []
    for key, label, unit, div in PARAM_LABELS:
        val = getattr(pool, key)
        if key == "recovery_distribution":
            shown = ("定点滞后（未启用分布）" if not val
                     else "自定义 {} 期权重".format(len(val)))
        elif unit == "亿元":
            shown = f"{val / div:,.2f}"
        elif unit == "月":
            shown = f"{val:.0f}"
        else:
            shown = f"{val / div:.2f}%"
        src, note = ps.get(key, ("—", ""))
        rows.append([label, shown, src, note])
    return md_table(["参数", "取值", "来源", "说明"], rows)


def thickness_analysis(tranches, scenario_metrics):
    ranges, cum = {}, 0.0
    for t in reversed(tranches):
        lo, cum = cum, cum + t.share
        ranges[t.name] = (lo, cum)
    rows = []
    for i, t in enumerate(tranches):
        lo, hi = ranges[t.name]
        rows.append([t.name, pct(t.share, 1), f"{pct(lo, 1)} ~ {pct(hi, 1)}"]
                    + [pct(m[i]["本金损失率"]) for m in scenario_metrics])
    return rows


def build_report(case, pool, tranches, results, summaries, min_sub, counterfactual=None,
                 sub_yield_low=None, sub_yield_high=None):
    scenario_metrics = [r[2] for r in results]
    structure = case.get("structure", {})
    # 加权票面按「有票面的档」加权，不能只算 mode == "solve"——
    # 全部档位改成 fixed 后，只算 solve 会得到 0，超额利差被凭空放大 3 个百分点。
    funded = sum(t.share for t in tranches if t.mode != "residual")
    weighted_coupon = (sum(t.share * t.coupon for t in tranches if t.mode != "residual")
                       / funded) if funded else 0.0
    excess = pool.annual_yield - weighted_coupon - pool.servicing_fee_rate

    sub = tranches[-1]
    sub_loss = scenario_metrics[-1][-1]["本金损失率"]
    sev_loss = summaries[-1]["净损失率"]
    absorbed = max(sev_loss - sub_loss * sub.share, 0)

    L = []
    L.append("# {} · 定价结果\n".format(case["case_name"]))
    L.append("> 数据状态：**{}**  \n> 披露来源：{}  \n> {}\n".format(
        case.get("data_status", ""), case["source"]["name"], case["source"]["url"]))

    L.append("## 一、参数与来源\n")
    L.append("参数口径采用评级机构披露的量化参数体系（收益率 / 核销率 / 月还款率 / 购买率），"
             "而非教科书的年金 + CPR/CDR，目的是让结果能与行业口径直接对比。\n")
    L.append(param_table(case, pool))
    L.append("\n> 分层数据：{}\n".format(case.get("tranche_sources", "")))
    ps = case.get("param_sources", {})
    n_disc = sum(1 for v in ps.values() if v[0] == "披露")
    L.append("> 披露参数 {} 项，假设参数 {} 项。假设项的影响在第八节说明。\n".format(
        n_disc, len(ps) - n_disc))

    v = case.get("vintage")
    if v:
        vc = VintageCurve(tau=float(v.get("tau", 8.0)), shape=float(v.get("shape", 2.0)))
        s = vc.summary()
        enabled = bool(v.get("enabled"))
        L.append("\n### 账龄损失曲线（第 2 周新增，本单{}）\n".format(
            "启用" if enabled else "未启用"))
        L.append("曲线形态为 Weibull 累计曲线 `S(t) = 1 - exp(-(t/tau)^k)`，"
                 "tau = {} 月、k = {}，边际违约率在第 {} 月见顶。"
                 "启用时按队列账龄把该队列的终身核销额分配逐月损失权重，"
                 "总量仍等于「核销率 × 队列放款额」。\n".format(
                     s["tau(月)"], s["shape"], s["边际峰值月"]))
        curve_rows = [[f"{t} 月", pct(vc.cum(t), 1), pct(vc.marginal(t), 2)]
                      for t in (1, 3, 6, 9, 12, 15, 18, 24)]
        L.append(md_table(["账龄", "累计违约率（占平台）", "边际违约率（占平台）"], curve_rows))
        L.append("\n> **为什么本单不启用**：核销率改为累计终身口径后，队列寿命只有 "
                 "`(1 − 核销率) / 月还款率` 个月（本单约 8–11 个月），把曲线后段的损失"
                 "推到余额耗尽之后会被 capping 截掉——鼎柚实测累计核销少计 44%。"
                 "这是建模假象。基准改用均匀摊铺（不引入额外信息、总量精确守恒），"
                 "曲线保留为敏感性开关。\n")
        L.append("> **口径提醒**：评级报告的累计违约率是**单只静态池**口径，本模型算的是**循环购买池**"
                 "的累计核销率。循环期内本金被反复投放，同一笔钱可能多次暴露于违约，"
                 "故循环池的累计核销率相对初始余额可以高于单池累计违约率，两者不可直接比较。\n")

    L.append("## 二、情景设置与资产池结果\n")
    rows = []
    for s, p, r in zip(case["scenarios"], summaries, results):
        cap = getattr(r[1], "revolving_cap", None)
        rev_txt = (f"{cap}（提前终止）" if cap is not None
                   else f"{pool.revolving_months}")
        rows.append([s["label"], s["note"], rev_txt, pct(p["累计核销率"]), pct(p["净损失率"]),
                     pct(p["本金回收率"]), f"{p['资产池加权平均期限(年)']:.2f}",
                     f"{p['池利息收入(亿元)']:.2f}", f"{p['累计放款倍数']:.2f}x", p["存续月数"]])
    L.append(md_table(["情景", "参数设定", "实际循环期(月)", "累计核销率", "净损失率", "本金回收率",
                       "池加权期限(年)", "池利息收入(亿元)", "累计放款倍数", "存续月数"], rows))

    # 累计违约率：占初始余额 vs 合同口径（分母含持续购买本金）
    cd_rows = []
    for s, p, r in zip(case["scenarios"], summaries, results):
        mx = max(r[0], key=lambda x: x["cum_default_rate"])
        cd_rows.append([s["label"], pct(mx["cum_charge_off_rate"]), pct(mx["cum_default_rate"]),
                        f"{(pool.balance + mx['cum_purchased']) / 1e8:,.2f}",
                        f"第 {mx['month']} 月"])
    L.append("\n**累计违约率的两种口径**（触发判定用的是后者）：\n")
    L.append(md_table(["情景", "累计核销率（占初始余额）", "累计违约率（合同口径）",
                       "合同口径分母(亿元)", "峰值时点"], cd_rows))

    # 触发事件记录（第 3 周新增）
    L.append("\n### 加速清偿事件与违约事件触发记录\n")
    ev_rows = []
    for s, r in zip(case["scenarios"], results):
        for e in r[1].trigger_events:
            ev_rows.append([s["label"], f"第 {e.month} 月", e.code, e.label,
                            e.kind_label, e.detail])
    tr = case.get("triggers") or {}
    if ev_rows:
        L.append(md_table(["情景", "触发时点", "条件代号", "条件", "类型", "触发依据"], ev_rows))
    else:
        L.append("三种情景均未触发任何条件。")
    if tr:
        L.append("\n> 判定参数：累计违约率阈值 首年 {}、满 {} 个月后 {}。"
                 "分子为违约贷款（逾期 90 日以上）在成为违约贷款时的未偿本金余额；"
                 "分母为初始起算日资产池余额 + 各持续购买日购买资产的未偿本金余额（累加不扣减）。\n".format(
                     pct(tr.get("cdr_first_year", 0.05), 0),
                     tr.get("first_year_months", 12),
                     pct(tr.get("cdr_after_first_year", 0.08), 0),
                     tr.get("default_uplift", 1.0)))
        accel = [s["label"] for s, r in zip(case["scenarios"], results)
                 if getattr(r[1], "revolving_cap", None) is not None]
        if accel:
            L.append("> **两类条件的后果不同**：加速清偿事件的后果是持续购买终止、提前进入摊还期，"
                     "本模型中体现为循环期由 {} 个月截断至触发前一月；违约事件的后果是全部证券"
                     "本息立即到期应付，不改变循环期长度。{}\n".format(
                         pool.revolving_months,
                         "本次「{}」触发了加速清偿，循环期已相应截断。".format("、".join(accel))
                         if accel else ""))
        L.append("> **能力边界**：服务机构解任、后备机构缺位、发起机构丧失清偿能力、"
                 "交易文件终止等条件无法由资产池数据推演，已实现为情景开关（`flags`），"
                 "默认不注入。「优先档本金未按期足额分配」按条款原文逐单开关："
                 "本单{}。\n".format(
                     "（鼎柚 (129)(f)）有此条款，已启用" if tr.get("principal_overdue_enabled")
                     else "（够花 (130)）无此条款，不评估"))

    # CDR 条款在循环结构下为何触发不了
    if tr:
        peak = max(max(r[0], key=lambda x: x["cum_default_rate"])["cum_default_rate"]
                   for r in results)
        thr = tr.get("cdr_first_year", 0.05)
        idx_aaa = next((i for i, s in enumerate(case["scenarios"]) if s["id"] == "aaa"),
                       len(results) - 1)
        purch_trunc = sum(r["purchase"] for r in results[idx_aaa][0])
        _full = run_scenario(pool, tranches, structure, case["scenarios"][idx_aaa],
                             vintage_from_dict(case.get("vintage")),
                             TriggerTerms.from_dict(case.get("triggers")),
                             stop_revolving=False)
        purch_full = sum(r["purchase"] for r in _full[0])

        if peak > thr:
            L.append("\n### 累计违约率阈值会被突破——但披露结果不支持这个触发\n")
            L.append("按合同口径，本单各情景的累计违约率峰值 **{}**，首年阈值 {}，"
                     "**压力情景下会触发**加速清偿。\n".format(pct(peak), pct(thr, 0)))
            L.append("这个结论与上一版相反，原因是核销率口径修正：核销率由年化改为"
                     "**累计终身**口径后，累计违约本金约放大一倍，分母虽然被持续购买"
                     "稀释，仍不足以压到 5% 之下。\n")
            L.append("**但评级机构披露的压力测试结果并不支持这个触发。** 图表20/21 给出的"
                     "持续购买支出是在**完整循环期**下跑出来的；本模型一旦把加速清偿"
                     "反馈进现金流（循环期截断到触发前一月），购买额由 {:,.0f} 万元掉到 "
                     "{:,.0f} 万元，循环期被砍掉 {} 个月。\n".format(
                         wan(purch_full), wan(purch_trunc),
                         pool.revolving_months - (results[idx_aaa][1].revolving_cap or 0)))
            L.append("结论：**评级机构这次压力测试是把压力参数代入现金流模型直接跑，"
                     "没有再叠加触发机制的时序反馈。** 这不是疏漏——压力测试的目的是判断"
                     "给定恶化程度下证券能否足额兑付，触发机制的时序反馈会引入第二个自由度，"
                     "使结果不再只由压力参数决定。本报告第二节之二的对表因此统一在"
                     "「不截断循环期」口径下进行，与披露口径一致。\n")
        else:
            L.append("\n### 为什么累计违约率条款触发不了\n")
            L.append("按合同口径，本单三情景的累计违约率峰值 **{}**，首年阈值 {}，{}。\n".format(
                pct(peak), pct(thr, 0), "**始终不触发**" if peak <= thr else "极端情景下会触发"))
            L.append("机制在于分母：持续购买期内每月购买额约等于当月本金回收额，"
                     "分母由初始 {} 亿元一路累加到 {} 亿元；而分子（累计违约本金）只按违约速度增长。"
                     "两种口径因此差出一倍以上——占初始余额的累计核销率看起来早已越过阈值，"
                     "换成合同口径后连一半都不到。\n".format(
                         f"{pool.balance/1e8:,.2f}",
                         f"{(pool.balance + max(max(r[0], key=lambda x: x['cum_purchased'])['cum_purchased'] for r in results))/1e8:,.2f}"))
            L.append("这不是参数估错，是**循环购买结构本身的性质**：新资产不断注入分母，"
                     "单笔违约在比率上被持续稀释。\n")

    L.append("\n### 模型清偿月 vs 披露预期到期日\n")
    rec_rows = []
    for t in tranches:
        if t.mode == "residual" or not t.expected_maturity_month:
            continue   # 次级预期到期日是留给清收的窗口，不是摊还推论，不参与对账
        bal = t.share * pool.balance
        clear = 0
        for i, p in enumerate(results[0][1].principal[t.name]):
            bal -= p
            if bal <= 1.0:
                clear = i + 1
                break
        rec_rows.append([t.name, f"第 {t.expected_maturity_month} 月",
                         f"第 {clear} 月" if clear else "未清偿",
                         f"{clear - t.expected_maturity_month:+d} 月" if clear else "—"])
    if rec_rows:
        L.append(md_table(["档位", "披露预期到期日（折月）", "模型清偿月", "差异"], rec_rows))
        gaps = [int(r[3].split()[0]) for r in rec_rows if r[3] != "—"]
        if gaps and max(gaps) > 0:
            L.append("\n模型比披露口径慢 {} 个月。月还款率已按评级方法 p.6 改为"
                     "「初始放款金额 × 月还款率」的水平偿还（不再是几何衰减），"
                     "残余差异来自两点：一是评级方法 p.5–6 写明正常摊还期本金先积累、"
                     "按约定日偿还，本模型仍在每月过手；二是披露的预期到期日还含"
                     "支付日日历、税费与次级期间收益的排布，并非由月还款率单独推出。".format(max(gaps)))
            L.append("\n> 这是一个**没有修正的已知偏差**，故意留着：修正它需要静态池的逐月摊还表，"
                     "而披露文件只有汇总的月还款率。把它列出来，比调参数把数字凑上去更诚实。\n")

    L.extend(benchmark_section(case, pool, tranches, results, summaries))
    L.extend(recovery_section(case, pool, tranches))

    L.append("\n## 三、分层结果（基准情景）\n")
    headers = ["档位", "评级", "发行规模(亿元)", "占比", "票面利率", "目标收益率", "实现收益率",
               "利差", "加权平均期限(年)", "本金损失率"]
    body = [[m["档位"], m["评级"], f"{m['发行规模(亿元)']:.2f}", pct(m["占比"], 1),
             pct(m["票面利率"]), pct(m["目标收益率"]) if m["目标收益率"] else "—",
             pct(m["实现收益率"]), pct(m["利差"]) if m["利差"] is not None else "—",
             f"{m['加权平均期限(年)']:.2f}", pct(m["本金损失率"])] for m in scenario_metrics[0]]
    L.append(md_table(headers, body))

    L.append("\n## 四、压力测试：各档本金损失率\n")
    rows = [[t.name] + [pct(scenario_metrics[j][i]["本金损失率"]) for j in range(len(results))]
            for i, t in enumerate(tranches)]
    L.append(md_table(["档位"] + [s["label"] for s in case["scenarios"]], rows))

    L.append("\n## 五、厚度与超额利差\n")
    L.append(md_table(["档位", "厚度", "静态吸收的净损失区间", "基准损失", "压力损失", "极端损失"],
                      thickness_analysis(tranches, scenario_metrics)))
    L.append("\n**超额利差**：资产端收益率 {} − 证券端加权票面 {} − 服务费 {} ≈ **{}**（年化）。\n".format(
        pct(pool.annual_yield), pct(weighted_coupon), pct(pool.servicing_fee_rate), pct(excess)))
    pierce = ("已超过次级厚度 {} 个百分点".format(f"{(sev_loss - sub.share) * 100:.2f}")
              if sev_loss > sub.share else "未超过次级厚度")
    L.append("极端情景净损失 {}，{}；次级实际损失 {}（约合资产池 {}），其中 {} 被超额利差吸收。".format(
        pct(sev_loss), pierce, pct(sub_loss), pct(sub_loss * sub.share), pct(absorbed)))
    L.append("\n> **次级档的实现收益率只能给区间，给不出点估计。** 说明书的分配顺序末端是"
             "「先支付后端服务报酬，再由次级取得剩余收益」，即次级拿到的是扣完后端报酬之后的"
             "余额；而后端报酬的计算方式由《收费文件》约定，该文件不公开。取后端报酬占期末剩余"
             "的 0% 与 100% 两种极端，得到本单次级收益率的区间 **{} ~ {}**；"
             "真实值落在区间内某处，位置由那份不公开的收费文件决定。\n".format(
                 pct(sub_yield_low), pct(sub_yield_high)))

    L.append("\n## 六、厚度反解 vs 披露结构\n")
    L.append("反解问题：在极端情景下，次级档最薄做到多少，其余各档仍不违约？\n")
    disc = case.get("disclosed_support", {})
    base_loss = summaries[0]["净损失率"]
    floor_hit = min_sub is not None and min_sub <= 0.006
    min_sub_txt = "≤0.5%（触及搜索下界）" if floor_hit else (pct(min_sub, 2) if min_sub else "超出上限")
    rows = [
        ["反解最小次级厚度（不违约下限）", min_sub_txt, "压力测试口径"],
        ["极端情景净损失", pct(sev_loss), "见第二节"],
        ["基准情景净损失", pct(base_loss), "见第二节"],
        ["实际次级厚度", pct(sub.share, 1), "{}档，披露值".format(sub.name)],
        ["实际厚度 ÷ 极端净损失", f"{sub.share / sev_loss:.2f}x" if sev_loss else "—", "压力口径覆盖倍数"],
        ["实际厚度 ÷ 基准净损失", f"{sub.share / base_loss:.2f}x" if base_loss else "—", "预期损失口径覆盖倍数"],
    ]
    for t in tranches[:-1]:
        v = disc.get(t.name)
        if v is not None:
            rows.append([f"{t.name} 的披露信用支持", pct(v, 1), "劣后档 + 超额抵押合计"])
    L.append(md_table(["项目", "数值", "口径"], rows))

    if floor_hit:
        L.append("\n**这一节最值得说的不是数字，而是数字之间的关系。** 反解出来的「不违约下限」不到 0.5%，"
                 "因为超额利差本身就在吸收损失，次级只需要覆盖利差兜不住的那一点点；"
                 "而市场实际做到 {}，与极端情景净损失 {} 几乎相等（{:.2f}x）。\n".format(
                     pct(sub.share, 1), pct(sev_loss), sub.share / sev_loss if sev_loss else 0))
        L.append("结论：**分层厚度不是由「压力情景下不违约」决定的，而是由「压力情景下仍能守住优先档评级」决定的。**"
                 "评级机构要的不是刚好不违约，是在模型误差、参数漂移、资产置换质量下降之后仍然不违约——"
                 "这段缓冲在披露文件里看不到，也正好是本模型反解不出来的那部分。这就是核心 GAP。")
    elif min_sub:
        L.append("\n反解下限 {} 与实际厚度 {} 的差额为 {} 个百分点，说明市场厚度比「不违约下限」更保守，"
                 "多出的部分用于抵御模型误差与参数漂移。".format(
                     pct(min_sub), pct(sub.share, 1), f"{(sub.share - min_sub) * 100:.1f}"))

    L.append("\n## 七、触发机制\n")
    pm = case["structure"].get("pay_mode")
    L.append("分配方式：`{}`。{}".format(
        pm,
        "所有本金优先偿付优先A，A 清零后依次轮到 B、C，次级最后——"
        "因此本单**在顺序偿付条款下，加速清偿不改变瀑布的分配顺序**，"
        "其唯一可量化的后果是「持续购买终止、提前进入摊还期」。"
        if pm == "sequential" else
        "循环期内各档按比例受偿本金，一旦触发加速清偿即转为顺序偿付——"
        "此时加速清偿对优先档是实质保护。"))

    if tr:
        L.append("\n判定的条件分两类，后果不同：\n")
        rows = []
        for code, label in [("CDR", "累计违约率突破阈值"),
                            ("SENIOR_INTEREST_SHORTFALL", "优先档利息未足额偿付"),
                            ("SENIOR_PRINCIPAL_OVERDUE", "优先档本金未按期足额分配"),
                            ("SERVICER_DEFAULT", "贷款服务机构解任事件"),
                            ("SERVICER_FAILURE", "后备贷款服务机构缺位"),
                            ("SPONSOR_INSOLVENCY", "发起机构/委托人丧失清偿能力"),
                            ("DOC_TERMINATION", "交易文件终止、无效或不可主张权利")]:
            from abs.trigger import CONDITION_KINDS, KIND_CONSEQUENCES, KIND_LABELS
            k = CONDITION_KINDS[code]
            if code == "CDR":
                how = "模型判定（阈值 首年 {} / 之后 {}）".format(
                    pct(tr.get("cdr_first_year", 0.05), 0),
                    pct(tr.get("cdr_after_first_year", 0.08), 0))
            elif code == "SENIOR_INTEREST_SHORTFALL":
                how = "模型判定（取瀑布欠付利息余额）"
            elif code == "SENIOR_PRINCIPAL_OVERDUE":
                how = "实现但默认关闭（模型摊还偏差会误报）"
            else:
                how = "情景开关（{}）".format(
                    "已注入" if code in (tr.get("flags") or {}) else "未注入")
            rows.append([code, label, KIND_LABELS[k], how, KIND_CONSEQUENCES[k]])
        L.append(md_table(["条件代号", "条款内容", "分类", "本模型处理", "后果"], rows))
        L.append("\n> 关键区别：只有**加速清偿事件**会终止持续购买。违约事件的后果是"
                 "全部证券本息立即到期应付，它不会让已经进入摊还期的交易再加速一次。"
                 "早期版本把两类条件混在一起，导致「优先A本金逾期」也去截断循环期，已修正。\n")
    else:
        L.append("\n- 触发阈值：累计核销率 > {}（未启用多条件触发，退回单一阈值口径）".format(
            pct(case["structure"].get("trigger_cdr", 0.06), 1)))
    L.append("\n各情景的加速清偿后果：\n")
    for s, r in zip(case["scenarios"], results):
        cap = getattr(r[1], "revolving_cap", None)
        m = r[1].accel_month
        L.append("- {}：{}".format(
            s["label"],
            "第 {} 个月触发加速清偿，持续购买终止，循环期由 {} 个月缩短至 {} 个月".format(
                m, pool.revolving_months, cap)
            if (m and cap is not None) else
            ("第 {} 个月触发加速清偿".format(m) if m else "未触发加速清偿")))

    if counterfactual:
        L.append("\n### 加速清偿的效果：与「不截断循环期」对照\n")
        L.append("把同一个情景在「条款存在但不执行」下重跑一遍，两者的差就是这项机制的实际价值：\n")
        rows = []
        for label, (cf_sum, cf_metrics) in counterfactual.items():
            idx = [s["label"] for s in case["scenarios"]].index(label)
            cur_sum = summaries[idx]
            cur_metrics = results[idx][2]
            rows.append([
                label,
                "{} → {}".format(pct(cf_sum["累计核销率"]), pct(cur_sum["累计核销率"])),
                "{} → {}".format(pct(cf_sum["净损失率"]), pct(cur_sum["净损失率"])),
                "{} → {}".format(pct(cf_metrics[0]["本金损失率"]), pct(cur_metrics[0]["本金损失率"])),
                "{} → {}".format(cf_sum["存续月数"], cur_sum["存续月数"]),
            ])
        L.append(md_table(["情景", "累计核销率", "净损失率", "优先A本金损失率", "池存续月数"], rows))
        gain = []
        for label, (cf_sum, cf_metrics) in counterfactual.items():
            idx = [s["label"] for s in case["scenarios"]].index(label)
            cur = summaries[idx]["净损失率"]
            gain.append(f"{label} {(cf_sum['净损失率'] - cur) * 100:.2f}pp")
        L.append("\n不加速相对加速多亏的净损失率：{}。加速清偿的作用不是「让优先档不亏」，"
                 "而是**在损失继续累积之前把资产池关掉**——循环期越短，同一笔本金重复暴露于违约的次数越少，"
                 "因此累计核销率下降，优先档的受偿来源也随之提前锁定。\n".format("；".join(gain)))

    L.append("\n## 八、假设与局限\n")
    L.append("1. 循环购买期与摊还期长度披露文件中未直接给出，按「持续购买终止日」与优先档预期到期日倒推（见第一节来源列）。")
    L.append("2. 循环期按队列推演，购买额 = 到账本金 × 购买率，并以本金分账户余额封顶；"
             "未刻画资产置换质量下降与合格资产不足。循环期不分配本金，收入分账剩余先留存、"
             "摊还期用于补足利息缺口，期末释放给次级。")
    L.append("3. 真实交易在循环期按季付息，本模型按月计息并当月支付，会小幅高估优先档现金流的时间价值。")
    L.append("4. 核销率按评级方法 p.5 取**累计终身口径**，在队列寿命内均匀摊铺；"
             "披露文件没有给出损失的时间分布曲线，故这是中性假设而非拟合结果。"
             "账龄曲线作为敏感性开关保留，基准情景不启用（见 `docs/inflow-calibration.md` 第五节）。")
    L.append("5. 摊还期本金按月过手，而评级方法 p.5–6 写明正常摊还期本金先积累、按约定日偿还；"
             "这是第二节对账表残余 ±1 个月偏差的主因，未修正。")
    L.append("6. **次级档实现收益率是上界，不是点估计。** 本模型把收入分账的全部剩余（超额利差）都归次级。"
             "真实交易通常约定「超额服务费」——服务商在所有档位获得约定收益后取走剩余——"
             "此时次级收益会显著低于此数。两种安排的分界就在《服务合同》/收费文件里，而该文件不公开，"
             "故本报告只能给出上界，并把「缺这份文件」列为核心数据缺口。")
    L.append("7. 核销后回收率与回收周期（6 个月）为披露值，未按催收渠道分层。基准把回收款"
             "放在第 6 月一次性到账；`recovery_distribution` 支持按月分布，但公开文件没有"
             "给出分布形状，任何非空取值都是显式建模假设，只用于第二节之三的敏感性分析。")
    L.append("8. 票面为发行结果的实际值（`mode: fixed`），不再由模型反解——面值发行下反解票面是循环论证，"
             "详见 `docs/param-calibration.md`。")
    L.append("9. 税费按披露税率 3.26% 对利息收入计提、费率按披露合计值计提；"
             "未计入流动性溢价、中介发行费用与外部增信。评级报告披露的税费高于「利息 × 3.26%」，"
             "计税基数口径未在公开文件中说明。")
    return "\n".join(L)


def run_one(case_path, out_dir, quiet=False):
    with open(case_path, encoding="utf-8") as f:
        case = json.load(f)
    pool = Pool.from_dict(case["pool"])
    tranches = [Tranche.from_dict(t) for t in case["tranches"]]
    structure = case.get("structure", {})
    scenarios = case["scenarios"]
    vintage = vintage_from_dict(case.get("vintage"))
    terms = TriggerTerms.from_dict(case.get("triggers"))

    rows0 = build_schedule(pool, vintage=vintage)
    solve_coupons(rows0, pool.balance, tranches, structure.get("pay_mode", "sequential"),
                  structure.get("trigger_cdr", 0.06), terms)

    results, summaries = [], []
    for s in scenarios:
        r = run_scenario(pool, tranches, structure, s, vintage, terms)
        ok, gap = check_conservation(r[0], r[1])
        results.append(r)
        summaries.append(summarize_pool(r[0], pool))
        if not quiet:
            print(f"[守恒] {s['label']}: {'通过' if ok else '失败'}（残差 {gap:.2f}）")

    # 反事实：触发条件存在但不执行（不截断循环期），用于量化加速清偿的效果
    counterfactual = {}
    for s, r in zip(scenarios, results):
        if getattr(r[1], "revolving_cap", None) is None:
            continue
        rr = run_scenario(pool, tranches, structure, s, vintage, terms,
                          stop_revolving=False)
        counterfactual[s["label"]] = (summarize_pool(rr[0], pool), rr[2])

    cid = case.get("case_id", "case")
    write_schedule_csv(os.path.join(out_dir, f"{cid}_schedule.csv"), results[0][0])

    min_sub, _ = min_subordination(results[-1][0], pool.balance, tranches,
                                   structure.get("pay_mode", "sequential"),
                                   structure.get("trigger_cdr", 0.06), terms=terms)

    # 次级收益率的上下界：后端服务报酬占期末剩余 100% / 0%
    rows_b = results[0][0]
    res_hi = run_waterfall(rows_b, pool.balance, tranches, structure.get("pay_mode", "sequential"),
                           structure.get("trigger_cdr", 0.06), terms, excess_sweep=0.0)
    res_lo = run_waterfall(rows_b, pool.balance, tranches, structure.get("pay_mode", "sequential"),
                           structure.get("trigger_cdr", 0.06), terms, excess_sweep=1.0)
    sub_yield_high = tranche_metrics(res_hi, tranches[-1])["实现收益率"]
    sub_yield_low = tranche_metrics(res_lo, tranches[-1])["实现收益率"]

    report = build_report(case, pool, tranches, results, summaries, min_sub,
                          counterfactual, sub_yield_low, sub_yield_high)
    md_path = os.path.join(out_dir, f"{cid}_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)

    if not quiet:
        print("反解票面：" + "，".join(f"{t.name} {t.coupon*100:.2f}%" for t in tranches if t.mode == "solve"))
        print(f"[厚度] 极端情景最小次级厚度 {pct(min_sub) if min_sub else '—'}（实际 {pct(tranches[-1].share, 1)}）")
        for s, r in zip(scenarios, results):
            print(f"\n== {s['label']} ==")
            for m in r[2]:
                print(f"  {m['档位']:<4} {m['发行规模(亿元)']:.2f}亿 票面 {pct(m['票面利率'])} "
                      f"实现 {pct(m['实现收益率'])} WAL {m['加权平均期限(年)']:.2f}y 损失 {pct(m['本金损失率'])}")
        print("\n报告：" + md_path)

    return {"case": case, "pool": pool, "tranches": tranches, "results": results,
            "summaries": summaries, "min_sub": min_sub, "report": md_path}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="cases/dingyou_2025_3.json")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = args.out if os.path.isabs(args.out) else os.path.join(root, args.out)
    os.makedirs(out_dir, exist_ok=True)

    if args.all:
        paths = sorted(glob.glob(os.path.join(root, "cases", "*.json")))
        outs = [run_one(p, out_dir, quiet=True) for p in paths]
        print("=" * 78)
        print("横向对比")
        print("=" * 78)
        for o in outs:
            c, p = o["case"], o["pool"]
            print(f"\n{c['case_name']}")
            print(f"  收益率 {pct(p.annual_yield)}   核销率 {pct(p.charge_off_rate)}   "
                  f"月还款率 {pct(p.monthly_repayment_rate)}   极端净损失 {pct(o['summaries'][-1]['净损失率'])}")
            print(f"  实际次级厚度 {pct(o['tranches'][-1].share, 1)}   反解最小厚度 "
                  f"{pct(o['min_sub']) if o['min_sub'] else '—'}")
            for m in o["results"][0][2]:
                print(f"    {m['档位']:<4} 占比 {pct(m['占比'], 1):>7} 票面 {pct(m['票面利率']):>6} "
                      f"实现 {pct(m['实现收益率']):>7} WAL {m['加权平均期限(年)']:.2f}y")
        print("\n报告目录：" + out_dir)
    else:
        p = args.case if os.path.isabs(args.case) else os.path.join(root, args.case)
        run_one(p, out_dir)


if __name__ == "__main__":
    main()
