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


def run_scenario(pool, tranches, structure, sc):
    rows = build_schedule(
        pool,
        co_mult=sc.get("co_mult", 1.0),
        mrr_mult=sc.get("mrr_mult", 1.0),
        recovery_mult=sc.get("recovery_mult", 1.0),
        yield_mult=sc.get("yield_mult", 1.0),
    )
    result = run_waterfall(
        rows, pool.balance, tranches,
        pay_mode=structure.get("pay_mode", "sequential"),
        trigger_cdr=structure.get("trigger_cdr", 0.06),
    )
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


def build_report(case, pool, tranches, results, summaries, min_sub):
    scenario_metrics = [r[2] for r in results]
    funded = sum(t.share for t in tranches if t.mode == "solve")
    weighted_coupon = sum(t.share * t.coupon for t in tranches if t.mode == "solve") / funded if funded else 0.0
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

    L.append("## 二、情景设置与资产池结果\n")
    rows = []
    for s, p in zip(case["scenarios"], summaries):
        rows.append([s["label"], s["note"], pct(p["累计核销率"]), pct(p["净损失率"]),
                     pct(p["本金回收率"]), f"{p['资产池加权平均期限(年)']:.2f}",
                     f"{p['池利息收入(亿元)']:.2f}", p["存续月数"]])
    L.append(md_table(["情景", "参数设定", "累计核销率", "净损失率", "本金回收率",
                       "池加权期限(年)", "池利息收入(亿元)", "存续月数"], rows))

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

    L.append("\n## 七、结构触发\n")
    L.append("- 分配方式：`{}`；触发阈值：累计核销率 > {}".format(
        case["structure"].get("pay_mode"), pct(case["structure"].get("trigger_cdr", 0.06), 1)))
    for s, r in zip(case["scenarios"], results):
        m = r[1].accel_month
        L.append("  - {}：{}".format(s["label"], f"第 {m} 个月触发加速清偿" if m else "未触发"))

    L.append("\n## 八、假设与局限\n")
    L.append("1. 循环购买期与摊还期长度披露文件中未直接给出，按优先档预期到期日倒推假设（见第一节来源列）。")
    L.append("2. 循环期内假设购买率 100%、资产池余额严格维持不变，未刻画资产置换质量下降。")
    L.append("3. 真实交易在循环期按季付息，本模型按月计息并当月支付，会小幅高估优先档现金流的时间价值。")
    L.append("4. 核销后回收率与回收滞后为假设值，未按催收渠道分层。")
    L.append("5. 票面为面值发行下的反解值；若已知实际票面，可替换 `coupon` 并把 `mode` 改为 `fixed`。")
    L.append("6. 未计入税费、中介费用、流动性溢价与外部增信。")
    return "\n".join(L)


def run_one(case_path, out_dir, quiet=False):
    with open(case_path, encoding="utf-8") as f:
        case = json.load(f)
    pool = Pool.from_dict(case["pool"])
    tranches = [Tranche.from_dict(t) for t in case["tranches"]]
    structure = case.get("structure", {})
    scenarios = case["scenarios"]

    rows0 = build_schedule(pool)
    solve_coupons(rows0, pool.balance, tranches, structure.get("pay_mode", "sequential"),
                  structure.get("trigger_cdr", 0.06))

    results, summaries = [], []
    for s in scenarios:
        r = run_scenario(pool, tranches, structure, s)
        ok, gap = check_conservation(r[0], r[1])
        results.append(r)
        summaries.append(summarize_pool(r[0], pool))
        if not quiet:
            print(f"[守恒] {s['label']}: {'通过' if ok else '失败'}（残差 {gap:.2f}）")

    cid = case.get("case_id", "case")
    write_schedule_csv(os.path.join(out_dir, f"{cid}_schedule.csv"), results[0][0])

    min_sub, _ = min_subordination(results[-1][0], pool.balance, tranches,
                                   structure.get("pay_mode", "sequential"),
                                   structure.get("trigger_cdr", 0.06))

    report = build_report(case, pool, tranches, results, summaries, min_sub)
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
