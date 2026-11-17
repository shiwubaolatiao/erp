"""S5 导入链路端到端（TC-IMP-001~015）— 上传→ODS→DWD→批次→锁结"""
import pytest

from conftest import EXP, PERIOD, UK_SALES_ROWS, PRODUCT_ROWS


@pytest.mark.usefixtures('e2e')
class TestImportPipeline:
    def test_task_success_and_counts(self, e2e):  # TC-IMP-001
        st = e2e['task_lmg']
        assert st['status'] == 'success'
        batches = {b['file_name']: b for b in st['batches']}
        b = batches['LMG-EU_2026-01.xlsx']
        # 成功行：UK 3（1 行坏日期进异常池）+ DE 1 + 产品 3 + 汇率 2 = 9；失败 1 → 部分成功
        assert b['success'] == 9, f"实际 {b}"
        assert b['failed'] == 1
        assert b['status'] in ('success', 'partial')

    def test_ods_immutable(self):  # TC-IMP-002 ODS 原始层落库与原值保留
        import json as _json
        from conftest import sql
        rows = sql("""
            SELECT r.sheet_name, r.row_no, r.headers_json, r.values_json
            FROM raw_sheet_row r JOIN raw_sheet s ON s.id = r.sheet_id
            WHERE s.sheet_name = 'DE销售' ORDER BY r.row_no""")
        assert len(rows) == 1
        hdr = _json.loads(rows[0]['headers_json'])
        vals = _json.loads(rows[0]['values_json'])
        # ODS 保存的是德语原表头与原值（未归一）
        assert 'Datum/Uhrzeit' in hdr and 'Umsätze' in hdr
        i_type = hdr.index('type')
        assert str(vals[i_type]) == 'Bestellung'
        # UK sheet（仅 LMG 主文件）原始数据行 = 4（含坏日期行）
        n = sql("""SELECT COUNT(*) c FROM raw_sheet_row r
                   JOIN raw_sheet s ON s.id=r.sheet_id
                   JOIN import_batch b ON b.id=s.batch_id
                   WHERE s.sheet_name='UK销售' AND b.file_name='LMG-EU_2026-01.xlsx'""")[0]['c']
        assert n == len(UK_SALES_ROWS)

    def test_dwd_sales_correctness(self):  # TC-IMP-003 fact 落库与金额正确
        from conftest import sql
        rows = sql("""
            SELECT f.country, f.transaction_type, f.order_id, f.sku, f.asin,
                   f.quantity, f.product_sales, f.selling_fee, f.fba_fee
            FROM fact_sales_transaction f
            JOIN store st ON st.id = f.store_id WHERE st.store_code='LMG'
              AND f.biz_date >= '2026-01-01' AND f.biz_date < '2026-02-01'
            ORDER BY f.order_id, f.transaction_type""")
        assert len(rows) == 4  # UK 3 + DE 1（坏日期行不入 fact）
        ps_sum = sum(float(r['product_sales']) for r in rows)
        assert abs(ps_sum - 300.50) < 0.001
        refund = [r for r in rows if r['transaction_type'] == 'Refund']
        assert len(refund) == 1 and refund[0]['quantity'] == -1

    def test_type_normalized_in_db(self):  # TC-IMP-004 Bestellung→Order
        from conftest import sql
        row = sql("SELECT transaction_type FROM fact_sales_transaction "
                  "WHERE order_id='202-DDD'")[0]
        assert row['transaction_type'] == 'Order'

    def test_biz_date_from_row_not_filename(self):  # TC-IMP-005 业务日期归月
        from conftest import sql
        rows = sql("""SELECT biz_date FROM fact_sales_transaction f
                      JOIN store st ON st.id=f.store_id WHERE st.store_code='LMG'""")
        dates = {str(r['biz_date'])[:10] for r in rows}
        assert {'2026-01-05', '2026-01-08', '2026-01-15', '2026-01-01',
                '2026-02-10'} <= dates

    def test_bad_row_into_error_pool(self, e2e):  # TC-IMP-006 异常行进异常池
        from conftest import sql
        batch_id = e2e['lmg_batch_ids'][0]
        errs = sql("SELECT error_type, error_message FROM import_row_error "
                   "WHERE batch_id=:b", b=batch_id)
        assert len(errs) == 1
        assert errs[0]['error_type'] == 'bad_date'

    def test_sheet_detection(self, e2e):  # TC-IMP-007 sheet 类型/国家/表头识别
        from conftest import sql
        sheets = sql("""SELECT s.sheet_name, s.report_type, s.country,
                               s.detected_header_row, s.row_count
                        FROM raw_sheet s JOIN import_batch b ON b.id=s.batch_id
                        WHERE b.file_name='LMG-EU_2026-01.xlsx'""")
        m = {s['sheet_name']: s for s in sheets}
        assert m['UK销售']['report_type'] == 'sales'
        assert m['UK销售']['country'] == 'UK'
        assert m['DE销售']['report_type'] == 'sales'
        assert m['DE销售']['country'] == 'DE'
        assert m['产品']['report_type'] == 'product'
        assert m['汇率']['report_type'] == 'exchange_rate'
        # detected_header_row 1 基 = 1（0 基 0）
        assert m['UK销售']['detected_header_row'] == 1

    def test_header_at_row10(self, e2e):  # TC-IMP-008 表头在第 10 行的报表
        from conftest import sql
        s = sql("""SELECT s.detected_header_row, s.row_count FROM raw_sheet s
                   JOIN import_batch b ON b.id=s.batch_id
                   WHERE b.file_name='MGT-2026-01.xlsx' AND s.sheet_name='UK销售'""")[0]
        assert s['detected_header_row'] == 10
        # MGT 数据正确入库
        f = sql("""SELECT f.product_sales FROM fact_sales_transaction f
                   JOIN store st ON st.id=f.store_id WHERE st.store_code='MGT'""")
        assert len(f) == 1 and float(f[0]['product_sales']) == 40.00

    def test_product_cost_history(self):  # TC-IMP-009 产品表→成本版本
        import json as _json
        from conftest import sql
        costs = sql("""SELECT c.asin, c.sku, c.cost_rmb, c.freight_json
                       FROM product_cost_history c JOIN store st ON st.id=c.store_id
                       WHERE st.store_code='LMG' AND c.period=:p
                         AND c.source_type='report'""", p=PERIOD)
        m = {c['sku']: c for c in costs}
        assert set(m) == {'SKU-A', 'SKU-B', 'SKU-D'}
        assert float(m['SKU-A']['cost_rmb']) == 10
        assert (_json.loads(m['SKU-A']['freight_json'])
                if isinstance(m['SKU-A']['freight_json'], str)
                else m['SKU-A']['freight_json']).get('default') == 2
        # 三元组主档已建
        prods = sql("""SELECT p.asin, p.status FROM store_product p
                       JOIN store st ON st.id=p.store_id WHERE st.store_code='LMG'""")
        assert {p['asin'] for p in prods} >= {'B0AAAAAAA1', 'B0BBBBBBB2', 'B0DDDDDDD4'}

    def test_exchange_rates_stored(self):  # TC-IMP-010 汇率落库
        from conftest import sql
        rates = sql("""SELECT e.country_code, e.rate_to_rmb, e.source FROM exchange_rate e
                       JOIN store st ON st.id=e.store_id
                       WHERE st.store_code='LMG' AND e.period_month=:p""", p=PERIOD)
        m = {r['country_code']: r for r in rates}
        assert float(m['UK']['rate_to_rmb']) == 9.0
        assert float(m['DE']['rate_to_rmb']) == 8.5
        assert m['UK']['source'] == 'report'

    def test_unsupported_file_type(self, client, admin_headers, tmp_path):  # TC-IMP-011
        p = tmp_path / 'bad.txt'
        p.write_text('not a report', encoding='utf-8')
        with open(p, 'rb') as f:
            r = client.post('/api/v2/import/upload', headers=admin_headers,
                            files={'files': ('bad.txt', f, 'text/plain')})
        assert r.status_code == 400
        assert '不支持' in r.json()['detail']

    def test_batch_report(self, client, admin_headers, e2e):  # TC-IMP-012
        batch_id = e2e['lmg_batch_ids'][0]
        r = client.get(f'/api/v2/import/batches/{batch_id}/report', headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert len(body['sheets']) == 4
        assert body['failed_rows'] == 1
        assert any(e['error_type'] == 'bad_date' for e in body['errors'])

    def test_lock_unlock(self, client, admin_headers, limited, e2e):  # TC-IMP-013
        batch_id = e2e['mgt_batch_id']
        assert client.post(f'/api/v2/import/batches/{batch_id}/lock',
                           headers=admin_headers).json()['is_locked'] is True
        # 非管理员不能解锁
        r = client.post(f'/api/v2/import/batches/{batch_id}/unlock',
                        headers=limited['headers'])
        assert r.status_code == 403
        assert client.post(f'/api/v2/import/batches/{batch_id}/unlock',
                           headers=admin_headers).json()['is_locked'] is False
        # 操作留痕
        logs = client.get('/api/v2/admin/operation-logs', headers=admin_headers).json()['items']
        actions = {l['action'] for l in logs}
        assert {'lock_batch', 'unlock_batch'} <= actions

    def test_task_404(self, client, admin_headers):  # TC-IMP-014
        assert client.get('/api/v2/import/tasks/does-not-exist',
                          headers=admin_headers).status_code == 404

    def test_supported_meta(self, client):  # TC-IMP-015
        r = client.get('/api/v2/import/supported')
        assert r.status_code == 200
        body = r.json()
        assert 'LMG' in body['stores']
        assert 'sales' in body['report_types'] and 'exchange_rate' in body['report_types']
        assert body['max_files'] >= 1
