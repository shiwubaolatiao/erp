# -*- coding: utf-8 -*-
"""存量 amzn.gr.* 替换件重归一 CLI（mapping_review_enhance 组2.7）

薄封装：核心逻辑在 app/services/mapping/similarity.py::realign_replacements（与 API 同源）。

安全设计：
  · 默认 --dry-run：只读取与重算，零写库
  · 正式执行前 mysqldump 备份 sku_replacement（MYSQL_PWD 传密码不落命令行）
  · 修正留痕：旧值写入 prev_parent_sku、刷新 updated_at（禁止静默改写）
  · 幂等可重跑：第二次执行 changed 应为 0

用法：
  python tools/realign_replacements.py --dry-run              # 预览变更明细
  python tools/realign_replacements.py --store LMG            # 限定店铺正式执行
  python tools/realign_replacements.py                        # 全部店铺正式执行（先备份）
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Store
from app.services.mapping.similarity import realign_replacements


def mysqldump_backup(backup_dir: Path):
    """mysqldump 备份 sku_replacement；不可用则跳过（prev_parent_sku 亦可反向恢复）"""
    try:
        subprocess.run(["mysqldump", "--version"], capture_output=True, check=True, timeout=10)
    except Exception:
        print("⚠ mysqldump 不可用，跳过 SQL 备份（prev_parent_sku 留痕仍可反向恢复）")
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"sku_replacement_{datetime.now():%Y%m%d_%H%M%S}.sql"
    env = dict(os.environ, MYSQL_PWD=settings.DB_PASSWORD)
    cmd = ["mysqldump", f"-h{settings.DB_HOST}", f"-P{settings.DB_PORT}",
           f"-u{settings.DB_USER}", "--single-transaction", "--no-tablespaces",
           settings.DB_NAME, "sku_replacement"]
    with open(out, "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=env, timeout=600)
    if r.returncode != 0:
        print(f"⚠ mysqldump 失败: {r.stderr.decode(errors='replace')[:200]}")
        return None
    print(f"✓ mysqldump 备份: {out}")
    return str(out)


def print_report(result: dict, limit: int = 20) -> None:
    """打印重归一报告（计数 + 变更明细前 N 条）"""
    s = result["summary"]
    print("\n" + "=" * 64)
    print(f"重归一报告 (dry_run={result['dry_run']}, 范围={result['store_scope']}):")
    print(f"  扫描 {s['scanned']} | 一致 {s['unchanged']} | 修正 {s['changed']} "
          f"| 旧目标非法 {s['invalid_target']} | 保留原值 {s['kept_raw']}")
    changed = result["details"].get("changed") or []
    if changed:
        print(f"\n  变更明细（前 {min(limit, len(changed))} 条，共 {len(changed)} 条）:")
        for c in changed[:limit]:
            print(f"    [{c['store_code']}] {c['replacement_sku'][:52]:52s} "
                  f"{c['old_parent_sku']!r} -> {c['new_parent_sku']!r}")
    unclassified = result["details"].get("unclassified") or []
    if unclassified:
        print(f"\n  待归类（前 {min(limit, len(unclassified))} 条，共 {len(unclassified)} 条）:")
        for u in unclassified[:limit]:
            cand = ", ".join(f"{c['sku']}({c['score']})" for c in u["candidates"][:3]) or "无候选"
            print(f"    [{u['store_code']}] {u['replacement_sku'][:52]:52s} 原因={u['reason']} 候选: {cand}")


def main() -> int:
    ap = argparse.ArgumentParser(description="存量 amzn.gr.* 替换件重归一（组2.7）")
    ap.add_argument("--store", help="限定店铺代码（默认全部）")
    ap.add_argument("--dry-run", action="store_true", help="仅预览，不写库（默认行为）")
    ap.add_argument("--execute", action="store_true", help="正式执行（不加此参数即为 dry-run）")
    ap.add_argument("--backup-dir",
                    default=str(Path(__file__).resolve().parent.parent / "archive" / "mapping"))
    args = ap.parse_args()

    dry_run = not args.execute
    db = SessionLocal()
    try:
        store_id = None
        if args.store:
            st = db.query(Store).filter(Store.store_code == args.store.upper()).first()
            if not st:
                print(f"✗ 店铺不存在: {args.store}")
                return 1
            store_id = st.id

        if not dry_run:
            mysqldump_backup(Path(args.backup_dir))

        result = realign_replacements(db, store_id=store_id, dry_run=dry_run)
        print_report(result)
        print(f"\n结论: {'✓ 预览完成（零写库）' if dry_run else '✓ 执行完成'}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())