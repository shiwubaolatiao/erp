"""存量快照批量重算（tasks.md 5.1 / B-01 修复回填）

背景：B-01 修复（店铺级多站点逐国折算）上线前生成的店铺级快照（country=''）
按单一汇率折算，数字失真（UK 等站点被按 US 默认汇率折算，利润偏差可达 ~28%）。

识别规则：rate_source 是组 2（B-01 修复）新增列 —— 修复版核算必写
per_country / single / default_fallback；NULL 即修复前生成的旧版快照。

行为：按 (店铺, 月份) 去重后逐月调用修复版 calculate_snapshot 生成新 version，
旧版本永久保留可追溯；逐月独立事务（calculate_snapshot 内部单事务提交，
单月失败回滚该月并继续其余月份）。

调用方：tools/recalc_snapshots.py（CLI）与 /api/v2/admin/snapshots/recalculate（API）。
"""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Store, DwsProfitSnapshot
from app.services.accounting.service import calculate_snapshot

TOL = Decimal('0.01')


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


def latest_of(rows: list[DwsProfitSnapshot]) -> dict[tuple[str, str], DwsProfitSnapshot]:
    """同 (period_month, country) 多版本取 version 最大"""
    best: dict = {}
    for s in rows:
        k = (s.period_month or '', s.country or '')
        if k not in best or s.version > best[k].version:
            best[k] = s
    return best


def find_affected(db: Session, stores: list[str] | None = None,
                  months: list[str] | None = None) -> list[DwsProfitSnapshot]:
    """受 B-01 影响的旧版店铺级快照：country='' 且 rate_source IS NULL"""
    q = db.query(DwsProfitSnapshot).filter(
        DwsProfitSnapshot.country == '',
        DwsProfitSnapshot.rate_source.is_(None),
    )
    if stores:
        q = q.join(Store, Store.id == DwsProfitSnapshot.store_id).filter(
            Store.store_code.in_(stores))
    if months:
        q = q.filter(DwsProfitSnapshot.period_month.in_(months))
    return q.order_by(DwsProfitSnapshot.store_id, DwsProfitSnapshot.period_month,
                      DwsProfitSnapshot.version).all()


def recalc_affected(db: Session, stores: list[str] | None = None,
                    months: list[str] | None = None, dry_run: bool = False) -> dict:
    """批量重算受 B-01 影响的旧版店铺级快照，返回重算报告。

    逐月独立事务：calculate_snapshot 单事务提交；单月异常 rollback 后继续其余月份。
    """
    rows = find_affected(db, stores, months)
    targets: dict[tuple, DwsProfitSnapshot] = {}
    for s in rows:
        targets.setdefault((s.store_id, s.period_month), s)

    store_map = {s.id: s.store_code for s in db.query(Store).all()}
    items: list[dict] = []
    for (store_id, pm), old in sorted(targets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        code = store_map.get(store_id, str(store_id))
        if dry_run:
            items.append({'store': code, 'period_month': pm, 'old_version': old.version,
                          'old_net_profit_rmb': float(_d(old.net_profit_rmb)),
                          'status': 'dry_run'})
            continue
        try:
            new = calculate_snapshot(db, store_id, '', pm)
            items.append({
                'store': code, 'period_month': pm,
                'old_version': old.version, 'new_version': new.version,
                'old_net_profit_rmb': float(_d(old.net_profit_rmb)),
                'new_net_profit_rmb': float(_d(new.net_profit_rmb)),
                'rate_source': new.rate_source,
                'status': 'ok',
            })
        except Exception as e:
            db.rollback()
            items.append({'store': code, 'period_month': pm, 'status': 'failed',
                          'error': str(e)[:200]})

    success = sum(1 for i in items if i['status'] == 'ok')
    failed = sum(1 for i in items if i['status'] == 'failed')
    return {
        'affected': len(rows),
        'targets': len(targets),
        'dry_run': dry_run,
        'success': success,
        'failed': failed,
        'ok': failed == 0,
        'items': items,
    }


def verify_snapshots(db: Session, stores: list[str] | None = None) -> dict:
    """全量校验：各店各月最新店铺级快照 == Σ 同期各站点级快照（差异 ≤ 0.01 才算过）

    仅对"店铺级为新版口径（rate_source 非空）且有站点级快照"的组合校验；
    旧版快照（rate_source NULL，待重算）或无站点级快照的月份记 skip。
    """
    q = db.query(DwsProfitSnapshot)
    if stores:
        q = q.join(Store, Store.id == DwsProfitSnapshot.store_id).filter(
            Store.store_code.in_(stores))
    all_snaps = q.all()
    store_map = {s.id: s.store_code for s in db.query(Store).all()}

    grouped: dict = {}
    for s in all_snaps:
        grouped.setdefault((s.store_id, s.period_month), []).append(s)

    checked = passed = skipped = 0
    failures: list[dict] = []
    skips: list[dict] = []
    for (store_id, pm), rows in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1] or '')):
        best = latest_of(rows)
        # latest_of 的 key 是 (period_month, country) 元组：店铺级取 ('', ) 即 country=''
        store_snap = best.get((pm or '', ''))
        site_snaps = {c[1]: s for c, s in best.items() if c[1]}
        code = store_map.get(store_id, str(store_id))
        if store_snap is None or not site_snaps:
            skipped += 1
            continue
        if store_snap.rate_source is None:
            skipped += 1
            skips.append({'store': code, 'period_month': pm,
                          'detail': '店铺级为旧版快照（未重算，先执行重算）'})
            continue
        sum_profit = sum((_d(s.net_profit_rmb) for s in site_snaps.values()), Decimal(0))
        sum_sales = sum((_d(s.product_sales_rmb) for s in site_snaps.values()), Decimal(0))
        ok = (abs(sum_profit - _d(store_snap.net_profit_rmb)) <= TOL
              and abs(sum_sales - _d(store_snap.product_sales_rmb)) <= TOL)
        checked += 1
        if ok:
            passed += 1
        else:
            failures.append({
                'store': code, 'period_month': pm,
                'detail': f"店铺级 net_profit={store_snap.net_profit_rmb} != Σ站点级 {sum_profit}"
                          f"（sales {store_snap.product_sales_rmb} vs {sum_sales}）"
                          f"—— 站点级快照可能滞后，重算站点级后再验",
            })
    return {'checked': checked, 'passed': passed, 'skipped': skipped,
            'ok': checked > 0 and passed == checked,
            'failures': failures, 'skips': skips}