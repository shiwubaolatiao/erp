"""S15 公共口径层 — 汇率/费用/广告归月契约测试（tasks.md 1.2 / 1.3 / 1.5 / 1.6）

覆盖：
  · RateResolver 四级回退逐层命中 + 与 engine.get_exchange_rate 逐国一致
  · upsert_rate_with_audit 人工值保护（B-08）+ history 留痕
  · fees month_col 过滤（带/不带时间过滤差异、月份等值命中）
  · admonth SQL 归月与存量 Python 实现三类行逐行一致
依赖 conftest 的 e2e fixtures（LMG/MGT 合成数据）。
"""
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import or_

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models import (
    Store, ExchangeRate, ExchangeRateHistory, ImportBatch,
    FactFeeStorage, FactFeeAgedStorage, FactFeeReturn, FactAdDaily,
    FactFeeRemoval,
)
from app.services.profit.engine import get_exchange_rate, DEFAULT_RATES
from app.services.common.rates import RateResolver, upsert_rate_with_audit
from app.services.common import fees as common_fees


def _lmg(db) -> Store:
    st = db.query(Store).filter(Store.store_code == 'LMG').first()
    assert st, 'LMG 店铺未初始化（依赖 e2e fixture）'
    return st


def _cleanup_rates(db, store_id, country):
    """清理测试汇率行 — 含全局行（store_id=None），否则全局残留会污染后续回退测试"""
    db.query(ExchangeRateHistory).filter(
        ExchangeRateHistory.country_code == country,
        or_(ExchangeRateHistory.store_id == store_id,
            ExchangeRateHistory.store_id.is_(None)),
    ).delete()
    db.query(ExchangeRate).filter(
        ExchangeRate.country_code == country,
        or_(ExchangeRate.store_id == store_id,
            ExchangeRate.store_id.is_(None)),
    ).delete()
    db.commit()


class TestRateResolver:
    """四级回退（与 engine.get_exchange_rate 语义逐层一致）"""

    def _check_consistency(self, db, store_id, countries, pm):
        resolver = RateResolver(db, store_id=store_id, period_month=pm)
        batch = resolver.resolve(countries)
        for c in countries:
            single = get_exchange_rate(db, c, store_id=store_id, period_month=pm,
                                       ExchangeRateModel=ExchangeRate)
            assert batch[c] == single, f"{c}: 批量 {batch[c]} != 逐国 {single}"

    def test_fallback_chain_hits_each_level(self, e2e):
        db = SessionLocal()
        try:
            st = _lmg(db)
            sid, pm = st.id, '2026-01'
            _cleanup_rates(db, sid, 'FR')
            # ④ 默认表兜底（FR 无任何记录）
            assert RateResolver(db, sid, pm).resolve(['FR'])['FR'] == Decimal(DEFAULT_RATES['FR'])
            # ② 全局精确月（e2e 汇率表导入：LMG 报表汇率是店铺级还是全局级取决于管线，
            #    这里显式构造全局记录验证层级）
            db.add(ExchangeRate(store_id=None, country_code='FR', period_month=pm,
                                rate_to_rmb=7.77, source='manual'))
            db.commit()
            assert RateResolver(db, sid, pm).resolve(['FR'])['FR'] == Decimal('7.77')
            # ① 店铺专属精确月 优先于全局
            db.add(ExchangeRate(store_id=sid, country_code='FR', period_month=pm,
                                rate_to_rmb=7.88, source='manual'))
            db.commit()
            assert RateResolver(db, sid, pm).resolve(['FR'])['FR'] == Decimal('7.88')
            # ③ 店铺专属最近月（精确月不命中时回退到历史最近）
            assert RateResolver(db, sid, '2026-03').resolve(['FR'])['FR'] == Decimal('7.88')
            # 一致性：批量 == 逐国（覆盖四级混合场景）
            self._check_consistency(db, sid, ['FR', 'UK', 'DE', 'US'], pm)
            self._check_consistency(db, sid, ['FR', 'UK'], '2026-06')
        finally:
            st = _lmg(db)
            _cleanup_rates(db, st.id, 'FR')
            db.close()

    def test_batch_equals_engine_on_real_data(self, e2e):
        """对 e2e 真实合成数据：批量结果与 engine 逐国完全一致"""
        db = SessionLocal()
        try:
            st = _lmg(db)
            self._check_consistency(db, st.id, ['UK', 'DE', 'US', 'XX'], '2026-01')
        finally:
            db.close()

    def test_resolver_is_request_scoped(self):
        """RateResolver 无模块级全局状态（P-07 防线）：每次实例化独立查询"""
        r1 = RateResolver(None, store_id=1, period_month='2026-01')
        r2 = RateResolver(None, store_id=2, period_month='2026-02')
        assert r1._store_id != r2._store_id
        assert r1._period_month != r2._period_month
        assert r1._rows is None and r2._rows is None


class TestUpsertRateWithAudit:
    """人工值保护（B-08）+ 历史留痕"""

    def test_report_cannot_overwrite_manual(self, e2e):
        db = SessionLocal()
        try:
            st = _lmg(db)
            pm = '2026-09'
            _cleanup_rates(db, st.id, 'IT')
            # 人工维护 9.5
            r1 = upsert_rate_with_audit(db, st.id, 'IT', pm, '9.5', 'manual',
                                        operator_user_id=1)
            db.commit()
            assert r1.applied and not r1.conflict
            # 报表导入 8.0 → 拒绝覆盖
            r2 = upsert_rate_with_audit(db, st.id, 'IT', pm, '8.0', 'report',
                                        operator_batch_id=999)
            db.commit()
            assert not r2.applied and r2.conflict
            assert r2.new_rate == Decimal('9.5')   # 生效值仍是人工值
            er = db.query(ExchangeRate).filter(ExchangeRate.store_id == st.id,
                                               ExchangeRate.country_code == 'IT',
                                               ExchangeRate.period_month == pm).first()
            assert Decimal(str(er.rate_to_rmb)) == Decimal('9.5')
            # history 留痕：reject_overwrite 事件 + 被拒值
            h = db.query(ExchangeRateHistory).filter(
                ExchangeRateHistory.store_id == st.id,
                ExchangeRateHistory.country_code == 'IT',
                ExchangeRateHistory.period_month == pm,
                ExchangeRateHistory.action == 'reject_overwrite').first()
            assert h is not None
            assert Decimal(str(h.new_rate)) == Decimal('8.0')
            assert h.operator_batch_id == 999
        finally:
            st = _lmg(db)
            _cleanup_rates(db, st.id, 'IT')
            db.close()

    def test_normal_update_writes_history(self, e2e):
        db = SessionLocal()
        try:
            st = _lmg(db)
            pm = '2026-08'
            _cleanup_rates(db, st.id, 'ES')
            upsert_rate_with_audit(db, st.id, 'ES', pm, '7.1', 'report', operator_batch_id=1)
            db.commit()
            r = upsert_rate_with_audit(db, st.id, 'ES', pm, '7.2', 'manual', operator_user_id=2)
            db.commit()
            assert r.applied and not r.conflict and r.old_rate == Decimal('7.1')
            h = db.query(ExchangeRateHistory).filter(
                ExchangeRateHistory.store_id == st.id,
                ExchangeRateHistory.country_code == 'ES',
                ExchangeRateHistory.period_month == pm).all()
            assert len(h) == 2   # 首次插入 + 更新
            assert h[0].action == 'update' and h[0].old_rate is None
            assert h[1].action == 'update'
            assert Decimal(str(h[1].old_rate)) == Decimal('7.1')
            assert Decimal(str(h[1].new_rate)) == Decimal('7.2')
        finally:
            st = _lmg(db)
            _cleanup_rates(db, st.id, 'ES')
            db.close()


class TestCommonFees:
    """费用归集：month_col 过滤 + 月份等值命中 + by_key/by_country"""

    def test_month_col_validation(self):
        with pytest.raises(ValueError):
            common_fees.fee_by_country(None, FactFeeStorage, 'estimated_monthly_storage_fee',
                                       'wrong_col', '2026-01')
        # 超龄表配 month_of_charge → 模型无该列，报错
        with pytest.raises(ValueError):
            common_fees.fee_by_country(None, FactFeeAgedStorage, 'amount_charged',
                                       'month_of_charge', '2026-01')

    def test_dirty_timestamp_prefix_match(self, e2e):
        """月份值（组10.1 清洗后 YYYY-MM）等值命中（组10.3 口径）"""
        db = SessionLocal()
        try:
            st = _lmg(db)
            db.query(FactFeeStorage).filter(FactFeeStorage.store_id == st.id).delete()
            db.add(FactFeeStorage(store_id=st.id, asin='B0TEST0001', country='UK',
                                  month_of_charge='2026-01',
                                  estimated_monthly_storage_fee=12.34, batch_id=1,
                                  raw_row_id=1))
            db.commit()
            got = common_fees.fee_by_country(db, FactFeeStorage,
                                             'estimated_monthly_storage_fee',
                                             'month_of_charge', '2026-01', stores=st.id)
            assert got.get('UK') == Decimal('12.34')
            # 其他月份不得命中
            got2 = common_fees.fee_by_country(db, FactFeeStorage,
                                              'estimated_monthly_storage_fee',
                                              'month_of_charge', '2026-02', stores=st.id)
            assert got2 == {}
        finally:
            db.query(FactFeeStorage).filter(FactFeeStorage.store_id == st.id).delete()
            db.commit()
            db.close()

    def test_by_key_and_by_country_consistency(self, e2e):
        db = SessionLocal()
        try:
            st = _lmg(db)
            by_key = common_fees.fee_by_key(db, FactFeeStorage,
                                            'estimated_monthly_storage_fee',
                                            'month_of_charge', '2026-01', stores=st.id)
            by_c = common_fees.fee_by_country(db, FactFeeStorage,
                                              'estimated_monthly_storage_fee',
                                              'month_of_charge', '2026-01', stores=st.id)
            # by_key 按 country 分桶求和 == by_country
            for (asin, c), v in by_key.items():
                assert c in by_c
            for c, v in by_c.items():
                assert v == sum(x for (a, cc), x in by_key.items() if cc == c)
        finally:
            db.close()

    def test_aged_storage_uses_snapshot_date(self, e2e):
        """超龄表 month_col 必须是 snapshot_date（B-02 防线）"""
        db = SessionLocal()
        try:
            st = _lmg(db)
            db.query(FactFeeAgedStorage).filter(FactFeeAgedStorage.store_id == st.id).delete()
            db.add(FactFeeAgedStorage(store_id=st.id, asin='B0TEST0002', country='DE',
                                      snapshot_date='2026-01',
                                      amount_charged=5.00, batch_id=1, raw_row_id=1))
            db.commit()
            got = common_fees.fee_by_country(db, FactFeeAgedStorage, 'amount_charged',
                                             'snapshot_date', '2026-01', stores=st.id)
            assert got.get('DE') == Decimal('5.00')
            got2 = common_fees.fee_by_country(db, FactFeeAgedStorage, 'amount_charged',
                                              'snapshot_date', '2026-06', stores=st.id)
            assert got2 == {}
        finally:
            db.query(FactFeeAgedStorage).filter(FactFeeAgedStorage.store_id == st.id).delete()
            db.commit()
            db.close()

    def test_time_filter_validation(self, e2e):
        """month_col/date_col 二选一强制：都未传 raise（B-02）、同传 raise、date 缺区间 raise"""
        db = SessionLocal()
        try:
            # ① 都未传 → raise（费用表无 biz_date，漏过滤=全历史累计）
            with pytest.raises(ValueError):
                common_fees.fee_by_key(db, FactFeeStorage, 'estimated_monthly_storage_fee')
            with pytest.raises(ValueError):
                common_fees.fee_by_country(db, FactFeeStorage, 'estimated_monthly_storage_fee')
            # ② 互斥同传 → raise
            with pytest.raises(ValueError):
                common_fees.fee_by_key(db, FactFeeStorage, 'estimated_monthly_storage_fee',
                                       'month_of_charge', '2026-01',
                                       date_col=FactFeeRemoval.request_date)
            # ③ date_col 模式缺 start/end → raise
            with pytest.raises(ValueError):
                common_fees.fee_by_key(db, FactFeeRemoval, 'removal_fee',
                                       date_col=FactFeeRemoval.request_date)
        finally:
            db.close()

    def test_date_col_range_mode(self, e2e):
        """移除费 date_col 半开区间 [start, end)：区内计入、边界/区外不计"""
        db = SessionLocal()
        try:
            st = _lmg(db)
            db.query(FactFeeRemoval).filter(FactFeeRemoval.store_id == st.id).delete()
            for asin, dt, fee in [
                ('B0TEST0010', datetime(2026, 1, 10), 4.00),    # 区内
                ('B0TEST0011', datetime(2026, 1, 31, 23, 59), 8.00),  # 区内（月末尾）
                ('B0TEST0012', datetime(2026, 2, 1), 16.00),     # 边界外（半开）
                ('B0TEST0013', datetime(2025, 12, 31), 32.00),   # 区前
            ]:
                db.add(FactFeeRemoval(store_id=st.id, asin=asin, country='UK',
                                      request_date=dt, removal_fee=fee,
                                      batch_id=1, raw_row_id=1))
            db.commit()
            got = common_fees.fee_by_key(
                db, FactFeeRemoval, 'removal_fee',
                date_col=FactFeeRemoval.request_date,
                start=datetime(2026, 1, 1), end=datetime(2026, 2, 1), stores=st.id)
            assert got.get(('B0TEST0010', 'UK')) == Decimal('4.00')
            assert got.get(('B0TEST0011', 'UK')) == Decimal('8.00')
            assert ('B0TEST0012', 'UK') not in got
            assert ('B0TEST0013', 'UK') not in got
        finally:
            db.query(FactFeeRemoval).filter(FactFeeRemoval.store_id == st.id).delete()
            db.commit()
            db.close()

    def test_month_col_period_none_means_all(self, e2e):
        """month_col + period=None → 全量归集（调用方明确选择，对齐广告侧全量口径）"""
        db = SessionLocal()
        try:
            st = _lmg(db)
            db.query(FactFeeStorage).filter(FactFeeStorage.store_id == st.id).delete()
            db.add(FactFeeStorage(store_id=st.id, asin='B0TEST0009', country='UK',
                                  month_of_charge='2025-12',
                                  estimated_monthly_storage_fee=1.11, batch_id=1,
                                  raw_row_id=1))
            db.add(FactFeeStorage(store_id=st.id, asin='B0TEST0009', country='UK',
                                  month_of_charge='2026-01',
                                  estimated_monthly_storage_fee=2.22, batch_id=1,
                                  raw_row_id=2))
            db.commit()
            got = common_fees.fee_by_key(db, FactFeeStorage, 'estimated_monthly_storage_fee',
                                         'month_of_charge', None, stores=st.id)
            assert got.get(('B0TEST0009', 'UK')) == Decimal('3.33')
        finally:
            db.query(FactFeeStorage).filter(FactFeeStorage.store_id == st.id).delete()
            db.commit()
            db.close()


class TestAdMonth:
    """广告归月 SQL 化与存量 Python 实现三类行一致"""

    def _python_ad_month(self, db, r) -> str | None:
        """存量 Python 三级归月（query.py:100-110 语义的独立复现）"""
        if r.biz_date:
            return r.biz_date.strftime('%Y-%m')
        if getattr(r, 'period_month', None):
            return r.period_month
        b = db.query(ImportBatch).get(r.batch_id)
        return (getattr(b, 'period_month', None) or
                (b.imported_at.strftime('%Y-%m') if b and b.imported_at else None))

    def test_sql_month_equals_python(self, e2e):
        """构造三类行（biz_date / period_month / 批次兜底），SQL 归月逐行一致"""
        from app.services.common.admonth import ad_spend_by_key
        db = SessionLocal()
        try:
            st = _lmg(db)
            db.query(FactAdDaily).filter(FactAdDaily.asin == 'B0ADTEST01').delete()
            db.commit()
            b = db.query(ImportBatch).first()
            assert b, '无导入批次（依赖 e2e fixture）'
            rows = [
                FactAdDaily(store_id=st.id, asin='B0ADTEST01', country='UK', spend=1.0,
                            biz_date=datetime(2026, 1, 10), period_month=None,
                            batch_id=b.id, raw_row_id=1),
                FactAdDaily(store_id=st.id, asin='B0ADTEST01', country='UK', spend=2.0,
                            biz_date=None, period_month='2026-01',
                            batch_id=b.id, raw_row_id=1),
                FactAdDaily(store_id=st.id, asin='B0ADTEST01', country='UK', spend=4.0,
                            biz_date=None, period_month=None,
                            batch_id=b.id, raw_row_id=1),  # 批次兜底
            ]
            db.add_all(rows)
            db.commit()
            inserted = db.query(FactAdDaily).filter(FactAdDaily.asin == 'B0ADTEST01').all()
            # 期望值：Python 独立复算（三类行各自归月）
            py_by_month: dict = {}
            for r in inserted:
                m = self._python_ad_month(db, r)
                py_by_month[m] = py_by_month.get(m, Decimal(0)) + Decimal(str(r.spend or 0))
            # SQL 逐月对照（覆盖三类行各自落入的月份 + 干扰月）
            months = {m for m in py_by_month if m} | {'2026-01', '2026-02'}
            for m in sorted(months):
                sql_val = ad_spend_by_key(db, stores=st.id, period_month=m).get(
                    ('B0ADTEST01', 'UK'), Decimal(0))
                assert sql_val == py_by_month.get(m, Decimal(0)), \
                    f"月份 {m}: SQL {sql_val} != Python {py_by_month.get(m, Decimal(0))}"
            # 整年口径同样一致（2026）
            sql_year = ad_spend_by_key(db, stores=st.id, year=2026).get(
                ('B0ADTEST01', 'UK'), Decimal(0))
            py_year = sum(v for m, v in py_by_month.items()
                          if m and m.startswith('2026-'))
            assert sql_year == py_year
        finally:
            db.query(FactAdDaily).filter(FactAdDaily.asin == 'B0ADTEST01').delete()
            db.commit()
            db.close()