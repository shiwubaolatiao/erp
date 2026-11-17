"""核算取数层 — 批量 SQL 聚合（B-01 修复的取数编排，利润公式层不动）

四组取数（tasks.md 2.2），全部 SQL 聚合、返回 TypedDict/plain 行（禁止 ORM 实体全量列表）：
  ① 销售聚合   单条 GROUP BY (asin, sku, country)：11 金额列 SUM/CASE + Adjustment 分桶 SQL 化
  ② 费用聚合   仓储/超龄/退货（月份列前缀匹配）+ 移除/入库（日期区间），GROUP BY (asin, country)
  ③ 成本版本   当月 + 历史各 1 条批量查询（消灭逐 SKU 2 次）
  ④ 广告聚合   LEFT JOIN import_batch 单条查询（三级归月 SQL 化，语义对齐 service.py 存量实现）

★ 语义对齐声明：聚合结果与原 service.py 逐项一致（含 orders 恒 0 的存量行为），
  唯一有意变化：聚合键从 (asin, sku) 扩展为 (asin, sku, country)——B-01 逐站点折算的前提。
"""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func as sa_func, case as sa_case, and_ as sa_and, or_ as sa_or
from sqlalchemy.orm import Session, aliased

from app.models import (
    FactSalesTransaction, FactAdDaily, ImportBatch,
    FactFeeStorage, FactFeeAgedStorage, FactFeeRemoval, FactFeeInboundConfig,
    FactFeeReturn, ProductCostHistory,
)
from app.services.common.fees import fee_by_key
from app.services.common.costs import pick_period

PROFIT_TYPES = ('Order', 'Refund')


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


# ==================== ① 销售聚合 ====================

def fetch_sales_agg(db: Session, store_id: int, start: datetime, end: datetime,
                    country: Optional[str] = None) -> list[dict]:
    """销售按 (asin, normalized_sku, country) 聚合 — 单条 SQL

    11 金额列只累计 type ∈ {Order, Refund}；Adjustment 分桶（has_order_id →
    other_amount，否则 line_total）也在 SQL 内完成（语义对齐 service.py:80-101）。
    orders 恒 0 —— 与存量实现一致（d['orders'] 从未累计，快照明细 order_count 为全店级）。
    """
    t = FactSalesTransaction
    cols_11 = [('ps', 'product_sales'), ('pst', 'product_sales_tax'),
               ('pc', 'postage_credits'), ('sct', 'shipping_credits_tax'),
               ('gwc', 'gift_wrap_credits'), ('gwct', 'giftwrap_credits_tax'),
               ('promo', 'promo_rebate'), ('promo_tax', 'promo_rebate_tax'),
               ('mkt', 'marketplace_withheld_tax'), ('sf', 'selling_fee'),
               ('fba', 'fba_fee')]
    selects = [
        t.asin.label('asin'),
        t.normalized_sku.label('sku'),
        t.country.label('country'),
    ]
    for alias, col in cols_11:
        selects.append(sa_func.sum(sa_case(
            (t.transaction_type.in_(PROFIT_TYPES), getattr(t, col)),
            else_=0,
        )).label(alias))
    # Adjustment 分桶：有 order_id → other 列；无 → total 列（两桶同入 adj）

    adj_expr = sa_func.sum(sa_case(
        (t.transaction_type == 'Adjustment',
         sa_case((sa_func.coalesce(t.has_order_id, 0) == 1, t.other_amount),
                 else_=t.line_total)),
        else_=0,
    )).label('adj')
    selects.append(adj_expr)
    selects.extend([
        sa_func.sum(sa_case((t.transaction_type == 'Order', sa_func.coalesce(t.quantity, 0)),
                            else_=0)).label('qty'),
        (sa_func.sum(sa_case((t.transaction_type == 'Order', sa_func.coalesce(t.quantity, 0)),
                             else_=0))
         - sa_func.sum(sa_case((t.transaction_type == 'Refund',
                                sa_func.abs(sa_func.coalesce(t.quantity, 0))),
                               else_=0))).label('net_qty'),
        sa_func.group_concat(sa_func.distinct(t.batch_id)).label('batches'),
    ])
    q = db.query(*selects).filter(
        t.store_id == store_id,
        t.biz_date >= start, t.biz_date < end,
    )
    if country:
        q = q.filter(t.country == country)
    q = q.group_by(t.asin, t.normalized_sku, t.country)
    rows = []
    for r in q.all():
        rows.append({
            'asin': r.asin or '', 'sku': r.sku or '', 'country': r.country or '',
            'ps': _d(r.ps), 'pst': _d(r.pst), 'pc': _d(r.pc), 'sct': _d(r.sct),
            'gwc': _d(r.gwc), 'gwct': _d(r.gwct), 'promo': _d(r.promo),
            'promo_tax': _d(r.promo_tax), 'mkt': _d(r.mkt), 'sf': _d(r.sf),
            'fba': _d(r.fba), 'adj': _d(r.adj),
            'qty': int(r.qty or 0), 'net_qty': int(r.net_qty or 0),
            'orders': 0,   # 存量行为：明细 order_count 恒 0（全店级计数在快照层）
            'batch_ids': sorted({int(x) for x in (r.batches or '').split(',') if x}),
        })
    return rows


def fetch_order_count(db: Session, store_id: int, start: datetime, end: datetime,
                      country: Optional[str] = None) -> int:
    """全店（或站点）去重订单数 — 与存量 total_order_ids 语义一致（Order/Refund 均计）"""
    t = FactSalesTransaction
    q = db.query(sa_func.count(sa_func.distinct(t.order_id))).filter(
        t.store_id == store_id,
        t.biz_date >= start, t.biz_date < end,
        t.transaction_type.in_(PROFIT_TYPES),
        t.order_id.isnot(None), t.order_id != '',
    )
    if country:
        q = q.filter(t.country == country)
    return q.scalar() or 0


# ==================== ② 费用聚合 ====================

def fetch_fee_agg(db: Session, store_id: int, period_month: str,
                  start: datetime, end: datetime,
                  country: Optional[str] = None) -> dict[str, dict[tuple[str, str], Decimal]]:
    """五类费用按 (asin, country) 聚合 → {费用名: {(asin, country): 原币合计}}

    唯一实现 common/fees.fee_by_key：仓储/超龄/退货走月份列（前缀→组10 清洗后
    全系统统一切等值）；移除/入库走业务日期半开区间。
    """
    return {
        'storage': fee_by_key(db, FactFeeStorage, 'estimated_monthly_storage_fee',
                              'month_of_charge', period_month, store_id, country),
        'aged': fee_by_key(db, FactFeeAgedStorage, 'amount_charged',
                           'snapshot_date', period_month, store_id, country),
        'returns': fee_by_key(db, FactFeeReturn, 'sku_returns_fee',
                              'month_of_charge', period_month, store_id, country),
        'removal': fee_by_key(db, FactFeeRemoval, 'removal_fee',
                              date_col=FactFeeRemoval.request_date, start=start, end=end,
                              stores=store_id, country=country),
        'inbound': fee_by_key(db, FactFeeInboundConfig, 'total_fee',
                              date_col=FactFeeInboundConfig.transaction_date, start=start, end=end,
                              stores=store_id, country=country),
    }


# ==================== ③ 成本版本 ====================

class CostLookup:
    """(asin, sku) → (cost_rmb, freight_rmb, status, inherited_from) 批量查询

    语义与 service.get_cost 逐字一致：
      ① 当月有版本 → normal（cost_rmb NULL → missing）
      ② 回退历史最近版本 → inherited（cost_rmb NULL → missing），inherited_from=该月
      ③ 从未维护 → missing (None, None)
    """

    def __init__(self, db: Session, store_id: int, period_month: str):
        self._by_key: dict[tuple[str, str], dict[str, tuple]] = {}
        rows = db.query(ProductCostHistory).filter(
            ProductCostHistory.store_id == store_id,
        ).order_by(ProductCostHistory.period.desc(), ProductCostHistory.id.desc()).all()
        for ch in rows:
            k = (ch.asin or '', ch.sku or '')
            per = self._by_key.setdefault(k, {})
            if ch.period in per:
                continue   # 同月已取（更新的一条在前，保持"取最新一条"语义）
            per[ch.period] = (ch.cost_rmb, _freight(ch))

    def get(self, asin: str, sku: str, period_month: str):
        per = self._by_key.get((asin or '', sku or ''), {})
        p = pick_period(per, period_month)   # 月度回退唯一实现 common/costs
        if p is None:
            return None, None, 'missing', ''
        cost, freight = per[p]
        if cost is None:
            # 版本存在但 cost_rmb NULL → missing（inherited_from 仍指向该版本，与存量一致）
            return _d(cost), freight, 'missing', ('' if p == period_month else p)
        status = 'normal' if p == period_month else 'inherited'
        return _d(cost), freight, status, ('' if p == period_month else p)


def _freight(ch: ProductCostHistory) -> Optional[Decimal]:
    """从 freight_json 取头程运费（与 service._freight 一致：default 键，无则 None）"""
    if not ch.freight_json:
        return None
    fj = ch.freight_json if isinstance(ch.freight_json, dict) else {}
    v = fj.get('default')
    return Decimal(str(v)) if v is not None else None


def fetch_cost_lookup(db: Session, store_id: int, period_month: str) -> CostLookup:
    return CostLookup(db, store_id, period_month)


# ==================== ④ 广告聚合 ====================

def fetch_ad_agg(db: Session, store_id: int, period_month: str,
                 start: datetime, end: datetime,
                 country: Optional[str] = None) -> dict[tuple[str, str, str], Decimal]:
    """广告花费按 (asin, sku, country) 聚合 — 单条 SQL + 三级归月

    语义对齐 service.py:182-206（★ 与 /query 广告页的"归月==period_month"口径不同）：
      biz_date 非空 → 按业务日期区间过滤（支持自定义区间核算）；
      biz_date 为空 → COALESCE(period_month, 批次月, 批次导入月) == period_month。
    """
    a = FactAdDaily
    batch = aliased(ImportBatch)
    fallback_month = sa_func.coalesce(
        a.period_month, batch.period_month,
        sa_func.date_format(batch.imported_at, '%Y-%m'),
    )
    q = db.query(
        a.asin.label('asin'), a.sku.label('sku'), a.country.label('country'),
        sa_func.sum(a.spend).label('spend'),
    ).outerjoin(batch, a.batch_id == batch.id).filter(
        a.store_id == store_id,
        # ★ or_/and_ 是 SQLAlchemy 顶层构造（SQL 关键字），不是 func 方法——
        #   sa_func.or_ 会渲染成字面量 or(and(...)) → MySQL 1064
        sa_or(
            sa_and(a.biz_date.isnot(None), a.biz_date >= start, a.biz_date < end),
            sa_and(a.biz_date.is_(None), fallback_month == period_month),
        ),
    )
    if country:
        q = q.filter(a.country == country)
    out: dict[tuple[str, str, str], Decimal] = {}
    for asin, sku, c, spend in q.group_by(a.asin, a.sku, a.country).all():
        out[(asin or '', sku or '', c or '')] = _d(spend)
    return out