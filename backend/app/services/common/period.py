"""期间解析 — 唯一实现（迁移自 dashboard._resolve_period 与 query 年月兼容解析）

语义（与存量实现完全一致，仅收敛入口）：
    优先级：period_month > year+month > date_from+date_to > year > 当前月
    · period_month='YYYY-MM'      → month 模式（start/end 为该月半开区间）
    · year+month                  → 等效 period_month，month 模式
    · date_from+date_to           → range 模式（自定义区间，period_month=None）
    · year（month 省略/0）        → year 模式（整年区间，period_month=None）
    · 全部为空                    → 当前自然月（month 模式）

纯函数：无 DB 访问；非法格式抛 ValueError（API 层负责转 400）。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class PeriodSpec:
    """解析后的查询期间"""
    start: datetime                 # 半开区间起点（含）
    end: datetime                   # 半开区间终点（不含）
    period_month: Optional[str]     # 归属月 YYYY-MM；year/range 模式为 None
    mode: str                       # 'month' | 'year' | 'range'


class PeriodFormatError(ValueError):
    """期间参数格式非法（API 层应转为 400）"""


def month_bounds(period_month: str) -> tuple[datetime, datetime]:
    """YYYY-MM → [月初, 次月初) 的半开区间（全系统唯一定义）"""
    _validate_period_month(period_month)
    y, m = int(period_month[:4]), int(period_month[5:7])
    return datetime(y, m, 1), datetime(y + (m == 12), (m % 12) + 1, 1)


def _validate_period_month(period_month: str) -> None:
    if not isinstance(period_month, str) or len(period_month) != 7 \
            or period_month[4] != '-' or not period_month[:4].isdigit() \
            or not period_month[5:7].isdigit() or not (1 <= int(period_month[5:7]) <= 12):
        raise PeriodFormatError(f"period_month 格式应为 YYYY-MM，收到: {period_month!r}")


def _parse_date(v, name: str) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(v.strip(), fmt)
            except ValueError:
                continue
    raise PeriodFormatError(f"{name} 格式应为 YYYY-MM-DD，收到: {v!r}")


def resolve_period(period_month: Optional[str] = None,
                   year: Optional[int] = None,
                   month: Optional[int] = None,
                   date_from=None,
                   date_to=None) -> PeriodSpec:
    """统一解析查询期间 — 兼容新旧参数风格（全系统唯一实现）

    返回 PeriodSpec；三种模式见模块 docstring。
    """
    if period_month:
        s, e = month_bounds(period_month)
        return PeriodSpec(s, e, period_month, 'month')
    if year and month:
        pm = f"{int(year)}-{int(month):02d}"
        s, e = month_bounds(pm)
        return PeriodSpec(s, e, pm, 'month')
    if date_from and date_to:
        return PeriodSpec(_parse_date(date_from, 'date_from'),
                          _parse_date(date_to, 'date_to'), None, 'range')
    if year:
        return PeriodSpec(datetime(int(year), 1, 1), datetime(int(year) + 1, 1, 1), None, 'year')
    pm = datetime.now().strftime('%Y-%m')
    s, e = month_bounds(pm)
    return PeriodSpec(s, e, pm, 'month')