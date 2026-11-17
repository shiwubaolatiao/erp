"""S11 数据一致性（TC-CON-001~005）★ 验收基准：ODS 独立复算 == fact == 快照

方法：绕开导入管线，直接解析 raw_sheet_row.values_json 对 12 个利润字段独立求和，
与 fact 表聚合、快照 net_amazon 三方交叉比对；净利润用利润引擎公式对快照组件复算。
"""
import sys
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import EXP
from app.services.importer.parser import map_header_to_canonical, normalize_transaction_type, parse_date
from app.services.profit.engine import compute_net_profit

# 与 net_amazon 公式对应的 11 个 fact 字段（除 other_amount 特殊处理外）
FIELDS_11 = ['product_sales', 'product_sales_tax', 'postage_credits',
             'shipping_credits_tax', 'gift_wrap_credits', 'giftwrap_credits_tax',
             'promo_rebate', 'promo_rebate_tax', 'marketplace_withheld_tax',
             'selling_fee', 'fba_fee']
# fact 列名 → 解析器 canonical 字段名（map_header_to_canonical 返回的键）
CANON_ALIAS = {'selling_fee': 'selling_fees', 'fba_fee': 'fba_fees'}
PROFIT_TYPES = {'Order', 'Refund'}


def _d(v):
    return Decimal(str(v)) if v is not None else Decimal(0)


class TestConsistency:
    def test_ods_vs_fact(self, e2e):  # TC-CON-001 ODS 独立复算 == fact 聚合
        from conftest import sql
        ods_rows = sql("""
            SELECT r.headers_json, r.values_json FROM raw_sheet_row r
            JOIN raw_sheet s ON s.id = r.sheet_id
            JOIN import_batch b ON b.id = r.batch_id
            JOIN store st ON st.id = b.store_id
            WHERE st.store_code='LMG' AND s.report_type='sales'
              AND b.file_name='LMG-EU_2026-01.xlsx'""")
        ods_sum = {f: Decimal(0) for f in FIELDS_11}
        n_ods = 0
        for row in ods_rows:
            hdrs = json.loads(row['headers_json'])
            vals = json.loads(row['values_json'])
            cmap = map_header_to_canonical(hdrs)
            t_idx = cmap.get('transaction_type', -1)
            d_idx = cmap.get('date_time', -1)
            # 独立复现清洗规则：业务日期不可解析的行不参与利润（与管线 bad_date 拒绝一致）
            if d_idx < 0 or parse_date(str(vals[d_idx] or '')) is None:
                continue
            # 类型按管线规则归一后再过滤（ODS 存原值，如 Bestellung→Order）
            if t_idx < 0 or normalize_transaction_type(str(vals[t_idx] or '')) not in PROFIT_TYPES:
                continue
            n_ods += 1
            for f in FIELDS_11:
                i = cmap.get(CANON_ALIAS.get(f, f), -1)
                if 0 <= i < len(vals) and vals[i] not in (None, ''):
                    ods_sum[f] += _d(str(vals[i]).replace(',', '.'))
        fact_rows = sql("""
            SELECT f.* FROM fact_sales_transaction f
            JOIN store st ON st.id=f.store_id
            WHERE st.store_code='LMG' AND f.biz_date >= '2026-01-01'
              AND f.biz_date < '2026-02-01' AND f.transaction_type IN ('Order','Refund')""")
        assert len(fact_rows) == n_ods, f"参与复算行数不一致 ODS={n_ods} fact={len(fact_rows)}"
        for f in FIELDS_11:
            fact_sum = sum(_d(r[f]) for r in fact_rows)
            assert abs(ods_sum[f] - fact_sum) < Decimal('0.001'), \
                f"{f}: ODS 复算={ods_sum[f]} fact={fact_sum}"

    def test_fact_vs_snapshot(self, calc):  # TC-CON-002 fact 聚合 == 快照 net_amazon
        from conftest import sql
        for country, key in (('UK', 'uk'), ('DE', 'de')):
            snap_na = Decimal(str(calc[key]['net_amazon']))
            rows = sql("""
                SELECT f.product_sales, f.product_sales_tax, f.postage_credits,
                       f.shipping_credits_tax, f.gift_wrap_credits,
                       f.giftwrap_credits_tax, f.promo_rebate, f.promo_rebate_tax,
                       f.marketplace_withheld_tax, f.selling_fee, f.fba_fee,
                       f.other_amount, f.line_total, f.has_order_id,
                       f.transaction_type
                FROM fact_sales_transaction f
                JOIN store st ON st.id=f.store_id
                WHERE st.store_code='LMG' AND f.country=:c
                  AND f.biz_date >= '2026-01-01' AND f.biz_date < '2026-02-01'""", c=country)
            total = Decimal(0)
            for r in rows:
                if r['transaction_type'] in PROFIT_TYPES:
                    total += sum(_d(r[f]) for f in FIELDS_11)
                elif r['transaction_type'] == 'Adjustment':
                    total += _d(r['other_amount'] if r['has_order_id'] else r['line_total'])
            assert abs(total - snap_na) < Decimal('0.001'), \
                f"{country}: fact 聚合={total} 快照={snap_na}"

    def test_snapshot_item_formula(self, calc):  # TC-CON-003 净利润公式独立复算
        from conftest import sql
        for key in ('uk', 'de'):
            snap = calc[key]
            er = Decimal(str(snap['exchange_rate']))
            items = sql("SELECT * FROM dws_profit_snapshot_item WHERE snapshot_id=:s",
                        s=snap['snapshot_id'])
            assert items
            total = Decimal(0)
            for it in items:
                # 引擎作公式 oracle：net_amazon×er − 成本RMB − 运费RMB − (广告+仓储(含超龄)+退货+入库+移除)×er
                net = compute_net_profit(
                    _d(it['net_amazon']), *[_d(0)] * 11, er,
                    _d(it['product_cost_rmb']), _d(it['freight_cost_rmb']),
                    _d(it['ad_spend']), _d(it['storage_fee']) + _d(it['aged_storage_fee']),
                    _d(it['returns_fee']), _d(it['inbound_fee']), _d(it['removal_fee']))
                assert abs(net - _d(it['net_profit_rmb'])) < Decimal('0.01'), \
                    f"{it['sku']}: 复算={net} 快照={it['net_profit_rmb']}"
                total += _d(it['net_profit_rmb'])
            assert abs(total - Decimal(str(snap['net_profit_rmb']))) < Decimal('0.01'), \
                f"{key}: 明细合计={total} 快照汇总={snap['net_profit_rmb']}"

    def test_known_anchors(self, calc):  # TC-CON-004 已知值锚点
        assert abs(calc['uk']['net_amazon'] - EXP['uk_net_amazon']) < 0.005
        assert abs(calc['de']['net_amazon'] - EXP['de_net_amazon']) < 0.005

    def test_query_fees_vs_summary_five_fees(self, client, admin_headers, e2e):
        """TC-CON-006 /query/fees 与 /dashboard/summary 五类费用逐项相等（B-05/B-02 验收）

        覆盖两类时间语义：月份列前缀匹配（仓储/超龄/退货，含脏值时间戳）
        与日期列区间过滤（移除 request_date / 入库 transaction_date）。
        期望值（人工设定，两接口必须同时命中）：
          2026-01：storage 12.34 / aged 7.77 / returns 8.88 / removal 9.99 / inbound 10.10
          2026-02：storage 56.78，其余 0（月份隔离，不累计）
        """
        from app.core.database import SessionLocal
        from app.models import (Store, FactFeeStorage, FactFeeAgedStorage, FactFeeReturn,
                                FactFeeRemoval, FactFeeInboundConfig)
        fee_models = (FactFeeStorage, FactFeeAgedStorage, FactFeeReturn,
                      FactFeeRemoval, FactFeeInboundConfig)
        db = SessionLocal()
        st = None
        try:
            st = db.query(Store).filter(Store.store_code == 'LMG').first()
            assert st, 'LMG 店铺未初始化'
            for m in fee_models:
                db.query(m).filter(m.store_id == st.id).delete()
            db.add_all([
                # 月份列（组10.3 已切等值匹配；值经组10.1 治理清洗为 YYYY-MM）
                FactFeeStorage(store_id=st.id, asin='B0AAAAAAA1', country='UK',
                               month_of_charge='2026-01',
                               estimated_monthly_storage_fee=12.34, batch_id=1, raw_row_id=1),
                FactFeeStorage(store_id=st.id, asin='B0AAAAAAA1', country='UK',
                               month_of_charge='2026-02',
                               estimated_monthly_storage_fee=56.78, batch_id=1, raw_row_id=1),
                FactFeeAgedStorage(store_id=st.id, asin='B0AAAAAAA1', country='UK',
                                   snapshot_date='2026-01',
                                   amount_charged=7.77, batch_id=1, raw_row_id=1),
                FactFeeReturn(store_id=st.id, asin='B0AAAAAAA1', country='UK',
                              month_of_charge='2026-01',
                              sku_returns_fee=8.88, batch_id=1, raw_row_id=1),
                # 日期列（区间过滤语义）
                FactFeeRemoval(store_id=st.id, asin='B0AAAAAAA1', country='UK',
                               request_date=datetime(2026, 1, 20, 10, 0, 0),
                               removal_fee=9.99, batch_id=1, raw_row_id=1),
                FactFeeInboundConfig(store_id=st.id, asin='B0AAAAAAA1', country='UK',
                                     transaction_date=datetime(2026, 1, 25, 10, 0, 0),
                                     total_fee=10.10, batch_id=1, raw_row_id=1),
            ])
            db.commit()

            cases = (
                ('2026-01', {'storage_fee': 12.34, 'aged_storage_fee': 7.77,
                             'returns_fee': 8.88, 'removal_fee': 9.99, 'inbound_fee': 10.10}),
                ('2026-02', {'storage_fee': 56.78, 'aged_storage_fee': 0.0,
                             'returns_fee': 0.0, 'removal_fee': 0.0, 'inbound_fee': 0.0}),
            )
            for pm, expect in cases:
                q = client.get('/api/v2/query/fees', headers=admin_headers,
                               params={'store': 'LMG', 'period_month': pm}).json()
                s = client.get('/api/v2/dashboard/summary', headers=admin_headers,
                               params={'store': 'LMG', 'period_month': pm}).json()
                for k, val in expect.items():
                    assert abs(q[k] - val) < 0.005, f"query/fees {pm} {k}: {q[k]} != {val}"
                    assert abs(s[k] - val) < 0.005, f"summary {pm} {k}: {s[k]} != {val}"
                    assert abs(q[k] - s[k]) < 0.005, \
                        f"{pm} {k} 两接口不一致: query={q[k]} summary={s[k]}"
        finally:
            if st is not None:
                for m in fee_models:
                    db.query(m).filter(m.store_id == st.id).delete()
                db.commit()
            db.close()

    def test_rate_fallback_four_tiers(self, client, admin_headers, e2e):  # TC-CON-005 汇率四级回退实测
        from app.core.database import SessionLocal
        from app.models import ExchangeRate, Store
        from app.services.profit.engine import get_exchange_rate
        # ② 先写后读：全局汇率经 API 写入后，再开会话查询（避免 REPEATABLE READ 旧快照）
        r = client.post('/api/v2/admin/exchange-rates', headers=admin_headers, json={
            'country_code': 'FR', 'period_month': '2026-03', 'rate_to_rmb': 7.77})
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            lmg = db.query(Store).filter(Store.store_code == 'LMG').first()
            # ① 店铺专属精确月：导入的 (LMG, UK, 2026-01) = 9.0
            er = get_exchange_rate(db, 'UK', store_id=lmg.id, period_month='2026-01',
                                   ExchangeRateModel=ExchangeRate)
            assert er == Decimal('9.0')
            # ② 全局精确月：FR 2026-03=7.77（LMG 无 FR 数据）
            er = get_exchange_rate(db, 'FR', store_id=lmg.id, period_month='2026-03',
                                   ExchangeRateModel=ExchangeRate)
            assert er == Decimal('7.77')
            # ③ 店铺最近月：LMG UK 只有 2026-01，查 2026-09 → 回退最近月 9.0
            er = get_exchange_rate(db, 'UK', store_id=lmg.id, period_month='2026-09',
                                   ExchangeRateModel=ExchangeRate)
            assert er == Decimal('9.0')
            # ④ 内置默认表：SE 无任何汇率记录 → 0.65
            er = get_exchange_rate(db, 'SE', store_id=lmg.id, period_month='2026-09',
                                   ExchangeRateModel=ExchangeRate)
            assert er == Decimal('0.65')
        finally:
            db.close()
