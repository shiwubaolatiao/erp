"""独立复算对账工具 — 验收核心

思路：**绕开导入管线的清洗逻辑**，直接从 ODS 原始行（raw_sheet_row.headers_json/values_json）
按表头名取值、独立聚合，再套用与 profit.engine 相同的公式，与 fact 表 / 快照三方交叉对账。

三方对账：
  ① ODS 独立复算（原值直接求和）
  ② fact_sales_transaction（管线清洗结果）
  ③ dws_profit_snapshot（核算快照）

任一处不一致 → 说明中间有丢数据/字段错位/口径偏差。

用法: python tools/reconcile.py <store_code> <period_month> [country]
"""
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import func as sa_func
from app.core.database import SessionLocal
from app.models import (
    Store, RawSheet, RawSheetRow, FactSalesTransaction, FactAdDaily,
    FactFeeStorage, FactFeeAgedStorage, FactFeeReturn, FactFeeRemoval,
    FactFeeInboundConfig, DwsProfitSnapshot, DwsProfitSnapshotItem,
)
from app.services.profit.engine import compute_net_profit
from app.services.importer.parser import (
    parse_decimal, parse_date, normalize_transaction_type,
    normalize_header, map_header_to_canonical,
)

D = Decimal
PROFIT_TYPES = ('Order', 'Refund')
# Transfer 不进 fact_sales_transaction（单独进资金流表），对账时从 ODS 侧同样剔除
EXCLUDED_FROM_FACT = ('Transfer',)

# ODS 表头名（原始，含多语言）→ 标准字段：用与管线相同的归一逻辑反查
FIELDS = {
    'product_sales': ['product sales', '商品销售额', 'umsätze', 'ventes de produits', 'ventas de productos'],
    'product_sales_tax': ['product sales tax'],
    'postage_credits': ['postage credits', 'gutschrift für versandkosten'],
    'shipping_credits_tax': ['shipping credits tax'],
    'gift_wrap_credits': ['gift wrap credits'],
    'giftwrap_credits_tax': ['giftwrap credits tax'],
    'promo_rebate': ['promotional rebates', 'rabatte aus werbeaktionen'],
    'promo_rebate_tax': ['promotional rebates tax'],
    'marketplace_withheld_tax': ['marketplace withheld tax'],
    'selling_fee': ['selling fees', 'verkaufsgebühren'],
    'fba_fee': ['fba fees', 'gebühren zu versand durch amazon'],
    'other_amount': ['other', 'andere', 'autre'],
    'line_total': ['total', 'gesamt'],
    'type': ['type', 'typ', 'tipo'],
    'order_id': ['order id', 'bestellnummer', 'numéro de la commande'],
    'date_time': ['date/time', 'datum/uhrzeit', 'date/heure', 'data/ora'],
    'quantity': ['quantity', 'menge'],
    'sku': ['sku'],
}


def month_bounds(pm: str):
    """YYYY-MM → [月初, 次月初) 的半开区间"""
    y, m = int(pm[:4]), int(pm[5:7])
    return datetime(y, m, 1), datetime(y + (m == 12), (m % 12) + 1, 1)


def find_idx(header_norm: list, names: list) -> int:
    """在表头列表中按候选名查找列索引（返回 -1 表示没有）"""
    for n in names:
        if n in header_norm:
            return header_norm.index(n)
    return -1


def main():
    """对指定店铺/月份做 ODS↔fact↔快照 三方对账并打印结果"""
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    store_code, period = sys.argv[1].upper(), sys.argv[2]
    only_country = sys.argv[3].upper() if len(sys.argv) > 3 else None

    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.store_code == store_code).first()
        if not store:
            print(f"店铺 {store_code} 不存在")
            return 1
        start, end = month_bounds(period)

        # ---------- ① ODS 独立复算 ----------
        sheets = db.query(RawSheet).filter(
            RawSheet.report_type == 'sales').all()
        sheet_ids = [s.id for s in sheets if s.country and s.country != '__none__']
        rows = db.query(RawSheetRow).filter(RawSheetRow.sheet_id.in_(sheet_ids)).all() if sheet_ids else []
        sheet_meta = {s.id: s for s in sheets}

        ods = {}   # country -> dict of sums
        ods_rows = 0
        for r in rows:
            s = sheet_meta.get(r.sheet_id)
            if not s:
                continue
            country = s.country
            if only_country and country != only_country:
                continue
            # ★ 与管线共用同一套表头归一 + 列映射字典（保证对账是"同口径"较量聚合逻辑）
            norm = normalize_header(r.headers_json or [])
            cmap = map_header_to_canonical(r.headers_json or [])
            val = r.values_json or []

            def get(field, default=None):
                """按标准字段名取当前原始行的值"""
                i = cmap.get(field, -1)
                if i < 0 or i >= len(val):
                    return default
                return val[i]

            ttype = normalize_transaction_type(str(get('transaction_type') or ''))
            dt = parse_date(get('date_time'))
            if dt is None or not (start <= dt < end):
                continue
            if ttype in EXCLUDED_FROM_FACT:
                continue          # Transfer 在 fact 侧不在 sales 表，剔除后可比
            d = ods.setdefault(country, {'ps': D(0), 'pst': D(0), 'pc': D(0), 'sct': D(0),
                                         'gwc': D(0), 'gwct': D(0), 'promo': D(0), 'ptax': D(0),
                                         'mkt': D(0), 'sf': D(0), 'fba': D(0), 'adj': D(0),
                                         'qty': 0, 'rows': 0})
            if ttype in PROFIT_TYPES:
                d['ps'] += parse_decimal(get('product_sales'), D(0))
                d['pst'] += parse_decimal(get('product_sales_tax'), D(0))
                d['pc'] += parse_decimal(get('postage_credits'), D(0))
                d['sct'] += parse_decimal(get('shipping_credits_tax'), D(0))
                d['gwc'] += parse_decimal(get('gift_wrap_credits'), D(0))
                d['gwct'] += parse_decimal(get('giftwrap_credits_tax'), D(0))
                d['promo'] += parse_decimal(get('promotional_rebates'), D(0))
                d['ptax'] += parse_decimal(get('promotional_rebates_tax'), D(0))
                d['mkt'] += parse_decimal(get('marketplace_withheld_tax'), D(0))
                d['sf'] += parse_decimal(get('selling_fees'), D(0))
                d['fba'] += parse_decimal(get('fba_fees'), D(0))
                if ttype == 'Order':
                    d['qty'] += int(parse_decimal(get('quantity'), D(0)))
            elif ttype == 'Adjustment':
                oid = str(get('order_id') or '').strip()
                if oid:
                    d['adj'] += parse_decimal(get('other'), D(0))
                else:
                    d['adj'] += parse_decimal(get('total'), D(0))
            d['rows'] += 1
            ods_rows += 1

        # ---------- ② fact 表聚合 ----------
        facts = db.query(FactSalesTransaction).filter(
            FactSalesTransaction.store_id == store.id,
            FactSalesTransaction.biz_date >= start,
            FactSalesTransaction.biz_date < end,
        ).all()
        fac = {}
        for r in facts:
            if only_country and r.country != only_country:
                continue
            d = fac.setdefault(r.country, {'ps': D(0), 'pst': D(0), 'pc': D(0), 'sct': D(0),
                                           'gwc': D(0), 'gwct': D(0), 'promo': D(0), 'ptax': D(0),
                                           'mkt': D(0), 'sf': D(0), 'fba': D(0), 'adj': D(0),
                                           'qty': 0, 'rows': 0})
            t = r.transaction_type
            if t in PROFIT_TYPES:
                d['ps'] += D(str(r.product_sales or 0)); d['pst'] += D(str(r.product_sales_tax or 0))
                d['pc'] += D(str(r.postage_credits or 0)); d['sct'] += D(str(r.shipping_credits_tax or 0))
                d['gwc'] += D(str(r.gift_wrap_credits or 0)); d['gwct'] += D(str(r.giftwrap_credits_tax or 0))
                d['promo'] += D(str(r.promo_rebate or 0)); d['ptax'] += D(str(r.promo_rebate_tax or 0))
                d['mkt'] += D(str(r.marketplace_withheld_tax or 0))
                d['sf'] += D(str(r.selling_fee or 0)); d['fba'] += D(str(r.fba_fee or 0))
                if t == 'Order':
                    d['qty'] += r.quantity or 0
            elif t == 'Adjustment':
                d['adj'] += D(str(r.other_amount if r.has_order_id else r.line_total or 0))
            d['rows'] += 1

        # ---------- 输出对账 ----------
        print(f"=== 独立复算对账  {store_code} / {period} ===\n")
        print(f"ODS 参与复算行数: {ods_rows}   fact 行数: {len(facts)}\n")

        countries = sorted(set(ods) | set(fac))
        header = (f"{'国家':6s} {'行数(ods/fact)':16s} {'product_sales':>34s} {'net_amazon':>32s} {'差异':>10s}")
        print(header)
        print('-' * len(header))
        all_ok = True
        for c in countries:
            o = ods.get(c); f_ = fac.get(c)
            if not o or not f_:
                print(f"{c:6s} 仅在{'ODS' if o else 'fact'}中存在！ ods_rows={o['rows'] if o else 0} fact_rows={f_['rows'] if f_ else 0}")
                all_ok = False
                continue
            na_o = (o['ps'] + o['pst'] + o['pc'] + o['sct'] + o['gwc'] + o['gwct']
                    + o['promo'] + o['ptax'] + o['mkt'] + o['sf'] + o['fba'] + o['adj'])
            na_f = (f_['ps'] + f_['pst'] + f_['pc'] + f_['sct'] + f_['gwc'] + f_['gwct']
                    + f_['promo'] + f_['ptax'] + f_['mkt'] + f_['sf'] + f_['fba'] + f_['adj'])
            diff = na_o - na_f
            flag = 'OK' if abs(diff) < D('0.01') else '★差异★'
            if abs(diff) >= D('0.01'):
                all_ok = False
            print(f"{c:6s} {o['rows']:5d}/{f_['rows']:<10d} {o['ps']:>34} {na_o:>32} {str(diff):>10s} {flag}")

        print(f"\n对账结论: {'✓ 全部一致（ODS 独立复算 == fact 表聚合）' if all_ok else '✗ 存在差异，需排查'}")

        # ---------- ③ 与快照对比 ----------
        print("\n=== 与核算快照对比 ===")
        snaps = db.query(DwsProfitSnapshot).filter(
            DwsProfitSnapshot.store_id == store.id,
            DwsProfitSnapshot.period_month == period,
        ).all()
        latest = {}
        for s in snaps:
            k = s.country or ''
            if k not in latest or s.version > latest[k].version:
                latest[k] = s
        if not latest:
            print("  [无快照] 请先执行核算")
        for c, s in sorted(latest.items()):
            print(f"  {c or '(全部)':6s} 快照净利润={s.net_profit_rmb}  净利率={s.net_profit_rate}  "
                  f"net_amazon={s.net_amazon}  汇率={s.exchange_rate}  "
                  f"成本完整={s.cost_complete}  缺失SKU={s.cost_incomplete_skus}")
            if c in fac:
                f_ = fac[c]
                na_f = (f_['ps'] + f_['pst'] + f_['pc'] + f_['sct'] + f_['gwc'] + f_['gwct']
                        + f_['promo'] + f_['ptax'] + f_['mkt'] + f_['sf'] + f_['fba'] + f_['adj'])
                delta = na_f - D(str(s.net_amazon or 0))
                print(f"          net_amazon 复核: fact={na_f} 快照={s.net_amazon} 差异={delta} "
                      f"{'OK' if abs(delta) < D('0.01') else '★差异★'}")
        return 0 if all_ok else 2
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
