"""数据现状快照 — 表计数 + 月份分布（排查用）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import text
from app.core.database import SessionLocal

TABLES = [
    'import_batch', 'raw_sheet_row', 'fact_sales_transaction', 'fact_ad_daily',
    'fact_fee_storage', 'fact_fee_aged_storage', 'fact_fee_return', 'fact_fee_removal',
    'fact_fee_inbound_config', 'fact_cashflow_transfer',
    'store_product', 'store_sku_mapping', 'sku_replacement', 'product_cost_history',
    'exchange_rate', 'cost_missing_alert', 'dws_profit_snapshot', 'dws_profit_snapshot_item',
]


def main():
    db = SessionLocal()
    try:
        print('=== 表计数 ===')
        for t in TABLES:
            try:
                n = db.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
                print(f'  {t:30s} {n}')
            except Exception as e:
                print(f'  {t:30s} ERR {e}')
        print('\n=== fact_sales_transaction 月份分布 ===')
        rows = db.execute(text(
            "SELECT DATE_FORMAT(biz_date, '%Y-%m') AS m, COUNT(*) AS c "
            "FROM fact_sales_transaction GROUP BY m ORDER BY m")).fetchall()
        for m, c in rows:
            print(f'  {m}  {c}')
        if not rows:
            print('  （无数据或 biz_date 全为空）')
            nul = db.execute(text('SELECT COUNT(*) FROM fact_sales_transaction WHERE biz_date IS NULL')).scalar()
            print(f'  biz_date 为 NULL 的行数: {nul}')
        print('\n=== 每店铺 fact 行数 ===')
        rows = db.execute(text(
            "SELECT s.store_code, COUNT(f.id) FROM store s "
            "LEFT JOIN fact_sales_transaction f ON f.store_id = s.id "
            "GROUP BY s.store_code ORDER BY s.store_code")).fetchall()
        for code, c in rows:
            print(f'  {code:6s} {c}')
        print('\n=== 批次状态分布 ===')
        for st, c in db.execute(text(
                'SELECT status, COUNT(*) FROM import_batch GROUP BY status')).fetchall():
            print(f'  {st:10s} {c}')
    finally:
        db.close()


if __name__ == '__main__':
    main()
