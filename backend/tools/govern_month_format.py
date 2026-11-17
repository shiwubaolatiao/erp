# -*- coding: utf-8 -*-
"""费用月份格式治理 CLI（组 10.1 / D-03）— 薄封装，核心逻辑在 app/services/governance.py

安全设计：
  · 幂等清洗：重复执行命中 0 行
  · 分批 1000 行/批：先追加回放清单 → 再 UPDATE → commit，中途失败重跑即续跑
  · --rollback 清单回放旧值
  · mysqldump 可用时先做三表备份（MYSQL_PWD 传密码不落命令行），不可用以清单兜底
  · 执行前后自动跑 tools/acceptance.py 三方对账（--no-acceptance 可跳过）

用法：
  python tools/govern_month_format.py --dry-run        # 预览：脏行统计 + 一致性检查
  python tools/govern_month_format.py                  # 执行：备份 → 清洗 → 验证 → 前后对账
  python tools/govern_month_format.py --no-acceptance  # 执行但不跑对账（快速环境）
  python tools/govern_month_format.py --rollback governance_backup_XXXX.jsonl
"""
import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.governance import TARGETS, run_governance, rollback


def mysqldump_backup(backup_dir: Path):
    """mysqldump 三表备份；不可用则跳过（回放清单即精确备份）。密码经 MYSQL_PWD 传递。"""
    try:
        subprocess.run(["mysqldump", "--version"], capture_output=True, check=True, timeout=10)
    except Exception:
        print("⚠ mysqldump 不可用，跳过 SQL 备份（回放清单即精确备份）")
        return None
    out = backup_dir / f"govern_mysqldump_{datetime.now():%Y%m%d_%H%M%S}.sql"
    env = dict(os.environ, MYSQL_PWD=settings.DB_PASSWORD)
    cmd = ["mysqldump", f"-h{settings.DB_HOST}", f"-P{settings.DB_PORT}",
           f"-u{settings.DB_USER}", "--single-transaction", "--no-tablespaces",
           settings.DB_NAME, *[t for t, *_ in TARGETS]]
    with open(out, "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=env, timeout=600)
    if r.returncode != 0:
        print(f"⚠ mysqldump 失败: {r.stderr.decode(errors='replace')[:200]}")
        return None
    print(f"✓ mysqldump 备份: {out}")
    return str(out)


def run_acceptance() -> dict:
    """跑三方对账（ODS ↔ fact ↔ 快照）→ {ok, checks, mismatches, snapshots_failed}"""
    print("  运行 acceptance.py 三方对账 ...", flush=True)
    t0 = time.time()
    r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "acceptance.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=3600)
    out = r.stdout or ""
    m = re.search(r"独立复算对账:\s*(\d+) 个检查点，不一致 (\d+)", out)
    s = re.search(r"核算快照一致性: 成功 (\d+)，失败 (\d+)", out)
    result = {
        "ok": r.returncode == 0,
        "checks": int(m.group(1)) if m else None,
        "mismatches": int(m.group(2)) if m else None,
        "snapshots_failed": int(s.group(2)) if s else None,
        "returncode": r.returncode,
        "seconds": round(time.time() - t0, 1),
    }
    if not m:
        print(out[-1500:])
        print(f"acceptance 输出解析失败（returncode={r.returncode}）")
    return result


def main():
    ap = argparse.ArgumentParser(description="费用月份格式治理（组 10.1 / D-03）")
    ap.add_argument("--dry-run", action="store_true", help="仅预览脏行统计与一致性，不修改数据")
    ap.add_argument("--no-acceptance", action="store_true", help="跳过执行前后对账")
    ap.add_argument("--backup-dir",
                    default=str(Path(__file__).resolve().parent.parent / "archive" / "governance"))
    ap.add_argument("--rollback", metavar="MANIFEST", help="按回放清单回滚旧值")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        if args.rollback:
            n = rollback(db, Path(args.rollback))
            print(f"✓ 回滚完成，共 {n} 行；清单: {args.rollback}")
            return 0

        backup_dir = Path(args.backup_dir)
        if not args.dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            mysqldump_backup(backup_dir)

        reconcile = (run_acceptance if not args.no_acceptance else None)
        result = run_governance(db, dry_run=args.dry_run,
                                backup_dir=backup_dir, reconcile_fn=reconcile)

        print("\n" + "=" * 64)
        print(f"治理报告 (dry_run={result['dry_run']}):")
        for t in result["tables"]:
            print(f"  {t['table_name']}.{t['column']}: 脏行 {t['dirty_rows']}, 清洗 {t['cleaned_rows']}")
        print(f"  等值/前缀一致性: {'✓ 一致' if result['consistent'] else '✗ 不一致'}")
        for p in result.get("problems") or []:
            print(f"    ✗ {p}")
        rec = result.get("reconciliation")
        if rec:
            for tag in ("before", "after"):
                r = rec.get(tag)
                if r:
                    print(f"  对账[{tag}]: {'✓' if r['ok'] else '✗'} 检查点 {r['checks']}, "
                          f"不一致 {r['mismatches']}, 快照失败 {r['snapshots_failed']} ({r['seconds']}s)")
        if result.get("backup"):
            print(f"  回放清单: {result['backup']}")
        if result["aborted"]:
            print("\n结论: ✗ 已中止（清洗前对账存在差异）")
            return 2
        ok = result["consistent"] and not (result.get("problems") or []) \
            and all(r["ok"] for r in (rec or {}).values() if r)
        concl = "✓ 预览完成" if result["dry_run"] else ("✓ 治理成功" if ok else "✗ 存在问题")
        print(f"\n结论: {concl}")
        return 0 if ok else 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())