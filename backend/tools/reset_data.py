"""开发用数据重置工具 — 清空业务数据（保留账号/店铺/站点/配置）
按外键依赖顺序删除，可重复执行。

用法: python tools/reset_data.py [--all]
  --all  同时清空账号/店铺/站点/配置（仅开发环境使用）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import text
from app.core.database import SessionLocal

# 按依赖顺序：子表 → 父表
BUSINESS_TABLES = [
    'dws_profit_snapshot_item',
    'dws_profit_snapshot',
    'cost_missing_alert',
    'import_row_error',          # 引用 raw_sheet
    'fact_cashflow_transfer',
    'fact_sales_transaction',    # 引用 raw_sheet_row
    'fact_ad_daily',
    'fact_fee_storage',
    'fact_fee_aged_storage',
    'fact_fee_removal',
    'fact_fee_inbound_config',
    'fact_fee_return',
    'raw_sheet_row',             # 引用 import_batch / raw_sheet
    'raw_sheet',                 # 引用 import_batch / import_file
    'import_file',
    'import_batch',
    'product_cost_history',
    'store_sku_mapping',
    'sku_replacement',
    'store_product',
    'exchange_rate',
    'header_alias',
    'sheet_alias_map',
    'ads_action_item',
    'ads_data_record',
    'ads_message',
    'ads_session',
    'weekly_report_images',
    'weekly_reports',
    'operation_log',
]

CONFIG_TABLES = ['sys_user', 'store', 'site']


def main():
    """清空业务数据（按外键依赖顺序删除；--all 时连账号/店铺/站点一起清）"""
    all_data = '--all' in sys.argv
    db = SessionLocal()
    try:
        db.execute(text('SET FOREIGN_KEY_CHECKS=0'))
        tables = BUSINESS_TABLES + (CONFIG_TABLES if all_data else [])
        for t in tables:
            n = db.execute(text(f'DELETE FROM `{t}`')).rowcount
            if n:
                print(f'  cleared {t:32s} {n} rows')
        db.execute(text('SET FOREIGN_KEY_CHECKS=1'))
        db.commit()
        print('重置完成（账号/店铺/站点保留）' if not all_data else '全量重置完成')
    finally:
        db.close()


if __name__ == '__main__':
    main()
