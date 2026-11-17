"""S7 溯源链路（TC-TRC-001~006）— 汇总数字 → fact → 原始行 → 批次"""
import pytest

from conftest import UK_SALES_ROWS


class TestTraceChain:
    def test_batches_list(self, client, admin_headers, e2e):  # TC-TRC-001
        r = client.get('/api/v2/trace/batches', headers=admin_headers)
        assert r.status_code == 200
        ids = {b['id'] for b in r.json()['items']}
        assert set(e2e['lmg_batch_ids']) <= ids

    def test_batch_detail(self, client, admin_headers, e2e):  # TC-TRC-002
        r = client.get(f"/api/v2/trace/batch/{e2e['lmg_batch_ids'][0]}",
                       headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body['batch']['file_name'] == 'LMG-EU_2026-01.xlsx'
        assert body['batch']['file_hash']
        names = {s['sheet_name'] for s in body['sheets']}
        assert {'UK销售', 'DE销售', '产品', '汇率'} <= names

    def test_fact_to_raw_row(self, client, admin_headers, e2e):  # TC-TRC-003 ★ 全链路
        fact_id = e2e['uk_fact_id']
        r1 = client.get(f'/api/v2/trace/fact/sales/{fact_id}', headers=admin_headers)
        assert r1.status_code == 200
        raw_row_id = r1.json()['raw_row_id']
        assert raw_row_id
        r2 = client.get(f'/api/v2/trace/raw-row/{raw_row_id}', headers=admin_headers)
        assert r2.status_code == 200
        cells = {c['header']: c['value'] for c in r2.json()['cells']}
        # 单元格原值与构造数据一致（溯源验收基准）
        expect = UK_SALES_ROWS[0]
        assert str(cells['order id']) == expect[2]
        assert str(cells['sku']) == expect[3]
        assert float(cells['product sales']) == 100.00
        assert float(cells['selling fees']) == -15.00
        assert r2.json()['batch']['file_name'] == 'LMG-EU_2026-01.xlsx'

    def test_raw_row_404(self, client, admin_headers):  # TC-TRC-004
        assert client.get('/api/v2/trace/raw-row/99999999',
                          headers=admin_headers).status_code == 404

    def test_search(self, client, admin_headers):  # TC-TRC-005
        r = client.get('/api/v2/trace/search', headers=admin_headers,
                       params={'keyword': '202-AAA'})
        assert r.status_code == 200
        assert r.json()['count'] >= 1

    def test_snapshot_item_trace(self, client, admin_headers, calc):  # TC-TRC-006
        detail = client.get(f"/api/v2/accounting/snapshots/{calc['uk']['snapshot_id']}",
                            headers=admin_headers).json()
        item_id = detail['items'][0]['id']
        r = client.get(f'/api/v2/trace/snapshot-item/{item_id}', headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body['fact_count'] >= 1
        assert body['snapshot']['exchange_rate'] == 9.0
        assert body['source_facts'][0]['trace_url'].startswith('/api/v2/trace/raw-row/')
