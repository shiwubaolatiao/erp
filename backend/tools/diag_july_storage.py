"""排查 2026-07 仓储费为 0 的原因"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import text
from app.core.database import SessionLocal


def main():
    db = SessionLocal()
    try:
        print('fact_fee_storage.month_of_charge 全部分布:')
        for r in db.execute(text(
                "SELECT month_of_charge, COUNT(*), ROUND(SUM(estimated_monthly_storage_fee),2) "
                "FROM fact_fee_storage GROUP BY month_of_charge ORDER BY month_of_charge")):
            print(f'   {str(r[0]):10s} 行={r[1]:6d} 金额={r[2]}')

        print('\n各月份批次里的仓储费 sheet:')
        rows = db.execute(text(
            "SELECT b.period_month, b.file_name, rs.sheet_name, rs.report_type, rs.row_count "
            "FROM raw_sheet rs JOIN import_batch b ON b.id = rs.batch_id "
            "WHERE rs.sheet_name LIKE '%仓储%' ORDER BY b.period_month, b.file_name")).fetchall()
        seen = {}
        for pm, fn, sn, rt, n in rows:
            seen.setdefault(pm, []).append((fn, sn, rt, n))
        for pm in sorted(k for k in seen if k):
            lst = seen[pm]
            print(f'   {pm}: {len(lst)} 个仓储 sheet，合计 {sum(x[3] for x in lst)} 行')
            for fn, sn, rt, n in lst[:4]:
                print(f'       {fn[:30]:32s} {sn:12s} type={str(rt):10s} rows={n}')
    finally:
        db.close()


if __name__ == '__main__':
    main()
