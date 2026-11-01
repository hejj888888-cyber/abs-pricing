"""结构设计：反解最小次级厚度。

行业里分层厚度由两条路径确定：一是评级机构的目标评级映射表，二是承销商在压力情景下
反解"优先档不违约"所需的最薄次级档。后者是投行定价环节真正会做的事，本模块实现它。

问题形式：
    给定资产池、压力情景、优先档的目标收益率，
    求最小的次级档占比 s，使其余各档在压力情景下本金损失为零。
"""

from dataclasses import replace

from .waterfall import run_waterfall


def _build(template_tranches, sub_share):
    """按给定次级厚度重建分档；中间各档占比不变，优先级占比吸收差额。"""
    senior = template_tranches[0]
    sub = template_tranches[-1]
    middle = list(template_tranches[1:-1])
    mid_sum = sum(t.share for t in middle)
    senior_share = 1.0 - sub_share - mid_sum
    return [replace(senior, share=senior_share)] + [replace(t) for t in middle] + [replace(sub, share=sub_share)]


def min_subordination(rows, pool_balance, template_tranches, pay_mode="sequential",
                      trigger_cdr=0.06, lo=0.005, hi=0.45, tol=0.0025):
    """二分求最小次级厚度。返回 (最小厚度, 搜索轨迹)。"""
    senior_names = [t.name for t in template_tranches[:-1]]
    trace = []

    def safe_at(s):
        if 1.0 - s - sum(t.share for t in template_tranches[1:-1]) <= 0:
            return False
        trs = _build(template_tranches, s)
        res = run_waterfall(rows, pool_balance, trs, pay_mode, trigger_cdr)
        ok = all(res.unpaid_principal[n] <= 1.0 for n in senior_names)
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
