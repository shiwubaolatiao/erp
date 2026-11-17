# -*- coding: utf-8 -*-
"""pending 映射批量确认引擎（mapping_review_enhance 组4）

以 07 基准为依据批量确认 pending（spec 5.2）：
  · 三态判定（纯函数，无 DB 写）：confirmable / uncovered / conflict
  · 漂移裁决：同 (store, sku) 多候选指向不同 ASIN → 全部转 conflict（DD6 不择优）
  · dry-run 零写库；正式执行分片 500 条提交，幂等续跑
  · 红线：SKU 不允许跨 ASIN 漂移（复用 decide_mapping 语义，执行前二次校验）

计数自洽：total_pending = confirmed + uncovered + conflict + failed + skipped_already_normal
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import MappingBaselineItem, Store, StoreProduct, StoreSkuMapping
from app.services.mapping import report as report_svc
from app.services.mapping.baseline import build_baseline_index

CONFIRM_CHUNK = 500


class BaselineNotImported(ValueError):
    """未导入基准即执行批量确认（spec 5.2.3 场景 1 → API 400）"""


def classify(pending_rows: list, baseline: dict, store_code: str) -> dict:
    """三态判定（纯函数，design 2.4.2 判定顺序即优先级）

    pending_rows: StoreSkuMapping（status='pending'）
    baseline: 该店铺索引 {exact, sku2asin, asin2sku, conflicts}
    返回 {confirmable, uncovered, conflict}（元素为携带判定信息的 dict）
    """
    confirmable, uncovered, conflict = [], [], []
    for m in pending_rows:
        base = {'mapping_id': m.id, 'store_code': store_code,
                'sku': m.sku, 'asin': m.asin}
        # 规则1：基准内冲突（spec 5.2.4c）
        if m.sku in baseline['conflicts']:
            conflict.append({**base, 'conflict_type': 'baseline_internal',
                             'baseline_asin': None, 'baseline_skus': []})
            continue
        # 规则2：完全匹配 → 可确认候选（spec 5.2.1）
        if (m.sku, m.asin) in baseline['exact']:
            confirmable.append(base)
            continue
        # 规则3：同 SKU 不同 ASIN（spec 5.2.4a）
        if m.sku in baseline['sku2asin'] and m.asin not in baseline['sku2asin'][m.sku]:
            conflict.append({**base, 'conflict_type': 'same_sku_diff_asin',
                             'baseline_asin': sorted(baseline['sku2asin'][m.sku]),
                             'baseline_skus': []})
            continue
        # 规则4：同 ASIN 不同 SKU（spec 5.2.4b）
        if m.asin in baseline['asin2sku'] and m.sku not in baseline['asin2sku'][m.asin]:
            conflict.append({**base, 'conflict_type': 'same_asin_diff_sku',
                             'baseline_asin': None,
                             'baseline_skus': sorted(baseline['asin2sku'][m.asin])})
            continue
        # 规则5：基准未覆盖（spec 5.2.3）
        uncovered.append({**base, 'reason': 'not in baseline'})
    return {'confirmable': confirmable, 'uncovered': uncovered, 'conflict': conflict}


def _drift_adjudicate(confirmable: list, conflict: list,
                      existing_normal: dict[str, str],
                      skipped: list) -> list:
    """漂移裁决（spec 5.2.5 红线）：

    · 同 SKU 多条候选指向不同 ASIN → 全部转 conflict(sku_drift)（不择优）
    · 库内已有同 SKU normal：同 ASIN → skipped_already_normal（幂等跳过）；
      异 ASIN → conflict(sku_drift)
    返回扣除漂移/跳过后的 confirmable。
    """
    by_sku: dict[str, list[dict]] = {}
    for c in confirmable:
        by_sku.setdefault(c['sku'], []).append(c)
    kept = []
    for sku, group in by_sku.items():
        asins = {g['asin'] for g in group}
        normal_asin = existing_normal.get(sku)
        if normal_asin is not None:
            for g in group:
                if g['asin'] == normal_asin:
                    skipped.append(g)
                else:
                    conflict.append({**g, 'conflict_type': 'sku_drift',
                                     'baseline_asin': None, 'baseline_skus': []})
            continue
        if len(asins) > 1:
            for g in group:
                conflict.append({**g, 'conflict_type': 'sku_drift',
                                 'baseline_asin': None, 'baseline_skus': []})
            continue
        kept.extend(group)
    return kept


def _confirm_one(db: Session, mapping_id: int, user_id: int | None) -> None:
    """单条转正（复用 decide_mapping 语义：SKU 不跨 ASIN 漂移 + 主档同步）"""
    m = db.query(StoreSkuMapping).get(mapping_id)
    if not m or m.status != 'pending':
        return
    conflict = db.query(StoreSkuMapping).filter(
        StoreSkuMapping.store_id == m.store_id,
        StoreSkuMapping.sku == m.sku,
        StoreSkuMapping.status == 'normal',
        StoreSkuMapping.id != m.id,
    ).first()
    if conflict:
        raise ValueError(f"SKU {m.sku} 已绑定 ASIN {conflict.asin}（漂移红线）")
    m.status = 'normal'
    m.confirmed_by = user_id
    m.confirmed_at = datetime.now()
    if m.store_product_id:
        p = db.query(StoreProduct).get(m.store_product_id)
        if p and p.status == 'pending':
            p.status = 'normal'


def bulk_confirm(db: Session, store: str | None = None, dry_run: bool = True,
                 operator_user_id: int | None = None,
                 operator_username: str = '') -> dict:
    """批量确认编排（API 与 CLI 同源）

    dry_run=True  → 只读判定，返回内存报告（零写库、不落报告）
    dry_run=False → 分片 500 条提交转正 + 报告落库 + 审计留痕
    """
    if db.query(MappingBaselineItem).count() == 0:
        raise BaselineNotImported('请先导入 07 基准（mapping_baseline_item 为空）')

    store_q = db.query(Store)
    if store:
        st = store_q.filter(Store.store_code == store.upper()).first()
        if not st:
            raise ValueError(f"店铺不存在: {store}")
        targets = [st]
    else:
        targets = store_q.all()
    stores = {s.id: s.store_code for s in targets}
    store_ids = list(stores.keys())

    index = build_baseline_index(db, store_ids)
    confirmed, uncovered, conflict, failed, skipped = [], [], [], [], []
    total_pending = 0

    for sid in store_ids:
        code = stores[sid]
        pending_rows = db.query(StoreSkuMapping).filter(
            StoreSkuMapping.store_id == sid,
            StoreSkuMapping.status == 'pending').all()   # abnormal/rejected 天然排除
        total_pending += len(pending_rows)
        if not pending_rows:
            continue
        baseline = index.get(sid) or {'exact': set(), 'sku2asin': {},
                                      'asin2sku': {}, 'conflicts': set()}
        result = classify(pending_rows, baseline, code)

        # 库内同 (store, sku) normal 映射（幂等跳过 / 漂移红线依据）
        existing_normal: dict[str, str] = {}
        skus = {m.sku for m in pending_rows}
        if skus:
            for row in db.query(StoreSkuMapping).filter(
                    StoreSkuMapping.store_id == sid,
                    StoreSkuMapping.sku.in_(skus),
                    StoreSkuMapping.status == 'normal').all():
                existing_normal[row.sku] = row.asin

        kept = _drift_adjudicate(result['confirmable'], result['conflict'],
                                 existing_normal, skipped)
        confirmed.extend(kept)
        uncovered.extend(result['uncovered'])
        conflict.extend(result['conflict'])

    report = {
        'op_type': 'bulk_confirm',
        'dry_run': dry_run,
        'store_scope': (store.upper() if store else 'ALL'),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'summary': {
            'total_pending': total_pending,
            'confirmed': len(confirmed), 'uncovered': len(uncovered),
            'conflict': len(conflict), 'failed': 0,
            'skipped_already_normal': len(skipped),
        },
        'details': {'confirmed': confirmed, 'uncovered': uncovered,
                    'conflict': conflict, 'failed': failed},
        'warnings': [],
        'report_id': None,
    }

    if dry_run:
        return report

    # 正式执行：分片提交；单条失败回滚该分片计 failed，已完成分片保持有效
    done = 0
    for i in range(0, len(confirmed), CONFIRM_CHUNK):
        chunk = confirmed[i:i + CONFIRM_CHUNK]
        try:
            for c in chunk:
                _confirm_one(db, c['mapping_id'], operator_user_id)
            db.commit()
            done += len(chunk)
        except Exception as exc:
            db.rollback()
            for c in chunk:
                failed.append({**c, 'error': str(exc)[:200]})
    report['summary']['confirmed'] = done
    report['summary']['failed'] = len(failed)
    report['details']['confirmed'] = confirmed[:done]

    report['report_id'] = report_svc.save_report(db, report, operator_user_id)
    report_svc.log_mapping_operation(
        db, 'mapping_bulk_confirm', report['store_scope'],
        f"确认 {done} / 未覆盖 {len(uncovered)} / 冲突 {len(conflict)} / "
        f"失败 {len(failed)} / 跳过 {len(skipped)}",
        operator_user_id, operator_username)
    db.commit()
    return report


def rollback_by_report(db: Session, report_id: int,
                       operator_user_id: int | None = None,
                       operator_username: str = '') -> int:
    """按执行报告回滚：confirmed[].mapping_id 置回 pending 并清空确认人/时间"""

    from app.models import MappingOpReport
    r = db.query(MappingOpReport).get(report_id)
    if not r or r.op_type != 'bulk_confirm' or r.dry_run:
        raise ValueError("报告不存在或不是正式批量确认报告")
    ids = [c['mapping_id'] for c in (r.payload or {}).get('confirmed', [])]
    n = 0
    for i in range(0, len(ids), CONFIRM_CHUNK):
        chunk = ids[i:i + CONFIRM_CHUNK]
        for m in db.query(StoreSkuMapping).filter(StoreSkuMapping.id.in_(chunk)).all():
            if m.status == 'normal' and m.confirmed_by is not None:
                m.status = 'pending'
                m.confirmed_by = None
                m.confirmed_at = None
                n += 1
        db.commit()
    report_svc.log_mapping_operation(
        db, 'mapping_bulk_confirm', f"rollback report={report_id}",
        f"回滚 {n} 条至 pending", operator_user_id, operator_username)
    db.commit()
    return n