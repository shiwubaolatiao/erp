"""common/costs.pick_period 月度回退单测（纯函数，无 DB）

★ 口径红线：query._pick_cost 与 fetcher.CostLookup.get 的回退语义由此测试锁定
  （当月 → 历史最近 → 缺失），任何行为变化都会影响利润数字。
"""
import pytest

from app.services.common.costs import pick_period


class TestPickPeriod:
    def test_exact_month_hit(self):
        per = {'2026-01': (10, 1), '2026-03': (12, 2)}
        assert pick_period(per, '2026-03') == '2026-03'

    def test_fallback_to_nearest_history(self):
        """当月缺失 → 取小于当月的最大 period（历史最近版本）"""
        per = {'2025-11': (10, 1), '2026-01': (11, 1), '2026-04': (99, 9)}
        assert pick_period(per, '2026-03') == '2026-01'

    def test_no_history_before_month(self):
        """仅有未来版本 → 缺失（不回退到未来）"""
        per = {'2026-06': (10, 1)}
        assert pick_period(per, '2026-05') is None

    def test_empty_versions(self):
        assert pick_period({}, '2026-01') is None

    def test_future_only_versions_not_picked(self):
        per = {'2026-07': (1, 1), '2026-12': (2, 2)}
        assert pick_period(per, '2026-06') is None


    def test_string_comparison_semantics(self):
        """'YYYY-MM' 字典序与时间序一致（回退 max 的前提）"""
        per = {'2026-01': (1, 1), '2026-02': (2, 1), '2026-03': (3, 1)}
        assert pick_period(per, '2026-03') == '2026-03'
        assert pick_period(per, '2026-04') == '2026-03'
        assert pick_period(per, '2025-12') is None