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
    ("charge_off_rate", "核销率（年化）", "%", 0.01),
    ("reinvest_rate", "购买率", "%", 0.01),
    ("recovery_rate", "核销后回收率", "%", 0.01),
    ("recovery_lag_months", "回收滞后", "月", 1),
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
              yield_mult=sc.get("yield_mult", 1.0), vintage=vintage)

    rows = build_schedule(pool, **kw)
    result = run_waterfall(rows, pool.balance, tranches, pay_mode, trigger_cdr, terms)
    cap = None

    if stop_revolving and terms is not None:
        auto = [e for e in result.trigger_events if e.kind == "accel"]
        if auto:
            m = min(e.month for e in auto)
            cap = max(m - 1, 0)
            if cap < pool.revolving_months:
                rows2 = build_schedule(pool, revolving_cap=cap, **kw)
                res2 = run_waterfall(rows2, pool.balance, tranches, pay_mode,
                                     trigger_cdr, terms,
                                     pre_fired={e.code for e in auto})
                res2.trigger_events = list(result.trigger_events) + res2.trigger_events
                res2.accel_month = m
                rows, result = rows2, res2
            else:
                cap = None   # 触发发生在摊还期，循环期无需截断

    result.revolving_cap = cap
    return rows, result, [tranche_metrics(result, t) for t in tranches]


def check_conservation(rows, result):
    inflow = sum(r["total_collection"] for r in rows)
    fee = sum(r["servicing_fee"] for r in rows)
    paid = sum(sum(cfs) for cfs in result.cashflow.values())
    gap = inflow - fee - paid
    return abs(gap) < 1e-4 * max(1.0, inflow), gap


def write_schedule_csv(path, rows):
    cols = ["month", "phase", "bal_begin", "interest", "principal", "charge_off",
            "recovery", "servicing_fee", "total_collection", "bal_end", "cum_charge_off_rate"]
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


def param_table(case, pool):
    ps = case.get("param_sources", {})
    rows = []
    for key, label, unit, div in PARAM_LABELS:
        val = getattr(pool, key)
        if unit == "亿元":
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


def build_report(case, pool, tranches, results, summaries, min_sub, counterfactual=None):
    scenario_metrics = [r[2] for r in results]
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
    if v and v.get("enabled"):
        vc = VintageCurve(tau=float(v.get("tau", 8.0)), shape=float(v.get("shape", 2.0)))
        s = vc.summary()
        L.append("\n### 账龄损失曲线（第 2 周新增）\n")
        L.append("核销率不再按月恒定计提，改用 Weibull 累计曲线重整时序："
                 "`S(t) = 1 - exp(-(t/tau)^k)`，取 tau = {} 月、k = {}，"
                 "边际违约率在第 {} 月见顶。\n".format(s["tau(月)"], s["shape"], s["边际峰值月"]))
        curve_rows = [[f"{t} 月", pct(vc.cum(t), 1), pct(vc.marginal(t), 2)]
                      for t in (1, 3, 6, 9, 12, 15, 18, 24)]
        L.append(md_table(["账龄", "累计违约率（占平台）", "边际违约率（占平台）"], curve_rows))
        L.append("\n> **关键设计**：曲线只重整时间分布，不改变损失总量——总量仍等于恒定模式"
                 "算出的累计核销额。这样结果差异纯粹来自时序效应，不会与「总量估错」混在一起。\n")
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
                     f"{p['池利息收入(亿元)']:.2f}", p["存续月数"]])
    L.append(md_table(["情景", "参数设定", "实际循环期(月)", "累计核销率", "净损失率", "本金回收率",
                       "池加权期限(年)", "池利息收入(亿元)", "存续月数"], rows))

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
        L.append("\n> 判定参数：累计违约率阈值 首年 {}、满 {} 个月后 {}；"
                 "违约率 = 累计核销率 × 上调系数 {}。\n".format(
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
                 "默认不注入。累计违约率与累计核销率的换算系数无实证支撑，属显式假设。"
                 "「优先档本金未按期足额分配」一条实现但默认关闭——模型的摊还速度与披露的"
                 "预期到期日存在系统性偏差（见下节对账表），用模型口径判会把口径差误报成违约事件。\n")

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
            L.append("\n模型比披露口径慢 {} 个月。原因：本模型摊还期按「余额 × 月还款率」几何衰减，"
                     "而静态池的实际摊还曲线前期更陡（等额本息贷款的本金占比随时间上升，"
                     "按原余额计的月还款额近似水平而非等比衰减）。该偏差使优先档加权平均期限"
                     "被系统性高估，票面定价相应偏保守。".format(max(gaps)))
            L.append("\n> 这是一个**没有修正的已知偏差**，故意留着：修正它需要静态池的逐月摊还表，"
                     "而披露文件只有汇总的月还款率。把它列出来，比调参数把数字凑上去更诚实——"
                     "也说明披露的预期到期日并非由月还款率单独推出。\n")

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
    L.append("\n> 次级档的实现收益率 {} 是**上界**：本模型把收入分账的全部剩余都归次级，"
             "未考虑「超额服务费」安排（服务商在各档获得约定收益后取走剩余）。"
             "真实次级收益取决于《服务合同》的分配约定，而该文件不公开——"
             "这一项无法从披露文件推断，是本报告最大的单点不确定性。\n".format(
                 pct(scenario_metrics[0][-1]["实现收益率"])))

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
    L.append("2. 循环期内假设购买率 100%、资产池余额严格维持不变，未刻画资产置换质量下降；"
             "循环期不分配本金，收入分账剩余先留存、摊还期用于补足利息缺口，期末释放给次级。")
    L.append("3. 真实交易在循环期按季付息，本模型按月计息并当月支付，会小幅高估优先档现金流的时间价值。")
    L.append("4. 摊还期按「余额 × 月还款率」几何衰减，比披露的优先档预期到期日慢若干个月（见第二节对账表），"
             "该偏差未修正，方向是低估摊还速度、高估久期。")
    L.append("5. **次级档实现收益率是上界，不是点估计。** 本模型把收入分账的全部剩余（超额利差）都归次级。"
             "真实交易通常约定「超额服务费」——服务商在所有档位获得约定收益后取走剩余——"
             "此时次级收益会显著低于此数。两种安排的分界就在《服务合同》/收费文件里，而该文件不公开，"
             "故本报告只能给出上界，并把「缺这份文件」列为核心数据缺口。")
    L.append("6. 核销后回收率与回收滞后为披露或假设值，未按催收渠道分层，未刻画回收的时间分布形状。")
    L.append("7. 票面为发行结果的实际值（`mode: fixed`），不再由模型反解——面值发行下反解票面是循环论证，"
             "详见 `docs/param-calibration.md`。")
    L.append("8. 未计入税费、中介费用、流动性溢价与外部增信。")
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

    report = build_report(case, pool, tranches, results, summaries, min_sub,
                          counterfactual)
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
