"""成本版本月度回退 — 唯一实现（收敛 query._pick_cost 与 fetcher.CostLookup 的回退口径）

★ 语义（与存量两份实现逐字一致）：
    当月有版本 → 取当月；
    否则回退历史最近版本（period < 当月的最大值）；
    从未维护 → None（缺失）。

同月多版本"取最新一条"由调用方在构建 {period: 值} 字典时保证
（ProductCostHistory 按 period desc, id desc 遍历、同 period 只取首条）。
"""
from typing import Optional


def pick_period(per: dict, month: str) -> Optional[str]:
    """月度回退：当月 → 历史最近 → None

    per: {period: (cost, freight)} 版本字典；month: 'YYYY-MM'
    """
    if month in per:
        return month
    prevs = [p for p in per if p < month]
    return max(prevs) if prevs else None