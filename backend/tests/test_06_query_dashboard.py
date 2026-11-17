"""S8 查询与看板（TC-API-QRY-001~006、TC-API-DSH-001~008）

/query/sales 语义（源自 query.py 实现）：
  asin/country 完整核算口径：sales_usd = Order − Refund 净额
  （PROFIT_TYPES=('Order','Refund')，与核算快照/EXP['lmg_sales_usd'] 同公式）；
  refund_amount = Refund 行 |product_sales|；total = 分组数；默认 group_by=asin。
  仅 sku/month 简单分支为 Order 毛额。
合成数据期望：LMG 2026-01 → 净销售额 300.50（100+50.5−50+200）、Refund 50.00。
"""
import pytest

from conftest import EXP

NET_SALES = 300.50            # 100 + 50.5 − 50 + 200（与 EXP['lmg_sales_usd'] 一致）
UK_NET_SALES = 100.50         # 100 + 50.5 − 50


@pytest.mark.usefixtures('e2e')
class TestQuerySales:
    def test_monthly_aggregation(self, client, admin_headers):  # TC-API-QRY-001
        r = client.get('/api/v2/query/sales', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-01'})
        assert r.status_code == 200
        body = r.json()
        total_sales = sum(i['sales_usd'] for i in body['items'])
        total_refund = sum(i['refund_amount'] for i in body['items'])
        assert abs(total_sales - NET_SALES) < 0.01
        assert abs(total_refund - EXP['lmg_refund_amount']) < 0.01

    def test_group_by_variants(self, client, admin_headers):  # TC-API-QRY-002
        for gb, key in (('country', 'country'), ('month', 'period_month'), ('sku', 'sku')):
            r = client.get('/api/v2/query/sales', headers=admin_headers,
                           params={'store': 'LMG', 'period_month': '2026-01', 'group_by': gb})
            assert r.status_code == 200
            items = r.json()['items']
            assert items and key in items[0], f"group_by={gb} 缺少键 {key}"
        by_country = client.get('/api/v2/query/sales', headers=admin_headers,
                                params={'store': 'LMG', 'period_month': '2026-01',
                                        'group_by': 'country'}).json()['items']
        m = {i['country']: i for i in by_country}
        assert set(m) == {'UK', 'DE'}
        assert abs(m['UK']['sales_usd'] - UK_NET_SALES) < 0.01   # Order−Refund 净额
        assert abs(m['UK']['refund_amount'] - 50.00) < 0.01
        assert abs(m['DE']['sales_usd'] - 200.00) < 0.01

    def test_filters(self, client, admin_headers):  # TC-API-QRY-003
        r = client.get('/api/v2/query/sales', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-01',
                               'asin': 'B0AAAAAAA1'})
        assert r.status_code == 200
        items = r.json()['items']
        assert items and all(i['asin'] == 'B0AAAAAAA1' for i in items)
        # SKU 过滤（按 normalized_sku 精确匹配）
        r2 = client.get('/api/v2/query/sales', headers=admin_headers,
                        params={'store': 'LMG', 'period_month': '2026-01',
                                'sku': 'SKU-D', 'group_by': 'sku'})
        items2 = r2.json()['items']
        assert items2 and all(i['sku'] == 'SKU-D' for i in items2)
        assert abs(sum(i['sales_usd'] for i in items2) - 200.00) < 0.01

    def test_products_triple(self, client, admin_headers):  # TC-API-QRY-004
        r = client.get('/api/v2/query/products', headers=admin_headers,
                       params={'store': 'LMG'})
        assert r.status_code == 200
        asins = {i['asin'] for i in r.json()['items']}
        assert {'B0AAAAAAA1', 'B0BBBBBBB2', 'B0DDDDDDD4'} <= asins

    def test_ad_contract(self, client, admin_headers):  # TC-API-QRY-005
        r = client.get('/api/v2/query/ad', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-01'})
        assert r.status_code == 200
        body = r.json()
        for k in ('total_spend', 'total_sales', 'acos', 'roas'):
            assert k in body, f"/query/ad 缺少 {k}"

    def test_fees_contract(self, client, admin_headers):  # TC-API-QRY-006
        r = client.get('/api/v2/query/fees', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-01'})
        assert r.status_code == 200


@pytest.mark.usefixtures('e2e')
class TestDashboard:
    ENDPOINTS = ['summary', 'trend', 'store-compare', 'fee-composition',
                 'refund-ranking', 'country-compare', 'product-distribution', 'transfer']

    def test_dashboard_endpoints(self, client, admin_headers):  # TC-API-DSH-001~008
        for ep in self.ENDPOINTS:
            r = client.get(f'/api/v2/dashboard/{ep}', headers=admin_headers,
                           params={'period_month': '2026-01'})
            assert r.status_code == 200, f"{ep} 失败: {r.text}"
            assert r.json() is not None
