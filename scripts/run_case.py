"""跑一个案例：反解票面 → 三个情景 → 现金流守恒校验 → 输出 CSV 与 Markdown 报告。

用法：
    python scripts/run_case.py
    python scripts/run_case.py --case cases/consumer_abs_base.json
"""

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abs import Pool, build_schedule, Tranche, run_waterfall, solve_coupons, tranche_metrics
from abs.cashflow import summarize_pool
from abs.structure import min_subordination


def pct(x, nd=2):
    return "—" if x is None else f"{x * 100:.{nd}f}%"


def run_scenario(pool, tranches, structure, mults):
    rows = build_schedule(
        pool,
        cdr_mult=mults.get("cdr_mult", 1.0),
        cpr_mult=mults.get("cpr_mult", 1.0),
        recovery_mult=mults.get("recovery_mult", 1.0),
    )
    result = run_waterfall(
        rows,
        pool.balance,
        tranches,
        pay_mode=structure.get("pay_mode", "sequential"),
        trigger_cdr=structure.get("trigger_cdr", 0.06),
    )
    metrics = [tranche_metrics(result, t) for t in tranches]
    return rows, result, metrics


def check_conservation(rows, result):
    """现金流守恒：池收回总额 = 服务费 + 各档分得现金流。"""
    inflow = sum(r["total_collection"] for r in rows)
    fee = sum(r["servicing_fee"] for r in rows)
    paid = sum(sum(cfs) for cfs in result.cashflow.values())
    gap = inflow - fee - paid
    return abs(gap) < 1e-4 * max(1.0, inflow), gap


def write_schedule_csv(path, rows):
    cols = ["month", "bal_begin", "interest", "sched_prin", "prepay", "default",
            "recovery", "servicing_fee", "total_collection", "bal_end", "cum_default_rate"]
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


def thickness_analysis(tranches, scenario_metrics):
    """每一档的厚度、静态吸收区间，以及三个情景下的实际本金损失率。

    损失自下而上吸收：次级先吃 0 ~ 次级厚度，夹层吃下一段，优先A 吃最后一段。
    """
    ranges = {}
    cum = 0.0
    for t in reversed(tranches):
        lo, cum = cum, cum + t.share
        ranges[t.name] = (lo, cum)

    rows = []
    for i, t in enumerate(tranches):
        lo, hi = ranges[t.name]
        losses = [pct(m[i]["本金损失率"]) for m in scenario_metrics]
        rows.append([t.name, pct(t.share, 1), f"{pct(lo, 1)} ~ {pct(hi, 1)}"] + losses)
    return rows


def build_report(case, pool, tranches, scenario_results, pool_summaries, min_sub):
    base_rows, base_res, base_m = scenario_results[0]
    scenario_metrics = [r[2] for r in scenario_results]
    loss_pairs = [(s["label"], p["净损失率"]) for s, p in zip(case["scenarios"], pool_summaries)]

    # 超额利差：资产端加权利率 - 证券端加权票面 - 服务费
    funded = sum(t.share for t in tranches if t.mode == "solve")
    weighted_coupon = sum(t.share * t.coupon for t in tranches if t.mode == "solve") / funded if funded else 0.0
    excess = pool.wac - weighted_coupon - pool.servicing_fee_rate

    sub = tranches[-1]
    sub_loss_extreme = scenario_metrics[-1][-1]["本金损失率"]
    sev_loss = pool_summaries[-1]["净损失率"]
    absorbed_by_excess = sev_loss - sub_loss_extreme * sub.share

    pierce = sev_loss - sub.share
    if pierce > 0:
        static_note = "静态看已超过次级厚度 {} 个百分点".format(f"{pierce * 100:.2f}")
    else:
        static_note = "未穿透次级厚度"

    excess_note = (
        "**超额利差测算**：资产端加权利率 {} − 证券端加权票面 {} − 服务费 {} ≈ **{}**（年化，占资产池）。\n\n"
        "**读法**：极端情景下资产池净损失 {}，{}（次级厚度 {}）。若只按厚度判断，损失的这一部分应当由夹层承担；"
        "但实际跑下来次级损失为 {}（约合资产池 {}），差额 {} 被超额利差吸收了，所以夹层分文未损。\n\n"
        "**结论：厚度决定的是下限，超额利差决定的是实际结果。**只看厚度会低估次级的承受力，"
        "只看实际结果又会高估整个结构的稳健性——两者必须并列看。"
    ).format(
        pct(pool.wac), pct(weighted_coupon), pct(pool.servicing_fee_rate), pct(excess),
        pct(sev_loss), static_note, pct(sub.share, 1),
        pct(sub_loss_extreme), pct(sub_loss_extreme * sub.share), pct(max(absorbed_by_excess, 0)),
    )

    if min_sub:
        gap = sub.share - min_sub
        if gap > 0.005:
            thickness_conclusion = (
                "结论：在极端情景（违约率×3.5、回收率×0.4）下，次级档做到 **{}** 即可让夹层与优先A 零损失，"
                "当前设计 {} 比压力测试要求高出 **{} 个百分点**。这多出来的厚度并不产生评级收益，"
                "只降低发行效率——这正是分层定价里最容易被忽略的成本项。\n\n"
                "**但这里有个必须讲清楚的区别**：反解出来的是「不违约下限」，市场上真实的次级厚度通常在 10%–15%，"
                "差额来自评级机构的损失覆盖倍数要求、循环购买期、服务机构替换与混同风险等本模型尚未刻画的因素。"
                "**下限与实际厚度之间的这段距离，就是我方与行业第一名之间的主要差距之一**（详见 `docs/gap-analysis.md`）。"
            ).format(pct(min_sub, 2), pct(sub.share, 1), f"{gap * 100:.1f}")
        else:
            thickness_conclusion = (
                "结论：当前次级厚度 {} 与压力测试要求的 {} 基本相当，结构已无压缩空间；"
                "若要进一步提高优先A 占比，只能降低目标收益率或引入外部增信。"
            ).format(pct(sub.share, 1), pct(min_sub, 2))
    else:
        thickness_conclusion = "结论：在搜索上限内未能找到使夹层零损失的次级厚度，说明该压力情景下结构需要外部增信。"

    lines = []
    lines.append("# {} · 定价结果\n".format(case["case_name"]))
    status = "合成基准参数（尚未替换真实产品）" if case.get("data_status") == "synthetic" else case.get("data_status", "")
    lines.append("> 数据状态：**{}**  \n> {}\n".format(status, case.get("data_note", "")))

    lines.append("## 一、资产池假设\n")
    lines.append(md_table(
        ["参数", "取值", "说明"],
        [["初始本金余额", f"{pool.balance / 1e8:.2f} 亿元", "基础资产未偿本金"],
         ["加权平均利率", pct(pool.wac), "资产端收益率"],
         ["剩余期限", f"{pool.term_months} 个月", ""],
         ["条件提前还款率 CPR", pct(pool.cpr), "年化，折算为月度 SMM"],
         ["条件违约率 CDR", pct(pool.cdr), "年化，折算为月度 MDR"],
         ["回收率", pct(pool.recovery_rate), "违约后本息回收比例"],
         ["回收滞后", f"{pool.recovery_lag_months} 个月", "违约到实际收到回收款"],
         ["服务费率", pct(pool.servicing_fee_rate), "按月初余额计提，最先受偿"]]))

    lines.append("\n## 二、情景设置与资产池结果\n")
    rows = []
    for s, p in zip(case["scenarios"], pool_summaries):
        rows.append([s["label"], s["note"], pct(p["累计违约率"]), pct(p["净损失率"]),
                     pct(p["本金回收率"]), f"{p['资产池加权平均期限(年)']:.2f}", p["存续月数"]])
    lines.append(md_table(["情景", "参数设定", "累计违约率", "净损失率", "本金回收率", "池加权期限(年)", "存续月数"], rows))

    lines.append("\n## 三、分层结果（基准情景）\n")
    headers = ["档位", "评级", "发行规模(亿元)", "占比", "票面利率", "目标收益率", "实现收益率",
               "利差", "加权平均期限(年)", "本金损失率", "未付利息(万元)"]
    body = [[m["档位"], m["评级"], f"{m['发行规模(亿元)']:.2f}", pct(m["占比"], 1),
             pct(m["票面利率"]), pct(m["目标收益率"]) if m["目标收益率"] else "—",
             pct(m["实现收益率"]), pct(m["利差"]) if m["利差"] is not None else "—",
             f"{m['加权平均期限(年)']:.2f}", pct(m["本金损失率"]), f"{m['未付利息(万元)']:.1f}"]
            for m in base_m]
    lines.append(md_table(headers, body))

    lines.append("\n## 四、压力测试：各档本金损失率\n")
    rows = []
    for i, t in enumerate(tranches):
        rows.append([t.name] + [pct(scenario_results[j][2][i]["本金损失率"]) for j in range(len(scenario_results))])
    lines.append(md_table(["档位"] + [s["label"] for s in case["scenarios"]], rows))

    lines.append("\n## 五、分层厚度是否足够\n")
    lines.append("判断逻辑有两层，缺一层就会误判：")
    lines.append("1. **静态保护**：各档厚度决定它能挡住多少净损失（次级先吸收，再夹层，最后优先A）。")
    lines.append("2. **动态保护**：资产端利率高于证券端票面，形成的超额利差每月先进次级，这部分缓冲不计入厚度但确实在吃损失。")
    lines.append("")
    lines.append(md_table(["档位", "厚度", "静态吸收的净损失区间", "基准损失", "压力损失", "极端损失"],
                          thickness_analysis(tranches, scenario_metrics)))
    lines.append("")
    lines.append(excess_note)

    lines.append("\n## 六、分层厚度反解\n")
    lines.append("把上面的判断反过来问：**在极端情景下，次级档最薄可以做到多少，夹层仍然不违约？**")
    lines.append("")
    lines.append(md_table(["项目", "取值"],
                          [["当前次级厚度", pct(tranches[-1].share, 1)],
                           ["极端情景反解的最小次级厚度", pct(min_sub, 2) if min_sub else "超出搜索上限"],
                           ["厚度富裕度", pct(tranches[-1].share - min_sub, 2) if min_sub else "—"],
                           ["含义", "当前分层比压力测试要求更保守" if min_sub and tranches[-1].share > min_sub + 0.005
                            else "当前分层接近压力测试的临界水平"]]))
    lines.append("")
    lines.append(thickness_conclusion)

    lines.append("\n## 七、结构触发\n")
    lines.append("- 分配方式：`{}`，顺序偿付下本金先清偿优先A".format(case["structure"].get("pay_mode")))
    lines.append("- 加速清偿触发阈值：累计违约率 > {}".format(pct(case["structure"].get("trigger_cdr", 0.06), 1)))
    for s, r in zip(case["scenarios"], scenario_results):
        m = r[1].accel_month
        lines.append("  - {}：{}".format(s["label"], f"第 {m} 个月触发加速清偿" if m else "未触发"))

    lines.append("\n## 八、假设与局限\n")
    for i, t in enumerate([
        "计划摊还额按初始余额的等额本息月供固定，早偿/违约导致余额加速下降时不重算月供，属池层面近似。",
        "利息按月初余额计，当月违约的资产当月仍计息。",
        "回收按固定回收率与固定滞后处理，未刻画回收的时间分布。",
        "各档票面为反解结果（按面值发行、达到目标收益率），未做询价与簿记环节的价差调整。",
        "未计入税费、中介费用与流动性溢价；次级档按 residual 处理，不设票面，收益全部来自超额利差与剩余本金。",
    ], 1):
        lines.append(f"{i}. {t}")
    return "\n".join(lines)


def _conclusion(tranches, loss_pairs):
    subs = []
    for label, loss in loss_pairs:
        absorbed = "次级"
        cum = 0.0
        for t in tranches:
            cum += t.share
            if loss <= cum:
                absorbed = t.name
                break
        else:
            absorbed = "全部档位（已穿透优先A）"
        subs.append(f"{label}净损失 {pct(loss)} 由{absorbed}吸收")
    return "；".join(subs) + "。"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="cases/consumer_abs_base.json")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    case_path = args.case if os.path.isabs(args.case) else os.path.join(root, args.case)
    out_dir = args.out if os.path.isabs(args.out) else os.path.join(root, args.out)
    os.makedirs(out_dir, exist_ok=True)

    with open(case_path, encoding="utf-8") as f:
        case = json.load(f)

    pool = Pool.from_dict(case["pool"])
    tranches = [Tranche.from_dict(t) for t in case["tranches"]]
    structure = case.get("structure", {})
    scenarios = case["scenarios"]

    # 票面利率在基准情景下反解，之后各情景固定不变（压力测试的常规做法）
    rows0 = build_schedule(pool)
    solve_coupons(rows0, pool.balance, tranches, structure.get("pay_mode", "sequential"),
                  structure.get("trigger_cdr", 0.06))
    print("反解票面：" + "，".join(f"{t.name} {t.coupon*100:.3f}%" for t in tranches if t.mode == "solve"))

    results, summaries = [], []
    for s in scenarios:
        r = run_scenario(pool, tranches, structure, s)
        ok, gap = check_conservation(r[0], r[1])
        print(f"[现金流守恒] {s['label']}: {'通过' if ok else '不通过'}（残差 {gap:.2f} 元）")
        results.append(r)
        summaries.append(summarize_pool(r[0], pool))

    cid = case.get("case_id", "case")
    write_schedule_csv(os.path.join(out_dir, f"{cid}_schedule.csv"), results[0][0])

    # 用极端情景反解最小次级厚度
    min_sub, _ = min_subordination(
        results[-1][0], pool.balance, tranches,
        structure.get("pay_mode", "sequential"), structure.get("trigger_cdr", 0.06))
    print(f"[分层厚度反解] 极端情景下最小次级厚度："
          f"{'%.2f%%' % (min_sub * 100) if min_sub else '超出上限'}（当前 {tranches[-1].share*100:.1f}%）")

    report = build_report(case, pool, tranches, results, summaries, min_sub)
    md_path = os.path.join(out_dir, f"{cid}_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)

    for s, r, p in zip(scenarios, results, summaries):
        print(f"\n=== {s['label']}　净损失 {pct(p['净损失率'])}　累计违约 {pct(p['累计违约率'])} ===")
        for m in r[2]:
            print(f"  {m['档位']:<4} 规模 {m['发行规模(亿元)']:.2f}亿  票面 {pct(m['票面利率'])}  "
                  f"实现 {pct(m['实现收益率'])}  WAL {m['加权平均期限(年)']:.2f}y  本金损失 {pct(m['本金损失率'])}")
    print("\n报告：" + md_path)


if __name__ == "__main__":
    main()
