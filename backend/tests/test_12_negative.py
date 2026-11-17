"""S12 负面与安全（TC-NEG-001~006）— 不存在资源 / 注入样本 / 校验边界"""
import uuid


class TestNegative:
    def test_resource_404_matrix(self, client, admin_headers):  # TC-NEG-001
        cases = [
            ('GET', '/api/v2/import/batches/99999999/report', {}),
            ('GET', '/api/v2/accounting/snapshots/99999999', {}),
            ('GET', '/api/v2/trace/raw-row/99999999', {}),
            ('GET', '/api/v2/trace/batch/99999999', {}),
            ('DELETE', '/api/v2/auth/users/99999999', {}),
            ('GET', '/api/v2/export/snapshot/99999999', {'confirm': 'true'}),
            ('GET', '/api/v2/trace/fact/sales/99999999', {}),
        ]
        for method, url, params in cases:
            r = getattr(client, method.lower())(url, headers=admin_headers, params=params)
            assert r.status_code == 404, f"{method} {url} → {r.status_code}"

    def test_sql_injection_samples(self, client, admin_headers, e2e):  # TC-NEG-002
        payloads = ["' OR '1'='1", "x'; DROP TABLE fact_sales_transaction--",
                    "1' UNION SELECT password FROM sys_user--"]
        for p in payloads:
            r = client.get('/api/v2/query/sales', headers=admin_headers,
                           params={'store': 'LMG', 'period_month': '2026-01', 'asin': p})
            assert r.status_code == 200, f"注入样本 {p} 导致 {r.status_code}"
            assert 'password' not in r.text.lower() or r.json().get('total', 0) == 0
        # 注入后表仍可用（未被 DROP）
        r2 = client.get('/api/v2/query/sales', headers=admin_headers,
                        params={'store': 'LMG', 'period_month': '2026-01'})
        assert r2.status_code == 200 and r2.json()['total'] >= 1

    def test_xss_storage_passthrough(self, client, admin_headers):  # TC-NEG-003
        from conftest import sql_one
        asin = f'B0XSS{uuid.uuid4().hex[:4].upper()}'
        payload = '<script>alert(1)</script>'
        r = client.post('/api/v2/admin/products', headers=admin_headers, json={
            'store': 'MGT', 'asin': asin, 'sku': 'SKU-XSS', 'title': payload})
        assert r.status_code == 200, r.text
        pid = r.json()['id']
        # API 层原样存储（渲染层转义属前端范畴，见测试报告说明）
        row = sql_one("SELECT title FROM store_product WHERE id=:i", i=pid)
        assert row['title'] == payload
        client.delete(f'/api/v2/admin/products/{pid}', headers=admin_headers)

    def test_invalid_limit_no_500(self, client, admin_headers):  # TC-NEG-004
        for limit in ('-1', '0', '99999999'):
            r = client.get('/api/v2/query/sales', headers=admin_headers,
                           params={'store': 'LMG', 'period_month': '2026-01', 'limit': limit})
            assert r.status_code in (200, 422), f"limit={limit} → {r.status_code}"

    def test_unprocessable_body(self, client, admin_headers):  # TC-NEG-005
        r = client.post('/api/v2/accounting/calculate', headers=admin_headers, json={
            'wrong_field': 1})
        assert r.status_code == 422

    def test_no_500_on_hostile_input(self, client, admin_headers):  # TC-NEG-006
        hostile = [
            ('/api/v2/query/sales', {'store': 'LMG', 'period_month': 'not-a-month'}),
            ('/api/v2/query/sales', {'store': 'LMG', 'period_month': '2026-13'}),
            ('/api/v2/trace/search', {'keyword': '%' * 500}),
            ('/api/v2/query/products', {'store': 'LMG', 'status': 'unknown-status'}),
        ]
        for url, params in hostile:
            r = client.get(url, headers=admin_headers, params=params)
            assert r.status_code < 500, f"{url} {params} → {r.status_code}"
