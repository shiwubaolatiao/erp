"""S21 存量快照批量重算（tasks.md 5.1，B-01 修复回填）

修复前（B-01）：店铺级核算走 `country or 'US'` 单汇率路径，UK/DE 站点被按
默认汇率折算，利润失真；这些旧版快照 country='' 且 rate_source 为 NULL。

修复后（重算工具 + 管理接口）：
  · POST /api/v2/admin/snapshots/recalculate 逐月重算生成新 version
    （旧版本永久保留可追溯；单月失败回滚不影响其余月份）
  · GET  /api/v2/admin/snapshots/verify 校验 店铺级 == Σ同期站点级（差异 ≤ 0.01）
  · 仅管理员可触发

期望值独立推导：店铺级净利润 = UK 544.50 + DE 1507.00 = 2051.50（conftest.EXP）。
"""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func as sa_func

from app.core.database import SessionLocal
from app.models import Store, DwsProfitSnapshot

from conftest import EXP, sql_one

PM = '2026-01'


def _lmg_id() -> int:
    db = SessionLocal()
    try:
        return db.query(Store).filter(Store.store_code == 'LMG').first().id
    finally:
        db.close()


def _insert_old_snapshot(pm: str, net_profit: float) -> int:
    """构造一条 B-01 修复前的旧版店铺级快照（country='' 且 rate_source=NULL）

    exchange_rate=6.8 模拟旧版按 US 默认汇率单值折算的特征；net_profit 传失真假值。
    """
    db = SessionLocal()
    try:
        store_id = _lmg_id()
        max_v = db.query(sa_func.max(DwsProfitSnapshot.version)).filter(
            DwsProfitSnapshot.store_id == store_id,
            DwsProfitSnapshot.country == '',
            DwsProfitSnapshot.period_month == pm,
        ).scalar() or 0
        snap = DwsProfitSnapshot(
            store_id=store_id, country='', period_month=pm, version=max_v + 1,
            exchange_rate=Decimal('6.8'), net_amazon=Decimal('242.50'),
            net_profit_rmb=Decimal(str(net_profit)),
            product_sales_rmb=Decimal('1645.40'),
        )
        db.add(snap)
        db.commit()
        return snap.id
    finally:
        db.close()


def _delete_snapshot(snap_id: int):
    db = SessionLocal()
    try:
        db.query(DwsProfitSnapshot).filter(DwsProfitSnapshot.id == snap_id).delete()
        db.commit()
    finally:
        db.close()


def _store_level_latest(pm: str):
    return sql_one(
        "SELECT * FROM dws_profit_snapshot WHERE store_id=:sid AND country='' "
        "AND period_month=:pm ORDER BY version DESC LIMIT 1",
        sid=_lmg_id(), pm=pm)


class TestRecalcSnapshots:
    def test_recalc_fixes_legacy_snapshot(self, client, admin_headers, calc):
        """旧版失真快照（1800.00）→ 重算 → 新 version=2051.50（UK+DE 逐国折算），旧版保留"""
        old_id = _insert_old_snapshot(PM, 1800.00)
        r = client.post('/api/v2/admin/snapshots/recalculate',
                        headers=admin_headers, json={'store': 'LMG'})
        assert r.status_code == 200, r.text
        report = r.json()
        assert report['ok'] and report['failed'] == 0, report
        assert report['affected'] >= 1

        item = next(i for i in report['items'] if i['period_month'] == PM)
        assert item['status'] == 'ok'
        assert item['old_version'] + 1 == item['new_version'], '快照 version 应递增'
        expected = EXP['uk_net_profit'] + EXP['de_net_profit']
        assert abs(item['new_net_profit_rmb'] - expected) < 0.01, \
            f"重算后净利润应等于 UK+DE 站点级之和 {expected}: {item}"
        assert item['rate_source'] == 'per_country', '修复版核算必写 rate_source'

        latest = _store_level_latest(PM)
        assert latest['rate_source'] == 'per_country'
        assert abs(float(latest['net_profit_rmb']) - expected) < 0.01
        assert latest['rate_detail'], '新版快照应记录站点级汇率明细'

        # 旧版本永久保留（可按 version 追溯），数字不被篡改
        old = sql_one("SELECT * FROM dws_profit_snapshot WHERE id=:i", i=old_id)
        assert old and old['rate_source'] is None
        assert abs(float(old['net_profit_rmb']) - 1800.00) < 0.005

        log = sql_one(
            "SELECT * FROM operation_log WHERE action='snapshot_recalc' "
            "ORDER BY id DESC LIMIT 1")
        assert log and log['target'] == 'LMG' and 'affected=' in (log['detail'] or '')

    def test_dry_run_lists_without_recalc(self, client, admin_headers, calc):
        """dry-run：列出受影响快照但不生成新版本（快照总数不变）"""
        old_id = _insert_old_snapshot('2026-02', 999.00)
        try:
            before = sql_one(
                "SELECT COUNT(*) AS c FROM dws_profit_snapshot WHERE store_id=:sid "
                "AND country='' AND period_month='2026-02'", sid=_lmg_id())['c']
            r = client.post('/api/v2/admin/snapshots/recalculate', headers=admin_headers,
                            json={'store': 'LMG', 'dry_run': True})
            assert r.status_code == 200, r.text
            report = r.json()
            assert report['dry_run'] is True and report['success'] == 0
            assert any(i['period_month'] == '2026-02' and i['status'] == 'dry_run'
                       for i in report['items'])
            after = sql_one(
                "SELECT COUNT(*) AS c FROM dws_profit_snapshot WHERE store_id=:sid "
                "AND country='' AND period_month='2026-02'", sid=_lmg_id())['c']
            assert before == after, 'dry-run 不得产生新快照'
        finally:
            _delete_snapshot(old_id)

    def test_verify_store_equals_sum_of_sites(self, client, admin_headers, calc):
        """全量校验：重算后 店铺级 == Σ同期站点级（差异 0.00）且无失败项"""
        r = client.get('/api/v2/admin/snapshots/verify', headers=admin_headers)
        assert r.status_code == 200, r.text
        v = r.json()
        assert v['checked'] >= 1, '至少应校验 LMG 2026-01 一组'
        assert v['passed'] == v['checked'] and v['failures'] == [], v
        assert v['ok'] is True

    def test_store_not_found(self, client, admin_headers):
        r = client.post('/api/v2/admin/snapshots/recalculate',
                        headers=admin_headers, json={'store': 'NOPE'})
        assert r.status_code == 404

    def test_admin_only(self, client, admin_headers, limited):
        """非管理员触发重算/校验 → 403（敏感数据操作仅管理员）"""
        r1 = client.post('/api/v2/admin/snapshots/recalculate',
                         headers=limited['headers'], json={'store': 'LMG'})
        assert r1.status_code == 403
        r2 = client.get('/api/v2/admin/snapshots/verify',
                        headers=limited['headers'])
        assert r2.status_code == 403

    def test_no_affected_returns_empty_report(self, client, admin_headers):
        """无受影响快照的店铺 → affected=0 且 ok（幂等，可重复执行）"""
        r = client.post('/api/v2/admin/snapshots/recalculate',
                        headers=admin_headers, json={'store': 'MGT'})
        assert r.status_code == 200, r.text
        report = r.json()
        assert report['affected'] == 0 and report['targets'] == 0 and report['ok']