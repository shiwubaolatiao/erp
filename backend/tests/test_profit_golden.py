"""利润公式黄金测试 — 用真实报表数值（实测 UK销售 2026-06 样本）锁定公式
公式改动（哪怕是精度调整）必须先在此更新并说明原因。
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.profit.engine import compute_net_profit, compute_net_amazon  # noqa: E402
from app.services.importer.parser import parse_date, parse_decimal  # noqa: E402


def _D(x):
    return Decimal(x)


# ---- 案例1：旧系统 profit.py 公式复算样本（单笔 Order 行，23.32 GBP 售价样例） ----
# 来自旧库 raw_transactions 实测行：
#   ps=23.32 pst=4.66 pc=0 sct=0 gwc=0 gwct=0 promo=0 promo_tax=0 mkt=0
#   sf=-4.67 fba=-1.96 adj=0 → net_amazon = 21.35
# 汇率 UK=9.0，无成本费用 → net_profit = 21.35*9.0 = 192.15
def test_order_line_basic():
    net_amazon = compute_net_amazon(
        _D('23.32'), _D('4.66'), _D('0'), _D('0'), _D('0'), _D('0'),
        _D('0'), _D('0'), _D('0'), _D('-4.67'), _D('-1.96'), _D('0'))
    assert net_amazon == _D('21.35'), f"net_amazon={net_amazon}"
    net = compute_net_profit(
        _D('23.32'), _D('4.66'), _D('0'), _D('0'), _D('0'), _D('0'),
        _D('0'), _D('0'), _D('0'), _D('-4.67'), _D('-1.96'), _D('0'),
        _D('9.0'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'))
    assert net == _D('192.15'), f"net={net}"


# ---- 案例2：完整扣减项（含成本/运费/五项费用） ----
# net_amazon = 100 + 各费用抵扣后 = 84.00；er=6.8
# net = 84*6.8 - 30 - 10 - (2+1+0.5+0.3+0.2)*6.8 = 571.2 - 40 - 27.2 = 504.00
def test_full_deductions():
    net = compute_net_profit(
        _D('100'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'),
        _D('0'), _D('0'), _D('0'), _D('-10'), _D('-6'), _D('0'),
        _D('6.8'), _D('30'), _D('10'),
        _D('2'), _D('1'), _D('0.5'), _D('0.3'), _D('0.2'))
    assert net == _D('504.00'), f"net={net}"


# ---- 案例3：精度 — quantize(0.01) ----
def test_quantize_2dp():
    net = compute_net_profit(
        _D('10'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'),
        _D('0'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'),
        _D('7.5'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'))
    assert net == _D('75.00')


# ---- 案例4：日期解析（实测格式全集，含 12 小时制与时区） ----
def test_parse_dates():
    cases = {
        # 德语点分 + UTC/MEZ
        '01.01.2026 14:03:54 UTC': (2026, 1, 1, 14, 3, 54),
        '01.01.2026 19:06:54 MEZ': (2026, 1, 1, 19, 6, 54),
        # 英文日在前
        '31 May 2026 23:04:30': (2026, 5, 31, 23, 4, 30),
        '1 Jan 2026 12:48:25 UTC': (2026, 1, 1, 12, 48, 25),
        # ★ 12 小时制（PM 必须 +12；这是会被时区剥离误伤的经典 bug）
        '3 Jan 2026 3:08:30 PM UTC': (2026, 1, 3, 15, 8, 30),
        'Mar 1, 2026 1:52:01 AM PST': (2026, 3, 1, 1, 52, 1),
        'Jan 2, 2026 6:15:36 p.m. PST': (2026, 1, 2, 18, 15, 36),
        'Apr 1, 2026 6:48:25 PM UTC': (2026, 4, 1, 18, 48, 25),
        'Jan 2, 2026 12:01:11 p.m. PST': (2026, 1, 2, 12, 1, 11),
        # 西班牙语（abr=4月 / mar=3月）+ GMT-7
        '2 abr 2026 4:48:25 p.m. GMT-7': (2026, 4, 2, 16, 48, 25),
        '29 mar 2026 3:46:58 p.m. GMT-7': (2026, 3, 29, 15, 46, 58),
        # ISO 带时区
        '2026-06-15T00:02:00+00:00': (2026, 6, 15, 0, 2, 0),
        # 纯日期
        '2026-05-01 00:00:00': (2026, 5, 1, 0, 0, 0),
    }
    for text, expected in cases.items():
        d = parse_date(text)
        assert d is not None, f"解析失败: {text}"
        got = (d.year, d.month, d.day, d.hour, d.minute, d.second)
        assert got == expected, f"{text} → {got}，期望 {expected}"
    assert parse_date('') is None
    assert parse_date(None) is None
    assert parse_date('not a date') is None


# ---- 案例4b：交易类型多语言归一（不归一则外语站点利润算不出来） ----
def test_normalize_transaction_type():
    from app.services.importer.parser import normalize_transaction_type as nt
    assert nt('Order') == 'Order'
    assert nt('Bestellung') == 'Order'        # 德语
    assert nt('Erstattung') == 'Refund'       # 德语退款
    assert nt('Anpassung') == 'Adjustment'    # 德语调整
    assert nt('Pedido') == 'Order'            # 西班牙语
    assert nt('Remboursement') == 'Refund'    # 法语
    assert nt('Servicegebühr') == 'Service Fee'
    assert nt('Überweisung') == 'Transfer'    # 德语提款
    assert nt('未知类型') == '未知类型'          # 未识别原样返回


# ---- 案例4c：多语言表头归一 ----
def test_normalize_header():
    from app.services.importer.parser import normalize_header
    assert normalize_header(['Datum/Uhrzeit', 'Menge', 'Umsätze']) == ['date/time', 'quantity', 'product sales']
    assert normalize_header(['fecha y hora', 'cantidad']) == ['date/time', 'quantity']
    # 连字符/下划线折叠（Amazon 费用类报表实际写法）
    assert normalize_header(['snapshot-date', 'amount-charged', 'month_of_charge']) == \
        ['snapshot date', 'amount charged', 'month of charge']


# ---- 案例5：数值解析（欧洲格式/Unicode减号/括号负数） ----
def test_parse_decimals():
    assert parse_decimal('1.234,56') == _D('1234.56')
    assert parse_decimal('1,234.56') == _D('1234.56')
    assert parse_decimal('−5.5') == _D('-5.5')  # Unicode 减号
    assert parse_decimal('(8.33)') == _D('-8.33')  # 括号负数
    assert parse_decimal('') is None
    assert parse_decimal('abc') is None


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    test_order_line_basic()
    test_full_deductions()
    test_quantize_2dp()
    test_parse_dates()
    test_normalize_transaction_type()
    test_normalize_header()
    test_parse_decimals()
    print("ALL GOLDEN TESTS PASSED")
