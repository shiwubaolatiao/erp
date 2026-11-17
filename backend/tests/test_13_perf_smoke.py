"""S13 性能冒烟（TC-PERF-001）— ENV-T 关键接口时延（标记 perf，可 -m 排除）"""
import time

import pytest

ENDPOINTS = [
    ('/api/v2/query/sales', {'store': 'LMG', 'period_month': '2026-01'}),
    ('/api/v2/query/products', {'store': 'LMG'}),
    ('/api/v2/dashboard/summary', {'period_month': '2026-01'}),
    ('/api/v2/dashboard/trend', {'period_month': '2026-01'}),
    ('/api/v2/dashboard/store-compare', {'period_month': '2026-01'}),
    ('/api/v2/dashboard/country-compare', {'period_month': '2026-01'}),
    ('/api/v2/trace/batches', {}),
    ('/api/v2/admin/operation-logs', {}),
]
THRESHOLD_MS = 2000
SAMPLES = 3


@pytest.mark.perf
@pytest.mark.usefixtures('e2e')
class TestPerfSmoke:
    def test_key_endpoint_latency(self, client, admin_headers):  # TC-PERF-001
        report = {}
        for url, params in ENDPOINTS:
            times = []
            for _ in range(SAMPLES):
                t0 = time.perf_counter()
                r = client.get(url, headers=admin_headers, params=params)
                times.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200, f"{url} → {r.status_code}"
            avg = sum(times) / len(times)
            report[url] = round(avg, 1)
            assert avg < THRESHOLD_MS, f"{url} 平均 {avg:.0f}ms 超阈值 {THRESHOLD_MS}ms"
        print('\nPERF(ENV-T ms):', report)
