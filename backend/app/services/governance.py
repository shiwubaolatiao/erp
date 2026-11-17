# -*- coding: utf-8 -*-
"""费用月份格式治理 — 核心服务（组 10 / D-03）

被两处复用：
  · tools/govern_month_format.py    CLI（含 mysqldump 备份 / acceptance 对账 / 回放）
  · app/api/v2/admin.py             POST /admin/governance/month-format（管理入口）

清洗规则：正则 ^\\d{4}-\\d{2}. 命中才截取前 7 位（纯前 7 位截取不做语义变更）；
幂等：清洗后 ^\\d{4}-\\d{2}$ 不再命中。

 GovernanceResult 契约（tasks 10.2）：
  {
    "dry_run": bool,
    "tables": [{"table_name", "column", "dirty_rows", "cleaned_rows"}],
    "consistent": bool,        # 等值/前缀聚合一致性（差异必须为 0）
    "reconciliation": {...},   # 三方对账结论（调用方注入 reconcile_fn 才有）
  }
"""
import json
import os
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text as sa_text

from app.models import FactFeeStorage, FactFeeReturn, FactFeeAgedStorage

# (表名, 列名, 模型, 金额列) — 金额列用于等值/前缀聚合一致性验证
TARGETS = [
    ("fact_fee_storage", "month_of_charge", FactFeeStorage, "estimated_monthly_storage_fee"),
    ("fact_fee_return", "month_of_charge", FactFeeReturn, "sku_returns_fee"),
    ("fact_fee_aged_storage", "snapshot_date", FactFeeAgedStorage, "amount_charged"),
]

BATCH_SIZE = 1000
# 命中规则：以 YYYY-MM 开头且长度 > 7（'.' 匹配任意单字符；
# 纯 YYYY-MM / 空串 / 其他格式一律不动）
DIRTY_SQL = "REGEXP '^[0-9]{4}-[0-9]{2}.'"



def _dirty_where(col: str) -> str:
    return f"{col} {DIRTY_SQL}"


def count_dirty(db, table: str, col: str) -> int:
    return db.execute(sa_text(
        f"SELECT COUNT(*) FROM {table} WHERE {_dirty_where(col)}")).scalar() or 0


def list_dirty_ids(db, table: str, col: str, after_id: int, limit: int) -> list:
    """按 id 升序扫描一批脏行 → [(id, 原值)]（after_id 游标避免 OFFSET 漂移）"""
    rows = db.execute(sa_text(
        f"SELECT id, {col} FROM {table} WHERE {_dirty_where(col)} "
        f"AND id > :after ORDER BY id LIMIT :lim"),
        {"after": after_id, "lim": limit}).all()
    return [(r[0], r[1]) for r in rows]


def clean_table(db, table: str, col: str, backup_io, batch_size: int = BATCH_SIZE) -> int:
    """单表分批清洗：每批 先追加回放清单(flush+fsync) → UPDATE → commit。

    中途失败直接重跑即可续跑（幂等扫描剩余脏行）。
    """
    cleaned = 0
    after_id = 0
    while True:
        batch = list_dirty_ids(db, table, col, after_id, batch_size)
        if not batch:
            break
        after_id = batch[-1][0]
        # ① 先落清单（crash 时清单 ⊇ 已执行批次，回放多写无害：写回旧值可被重跑恢复）
        for rid, old in batch:
            backup_io.write(json.dumps(
                {"table": table, "column": col, "id": rid, "old": old},
                ensure_ascii=False) + "\n")
        backup_io.flush()
        os.fsync(backup_io.fileno())
        # ② 再执行本批 UPDATE（id 为库内自增整数，直接拼接无注入面）
        ids = ",".join(str(rid) for rid, _ in batch)
        db.execute(sa_text(
            f"UPDATE {table} SET {col} = LEFT({col}, 7) WHERE id IN ({ids})"))
        db.commit()
        cleaned += len(batch)
    return cleaned


def verify_consistency(db) -> tuple:
    """等值/前缀聚合一致性验证：清洗后 SUM(金额) GROUP BY 列值 == GROUP BY LEFT(列值,7)"""
    problems = []
    for table, col, _model, amount_col in TARGETS:
        prefix_agg = db.execute(sa_text(
            f"SELECT LEFT({col}, 7) m, COUNT(*) c, COALESCE(SUM({amount_col}),0) s "
            f"FROM {table} GROUP BY LEFT({col}, 7)")).all()
        exact_agg = db.execute(sa_text(
            f"SELECT {col} m, COUNT(*) c, COALESCE(SUM({amount_col}),0) s "
            f"FROM {table} GROUP BY {col}")).all()
        p = {r[0]: (r[1], Decimal(str(r[2]))) for r in prefix_agg}
        e = {r[0]: (r[1], Decimal(str(r[2]))) for r in exact_agg}
        if p != e:
            problems.append(f"{table}.{col}: 前缀聚合 {len(p)} 组 != 等值聚合 {len(e)} 组")
        bad = db.execute(sa_text(
            f"SELECT COUNT(*) FROM {table} WHERE {_dirty_where(col)}")).scalar() or 0
        if bad:
            problems.append(f"{table}.{col}: 仍有 {bad} 行带时间戳脏值")
    return (not problems), problems


def _rollback_batch(db, table: str, col: str, pairs: list) -> int:
    """一批回放：CASE WHEN 逐 id 精确写回旧值"""
    when = " ".join(f"WHEN {rid} THEN :old_{j}" for j, (rid, _o) in enumerate(pairs))
    params = {f"old_{j}": o for j, (_rid, o) in enumerate(pairs)}
    ids = ",".join(str(rid) for rid, _o in pairs)
    db.execute(sa_text(
        f"UPDATE {table} SET {col} = CASE id {when} END WHERE id IN ({ids})"), params)
    db.commit()
    return len(pairs)


def rollback(db, manifest: Path) -> int:
    """按回放清单把旧值精确写回（多写无害：写回的旧值重跑清洗即可恢复）"""
    n = 0
    buf = {}
    with open(manifest, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            buf.setdefault((rec["table"], rec["column"]), []).append((rec["id"], rec["old"]))
    for (table, col), pairs in buf.items():
        pending = []
        for rid, old in pairs:
            pending.append((rid, old))
            if len(pending) >= BATCH_SIZE:
                n += _rollback_batch(db, table, col, pending)
                pending = []
        if pending:
            n += _rollback_batch(db, table, col, pending)
    return n


def run_governance(db, dry_run: bool, backup_dir: Path = None,
                   reconcile_fn=None) -> dict:
    """治理主流程 → GovernanceResult（CLI 与 10.2 API 共用核心）

    reconcile_fn: 无参可调用 → 对账 dict（由调用方注入；None 表示跳过对账）。
    清洗前对账失败 → 中止不清洗（aborted=True）。
    """
    tables = []
    for table, col, _model, _amount in TARGETS:
        dirty = count_dirty(db, table, col)
        tables.append({"table_name": table, "column": col,
                       "dirty_rows": dirty, "cleaned_rows": 0})

    if dry_run:
        consistent, problems = verify_consistency(db)
        return {"dry_run": True, "tables": tables, "consistent": consistent,
                "reconciliation": None, "problems": problems, "aborted": False}

    backup_path = None
    if backup_dir is not None:
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        backup_path = backup_dir / f"governance_backup_{datetime.now():%Y%m%d_%H%M%S}.jsonl"

    recon_before = reconcile_fn() if reconcile_fn else None
    if recon_before and not recon_before.get("ok"):
        return {"dry_run": False, "tables": tables, "consistent": False,
                "reconciliation": {"before": recon_before, "after": None},
                "problems": ["清洗前对账存在差异，已中止"], "aborted": True}

    if backup_path is not None:
        with open(backup_path, "a", encoding="utf-8") as bio:
            for i, (table, col, _model, _amount) in enumerate(TARGETS):
                tables[i]["cleaned_rows"] = clean_table(db, table, col, bio)
    else:
        for i, (table, col, _model, _amount) in enumerate(TARGETS):
            import io as _io
            tables[i]["cleaned_rows"] = clean_table(db, table, col, _io.StringIO())

    consistent, problems = verify_consistency(db)
    recon_after = reconcile_fn() if reconcile_fn else None
    if recon_after and not recon_after.get("ok"):
        problems.append("清洗后对账存在差异，请回滚")

    return {"dry_run": False, "tables": tables, "consistent": consistent,
            "reconciliation": {"before": recon_before, "after": recon_after},
            "problems": problems,
            "backup": str(backup_path) if backup_path else None, "aborted": False}