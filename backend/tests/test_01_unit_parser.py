"""S1 单元测试 — 报表解析引擎（TC-UNIT-P-001~030）

依据 docs/测试文档.md 第 4 章 S1。方法：等价类划分 + 边界值 + 历史错误回归。
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.importer.parser import (
    parse_date, parse_decimal, normalize_transaction_type, normalize_header,
    map_header_to_canonical, detect_header_row, detect_report_type,
    detect_country_from_sheet_name, parse_store_from_filename,
    find_col, find_col_like,
)

# ---------------- 日期解析 TC-UNIT-P-001~008 ----------------


class TestParseDate:
    def test_iso_with_tz(self):  # TC-UNIT-P-001
        d = parse_date('2026-06-15T00:02:00+00:00')
        assert (d.year, d.month, d.day, d.hour, d.minute) == (2026, 6, 15, 0, 2)

    def test_german_dotted(self):  # TC-UNIT-P-002
        d = parse_date('01.01.2026 14:03:54 UTC')
        assert (d.year, d.month, d.day, d.hour, d.minute, d.second) == (2026, 1, 1, 14, 3, 54)

    def test_day_first_english(self):  # TC-UNIT-P-003
        d = parse_date('31 May 2026 23:04:30')
        assert (d.year, d.month, d.day) == (2026, 5, 31)

    def test_12h_pm_not_stripped(self):  # TC-UNIT-P-004 ★ 历史错误回归
        d = parse_date('3 Jan 2026 3:08:30 PM UTC')
        assert d.hour == 15, f"PM 被当时区剥离: {d}"

    def test_ampm_variants(self):  # TC-UNIT-P-005
        assert parse_date('Jan 2, 2026 6:15:36 p.m. PST').hour == 18
        assert parse_date('Mar 1, 2026 1:52:01 AM PST').hour == 1

    def test_spanish_month_gmt_negative(self):  # TC-UNIT-P-006
        d = parse_date('2 abr 2026 4:48:25 p.m. GMT-7')
        assert (d.year, d.month, d.day, d.hour) == (2026, 4, 2, 16)

    def test_noon_boundary(self):  # TC-UNIT-P-007
        d = parse_date('Jan 2, 2026 12:01:11 p.m. PST')
        assert d.hour == 12

    def test_invalid_returns_none(self):  # TC-UNIT-P-008
        assert parse_date('') is None
        assert parse_date(None) is None
        assert parse_date('not a date') is None


# ---------------- 数值解析 TC-UNIT-P-009~013 ----------------


class TestParseDecimal:
    def test_european_format(self):  # TC-UNIT-P-009
        assert parse_decimal('1.234,56') == Decimal('1234.56')

    def test_us_format(self):  # TC-UNIT-P-010
        assert parse_decimal('1,234.56') == Decimal('1234.56')

    def test_unicode_minus(self):  # TC-UNIT-P-011
        assert parse_decimal('−5.5') == Decimal('-5.5')

    def test_paren_negative(self):  # TC-UNIT-P-012
        assert parse_decimal('(8.33)') == Decimal('-8.33')

    def test_invalid(self):  # TC-UNIT-P-013
        assert parse_decimal('') is None
        assert parse_decimal('abc') is None


# ---------------- 交易类型归一 TC-UNIT-P-014~016 ----------------


class TestNormalizeTransactionType:
    def test_german(self):  # TC-UNIT-P-014
        assert normalize_transaction_type('Bestellung') == 'Order'
        assert normalize_transaction_type('Erstattung') == 'Refund'
        assert normalize_transaction_type('Anpassung') == 'Adjustment'
        assert normalize_transaction_type('Servicegebühr') == 'Service Fee'
        assert normalize_transaction_type('Überweisung') == 'Transfer'

    def test_spanish_french(self):  # TC-UNIT-P-015
        assert normalize_transaction_type('Pedido') == 'Order'
        assert normalize_transaction_type('Remboursement') == 'Refund'

    def test_unknown_passthrough(self):  # TC-UNIT-P-016（不入利润的关键前提）
        assert normalize_transaction_type('未知类型') == '未知类型'
        assert normalize_transaction_type('') == ''


# ---------------- 表头归一与映射 TC-UNIT-P-017~020 ----------------


class TestHeaderNormalize:
    def test_german_headers(self):  # TC-UNIT-P-017
        assert normalize_header(['Datum/Uhrzeit', 'Menge', 'Umsätze']) == \
            ['date/time', 'quantity', 'product sales']

    def test_spanish_headers(self):  # TC-UNIT-P-018
        assert normalize_header(['fecha y hora', 'cantidad']) == ['date/time', 'quantity']

    def test_separator_folding(self):  # TC-UNIT-P-019
        assert normalize_header(['snapshot-date', 'month_of_charge']) == \
            ['snapshot date', 'month of charge']

    def test_canonical_mapping(self):  # TC-UNIT-P-020
        header = ['date/time', 'type', 'order id', 'sku', 'asin', 'quantity',
                  'product sales', 'product sales tax', 'shipping credits',
                  'selling fees', 'fba fees', 'other', 'total']
        m = map_header_to_canonical(header)
        for f in ('date_time', 'transaction_type', 'order_id', 'sku', 'asin',
                  'quantity', 'product_sales', 'selling_fees', 'fba_fees', 'total'):
            assert f in m, f"缺少 {f}: {m}"
        assert header[m['product_sales']] == 'product sales'
        assert header[m['date_time']] == 'date/time'


# ---------------- 动态表头定位 TC-UNIT-P-021~023 ----------------


class TestDetectHeaderRow:
    def test_header_at_row10(self):  # TC-UNIT-P-021（0 基 = 9）
        rows = [[f'多语言说明 {i}'] for i in range(9)]
        rows.append(['date/time', 'type', 'sku', 'asin', 'quantity', 'product sales'])
        assert detect_header_row(rows) == 9

    def test_header_first_row(self):  # TC-UNIT-P-022
        rows = [['date/time', 'type', 'sku', 'quantity'], ['x', 'y', 'z', '1']]
        assert detect_header_row(rows) == 0

    def test_no_header(self):  # TC-UNIT-P-023
        rows = [['随便', '内容', '这里'], ['1', '2', '3']]
        assert detect_header_row(rows) == -1


# ---------------- sheet 类型与国家识别 TC-UNIT-P-024~027 ----------------


class TestDetectReportType:
    def test_sales_uk(self):  # TC-UNIT-P-024
        rt, c = detect_report_type('UK销售')
        assert (rt, c) == ('sales', 'UK')

    def test_aged_storage_priority(self):  # TC-UNIT-P-025（必须先于普通仓储费）
        rt, c = detect_report_type('DE超龄仓储费')
        assert rt == 'aged_storage'
        assert c in ('DE', None)

    def test_exchange_and_product(self):  # TC-UNIT-P-026
        assert detect_report_type('汇率')[0] == 'exchange_rate'
        assert detect_report_type('产品')[0] == 'product'

    def test_unknown(self):  # TC-UNIT-P-027
        assert detect_report_type('随便')[0] is None


# ---------------- 文件名解析 TC-UNIT-P-028~029 ----------------


class TestStoreFromFilename:
    def test_store_and_region(self):  # TC-UNIT-P-028
        assert parse_store_from_filename('MGK-EU_01.xlsx') == ('MGK', 'EU')
        assert parse_store_from_filename('WEL-NA01-03.xlsx') == ('WEL', 'NA')

    def test_no_store(self):  # TC-UNIT-P-029
        assert parse_store_from_filename('报表.xlsx')[0] is None


# ---------------- 列匹配 TC-UNIT-P-030 ----------------


class TestFindCol:
    def test_exact_and_like(self):  # TC-UNIT-P-030
        header = ['date/time', 'Type', ' sku ', 'product sales']
        assert find_col(header, 'type') == 1
        assert find_col_like(header, 'sales') == 3
        assert find_col(header, 'nope') == -1
