"""S15 公共口径层 — 最新快照读取粒度回归测试（tasks.md 1.4）

★ 防线：country 必传（缺参报错）、店铺级/站点级粒度二选一、
  批量 store_ids 与逐店查询结果一致。
依赖 conftest 的 e2e / calc fixtures（LMG 店铺 UK+DE 站点级快照）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models import Store, DwsProfitSnapshot
from app.services.common.snapshots import latest_snapshots


def _lmg_store_id() -> int:
    db = SessionLocal()
    try:
        st = db.query(Store).filter(Store.store_code == 'LMG').first()
        assert st, 'LMG 店铺未初始化'
        return st.id
    finally:
        db.close()


class TestSnapshotGranularity:
    def test_country_is_required(self):
        """country 缺参（None）必须直接报 TypeError，杜绝漏加粒度过滤翻倍"""
        db = SessionLocal()
        try:
            with pytest.raises(TypeError):
                latest_snapshots(db, [1], country=None, period='2026-01')
        finally:
            db.close()

    def test_store_level_and_site_level_are_exclusive(self, calc):
        """店铺级(country='')与站点级('UK')互不混入 — 同一查询只返回一种粒度"""
        db = SessionLocal()
        try:
            store_id = _lmg_store_id()
            store_level = latest_snapshots(db, [store_id], country='', period='2026-01')
            site_uk = latest_snapshots(db, [store_id], country='UK', period='2026-01')
            site_de = latest_snapshots(db, [store_id], country='DE', period='2026-01')
            assert store_level, '店铺级快照缺失（前置 calc fixture 应已生成站点级；店铺级可能未算）'
            assert all(s.country == '' for s in store_level)
            assert all(s.country == 'UK' for s in site_uk)
            assert all(s.country == 'DE' for s in site_de)
        finally:
            db.close()

    def test_latest_version_dedup(self, calc):
        """同键多版本只取 version 最大"""
        db = SessionLocal()
        try:
            store_id = _lmg_store_id()
            snaps = latest_snapshots(db, [store_id], country='UK', period='2026-01')
            assert len(snaps) == 1
            max_ver = db.query(
                __import__('sqlalchemy').func.max(DwsProfitSnapshot.version)).filter(
                DwsProfitSnapshot.store_id == store_id,
                DwsProfitSnapshot.country == 'UK',
                DwsProfitSnapshot.period_month == '2026-01',
            ).scalar()
            assert snaps[0].version == max_ver
        finally:
            db.close()

    def test_batch_equals_per_store(self, calc):
        """批量 store_ids 结果 == 逐店查询合并"""
        db = SessionLocal()
        try:
            stores = [s.id for s in db.query(Store).all()]
            batch = latest_snapshots(db, stores, country='UK', period='2026-01')
            per_store = []
            for sid in stores:
                per_store.extend(latest_snapshots(db, [sid], country='UK', period='2026-01'))
            assert {(s.store_id, s.period_month, s.version) for s in batch} == \
                   {(s.store_id, s.period_month, s.version) for s in per_store}
        finally:
            db.close()

    def test_store_ids_empty_list_returns_nothing(self):
        """空 store 列表 → 空结果（不越权）"""
        db = SessionLocal()
        try:
            assert latest_snapshots(db, [], country='', period='2026-01') == []
        finally:
            db.close()