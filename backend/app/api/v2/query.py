"""查询 — 销售/订单/商品/广告/费用 多维统计（按店铺/站点/ASIN/SKU/月份或区间）
查询直读 DWD fact 表 + 映射主档；支持下钻到溯源。
"""
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func, case as sa_case, and_, or_, cast, Numeric
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.config import settings
from app.core.security import (
    get_current_user, require_module, filter_store_by_permission, get_authorized_store_ids,
)
from app.models import (
    FactSalesTransaction, FactAdDaily, Store, StoreProduct, StoreSkuMapping,
    FactFeeStorage, FactFeeReturn, FactFeeRemoval, FactFeeInboundConfig, FactFeeAgedStorage,
    ProductCostHistory, ImportBatch, ExchangeRate,
)
from app.services.profit.engine import get_exchange_rate
from app.services.common.admonth import ad_spend_by_key, ad_spend_all_by_key, ad_month_expr, ad_spend_subquery
from app.services.common.fees import fee_by_key, month_filter_expr
from app.services.common.period import month_bounds, PeriodFormatError
from app.services.common.costs import pick_period
from app.services.common.rates import RateResolver
from app.services.common.contract_alias import alias_row, inject_legacy
from sqlalchemy.orm import aliased

router = APIRouter()

PROFIT_TYPES = ('Order', 'Refund')


def _month_range(period_month: str) -> tuple[datetime, datetime]:
    """YYYY-MM → [月初, 次月初) — 唯一实现在 common/period.month_bounds（非法格式 400）"""
    try:
        return month_bounds(period_month)
    except PeriodFormatError:
        raise HTTPException(status_code=400, detail="period_month 格式应为 YYYY-MM")


def _resolve_store(db: Session, user, store_code: str | None) -> Store | None:
    """解析 store 参数为店铺对象（同时做数据权限校验）"""
    if not store_code:
        return None
    return filter_store_by_permission(user, store_code, db)


def _apply_store_scope(q, model, user, db, store_obj):
    """数据权限：指定店铺按该店铺过滤；未指定店铺时，非管理员限定在其授权店铺内"""
    if store_obj:
        return q.filter(model.store_id == store_obj.id)
    ids = get_authorized_store_ids(user, db)
    if ids is not None:
        if not ids:
            return q.filter(model.store_id == -1)   # 无授权店铺 → 空结果
        return q.filter(model.store_id.in_(ids))
    return q


def _D(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


def _scope_filter(q, model, ids):
    """store 过滤：ids=None（管理员不限）不过滤；空列表 → 永假条件"""
    if ids is None:
        return q
    return q.filter(model.store_id.in_(ids or [-1]))


def _net_amazon_expr(t=FactSalesTransaction):
    """net_amazon 口径：11 个金额列之和（Order/Refund 行 signed 合计）— 与核算快照一致"""
    return (t.product_sales + t.product_sales_tax + t.postage_credits + t.shipping_credits_tax
            + t.gift_wrap_credits + t.giftwrap_credits_tax + t.promo_rebate + t.promo_rebate_tax
            + t.marketplace_withheld_tax + t.selling_fee + t.fba_fee)


# （费用按 (asin,country) 归集已收敛）唯一实现在 common/fees.fee_by_key：
#   month_col 模式（仓储/超龄/退货，前缀→组10 清洗后统一切等值）与
#   date_col 模式（移除 request_date / 入库 transaction_date 半开区间）。


def _ad_by_key(db, ids, country, period_month, year):
    """广告花费按 (asin, country) 归集 — 全系统唯一实现 common/admonth（SQL 三级归月）

    P-07 修复：原实现在模块级 dict 缓存批次归属月（无失效、跨会话污染）且全表
    加载 Python 归月，已删除；语义由 admonth SQL 表达式逐行对齐。
    """
    if period_month or year:
        return ad_spend_by_key(db, ids, country, period_month, year)
    # 区间/全量模式：不按归月过滤（对齐原 _ad_by_key 在无 period_month/year 时的行为）
    return ad_spend_all_by_key(db, ids, country)


def _cost_versions(db, ids, asins):
    """成本版本 → {asin: {period: (cost_rmb, freight_rmb)}}（同月同 asin 多版本取最新）"""
    if not asins:
        return {}
    q = db.query(ProductCostHistory).filter(
        ProductCostHistory.asin.in_(asins),
    )
    q = _scope_filter(q, ProductCostHistory, ids)
    rows = q.order_by(ProductCostHistory.period.desc(), ProductCostHistory.id.desc()).all()
    out: dict = {}
    for ch in rows:
        per = out.setdefault(ch.asin, {})
        if ch.period in per:      # 同月已取过（更新的一条）
            continue
        fj = ch.freight_json if isinstance(ch.freight_json, dict) else {}
        per[ch.period] = (_D(ch.cost_rmb), _D(fj.get('default') if fj.get('default') is not None else 0))
    return out


def _pick_cost(per: dict, month: str):
    """月度成本回退 — 唯一实现在 common/costs.pick_period（当月 → 历史最近 → 缺失）"""
    p = pick_period(per, month)
    return per[p] if p is not None else (None, None)


# 下推排序键映射：前端 sort_by → 合并行 SELECT 别名（未命中则回退 Python 路径）
_PUSH_SORT_MAP = {
    'sales_usd': 'v_sales', 'sales': 'v_sales', 'product_sales_usd': 'v_sales',
    'product_sales_rmb': 'v_sales_rmb', 'net_profit_rmb': 'v_profit',
    'net_amazon_usd': 'v_net_amazon', 'qty': 'v_qty', 'net_qty': 'v_net_qty',
    'refund_qty': 'v_rqty', 'refund_amount': 'v_refund', 'order_count': 'v_orders',
    'commission_usd': 'v_commission', 'fba_fee_usd': 'v_fba', 'ad_spend_usd': 'v_ad',
    'storage_fee_usd': 'v_st', 'aged_storage_fee_usd': 'v_aged',
    'returns_fee_usd': 'v_ret', 'inbound_fee_usd': 'v_inb',
    'removal_fee_usd': 'v_rem', 'product_cost_rmb': 'v_cost',
    'freight_cost_rmb': 'v_freight',
}


def _cost_effective_subquery(db, ids, period_month):
    """每 asin 的生效成本版本子查询（当月 → 历史最近，同月取最新 id）

    语义对齐 _cost_versions + _pick_cost：period<=当月的最大 period（同 period 取 max id），
    返回 (asin, cost_rmb, freight)——freight 取 freight_json['default']（缺失→NULL→COALESCE 0）。
    """
    pch = ProductCostHistory
    d_sq = db.query(
        pch.asin.label('asin'), pch.period.label('period'),
        sa_func.max(pch.id).label('mid'),
    ).group_by(pch.asin, pch.period).subquery()
    b_q = db.query(pch.asin.label('asin'), sa_func.max(pch.period).label('mp')).filter(
        pch.period <= period_month)
    if ids is not None:
        b_q = b_q.filter(pch.store_id.in_(ids or [-1]))
    b_sq = b_q.group_by(pch.asin).subquery()
    return db.query(
        pch.asin.label('asin'),
        pch.cost_rmb.label('cost_rmb'),
        cast(sa_func.json_extract(pch.freight_json, '$.default'),
             Numeric(14, 4)).label('freight'),
    ).join(
        d_sq, and_(d_sq.c.asin == pch.asin, d_sq.c.period == pch.period, d_sq.c.mid == pch.id)
    ).join(
        b_sq, and_(b_sq.c.asin == pch.asin, b_sq.c.mp == pch.period)
    ).subquery()


def _sales_pushdown(db, base_q, scoped_ids, store_obj, period_month, country,
                    sort_by, sort_order, page, page_size, group_by='asin'):
    """/query/sales 月度 asin 口径 SQL 下推（P-02，design 2.2 方案）

    主聚合 GROUP BY (store_id, asin, country) 为基表，费用/广告/成本各自聚合为
    LEFT JOIN 派生表，汇率按 (country, period) 预解析为 CASE 常量注入；
    SQL 端计算净利（行级 ROUND 2 位，对齐 Python quantize 后累加的存量口径）
    并排序 + LIMIT/OFFSET；(asin, sku) 跨国合并在 SQL GROUP BY 完成。
    返回完整响应 dict；不适用（非月度/非 asin 分组/排序键不可映射/开关关）返回 None。
    """
    if not settings.PERF_SALES_PUSHDOWN or not period_month:
        return None
    # 仅 asin 口径可下推：sku/month 走简单分支；country 需 Python 侧跨店合并再聚合
    if group_by != 'asin':
        return None
    sort_col = _PUSH_SORT_MAP.get(sort_by or 'sales_usd')
    if sort_col is None:
        return None
    fs, fe = _month_range(period_month)

    # ---- 基表：主聚合 (store_id, asin, country) ----
    base = base_q.group_by(FactSalesTransaction.store_id,
                           FactSalesTransaction.asin,
                           FactSalesTransaction.country).subquery()
    b = base.c
    co = sa_func.coalesce

    # ---- 汇率 CASE 常量（RateResolver 批量解析，四级回退与 engine 一致）----
    countries = [r[0] for r in db.query(base.c.country).distinct().all() if r[0]]
    if not countries:
        return inject_legacy({"total": 0, "items": [], "totals": {}}, 'query/sales')
    rates = RateResolver(db, store_obj.id if store_obj else None,
                         period_month).resolve(countries)
    # ★ SQLAlchemy 2.0：case 的 whens 必须位置传参，sa_case([...]) 会 ArgumentError
    rate_case = sa_case(*[(b.country == c, rates[c]) for c in countries],
                        else_=Decimal('6.8'))

    # ---- 费用/广告派生表（各一条聚合子查询，语义同 _fee_like/_fee_range/_ad_by_key）----
    def fee_subq(model, val_col, month_col=None, date_col=None):
        q = db.query(model.asin.label('asin'), model.country.label('country'),
                     sa_func.sum(getattr(model, val_col)).label('v'))
        if month_col is not None:
            # 月份过滤走 common/fees 唯一切换点（前缀→组10 清洗后等值，与全系统一致）
            q = q.filter(month_filter_expr(model, month_col, period_month))
        else:
            q = q.filter(date_col >= fs, date_col < fe)
        if scoped_ids is not None:
            q = q.filter(model.store_id.in_(scoped_ids or [-1]))
        if country:
            q = q.filter(model.country == country)
        return q.group_by(model.asin, model.country).subquery()

    subs = {
        'ad': ad_spend_subquery(db, scoped_ids, country, period_month=period_month),
        'st': fee_subq(FactFeeStorage, 'estimated_monthly_storage_fee', month_col='month_of_charge'),
        'aged': fee_subq(FactFeeAgedStorage, 'amount_charged', month_col='snapshot_date'),
        'ret': fee_subq(FactFeeReturn, 'sku_returns_fee', month_col='month_of_charge'),
        'rem': fee_subq(FactFeeRemoval, 'removal_fee', date_col=FactFeeRemoval.request_date),
        'inb': fee_subq(FactFeeInboundConfig, 'total_fee', date_col=FactFeeInboundConfig.transaction_date),
    }
    cost_sq = _cost_effective_subquery(db, scoped_ids, period_month)

    # ---- 外层：GROUP BY (asin, sku) 跨国/跨店合并，SQL 端计算净利 ----
    nq = co(b.qty, 0) - co(b.rqty, 0)
    fee_row = sum(sa_func.coalesce(subs[k].c.v, 0) for k in subs)
    profit_row = (
        (co(b.core, 0) + co(b.adj, 0) - fee_row) * rate_case
        - sa_func.coalesce(cost_sq.c.cost_rmb, 0) * sa_case((nq > 0, nq), else_=0)
        - sa_func.coalesce(cost_sq.c.freight, 0) * sa_case((nq > 0, nq), else_=0)
    )
    o = db.query(
        b.asin.label('asin'), co(b.sku, '').label('sku'),
        sa_func.group_concat(sa_func.distinct(b.country)).label('countries'),
        sa_func.sum(co(b.qty, 0)).label('v_qty'),
        sa_func.sum(co(b.rqty, 0)).label('v_rqty'),
        sa_func.sum(co(b.qty, 0) - co(b.rqty, 0)).label('v_net_qty'),
        sa_func.sum(co(b.refund, 0)).label('v_refund'),
        sa_func.sum(co(b.orders, 0)).label('v_orders'),
        sa_func.sum(co(b.sales_net, 0)).label('v_sales'),
        sa_func.sum(sa_func.round(co(b.sales_net, 0) * rate_case, 2)).label('v_sales_rmb'),
        sa_func.sum(co(b.commission, 0)).label('v_commission'),
        sa_func.sum(co(b.fba, 0)).label('v_fba'),
        (sa_func.sum(co(b.core, 0)) + sa_func.sum(co(b.adj, 0))).label('v_net_amazon'),
        *[sa_func.sum(co(subs[k].c.v, 0)).label(f'v_{k}') for k in subs],
        # 成本量：行级净销量为正才计成本（对齐 _cost_freight_for 的 max(net_qty, 0)）
        sa_func.sum(sa_case((nq > 0, nq), else_=0)).label('cost_qty'),
        # cost/freight 非分组列 → MAX 包裹（组内 per asin 常量，取值不受影响；
        # 同时规避 only_full_group_by 对 JOIN 非主分组列的拒绝）
        (sa_func.max(sa_func.coalesce(cost_sq.c.cost_rmb, 0))
         * sa_func.sum(sa_case((nq > 0, nq), else_=0))).label('v_cost'),
        (sa_func.max(sa_func.coalesce(cost_sq.c.freight, 0))
         * sa_func.sum(sa_case((nq > 0, nq), else_=0))).label('v_freight'),
        sa_func.sum(sa_func.round(profit_row, 2)).label('v_profit'),
    )
    o = o.outerjoin(cost_sq, cost_sq.c.asin == b.asin)
    for k, sq in subs.items():
        o = o.outerjoin(sq, and_(sq.c.asin == b.asin, sq.c.country == b.country))
    merged = o.group_by(b.asin, co(b.sku, '')).subquery()
    m = merged.c

    total = db.query(sa_func.count()).select_from(merged).scalar() or 0
    if not total:
        return inject_legacy({"total": 0, "items": [], "totals": {}}, 'query/sales')
    tr = db.query(
        sa_func.sum(m.v_net_qty), sa_func.sum(m.v_sales), sa_func.sum(m.v_sales_rmb),
        sa_func.sum(m.v_commission), sa_func.sum(m.v_fba), sa_func.sum(m.v_ad),
        sa_func.sum(m.v_profit),
    ).select_from(merged).one()
    totals = {
        'net_qty': int(tr[0] or 0),
        'product_sales_usd': round(float(_D(tr[1])), 2),
        'product_sales_rmb': round(float(_D(tr[2])), 2),
        'commission_usd': round(float(_D(tr[3])), 2),
        'fba_fee_usd': round(float(_D(tr[4])), 2),
        'ad_spend_usd': round(float(_D(tr[5])), 2),
        'net_profit_rmb': round(float(_D(tr[6])), 2),
    }
    order_col = getattr(m, sort_col)
    page_rows = db.query(merged).order_by(
        order_col.desc() if sort_order != 'asc' else order_col.asc(),
        m.asin, m.sku,
    ).offset((page - 1) * page_size).limit(page_size).all()

    # ---- 当页行组装（title / 单价成本批量补全，各 1 次查询）----
    page_asins = {r.asin for r in page_rows if r.asin}
    title_map: dict = {}
    if page_asins:
        tp = db.query(StoreProduct.asin, StoreProduct.title).filter(
            StoreProduct.asin.in_(page_asins)).order_by(StoreProduct.id).all()
        for a, title in tp:
            title_map.setdefault(a, title or '')
    cost_per = _cost_versions(db, scoped_ids, page_asins) if page_asins else {}

    out = []
    for r in page_rows:
        cs = sorted({c for c in (r.countries or '').split(',') if c})
        c0 = cs[0] if len(cs) == 1 else ''
        sales = _D(r.v_sales)
        sales_rmb = _D(r.v_sales_rmb)
        profit = _D(r.v_profit)
        qty_i, rqty_i = int(r.v_qty or 0), int(r.v_rqty or 0)
        per_cost, per_freight = _pick_cost(cost_per.get(r.asin) or {}, period_month)
        row = {
            'asin': r.asin or '', 'sku': r.sku or '',
            'country': c0,
            'product_name': title_map.get(r.asin, ''),
            'color': '',
            'qty': qty_i, 'net_qty': int(r.v_net_qty or 0),
            'refund_qty': rqty_i, 'refund_amount': float(_D(r.v_refund)),
            'order_count': int(r.v_orders or 0),
            'sales_usd': float(sales),
            'product_sales_rmb': float(sales_rmb),
            'commission_usd': float(_D(r.v_commission)), 'fba_fee_usd': float(_D(r.v_fba)),
            'ad_spend_usd': float(_D(r.v_ad)), 'storage_fee_usd': float(_D(r.v_st)),
            'aged_storage_fee_usd': float(_D(r.v_aged)), 'returns_fee_usd': float(_D(r.v_ret)),
            'inbound_fee_usd': float(_D(r.v_inb)), 'removal_fee_usd': float(_D(r.v_rem)),
            'product_cost_rmb': float(_D(r.v_cost)), 'freight_cost_rmb': float(_D(r.v_freight)),
            'cost_rmb': float(per_cost) if per_cost is not None else None,
            'freight_per_unit': float(per_freight) if per_freight is not None else None,
            'net_amazon_usd': float(_D(r.v_net_amazon)), 'net_profit_rmb': float(profit),
            'net_profit_rate': round(float(profit / sales_rmb * 100), 2) if sales_rmb else 0.0,
            'ad_spend_pct': round(float(_D(r.v_ad) / sales * 100), 2) if sales else 0.0,
            'storage_fee_pct': round(float(_D(r.v_st) / sales * 100), 2) if sales else 0.0,
            'return_rate': round(rqty_i / qty_i * 100, 2) if qty_i else 0.0,
        }
        alias_row(row, 'query/sales_row')
        out.append(row)
    return inject_legacy({"total": int(total), "items": out, "totals": totals}, 'query/sales')


@router.get("/sales")
def query_sales(
    store: str | None = Query(None, description="店铺代码"),
    country: str | None = Query(None, description="站点"),
    period_month: str | None = Query(None, description="自然月 YYYY-MM"),
    year: int | None = Query(None, description="年份（旧前端兼容；带 month 等效 period_month）", deprecated=True),
    month: int | None = Query(None, description="月份 1-12（旧前端兼容）", deprecated=True),
    date_from: str | None = None,
    date_to: str | None = None,
    asin: str | None = None,
    sku: str | None = None,
    keyword: str | None = Query(None, description="ASIN/SKU 关键字（模糊）"),
    group_by: str = Query("asin", description="asin/sku/country/month"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    sort_by: str | None = Query(None, description="排序字段（销售额/利润/销量等）"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    user=Depends(require_module("query")),
):
    """销售/订单统计 — 按原始业务日期过滤（绝不按文件月份）

    ★ 旧前端兼容（DataQuery 页）：接受 year/month 参数；支持 page/page_size/
      keyword/sort_by/sort_order；响应同时返回 items 与 data、totals（页顶汇总卡）。
    ★ asin/country 分组走**完整核算口径**（与核算快照/看板同公式）：
      净利润 = net_amazon×汇率 − 采购成本 − 头程运费 − (广告+仓储+超龄+退货+入库+移除)×汇率；
      成本按月版本回退；全年模式按月度加权。
    ★ 性能：SQL 聚合（GROUP BY + CASE WHEN），费用/广告/成本各一条聚合查询。
    """
    # ---- 时间参数兼容：year(+month) → period_month / 年区间 ----
    if not period_month and year:
        if month:
            period_month = f"{int(year)}-{int(month):02d}"
        else:
            date_from = date_from or f"{int(year)}-01-01"
            date_to = date_to or f"{int(year) + 1}-01-01"

    store_obj = _resolve_store(db, user, store)
    t = FactSalesTransaction
    conds = [t.transaction_type.in_(PROFIT_TYPES)]
    if period_month:
        s, e = _month_range(period_month)
        conds += [t.biz_date >= s, t.biz_date < e]
    elif date_from and date_to:
        conds += [t.biz_date >= datetime.fromisoformat(date_from),
                  t.biz_date < datetime.fromisoformat(date_to)]
    if country:
        conds.append(t.country == country)
    if asin:
        conds.append(t.asin == asin)
    if sku:
        conds.append(t.normalized_sku == sku)
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        conds.append(or_(t.asin.like(kw), t.normalized_sku.like(kw)))

    scoped_ids = [store_obj.id] if store_obj else get_authorized_store_ids(user, db)

    # ---------- 基础分组（sku/month：仅销售维度，无核算口径）----------
    if group_by in ('sku', 'month'):
        if group_by == 'month':
            dims = [sa_func.date_format(t.biz_date, '%Y-%m').label('period_month')]
        else:
            dims = [t.normalized_sku.label('sku'), t.country.label('country')]
        is_order = t.transaction_type == 'Order'
        sales_c = sa_func.sum(sa_case((is_order, t.product_sales), else_=0)).label('sales')
        refund_c = sa_func.sum(sa_case((~is_order, sa_func.abs(t.product_sales)), else_=0)).label('refund')
        qty_c = sa_func.sum(sa_case((is_order, t.quantity), else_=0)).label('qty')
        rqty_c = sa_func.sum(sa_case((~is_order, sa_func.abs(t.quantity)), else_=0)).label('rqty')
        orders_c = sa_func.count(sa_func.distinct(t.order_id)).label('orders')
        q = db.query(*dims, sales_c, refund_c, qty_c, rqty_c, orders_c).filter(*conds)
        q = _apply_store_scope(q, t, user, db, store_obj)
        gq = q.group_by(*dims)
        total = db.query(sa_func.count()).select_from(gq.subquery()).scalar() or 0
        rows = gq.offset((page - 1) * page_size).limit(page_size).all()
        out = []
        for row in rows:
            item = {'qty': int(row.qty or 0), 'order_count': int(row.orders or 0),
                    'sales_usd': float(row.sales or 0),
                    'refund_qty': int(row.rqty or 0), 'refund_amount': float(row.refund or 0),
                    'net_qty': int(row.qty or 0) - int(row.rqty or 0)}
            if group_by == 'month':
                item['period_month'] = row.period_month or ''
            else:
                item['sku'], item['country'] = row.sku or '', row.country or ''
            alias_row(item, 'query/sales_row')
            out.append(item)
        return inject_legacy({"total": int(total), "items": out, "totals": {}}, 'query/sales')

    # ---------- 完整核算口径（asin / country 分组，DataQuery 页）----------
    is_profit = t.transaction_type.in_(PROFIT_TYPES)
    is_order = t.transaction_type == 'Order'
    sales_net = sa_func.sum(sa_case((is_profit, t.product_sales), else_=0)).label('sales_net')
    refund_a = sa_func.sum(sa_case((t.transaction_type == 'Refund', sa_func.abs(t.product_sales)),
                                   else_=0)).label('refund')
    commission_c = sa_func.sum(sa_case((is_profit, -t.selling_fee), else_=0)).label('commission')
    fba_c = sa_func.sum(sa_case((is_profit, -t.fba_fee), else_=0)).label('fba')
    core_c = sa_func.sum(sa_case((is_profit, _net_amazon_expr()), else_=0)).label('core')
    adj_c = sa_func.sum(sa_case(
        (t.transaction_type == 'Adjustment',
         sa_case((t.has_order_id.is_(True), t.other_amount), else_=t.line_total)),
        else_=0)).label('adj')
    qty_c = sa_func.sum(sa_case((is_order, t.quantity), else_=0)).label('qty')
    rqty_c = sa_func.sum(sa_case((~is_order, sa_func.abs(t.quantity)), else_=0)).label('rqty')
    # 订单数：非空 order_id 去重（Order+Refund 同单不重复计，与核算口径一致）
    orders_c = sa_func.count(sa_func.distinct(
        sa_case((t.order_id != '', t.order_id), else_=None))).label('orders')
    sku_c = sa_func.max(t.normalized_sku).label('sku')

    q = db.query(t.store_id, t.asin.label('asin'), t.country.label('country'), sku_c,
                sales_net, refund_a, commission_c, fba_c, core_c, adj_c, qty_c, rqty_c,
                orders_c).filter(*conds)
    q = _apply_store_scope(q, t, user, db, store_obj)

    # ★ 7.3 P-02 下推：月度 asin 口径在 SQL 端 JOIN 费用/广告/成本 + 汇率 CASE 常量，
    #   计算净利排序分页（Python 仅组装当页行）；不适用/开关关闭返回 None 走原路径。
    pushed = _sales_pushdown(db, q, scoped_ids, store_obj, period_month, country,
                             sort_by, sort_order, page, page_size, group_by)
    if pushed is not None:
        return pushed

    rows = q.group_by(t.store_id, t.asin, t.country).all()
    total = len(rows)
    if not rows:
        return inject_legacy({"total": 0, "items": [], "totals": {}}, 'query/sales')

    # ---- 行级补全数据（各一条 SQL 聚合）----
    # ★ 无时间过滤（年份=全部且无 period_month）时按全量归集（对齐广告侧 ad_spend_all_by_key）
    asin_set = {r.asin for r in rows}
    if period_month:
        fee_period = period_month             # 精确月
        fs, fe = _month_range(period_month)
    elif date_from and date_to:
        fee_period = date_from[:4]            # 区间模式：月份列按年份前缀
        fs, fe = datetime.fromisoformat(date_from), datetime.fromisoformat(date_to)
    else:
        fee_period = None                     # 全量：月份列不过滤（None=调用方明确选择）
        fs, fe = datetime(2000, 1, 1), datetime(2100, 1, 1)

    ad_by_key = _ad_by_key(db, scoped_ids, country, period_month, year)
    storage_by_key = fee_by_key(db, FactFeeStorage, 'estimated_monthly_storage_fee',
                                'month_of_charge', fee_period, scoped_ids, country)
    aged_by_key = fee_by_key(db, FactFeeAgedStorage, 'amount_charged',
                             'snapshot_date', fee_period, scoped_ids, country)
    returns_by_key = fee_by_key(db, FactFeeReturn, 'sku_returns_fee',
                                'month_of_charge', fee_period, scoped_ids, country)
    removal_by_key = fee_by_key(db, FactFeeRemoval, 'removal_fee',
                                date_col=FactFeeRemoval.request_date, start=fs, end=fe,
                                stores=scoped_ids, country=country)
    inbound_by_key = fee_by_key(db, FactFeeInboundConfig, 'total_fee',
                                date_col=FactFeeInboundConfig.transaction_date, start=fs, end=fe,
                                stores=scoped_ids, country=country)

    # 成本版本：月度=当月回退；全年=按月加权（月净销量 × 各月单价）
    cost_per = _cost_versions(db, scoped_ids, asin_set)
    monthly_qty = {}
    if not period_month and date_from:
        mq = db.query(t.asin, t.country,
                      sa_func.date_format(t.biz_date, '%Y-%m').label('m'),
                      sa_func.sum(sa_case((is_order, t.quantity), else_=0)).label('sq'),
                      sa_func.sum(sa_case((~is_order, sa_func.abs(t.quantity)), else_=0)).label('rq'),
                      ).filter(*conds)
        mq = _apply_store_scope(mq, t, user, db, store_obj).group_by(
            t.asin, t.country,
            sa_func.date_format(t.biz_date, '%Y-%m')).all()
        for a, c, m, sq, rq in mq:
            monthly_qty[(a, c, m)] = int(sq or 0) - int(rq or 0)

    # 商品主档名称
    title_map = {}
    if rows:
        tp = db.query(StoreProduct.store_id, StoreProduct.asin, StoreProduct.title).filter(
            StoreProduct.store_id.in_([r.store_id for r in rows] or [-1]),
            StoreProduct.asin.in_(asin_set),
        ).all()
        title_map = {(sid, a): title or '' for sid, a, title in tp}

    rates: dict = {}
    # 单价取值月：月度=当月（回退）；全年/全部=期末月（无版本则取最新）
    unit_month = period_month or (date_to[:7] if date_to else '9999')

    def rate_of(c):
        if c not in rates:
            rates[c] = get_exchange_rate(db, c, store_obj.id if store_obj else None,
                                         period_month, ExchangeRateModel=ExchangeRate)
        return rates[c]

    def _cost_freight_for(a: str, c: str, net_qty_row: int):
        """该行的总采购成本/头程运费（月度=单版本×净销量；全年=月度加权，负月不计）"""
        per = cost_per.get(a) or {}
        if period_month or not monthly_qty:
            cost, freight = _pick_cost(per, unit_month)
            qty = net_qty_row if net_qty_row > 0 else 0
            return (cost or Decimal(0)) * qty, (freight or Decimal(0)) * qty
        tc = tf = Decimal(0)
        touched = False
        for (aa, cc, m), nq in monthly_qty.items():
            if aa != a or cc != c:
                continue
            touched = True
            cost, freight = _pick_cost(per, m)
            nq = nq if nq > 0 else 0
            tc += (cost or Decimal(0)) * nq
            tf += (freight or Decimal(0)) * nq
        if not touched:
            cost, freight = _pick_cost(per, unit_month)
            nq = net_qty_row if net_qty_row > 0 else 0
            tc, tf = (cost or Decimal(0)) * nq, (freight or Decimal(0)) * nq
        return tc, tf

    # ---- 逐行组装（完整口径利润，公式与核算快照一致）----
    full = []
    for r in rows:
        c = r.country or ''
        key = (r.asin or '', c)
        er = rate_of(c)
        net_qty = int(r.qty or 0) - int(r.rqty or 0)
        sales = _D(r.sales_net)
        net_amazon = _D(r.core) + _D(r.adj)
        ad = ad_by_key.get(key, Decimal(0))
        st = storage_by_key.get(key, Decimal(0))
        aged = aged_by_key.get(key, Decimal(0))
        ret = returns_by_key.get(key, Decimal(0))
        rem = removal_by_key.get(key, Decimal(0))
        inb = inbound_by_key.get(key, Decimal(0))
        cost, freight = _cost_freight_for(r.asin or '', c, net_qty)
        net_profit = (net_amazon * er - cost - freight
                      - (ad + st + aged + ret + inb + rem) * er).quantize(Decimal('0.01'))
        sales_rmb = (sales * er).quantize(Decimal('0.01'))
        qty_i, rqty_i = int(r.qty or 0), int(r.rqty or 0)
        per_cost, per_freight = _pick_cost(cost_per.get(r.asin) or {}, unit_month)
        row = {
            'asin': r.asin or '', 'sku': r.sku or '', 'country': c,
            'product_name': title_map.get((r.store_id, r.asin), ''),
            'color': '',
            'qty': qty_i, 'net_qty': net_qty,
            'refund_qty': rqty_i, 'refund_amount': float(_D(r.refund)),
            'order_count': int(r.orders or 0),
            'sales_usd': float(sales),
            'product_sales_rmb': float(sales_rmb),
            'commission_usd': float(_D(r.commission)), 'fba_fee_usd': float(_D(r.fba)),
            'ad_spend_usd': float(ad), 'storage_fee_usd': float(st),
            'aged_storage_fee_usd': float(aged), 'returns_fee_usd': float(ret),
            'inbound_fee_usd': float(inb), 'removal_fee_usd': float(rem),
            'product_cost_rmb': float(cost), 'freight_cost_rmb': float(freight),
            'cost_rmb': float(per_cost) if per_cost is not None else None,
            'freight_per_unit': float(per_freight) if per_freight is not None else None,
            'net_amazon_usd': float(net_amazon), 'net_profit_rmb': float(net_profit),
            'net_profit_rate': round(float(net_profit / sales_rmb * 100), 2) if sales_rmb else 0.0,
            'ad_spend_pct': round(float(ad / sales * 100), 2) if sales else 0.0,
            'storage_fee_pct': round(float(st / sales * 100), 2) if sales else 0.0,
            'return_rate': round(float(rqty_i / qty_i * 100), 2) if qty_i else 0.0,
        }

        full.append(row)

    # ---- 输出行（country=按国家合并跨店；asin=原样）+ Python 排序分页 ----
    if group_by == 'country':
        by_c: dict = {}
        for it in full:
            by_c.setdefault(it['country'], []).append(it)
        NUM_KEYS = ('qty', 'net_qty', 'refund_qty', 'refund_amount', 'sales_usd',
                    'product_sales_rmb', 'commission_usd', 'fba_fee_usd',
                    'ad_spend_usd', 'storage_fee_usd', 'aged_storage_fee_usd', 'returns_fee_usd',
                    'inbound_fee_usd', 'removal_fee_usd', 'product_cost_rmb', 'freight_cost_rmb',
                    'net_amazon_usd', 'net_profit_rmb')
        out = []
        for c, items in by_c.items():
            agg = {k: sum(_D(it.get(k) or 0) for it in items) for k in NUM_KEYS}
            agg['order_count'] = sum(int(it.get('order_count') or 0) for it in items)
            sales_usd = _D(agg['sales_usd'])
            row = {'country': c,
                   **{k: float(v) for k, v in agg.items()}}
            row['net_profit_rate'] = round(float(_D(agg['net_profit_rmb']) / _D(agg['product_sales_rmb']) * 100), 2) if agg['product_sales_rmb'] else 0.0
            row['ad_spend_pct'] = round(float(_D(agg['ad_spend_usd']) / sales_usd * 100), 2) if sales_usd else 0.0
            row['storage_fee_pct'] = round(float(_D(agg['storage_fee_usd']) / sales_usd * 100), 2) if sales_usd else 0.0
            row['return_rate'] = round(float(agg['refund_qty'] / agg['qty'] * 100), 2) if agg['qty'] else 0.0
            nq = agg['net_qty']
            row['cost_rmb'] = float(_D(agg['product_cost_rmb']) / nq) if nq > 0 else None
            row['freight_per_unit'] = float(_D(agg['freight_cost_rmb']) / nq) if nq > 0 else None
            alias_row(row, 'query/sales_row')
            out.append(row)
        _py_sort(out, sort_by or 'product_sales_usd', sort_order != 'asc')
        return inject_legacy({"total": len(out), "items": out, "totals": _totals_of(out)}, 'query/sales')

    # ---- 合并同 (asin, sku) 的跨国/跨店行 → 一个产品一行 ----
    # ★ 前端产品明细表 rowKey=`${asin}-${sku}` 且无国家列：必须 (asin, sku) 唯一，
    #   否则 React duplicate key 直接白屏。逐国折算已在上面完成，合并只累加
    #   RMB/原币结果，不损失汇率精度；比率类字段按合并后总量重算。
    NUM_KEYS = ('qty', 'net_qty', 'refund_qty', 'refund_amount', 'sales_usd',
                'product_sales_rmb', 'commission_usd', 'fba_fee_usd',
                'ad_spend_usd', 'storage_fee_usd', 'aged_storage_fee_usd', 'returns_fee_usd',
                'inbound_fee_usd', 'removal_fee_usd', 'product_cost_rmb', 'freight_cost_rmb',
                'net_amazon_usd', 'net_profit_rmb')
    by_p: dict = {}
    for it in full:
        k = (it['asin'], it['sku'])
        m = by_p.get(k)
        if m is None:
            m = dict(it)
            m['_cs'] = {it['country']}
            by_p[k] = m
            continue
        for f in NUM_KEYS:
            m[f] = _D(m[f]) + _D(it[f])
        m['order_count'] = int(m.get('order_count') or 0) + int(it.get('order_count') or 0)
        if not m.get('product_name') and it.get('product_name'):
            m['product_name'] = it['product_name']
        if m.get('cost_rmb') is None and it.get('cost_rmb') is not None:
            m['cost_rmb'] = it['cost_rmb']
        if m.get('freight_per_unit') is None and it.get('freight_per_unit') is not None:
            m['freight_per_unit'] = it['freight_per_unit']
        m['_cs'].add(it['country'])
    out_rows = []
    for m in by_p.values():
        cs = sorted(m.pop('_cs'))
        c0 = cs[0] if len(cs) == 1 else ''
        m['country'] = c0
        sales = _D(m['sales_usd'])
        sales_rmb = _D(m['product_sales_rmb'])
        m['net_profit_rate'] = round(float(_D(m['net_profit_rmb']) / sales_rmb * 100), 2) if sales_rmb else 0.0
        m['ad_spend_pct'] = round(float(_D(m['ad_spend_usd']) / sales * 100), 2) if sales else 0.0
        m['storage_fee_pct'] = round(float(_D(m['storage_fee_usd']) / sales * 100), 2) if sales else 0.0
        m['return_rate'] = round(float(m['refund_qty'] / m['qty'] * 100), 2) if m['qty'] else 0.0
        alias_row(m, 'query/sales_row')
        out_rows.append(m)
    total = len(out_rows)
    _py_sort(out_rows, sort_by or 'sales_usd', sort_order != 'asc')
    page_rows = out_rows[(page - 1) * page_size: (page - 1) * page_size + page_size]
    return inject_legacy({"total": total, "items": page_rows, "totals": _totals_of(out_rows)}, 'query/sales')


def _py_sort(items, field, desc):
    """Python 层排序：None 排末尾；数值按数值、字符串按字典序"""
    def key(it):
        v = it.get(field)
        if v is None:
            return (1, 0, '')
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return (0, v, '')
        return (0, 0, str(v))
    items.sort(key=key, reverse=desc)


def _totals_of(rows):
    """页顶汇总卡（DataQuery 消费）：net_qty/销售额/佣金/FBA/广告/净利润(RMB)"""
    tot = {k: Decimal(0) for k in ('net_qty', 'product_sales_usd', 'product_sales_rmb',
                                   'commission_usd', 'fba_fee_usd', 'ad_spend_usd', 'net_profit_rmb')}
    for it in rows:
        tot['net_qty'] += _D(it.get('net_qty'))
        tot['product_sales_usd'] += _D(it.get('product_sales_usd'))
        tot['product_sales_rmb'] += _D(it.get('product_sales_rmb'))
        tot['commission_usd'] += _D(it.get('commission_usd'))
        tot['fba_fee_usd'] += _D(it.get('fba_fee_usd'))
        tot['ad_spend_usd'] += _D(it.get('ad_spend_usd'))
        tot['net_profit_rmb'] += _D(it.get('net_profit_rmb'))
    out = {k: round(float(v), 2) for k, v in tot.items()}
    out['net_qty'] = int(tot['net_qty'])
    return out


@router.get("/products")
def list_products(
    store: str | None = None,
    status: str | None = Query(None, description="normal/pending/abnormal"),
    keyword: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user=Depends(require_module("query")),
):
    """商品主档 + SKU 映射（三元组）

    ★ P-03 修复：原实现逐商品查映射 + 查店铺（200 商品 = 400+ 次查询）；
      现三步批量 — ① 主档一页一次；② 映射按 (store_id IN, asin IN) 一次取回分组；
      ③ 本页店铺信息一次查询。查询次数恒定 3 次，与商品数无关。
    """
    store_obj = _resolve_store(db, user, store)
    q = db.query(StoreProduct)
    q = _apply_store_scope(q, StoreProduct, user, db, store_obj)
    if status:
        q = q.filter(StoreProduct.status == status)
    if keyword:
        q = q.filter(or_(StoreProduct.asin.like(f"%{keyword}%"),
                         StoreProduct.title.like(f"%{keyword}%")))
    products = q.limit(limit).all()
    mapping_by_key: dict = {}
    store_codes: dict = {}
    if products:
        store_ids = {p.store_id for p in products}
        asins = {p.asin for p in products if p.asin}
        # ② SKU 映射批量：一次取回本页 (store_id, asin) 全部映射（按 id 保插入序）
        if asins:
            mappings = db.query(StoreSkuMapping).filter(
                StoreSkuMapping.store_id.in_(store_ids or [-1]),
                StoreSkuMapping.asin.in_(asins),
            ).order_by(StoreSkuMapping.id).all()
            for m in mappings:
                mapping_by_key.setdefault((m.store_id, m.asin), []).append(m)
        # ③ 店铺信息批量：本页涉及的 store 一次取回
        store_rows = db.query(Store).filter(Store.id.in_(store_ids or [-1])).all()
        store_codes = {s.id: s.store_code for s in store_rows}
    out = []
    for p in products:
        mappings = mapping_by_key.get((p.store_id, p.asin), [])
        store_code = store_codes.get(p.store_id, '')
        item = {
            'id': p.id, 'store_code': store_code,
            'asin': p.asin, 'title': p.title, 'status': p.status,
            'skus': [{'sku': m.sku, 'status': m.status, 'note': m.note} for m in mappings],
        }
        alias_row(item, 'query/product_row')
        out.append(item)
    return inject_legacy({"total": len(out), "items": out}, 'query/products')


@router.get("/ad")
def query_ad(
    store: str | None = None,
    country: str | None = None,
    period_month: str | None = None,
    year: int | None = Query(None, description="年份（Deprecated，请用 period_month）", deprecated=True),
    month: int | None = Query(None, description="月份（Deprecated，请用 period_month）", deprecated=True),
    db: Session = Depends(get_db),
    user=Depends(require_module("query")),
):
    """广告花费与效果指标

    ★ 同时返回新字段（total_spend/…) 与旧前端 Advertising 页所需别名
      （total_ad_spend / avg_acos / avg_roas）。
    ★ 归月 SQL 化（P-02/P-07）：三级归月（biz_date → period_month 列 → 批次）
      在 SQL 端完成，不再全表加载 Python 过滤；口径与 common/admonth 唯一实现一致。
    """
    # 兼容旧参数 year/month
    if not period_month and year and month:
        period_month = f"{year}-{int(month):02d}"
    store_obj = _resolve_store(db, user, store)
    batch = aliased(ImportBatch)
    _, month_expr = ad_month_expr(batch)
    q = db.query(FactAdDaily).outerjoin(batch, FactAdDaily.batch_id == batch.id)
    q = _apply_store_scope(q, FactAdDaily, user, db, store_obj)
    if country:
        q = q.filter(FactAdDaily.country == country)
    if period_month:
        q = q.filter(month_expr == period_month)
    elif year:
        q = q.filter(month_expr.like(f"{int(year)}-%"))
    rows = q.all()
    total_spend = sum((Decimal(str(r.spend or 0)) for r in rows), Decimal(0))
    total_sales = sum((Decimal(str(r.sales or 0)) for r in rows), Decimal(0))
    acos = float((total_spend / total_sales).quantize(Decimal('0.0001'))) if total_sales else 0
    roas = float((total_sales / total_spend).quantize(Decimal('0.0001'))) if total_spend else 0
    items = [{
        'asin': r.asin or '', 'sku': r.sku or '', 'country': r.country or '',
        'sales': float(r.sales or 0), 'spend': float(r.spend or 0),
        'impressions': r.impressions or 0, 'clicks': r.clicks or 0,
        'orders': r.orders or 0, 'acos': float(r.acos or 0),
        'roas': float(r.roas or 0), 'ctr': float(r.ctr or 0), 'cpc': float(r.cpc or 0),
    } for r in rows]
    return inject_legacy({
        "total_spend": float(total_spend),
        "total_sales": float(total_sales),
        "acos": acos,
        "roas": roas,
        "impressions": sum(r.impressions or 0 for r in rows),
        "clicks": sum(r.clicks or 0 for r in rows),
        "orders": sum(r.orders or 0 for r in rows),
        "rows": len(rows),
        "items": items,
    }, 'query/ad')


@router.get("/fees")
def query_fees(
    store: str | None = None,
    country: str | None = None,
    period_month: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_module("query")),
):
    """各项费用汇总（仓储/超龄/退货/入库/移除）"""
    store_obj = _resolve_store(db, user, store)
    # 数据权限：指定店铺取该店；否则取授权店铺集合（非管理员受限）
    scoped_ids = [store_obj.id] if store_obj else get_authorized_store_ids(user, db)
    pm = period_month or datetime.now().strftime('%Y-%m')
    try:
        start, end = month_bounds(pm)
    except PeriodFormatError:
        raise HTTPException(status_code=400, detail="period_month 格式应为 YYYY-MM")

    def q_or_all(model, *filters):
        """按数据权限过滤店铺范围后查询费用表（行级拉取，供合计 + counts）"""
        query = db.query(model)
        if scoped_ids is not None:
            query = query.filter(model.store_id.in_(scoped_ids or [-1]))
        if country and hasattr(model, 'country'):
            query = query.filter(model.country == country)
        for f in filters:
            query = query.filter(f)
        return query.all()

    # 月份过滤走 common/fees.month_filter_expr（前缀→组10 清洗后等值，全系统唯一切换点）：
    #   B-05 修复：历史落库值带时间戳 '2026-06-01 00:00:00'，等值匹配恒 0；
    #   B-02 修复：超龄费按 snapshot_date 过滤（此前无任何时间过滤 → 历史累计）。
    storage = q_or_all(FactFeeStorage, month_filter_expr(FactFeeStorage, 'month_of_charge', pm))
    returns = q_or_all(FactFeeReturn, month_filter_expr(FactFeeReturn, 'month_of_charge', pm))
    removal = q_or_all(FactFeeRemoval, FactFeeRemoval.request_date >= start, FactFeeRemoval.request_date < end)
    inbound = q_or_all(FactFeeInboundConfig, FactFeeInboundConfig.transaction_date >= start,
                       FactFeeInboundConfig.transaction_date < end)
    aged = q_or_all(FactFeeAgedStorage, month_filter_expr(FactFeeAgedStorage, 'snapshot_date', pm))

    def s(rows, attr):
        """费用表指定列的 Decimal 合计"""
        return float(sum((Decimal(str(getattr(r, attr) or 0)) for r in rows), Decimal(0)))

    return {
        'period_month': pm,
        'storage_fee': s(storage, 'estimated_monthly_storage_fee'),
        'aged_storage_fee': s(aged, 'amount_charged'),
        'returns_fee': s(returns, 'sku_returns_fee'),
        'removal_fee': s(removal, 'removal_fee'),
        'inbound_fee': s(inbound, 'total_fee'),
        'counts': {'storage': len(storage), 'returns': len(returns), 'removal': len(removal),
                   'inbound': len(inbound), 'aged': len(aged)},
    }
