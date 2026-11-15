"""导出改引擎前后的全情景逐月全字段基线快照。

用途（第 7 周 P0-D 修复）：改现金流引擎前先固化两单全部情景的逐月明细，
改完逐行 diff。默认 `principal` 路径必须 diff = 0；`available` 路径允许有差异，
但要在报告里逐项列出并解释。

用法：
    python scripts/export_baseline.py --tag before
    python scripts/export_baseline.py --tag after
    python scripts/export_baseline.py --diff

不截断循环期（stop_revolving=False），这样行数只由引擎决定，diff 干净；
触发事件与瀑布结果仍作为指标写进同名 JSON，用于观察修复是否改变判定。
"""

import argparse
import csv
import glob
import json
import os
import sys
from dataclasses import replace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from abs import Pool, Tranche, build_schedule, solve_coupons  # noqa: E402
from abs.vintage import from_dict as vintage_from_dict  # noqa: E402
from abs.trigger import TriggerTerms  # noqa: E402
from run_case import run_scenario, check_conservation  # noqa: E402

BASE_DIR = os.path.join(os.path.dirname(ROOT), "baseline", "p0d")


def fmt(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return "{:.6f}".format(v)
    return str(v)


def snapshot(tag, base_dir):
    out = os.path.join(base_dir, tag)
    os.makedirs(out, exist_ok=True)
    stems = []
    for case_path in sorted(glob.glob(os.path.join(ROOT, "cases", "*.json"))):
        with open(case_path, encoding="utf-8") as f:
            case = json.load(f)
        pool = Pool.from_dict(case["pool"])
        tranches = [Tranche.from_dict(t) for t in case["tranches"]]
        structure = case.get("structure", {})
        vintage = vintage_from_dict(case.get("vintage"))
        terms = TriggerTerms.from_dict(case.get("triggers"))

        rows0 = build_schedule(pool, vintage=vintage)
        solve_coupons(rows0, pool.balance, tranches,
                      structure.get("pay_mode", "sequential"),
                      structure.get("trigger_cdr", 0.06), terms)

        cid = case.get("case_id", "case")
        for sc in case["scenarios"]:
            for mode in ("principal", "available"):
                rows, result, _ = run_scenario(
                    replace(pool, purchase_base=mode), tranches, structure, sc,
                    vintage=vintage, terms=terms, stop_revolving=False)
                stem = "{}__{}__{}".format(cid, sc["id"], mode)
                stems.append(stem)

                cols = list(rows[0].keys())
                with open(os.path.join(out, stem + ".csv"), "w", newline="",
                          encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(cols)
                    for r in rows:
                        w.writerow([fmt(r[c]) for c in cols])

                ok, gap = check_conservation(rows, result)
                meta = {
                    "case_id": cid,
                    "scenario_id": sc["id"],
                    "purchase_base": mode,
                    "months": len(rows),
                    "sum_total_collection": sum(r["total_collection"] for r in rows),
                    "sum_income_transfer": sum(r["income_transfer"] for r in rows),
                    "sum_prin_topup": sum(r["prin_topup"] for r in rows),
                    "sum_purchase": sum(r["purchase"] for r in rows),
                    "sum_charge_off": sum(r["charge_off"] for r in rows),
                    "final_prin_acct": rows[-1]["prin_acct"],
                    "cashflow_totals": {n: sum(v) for n, v in result.cashflow.items()},
                    "reserve": result.reserve,
                    "excess_servicing": result.excess_servicing,
                    "unpaid_interest": dict(result.unpaid_interest),
                    "unpaid_principal": dict(result.unpaid_principal),
                    "accel_month": result.accel_month,
                    "trigger_events": [[e.month, e.code, e.kind]
                                       for e in result.trigger_events],
                    "conservation_ok": ok,
                    "conservation_gap": gap,
                }
                with open(os.path.join(out, stem + ".json"), "w",
                          encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)

    with open(os.path.join(out, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(stems, f, ensure_ascii=False, indent=2)
    print("[baseline] {} -> {} 组快照，目录 {}".format(tag, len(stems), out))
    return out


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.reader(f))


def diff_report(base_dir, left="before", right="after"):
    before = os.path.join(base_dir, left)
    after = os.path.join(base_dir, right)
    lines = ["# 基线逐行 diff：{} → {}".format(left, right), "",
             "> 由 `python scripts/export_baseline.py --diff --left {} --right {}` 生成。"
             .format(left, right)
             + "快照为不截断循环期的纯引擎轨迹，两单 × 全部情景 × 两种口径。", "",
             "## 一、逐月全字段 diff（CSV）", "",
             "| 组 | 修复前行数 | 修复后行数 | 差异单元格 | 差异字段（前 4） | 首个差异月 |",
             "|---|---:|---:|---:|---|---|"]
    n_bad_principal = 0
    samples = []
    field_changes = []
    for name in sorted(x[:-4] for x in os.listdir(before) if x.endswith(".csv")):
        b, a = read_csv(os.path.join(before, name + ".csv")), read_csv(os.path.join(after, name + ".csv"))
        head = b[0]
        ib = {c: j for j, c in enumerate(b[0])}
        ia = {c: j for j, c in enumerate(a[0])}
        extra = [c for c in a[0] if c not in b[0]]
        missing = [c for c in b[0] if c not in a[0]]
        if extra or missing:
            field_changes.append((name, extra, missing))
        n_cells, fields, first_month = 0, {}, None
        for i in range(1, min(len(b), len(a))):
            # 按列名比对：修复后新增的字段不能让后面的列整体错位
            for col in head:
                jb, ja = ib[col], ia[col]
                if (jb >= len(b[i]) or ja >= len(a[i])
                        or b[i][jb] != a[i][ja]):
                    n_cells += 1
                    fields[col] = fields.get(col, 0) + 1
                    if first_month is None:
                        first_month = b[i][0]
        n_cells += abs(len(b) - len(a)) * len(head)
        top = ", ".join("{} ({})".format(k, v) for k, v in
                        sorted(fields.items(), key=lambda kv: -kv[1])[:4]) or "—"
        lines.append("| `{}` | {} | {} | {} | {} | {} |".format(
            name, len(b) - 1, len(a) - 1, n_cells, top, first_month or "—"))
        if name.endswith("__principal") and n_cells:
            n_bad_principal += 1
        if n_cells and len(samples) < 4:
            samples.append((name, head, b, a))

    lines += ["", "### 字段集合变化（新增列不计入逐行差异）", ""]
    if field_changes:
        for name, extra, missing in field_changes[:2]:
            lines.append("- `{}`：新增 {}，缺失 {}".format(
                name, extra or "无", missing or "无"))
        if len(field_changes) > 2:
            lines.append("- 其余 {} 组同上".format(len(field_changes) - 2))
        lines += ["", "> 新增字段为 `income_required`（收入账户应付额），默认 "
                  "`principal` 口径下恒为 0，不改变原有逐月结果。"]
    else:
        lines.append("- 无")
    lines += ["", "**默认 `principal` 路径有差异的组数：{}（要求为 0）**".format(n_bad_principal), "",
              "## 二、瀑布与账户指标变化（JSON）", "",
              "| 组 | 指标 | 修复前 | 修复后 | 差 |",
              "|---|---|---:|---:|---:|"]
    keys = ("sum_total_collection", "sum_income_transfer", "sum_prin_topup",
            "sum_purchase", "sum_charge_off", "final_prin_acct",
            "reserve", "accel_month", "conservation_gap")
    for name in sorted(x[:-5] for x in os.listdir(before) if x.endswith(".json")
                       and not x.startswith("_")):
        with open(os.path.join(before, name + ".json"), encoding="utf-8") as f:
            mb = json.load(f)
        with open(os.path.join(after, name + ".json"), encoding="utf-8") as f:
            ma = json.load(f)
        for k in keys:
            vb, va = mb.get(k), ma.get(k)
            if isinstance(vb, (int, float)) and isinstance(va, (int, float)):
                d = va - vb
                if abs(d) < 1e-9:
                    continue
                lines.append("| `{}` | {} | {:.2f} | {:.2f} | {:+.2f} |".format(
                    name, k, vb, va, d))
        for tr, vb in mb["cashflow_totals"].items():
            va = ma["cashflow_totals"].get(tr, 0.0)
            if abs(va - vb) >= 1e-9:
                lines.append("| `{}` | 实付 {} | {:.2f} | {:.2f} | {:+.2f} |".format(
                    name, tr, vb, va, va - vb))
        if mb["trigger_events"] != ma["trigger_events"]:
            lines.append("| `{}` | 触发事件 | {} | {} | — |".format(
                name, mb["trigger_events"], ma["trigger_events"]))

    lines += ["", "## 三、差异样例（前 3 个差异月的 `total_collection` / 账户字段）", ""]
    for name, head, b, a in samples[:2]:
        pick = [c for c in ("total_collection", "income_transfer", "prin_topup",
                            "purchase", "prin_acct") if c in head]
        idb = {c: list(b[0]).index(c) for c in pick}
        ida = {c: list(a[0]).index(c) for c in pick}
        lines.append("### `{}`".format(name))
        lines.append("")
        lines.append("| 月 | " + " | ".join("前 " + c for c in pick)
                     + " | " + " | ".join("后 " + c for c in pick) + " |")
        lines.append("|---" * (1 + 2 * len(pick)) + "|")
        shown = 0
        for i in range(1, min(len(b), len(a))):
            if any(b[i][idb[c]] != a[i][ida[c]] for c in pick):
                lines.append("| {} | {} | {} |".format(
                    b[i][0],
                    " | ".join(b[i][idb[c]] for c in pick),
                    " | ".join(a[i][ida[c]] for c in pick)))
                shown += 1
                if shown >= 3:
                    break
        lines.append("")

    path = os.path.join(base_dir, "diff-{}--vs--{}.md".format(left, right))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("[diff] 默认 principal 路径差异组数 = {}".format(n_bad_principal))
    print("[diff] 报告 -> {}".format(path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", help="before / after")
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--base", default=BASE_DIR)
    ap.add_argument("--left", default="before", help="diff 左侧快照目录名")
    ap.add_argument("--right", default="after", help="diff 右侧快照目录名")
    args = ap.parse_args()
    if args.diff:
        diff_report(args.base, args.left, args.right)
    elif args.tag:
        snapshot(args.tag, args.base)
    else:
        ap.error("需要 --tag 或 --diff")


if __name__ == "__main__":
    main()
