# -*- coding: utf-8 -*-
"""pending 映射批量确认 CLI（mapping_review_enhance 组4.6）— 与 API 同源

安全设计：
  · 默认 --dry-run 零写库；正式执行分片 500 条提交、幂等续跑
  · --rollback-report <id>：按执行报告把确认项安全置回 pending
  · 漂移红线：同 SKU 多候选全部进冲突清单，不择优（DD6）

用法：
  python tools/bulk_confirm_mappings.py --dry-run          # 全部店铺预览
  python tools/bulk_confirm_mappings.py --store LMG        # 单店铺正式执行
  python tools/bulk_confirm_mappings.py --rollback-report 12
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.database import SessionLocal
from app.models import SysUser
from app.services.mapping.bulk_confirm import (
    BaselineNotImported, bulk_confirm, rollback_by_report,
)


def _default_operator(db):
    """CLI 默认操作人：admin 种子账号（正式执行必须留确认人，spec 5.2.1.7）"""
    admin = db.query(SysUser).filter(SysUser.username == 'admin').first()
    return (admin.id, admin.username) if admin else (None, 'cli')


def print_report(result: dict, limit: int = 15) -> None:
    s = result['summary']
    print("=" * 64)
    print(f"批量确认报告 (dry_run={result['dry_run']}, 范围={result['store_scope']}):")
    print(f"  待确认 {s['total_pending']} | 可确认 {s['confirmed']} | 未覆盖 {s['uncovered']} "
          f"| 冲突 {s['conflict']} | 失败 {s['failed']} | 已转正跳过 {s['skipped_already_normal']}")
    d = result['details']
    if d.get('uncovered'):
        print(f"\n  未覆盖（前 {min(limit, len(d['uncovered']))} 条，共 {len(d['uncovered'])} 条）:")
        for m in d['uncovered'][:limit]:
            print(f"    [{m['store_code']}] {m['sku'][:40]:40s} {m['asin']}")
    if d.get('conflict'):
        print(f"\n  冲突（前 {min(limit, len(d['conflict']))} 条，共 {len(d['conflict'])} 条）:")
        for m in d['conflict'][:limit]:
            extra = m.get('baseline_asin') or m.get('baseline_skus') or ''
            print(f"    [{m['store_code']}] {m['sku'][:40]:40s} {m['asin']} "
                  f"冲突={m['conflict_type']} 基准={extra}")
    if d.get('failed'):
        print(f"\n  失败（{len(d['failed'])} 条）:")
        for m in d['failed'][:limit]:
            print(f"    [{m['store_code']}] {m['sku'][:40]:40s} {m['error']}")
    if result.get('report_id'):
        print(f"\n  报告 report_id: {result['report_id']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="pending 映射批量确认（组4.6）")
    ap.add_argument("--store", help="限定店铺代码（默认全部）")
    ap.add_argument("--dry-run", action="store_true", help="仅预览，零写库（默认行为）")
    ap.add_argument("--execute", action="store_true", help="正式执行（不加即 dry-run）")
    ap.add_argument("--rollback-report", type=int, default=None, metavar="REPORT_ID",
                    help="按执行报告回滚确认项")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        if args.rollback_report:
            n = rollback_by_report(db, args.rollback_report)
            print(f"✓ 回滚完成：{n} 条已置回 pending（报告 {args.rollback_report}）")
            return 0

        dry_run = not args.execute
        op_id, op_name = _default_operator(db)
        result = bulk_confirm(db, store=args.store, dry_run=dry_run,
                              operator_user_id=op_id, operator_username=op_name)
        print_report(result)
        print(f"\n结论: {'✓ 预览完成（零写库）' if dry_run else '✓ 执行完成'}")
        return 0
    except BaselineNotImported as exc:
        print(f"✗ {exc}")
        return 2
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())