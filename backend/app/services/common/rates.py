"""汇率解析与写入 — 唯一实现（消灭逐国×3 次往返的 P-04 一部分 + B-08 人工值保护）

回退语义与 app/services/profit/engine.py::get_exchange_rate 完全一致（四级）：
    店铺专属(精确月) → 全局(精确月) → 店铺专属(任意最近月) → DEFAULT_RATES 默认值

★ RateResolver 必须请求级实例化，禁止模块级全局缓存（P-07 教训：无失效、跨会话污染）。
★ 所有汇率写入（报表导入 / 管理员人工 / 核算维护）统一走 upsert_rate_with_audit，
  人工值（source='manual'）不被报表导入静默覆盖（B-08），且全部变更留痕 exchange_rate_history。
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import or_, and_
from sqlalchemy.orm import Session

from app.models import ExchangeRate, ExchangeRateHistory
from app.services.profit.engine import DEFAULT_RATES


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


def _valid_rate(er: Optional[ExchangeRate]) -> bool:
    """有效汇率行：rate_to_rmb 非 None 且非 0（与 engine.get_exchange_rate 判定一致）"""
    return er is not None and er.rate_to_rmb is not None and Decimal(str(er.rate_to_rmb)) != 0


class RateResolver:
    """请求级批量汇率解析器 — 每次调用批量取回相关行，内存完成四级回退

    用法（每个请求内创建一次）：
        resolver = RateResolver(db, store_id=store_id, period_month='2026-06')
        rates = resolver.resolve(['UK', 'DE', 'FR'])
    """

    def __init__(self, db: Session, store_id: Optional[int] = None,
                 period_month: Optional[str] = None):
        self._db = db
        self._store_id = store_id
        self._period_month = period_month
        self._rows: Optional[list] = None   # 惰性加载，仅查询一次

    def _load(self, countries: list[str]) -> None:
        """一次性取回候选国家在（该店铺 + 全局）的全部汇率行"""
        q = self._db.query(ExchangeRate).filter(ExchangeRate.country_code.in_(countries))
        if self._store_id is not None:
            q = q.filter(or_(ExchangeRate.store_id == self._store_id,
                             ExchangeRate.store_id.is_(None)))
        else:
            q = q.filter(ExchangeRate.store_id.is_(None))
        self._rows = q.all()

    def resolve(self, countries: Iterable[str]) -> dict[str, Decimal]:
        """批量解析 {国家: 汇率(RMB)} — 至多 1 次 DB 查询，回退结果与 engine 逐国一致"""
        resolved = self.resolve_with_source(countries)
        return {c: r for c, (r, _src) in resolved.items()}

    def resolve_with_source(self, countries: Iterable[str]) -> dict[str, tuple[Decimal, str]]:
        """批量解析 {国家: (汇率, 来源层级)}

        来源层级 ∈ store_exact（店铺精确月）/ global_exact（全局精确月）/
        store_recent（店铺最近月）/ default（DEFAULT_RATES 兜底）。
        """
        cs = sorted({c for c in countries if c})
        if not cs:
            return {}
        if self._rows is None:
            self._load(cs)
        out: dict[str, tuple[Decimal, str]] = {}
        for c in cs:
            out[c] = self._resolve_one_with_source(c)
        return out

    def _resolve_one(self, country: str) -> Decimal:
        return self._resolve_one_with_source(country)[0]

    def _resolve_one_with_source(self, country: str) -> tuple[Decimal, str]:
        rows = [r for r in self._rows if r.country_code == country]
        pm = self._period_month
        # ① 店铺专属（精确月）
        if pm and self._store_id is not None:
            for r in rows:
                if r.store_id == self._store_id and r.period_month == pm and _valid_rate(r):
                    return Decimal(str(r.rate_to_rmb)), 'store_exact'
        # ② 全局（无店铺）精确月
        if pm:
            for r in rows:
                if r.store_id is None and r.period_month == pm and _valid_rate(r):
                    return Decimal(str(r.rate_to_rmb)), 'global_exact'
        # ③ 店铺专属（任意月份，取最近）
        if self._store_id is not None:
            store_rows = [r for r in rows
                          if r.store_id == self._store_id and _valid_rate(r)]
            if store_rows:
                best = max(store_rows, key=lambda r: r.period_month or '')
                return Decimal(str(best.rate_to_rmb)), 'store_recent'
        # ④ 默认汇率表
        return Decimal(DEFAULT_RATES.get(country, '6.8')), 'default'


def resolve_for_stores(db: Session, store_ids: Iterable[int], countries: Iterable[str],
                       period_month: Optional[str] = None) -> dict[int, dict[str, Decimal]]:
    """多店铺批量汇率解析 — {store_id: {country: 汇率}}

    店铺对比场景（P-04）：一次查询取回全部候选店铺 + 全局的汇率行，
    按店铺分组复用 RateResolver 的四级回退（与逐店单解析结果逐字一致），
    消灭"每店铺 × 逐国 × 3 次往返"。
    """
    cs = sorted({c for c in countries if c})
    ids = list(store_ids or [])
    if not cs:
        return {sid: {} for sid in ids}
    q = db.query(ExchangeRate).filter(ExchangeRate.country_code.in_(cs))
    if ids:
        q = q.filter(or_(ExchangeRate.store_id.in_(ids), ExchangeRate.store_id.is_(None)))
    else:
        q = q.filter(ExchangeRate.store_id.is_(None))
    rows = q.all()
    out: dict[int, dict[str, Decimal]] = {}
    for sid in ids:
        resolver = RateResolver(db, store_id=sid, period_month=period_month)
        # 注入预取行（store_id == sid 或全局），跳过 resolver 自身查询
        resolver._rows = [r for r in rows if r.store_id == sid or r.store_id is None]
        out[sid] = resolver.resolve(cs)
    return out


@dataclass
class UpsertRateResult:
    applied: bool                    # 是否实际写入/更新
    conflict: bool                   # 是否因人工值保护被拒绝
    old_rate: Optional[Decimal]      # 变更前值（首次写入为 None）
    new_rate: Optional[Decimal]      # 实际生效值（被拒绝时为保留的人工值）


def upsert_rate_with_audit(db: Session, store_id: Optional[int], country: str,
                           period_month: str, new_rate, source: str,
                           operator_user_id: Optional[int] = None,
                           operator_batch_id: Optional[int] = None,
                           ) -> UpsertRateResult:
    """汇率统一写入入口 — 人工值保护 + 历史留痕（不 commit，事务由调用方控制）

    规则：
      · existing.source == 'manual' 且新值来源非人工（source != 'manual'）且值不同
        → 拒绝覆盖（conflict=True），history 记录 action='reject_overwrite'（new_rate 记录被拒值）
        （人工值保护 B-08：只防报表自动覆盖；管理员人工改人工正常生效）
      · 其余情况正常写入（新增或更新），history 记录 action='update'（old→new）
    """
    new_rate_d = Decimal(str(new_rate)) if new_rate is not None else None
    existing = db.query(ExchangeRate).filter(
        ExchangeRate.store_id == store_id if store_id is not None
        else ExchangeRate.store_id.is_(None),
        ExchangeRate.country_code == country,
        ExchangeRate.period_month == period_month,
    ).first()

    if existing is not None:
        old_d = Decimal(str(existing.rate_to_rmb)) if existing.rate_to_rmb is not None else None
        changed = new_rate_d is not None and (old_d is None or new_rate_d != old_d)
        if not changed:
            # 值未变化：保持原状（source 不漂移），不留历史
            return UpsertRateResult(applied=True, conflict=False,
                                    old_rate=old_d, new_rate=old_d)
        if existing.source == 'manual' and source != 'manual':
            # 人工值保护（B-08）：拒绝报表覆盖 + 留痕（new_rate 记录被拒值）
            db.add(ExchangeRateHistory(
                store_id=store_id, country_code=country, period_month=period_month,
                old_rate=old_d, new_rate=new_rate_d, source=source,
                action='reject_overwrite',
                operator_user_id=operator_user_id, operator_batch_id=operator_batch_id,
            ))
            return UpsertRateResult(applied=False, conflict=True,
                                    old_rate=old_d, new_rate=old_d)
        # 正常更新 + old→new 历史
        existing.rate_to_rmb = new_rate_d
        existing.source = source
        existing.updated_by = operator_user_id
        db.add(ExchangeRateHistory(
            store_id=store_id, country_code=country, period_month=period_month,
            old_rate=old_d, new_rate=new_rate_d, source=source, action='update',
            operator_user_id=operator_user_id, operator_batch_id=operator_batch_id,
        ))
        return UpsertRateResult(applied=True, conflict=False,
                                old_rate=old_d, new_rate=new_rate_d)

    # 首次写入
    db.add(ExchangeRate(
        store_id=store_id, country_code=country, period_month=period_month,
        rate_to_rmb=new_rate_d, source=source, updated_by=operator_user_id,
    ))
    db.add(ExchangeRateHistory(
        store_id=store_id, country_code=country, period_month=period_month,
        old_rate=None, new_rate=new_rate_d, source=source, action='update',
        operator_user_id=operator_user_id, operator_batch_id=operator_batch_id,
    ))
    return UpsertRateResult(applied=True, conflict=False, old_rate=None, new_rate=new_rate_d)