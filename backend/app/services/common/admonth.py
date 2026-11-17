"""广告归月 — 唯一实现（SQL 化三级归月，替代 query.py:97-130 的 Python 全表归月）

三级归月语义（与存量 Python 实现逐行一致，绝不按导入时间归月）：
    ① fact_ad_daily.biz_date       → DATE_FORMAT(biz_date, '%Y-%m')
    ② fact_ad_daily.period_month   → 导入时写入的报表归属月
    ③ import_batch.period_month    → 同批次归属月（LEFT JOIN 派生）
    ④ import_batch.imported_at     → 批次导入时间所在月（最后兜底）

SQL 表达式：COALESCE(①, ②, ③, DATE_FORMAT(④)) —— 与 Python 版 if/elif 链等价
（biz_date 非 NULL 时 ① 非 NULL；全 NULL 的行不命中任何月份过滤，与 Python 版跳过一致）。
"""
from decimal import Decimal
from typing import Optional, Union

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session, aliased

from app.models import FactAdDaily, ImportBatch


def ad_month_expr(batch_alias=None):
    """广告行归属月份的 SQL 表达式

    batch_alias: 调用方已 JOIN 的 import_batch 别名（aliased(ImportBatch)）；
                 缺省时新建别名（调用方必须以其 JOIN，否则批次兜底失效）。
    """
    batch = batch_alias if batch_alias is not None else aliased(ImportBatch)
    return batch, sa_func.coalesce(
        sa_func.date_format(FactAdDaily.biz_date, '%Y-%m'),
        FactAdDaily.period_month,
        batch.period_month,
        sa_func.date_format(batch.imported_at, '%Y-%m'),
    )


def _stores_filter(q, stores):
    """store 过滤：int=单店 / list=多店（空→永假）/ None=不限"""
    if stores is None:
        return q
    if isinstance(stores, (list, tuple)):
        return q.filter(FactAdDaily.store_id.in_(stores or [-1]))
    return q.filter(FactAdDaily.store_id == stores)


def ad_spend_subquery(db: Session, stores: Union[int, list, None] = None,
                      country: Optional[str] = None,
                      period_month: Optional[str] = None,
                      year: Optional[int] = None):
    """广告花费 (asin, country) 聚合的 SQLAlchemy 子查询 — 供 /query/sales JOIN 下推复用

    归月过滤逻辑与 ad_spend_by_key 同源（本函数是唯一构建点）。
    """
    batch = aliased(ImportBatch)
    _, month_expr = ad_month_expr(batch)
    q = db.query(
        FactAdDaily.asin.label('asin'),
        FactAdDaily.country.label('country'),
        sa_func.sum(FactAdDaily.spend).label('v'),
    ).outerjoin(batch, FactAdDaily.batch_id == batch.id)
    q = _stores_filter(q, stores)
    if country:
        q = q.filter(FactAdDaily.country == country)
    if period_month:
        q = q.filter(month_expr == period_month)
    elif year:
        q = q.filter(month_expr.like(f"{int(year)}-%"))
    return q.group_by(FactAdDaily.asin, FactAdDaily.country).subquery()


def ad_spend_by_key(db: Session, stores: Union[int, list, None] = None,
                    country: Optional[str] = None,
                    period_month: Optional[str] = None,
                    year: Optional[int] = None) -> dict[tuple[str, str], Decimal]:
    """广告花费按 (asin, country) 归集 — SQL 聚合 + SQL 归月（全系统唯一实现）

    period_month 精确月 / year 整年（二者给其一；都给以 period_month 为准）。
    stores: int=单店 / list=多店（空→永假）/ None=不限。
    """
    if not period_month and not year:
        raise ValueError("period_month 与 year 必须提供其一")
    sq = ad_spend_subquery(db, stores, country, period_month, year)
    out: dict[tuple[str, str], Decimal] = {}
    for a, c, spend in db.query(sq.c.asin, sq.c.country, sq.c.v).all():
        out[(a or '', c or '')] = Decimal(str(spend)) if spend is not None else Decimal(0)
    return out


def ad_spend_all_by_key(db: Session, stores: Union[int, list, None] = None,
                        country: Optional[str] = None) -> dict[tuple[str, str], Decimal]:
    """无时间过滤的广告花费全量归集 (asin, country)

    对齐 query._ad_by_key 在 period_month/year 均未提供时的行为
    （/query/sales 区间/全量模式：广告不按归月过滤，直接累加）。
    """
    q = db.query(
        FactAdDaily.asin.label('asin'),
        FactAdDaily.country.label('country'),
        sa_func.sum(FactAdDaily.spend).label('spend'),
    )
    q = _stores_filter(q, stores)
    if country:
        q = q.filter(FactAdDaily.country == country)
    out: dict[tuple[str, str], Decimal] = {}
    for a, c, spend in q.group_by(FactAdDaily.asin, FactAdDaily.country).all():
        out[(a or '', c or '')] = Decimal(str(spend)) if spend is not None else Decimal(0)
    return out