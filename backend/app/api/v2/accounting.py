"""核算 — 自然月/区间核算、快照版本、成本缺失预警与补录

接口前缀: /api/v2/accounting
利润公式来自 app/services/profit/engine.py（旧系统原样移植，逐字未改）

核心口径（改动前必读 README 第六节）：
    net_amazon = Σ(type ∈ {Order, Refund}) 的 12 项 + Adjustment 分桶
    Adjustment 分桶：order_id 非空 → other 列；无 order_id → total 按正负
    净利润 = net_amazon × 汇率 − 采购成本 − 头程运费 − (广告+仓储+超龄+退货+入库+移除) × 汇率
    不计入利润：Service Fee / Amazon Fees / FBA Inventory Fee / FBA transaction fees / Transfer

快照与版本：
    · 每次核算生成**新版本**（version 自增），历史版本永久保留，可 compare 对比
    · 核算按原始业务日期过滤；费用类按 month_of_charge 整月归属
    · 成本缺失不静默按 0：快照标 cost_complete=False 并在 cost_missing_alert 排队
"""
from datetime import datetime
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import require_module, get_current_user, filter_store_by_permission
from app.models import (
    Store, DwsProfitSnapshot, DwsProfitSnapshotItem, CostMissingAlert,
    ProductCostHistory, OperationLog, SysUser,
)
from app.services.accounting.service import calculate_snapshot
from app.workers.task_worker import submit_task, get_task, list_tasks
from app.workers.accounting_worker import TASK_TYPE  # noqa: F401 注册核算 handler

router = APIRouter()

# 同步模式（async=false）等待任务完成的最长秒数（旧调用方兼容）
SYNC_WAIT_SECONDS = 600


class CalculateRequest(BaseModel):
    """核算请求：period_month（自然月，默认口径）与 date_from/date_to（高级区间）二选一"""
    model_config = ConfigDict(populate_by_name=True)

    store: str
    country: str = ""                    # 空 = 该店铺全部站点汇总
    period_month: str | None = None      # YYYY-MM（默认口径）
    date_from: str | None = None         # ISO 日期（高级：自定义区间）
    date_to: str | None = None
    snapshot_name: str = ""              # 命名快照，如 "UK-2026年6月结算-终版"
    async_mode: bool = Field(default=True, alias="async",
                             description="true=后台任务（默认，1 秒内返回句柄）；false=同步等待（Deprecated）")


class CostEntryRequest(BaseModel):
    """成本补录请求：成本与运费至少填一项，写入 product_cost_history(来源=manual)"""
    store: str
    asin: str
    sku: str
    period: str
    cost_rmb: float | None = None
    freight: float | None = None


@router.post("/calculate")
def calculate(req: CalculateRequest, db: Session = Depends(get_db),
              user: SysUser = Depends(require_module("accounting"))):
    """执行核算 → 生成新版本快照（不覆盖历史版本）

    默认 async=true：1 秒内返回任务句柄（后台核算期间其他接口正常服务）；
    async=false（Deprecated，保留一个版本）：内部仍走后台任务但同步等待完成，
    响应结构与旧同步版一致，避免旧调用方超时断裂。
    """
    store = filter_store_by_permission(user, req.store, db)
    if not store:
        raise HTTPException(status_code=404, detail=f"店铺 {req.store} 不存在")
    if not req.period_month and not (req.date_from and req.date_to):
        raise HTTPException(status_code=400, detail="必须指定 period_month 或 date_from+date_to")

    payload = {
        'store_id': store.id, 'store_code': store.store_code,
        'country': req.country or '', 'period_month': req.period_month or '',
        'date_from': req.date_from, 'date_to': req.date_to,
        'snapshot_name': req.snapshot_name, 'created_by': user.id,
        'username': user.username,
    }
    task = submit_task(TASK_TYPE, payload, created_by=user.id)

    if not req.async_mode:
        # 同步语义：等待后台任务终态（核算失败抛 500，无半成品快照）
        deadline = time.time() + SYNC_WAIT_SECONDS
        t = None
        while time.time() < deadline:
            t = get_task(task['task_id'])
            if t and t['status'] in ('success', 'failed', 'interrupted'):
                break
            time.sleep(0.2)
        if not t or t['status'] != 'success':
            reason = (t or {}).get('error') or f"等待超时（{SYNC_WAIT_SECONDS}s）"
            raise HTTPException(status_code=500, detail=f"核算任务失败: {str(reason)[:300]}")
        return {**t['result'], 'task_id': t['task_id'], 'status': 'done'}

    return {
        'task_id': task['task_id'],
        'status': task['status'],        # queued / running
        'poll_url': f"/api/v2/accounting/tasks/{task['task_id']}",
    }


@router.get("/tasks/{task_id}")
def get_accounting_task(task_id: str, db: Session = Depends(get_db),
                        user=Depends(require_module("accounting"))):
    """核算任务状态（前端轮询）：status/progress/stage/snapshot_id/error"""
    t = get_task(task_id)
    if not t or t['task_type'] != TASK_TYPE:
        raise HTTPException(status_code=404, detail="任务不存在")
    result = t.get('result') or {}
    return {
        'task_id': t['task_id'], 'status': t['status'],
        'progress': t['progress'], 'stage': t['stage'],
        'snapshot_id': int(t['result_ref']) if t.get('result_ref') else None,
        'snapshot': result if t['status'] == 'success' else None,
        'error': t.get('error'),
        'period_month': (t.get('payload') or {}).get('period_month'),
        'country': (t.get('payload') or {}).get('country'),
        'started_at': t.get('started_at'), 'finished_at': t.get('finished_at'),
    }


@router.get("/tasks")
def list_accounting_tasks(limit: int = 30, db: Session = Depends(get_db),
                          user=Depends(require_module("accounting"))):
    """核算任务列表（跨重启可见中断态与历史，可对未完成任务重试）"""
    return {'items': [dict(t, snapshot_id=int(t['result_ref']) if t.get('result_ref') else None)
                      for t in list_tasks(TASK_TYPE, limit)]}


@router.get("/snapshots")
def list_snapshots(store: str | None = None, period_month: str | None = None,
                   db: Session = Depends(get_db), user=Depends(require_module("accounting"))):
    """快照列表（含历史版本）"""
    store_obj = filter_store_by_permission(user, store, db) if store else None
    q = db.query(DwsProfitSnapshot)
    if store_obj:
        q = q.filter(DwsProfitSnapshot.store_id == store_obj.id)
    if period_month:
        q = q.filter(DwsProfitSnapshot.period_month == period_month)
    snaps = q.order_by(DwsProfitSnapshot.created_at.desc()).limit(200).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    return {'items': [{
        'id': s.id, 'store_code': stores.get(s.store_id, ''),
        'country': s.country, 'period_month': s.period_month,
        'date_from': s.date_from.isoformat() if s.date_from else None,
        'date_to': s.date_to.isoformat() if s.date_to else None,
        'version': s.version, 'snapshot_name': s.snapshot_name,
        'net_profit_rmb': float(s.net_profit_rmb or 0),
        'net_profit_rate': float(s.net_profit_rate or 0),
        'product_sales_rmb': float(s.product_sales_rmb or 0),
        'cost_complete': s.cost_complete,
        'cost_incomplete_skus': s.cost_incomplete_skus,
        'is_frozen': s.is_frozen,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    } for s in snaps]}


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int, db: Session = Depends(get_db),
                 user=Depends(require_module("accounting"))):
    """快照详情 + 明细（含逐 SKU 溯源入口）"""
    snap = db.query(DwsProfitSnapshot).get(snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="快照不存在")
    items = db.query(DwsProfitSnapshotItem).filter(
        DwsProfitSnapshotItem.snapshot_id == snapshot_id).all()
    store = db.query(Store).get(snap.store_id)
    return {
        'snapshot': {
            'id': snap.id, 'store_code': store.store_code if store else '',
            'country': snap.country, 'period_month': snap.period_month,
            'date_from': snap.date_from.isoformat() if snap.date_from else None,
            'date_to': snap.date_to.isoformat() if snap.date_to else None,
            'version': snap.version, 'snapshot_name': snap.snapshot_name,
            'is_frozen': snap.is_frozen,
            'net_amazon': float(snap.net_amazon or 0),
            'exchange_rate': float(snap.exchange_rate or 0),
            'rate_source': snap.rate_source,
            'rate_detail': snap.rate_detail,
            'rate_missing_countries': (snap.rate_detail or {}).get('_missing', []),
            'product_cost_rmb': float(snap.product_cost_rmb or 0),
            'freight_cost_rmb': float(snap.freight_cost_rmb or 0),
            'ad_spend': float(snap.ad_spend or 0),
            'storage_fee': float(snap.storage_fee or 0),
            'aged_storage_fee': float(snap.aged_storage_fee or 0),
            'returns_fee': float(snap.returns_fee or 0),
            'inbound_fee': float(snap.inbound_fee or 0),
            'removal_fee': float(snap.removal_fee or 0),
            'product_sales_rmb': float(snap.product_sales_rmb or 0),
            'net_profit_rmb': float(snap.net_profit_rmb or 0),
            'net_profit_rate': float(snap.net_profit_rate or 0),
            'order_count': snap.order_count, 'order_qty': snap.order_qty,
            'cost_complete': snap.cost_complete,
            'cost_incomplete_skus': snap.cost_incomplete_skus,
        },
        'items': [{
            'id': it.id, 'asin': it.asin, 'sku': it.sku,
            'country': it.country or '',
            'exchange_rate': float(it.exchange_rate) if it.exchange_rate is not None else None,
            'net_amazon': float(it.net_amazon or 0), 'quantity': it.quantity,
            'product_cost_rmb': float(it.product_cost_rmb or 0),
            'freight_cost_rmb': float(it.freight_cost_rmb or 0),
            'ad_spend': float(it.ad_spend or 0), 'storage_fee': float(it.storage_fee or 0),
            'aged_storage_fee': float(it.aged_storage_fee or 0),
            'returns_fee': float(it.returns_fee or 0), 'inbound_fee': float(it.inbound_fee or 0),
            'removal_fee': float(it.removal_fee or 0),
            'net_profit_rmb': float(it.net_profit_rmb or 0),
            'cost_status': it.cost_status, 'cost_inherited_from': it.cost_inherited_from,
        } for it in items],
    }


@router.get("/compare")
def compare_versions(snapshot_a: int, snapshot_b: int, db: Session = Depends(get_db),
                     user=Depends(require_module("accounting"))):
    """版本对比：逐 SKU（新版快照为 SKU×站点粒度）差异

    键策略（B-01 后新旧快照混合兼容）：
      · 新版快照 = rate_source 非空（明细带 country）；旧版 = rate_source 空
      · 两边都新版 → 键 (asin, sku, country)，输出行带 country
      · 任一边旧版（跨代对比）→ 新版先按 (asin, sku) 归一（各站点合并），键 (asin, sku)
    """
    a = db.query(DwsProfitSnapshot).get(snapshot_a)
    b = db.query(DwsProfitSnapshot).get(snapshot_b)
    if not a or not b:
        raise HTTPException(status_code=404, detail="快照不存在")

    def _load(snap):
        return db.query(DwsProfitSnapshotItem).filter(
            DwsProfitSnapshotItem.snapshot_id == snap.id).all()

    def _norm(items, by_country: bool):
        out = {}
        for i in items:
            key = (i.asin, i.sku, i.country or '') if by_country else (i.asin, i.sku)
            cur = out.get(key)
            if cur is None:
                out[key] = {'asin': i.asin, 'sku': i.sku, 'country': (i.country or '') if by_country else '',
                            'profit': float(i.net_profit_rmb or 0)}
            else:
                cur['profit'] += float(i.net_profit_rmb or 0)
        return out

    a_is_new = bool(a.rate_source)
    b_is_new = bool(b.rate_source)
    by_country = a_is_new and b_is_new
    ai = _norm(_load(a), by_country)
    bi = _norm(_load(b), by_country)
    diffs = []
    for k in sorted(set(ai) | set(bi)):
        x, y = ai.get(k), bi.get(k)
        px = x['profit'] if x else 0.0
        py = y['profit'] if y else 0.0
        if abs(px - py) > 0.005:
            src = (x or y)
            diffs.append({'asin': src['asin'], 'sku': src['sku'], 'country': src['country'],
                          'profit_a': round(px, 2), 'profit_b': round(py, 2),
                          'delta': round(py - px, 2)})
    return {
        'snapshot_a': {'id': a.id, 'version': a.version,
                       'profit': float(a.net_profit_rmb or 0)},
        'snapshot_b': {'id': b.id, 'version': b.version,
                       'profit': float(b.net_profit_rmb or 0)},
        'total_delta': round(float(b.net_profit_rmb or 0) - float(a.net_profit_rmb or 0), 2),
        'changed_items': len(diffs), 'diffs': diffs[:500],
    }


@router.get("/cost-alerts")
def list_cost_alerts(store: str | None = None, period_month: str | None = None,
                     status: str = "pending",
                     db: Session = Depends(get_db), user=Depends(require_module("accounting"))):
    """成本/运费缺失预警清单（待补录队列）"""
    store_obj = filter_store_by_permission(user, store, db) if store else None
    q = db.query(CostMissingAlert)
    if store_obj:
        q = q.filter(CostMissingAlert.store_id == store_obj.id)
    if period_month:
        q = q.filter(CostMissingAlert.period == period_month)
    if status and status != 'all':
        q = q.filter(CostMissingAlert.status == status)
    alerts = q.order_by(CostMissingAlert.detected_at.desc()).limit(500).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    return {'total': len(alerts), 'items': [{
        'id': a.id, 'store_code': stores.get(a.store_id, ''),
        'asin': a.asin, 'sku': a.sku, 'country': a.country, 'period': a.period,
        'missing_type': a.missing_type, 'status': a.status,
        'detected_at': a.detected_at.isoformat() if a.detected_at else None,
    } for a in alerts]}


@router.post("/cost-entry")
def create_cost_entry(req: CostEntryRequest, db: Session = Depends(get_db),
                      user: SysUser = Depends(require_module("accounting"))):
    """成本补录 — 写入 product_cost_history（source_type=manual），并关闭对应预警"""
    store = filter_store_by_permission(user, req.store, db)
    if not store:
        raise HTTPException(status_code=404, detail=f"店铺 {req.store} 不存在")
    if req.cost_rmb is None and req.freight is None:
        raise HTTPException(status_code=400, detail="成本和运费至少填一项")
    existing = db.query(ProductCostHistory).filter(
        ProductCostHistory.store_id == store.id,
        ProductCostHistory.asin == req.asin, ProductCostHistory.sku == req.sku,
        ProductCostHistory.period == req.period, ProductCostHistory.source_type == 'manual',
    ).first()
    freight_json = {'default': req.freight} if req.freight is not None else None
    if existing:
        if req.cost_rmb is not None:
            existing.cost_rmb = req.cost_rmb
        if freight_json is not None:
            existing.freight_json = freight_json
    else:
        db.add(ProductCostHistory(
            store_id=store.id, asin=req.asin, sku=req.sku, period=req.period,
            cost_rmb=req.cost_rmb, freight_json=freight_json,
            source_type='manual', created_by=user.id,
        ))
    # 关闭预警
    closed = 0
    for a in db.query(CostMissingAlert).filter(
        CostMissingAlert.store_id == store.id,
        CostMissingAlert.asin == req.asin, CostMissingAlert.sku == req.sku,
        CostMissingAlert.period == req.period, CostMissingAlert.status == 'pending',
    ).all():
        a.status = 'resolved'
        a.resolved_at = datetime.now()
        a.resolved_by = user.id
        closed += 1
    db.add(OperationLog(user_id=user.id, username=user.username, action="cost_entry",
                        target=f"{req.store}/{req.asin}/{req.sku}/{req.period}", result="ok",
                        detail=f"cost={req.cost_rmb} freight={req.freight} closed_alerts={closed}"))
    db.commit()
    return {'ok': True, 'closed_alerts': closed}


@router.post("/cost-alerts/{alert_id}/ignore")
def ignore_alert(alert_id: int, db: Session = Depends(get_db),
                 user: SysUser = Depends(require_module("accounting"))):
    """忽略预警（已忽略项是否下期重现 = 待确认 Q14；当前实现为下期重新检出）"""
    a = db.query(CostMissingAlert).get(alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="预警不存在")
    a.status = 'ignored'
    a.resolved_at = datetime.now()
    a.resolved_by = user.id
    db.add(OperationLog(user_id=user.id, username=user.username, action="ignore_alert",
                        target=str(alert_id), result="ok",
                        detail=f"{a.asin}/{a.sku}/{a.period}: pending -> ignored"))
    db.commit()
    return {'ok': True}
