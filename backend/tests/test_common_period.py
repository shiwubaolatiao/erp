"""S15 公共口径层 — 期间解析单元测试（纯函数，无 DB 依赖）

覆盖 tasks.md 1.1 四类输入 + 边界（12 月跨年 / 非法格式）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.common.period import (
    resolve_period, month_bounds, PeriodSpec, PeriodFormatError,
)


class TestResolvePeriod:
    def test_period_month_direct(self):
        """① period_month 直传 → month 模式半开区间"""
        spec = resolve_period(period_month='2026-06')
        assert isinstance(spec, PeriodSpec)
        assert spec.mode == 'month'
        assert spec.period_month == '2026-06'
        assert spec.start == datetime(2026, 6, 1)
        assert spec.end == datetime(2026, 7, 1)

    def test_year_month_combo(self):
        """② year+month 组合 → 等效 period_month"""
        spec = resolve_period(year=2026, month=1)
        assert spec.mode == 'month'
        assert spec.period_month == '2026-01'
        assert spec.start == datetime(2026, 1, 1)
        assert spec.end == datetime(2026, 2, 1)

    def test_year_only(self):
        """②b year 省略 month → 整年区间，period_month=None"""
        spec = resolve_period(year=2025)
        assert spec.mode == 'year'
        assert spec.period_month is None
        assert spec.start == datetime(2025, 1, 1)
        assert spec.end == datetime(2026, 1, 1)

    def test_date_range(self):
        """③ date_from+date_to → range 模式"""
        spec = resolve_period(date_from='2026-01-15', date_to='2026-02-10')
        assert spec.mode == 'range'
        assert spec.period_month is None
        assert spec.start == datetime(2026, 1, 15)
        assert spec.end == datetime(2026, 2, 10)

    def test_default_current_month(self):
        """④ 全部为空 → 当前自然月"""
        spec = resolve_period()
        now = datetime.now()
        assert spec.mode == 'month'
        assert spec.period_month == now.strftime('%Y-%m')
        assert spec.start == datetime(now.year, now.month, 1)

    def test_priority_period_month_wins(self):
        """优先级：period_month > year+month"""
        spec = resolve_period(period_month='2026-06', year=2025, month=3)
        assert spec.period_month == '2026-06'

    def test_december_boundary(self):
        """12 月 → 次年 1 月（跨年边界）"""
        spec = resolve_period(period_month='2026-12')
        assert spec.start == datetime(2026, 12, 1)
        assert spec.end == datetime(2027, 1, 1)
        s2 = resolve_period(year=2026, month=12)
        assert s2.end == datetime(2027, 1, 1)

    def test_invalid_formats(self):
        """非法格式 → PeriodFormatError（API 层转 400）；空串视为未传（与存量语义一致）"""
        for bad in ('2026-13', '2026-00', 'bad', '202606', '2026-6'):
            with pytest.raises(PeriodFormatError):
                resolve_period(period_month=bad)
        with pytest.raises(PeriodFormatError):
            resolve_period(date_from='not-a-date', date_to='2026-02-01')

    def test_month_bounds_consistency(self):
        """month_bounds 与 resolve_period 输出一致"""
        for pm in ('2026-01', '2026-06', '2026-12'):
            s, e = month_bounds(pm)
            spec = resolve_period(period_month=pm)
            assert (s, e) == (spec.start, spec.end)