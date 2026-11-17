"""S18 看板利润占位符修复（B-03 / B-04，tasks.md 3.2 / 3.3）

修复前：
  · /dashboard/country-compare     net_profit 硬编码 0 —— 站点级快照已算出利润却不展示
  · /dashboard/product-distribution net_profit 硬编码 0 —— 不读快照明细，单品利润恒 0

期望值人工独立推导（conftest 合成数据，汇率 UK=9.0 / DE=8.5）：
  UK SKU-A(B0AAAAAAA1)：net_amazon = 82 − 64 = 18、净量 1 → 18×9 − 10 − 2 = 150.00
     SKU-B(B0BBBBBBB2)：44.5×9 − 5 − 1 = 394.50        → UK 合计 544.50
  DE SKU-D(B0DDDDDDD4)：180×8.5 − 20 − 3 = 1507.00
  店铺级（SKU×站点明细按 ASIN 聚合）：150 + 394.5 + 1507 = 2051.50
未核算月份（2026-02 仅 UK 销售、无快照）→ null + has_snapshot=False（区分"未核算"，非假 0）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import EXP, PERIOD, calc_and_wait

from app.core.database import SessionLocal
from app.models import DwsProfitSnapshot


def _drop_snapshots(pm: str):
    """清空指定月份全部快照，恢复"未核算"前提

    数字序测试（test_07 成本回填场景）会核算 2026-02；本文件字母序在其后，
    未核算月用例需先清理才能构造"有销售、无快照"场景。
    """
    db = SessionLocal()
    try:
        db.query(DwsProfitSnapshot).filter(
            DwsProfitSnapshot.period_month == pm).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


class TestCountryCompareProfit:
    """B-03：/dashboard/country-compare 利润来自站点级快照，未核算为 null"""

    def test_profit_matches_site_snapshots(self, client, admin_headers, calc):
        r = client.get('/api/v2/dashboard/country-compare', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': PERIOD})
        assert r.status_code == 200, r.text
        by_c = {it['country']: it for it in r.json()['items']}
        uk, de = by_c['UK'], by_c['DE']
        # 利润非 0 且等于站点级快照净利润（修复前恒 0）
        assert abs(uk['net_profit_rmb'] - EXP['uk_net_profit']) < 0.005, \
            f"UK: {uk['net_profit_rmb']} != {EXP['uk_net_profit']}"
        assert abs(de['net_profit_rmb'] - EXP['de_net_profit']) < 0.005, \
            f"DE: {de['net_profit_rmb']} != {EXP['de_net_profit']}"
        assert uk['has_snapshot'] is True
        assert de['has_snapshot'] is True
        # 利润率 = 站点级快照利润 / 快照销售额（UK 904.50 / DE 1700.00）
        assert abs(uk['net_profit_rate'] - EXP['uk_profit_rate']) < 0.0005
        assert abs(de['net_profit_rate'] - EXP['de_profit_rate']) < 0.0005

    def test_uncalculated_month_returns_null(self, client, admin_headers, e2e):
        """2026-02 仅 UK 销售、无核算快照 → null + has_snapshot=False（不得假 0）"""
        _drop_snapshots('2026-02')
        r = client.get('/api/v2/dashboard/country-compare', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-02'})
        assert r.status_code == 200, r.text
        uk = next(it for it in r.json()['items'] if it['country'] == 'UK')
        assert uk['sales_rmb'] > 0                       # 销售侧有数（68×9=612）
        assert uk['net_profit_rmb'] is None              # 修复前为 0（假值）
        assert uk['has_snapshot'] is False
        assert uk['net_profit_rate'] is None


class TestProductDistributionProfit:
    """B-04：/dashboard/product-distribution 利润按 ASIN 聚合快照明细"""

    def test_profit_by_asin_matches_snapshot_items(self, client, admin_headers, calc):
        # 店铺级核算（country 省略）→ 快照明细为 SKU×站点，页面按 ASIN 聚合
        snap = calc_and_wait(client, admin_headers,
                             {'store': 'LMG', 'period_month': PERIOD})
        assert abs(snap['net_profit_rmb'] - 2051.50) < 0.01

        r = client.get('/api/v2/dashboard/product-distribution', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': PERIOD})
        assert r.status_code == 200, r.text
        items = r.json()['items']
        by_asin = {it['asin']: it for it in items}
        expect = {'B0AAAAAAA1': 150.00, 'B0BBBBBBB2': 394.50, 'B0DDDDDDD4': 1507.00}
        for asin, val in expect.items():
            it = by_asin[asin]
            assert it['net_profit_rmb'] is not None, f"{asin}: 利润为 None（未读到快照明细）"
            assert abs(it['net_profit_rmb'] - val) < 0.005, \
                f"{asin}: {it['net_profit_rmb']} != {val}"
            assert it['has_snapshot'] is True
        # Σ ASIN 利润 == 店铺级快照净利润（按 ASIN 聚合无遗漏、无重复）
        total = sum(it['net_profit_rmb'] for it in items if it['net_profit_rmb'] is not None)
        assert abs(total - 2051.50) < 0.01

    def test_uncalculated_month_returns_null(self, client, admin_headers, e2e):
        """2026-02 无店铺级快照 → SKU-E 利润 null（而非假 0）"""
        r = client.get('/api/v2/dashboard/product-distribution', headers=admin_headers,
                       params={'store': 'LMG', 'period_month': '2026-02'})
        assert r.status_code == 200, r.text
        e5 = next(it for it in r.json()['items'] if it['asin'] == 'B0EEEEEEE5')
        assert e5['sales_rmb'] > 0
        assert e5['net_profit_rmb'] is None
        assert e5['has_snapshot'] is False