"""费用归集 — 唯一实现（B-02 超龄费无时间过滤 / B-05 月份口径不一致的根治点）

★ 月份过滤策略（组 10.3 已切换）：**等值匹配**（MONTH_EXACT_MATCH=true 默认）。
  历史过渡期曾用前缀匹配兼容带时间戳存量值（'2026-06-01 00:00:00'）；
  组 10 月份格式治理清洗完成、等值/前缀聚合一致性验证通过后切换。
  异常回退：.env 置 MONTH_EXACT_MATCH=false 恢复前缀匹配（无需改码）。

★ month_col 与模型的合法配对（运行时校验，配错直接报错而非静默漏过滤）：
    FactFeeStorage    → 'month_of_charge'
    FactFeeReturn     → 'month_of_charge'
    FactFeeAgedStorage → 'snapshot_date'

★ 两种过滤模式（互斥，都未传直接 raise —— B-02 防线：费用表无 biz_date，
  漏过滤会退化成全历史累计）：
    month_col 模式：月份列等值匹配（仓储/超龄/退货；年份用 'YYYY-%' 前缀）
    date_col  模式：业务日期列半开区间 [start, end)（移除 request_date / 入库 transaction_date）
    month_col 模式下 period=None 表示调用方明确选择全量归集（对齐广告侧
    ad_spend_all_by_key 的"无期间过滤"口径；仅限年份=全部的统计场景）。

★ 全部 SQL 聚合（GROUP BY），绝不把明细行拉回 Python 分组。
"""
from decimal import Decimal
from typing import Optional, Union

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.core.config import settings

# ---- 月份过滤策略（唯一切换点；由 config.MONTH_EXACT_MATCH 驱动）----
_MONTH_PREFIX_MATCH = not settings.MONTH_EXACT_MATCH


def switch_to_exact_match() -> None:
    """运行时切等值（保留接口兼容；持久切换请配置 MONTH_EXACT_MATCH=true）"""
    global _MONTH_PREFIX_MATCH
    _MONTH_PREFIX_MATCH = False


def month_filter_expr(model, month_col: str, period: str):
    """月份过滤条件 — period 为 'YYYY-MM'（月）或 'YYYY'（整年）"""
    col = getattr(model, month_col)
    if _MONTH_PREFIX_MATCH:
        return col.like(f"{period}%")
    if len(period) == 7:
        return col == period
    return col.like(f"{period}-%")


_VALID_MONTH_COLS = {'month_of_charge', 'snapshot_date'}


def _check(model, month_col: str) -> None:
    if month_col not in _VALID_MONTH_COLS:
        raise ValueError(
            f"month_col 非法: {month_col!r}，合法值: {sorted(_VALID_MONTH_COLS)}")
    if not hasattr(model, month_col):
        raise ValueError(f"模型 {model.__name__} 没有列 {month_col!r}，配对错误")


def _apply_scope(q, model, stores: Union[int, list, None]):
    """store 过滤：int=单店；list=多店（空列表→永假）；None=不限"""
    if stores is None:
        return q
    if isinstance(stores, (list, tuple)):
        return q.filter(model.store_id.in_(stores or [-1]))
    return q.filter(model.store_id == stores)


def _time_filter(model, month_col, period, date_col, start, end):
    """构建时间过滤条件 — month_col/period 与 date_col/[start,end) 二选一"""
    if month_col and date_col is not None:
        raise ValueError("month_col 与 date_col 互斥，只能二选一")
    if month_col:
        _check(model, month_col)
        if period:
            return month_filter_expr(model, month_col, period)
        return None   # period=None：调用方明确选择全量归集
    if date_col is not None:
        if start is None or end is None:
            raise ValueError("date_col 模式必须同时提供 start/end（半开区间）")
        return (date_col >= start) & (date_col < end)
    raise ValueError(
        f"{getattr(model, '__name__', model)} 费用归集必须指定 month_col 或 date_col"
        "（否则全历史累计，B-02 类缺陷）")


def fee_by_country(db: Session, model, amount_col: str, month_col: Optional[str] = None,
                   period: Optional[str] = None, stores: Union[int, list, None] = None,
                   country: Optional[str] = None,
                   date_col=None, start=None, end=None) -> dict[str, Decimal]:
    """费用按国家归集 → {country: 合计(原币)}

    month_col 模式: period 为 'YYYY-MM' 精确月 / 'YYYY' 整年 / None 全量；
    date_col  模式: [start, end) 半开区间（period 无效）。
    """
    tf = _time_filter(model, month_col, period, date_col, start, end)
    q = db.query(
        model.country.label('country'),
        sa_func.sum(getattr(model, amount_col)).label('amt'),
    )
    if tf is not None:
        q = q.filter(tf)
    q = _apply_scope(q, model, stores)
    if country:
        q = q.filter(model.country == country)
    out: dict[str, Decimal] = {}
    for c, amt in q.group_by('country').all():
        out[c or ''] = Decimal(str(amt)) if amt is not None else Decimal(0)
    return out


def fee_by_key(db: Session, model, amount_col: str, month_col: Optional[str] = None,
               period: Optional[str] = None, stores: Union[int, list, None] = None,
               country: Optional[str] = None,
               date_col=None, start=None, end=None) -> dict[tuple[str, str], Decimal]:
    """费用按 (asin, country) 归集 → {(asin, country): 合计(原币)}（过滤模式同 fee_by_country）"""
    tf = _time_filter(model, month_col, period, date_col, start, end)
    q = db.query(
        model.asin.label('asin'),
        model.country.label('country'),
        sa_func.sum(getattr(model, amount_col)).label('amt'),
    )
    if tf is not None:
        q = q.filter(tf)
    q = _apply_scope(q, model, stores)
    if country:
        q = q.filter(model.country == country)
    out: dict[tuple[str, str], Decimal] = {}
    for a, c, amt in q.group_by('asin', 'country').all():
        out[(a or '', c or '')] = Decimal(str(amt)) if amt is not None else Decimal(0)
    return out