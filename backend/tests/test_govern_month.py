# -*- coding: utf-8 -*-
"""组10.1 月份格式治理 — govern_month_format 工具回归测试

覆盖：dry-run 预览不改数 / 清洗截前 7 位 / 幂等（二次 0 行）/
等值前缀聚合一致性 / --rollback 清单回放恢复旧值。
"""
import json
from pathlib import Path

import pytest

from app.core.database import SessionLocal
from app.models import (
    Store, ImportBatch, RawSheet, RawSheetRow,
    FactFeeStorage, FactFeeReturn, FactFeeAgedStorage,
)
from app.services.governance import run_governance, rollback, count_dirty


@pytest.fixture()
def dirty_db():
    """造数：1 店铺 + 1 批次 + 1 原始行 + 三类费用表各 3 行（脏/干净/空值）"""
    from app.core.database import engine, Base
    Base.metadata.create_all(bind=engine)  # 幂等：本文件不依赖 client fixture，需自建表
    db = SessionLocal()
    st = Store(store_code="GOV", store_name="治理测试店")
    db.add(st)
    db.flush()
    b = ImportBatch(file_name="gov.xlsx", file_hash="gov-hash-1", store_id=st.id)
    db.add(b)
    db.flush()
    sh = RawSheet(batch_id=b.id, sheet_name="Storage", report_type="fee_storage")
    db.add(sh)
    db.flush()
    row = RawSheetRow(batch_id=b.id, sheet_id=sh.id, sheet_name="Storage", row_no=2,
                      headers_json=["m"], values_json=["x"])
    db.add(row)
    db.flush()

    def mk(model, month_val, amount=1):
        r = model(store_id=st.id, country="US", raw_row_id=row.id, batch_id=b.id)
        if model is FactFeeStorage:
            r.month_of_charge = month_val
            r.estimated_monthly_storage_fee = amount
        elif model is FactFeeReturn:
            r.month_of_charge = month_val
            r.sku_returns_fee = amount
        else:
            r.snapshot_date = month_val
            r.amount_charged = amount
        db.add(r)
        return r

    rows = {
        "storage_dirty": mk(FactFeeStorage, "2026-06-01 00:00:00", 10),
        "storage_clean": mk(FactFeeStorage, "2026-06", 20),
        "storage_empty": mk(FactFeeStorage, "", 30),
        "return_dirty": mk(FactFeeReturn, "2026-05-15 12:30:00", 40),
        "return_clean": mk(FactFeeReturn, "2026-05", 50),
        "aged_dirty": mk(FactFeeAgedStorage, "2026-04-30 00:00:00", 60),
        "aged_dirty2": mk(FactFeeAgedStorage, "2026-04-01", 70),
    }
    db.commit()
    yield db
    for m in (FactFeeStorage, FactFeeReturn, FactFeeAgedStorage):
        db.query(m).filter(m.batch_id == b.id).delete()
    db.query(RawSheetRow).filter(RawSheetRow.batch_id == b.id).delete()
    db.query(RawSheet).filter(RawSheet.batch_id == b.id).delete()
    db.query(ImportBatch).filter(ImportBatch.id == b.id).delete()
    db.query(Store).filter(Store.id == st.id).delete()
    db.commit()
    db.close()


def _tbl(result, name):
    return next(t for t in result["tables"] if t["table_name"] == name)


def test_dry_run_reports_dirty_without_change(dirty_db):
    r = run_governance(dirty_db, dry_run=True, backup_dir=None)
    assert r["dry_run"] is True
    assert _tbl(r, "fact_fee_storage")["dirty_rows"] == 1
    assert _tbl(r, "fact_fee_return")["dirty_rows"] == 1
    assert _tbl(r, "fact_fee_aged_storage")["dirty_rows"] == 2
    assert all(t["cleaned_rows"] == 0 for t in r["tables"])
    # dry-run 不改数据
    assert count_dirty(dirty_db, "fact_fee_storage", "month_of_charge") == 1


def test_clean_idempotent_and_consistent(dirty_db, tmp_path):
    r = run_governance(dirty_db, dry_run=False, backup_dir=tmp_path)
    assert r["aborted"] is False
    assert _tbl(r, "fact_fee_storage")["cleaned_rows"] == 1
    assert _tbl(r, "fact_fee_return")["cleaned_rows"] == 1
    assert _tbl(r, "fact_fee_aged_storage")["cleaned_rows"] == 2
    assert r["consistent"] is True, r["problems"]
    assert r["problems"] == []

    # 值已被截为前 7 位（纯前 7 位截取，无语义变更）
    s = dirty_db.query(FactFeeStorage).filter(FactFeeStorage.batch_id != None).all()
    vals = {x.month_of_charge for x in s}
    assert "2026-06-01 00:00:00" not in vals
    assert "2026-06" in vals

    # 幂等：二次执行命中 0 行
    r2 = run_governance(dirty_db, dry_run=False, backup_dir=tmp_path)
    assert all(t["cleaned_rows"] == 0 for t in r2["tables"])
    assert r2["consistent"] is True

    # 备份清单存在且行数正确（4 条脏行记录）
    manifest = Path(r["backup"])
    assert manifest.exists()
    lines = [json.loads(l) for l in manifest.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 4


def test_rollback_restores_old_values(dirty_db, tmp_path):
    r = run_governance(dirty_db, dry_run=False, backup_dir=tmp_path)
    manifest = Path(r["backup"])

    n = rollback(dirty_db, manifest)
    assert n == 4

    # 旧值恢复（带时间戳原样回来）
    back = {
        (x.id, x.month_of_charge) for x in dirty_db.query(FactFeeStorage).all()
    }
    vals = [v for _i, v in back]
    assert "2026-06-01 00:00:00" in vals
    assert count_dirty(dirty_db, "fact_fee_storage", "month_of_charge") == 1
    assert count_dirty(dirty_db, "fact_fee_aged_storage", "snapshot_date") == 2

    # 恢复后可再次清洗（续跑能力）
    r3 = run_governance(dirty_db, dry_run=False, backup_dir=tmp_path)
    assert sum(t["cleaned_rows"] for t in r3["tables"]) == 4
    assert r3["consistent"] is True

def test_api_month_format(client, admin_headers, dirty_db):
    """组10.2：POST /admin/governance/month-format 与 CLI 核心行为一致 + 审计留痕"""
    # dry-run：预览不改数
    r = client.post('/api/v2/admin/governance/month-format',
                    headers=admin_headers, json={"dry_run": True})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["dry_run"] is True
    assert all(t["cleaned_rows"] == 0 for t in data["tables"])
    assert next(t for t in data["tables"] if t["table_name"] == "fact_fee_storage")["dirty_rows"] == 1
    assert count_dirty(dirty_db, "fact_fee_storage", "month_of_charge") == 1

    # 执行：清洗 + 一致性 + 审计留痕
    r2 = client.post('/api/v2/admin/governance/month-format',
                     headers=admin_headers, json={"dry_run": False})
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["dry_run"] is False
    assert next(t for t in d2["tables"] if t["table_name"] == "fact_fee_aged_storage")["cleaned_rows"] == 2
    assert d2["consistent"] is True

    from app.models import OperationLog
    dirty_db.rollback()  # 结束 REPEATABLE READ 旧快照，才能看到 API session 提交的新日志
    log = dirty_db.query(OperationLog).filter(
        OperationLog.action == "governance.month_format").order_by(
        OperationLog.id.desc()).first()
    assert log is not None
    assert "dry_run=False" in log.target and "cleaned=4" in log.detail