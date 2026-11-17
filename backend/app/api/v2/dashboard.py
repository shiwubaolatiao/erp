"""看板 — 汇总指标 / 趋势 / 店铺对比 / 费用构成 / 退货排名 / 资金流

接口前缀: /api/v2/dashboard

★ 参数兼容（重要，维护必读）
  现有前端页面按旧后端契约调用，发的是 `year=2026&month=6`；
  新后端内部统一用 `period_month='2026-06'`。为不改动页面逻辑，
  本模块所有接口**同时接受两种参数**：
      · period_month='2026-06'   （新，精确到月）
      · year=2026&month=6        （旧；month 省略或为 0 表示整年）
  解析统一走 `_resolve_period()`。

★ 金额口径（RMB 换算必须逐国算，否则多国汇总会错）
  报表原币金额（多为 USD/GBP/EUR）需乘**该笔所在国家的汇率**才是 RMB。
  多国汇总时绝不能用一个平均汇率粗算 —— 因此所有 RMB 指标都在本模块
  按国家分别聚合后再折算（`_country_sales_rmb()` 等）。

可下钻：net_profit 来自核算快照，快照可经「数据溯源」页下钻到原始行。
"""
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func as sa_func, and_ as sa_and, or_ as sa_or
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import get_user_permissions  # noqa: F401  (权限可扩展点)
from app.core.security import get_current_user, require_module, filter_store_by_permission, get_authorized_stores
from app.models import (
    FactSalesTransaction, FactAdDaily, Store,
    FactFeeStorage, FactFeeReturn, FactFeeRemoval, FactFeeInboundConfig, FactFeeAgedStorage,
    DwsProfitSnapshot, DwsProfitSnapshotItem, CostMissingAlert, SysUser,
)
from app.services.common.snapshots import latest_snapshots
from app.services.common.period import resolve_period, month_bounds
from app.services.common.fees import fee_by_country
from app.services.common.rates import RateResolver, resolve_for_stores
from app.services.common.contract_alias import inject_legacy
from app.core.cache import cached_aggregate

router = APIRouter()

# 参与利润核算的交易类型（与核算层一致）
PROFIT_TYPES = ('Order', 'Refund')


# ============================================================
# 通用工具
# ============================================================


def _d(v):
    """None 安全的 Decimal 转换"""
    return Decimal(str(v)) if v is not None else Decimal(0)


def _resolve_period(period_month: str | None, year: int | None, month: int | None):
    """统一解析查询期间 — 唯一实现在 common/period.resolve_period（本函数仅解包适配）

    返回 (start, end, period_month|None, mode)
        mode='month'  指定了具体月份（start/end 为该月区间，period_month 有值）
        mode='year'   指定了整年  （start/end 为该年区间，period_month=None）
    默认（什么都没传）取当前自然月。
    """
    spec = resolve_period(period_month=period_month, year=year, month=month)
    return spec.start, spec.end, spec.period_month, spec.mode


def _country_rates(db: Session, store_id: int | None, period_month: str | None) -> dict:
    """取 {国家代码: 汇率(RMB)} —— 逐国折算 RMB 的基础

    P-04 修复：原实现逐国调 engine.get_exchange_rate（每国最多 3 次往返）；
    现改 RateResolver 批量解析 — 1 次 distinct（国家集合口径不变）+ 1 次批量取行，
    四级回退结果与 engine 逐国一致（common/rates 已对齐验证）。
    """
    pm = period_month
    if not pm:
        pm = datetime.now().strftime('%Y-%m')
    countries = [r[0] for r in db.query(FactSalesTransaction.country).distinct().all() if r[0]]
    if not countries:
        return {}
    return RateResolver(db, store_id=store_id, period_month=pm).resolve(countries)


def _scope(q, model, user, db, store_obj):
    """数据权限：指定店铺按店铺过滤；未指定时非管理员限定在授权店铺内"""
    if store_obj:
        return q.filter(model.store_id == store_obj.id)
    from app.core.security import get_authorized_store_ids
    ids = get_authorized_store_ids(user, db)
    if ids is not None:
        return q.filter(model.store_id.in_(ids or [-1]))
    return q


def _ad_spend_by_country(db: Session, store_id: int | None, start: datetime, end: datetime,
                         period_month: str | None) -> dict:
    """按国家归集广告花费（原币）— SQL 聚合（GROUP BY 国家）

    ★ 亚马逊广告报表没有日期列，归月用 `fact_ad_daily.period_month`
      （导入时写入的报表归属月份，历史数据已全量回填）；
      有 biz_date 的行按日期区间过滤。指定月份时只取该月；整年取该年内月份。
    """
    want = period_month                       # 'YYYY-MM' 或 None（整年）
    year_prefix = f"{start.year}-"
    month_cond = (FactAdDaily.period_month == want) if want \
        else FactAdDaily.period_month.like(f"{year_prefix}%")
    q = db.query(
        FactAdDaily.country.label('country'),
        sa_func.sum(FactAdDaily.spend).label('spend'),
    ).filter(sa_or(
        # 有日期行：按日期区间
        sa_and(FactAdDaily.biz_date.isnot(None),
               FactAdDaily.biz_date >= start, FactAdDaily.biz_date < end),
        # 无日期行：按归月列
        sa_and(FactAdDaily.biz_date.is_(None), month_cond),
    ))
    if store_id:
        q = q.filter(FactAdDaily.store_id == store_id)
    out = {}
    for c, spend in q.group_by('country').all():
        out[c or ''] = _d(spend or 0)
    return out


# （费用按国家归集已收敛）唯一实现在 common/fees.fee_by_country ——
#   month_col 前缀/等值匹配（组10 清洗后全系统统一切换）、date_col 半开区间、
#   两者都未传直接 raise（B-02 防线）。


# ============================================================
# 接口
# ============================================================

def _latest_snapshots(db: Session, store_obj, country: str | None,
                      pm: str | None, year: int | None):
    """取指定口径下的**最新版本快照**列表 — 唯一实现在 common/snapshots.latest_snapshots

    ★ 关键口径（否则数字会翻倍）：快照有两种粒度并存
        · 店铺级：country = ''（该店铺全部站点汇总）
        · 站点级：country = 'UK' 等
      汇总展示时必须二选一：
        · 未指定站点 → **只取店铺级**（country=''）
        · 指定了站点 → 只取该站点的
      否则会把"店铺汇总"和"站点明细"加在一起，利润直接虚高。
    """
    return latest_snapshots(
        db,
        [store_obj.id] if store_obj else None,
        country or '',
        pm or (str(year) if year else None))



@router.get("/summary")
def summary(
    store: str | None = None,
    country: str | None = None,
    period_month: str | None = Query(None, description="新参数：YYYY-MM"),
    year: int | None = Query(None, description="旧参数：年份（与 month 搭配）", deprecated=True),
    month: int | None = Query(None, description="旧参数：月份，省略/0=整年", deprecated=True),
    db: Session = Depends(get_db),
    user: SysUser = Depends(require_module("dashboard")),
):
    """核心指标：销售额 / 销量 / 订单 / 广告 / 费用 / 利润 + 成本缺失告警数

    同时返回新字段（*_usd 原币、*_rmb 折算 RMB）与旧前端所需的 current/previous/change_percent，
    以便现有看板页面无需改动即可正确渲染。
    ★ 请求级缓存（spec 5.3.1 R6）：键含用户与数据版本号（导入/核算完成天然失效），TTL 兜底。
    """
    params = {'store': store, 'country': country, 'period_month': period_month,
              'year': year, 'month': month}
    return cached_aggregate(
        'dashboard/summary', params, user.id, db,
        lambda: _summary_impl(db, user, store, country, period_month, year, month))


def _summary_impl(db, user, store, country, period_month, year, month):
    start, end, pm, mode = _resolve_period(period_month, year, month)
    store_obj = filter_store_by_permission(user, store, db) if store else None
    rates = _country_rates(db, store_obj.id if store_obj else None, pm)

    # ---- 销售聚合（按国家，便于逐国折算 RMB）----
    # ★ SQL 聚合（GROUP BY 国家+类型），不把明细行拉回内存——
    #   全年模式约 40 万行，ORM 全量 hydration 会超时（与 /trend 的教训一致）。
    base_filters = [
        FactSalesTransaction.biz_date >= start,
        FactSalesTransaction.biz_date < end,
        FactSalesTransaction.transaction_type.in_(PROFIT_TYPES),
    ]
    sq = db.query(
        FactSalesTransaction.country.label('country'),
        FactSalesTransaction.transaction_type.label('ttype'),
        sa_func.sum(FactSalesTransaction.product_sales).label('sales'),
        sa_func.sum(FactSalesTransaction.quantity).label('qty'),
    ).filter(*base_filters)
    sq = _scope(sq, FactSalesTransaction, user, db, store_obj)
    if country:
        sq = sq.filter(FactSalesTransaction.country == country)

    sales_by_c = {}
    qty = refund_qty = 0
    for c, ttype, sales, q2 in sq.group_by('country', 'ttype').all():
        c = c or ''
        d = sales_by_c.setdefault(c, Decimal(0))
        val = _d(sales or 0)
        if ttype == 'Order':
            d += val
            qty += int(q2 or 0)
        else:
            d -= abs(val)
            refund_qty += abs(int(q2 or 0))

    # 订单数：SQL 端 DISTINCT 计数（与原 order_ids 集合语义一致，Order/Refund 均计）
    oq = db.query(
        sa_func.count(sa_func.distinct(FactSalesTransaction.order_id))
    ).filter(*base_filters, FactSalesTransaction.order_id.isnot(None))
    oq = _scope(oq, FactSalesTransaction, user, db, store_obj)
    if country:
        oq = oq.filter(FactSalesTransaction.country == country)
    order_count = int(oq.scalar() or 0)

    sales_usd = sum(sales_by_c.values(), Decimal(0))
    sales_rmb = sum((v * rates.get(c, Decimal('6.8')) for c, v in sales_by_c.items()), Decimal(0))

    # ---- 广告 / 费用（原币）→ RMB ----
    ad_by_c = _ad_spend_by_country(db, store_obj.id if store_obj else None, start, end, pm)
    if country:
        ad_by_c = {c: v for c, v in ad_by_c.items() if c == country}
    ad_usd = sum(ad_by_c.values(), Decimal(0))
    ad_rmb = sum((v * rates.get(c, Decimal('6.8')) for c, v in ad_by_c.items()), Decimal(0))

    def fee_rmb(model, attr, month_col=None, date_col=None):
        """费用归集 → (原币合计, RMB 合计) — 唯一实现 common/fees.fee_by_country

        month_col 项按 period 前缀/等值匹配（精确月 / 整年）；
        date_col 项（移除/入库）按 [start, end) 半开区间；
        country 下推 SQL 过滤（common 内部），逐国折 RMB 在调用侧完成。
        """
        m = fee_by_country(
            db, model, attr,
            month_col=month_col, period=(pm or str(start.year)) if month_col else None,
            stores=store_obj.id if store_obj else None, country=country,
            date_col=date_col, start=start, end=end)
        return (sum(m.values(), Decimal(0)),
                sum((v * rates.get(c, Decimal('6.8')) for c, v in m.items()), Decimal(0)))

    storage_usd, storage_rmb = fee_rmb(FactFeeStorage, 'estimated_monthly_storage_fee', 'month_of_charge')
    # B-02 修复：超龄按 snapshot_date 前缀过滤（此前无任何时间过滤 → 返回历史累计）；
    # 移除/入库按各自日期列区间过滤（费用表无 biz_date，此前同样漏过滤）
    aged_usd, aged_rmb = fee_rmb(FactFeeAgedStorage, 'amount_charged', 'snapshot_date')
    returns_usd, returns_rmb = fee_rmb(FactFeeReturn, 'sku_returns_fee', 'month_of_charge')
    removal_usd, removal_rmb = fee_rmb(FactFeeRemoval, 'removal_fee',
                                       date_col=FactFeeRemoval.request_date)
    inbound_usd, inbound_rmb = fee_rmb(FactFeeInboundConfig, 'total_fee',
                                       date_col=FactFeeInboundConfig.transaction_date)

    # ---- 快照（利润口径唯一来源；按粒度去重，避免店铺级与站点级重复相加）----
    snaps = _latest_snapshots(db, store_obj, country, pm, start.year)
    net_profit = sum((_d(s.net_profit_rmb) for s in snaps), Decimal(0))
    snap_sales_rmb = sum((_d(s.product_sales_rmb) for s in snaps), Decimal(0))
    # 有快照时以快照的销售额为准（快照是已核算的权威口径）
    if snap_sales_rmb > 0:
        sales_rmb = snap_sales_rmb
    profit_rate = (net_profit / sales_rmb) if sales_rmb else Decimal(0)
    cost_complete = all(s.cost_complete for s in snaps) if snaps else None

    alert_q = db.query(sa_func.count(CostMissingAlert.id)).filter(CostMissingAlert.status == 'pending')
    if store_obj:
        alert_q = alert_q.filter(CostMissingAlert.store_id == store_obj.id)
    if country:
        alert_q = alert_q.filter(CostMissingAlert.country == country)
    pending_alerts = alert_q.scalar() or 0

    # ---- 环比（previous/change_percent 规范命名；旧 total_* 视图由别名层生成）----
    cur = {
        'net_profit_rmb': float(net_profit),
        'sales_rmb': float(sales_rmb),
        'order_count': order_count,
        'net_profit_rate': float(profit_rate),
        'refund_qty': refund_qty,
    }
    previous, change = _previous_and_change(
        db, user, store_obj, country, start, end, pm, mode, cur, rates)

    return inject_legacy({
        'period_month': pm, 'mode': mode,
        'start': start.isoformat(), 'end': end.isoformat(),
        'sales_usd': float(sales_usd), 'sales_rmb': float(sales_rmb),
        'qty': qty, 'refund_qty': refund_qty, 'net_qty': qty - refund_qty,
        'order_count': order_count,
        'ad_spend': float(ad_usd), 'ad_spend_rmb': float(ad_rmb),
        'ad_sales': 0.0, 'acos': 0.0, 'roas': 0.0,
        'storage_fee': float(storage_usd), 'storage_fee_rmb': float(storage_rmb),
        'aged_storage_fee': float(aged_usd),
        'returns_fee': float(returns_usd), 'removal_fee': float(removal_usd),
        'inbound_fee': float(inbound_usd),
        'net_profit_rmb': float(net_profit),
        'net_profit_rate': float(profit_rate),
        'cost_complete': cost_complete,
        'pending_cost_alerts': pending_alerts,
        'previous': previous,
        'change_percent': change,
    }, 'dashboard/summary')


def _previous_and_change(db, user, store_obj, country, start, end, pm, mode, current, rates):
    """计算对比期与环比（旧前端展示"环比"用）

    对比期口径与旧系统一致：指定月份 → 上月；整年 → 上一年。
    """
    if pm:
        y, m = int(pm[:4]), int(pm[5:7])
        prev_pm = f"{y-1}-12" if m == 1 else f"{y}-{m-1:02d}"
        ps, pe = month_bounds(prev_pm)
    else:
        prev_pm = None
        ps, pe = datetime(start.year - 1, 1, 1), datetime(start.year, 1, 1)

    q = db.query(
        FactSalesTransaction.country.label('country'),
        FactSalesTransaction.transaction_type.label('ttype'),
        sa_func.sum(FactSalesTransaction.product_sales).label('sales'),
        sa_func.sum(FactSalesTransaction.quantity).label('qty'),
    ).filter(
        FactSalesTransaction.biz_date >= ps, FactSalesTransaction.biz_date < pe,
        FactSalesTransaction.transaction_type.in_(PROFIT_TYPES))
    q = _scope(q, FactSalesTransaction, user, db, store_obj)
    if country:
        q = q.filter(FactSalesTransaction.country == country)
    sales_rmb = Decimal(0)
    refund_qty = 0
    for c, ttype, sales, q2 in q.group_by('country', 'ttype').all():
        rate = rates.get(c or '', Decimal('6.8'))
        val = _d(sales or 0)
        if ttype == 'Order':
            sales_rmb += val * rate
        else:
            sales_rmb -= abs(val) * rate
            refund_qty += abs(int(q2 or 0))

    oq = db.query(
        sa_func.count(sa_func.distinct(FactSalesTransaction.order_id))
    ).filter(
        FactSalesTransaction.biz_date >= ps, FactSalesTransaction.biz_date < pe,
        FactSalesTransaction.transaction_type.in_(PROFIT_TYPES),
        FactSalesTransaction.order_id.isnot(None))
    oq = _scope(oq, FactSalesTransaction, user, db, store_obj)
    if country:
        oq = oq.filter(FactSalesTransaction.country == country)
    order_count_prev = int(oq.scalar() or 0)

    sq = _latest_snapshots(db, store_obj, country, prev_pm, ps.year)  # 粒度去重 + 最新版本
    profit = sum((_d(s.net_profit_rmb) for s in sq), Decimal(0))

    previous = {
        'net_profit_rmb': float(profit),
        'sales_rmb': float(sales_rmb),
        'order_count': order_count_prev,
        'net_profit_rate': float(profit / sales_rmb) if sales_rmb else 0.0,
        'refund_qty': refund_qty,
    }
    change = {}
    for k in ('net_profit_rmb', 'sales_rmb', 'order_count', 'net_profit_rate', 'refund_qty'):
        cur, prev = current.get(k, 0), previous.get(k, 0)
        change[k] = round((cur - prev) / abs(prev) * 100, 2) if prev else None
    return previous, change


@router.get("/trend")
def trend(store: str | None = None, country: str | None = None,
          dimension: str = Query('monthly', description="monthly=按月 / year=按年（旧前端参数）"),
          months: int = 24,
          period_month: str | None = None,
          year: int | None = Query(None, description="年份（Deprecated，请用 period_month）", deprecated=True), month: int | None = Query(None, description="月份（Deprecated，请用 period_month）", deprecated=True),
          db: Session = Depends(get_db), user: SysUser = Depends(require_module("dashboard"))):
    """趋势（销售额/销量/订单/利润/广告/仓储）

    返回 `items`（新）与 `data`（旧前端兼容，字段为 label/net_profit/sales/orders/ad_spend/storage_fee）。

    ★ 性能要点：销售数据用 **SQL 聚合**（按月+国家 GROUP BY）而不是把 ORM 对象全部取回内存
      —— 全量 18 万行 hydration 会超过 30 秒导致前端超时。
    ★ 请求级缓存（spec 5.3.1 R6）。
    """
    params = {'store': store, 'country': country, 'dimension': dimension, 'months': months,
              'period_month': period_month, 'year': year, 'month': month}
    return cached_aggregate(
        'dashboard/trend', params, user.id, db,
        lambda: _trend_impl(db, user, store, country, dimension, months,
                            period_month, year, month))


def _trend_impl(db, user, store, country, dimension, months, period_month, year, month):
    store_obj = filter_store_by_permission(user, store, db) if store else None
    scope_ids = None
    if store_obj:
        scope_ids = [store_obj.id]
    else:
        from app.core.security import get_authorized_store_ids
        scope_ids = get_authorized_store_ids(user, db)

    if dimension == 'year':
        period_expr = sa_func.date_format(FactSalesTransaction.biz_date, '%Y')
    else:
        period_expr = sa_func.date_format(FactSalesTransaction.biz_date, '%Y-%m')

    # ---- 销售：按月/国家 SQL 聚合（原币销售额 + 数量）----
    sq = db.query(
        period_expr.label('period'),
        FactSalesTransaction.country.label('country'),
        FactSalesTransaction.transaction_type.label('ttype'),
        sa_func.sum(FactSalesTransaction.product_sales).label('sales'),
        sa_func.sum(FactSalesTransaction.quantity).label('qty'),
    ).filter(
        FactSalesTransaction.transaction_type.in_(PROFIT_TYPES),
        FactSalesTransaction.biz_date.isnot(None),
    )
    if scope_ids is not None:
        sq = sq.filter(FactSalesTransaction.store_id.in_(scope_ids or [-1]))
    if country:
        sq = sq.filter(FactSalesTransaction.country == country)
    sq = sq.group_by('period', 'country', 'ttype')

    sales_by_cm = {}      # (国家, 期间) -> 原币销售额（Refund 取负）
    period_qty = {}       # 期间 -> 数量（仅 Order）
    period_sales = {}     # 期间 -> 原币销售额合计（用于确定要展示哪些期间）
    for period, c, ttype, sales, qty in sq.all():
        if not period:
            continue
        val = _d(sales or 0)
        signed = val if ttype == 'Order' else -abs(val)
        key = (c or '', period)
        sales_by_cm[key] = sales_by_cm.get(key, Decimal(0)) + signed
        if ttype == 'Order':
            period_qty[period] = period_qty.get(period, 0) + int(qty or 0)
        period_sales[period] = period_sales.get(period, Decimal(0)) + signed

    # ---- 订单数：按期间去重计数（SQL 端 DISTINCT，避免取全量订单号）----
    oq = db.query(
        period_expr.label('period'),
        sa_func.count(sa_func.distinct(FactSalesTransaction.order_id)).label('orders'),
    ).filter(
        FactSalesTransaction.transaction_type == 'Order',
        FactSalesTransaction.biz_date.isnot(None),
        FactSalesTransaction.order_id.isnot(None),
    )
    if scope_ids is not None:
        oq = oq.filter(FactSalesTransaction.store_id.in_(scope_ids or [-1]))
    if country:
        oq = oq.filter(FactSalesTransaction.country == country)
    period_orders = {p: int(n or 0) for p, n in oq.group_by('period').all() if p}

    # ---- 汇率（趋势跨月，用最新可得月份近似）----
    rates = _country_rates(db, store_obj.id if store_obj else None, period_month
                           or (f"{year}-{int(month):02d}" if year and month else None))

    # ---- 利润与销售额来自快照（保证与 /summary 口径一致）----
    # ★ 为什么销售额也用快照：快照内是"逐国按各自汇率折算"后的 RMB，
    #   而这里若用一组近似汇率重算，会出现趋势图与汇总卡片数字不一致。
    # ★ 只取与请求相同的粒度（无站点时取店铺级 country=''），否则店铺级+站点级会重复相加。
    snaps = db.query(DwsProfitSnapshot).filter(
        DwsProfitSnapshot.period_month.isnot(None),
        DwsProfitSnapshot.country == (country or ''))
    if store_obj:
        snaps = snaps.filter(DwsProfitSnapshot.store_id == store_obj.id)
    profit_best = {}
    sales_best = {}
    for s in snaps.all():
        key = s.period_month if dimension != 'year' else s.period_month[:4]
        skey = (key, s.store_id, s.country or '')
        if skey not in profit_best or s.version > profit_best[skey][1]:
            profit_best[skey] = (float(s.net_profit_rmb or 0), s.version)
            sales_best[skey] = float(s.product_sales_rmb or 0)
    # 同一期间可能有多家店铺的快照 → 必须**累加**（早期写成覆盖，导致趋势利润偏小）
    profit_acc = {}
    for (key, _sid, _c), (val, _v) in profit_best.items():
        profit_acc[key] = profit_acc.get(key, 0.0) + val
    profit_map = {k: (v, 0) for k, v in profit_acc.items()}
    sales_snap_by_period = {}
    for (key, _sid, _c), val in sales_best.items():
        sales_snap_by_period[key] = sales_snap_by_period.get(key, Decimal(0)) + _d(val)

    # ---- 广告 / 仓储（按国家；趋势图用其折算 RMB）----
    ad_all = _all_ad_by_month_country(db, store_obj, country)
    storage_all = _all_fee_by_month_country(
        db, FactFeeStorage, 'estimated_monthly_storage_fee', 'month_of_charge', store_obj, country)

    def to_rmb(by_cm, key):
        """把 {(国家, 期间): 原币} 在指定期间上折算 RMB"""
        return sum((v * rates.get(c, Decimal('6.8'))
                    for (c, k2), v in by_cm.items() if k2 == key), Decimal(0))

    items = []
    for key in sorted(period_sales.keys())[-months:]:
        # 有快照时优先用快照的销售额（与 /summary 一致）；否则用 fact + 汇率估算
        sales_rmb_val = sales_snap_by_period.get(key)
        if not sales_rmb_val:
            sales_rmb_val = to_rmb(sales_by_cm, key)
        items.append({
            'period': key,
            'sales_rmb': float(sales_rmb_val),
            'qty': period_qty.get(key, 0),
            'order_count': period_orders.get(key, 0),
            'net_profit_rmb': profit_map.get(key, (None, None))[0],
            'ad_spend_rmb': float(to_rmb(ad_all, key)),
            'storage_fee_rmb': float(to_rmb(storage_all, key)),
        })

    return inject_legacy({'items': items, 'dimension': dimension}, 'dashboard/trend')


def _all_ad_by_month_country(db, store_obj, country):
    """{(国家, 月份): 广告花费原币} —— 趋势图用；SQL 聚合

    月份优先 biz_date（date_format），无日期行回退 fact_ad_daily.period_month
    （历史数据已全量回填，无需再回退批次月份）。
    """
    month_expr = sa_func.coalesce(
        sa_func.date_format(FactAdDaily.biz_date, '%Y-%m'),
        FactAdDaily.period_month,
    )
    q = db.query(
        FactAdDaily.country.label('country'),
        month_expr.label('month'),
        sa_func.sum(FactAdDaily.spend).label('spend'),
    ).filter(month_expr.isnot(None))
    if store_obj:
        q = q.filter(FactAdDaily.store_id == store_obj.id)
    if country:
        q = q.filter(FactAdDaily.country == country)
    out = {}
    for c, month, spend in q.group_by('country', 'month').all():
        ck = (c or '', month)
        out[ck] = out.get(ck, Decimal(0)) + _d(spend or 0)
    return out


def _all_fee_by_month_country(db, model, attr, month_col, store_obj, country):
    """{(国家, 月份): 费用原币} — SQL 聚合；月份取前 7 位（兼容历史带时间戳值）"""
    month_expr = sa_func.substring(getattr(model, month_col), 1, 7)
    q = db.query(
        model.country.label('country'),
        month_expr.label('month'),
        sa_func.sum(getattr(model, attr)).label('amt'),
    ).filter(getattr(model, month_col).isnot(None),
             getattr(model, month_col) != '')
    if store_obj:
        q = q.filter(model.store_id == store_obj.id)
    if country and hasattr(model, 'country'):
        q = q.filter(model.country == country)
    out = {}
    for c, month, amt in q.group_by('country', 'month').all():
        ck = (c or '', month)
        out[ck] = out.get(ck, Decimal(0)) + _d(amt or 0)
    return out


@router.get("/product-distribution")
def product_distribution(store: str | None = None, country: str | None = None,
                         period_month: str | None = None, year: int | None = Query(None, description="年份（Deprecated，请用 period_month）", deprecated=True),
                         month: int | None = Query(None, description="月份（Deprecated，请用 period_month）", deprecated=True), limit: int = 200,
                         db: Session = Depends(get_db),
                         user: SysUser = Depends(require_module("dashboard"))):
    """产品销售分布（ASIN 维度）

    返回 `items`（新）与 `data`（旧前端兼容：name/sales_rmb/net_profit）。
    """
    start, end, pm, mode = _resolve_period(period_month, year, month)
    store_obj = filter_store_by_permission(user, store, db) if store else None
    rates = _country_rates(db, store_obj.id if store_obj else None, pm)
    q = db.query(
        FactSalesTransaction.asin.label('asin'),
        FactSalesTransaction.country.label('country'),
        FactSalesTransaction.transaction_type.label('ttype'),
        sa_func.sum(FactSalesTransaction.product_sales).label('sales'),
        sa_func.sum(FactSalesTransaction.quantity).label('qty'),
    ).filter(
        FactSalesTransaction.biz_date >= start, FactSalesTransaction.biz_date < end,
        FactSalesTransaction.transaction_type.in_(PROFIT_TYPES),
        FactSalesTransaction.asin.isnot(None))
    q = _scope(q, FactSalesTransaction, user, db, store_obj)
    if country:
        q = q.filter(FactSalesTransaction.country == country)
    agg = {}
    for asin, c, ttype, sales, q2 in q.group_by('asin', 'country', 'ttype').all():
        if not asin:
            continue
        d = agg.setdefault(asin, {'sales': Decimal(0), 'qty': 0})
        val = _d(sales or 0)
        signed = val if ttype == 'Order' else -abs(val)
        d['sales'] += signed * rates.get(c or '', Decimal('6.8'))
        if ttype == 'Order':
            d['qty'] += int(q2 or 0)
    total = sum((v['sales'] for v in agg.values()), Decimal(0))

    # 单品利润（B-04：来自最新快照逐 SKU 明细，Σ该 ASIN 各站点明细利润；不再硬编码 0）
    # 快照粒度与 /summary 相同（未指定站点 → 店铺级快照，明细为 SKU×站点）；
    # 旧版快照明细（B-01 前，SKU 粒度）同样按 ASIN 聚合兼容。
    snaps = _latest_snapshots(db, store_obj, country, pm, start.year)
    profit_by_asin: dict[str, Decimal] = {}
    if snaps:
        pit = db.query(
            DwsProfitSnapshotItem.asin.label('asin'),
            sa_func.sum(DwsProfitSnapshotItem.net_profit_rmb).label('profit'),
        ).filter(DwsProfitSnapshotItem.snapshot_id.in_([s.id for s in snaps]))
        profit_by_asin = {a or '': _d(v) for a, v in pit.group_by('asin').all()}

    items = []
    for asin, v in agg.items():
        p = profit_by_asin.get(asin)
        items.append({'asin': asin, 'name': asin, 'sales_rmb': float(v['sales']), 'qty': v['qty'],
                      'share': float((v['sales'] / total).quantize(Decimal('0.0001'))) if total else 0,
                      # 该 ASIN 无快照明细 → null（未核算，前端展示区分而非假 0）
                      'net_profit_rmb': float(p) if p is not None else None,
                      'has_snapshot': p is not None})
    items.sort(key=lambda x: x['sales_rmb'], reverse=True)
    return inject_legacy(
        {'period_month': pm, 'total_sales_rmb': float(total), 'items': items[:limit]},
        'dashboard/product-distribution')


@router.get("/fee-composition")
def fee_composition(store: str | None = None, country: str | None = None,
                    period_month: str | None = None, year: int | None = Query(None, description="年份（Deprecated，请用 period_month）", deprecated=True),
                    month: int | None = Query(None, description="月份（Deprecated，请用 period_month）", deprecated=True),
                    db: Session = Depends(get_db),
                    user: SysUser = Depends(require_module("dashboard"))):
    """费用构成（饼图）

    返回 `items`（新）与旧前端兼容结构 `{sales, expenses:[{name,value}], net_profit}`
    —— 旧看板直接展开 `expenses`，缺这个键会导致整页白屏，务必保留。
    ★ 请求级缓存（spec 5.3.1 R6）。
    """
    params = {'store': store, 'country': country, 'period_month': period_month,
              'year': year, 'month': month}
    return cached_aggregate(
        'dashboard/fee-composition', params, user.id, db,
        lambda: _fee_composition_impl(db, user, store, country, period_month, year, month))


def _fee_composition_impl(db, user, store, country, period_month, year, month):
    start, end, pm, mode = _resolve_period(period_month, year, month)
    store_obj = filter_store_by_permission(user, store, db) if store else None
    rates = _country_rates(db, store_obj.id if store_obj else None, pm)

    def fee(model, attr, month_col=None, date_col=None):
        """费用 → RMB 合计 — 唯一实现 common/fees.fee_by_country（口径与 /summary 一致）

        month_col 前缀/等值匹配（精确月 / 整年），避免全年模式把上年账期行
        也算进来（与 summary 数字不一致）；date_col（移除/入库）按区间。
        """
        by_c = fee_by_country(
            db, model, attr,
            month_col=month_col, period=(pm or str(start.year)) if month_col else None,
            stores=store_obj.id if store_obj else None, country=country,
            date_col=date_col, start=start, end=end)
        return sum((v * rates.get(c, Decimal('6.8')) for c, v in by_c.items()), Decimal(0))

    ad = _ad_spend_by_country(db, store_obj.id if store_obj else None, start, end, pm)
    if country:
        ad = {c: v for c, v in ad.items() if c == country}
    ad_rmb = sum((v * rates.get(c, Decimal('6.8')) for c, v in ad.items()), Decimal(0))

    expenses = [
        {'name': '广告费', 'value': float(ad_rmb)},
        {'name': '仓储费', 'value': float(fee(FactFeeStorage, 'estimated_monthly_storage_fee',
                                              month_col='month_of_charge'))},
        {'name': '超龄仓储费', 'value': float(fee(FactFeeAgedStorage, 'amount_charged',
                                                month_col='snapshot_date'))},
        {'name': '退货费', 'value': float(fee(FactFeeReturn, 'sku_returns_fee',
                                              month_col='month_of_charge'))},
        {'name': '移除费', 'value': float(fee(FactFeeRemoval, 'removal_fee',
                                              date_col=FactFeeRemoval.request_date))},
        {'name': '入库费', 'value': float(fee(FactFeeInboundConfig, 'total_fee',
                                              date_col=FactFeeInboundConfig.transaction_date))},
    ]
    expenses = [e for e in expenses if e['value'] != 0]

    # 销售额与净利润（快照口径；按粒度去重，避免店铺级与站点级重复相加）
    snaps = _latest_snapshots(db, store_obj, country, pm, start.year)
    sales = sum((_d(s.product_sales_rmb) for s in snaps), Decimal(0))
    net_profit = sum((_d(s.net_profit_rmb) for s in snaps), Decimal(0))

    return inject_legacy({
        'period_month': pm,
        'items': expenses,
        'total_sales_rmb': float(sales),
        'net_profit_rmb': float(net_profit),
    }, 'dashboard/fee-composition')


@router.get("/refund-ranking")
def refund_ranking(store: str | None = None, country: str | None = None,
                   period_month: str | None = None, year: int | None = Query(None, description="年份（Deprecated，请用 period_month）", deprecated=True),
                   month: int | None = Query(None, description="月份（Deprecated，请用 period_month）", deprecated=True), limit: int = 20,
                   db: Session = Depends(get_db), user: SysUser = Depends(require_module("dashboard"))):
    """退货排名（按 ASIN）

    返回 `items`（新）与 `data`（旧前端兼容：name/asin/refund_amount/refund_qty/order_count/return_rate）。
    """
    start, end, pm, mode = _resolve_period(period_month, year, month)
    store_obj = filter_store_by_permission(user, store, db) if store else None
    # key = asin（空则回退 SKU）—— 与原逐行逻辑一致
    key_expr = sa_func.coalesce(sa_func.nullif(FactSalesTransaction.asin, ''),
                                FactSalesTransaction.normalized_sku, '')
    base = [
        FactSalesTransaction.biz_date >= start, FactSalesTransaction.biz_date < end,
        FactSalesTransaction.transaction_type.in_(PROFIT_TYPES),
    ]
    q = db.query(
        key_expr.label('key'),
        FactSalesTransaction.normalized_sku.label('sku'),
        FactSalesTransaction.transaction_type.label('ttype'),
        sa_func.sum(FactSalesTransaction.quantity).label('qty'),
        sa_func.sum(FactSalesTransaction.product_sales).label('sales'),
    ).filter(*base)
    q = _scope(q, FactSalesTransaction, user, db, store_obj)
    if country:
        q = q.filter(FactSalesTransaction.country == country)

    agg = {}
    # ★ group_by 传 label 表达式而非字符串：'sku' 会被 MySQL 解析为表原始列
    #   （而非 SELECT 别名 normalized_sku），触发 only_full_group_by 报错。
    _key_l, _sku_l, _ttype_l = q.column_descriptions[0]['expr'], \
        q.column_descriptions[1]['expr'], q.column_descriptions[2]['expr']
    for key, sku, ttype, q2, sales in q.group_by(_key_l, _sku_l, _ttype_l).all():
        d = agg.setdefault(key, {'sold': 0, 'returned': 0, 'refund_amt': Decimal(0),
                                 'orders': 0, 'sku': sku or ''})
        if ttype == 'Order':
            d['sold'] += int(q2 or 0)
        else:
            d['returned'] += abs(int(q2 or 0))
            d['refund_amt'] += abs(_d(sales or 0))

    # 订单数（Order 行去重计数）—— SQL 端 DISTINCT，避免取全量订单号
    oq = db.query(
        key_expr.label('key'),
        sa_func.count(sa_func.distinct(FactSalesTransaction.order_id)).label('orders'),
    ).filter(*base, FactSalesTransaction.transaction_type == 'Order',
             FactSalesTransaction.order_id.isnot(None))
    oq = _scope(oq, FactSalesTransaction, user, db, store_obj)
    if country:
        oq = oq.filter(FactSalesTransaction.country == country)
    _okey_l = oq.column_descriptions[0]['expr']
    order_counts = {k: int(n or 0) for k, n in oq.group_by(_okey_l).all()}
    for k, n in order_counts.items():
        if k in agg:
            agg[k]['orders'] = n

    items = []
    for asin, d in agg.items():
        if d['returned'] == 0:
            continue
        rate = round(d['returned'] / d['sold'] * 100, 1) if d['sold'] else 0
        items.append({'asin': asin, 'sku': d['sku'], 'sold': d['sold'], 'returned': d['returned'],
                      'order_count': d['orders'],
                      'refund_rate': round(d['returned'] / d['sold'], 4) if d['sold'] else None,
                      'return_rate_pct': rate,
                      'refund_amount': float(d['refund_amt'])})
    items.sort(key=lambda x: x['returned'], reverse=True)
    return inject_legacy({'period_month': pm, 'items': items[:limit]}, 'dashboard/refund-ranking')


@router.get("/store-compare")
@router.get("/store-comparison", include_in_schema=False)
def store_comparison(store: str | None = None, country: str | None = None,
                     period_month: str | None = None, year: int | None = Query(None, description="年份（Deprecated，请用 period_month）", deprecated=True),
                     month: int | None = Query(None, description="月份（Deprecated，请用 period_month）", deprecated=True),
                     db: Session = Depends(get_db),
                     user: SysUser = Depends(require_module("dashboard"))):
    """店铺对比（销售额/利润）

    返回 `items`（新）与 `data`（旧前端兼容：code/name/sales_rmb/net_profit/order_count/profit_rate）。

    ★ P-04 修复：原实现按店铺循环 4 类查询（N 店铺 = 4N+ 次）；现收敛为恒定 ≤6 次：
      ① 销售聚合单条 GROUP BY (store_id, country, ttype)；② 订单计数同模式；
      ③ 快照利润 latest_snapshots 批量版（store_id IN）；④ 汇率 resolve_for_stores
      （一次取回全部店铺 + 全局汇率行，内存按店铺做四级回退）。
    """
    start, end, pm, mode = _resolve_period(period_month, year, month)
    stores = get_authorized_stores(user, db)
    if store:
        stores = [s for s in stores if s.store_code == store]
    items = []
    store_ids = [s.id for s in stores]
    if not store_ids:
        return inject_legacy({'period_month': pm, 'items': items}, 'dashboard/store-compare')
    t = FactSalesTransaction

    # ④ 汇率（每店铺独立四级回退；国家集合口径与 _country_rates 一致 = 全表 distinct）
    countries = [r[0] for r in db.query(t.country).distinct().all() if r[0]]
    rates_by_store = resolve_for_stores(db, store_ids, countries, pm)

    # ① 销售聚合：单条 SQL GROUP BY (store_id, country, ttype)
    sales_agg: dict[int, dict[str, Decimal]] = {sid: {} for sid in store_ids}
    sq = db.query(
        t.store_id.label('sid'), t.country.label('country'),
        t.transaction_type.label('ttype'),
        sa_func.sum(t.product_sales).label('sales'),
    ).filter(
        t.store_id.in_(store_ids),
        t.biz_date >= start, t.biz_date < end,
        t.transaction_type.in_(PROFIT_TYPES))
    if country:
        sq = sq.filter(t.country == country)
    for sid, c, ttype, sales_v in sq.group_by('sid', 'country', 'ttype').all():
        val = _d(sales_v or 0)
        signed = val if ttype == 'Order' else -abs(val)
        d = sales_agg.setdefault(sid, {})
        d[c or ''] = d.get(c or '', Decimal(0)) + signed

    # ② 订单计数：单条 SQL GROUP BY store_id（去重订单号）
    order_counts: dict[int, int] = {sid: 0 for sid in store_ids}
    oq = db.query(
        t.store_id.label('sid'),
        sa_func.count(sa_func.distinct(t.order_id)).label('orders'),
    ).filter(
        t.store_id.in_(store_ids),
        t.biz_date >= start, t.biz_date < end,
        t.transaction_type.in_(PROFIT_TYPES),
        t.order_id.isnot(None))
    if country:
        oq = oq.filter(t.country == country)
    for sid, n in oq.group_by('sid').all():
        order_counts[sid] = int(n or 0)

    # ③ 快照利润：批量最新版本（店铺级/指定站点粒度，去重键与原循环版一致）
    snaps = latest_snapshots(db, store_ids, country or '', pm or str(start.year))
    profit_by_store: dict[int, Decimal] = {sid: Decimal(0) for sid in store_ids}
    for sn in snaps:
        profit_by_store[sn.store_id] = profit_by_store.get(sn.store_id, Decimal(0)) \
            + _d(sn.net_profit_rmb)

    for s in stores:
        rates = rates_by_store.get(s.id, {})
        sales = sum((v * rates.get(c, Decimal('6.8'))
                     for c, v in sales_agg.get(s.id, {}).items()), Decimal(0))
        order_count = order_counts.get(s.id, 0)
        profit = profit_by_store.get(s.id, Decimal(0))
        profit_rate = round(float(profit / sales * 100), 1) if sales else 0.0
        items.append({'store_code': s.store_code, 'store_name': s.store_name,
                      'sales_rmb': float(sales), 'net_profit_rmb': float(profit),
                      'order_count': order_count, 'profit_rate': profit_rate})
    items.sort(key=lambda x: x['sales_rmb'], reverse=True)
    return inject_legacy({'period_month': pm, 'items': items}, 'dashboard/store-compare')


@router.get("/country-compare")
@router.get("/country-comparison", include_in_schema=False)
def country_comparison(store: str | None = None, period_month: str | None = None,
                       year: int | None = Query(None, description="年份（Deprecated，请用 period_month）", deprecated=True), month: int | None = Query(None, description="月份（Deprecated，请用 period_month）", deprecated=True),
                       db: Session = Depends(get_db),
                       user: SysUser = Depends(require_module("dashboard"))):
    """站点对比（销售额/利润）

    返回 `items`（新）与 `data`（旧前端兼容：code/name/sales_rmb/net_profit/...）。
    """
    start, end, pm, mode = _resolve_period(period_month, year, month)
    store_obj = filter_store_by_permission(user, store, db) if store else None
    rates = _country_rates(db, store_obj.id if store_obj else None, pm)
    q = db.query(
        FactSalesTransaction.country.label('country'),
        FactSalesTransaction.transaction_type.label('ttype'),
        sa_func.sum(FactSalesTransaction.product_sales).label('sales'),
    ).filter(
        FactSalesTransaction.biz_date >= start, FactSalesTransaction.biz_date < end,
        FactSalesTransaction.transaction_type.in_(PROFIT_TYPES))
    q = _scope(q, FactSalesTransaction, user, db, store_obj)
    agg = {}
    for c, ttype, sales_v in q.group_by('country', 'ttype').all():
        c = c or '未知'
        d = agg.setdefault(c, {'sales': Decimal(0), 'orders': 0})
        val = _d(sales_v or 0)
        signed = val if ttype == 'Order' else -abs(val)
        d['sales'] += signed * rates.get(c if c != '未知' else '', Decimal('6.8'))

    oq = db.query(
        FactSalesTransaction.country.label('country'),
        sa_func.count(sa_func.distinct(FactSalesTransaction.order_id)).label('orders'),
    ).filter(
        FactSalesTransaction.biz_date >= start, FactSalesTransaction.biz_date < end,
        FactSalesTransaction.transaction_type == 'Order',
        FactSalesTransaction.order_id.isnot(None))
    oq = _scope(oq, FactSalesTransaction, user, db, store_obj)
    for c, n in oq.group_by('country').all():
        agg.setdefault(c or '未知', {'sales': Decimal(0), 'orders': 0})['orders'] = int(n or 0)

    # ---- 各站点最新**站点级**快照（B-03：利润不再硬编码 0）----
    # ★ 只取站点级（country=站点代码）快照，绝不混入店铺级汇总（country=''）——防重复相加；
    #   同站点跨店铺累加（与销售侧多店聚合口径一致）。
    from app.core.security import get_authorized_store_ids
    snap_store_ids = [store_obj.id] if store_obj else get_authorized_store_ids(user, db)
    snap_period = pm or str(start.year)
    profit_by_c: dict[str, Decimal] = {}
    sales_snap_by_c: dict[str, Decimal] = {}
    for c in sorted(k for k in agg if k != '未知'):
        snaps = latest_snapshots(db, snap_store_ids, c, snap_period)
        if snaps:
            profit_by_c[c] = sum((_d(s.net_profit_rmb) for s in snaps), Decimal(0))
            sales_snap_by_c[c] = sum((_d(s.product_sales_rmb) for s in snaps), Decimal(0))

    items = []
    for c, v in agg.items():
        profit = profit_by_c.get(c)
        snap_sales = sales_snap_by_c.get(c)
        rate = float((profit / snap_sales).quantize(Decimal('0.0001'))) \
            if (profit is not None and snap_sales) else None
        items.append({
            'country': c, 'sales_rmb': float(v['sales']), 'order_count': v['orders'],
            # 未核算站点 → null + has_snapshot=false（前端展示"未核算"而非假 0）
            'net_profit_rmb': float(profit) if profit is not None else None,
            'net_profit_rate': rate,
            'has_snapshot': profit is not None,
        })
    items.sort(key=lambda x: x['sales_rmb'], reverse=True)
    return inject_legacy({'period_month': pm, 'items': items}, 'dashboard/country-compare')


@router.get("/transfer")
@router.get("/transfer-summary", include_in_schema=False)
def cashflow_transfer(store: str | None = None, period_month: str | None = None,
                      year: int | None = Query(None, description="年份（Deprecated，请用 period_month）", deprecated=True), month: int | None = Query(None, description="月份（Deprecated，请用 period_month）", deprecated=True),
                      db: Session = Depends(get_db),
                      user: SysUser = Depends(require_module("dashboard"))):
    """资金流对账：Transfer 提款（不计入利润，单独统计）

    返回 `items`/`by_country`（规范）；旧前端 `total_rmb`/`total_usd`/`by_type` 由别名层注入。
    """
    from app.models import FactCashflowTransfer
    start, end, pm, mode = _resolve_period(period_month, year, month)
    store_obj = filter_store_by_permission(user, store, db) if store else None
    rates = _country_rates(db, store_obj.id if store_obj else None, pm)
    q = db.query(FactCashflowTransfer).filter(
        FactCashflowTransfer.biz_date >= start, FactCashflowTransfer.biz_date < end)
    q = _scope(q, FactCashflowTransfer, user, db, store_obj)
    rows = q.all()
    by_country = {}
    total = Decimal(0)
    total_rmb = Decimal(0)
    for r in rows:
        c = r.country or ''
        amt = _d(r.amount)
        total += amt
        total_rmb += amt * rates.get(c, Decimal('6.8'))
        d = by_country.setdefault(c, {'usd': Decimal(0), 'rmb': Decimal(0), 'count': 0})
        d['usd'] += amt
        d['rmb'] += amt * rates.get(c, Decimal('6.8'))
        d['count'] += 1
    country_list = [{'country': k, 'usd': round(float(v['usd']), 2),
                     'rmb': round(float(v['rmb']), 2), 'count': v['count']}
                    for k, v in sorted(by_country.items(), key=lambda x: x[1]['rmb'])]
    return inject_legacy({
        'period_month': pm, 'total': float(total), 'total_rmb': round(float(total_rmb), 2),
        'count': len(rows), 'by_country': country_list,
    }, 'dashboard/transfer')
