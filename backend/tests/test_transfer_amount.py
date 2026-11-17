"""S19 Transfer 提款金额死分支修复（B-07，tasks.md 4.2）

修复前：pipeline 对 Transfer 行取金额的写法是
    `gv('line_total') if col('total') >= 0 else gv('total')`
canonical 表头字段名是 'total'（不存在 'line_total' 别名），因此 total 列存在时
恒取 None → parse_decimal(None, 0) → 金额恒 0（提款数据全丢）。

期望值人工设定：Transfer 行 total=88.88 → fact_cashflow_transfer.amount == 88.88；
total 列缺失的 Transfer 行 → 进异常池（missing_column），不产 fact 行、不静默 0。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models import Store

from conftest import build_xlsx, sql, sql_one, upload_and_wait

TFR_CODE = 'TFR2-TEST'

BASE_HEADER = ['date/time', 'type', 'order id', 'sku', 'asin', 'quantity',
               'product sales', 'product sales tax', 'shipping credits',
               'selling fees', 'fba fees', 'other', 'total']
NO_TOTAL_HEADER = BASE_HEADER[:-1]          # 去掉 total 列
TRANSFER_ROW = ['2026-01-20 10:00:00 UTC', 'Transfer', '', '', '', None,
                0, 0, 0, 0, 0, 0, 88.88]
TRANSFER_ROW_NO_TOTAL = TRANSFER_ROW[:-1]   # 与无 total 表头对齐


def _ensure_store() -> None:
    db = SessionLocal()
    try:
        if not db.query(Store).filter(Store.store_code == TFR_CODE).first():
            db.add(Store(store_code=TFR_CODE, store_name='Transfer金额测试店', region='EU'))
            db.commit()
    finally:
        db.close()


class TestTransferAmount:
    def test_amount_takes_total_column_value(self, client, admin_headers, tmp_path):
        """金额 == total 列原值（修复前恒 0）"""
        _ensure_store()
        f = build_xlsx(tmp_path / 'TFR2_2026-01.xlsx', [
            ('UK销售', [BASE_HEADER] + [TRANSFER_ROW]),
        ])
        st = upload_and_wait(client, admin_headers, f, f.name, TFR_CODE, '2026-01')
        assert st['status'] == 'success', st.get('error')
        row = sql_one(
            "SELECT f.* FROM fact_cashflow_transfer f "
            "JOIN store s ON s.id=f.store_id WHERE s.store_code=:c", c=TFR_CODE)
        assert row, 'Transfer 行未落 fact_cashflow_transfer'
        assert abs(float(row['amount']) - 88.88) < 0.005, \
            f"amount={row['amount']} != 88.88（死分支修复失效？）"
        # 溯源：raw_row_id 指向原始行
        assert row['raw_row_id'], '缺失 raw_row_id 溯源键'

    def test_missing_total_column_goes_to_error_pool(self, client, admin_headers, tmp_path):
        """total 列缺失 → 异常行（missing_column），不产 fact、不静默 0"""
        _ensure_store()
        f = build_xlsx(tmp_path / 'TFR2-NOTOTAL_2026-01.xlsx', [
            ('UK销售', [NO_TOTAL_HEADER] + [TRANSFER_ROW_NO_TOTAL]),
        ])
        st = upload_and_wait(client, admin_headers, f, f.name, TFR_CODE, '2026-01')
        assert st['status'] in ('success', 'partial'), st.get('error')
        errs = sql(
            "SELECT e.* FROM import_row_error e "
            "JOIN import_batch b ON b.id=e.batch_id "
            "JOIN store s ON s.id=b.store_id "
            "WHERE s.store_code=:c AND e.error_type='missing_column'", c=TFR_CODE)
        assert errs, 'Transfer 缺 total 列未记异常行'
        assert any('Transfer' in (e['error_message'] or '') for e in errs), \
            [e['error_message'] for e in errs]
        # 不产 fact 行（按批次过滤，排除其他用例数据；既不能是 0 也不能静默入库）
        row = sql_one(
            "SELECT f.* FROM fact_cashflow_transfer f "
            "JOIN import_batch b ON b.id=f.batch_id "
            "WHERE b.file_name=:n", n='TFR2-NOTOTAL_2026-01.xlsx')
        assert row is None, f'缺 total 列的 Transfer 行不应入库: {row}'