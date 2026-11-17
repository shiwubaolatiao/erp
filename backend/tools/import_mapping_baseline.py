# -*- coding: utf-8 -*-
"""07 基准导入 CLI（mapping_review_enhance 组3.4）— 与 API 同源（services/mapping/baseline.py）

用法：
  python tools/import_mapping_baseline.py --dir "C:/Users/五包辣条/Desktop/汇总/07" --dry-run
  python tools/import_mapping_baseline.py --dir "..."            # 正式落库（幂等）
  python tools/import_mapping_baseline.py --file a.xlsx --file b.xlsx
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.mapping.baseline import import_baseline


def main() -> int:
    ap = argparse.ArgumentParser(description="07 基准产品信息表导入（组3.4）")
    ap.add_argument("--dir", default=None,
                    help="基准目录（默认 settings.MAPPING_BASELINE_DIR）")
    ap.add_argument("--file", action="append", default=[],
                    help="显式文件路径（可多次，与 --dir 二选一）")
    ap.add_argument("--dry-run", action="store_true", help="仅解析不落库")
    args = ap.parse_args()

    dir_path = args.dir or settings.MAPPING_BASELINE_DIR
    file_paths = args.file or None
    if not file_paths and not dir_path:
        print("✗ 未提供 --dir 或 --file")
        return 1

    db = SessionLocal()
    try:
        result = import_baseline(db, dir_path=dir_path, file_paths=file_paths,
                                 dry_run=args.dry_run)
        s = result['summary']
        print("=" * 64)
        print(f"基准导入报告 (dry_run={result['dry_run']}):")
        print(f"  文件: 总 {s['files_total']} / 解析 {s['files_ok']} / 跳过 "
              f"{s['files_skipped']} / 失败 {s['files_failed']}")
        print(f"  条目: {s['items_total']}（去重合并 {s['dedup_merged']}，空值行 {s['empty_rows']}）"
              f"  基准内冲突: {s['baseline_conflicts']} 组")
        if 'items_inserted' in s:
            print(f"  本次新增落库: {s['items_inserted']} 条（幂等：重复执行为 0）")
        for f in result['details']['files']:
            mark = {'ok': '✓', 'skipped': '⏭', 'failed': '✗'}[f['status']]
            print(f"    {mark} {f['file']:34s} {f['store_code'] or '-':4s} "
                  f"{f['sheet_name']:8s} 行 {f['rows']:4d} {f['message']}")
        for c in (result['details'].get('conflicts') or [])[:10]:
            print(f"    ⚠ 冲突 [{c['store_code']}] {c['sku']} -> {c['asins']}")
        if result.get('report_id'):
            print(f"  报告 report_id: {result['report_id']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())