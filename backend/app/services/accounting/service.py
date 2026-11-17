"""核算服务 — 自然月/区间核算 → 快照版本 → 成本完整性检查
利润公式 = app/services/profit/engine.py（旧系统公式原样移植，零改动）

分层（B-01 修复）：
  fetcher.py   批量取数（SQL 聚合，聚合键 (asin, sku, country)）
  calculator.py 纯内存计算（逐站点汇率单入口调用 engine 公式）
  service.py    编排 + 落库（本文件）

★ B-01 修复（多站点汇总核算汇率折算缺陷）：
  店铺级核算（country=''）不再走 `country or 'US'` 单汇率路径（删除 service.py:209 缺陷分支），
  改为 RateResolver 逐站点解析汇率 → calculator 逐站点折算 → 汇总。
  快照 exchange_rate 存加权平均（展示用），rate_source/rate_detail 记录真实折算构成。

口径（文档 MM-05 5.4，已定稿）：
- net_amazon = Σ(type ∈ {Order, Refund}) 13字段 + Adjustment 分桶
- Adjustment: 有order_id → other 列；无order_id → total 按正负分桶
- 不计入利润: Service Fee / Amazon Fees / FBA Inventory Fee / FBA transaction fees / Transfer
  （Transfer 单独资金流）
- 净利润 = net_amazon × 汇率 − 采购成本(RMB) − 头程运费(RMB) − (广告+仓储+超龄+退货+入库+移除)×汇率
- 成本完整性 4 状态：正常/沿用历史/成本缺失/运费缺失 — 缺失不得静默按0算，标记"成本不完整"
"""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.models import (
    ExchangeRate, DwsProfitSnapshot, DwsProfitSnapshotItem,
    CostMissingAlert, Store,
)
from app.services.profit.engine import get_exchange_rate
from app.services.common.rates import RateResolver
from app.services.accounting.fetcher import (
    fetch_sales_agg, fetch_fee_agg, fetch_ad_agg, fetch_cost_lookup, fetch_order_count,
)
from app.services.accounting.calculator import calculate_items, summarize


def month_range(period_month: str) -> tuple[datetime, datetime]:
    """YYYY-MM → [月初, 月末+1)"""
    y, m = int(period_month[:4]), int(period_month[5:7])
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), (m % 12) + 1, 1)
    return start, end


def calculate_snapshot(db: Session, store_id: int, country: str, period_month: str,
                       created_by: Optional[int] = None,
                       date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
                       snapshot_name: str = "") -> DwsProfitSnapshot:
    """按自然月（默认）或区间核算，产出新版本快照（对外签名不变）。

    核算按原始业务日期过滤（biz_date），绝不按文件月份。
    费用类按 month_of_charge / snapshot_date 整月归属（与自然月对齐）。
    广告无日期列时按三级归月（period_month → 批次月 → 批次导入月），见 fetcher.fetch_ad_agg。
    """
    if date_from and date_to:
        start, end = date_from, date_to
    else:
        start, end = month_range(period_month)

    store = db.query(Store).get(store_id)

    # ---------- 1. 批量取数（SQL 聚合，聚合键 (asin, sku, country)） ----------
    sales_rows = fetch_sales_agg(db, store_id, start, end, country or None)
    fees = fetch_fee_agg(db, store_id, period_month, start, end, country or None)
    ad_by_key = fetch_ad_agg(db, store_id, period_month, start, end, country or None)
    cost_lookup = fetch_cost_lookup(db, store_id, period_month)
    order_count_total = fetch_order_count(db, store_id, start, end, country or None)

    # ---------- 2. 汇率解析 ----------
    # 站点级：单汇率（存量路径不变）；店铺级：逐站点解析（B-01 修复核心）
    countries = sorted({r['country'] for r in sales_rows if r['country']})
    if country:
        er = get_exchange_rate(db, country, store_id=store_id,
                               period_month=period_month, ExchangeRateModel=ExchangeRate)
        rates = {country: er}
        rate_source = 'single'
        missing_countries: list[str] = []
    else:
        resolver = RateResolver(db, store_id=store_id, period_month=period_month)
        resolved = resolver.resolve_with_source(countries)
        rates = {c: r for c, (r, _src) in resolved.items()}
        missing_countries = sorted(c for c, (_r, src) in resolved.items() if src == 'default')
        rate_source = 'default_fallback' if missing_countries else 'per_country'

    # ---------- 3. 纯内存计算（逐 SKU×站点，公式单入口 engine） ----------
    items = calculate_items(sales_rows, fees, ad_by_key, cost_lookup, rates, period_month)
    summary = summarize(items, rates, sales_rows)

    # ---------- 4. 落库前一致性自检（D-04 根治：自检不过拒绝落库） ----------
    _self_check(items, sales_rows, summary, order_count_total)

    # ---------- 5. 快照 + 明细 + 预警 —— 单事务提交 ----------
    prev_version = db.query(sa_func.max(DwsProfitSnapshot.version)).filter(
        DwsProfitSnapshot.store_id == store_id,
        DwsProfitSnapshot.country == (country or ''),
        DwsProfitSnapshot.period_month == (period_month if not (date_from and date_to) else None),
        DwsProfitSnapshot.date_from == date_from, DwsProfitSnapshot.date_to == date_to,
    ).scalar() or 0

    rate_detail = {c: str(r) for c, r in sorted(rates.items())}
    if missing_countries:
        rate_detail['_missing'] = missing_countries

    snap = DwsProfitSnapshot(
        store_id=store_id, country=country or '',
        period_month=period_month if not (date_from and date_to) else None,
        date_from=date_from, date_to=date_to,
        version=prev_version + 1, snapshot_name=snapshot_name,
        net_amazon=summary['net_amazon'],
        exchange_rate=er if country else summary['weighted_exchange_rate'],
        rate_source=rate_source,
        rate_detail=rate_detail,
        product_cost_rmb=summary['product_cost_rmb'],
        freight_cost_rmb=summary['freight_cost_rmb'],
        ad_spend=summary['ad_spend'],
        storage_fee=summary['storage_fee'],
        aged_storage_fee=summary['aged_storage_fee'],
        returns_fee=summary['returns_fee'],
        inbound_fee=summary['inbound_fee'],
        removal_fee=summary['removal_fee'],
        net_profit_rmb=summary['net_profit_rmb'],
        net_profit_rate=(summary['net_profit_rmb'] / summary['product_sales_rmb']).quantize(
            Decimal('0.0001')) if summary['product_sales_rmb'] else Decimal(0),
        product_sales_rmb=summary['product_sales_rmb'],
        order_count=order_count_total,
        order_qty=summary['order_qty'],
        cost_complete=(summary['incomplete'] == 0),
        cost_incomplete_skus=summary['incomplete'],
        created_by=created_by,
    )
    db.add(snap)
    db.flush()
    for it in items:
        db.add(DwsProfitSnapshotItem(snapshot_id=snap.id, **it))

    # 成本缺失 → 预警表（缺失 SKU 进入"待补录"队列；不静默按 0 计算）
    # ★ session autoflush=False：同事务内已 add 未 flush 的行 db.query 查不到——
    #   店铺级快照明细为 SKU×站点多行，同 (asin,sku) 跨站点均缺失时若只查库会
    #   重复 add 撞 uq_alert_item(1062)。先查库（幂等）再按 seen 内存去重。
    seen_alert_keys: set = set()
    for it in items:
        if it['cost_status'] == 'missing':
            missing_type = 'both'
            akey = (store_id, it['asin'] or '', it['sku'] or '', period_month or '', missing_type)
            if akey in seen_alert_keys:
                continue
            existing = db.query(CostMissingAlert).filter(
                CostMissingAlert.store_id == store_id,
                CostMissingAlert.asin == it['asin'], CostMissingAlert.sku == it['sku'],
                CostMissingAlert.period == period_month, CostMissingAlert.missing_type == missing_type,
            ).first()
            if not existing:
                db.add(CostMissingAlert(store_id=store_id, asin=it['asin'], sku=it['sku'],
                                        country=country or '', period=period_month,
                                        missing_type=missing_type, status='pending'))
            seen_alert_keys.add(akey)
    db.commit()
    return snap


class SnapshotConsistencyError(Exception):
    """落库前自检失败（Σ明细 != 快照汇总）—— 拒绝落库，事务回滚"""


def _self_check(items: list[dict], sales_rows: list[dict],
                summary: dict, order_count_total: int) -> None:
    """落库前一致性自检 — 任一不过抛 SnapshotConsistencyError（调用方事务回滚）

    防线意图：summary 目前从 items 求和，自检数学上恒真；但它锁定"明细 → 汇总"
    的求和路径，防止未来 summary 改为独立计算源时口径漂移（D-04 根治手段）。
    """
    tol = Decimal('0.01')
    if len(items) != len(sales_rows):
        raise SnapshotConsistencyError(
            f"明细行数 {len(items)} != 销售聚合行数 {len(sales_rows)}")
    chk = sum((i['net_profit_rmb'] for i in items), Decimal(0))
    if abs(chk - summary['net_profit_rmb']) > tol:
        raise SnapshotConsistencyError(
            f"Σ明细净利润 {chk} != 汇总 {summary['net_profit_rmb']}（差异>{tol}）")
    chk2 = sum((i['net_amazon'] for i in items), Decimal(0))
    if abs(chk2 - summary['net_amazon']) > tol:
        raise SnapshotConsistencyError(
            f"Σ明细net_amazon {chk2} != 汇总 {summary['net_amazon']}（差异>{tol}）")
