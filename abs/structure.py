"""结构设计：反解最小次级厚度。

行业里分层厚度通常由两条路径确定：一是评级机构的目标评级映射表，二是承销商在压力情景下
反解"夹层不违约"所需的最薄次级档。后者是投行定价环节真正会做的事，本模块实现它。

问题形式：
    给定资产池、压力情景、优先A/夹层的目标收益率，
    求最小的次级档占比 s，使得在压力情景下夹层与优先A 的本金损失为零。
"""

from dataclasses import replace

from .waterfall import run_waterfall


def _build(template_tranches, sub_share, mezz_share):
    """按给定次级厚度重建分档；优先级占比 = 1 - 次级 - 夹层。"""
    senior = template_tranches[0]
    mezz = template_tranches[1]
    sub = template_tranches[-1]
    return [
        replace(senior, share=1.0 - sub_share - mezz_share),
        replace(mezz, share=mezz_share),
        replace(sub, share=sub_share),
    ]


def _safe(result, names):
    """优先A 与夹层均无本金损失即视为安全。"""
    return all(result.unpaid_principal[n] <= 1.0 for n in names)


def min_subordination(rows, pool_balance, template_tranches, pay_mode="sequential",
                      trigger_cdr=0.06, mezz_share=None, lo=0.02, hi=0.40, tol=0.0025):
    """二分求最小次级厚度。返回 (最小厚度, 搜索轨迹)。"""
    mezz_share = template_tranches[1].share if mezz_share is None else mezz_share
    names = [template_tranches[0].name, template_tranches[1].name]
    trace = []

    def safe_at(s):
        trs = _build(template_tranches, s, mezz_share)
        res = run_waterfall(rows, pool_balance, trs, pay_mode, trigger_cdr)
        ok = _safe(res, names)
        trace.append((s, ok))
        return ok

    if not safe_at(hi):
        return None, trace
    if safe_at(lo):
        return lo, trace

    while hi - lo > tol:
        mid = (lo + hi) / 2.0
        if safe_at(mid):
            hi = mid
        else:
            lo = mid
    return hi, trace


def run_structure(rows, pool_balance, template_tranches, pay_mode, trigger_cdr):
    s, _ = min_subordination(rows, pool_balance, template_tranches, pay_mode, trigger_cdr)
    return s
