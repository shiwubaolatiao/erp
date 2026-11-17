"""数据库结构增量迁移 — 补齐后加的列（幂等，可重复执行）

背景：`Base.metadata.create_all()` **只会建新表，不会给已有表加列**。
凡是模型新增字段，都要在这里补一条 ALTER，否则线上库缺列会直接报错。

用法: python tools/migrate_columns.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import text, inspect
from app.core.database import engine, ensure_database
import app.models  # noqa: F401

# (表, 列, 列定义)
COLUMNS = [
    # 广告表无日期列，必须靠归属月份归月（否则广告费无法按月统计）
    ('fact_ad_daily', 'period_month', "VARCHAR(7) NULL COMMENT '报表归属月份 YYYY-MM'"),
    # 批次记录报表归属月份（溯源时确认"这份数据是哪个月的报表"）
    ('import_batch', 'period_month', "VARCHAR(7) NULL COMMENT '报表归属月份 YYYY-MM'"),
    # B-06：归月来源标注（user_specified / default_current）
    ('import_batch', 'period_source',
     "VARCHAR(20) NULL DEFAULT 'user_specified' COMMENT '归月来源 user_specified=上传时指定 / default_current=未指定按当前月'"),
    # 4.4：批次告警（合并工作簿归月偏差等，非失败不计数）
    ('import_batch', 'warnings', "JSON NULL COMMENT '批次告警（kind+message，如合并工作簿归月偏差；非失败，不计数）'"),
    # 组2：快照承载站点级汇率来源（B-01 修复后核算结果可审计）
    ('dws_profit_snapshot', 'rate_source', "VARCHAR(30) NULL COMMENT '汇率来源层级 store_exact/global_exact/...'"),
    ('dws_profit_snapshot', 'rate_detail', "JSON NULL COMMENT '逐站点汇率明细 {country: rate}'"),
    ('dws_profit_snapshot_item', 'country', "VARCHAR(10) NULL COMMENT '站点代码（站点级明细）'"),
    ('dws_profit_snapshot_item', 'exchange_rate', "DECIMAL(12,6) NULL COMMENT '该行使用的汇率'"),
    # 组7 7.7：sheet 处理终态标记（断点续导：processed=True 的 sheet 重跑时跳过）
    ('raw_sheet', 'processed', "TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'sheet 处理终态（1=ODS+DWD 完成；断点续导依据）'"),
    # 组11.3：种子账号初始密码强制改密标记
    ('sys_user', 'must_change_password', "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '种子/管理员重置的初始密码，登录后强制改密'"),
    # mapping_review_enhance：存量重归一留痕（禁止静默改写，spec 6.3.4）
    ('sku_replacement', 'prev_parent_sku',
     "VARCHAR(100) NULL COMMENT '存量重归一修正前的父SKU（首次归并为NULL，禁止静默改写）'"),
    ('sku_replacement', 'updated_at',
     "DATETIME NULL COMMENT '归并关系最近修正时间'"),
]

# (表, 索引名, 索引列) — 幂等补建（create_all 不改已有表索引）
INDEXES = [
    # 广告归月字段建索引（按月聚合很频繁）
    ('fact_ad_daily', 'ix_fact_ad_daily_period_month', 'period_month'),
    # 组7 7.6 性能索引
    ('fact_sales_transaction', 'idx_fst_store_type_date', 'store_id, transaction_type, biz_date'),
    ('fact_ad_daily', 'idx_fad_store_month', 'store_id, period_month'),
    ('dws_profit_snapshot', 'idx_snapshot_country_period', 'country, period_month, version'),
    ('sys_task', 'idx_task_status_created', 'status, created_at'),
    # mapping_review_enhance：基准按 SKU 反查 / 报告列表
    ('mapping_baseline_item', 'idx_baseline_store_sku', 'store_id, sku'),
    ('mapping_op_report', 'idx_report_op_type_time', 'op_type, created_at'),
]


def main():
    ensure_database()
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, col, ddl in COLUMNS:
            existing = {c['name'] for c in insp.get_columns(table)} if insp.has_table(table) else set()
            if col in existing:
                print(f'  skip  {table}.{col}（已存在）')
                continue
            conn.execute(text(f'ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}'))
            print(f'  added {table}.{col}')
        for table, idx_name, cols in INDEXES:
            try:
                existing_idx = {i['name'] for i in insp.get_indexes(table)} if insp.has_table(table) else set()
                if idx_name in existing_idx:
                    print(f'  skip  index {idx_name}（已存在）')
                    continue
                conn.execute(text(f'CREATE INDEX `{idx_name}` ON `{table}` ({cols})'))
                print(f'  added index {idx_name}')
            except Exception as e:
                print(f'  index warn {idx_name}:', e)
    print('结构迁移完成')


if __name__ == '__main__':
    main()
