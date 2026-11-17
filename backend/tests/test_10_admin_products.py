"""S9 管理与配置（TC-ADM-001~009）— 汇率/商品成本/站点/别名/映射/日志/统计

契约说明（源自 admin.py / products.py）：
  - POST /exchange-rates → {'ok': True}；POST /countries → {'id':...}；PUT/DELETE 别名 → {'ok': True}
  - /mappings/{id}/decide 请求体为 {'action': 'confirm'|'reject'}
"""
import json
import uuid


def _items(client, admin_headers, url, params=None):
    body = client.get(url, headers=admin_headers, params=params or {}).json()
    return body['items'] if isinstance(body, dict) and 'items' in body else body


class TestAdminConfig:
    def test_exchange_rate_crud(self, client, admin_headers):  # TC-ADM-001
        code = f"Z{uuid.uuid4().hex[:1].upper()}"
        r = client.post('/api/v2/admin/exchange-rates', headers=admin_headers, json={
            'country_code': code, 'period_month': '2026-04', 'rate_to_rmb': 6.66})
        assert r.status_code == 200, r.text
        lst = _items(client, admin_headers, '/api/v2/admin/exchange-rates',
                     {'period_month': '2026-04'})
        mine = [x for x in lst if x['country_code'] == code]
        assert mine and float(mine[0]['rate_to_rmb']) == 6.66
        rid = mine[0]['id']
        r2 = client.put(f'/api/v2/admin/exchange-rates/{rid}', headers=admin_headers,
                        json={'rate_to_rmb': 6.88})
        assert r2.status_code == 200
        lst2 = _items(client, admin_headers, '/api/v2/admin/exchange-rates',
                      {'period_month': '2026-04'})
        assert any(x['id'] == rid and abs(float(x['rate_to_rmb']) - 6.88) < 1e-9
                   for x in lst2)
        assert client.delete(f'/api/v2/admin/exchange-rates/{rid}',
                             headers=admin_headers).status_code == 200

    def test_product_and_costs_crud(self, client, admin_headers):  # TC-ADM-002
        asin = f'B0TEST{uuid.uuid4().hex[:4].upper()}'
        r = client.post('/api/v2/admin/products', headers=admin_headers, json={
            'store': 'MGT', 'asin': asin, 'sku': f'SKU-{asin[-4:]}',
            'title': '测试商品'})
        assert r.status_code == 200, r.text
        pid = r.json()['id']
        r2 = client.put(f'/api/v2/admin/products/{pid}', headers=admin_headers,
                        json={'title': '测试商品改名'})
        assert r2.status_code == 200
        # 成本版本
        r3 = client.post(f'/api/v2/admin/products/{pid}/costs', headers=admin_headers, json={
            'period': '2026-03', 'cost_rmb': 12.5, 'freight': 2.5})
        assert r3.status_code == 200, r3.text
        cid = r3.json()['id']
        r4 = client.put(f'/api/v2/admin/products/{pid}/costs/{cid}', headers=admin_headers,
                        json={'cost_rmb': 13.0})
        assert r4.status_code == 200
        r5 = client.delete(f'/api/v2/admin/products/{pid}/costs/{cid}', headers=admin_headers)
        assert r5.status_code == 200
        assert client.delete(f'/api/v2/admin/products/{pid}',
                             headers=admin_headers).status_code == 200

    def test_country_maintenance(self, client, admin_headers):  # TC-ADM-003
        code = f"X{uuid.uuid4().hex[:1].upper()}"
        r = client.post('/api/v2/admin/countries', headers=admin_headers, json={
            'site_code': code, 'country_name': '测试站', 'default_currency': 'TST'})
        assert r.status_code == 200, r.text
        sites = _items(client, admin_headers, '/api/v2/stores/sites')
        assert any(s['site_code'] == code for s in sites)
        assert client.delete(f'/api/v2/admin/countries/{code}',
                             headers=admin_headers).status_code == 200

    def test_sheet_aliases_crud(self, client, admin_headers):  # TC-ADM-004
        alias = f'销售别名测试{uuid.uuid4().hex[:4]}'
        r = client.post('/api/v2/admin/sheet-aliases', headers=admin_headers, json={
            'report_type': 'sales', 'alias_name': alias})
        assert r.status_code == 200, r.text
        lst = _items(client, admin_headers, '/api/v2/admin/sheet-aliases')
        mine = [x for x in lst if x['alias_name'] == alias]
        assert mine, '新建 sheet 别名未出现在列表'
        assert client.delete(f"/api/v2/admin/sheet-aliases/{mine[0]['id']}",
                             headers=admin_headers).status_code == 200

    def test_header_aliases_crud(self, client, admin_headers):  # TC-ADM-005
        alias = f'表头别名测试{uuid.uuid4().hex[:4]}'
        r = client.post('/api/v2/admin/header-aliases', headers=admin_headers, json={
            'canonical_field': 'product sales', 'alias': alias})
        assert r.status_code == 200, r.text
        lst = _items(client, admin_headers, '/api/v2/admin/header-aliases')
        mine = [x for x in lst if x['alias'] == alias]
        assert mine, '新建表头别名未出现在列表'
        assert client.delete(f"/api/v2/admin/header-aliases/{mine[0]['id']}",
                             headers=admin_headers).status_code == 200

    def test_mappings_pending_and_decide(self, client, admin_headers, e2e):  # TC-ADM-006
        r = client.get('/api/v2/admin/mappings/pending', headers=admin_headers)
        assert r.status_code == 200
        items = r.json()['items']
        assert items, '导入 3 个新 ASIN 应产生待确认映射'
        target = items[0]
        r2 = client.post(f"/api/v2/admin/mappings/{target['id']}/decide",
                         headers=admin_headers,
                         json={'action': 'confirm',
                               'sku': (target.get('sku') or '') or 'SKU-A'})
        assert r2.status_code == 200, r2.text

    def test_operation_logs(self, client, admin_headers):  # TC-ADM-007
        r = client.get('/api/v2/admin/operation-logs', headers=admin_headers)
        assert r.status_code == 200
        actions = {l['action'] for l in r.json()['items']}
        # 导入管线不写 operation log（只记录 login/锁结/汇率/导出等管理动作）
        assert {'login', 'lock_batch', 'unlock_batch'} <= actions, f"实际 {actions}"

    def test_stats(self, client, admin_headers):  # TC-ADM-008
        r = client.get('/api/v2/admin/stats', headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body.get('stores') >= 5 and body.get('raw_rows') > 0

    def test_replacements_list(self, client, admin_headers):  # TC-ADM-009
        assert client.get('/api/v2/admin/replacements',
                          headers=admin_headers).status_code == 200
