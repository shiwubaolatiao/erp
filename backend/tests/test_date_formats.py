"""日期解析回归测试 — 覆盖全部实测格式（各语言 × 各时区 × 12/24小时制）

这个文件是"数据可信"的守门员：任何解析器改动都必须先过这里。
用法: python tests/test_date_formats.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from app.services.importer.parser import (
    parse_date, normalize_transaction_type, normalize_header, normalize_period_month,
)

# (原始值, 期望 (Y,M,D,h,m,s))
CASES = [
    # ---- 英文 / 日在前（UK/IE/AE/SA）----
    ('31 May 2026 23:04:30', (2026, 5, 31, 23, 4, 30)),
    ('1 Jan 2026 12:48:25 UTC', (2026, 1, 1, 12, 48, 25)),
    ('3 Jan 2026 3:08:30 PM UTC', (2026, 1, 3, 15, 8, 30)),
    # ---- 美式 月在前（US/CA，带 AM/PM 与 a.m./p.m.）----
    ('Mar 1, 2026 1:52:01 AM PST', (2026, 3, 1, 1, 52, 1)),
    ('Jan 2, 2026 8:53:37 a.m. PST', (2026, 1, 2, 8, 53, 37)),
    ('Jan 2, 2026 6:15:36 p.m. PST', (2026, 1, 2, 18, 15, 36)),
    ('Jan 2, 2026 12:01:11 p.m. PST', (2026, 1, 2, 12, 1, 11)),
    ('Apr 1, 2026 6:48:25 PM UTC', (2026, 4, 1, 18, 48, 25)),
    ('Jan 12, 2026 1:59:57 a.m. PST', (2026, 1, 12, 1, 59, 57)),
    # ---- 德语点分 + MEZ ----
    ('01.01.2026 14:03:54 UTC', (2026, 1, 1, 14, 3, 54)),
    ('01.01.2026 19:06:54 MEZ', (2026, 1, 1, 19, 6, 54)),
    # ---- 法语（含 avr. 带句点 / juin）----
    ('9 avr. 2026 15:20:22 UTC', (2026, 4, 9, 15, 20, 22)),
    ('1 juin 2026 00:45:12 UTC', (2026, 6, 1, 0, 45, 12)),
    ('29 juin 2026 12:15:05 UTC', (2026, 6, 29, 12, 15, 5)),
    ('15 juil 2026 10:00:00', (2026, 7, 15, 10, 0, 0)),
    ('1 mai 2026 00:00:00', (2026, 5, 1, 0, 0, 0)),
    # ---- 西班牙语（墨西哥站，p.m. + GMT-7）----
    ('29 mar 2026 3:46:58 p.m. GMT-7', (2026, 3, 29, 15, 46, 58)),
    ('2 abr 2026 4:48:25 p.m. GMT-7', (2026, 4, 2, 16, 48, 25)),
    ('10 mar 2026 11:00:22 a.m. GMT-7', (2026, 3, 10, 11, 0, 22)),
    # ---- 意大利语 ----
    ('5 mag 2026 10:00:00 UTC', (2026, 5, 5, 10, 0, 0)),
    ('1 giu 2026 10:00:00 UTC', (2026, 6, 1, 10, 0, 0)),
    # ---- 荷兰语 / 瑞典语 ----
    ('3 mrt 2026 08:00:00', (2026, 3, 3, 8, 0, 0)),
    ('7 mei 2026 08:00:00', (2026, 5, 7, 8, 0, 0)),
    ('1 maj 2026 08:00:00', (2026, 5, 1, 8, 0, 0)),
    # ---- 波兰语 ----
    ('1 cze 2026 09:25:23 UTC', (2026, 6, 1, 9, 25, 23)),
    ('4 lip 2026 04:12:05 UTC', (2026, 7, 4, 4, 12, 5)),
    ('15 wrz 2026 12:00:00', (2026, 9, 15, 12, 0, 0)),
    ('1 paź 2026 08:00:00', (2026, 10, 1, 8, 0, 0)),
    # ---- ISO 带时区 ----
    ('2026-06-15T00:02:00+00:00', (2026, 6, 15, 0, 2, 0)),
    ('2026-01-21T17:19:59+05:30', (2026, 1, 21, 17, 19, 59)),
    # ---- 纯日期 ----
    ('2026-05-01 00:00:00', (2026, 5, 1, 0, 0, 0)),
    ('2026-05-01', (2026, 5, 1, 0, 0, 0)),
]

TYPE_CASES = [
    ('Order', 'Order'), ('Bestellung', 'Order'), ('Pedido', 'Order'), ('Commande', 'Order'),
    ('Refund', 'Refund'), ('Erstattung', 'Refund'), ('Remboursement', 'Refund'), ('Zwrot', 'Refund'),
    ('Adjustment', 'Adjustment'), ('Anpassung', 'Adjustment'), ('Ajuste', 'Adjustment'),
    ('Transfer', 'Transfer'), ('Überweisung', 'Transfer'),
    ('Service Fee', 'Service Fee'), ('Servicegebühr', 'Service Fee'),
]


def main():
    failed = 0
    for text, exp in CASES:
        d = parse_date(text)
        got = (d.year, d.month, d.day, d.hour, d.minute, d.second) if d else None
        if got != exp:
            failed += 1
            print(f"  FAIL 日期 {text!r} -> {got}，期望 {exp}")
    for text, exp in TYPE_CASES:
        got = normalize_transaction_type(text)
        if got != exp:
            failed += 1
            print(f"  FAIL 类型 {text!r} -> {got!r}，期望 {exp!r}")

    # 表头归一
    assert normalize_header(['Datum/Uhrzeit', 'Menge', 'Umsätze']) == ['date/time', 'quantity', 'product sales']
    assert normalize_header(['snapshot-date', 'amount-charged', 'month_of_charge']) == \
        ['snapshot date', 'amount charged', 'month of charge']

    # ★ 月份归一：month_of_charge 实际是带时间戳的日期，
    #   若不做归一会导致"按月过滤永远匹配不到"→ 仓储费/退货费算成 0
    month_cases = {
        '2026-06': '2026-06',
        '2026-06-01': '2026-06',
        '2026-06-01 00:00:00': '2026-06',
        '2026-06-30 23:59:59': '2026-06',
        '2026/06/01': '2026-06',
        '2026-01-15T00:00:00+00:00': '2026-01',
        'June 2026': '2026-06',
        'Jun 2026': '2026-06',
        '2026年6月': '2026-06',
        '202606': '2026-06',
        '': '',
        None: '',
    }
    for text, exp in month_cases.items():
        got = normalize_period_month(text)
        if got != exp:
            failed += 1
            print(f"  FAIL 月份归一 {text!r} -> {got!r}，期望 {exp!r}")

    # 空值
    assert parse_date('') is None and parse_date(None) is None and parse_date('not a date') is None

    total = len(CASES) + len(TYPE_CASES) + 12
    if failed == 0:
        print(f"日期/类型解析回归：全部通过（{total} 项）")
        return 0
    print(f"日期/类型解析回归：失败 {failed}/{total}")
    return 1


if __name__ == '__main__':
    sys.exit(main())
