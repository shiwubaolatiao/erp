"""排查：广告费/仓储费无法按月归集的原因"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import text
from app.core.database import SessionLocal


def main():
    db = SessionLocal()
    try:
        print('=== fact_ad_daily 归月线索 ===')
        print('总行数:', db.execute(text('SELECT COUNT(*) FROM fact_ad_daily')).scalar())
        print('有 biz_date 的行:', db.execute(text(
            'SELECT COUNT(*) FROM fact_ad_daily WHERE biz_date IS NOT NULL')).scalar())
        print('spend 非 0 的行:', db.execute(text(
            'SELECT COUNT(*) FROM fact_ad_daily WHERE spend <> 0')).scalar())
        rows = db.execute(text(
            'SELECT b.imported_at, b.file_name, COUNT(*) n FROM fact_ad_daily a '
            'JOIN import_batch b ON b.id = a.batch_id GROUP BY b.id ORDER BY b.id LIMIT 6')).fetchall()
        print('  广告行按批次导入时间分布（前 6 批）:')
        for imp, fn, n in rows:
            print(f'    批次导入时间={imp}  文件={fn[:28]:30s} 行数={n}')

        print('\n=== fact_fee_storage.month_of_charge 实际值 ===')
        for v, n in db.execute(text(
                "SELECT month_of_charge, COUNT(*) FROM fact_fee_storage "
                "GROUP BY month_of_charge ORDER BY COUNT(*) DESC LIMIT 8")).fetchall():
            print(f'    {v!r:24s} {n}')

        print('\n=== fact_fee_return.month_of_charge 实际值 ===')
        for v, n in db.execute(text(
                "SELECT month_of_charge, COUNT(*) FROM fact_fee_return "
                "GROUP BY month_of_charge ORDER BY COUNT(*) DESC LIMIT 5")).fetchall():
            print(f'    {v!r:24s} {n}')

        print('\n=== fact_fee_aged_storage.snapshot_date 实际值 ===')
        for v, n in db.execute(text(
                "SELECT snapshot_date, COUNT(*) FROM fact_fee_aged_storage "
                "GROUP BY snapshot_date ORDER BY COUNT(*) DESC LIMIT 5")).fetchall():
            print(f'    {v!r:24s} {n}')

        print('\n=== 汇率表 period_month 实际值 ===')
        for v, n in db.execute(text(
                "SELECT period_month, COUNT(*) FROM exchange_rate "
                "GROUP BY period_month ORDER BY period_month LIMIT 10")).fetchall():
            print(f'    {v!r:12s} {n}')

        print('\n=== product_cost_history.period 实际值 ===')
        for v, n in db.execute(text(
                "SELECT period, COUNT(*) FROM product_cost_history "
                "GROUP BY period ORDER BY period LIMIT 10")).fetchall():
            print(f'    {v!r:12s} {n}')
    finally:
        db.close()


if __name__ == '__main__':
    main()
