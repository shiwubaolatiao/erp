"""S16 超龄仓储费时间过滤回归（B-02 修复 / tasks.md 3.1）

修复前：summary / fee-composition / query-fees 三接口的超龄费均无时间过滤
（FactFeeAgedStorage 无 biz_date 列，等值/区间条件都被跳过）→ 永远返回历史累计。

期望值人工独立推导：
  构造 2026-01 行 100.00（UK）与 2026-02 行 50.00（UK）→
    查 2026-01：三接口分别为 100.00（原币）/ 900.00（100×UK汇率9.0 RMB）/ 100.00
    查 2026-02：50.00 / 450.00 / 50.00（随查询月份变化，不再累计）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models import Store, FactFeeAgedStorage

from conftest import PERIOD


class TestAgedStorageFilter:
    """三接口超龄费一致 + 随查询月份变化（B-02 验收）"""

    def _setup_rows(self, db, store_id):
        db.query(FactFeeAgedStorage).filter(FactFeeAgedStorage.store_id == store_id).delete()
        db.add_all([
            FactFeeAgedStorage(store_id=store_id, asin='B0AAAAAAA1', country='UK',
                               snapshot_date='2026-01',
                               amount_charged=100.00, batch_id=1, raw_row_id=1),
            FactFeeAgedStorage(store_id=store_id, asin='B0AAAAAAA1', country='UK',
                               snapshot_date='2026-02',
                               amount_charged=50.00, batch_id=1, raw_row_id=1),
        ])
        db.commit()

    def test_three_endpoints_consistent_and_month_scoped(self, client, admin_headers, e2e):
        db = SessionLocal()
        st = None
        try:
            st = db.query(Store).filter(Store.store_code == 'LMG').first()
            assert st, 'LMG 店铺未初始化'
            self._setup_rows(db, st.id)

            for pm, usd_val, rmb_val in (('2026-01', 100.00, 900.00),
                                         ('2026-02', 50.00, 450.00)):
                # ① /dashboard/summary（原币）
                s = client.get('/api/v2/dashboard/summary', headers=admin_headers,
                               params={'store': 'LMG', 'period_month': pm}).json()
                assert abs(s['aged_storage_fee'] - usd_val) < 0.005, \
                    f"summary {pm}: {s['aged_storage_fee']} != {usd_val}"
                # ② /dashboard/fee-composition（RMB = 原币 × UK 9.0）
                f = client.get('/api/v2/dashboard/fee-composition', headers=admin_headers,
                               params={'store': 'LMG', 'period_month': pm}).json()
                aged = next(e['value'] for e in f['expenses'] if e['name'] == '超龄仓储费')
                assert abs(aged - rmb_val) < 0.05, \
                    f"fee-composition {pm}: {aged} != {rmb_val}"
                # ③ /query/fees（原币）
                q = client.get('/api/v2/query/fees', headers=admin_headers,
                               params={'store': 'LMG', 'period_month': pm}).json()
                assert abs(q['aged_storage_fee'] - usd_val) < 0.005, \
                    f"query/fees {pm}: {q['aged_storage_fee']} != {usd_val}"
        finally:
            if st is not None:
                db.query(FactFeeAgedStorage).filter(
                    FactFeeAgedStorage.store_id == st.id).delete()
                db.commit()
            db.close()

    def test_summary_aged_uses_snapshot_date_exact(self, client, admin_headers, e2e):
        """清洗后值（YYYY-MM）等值命中；组10.3 切等值后此口径即生产口径"""
        db = SessionLocal()
        st = None
        try:
            st = db.query(Store).filter(Store.store_code == 'LMG').first()
            db.query(FactFeeAgedStorage).filter(FactFeeAgedStorage.store_id == st.id).delete()
            db.add(FactFeeAgedStorage(store_id=st.id, asin='B0AAAAAAA1', country='UK',
                                      snapshot_date='2026-01',
                                      amount_charged=7.77, batch_id=1, raw_row_id=1))
            db.commit()
            q = client.get('/api/v2/query/fees', headers=admin_headers,
                           params={'store': 'LMG', 'period_month': '2026-01'}).json()
            assert abs(q['aged_storage_fee'] - 7.77) < 0.005   # 等值匹配精确命中
            s = client.get('/api/v2/dashboard/summary', headers=admin_headers,
                           params={'store': 'LMG', 'period_month': '2026-01'}).json()
            assert abs(s['aged_storage_fee'] - 7.77) < 0.005
        finally:
            if st is not None:
                db.query(FactFeeAgedStorage).filter(
                    FactFeeAgedStorage.store_id == st.id).delete()
                db.commit()
            db.close()