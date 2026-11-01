"""abs — 消费金融 / 绿色信贷 ABS 分层定价工具。

模块划分（对应说明文档中的四层结构）：
    pool      资产池参数与摊还假设
    cashflow  逐月现金流引擎
    waterfall 分层瀑布与触发机制
    pricing   票面利率反解、收益率指标与压力情景
"""

from .pool import Pool
from .cashflow import build_schedule
from .waterfall import Tranche, run_waterfall
from .pricing import solve_coupons, tranche_metrics, irr

__all__ = [
    "Pool",
    "build_schedule",
    "Tranche",
    "run_waterfall",
    "solve_coupons",
    "tranche_metrics",
    "irr",
]
