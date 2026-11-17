"""S20 报表汇率静默覆盖人工值修复（B-08，tasks.md 4.3）

修复前：导入汇率 sheet 直接 UPDATE exchange_rate（source 无条件改 'report'），
人工维护的汇率被报表值静默覆盖，且无任何痕迹。

修复后（统一走 common/rates.upsert_rate_with_audit）：
  · existing.source=='manual' 且值变化 → 拒绝覆盖，汇率仍为人工值
    + exchange_rate_history 记 action='reject_overwrite'（old/new/source 留痕）
    + ImportRowError 记 rate_conflict + 批次 error_summary 追加说明
  · 管理员人工修改 → history 记 action='update'（old→new），OperationLog 补记旧值
  · 无人工值 → 报表正常更新（source='report'）+ history 留痕
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models import ExchangeRate, ExchangeRateHistory, Store

from conftest import build_xlsx, sql, sql_one, upload_and_wait

RATE_KEY = {'store_code': 'LMG', 'country': 'UK', 'period': '2026-06'}


def _cleanup():
    db = SessionLocal()
    try:
        st = db.query(Store).filter(Store.store_code == 'LMG').first()
        if not st:
            return
        db.query(ExchangeRateHistory).filter(
            ExchangeRateHistory.country_code == 'UK',
            ExchangeRateHistory.period_month == '2026-06').delete()
        db.query(ExchangeRate).filter(
            ExchangeRate.store_id == st.id,
            ExchangeRate.country_code == 'UK',
            ExchangeRate.period_month == '2026-06').delete()
        db.commit()
    finally:
        db.close()


class TestRateProtection:
    def test_manual_value_survives_report_import(self, client, admin_headers, tmp_path):
        """人工 UK 2026-06=9.5 → 导入含 UK 8.0 的报表 → 仍为 9.5（spec 5.1.1 R7）"""
        _cleanup()
        try:
            r = client.post('/api/v2/admin/exchange-rates', headers=admin_headers, json={
                'store': 'LMG', 'country_code': 'UK',
                'period_month': '2026-06', 'rate_to_rmb': 9.5})
            assert r.status_code == 200, r.text
            assert r.json()['ok']

            f = build_xlsx(tmp_path / 'LMG-RATE_2026-06.xlsx', [
                ('汇率', [['国家', '汇率'], ['UK', 8.0]]),
            ])
            st = upload_and_wait(client, admin_headers, f, f.name, 'LMG', '2026-06')
            assert st['status'] == 'success', st.get('error')

            er = sql_one(
                "SELECT er.* FROM exchange_rate er JOIN store s ON s.id=er.store_id "
                "WHERE s.store_code=:c AND er.country_code=:k AND er.period_month=:p",
                c='LMG', k='UK', p='2026-06')
            assert er, '汇率行丢失'
            assert abs(float(er['rate_to_rmb']) - 9.5) < 0.005, \
                f"人工值被覆盖: {er['rate_to_rmb']}"
            assert er['source'] == 'manual'

            # history 留痕：拒绝覆盖事件（old=9.5 new=8.0 source=report）
            h = sql_one(
                "SELECT * FROM exchange_rate_history "
                "WHERE country_code='UK' AND period_month='2026-06' AND action='reject_overwrite'")
            assert h, '缺少 reject_overwrite 历史'
            assert abs(float(h['old_rate']) - 9.5) < 0.005
            assert abs(float(h['new_rate']) - 8.0) < 0.005
            assert h['source'] == 'report'

            # 批次报告可见冲突告警（异常池 + error_summary）
            batch_id = st['batches'][0]['batch_id']
            rep = client.get(f'/api/v2/import/batches/{batch_id}/report',
                             headers=admin_headers).json()
            assert any(e['error_type'] == 'rate_conflict' for e in rep['errors']), \
                '异常池缺少 rate_conflict 记录'
            b = sql_one("SELECT error_summary FROM import_batch WHERE id=:i", i=batch_id)
            assert '汇率冲突已保留人工值' in (b['error_summary'] or '')
        finally:
            _cleanup()

    def test_manual_edit_traceable(self, client, admin_headers):
        """人工改人工 9.5→9.6：history 记 update(old→new)，OperationLog 补记旧值"""
        _cleanup()
        try:
            r1 = client.post('/api/v2/admin/exchange-rates', headers=admin_headers, json={
                'store': 'LMG', 'country_code': 'UK',
                'period_month': '2026-06', 'rate_to_rmb': 9.5})
            assert r1.status_code == 200, r1.text
            r2 = client.post('/api/v2/admin/exchange-rates', headers=admin_headers, json={
                'store': 'LMG', 'country_code': 'UK',
                'period_month': '2026-06', 'rate_to_rmb': 9.6})
            assert r2.status_code == 200, r2.text
            body = r2.json()
            assert body['ok'] and body.get('conflict') is False
            assert abs(body['rate'] - 9.6) < 0.005

            ups = sql(
                "SELECT * FROM exchange_rate_history "
                "WHERE country_code='UK' AND period_month='2026-06' AND action='update' "
                "ORDER BY id")
            assert len(ups) >= 2, '应有两条 update 历史（首次写入 + 修改）'
            last = ups[-1]
            assert abs(float(last['old_rate']) - 9.5) < 0.005
            assert abs(float(last['new_rate']) - 9.6) < 0.005
            assert last['source'] == 'manual'

            log = sql(
                "SELECT * FROM operation_log WHERE action='upsert_rate' "
                "ORDER BY id DESC LIMIT 1")[0]
            assert 'old=9.5' in (log['detail'] or ''), f"OperationLog 未补记旧值: {log['detail']}"
        finally:
            _cleanup()

    def test_report_updates_when_no_manual_value(self, client, admin_headers, tmp_path):
        """无人工值 → 报表正常写入（source='report'）且 history 留痕"""
        _cleanup()
        try:
            f = build_xlsx(tmp_path / 'LMG-RATE2_2026-06.xlsx', [
                ('汇率', [['国家', '汇率'], ['UK', 7.3]]),
            ])
            st = upload_and_wait(client, admin_headers, f, f.name, 'LMG', '2026-06')
            assert st['status'] == 'success', st.get('error')

            er = sql_one(
                "SELECT er.* FROM exchange_rate er JOIN store s ON s.id=er.store_id "
                "WHERE s.store_code='LMG' AND er.country_code='UK' AND er.period_month='2026-06'")
            assert er and abs(float(er['rate_to_rmb']) - 7.3) < 0.005
            assert er['source'] == 'report'
            h = sql_one(
                "SELECT * FROM exchange_rate_history "
                "WHERE country_code='UK' AND period_month='2026-06' AND action='update'")
            assert h and abs(float(h['new_rate']) - 7.3) < 0.005
        finally:
            _cleanup()

