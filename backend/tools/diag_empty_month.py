"""排查 fact_fee_storage.month_of_charge 为空的行来自哪些 sheet"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import text
from app.core.database import SessionLocal


def main():
    db = SessionLocal()
    try:
        print('=== month_of_charge 为空的行，按 sheet 归组 ===')
        rows = db.execute(text(
            "SELECT rs.sheet_name, b.file_name, COUNT(*) n, "
            "       ROUND(SUM(f.estimated_monthly_storage_fee),2) amt "
            "FROM fact_fee_storage f "
            "JOIN raw_sheet_row rr ON rr.id = f.raw_row_id "
            "JOIN raw_sheet rs ON rs.id = rr.sheet_id "
            "JOIN import_batch b ON b.id = f.batch_id "
            "WHERE f.month_of_charge = '' OR f.month_of_charge IS NULL "
            "GROUP BY rs.sheet_name, b.file_name ORDER BY n DESC LIMIT 15")).fetchall()
        for sn, fn, n, amt in rows:
            print(f'   {sn[:20]:22s} {fn[:26]:28s} 行={n:5d} 金额={amt}')

        print('\n=== 这些 sheet 的表头里有没有 month_of_charge 类列 ===')
        sample = db.execute(text(
            "SELECT rr.headers_json FROM fact_fee_storage f "
            "JOIN raw_sheet_row rr ON rr.id = f.raw_row_id "
            "WHERE (f.month_of_charge = '' OR f.month_of_charge IS NULL) LIMIT 3")).fetchall()
        import json
        for (hj,) in sample:
            hdr = json.loads(hj) if isinstance(hj, str) else hj
            hit = [h for h in (hdr or []) if 'month' in str(h).lower() or '月' in str(h)]
            print(f'   表头 {len(hdr or [])} 列；含月相关列: {hit}')
            print(f'   全部表头: {[str(h)[:22] for h in (hdr or [])[:34]]}')

        print('\n=== 对比：有 month_of_charge 的行，表头样例 ===')
        sample2 = db.execute(text(
            "SELECT rr.headers_json FROM fact_fee_storage f "
            "JOIN raw_sheet_row rr ON rr.id = f.raw_row_id "
            "WHERE f.month_of_charge <> '' AND f.month_of_charge IS NOT NULL LIMIT 1")).fetchall()
        for (hj,) in sample2:
            hdr = json.loads(hj) if isinstance(hj, str) else hj
            hit = [h for h in (hdr or []) if 'month' in str(h).lower() or '月' in str(h)]
            print(f'   含月相关列: {hit}')
    finally:
        db.close()


if __name__ == '__main__':
    main()
