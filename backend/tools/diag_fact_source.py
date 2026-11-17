"""排查：某店铺/国家 的 fact 行来自哪些 sheet（用于核对对账口径）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import text
from app.core.database import SessionLocal


def main():
    store_code = sys.argv[1] if len(sys.argv) > 1 else 'MGK'
    country = sys.argv[2] if len(sys.argv) > 2 else 'SA'
    period = sys.argv[3] if len(sys.argv) > 3 else '2026-06'
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT rs.sheet_name, rs.report_type, rs.country AS sheet_country,
                   COUNT(*) AS n
            FROM fact_sales_transaction f
            JOIN raw_sheet_row rr ON rr.id = f.raw_row_id
            JOIN raw_sheet rs ON rs.id = rr.sheet_id
            JOIN store s ON s.id = f.store_id
            WHERE s.store_code = :sc AND f.country = :c
              AND DATE_FORMAT(f.biz_date, '%Y-%m') = :pm
            GROUP BY rs.sheet_name, rs.report_type, rs.country
            ORDER BY n DESC
        """), {'sc': store_code, 'c': country, 'pm': period}).fetchall()
        print(f'{store_code} / {country} / {period} 的 fact 来源 sheet：')
        if not rows:
            print('  （无）')
        for name, rtype, sc, n in rows:
            print(f'  sheet={name!r:24s} type={rtype!r:16s} sheet_country={sc!r:6s} rows={n}')

        # 对比：整个店铺各 sheet 的类型分布
        print(f'\n{store_code} 全部 sheet 类型分布：')
        for name, rtype, sc, n in db.execute(text("""
                SELECT rs.sheet_name, rs.report_type, rs.country, rs.row_count
                FROM raw_sheet rs JOIN import_batch b ON b.id = rs.batch_id
                WHERE b.store_id = (SELECT id FROM store WHERE store_code = :sc)
                ORDER BY rs.report_type, rs.sheet_name
        """), {'sc': store_code}).fetchall():
            print(f'  {str(rtype):16s} {name[:26]:28s} country={str(sc):5s} rows={n}')
    finally:
        db.close()


if __name__ == '__main__':
    main()
