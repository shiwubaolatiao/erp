"""S6+ 多站点汇总核算回归（B-01 修复 / tasks.md 2.6）— 店铺级==Σ站点级、逐站点汇率、兜底提示、落库自检

期望值人工独立推导（基准与 conftest.EXP 同源，不引用被测代码）：
  LMG 2026-01 店铺级（UK 9.0 + DE 8.5 逐站点折算后求和）：
    net_amazon        = 62.50 + 180.00 = 242.50
    net_profit_rmb    = 544.50 + 1507.00 = 2051.50
    product_sales_rmb = 904.50 + 1700.00 = 2604.50
    加权汇率          = 2604.50 / 300.50 = 8.6672（ps 加权，仅展示用）
  逐 SKU×站点：SKU-A(UK) 18×9−10−2 = 150.00；SKU-D(DE) 180×8.5−20−3 = 1507.00
    ★ 修复前（country or 'US' 单汇率）：SKU-D 被按 6.8 折算成 1201.00，本组用例必挂
  汇率缺失兜底（独立 TFR 店铺，2026-05 无任何汇率行）：
    UK/FR 均落四级回退第④级 DEFAULT_RATES（9.0 / 7.5）→
    rate_source='default_fallback' + rate_detail._missing 提示人工维护
"""
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models import (
    Store, RawSheetRow, FactSalesTransaction,
    DwsProfitSnapshot, DwsProfitSnapshotItem, CostMissingAlert,
)
from app.services.accounting.service import (
    calculate_snapshot, _self_check, SnapshotConsistencyError,
)

from conftest import PERIOD, calc_and_wait


@pytest.fixture(scope='session')
def store_calc(client, admin_headers, e2e, calc):
    """LMG 2026-01 店铺级核算（country 不传 = 全站点汇总）"""
    return calc_and_wait(client, admin_headers,
                         {'store': 'LMG', 'period_month': PERIOD})


class TestStoreLevelSnapshot:
    """B-01 核心：店铺级 == Σ站点级（逐站点汇率折算，差异 ≤ 0.01）"""

    def test_equals_sum_of_country_levels(self, store_calc, calc):  # B-01-1
        uk, de = calc['uk'], calc['de']
        assert abs(store_calc['net_profit_rmb']
                   - (uk['net_profit_rmb'] + de['net_profit_rmb'])) <= 0.01
        assert abs(store_calc['net_amazon']
                   - (uk['net_amazon'] + de['net_amazon'])) <= 0.01
        assert abs(store_calc['product_sales_rmb']
                   - (uk['product_sales_rmb'] + de['product_sales_rmb'])) <= 0.01

    def test_exact_values(self, store_calc):  # B-01-2
        assert abs(store_calc['net_amazon'] - 242.50) < 0.005
        assert abs(store_calc['net_profit_rmb'] - 2051.50) < 0.005
        assert abs(store_calc['product_sales_rmb'] - 2604.50) < 0.005
        assert store_calc['order_count'] == 3     # 202-AAA / 202-BBB / 202-DDD
        assert store_calc['order_qty'] == 3       # SKU-A 1 + SKU-B 1 + SKU-D 1
        assert abs(store_calc['exchange_rate'] - 8.6672) < 0.00005  # ps 加权平均

    def test_rate_source_per_country(self, client, admin_headers, store_calc):  # B-01-3
        body = client.get(f"/api/v2/accounting/snapshots/{store_calc['snapshot_id']}",
                          headers=admin_headers).json()
        s = body['snapshot']
        assert s['rate_source'] == 'per_country'
        assert s['rate_missing_countries'] == []
        assert float(s['rate_detail']['UK']) == 9.0
        assert float(s['rate_detail']['DE']) == 8.5

    def test_items_carry_country_and_rate(self, client, admin_headers, store_calc):  # B-01-4
        body = client.get(f"/api/v2/accounting/snapshots/{store_calc['snapshot_id']}",
                          headers=admin_headers).json()
        items = {(i['sku'], i['country']): i for i in body['items']}
        assert set(items) == {('SKU-A', 'UK'), ('SKU-B', 'UK'), ('SKU-D', 'DE')}
        a, d = items[('SKU-A', 'UK')], items[('SKU-D', 'DE')]
        assert a['exchange_rate'] == 9.0
        assert abs(a['net_profit_rmb'] - 150.00) < 0.005
        assert d['exchange_rate'] == 8.5
        # 若 B-01 未修（统一按 US 6.8 折算）：SKU-D = 180×6.8−23 = 1201.00
        assert abs(d['net_profit_rmb'] - 1507.00) < 0.005


TFR_CODE, TFR_MONTH = 'TFR-TEST', '2026-05'


class TestFallbackRate:
    """汇率缺失站点：DEFAULT_RATES 兜底 + default_fallback 标记 + _missing 提示

    独立 TFR 店铺直插 fact（导入链路已被 test_05 覆盖，此处只测核算回退），
    2026-05 无任何汇率行 → UK/FR 均落第④级（default）。
    """

    def _teardown_by_store(self, db, store_id):
        snap_ids = [r.id for r in db.query(DwsProfitSnapshot.id).filter(
            DwsProfitSnapshot.store_id == store_id).all()]
        if snap_ids:
            db.query(DwsProfitSnapshotItem).filter(
                DwsProfitSnapshotItem.snapshot_id.in_(snap_ids)).delete()
            db.query(DwsProfitSnapshot).filter(
                DwsProfitSnapshot.id.in_(snap_ids)).delete()
        db.query(CostMissingAlert).filter(CostMissingAlert.store_id == store_id).delete()
        db.query(FactSalesTransaction).filter(
            FactSalesTransaction.store_id == store_id).delete()
        db.query(Store).filter(Store.id == store_id).delete()
        db.commit()

    def test_default_fallback_marks_missing(self, e2e):
        db = SessionLocal()
        st = None
        try:
            # 幂等：上次异常残留先清
            for old in db.query(Store).filter(Store.store_code == TFR_CODE).all():
                self._teardown_by_store(db, old.id)
            raw = db.query(RawSheetRow).first()   # 借用 e2e 原始行作 FK（fact 按店铺隔离）
            assert raw, '无原始行（依赖 e2e fixture）'
            st = Store(store_code=TFR_CODE, store_name='多站点兜底测试店', region='EU')
            db.add(st)
            db.flush()
            db.add_all([
                FactSalesTransaction(
                    store_id=st.id, country='FR', raw_row_id=raw.id, batch_id=raw.batch_id,
                    biz_date=datetime(2026, 5, 10), transaction_type='Order',
                    has_order_id=True, order_id='202-TFR-FR',
                    sku='SKU-TFR1', normalized_sku='SKU-TFR1', asin='B0TFRTEST01',
                    quantity=1, product_sales=100, selling_fee=-10, fba_fee=-5,
                    line_total=85),
                FactSalesTransaction(
                    store_id=st.id, country='UK', raw_row_id=raw.id, batch_id=raw.batch_id,
                    biz_date=datetime(2026, 5, 11), transaction_type='Order',
                    has_order_id=True, order_id='202-TFR-UK',
                    sku='SKU-TFR2', normalized_sku='SKU-TFR2', asin='B0TFRTEST02',
                    quantity=1, product_sales=40, selling_fee=-4, fba_fee=-3,
                    line_total=33),
            ])
            db.commit()

            snap = calculate_snapshot(db, st.id, '', TFR_MONTH)
            assert snap.country == ''
            assert snap.rate_source == 'default_fallback'
            rd = snap.rate_detail
            assert rd['_missing'] == ['FR', 'UK']      # sorted
            assert float(rd['FR']) == 7.5              # DEFAULT_RATES
            assert float(rd['UK']) == 9.0
            # 净利润：FR 85×7.5=637.50 + UK 33×9.0=297.00（无成本记录 → 不扣成本）
            assert abs(float(snap.net_amazon) - 118.00) < 0.005
            assert abs(float(snap.net_profit_rmb) - 934.50) < 0.005
            # ps 加权 = (100×7.5 + 40×9.0) / 140 = 7.9286
            assert abs(float(snap.exchange_rate) - 7.9286) < 0.00005
            assert snap.order_count == 2
            items = db.query(DwsProfitSnapshotItem).filter(
                DwsProfitSnapshotItem.snapshot_id == snap.id).all()
            by_c = {i.country: i for i in items}
            assert float(by_c['FR'].exchange_rate) == 7.5
            assert float(by_c['UK'].exchange_rate) == 9.0
            assert snap.cost_complete is False         # 两 SKU 均无成本记录
            assert snap.cost_incomplete_skus == 2
        finally:
            if st is not None:
                self._teardown_by_store(db, st.id)
            db.close()


class TestSelfCheck:
    """落库前自检（D-04 防线）：明细/汇总不一致 → SnapshotConsistencyError（事务回滚）"""

    @staticmethod
    def _item(profit, net_amazon):
        return {'net_profit_rmb': Decimal(profit), 'net_amazon': Decimal(net_amazon)}

    def test_rejects_row_count_mismatch(self):
        with pytest.raises(SnapshotConsistencyError, match='明细行数'):
            _self_check([self._item('1', '1')], [{}, {}, {}],
                        {'net_profit_rmb': Decimal('1'), 'net_amazon': Decimal('1')}, 0)

    def test_rejects_profit_mismatch(self):
        with pytest.raises(SnapshotConsistencyError, match='净利润'):
            _self_check([self._item('10', '5')], [{}],
                        {'net_profit_rmb': Decimal('9'), 'net_amazon': Decimal('5')}, 0)

    def test_rejects_net_amazon_mismatch(self):
        with pytest.raises(SnapshotConsistencyError, match='net_amazon'):
            _self_check([self._item('10', '5')], [{}],
                        {'net_profit_rmb': Decimal('10'), 'net_amazon': Decimal('4')}, 0)

    def test_accepts_consistent(self):
        _self_check([self._item('10', '5')], [{}],
                    {'net_profit_rmb': Decimal('10'), 'net_amazon': Decimal('5')}, 7)