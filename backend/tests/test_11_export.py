"""S10 导出（TC-EXP-001~006）— 二进制完整性与下载头"""
import io


class TestExport:
    def test_sales_export(self, client, admin_headers, e2e):  # TC-EXP-001
        r = client.get('/api/v2/export/sales', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-01', 'confirm': 'true'})
        assert r.status_code == 200, r.text
        assert len(r.content) > 500
        assert 'attachment' in r.headers.get('content-disposition', '')

    def test_snapshot_export(self, client, admin_headers, calc):  # TC-EXP-002
        r = client.get(f"/api/v2/export/snapshot/{calc['uk']['snapshot_id']}",
                       headers=admin_headers, params={'confirm': 'true'})
        assert r.status_code == 200, r.text
        assert len(r.content) > 200

    def test_raw_rows_export(self, client, admin_headers, e2e):  # TC-EXP-003
        r = client.get('/api/v2/export/raw-rows', headers=admin_headers,
                       params={'batch_id': e2e['lmg_batch_ids'][0], 'confirm': 'true'})
        assert r.status_code == 200 and len(r.content) > 100

    def test_transfer_export(self, client, admin_headers):  # TC-EXP-004
        r = client.get('/api/v2/export/transfer', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-01', 'confirm': 'true'})
        assert r.status_code == 200

    def test_missing_skus_export(self, client, admin_headers, e2e):  # TC-EXP-005
        r = client.get('/api/v2/export/missing-skus', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-02', 'confirm': 'true'})
        assert r.status_code == 200

    def test_costs_export(self, client, admin_headers):  # TC-EXP-006
        r = client.get('/api/v2/export/costs', headers=admin_headers,
                       params={'confirm': 'true'})
        assert r.status_code == 200
