"""S6 核算与快照（TC-ACC-001~011）— 精确值断言 + 成本缺失闭环"""
import pytest

from conftest import EXP, PERIOD, calc_and_wait


class TestCalculateExact:
    def test_uk_exact(self, client, admin_headers, calc):  # TC-ACC-001
        uk = calc['uk']
        assert abs(uk['net_amazon'] - EXP['uk_net_amazon']) < 0.005
        assert abs(uk['net_profit_rmb'] - EXP['uk_net_profit']) < 0.005
        assert abs(uk['product_sales_rmb'] - EXP['uk_ps_rmb']) < 0.005
        assert abs(uk['net_profit_rate'] - EXP['uk_profit_rate']) < 0.0005
        assert uk['exchange_rate'] == 9.0
        assert uk['cost_complete'] is True
        assert uk['order_count'] == 2      # 202-AAA / 202-BBB

    def test_de_exact(self, client, admin_headers, calc):  # TC-ACC-002
        de = calc['de']
        assert abs(de['net_amazon'] - EXP['de_net_amazon']) < 0.005
        assert abs(de['net_profit_rmb'] - EXP['de_net_profit']) < 0.005
        assert abs(de['product_sales_rmb'] - EXP['de_ps_rmb']) < 0.005
        assert de['exchange_rate'] == 8.5

    def test_snapshot_items(self, client, admin_headers, calc):  # TC-ACC-004
        r = client.get(f"/api/v2/accounting/snapshots/{calc['uk']['snapshot_id']}",
                       headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        skus = {i['sku']: i for i in body['items']}
        assert set(skus) == {'SKU-A', 'SKU-B'}
        # SKU-A：净量 1（购 2 退 1）；成本 10×1、运费 2×1
        assert skus['SKU-A']['quantity'] == 1
        assert float(skus['SKU-A']['product_cost_rmb']) == 10
        assert skus['SKU-A']['cost_status'] == 'normal'

    def test_version_increment(self, client, admin_headers, calc):  # TC-ACC-003
        v2 = calc_and_wait(client, admin_headers, {
            'store': 'LMG', 'country': 'UK', 'period_month': PERIOD,
            'snapshot_name': '回归核算v2'})
        assert v2['version'] == calc['uk']['version'] + 1
        snaps = client.get('/api/v2/accounting/snapshots', headers=admin_headers,
                           params={'store': 'LMG', 'period_month': PERIOD}).json()['items']
        uk_snaps = [s for s in snaps if s['country'] == 'UK']
        assert len(uk_snaps) >= 2  # v1 保留

    def test_compare(self, client, admin_headers, calc):  # TC-ACC-005
        # 建 v2 再对比 v1/v2
        v2 = calc_and_wait(client, admin_headers, {
            'store': 'LMG', 'country': 'DE', 'period_month': PERIOD})
        r = client.get('/api/v2/accounting/compare', headers=admin_headers,
                       params={'snapshot_a': calc['de']['snapshot_id'],
                               'snapshot_b': v2['snapshot_id']})
        assert r.status_code == 200
        body = r.json()
        assert {'snapshot_a', 'snapshot_b', 'total_delta', 'changed_items', 'diffs'} \
            <= set(body.keys())
        assert abs(body['total_delta']) < 0.01  # 同数据重算 → 无差异


class TestCostMissingLoop:
    def test_missing_alert_and_repair(self, client, admin_headers, e2e):  # TC-ACC-006/007
        # 2026-02 的 SKU-E 无产品成本 → 核算应标"成本不完整"
        snap = calc_and_wait(client, admin_headers, {
            'store': 'LMG', 'country': 'UK', 'period_month': '2026-02'})
        assert snap['cost_complete'] is False
        assert snap['cost_incomplete_skus'] >= 1
        assert abs(snap['net_amazon'] - EXP['feb_net_amazon']) < 0.005
        # 预警队列出现 pending 记录
        alerts = client.get('/api/v2/accounting/cost-alerts', headers=admin_headers,
                            params={'store': 'LMG', 'period_month': '2026-02',
                                    'status': 'pending'}).json()['items']
        assert any(a['sku'] == 'SKU-E' for a in alerts)
        alert_id = next(a['id'] for a in alerts if a['sku'] == 'SKU-E')
        # 成本补录 → 关闭预警
        r2 = client.post('/api/v2/accounting/cost-entry', headers=admin_headers, json={
            'store': 'LMG', 'asin': 'B0EEEEEEE5', 'sku': 'SKU-E',
            'period': '2026-02', 'cost_rmb': 8, 'freight': 1})
        assert r2.status_code == 200 and r2.json()['closed_alerts'] >= 1
        # 重算后成本完整
        snap2 = calc_and_wait(client, admin_headers, {
            'store': 'LMG', 'country': 'UK', 'period_month': '2026-02'})
        assert snap2['cost_complete'] is True

    def test_ignore_alert(self, client, admin_headers, e2e):  # TC-ACC-008
        alerts = client.get('/api/v2/accounting/cost-alerts', headers=admin_headers,
                            params={'status': 'all'}).json()['items']
        assert alerts
        r = client.post(f"/api/v2/accounting/cost-alerts/{alerts[0]['id']}/ignore",
                        headers=admin_headers)
        assert r.status_code == 200

    def test_calc_missing_period_400(self, client, admin_headers):  # TC-ACC-009
        r = client.post('/api/v2/accounting/calculate', headers=admin_headers,
                        json={'store': 'LMG', 'country': 'UK'})
        assert r.status_code == 400

    def test_calc_unknown_store_404(self, client, admin_headers):  # TC-ACC-010
        r = client.post('/api/v2/accounting/calculate', headers=admin_headers,
                        json={'store': 'NOPE', 'country': 'UK', 'period_month': PERIOD})
        assert r.status_code == 404

    def test_range_calc(self, client, admin_headers):  # TC-ACC-011 区间核算
        body = calc_and_wait(client, admin_headers, {
            'store': 'LMG', 'country': 'UK',
            'date_from': '2026-01-01', 'date_to': '2026-02-01'})
        assert body['period_month'] is None
        assert abs(body['net_amazon'] - EXP['uk_net_amazon']) < 0.005
