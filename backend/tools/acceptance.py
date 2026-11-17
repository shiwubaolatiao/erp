"""全量验收脚本 — 对所有 店铺 × 月份 执行独立复算对账 + 核算

验收思路（用户确认的"溯源复算基准"）：
  不信任旧库数据，改为验证新系统自身数字可信性：
    ① **独立复算对账**：绕开导入管线，直接读 ODS 原始行(raw_sheet_row)的原始值，
       用与利润引擎相同的公式独立求和，与 fact 表聚合值逐国比对
    ② **核算一致性**：执行核算生成快照，校验快照 net_amazon == fact 表 net_amazon 合计
  任一处不一致 → 说明中间有丢数据/字段错位/口径偏差。

性能设计（重要）：
  原始行**每个店铺只加载并解析一次**，然后按 (国家, 月份) 分桶；
  否则 40 组 × 每组重扫全店原始行会慢 8 倍以上。

用法:
  python tools/acceptance.py                        # 全量
  python tools/acceptance.py --stores LMG,MGK       # 限定店铺
  python tools/acceptance.py --months 01,06         # 限定月份
"""
import sys
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import func as sa_func
from app.core.database import SessionLocal
from app.models import (
    Store, RawSheet, RawSheetRow, FactSalesTransaction, DwsProfitSnapshot,
)
from app.services.importer.parser import (
    parse_decimal, parse_date, normalize_transaction_type,
    normalize_header, map_header_to_canonical, detect_country_from_row,
)
from app.services.accounting.service import calculate_snapshot

D = Decimal
PROFIT_TYPES = ('Order', 'Refund')
EXCLUDED = ('Transfer',)          # Transfer 不在 fact_sales_transaction，两边都剔除


def month_bounds(pm):
    """YYYY-MM → [月初, 次月初) 半开区间"""
    y, m = int(pm[:4]), int(pm[5:7])
    return datetime(y, m, 1), datetime(y + (m == 12), (m % 12) + 1, 1)


def net_amazon_of(g) -> Decimal:
    """按利润口径算单行 net_amazon（g 为按标准字段取值的函数）

    与 app/services/profit/engine.py 的公式一致：Order/Refund 行计入 12 项；
    Adjustment 行由调用方按 order_id 分桶。
    """
    return (parse_decimal(g('product_sales'), D(0)) + parse_decimal(g('product_sales_tax'), D(0))
            + parse_decimal(g('postage_credits'), D(0)) + parse_decimal(g('shipping_credits_tax'), D(0))
            + parse_decimal(g('gift_wrap_credits'), D(0)) + parse_decimal(g('giftwrap_credits_tax'), D(0))
            + parse_decimal(g('promotional_rebates'), D(0)) + parse_decimal(g('promotional_rebates_tax'), D(0))
            + parse_decimal(g('marketplace_withheld_tax'), D(0))
            + parse_decimal(g('selling_fees'), D(0)) + parse_decimal(g('fba_fees'), D(0)))


def build_ods_index(db, store_id):
    """把某店铺的全部销售原始行解析一次，按 (国家, 月份) 分桶

    返回 {(country, 'YYYY-MM'): {'na': Decimal, 'ps': Decimal, 'rows': int}}
    """
    rows = db.query(RawSheetRow).filter(RawSheetRow.store_id == store_id).all()
    if not rows:
        return {}
    sheet_ids = {r.sheet_id for r in rows}
    # ★ 与管线口径一致：sales 与 未识别(None/unrecognized) 的 sheet 都会被按销售表清洗
    #   （实测 MGK 的 "SA销" sheet 早期未被识别为 sales，但数据已正确入库）
    meta = {s.id: s for s in db.query(RawSheet).filter(RawSheet.id.in_(sheet_ids)).all()
            if s.report_type in ('sales', 'unrecognized', None)}
    idx = defaultdict(lambda: {'na': D(0), 'ps': D(0), 'rows': 0})
    for r in rows:
        s = meta.get(r.sheet_id)
        if not s:
            continue
        cmap = map_header_to_canonical(r.headers_json or [])
        val = r.values_json or []

        def g(field, _c=cmap, _v=val):
            i = _c.get(field, -1)
            return _v[i] if 0 <= i < len(_v) else None

        t = normalize_transaction_type(str(g('transaction_type') or ''))
        dt = parse_date(g('date_time'))
        if dt is None or t in EXCLUDED:
            continue
        if dt.tzinfo is not None:                 # 统一去时区，避免与 naive 比较报错
            dt = dt.replace(tzinfo=None)
        # ★ 国家归属必须与管线一致：sheet 级识别 → 行级兜底（marketplace/country_code）
        #   实测 MGK 的部分沙特站数据在 sheet 级别识别不出国家，只能靠行内 marketplace 判定；
        #   若此处只用 sheet 级国家，会与 fact 表口径不一致，产生假差异。
        country = s.country or detect_country_from_row(normalize_header(r.headers_json or []),
                                                       val)
        if not country:
            continue
        key = (country, dt.strftime('%Y-%m'))
        d = idx[key]
        if t in PROFIT_TYPES:
            d['na'] += net_amazon_of(g)
            d['ps'] += parse_decimal(g('product_sales'), D(0))
        elif t == 'Adjustment':
            oid = str(g('order_id') or '').strip()
            d['na'] += parse_decimal(g('other'), D(0)) if oid else parse_decimal(g('total'), D(0))
        d['rows'] += 1
    return dict(idx)


def fact_index(db, store_id):
    """fact 表按 (国家, 月份) 聚合，与 ODS 侧同口径"""
    rows = db.query(FactSalesTransaction).filter(
        FactSalesTransaction.store_id == store_id,
        FactSalesTransaction.biz_date.isnot(None)).all()
    idx = defaultdict(lambda: {'na': D(0), 'ps': D(0), 'rows': 0})
    for r in rows:
        t = r.transaction_type
        key = (r.country, r.biz_date.strftime('%Y-%m'))
        d = idx[key]
        if t in PROFIT_TYPES:
            d['na'] += (D(str(r.product_sales or 0)) + D(str(r.product_sales_tax or 0))
                        + D(str(r.postage_credits or 0)) + D(str(r.shipping_credits_tax or 0))
                        + D(str(r.gift_wrap_credits or 0)) + D(str(r.giftwrap_credits_tax or 0))
                        + D(str(r.promo_rebate or 0)) + D(str(r.promo_rebate_tax or 0))
                        + D(str(r.marketplace_withheld_tax or 0))
                        + D(str(r.selling_fee or 0)) + D(str(r.fba_fee or 0)))
            d['ps'] += D(str(r.product_sales or 0))
        elif t == 'Adjustment':
            d['na'] += D(str(r.other_amount if r.has_order_id else r.line_total or 0))
        d['rows'] += 1
    return dict(idx)


def fact_net_amazon(db, store_id, period):
    """某店铺某月的 fact 侧 net_amazon 合计（全站点），用于与快照比对"""
    start, end = month_bounds(period)
    rows = db.query(FactSalesTransaction).filter(
        FactSalesTransaction.store_id == store_id,
        FactSalesTransaction.biz_date >= start,
        FactSalesTransaction.biz_date < end).all()
    total = D(0)
    for r in rows:
        t = r.transaction_type
        if t in PROFIT_TYPES:
            total += (D(str(r.product_sales or 0)) + D(str(r.product_sales_tax or 0))
                      + D(str(r.postage_credits or 0)) + D(str(r.shipping_credits_tax or 0))
                      + D(str(r.gift_wrap_credits or 0)) + D(str(r.giftwrap_credits_tax or 0))
                      + D(str(r.promo_rebate or 0)) + D(str(r.promo_rebate_tax or 0))
                      + D(str(r.marketplace_withheld_tax or 0))
                      + D(str(r.selling_fee or 0)) + D(str(r.fba_fee or 0)))
        elif t == 'Adjustment':
            total += D(str(r.other_amount if r.has_order_id else r.line_total or 0))
    return total


def main():
    args = sys.argv[1:]
    only_stores = set(args[args.index('--stores') + 1].split(',')) if '--stores' in args else None
    only_months = set(args[args.index('--months') + 1].split(',')) if '--months' in args else None

    db = SessionLocal()
    try:
        stores = [s for s in db.query(Store).order_by(Store.store_code).all()
                  if not only_stores or s.store_code in only_stores]
        months = sorted({r[0] for r in db.query(
            sa_func.date_format(FactSalesTransaction.biz_date, '%Y-%m')).distinct().all() if r[0]})
        if only_months:
            months = [m for m in months if m[5:7] in only_months]
        print(f"验收范围: {len(stores)} 个店铺 × {len(months)} 个月 = {len(stores)*len(months)} 组")
        print(f"月份: {', '.join(months)}\n")

        checks = mismatch = snap_ok = 0
        snap_fail = []
        t0 = time.time()
        for st in stores:
            # ★ 每店铺只解析一次原始行，显著加速
            ods = build_ods_index(db, st.id)
            fac = fact_index(db, st.id)
            for pm in months:
                keys = sorted({k for k in list(ods) + list(fac) if k[1] == pm})
                for c, m in keys:
                    checks += 1
                    o = ods.get((c, m), {'na': D(0), 'ps': D(0), 'rows': 0})
                    f_ = fac.get((c, m), {'na': D(0), 'ps': D(0), 'rows': 0})
                    if abs(o['na'] - f_['na']) >= D('0.01') or o['rows'] != f_['rows']:
                        mismatch += 1
                        print(f"  ★差异 {st.store_code} {m} {c}: "
                              f"ODS na={o['na']}({o['rows']}行) fact na={f_['na']}({f_['rows']}行)")
                # 核算 + 快照一致性校验
                try:
                    snap = calculate_snapshot(db, st.id, '', pm, created_by=None,
                                              snapshot_name=f"验收-{st.store_code}-{pm}")
                    na_fact = fact_net_amazon(db, st.id, pm)
                    if abs(na_fact - D(str(snap.net_amazon or 0))) < D('0.01'):
                        snap_ok += 1
                    else:
                        snap_fail.append(f"{st.store_code} {pm}: fact={na_fact} snap={snap.net_amazon}")
                except Exception as e:
                    snap_fail.append(f"{st.store_code} {pm}: 核算异常 {type(e).__name__}: {e}")
            print(f"  {st.store_code} 完成（对账累计 {checks} 检查点，差异 {mismatch}，"
                  f"快照 {snap_ok}）  {time.time()-t0:.0f}s", flush=True)

        print(f"\n{'='*64}")
        print(f"① 独立复算对账: {checks} 个检查点，不一致 {mismatch}  "
              f"{'✓ 全部一致（ODS 独立复算 == fact 表聚合）' if mismatch == 0 else '✗ 存在差异'}")
        print(f"② 核算快照一致性: 成功 {snap_ok}，失败 {len(snap_fail)}")
        for f_ in snap_fail[:10]:
            print('   ', f_)
        print(f"总耗时 {time.time()-t0:.1f}s")
        return 0 if mismatch == 0 and not snap_fail else 2
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
