"""S14 系统级（TC-SYS-001~004）— 在线实例只读冒烟（ENV-S: 8020/5180）+ 前端构建

★ 在线实例只允许只读请求（GET /health、POST /login、GET 查询），绝不写入业务数据。
登录会写 operation_log（系统正常审计行为，非数据污染）。
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest
import httpx

FRONTEND = Path(r'E:\项目\erp重构\frontend')
BASE = 'http://localhost:8020'


def _http(method, url, **kw):
    with httpx.Client(timeout=15) as c:
        return c.request(method, url, **kw)


@pytest.mark.live
class TestLiveSmoke:
    def test_health_identity(self):  # TC-SYS-001
        r = _http('GET', f'{BASE}/health')
        assert r.status_code == 200
        assert r.json()['app'] == 'LMG-ERP'

    def test_api_docs_reachable(self):  # TC-SYS-003
        assert _http('GET', f'{BASE}/docs').status_code == 200

    def test_frontend_reachable(self):  # TC-SYS-004
        r = _http('GET', 'http://localhost:5180')
        assert r.status_code == 200 and 'html' in r.headers.get('content-type', '')

    def test_live_readonly_probe(self):  # 在线只读探测：登录 + 关键查询（TC-PERF-002 数据源）
        r = _http('POST', f'{BASE}/api/v2/auth/login',
                  data={'username': 'admin', 'password': '123456'})
        assert r.status_code == 200
        h = {'Authorization': f"Bearer {r.json()['access_token']}"}
        results = {}
        for name, url in (('sales', '/api/v2/query/sales'),
                          ('dashboard', '/api/v2/dashboard/summary')):
            _http('GET', BASE + url, headers=h, params={'period_month': '2026-01'})  # 预热
            times = []
            for _ in range(3):
                t0 = time.perf_counter()
                rr = _http('GET', BASE + url, headers=h, params={'period_month': '2026-01'})
                assert rr.status_code == 200
                times.append((time.perf_counter() - t0) * 1000)
            times.sort()
            results[name] = times[1]  # 中位数（削弱网络抖动影响）
            assert times[1] < 5000, \
                f"在线 {name} 中位延迟 {times[1]:.0f}ms 超阈值 5000ms（样本 {times}）"
        print(f"\nPERF(ENV-S median ms): sales={results['sales']:.0f} "
              f"dashboard={results['dashboard']:.0f}")


class TestFrontendBuild:
    def test_npm_build(self):  # TC-SYS-002 前端可构建
        npm = 'npm.cmd'
        p = subprocess.run([npm, 'run', 'build'], cwd=FRONTEND, shell=True,
                           capture_output=True, text=True, timeout=420,
                           encoding='utf-8', errors='replace')
        assert p.returncode == 0, f"前端构建失败:\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}"
        assert (FRONTEND / 'dist' / 'index.html').exists()
