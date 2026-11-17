"""核算计算层 — 纯内存计算（除 engine 公式调用外无任何 DB 访问）

逐 (asin, sku, country) 聚合组：用**该站点汇率**单入口调用 engine.compute_net_profit
（公式零改动，Decimal 全程）。产出逐 SKU×站点明细行（含 country/exchange_rate/cost_status）
与汇总值；成本完整性标记语义对齐存量（missing 不静默按 0，由快照层强提醒）。
"""
from decimal import Decimal
from typing import Optional

from app.services.profit.engine import compute_net_profit, compute_net_amazon
from app.services.accounting.fetcher import CostLookup


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


def calculate_items(sales_rows: list[dict],
                    fees: dict[str, dict[tuple[str, str], Decimal]],
                    ad_by_key: dict[tuple[str, str, str], Decimal],
                    cost_lookup: CostLookup,
                    rates: dict[str, Decimal],
                    period_month: str) -> list[dict]:
    """逐 SKU×站点计算利润明细

    参数：
      sales_rows: fetcher.fetch_sales_agg 输出（键含 asin/sku/country/13 金额/qty/net_qty/batch_ids）
      fees:       fetcher.fetch_fee_agg 输出 {storage|aged|returns|removal|inbound: {(asin,country): v}}
      ad_by_key:  fetcher.fetch_ad_agg 输出 {(asin, sku, country): v}
      cost_lookup: fetcher.CostLookup（.get(asin, sku) → (cost, freight, status, inherited_from)）
      rates:      {country: 汇率}（RateResolver.resolve 或单站点 dict）
      period_month: 成本月版本回退基准月
    返回：明细行列表（每行含 country/exchange_rate/net_profit_rmb 等，键与快照明细对齐）
    """
    items: list[dict] = []
    for d in sales_rows:
        asin, sku, country = d['asin'], d['sku'], d['country']
        er = rates.get(country)
        if er is None:
            # 无汇率站点：跳过计算但不静默 —— 调用方（编排层）保证 rates 覆盖全部站点
            raise ValueError(f"站点 {country or '(空)'} 无汇率，无法核算（asin={asin}, sku={sku}）")
        cost, freight, cost_status, inherited_from = cost_lookup.get(asin, sku, period_month)
        ad_spend = ad_by_key.get((asin, sku, country), Decimal(0))
        st = fees.get('storage', {}).get((asin, country), Decimal(0))
        aged = fees.get('aged', {}).get((asin, country), Decimal(0))
        ret = fees.get('returns', {}).get((asin, country), Decimal(0))
        rem = fees.get('removal', {}).get((asin, country), Decimal(0))
        inb = fees.get('inbound', {}).get((asin, country), Decimal(0))
        # qty 为 0 时成本按 0（未销售不扣成本）；>0 按单位成本×数量（成本表是单位成本）
        qty = d['net_qty'] if d['net_qty'] > 0 else 0
        c_rmb = (cost or Decimal(0)) * qty
        f_rmb = (freight or Decimal(0)) * qty
        net = compute_net_profit(
            d['ps'], d['pst'], d['pc'], d['sct'], d['gwc'], d['gwct'],
            d['promo'], d['promo_tax'], d['mkt'], d['sf'], d['fba'], d['adj'],
            er, c_rmb, f_rmb,
            ad_spend, st + aged, ret, inb, rem,
        )
        items.append({
            'asin': asin, 'sku': sku, 'country': country,
            'net_amazon': compute_net_amazon(
                d['ps'], d['pst'], d['pc'], d['sct'], d['gwc'], d['gwct'],
                d['promo'], d['promo_tax'], d['mkt'], d['sf'], d['fba'], d['adj']),
            'quantity': d['net_qty'], 'order_count': d.get('orders', 0),
            'product_cost_rmb': c_rmb, 'freight_cost_rmb': f_rmb,
            'ad_spend': ad_spend, 'storage_fee': st, 'aged_storage_fee': aged,
            'returns_fee': ret, 'inbound_fee': inb, 'removal_fee': rem,
            'net_profit_rmb': net,
            'cost_status': cost_status, 'cost_inherited_from': inherited_from,
            'source_batch_ids': d.get('batch_ids', []),
            'exchange_rate': er,
        })
    return items


def summarize(items: list[dict], rates: dict[str, Decimal],
              sales_rows: list[dict]) -> dict:
    """汇总 —— 快照级合计 + 各站点利润构成（落库前自检与 rate_detail 依据）

    汇总口径与存量快照一致：
      · net_amazon / 各费用 = Σ明细原币（汇率前）
      · net_profit_rmb = Σ明细 RMB
      · product_sales_rmb = Σ(各组 ps × 该站点汇率)
      · exchange_rate（快照展示值）= Σ各组 ps×汇率 / Σ各组 ps（加权平均，仅展示用）
    """
    total_net_amazon = sum((i['net_amazon'] for i in items), Decimal(0))
    total_profit = sum((i['net_profit_rmb'] for i in items), Decimal(0))
    total_ps = sum((r['ps'] for r in sales_rows), Decimal(0))
    ps_weighted = sum((r['ps'] * rates.get(r['country'], Decimal(0)) for r in sales_rows),
                      Decimal(0))
    product_sales_rmb = ps_weighted.quantize(Decimal('0.01'))
    weighted_er = (ps_weighted / total_ps).quantize(Decimal('0.0001')) if total_ps else Decimal(0)
    return {
        'net_amazon': total_net_amazon,
        'net_profit_rmb': total_profit,
        'product_sales_rmb': product_sales_rmb,
        'weighted_exchange_rate': weighted_er,
        'product_cost_rmb': sum((i['product_cost_rmb'] for i in items), Decimal(0)),
        'freight_cost_rmb': sum((i['freight_cost_rmb'] for i in items), Decimal(0)),
        'ad_spend': sum((i['ad_spend'] for i in items), Decimal(0)),
        'storage_fee': sum((i['storage_fee'] for i in items), Decimal(0)),
        'aged_storage_fee': sum((i['aged_storage_fee'] for i in items), Decimal(0)),
        'returns_fee': sum((i['returns_fee'] for i in items), Decimal(0)),
        'inbound_fee': sum((i['inbound_fee'] for i in items), Decimal(0)),
        'removal_fee': sum((i['removal_fee'] for i in items), Decimal(0)),
        'order_qty': sum((i['quantity'] for i in items), 0),
        'incomplete': sum(1 for i in items if i['cost_status'] == 'missing'),
    }