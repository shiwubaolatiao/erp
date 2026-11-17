"""核算后台任务 handler — task_worker 注册制（组6，tasks.md 6.2）

进度语义（calculate_snapshot 为不可拆分的整体调用，阶段为粗粒度标记）：
  fetching(10) → calculating(40) → saving(90) → done(100)
任务失败：异常抛出 → task_worker 统一 rollback（calculate_snapshot 单事务，
无半成品快照）+ sys_task.error 保留原因。
"""
from datetime import datetime

from app.models import OperationLog
from app.services.accounting.service import calculate_snapshot
from app.workers.task_worker import register_handler

TASK_TYPE = 'accounting.calculate'


def _run_calculate(db, task, set_progress):
    p = task.payload or {}
    set_progress(10, stage='fetching')
    set_progress(40, stage='calculating')
    snap = calculate_snapshot(
        db, p['store_id'], p.get('country') or '', p.get('period_month') or '',
        created_by=p.get('created_by'),
        snapshot_name=p.get('snapshot_name') or '',
        date_from=datetime.fromisoformat(p['date_from']) if p.get('date_from') else None,
        date_to=datetime.fromisoformat(p['date_to']) if p.get('date_to') else None,
    )
    set_progress(90, stage='saving')
    db.add(OperationLog(
        user_id=p.get('created_by'), username=p.get('username') or '',
        action="calculate", target=f"store={p.get('store_code')} period={p.get('period_month')}",
        result="ok",
        detail=f"snapshot_id={snap.id} version={snap.version} profit={snap.net_profit_rmb}"))
    result = {
        'snapshot_id': snap.id, 'version': snap.version,
        'period_month': snap.period_month, 'country': snap.country,
        'net_amazon': float(snap.net_amazon or 0),
        'exchange_rate': float(snap.exchange_rate or 0),
        'rate_source': snap.rate_source,
        'product_sales_rmb': float(snap.product_sales_rmb or 0),
        'net_profit_rmb': float(snap.net_profit_rmb or 0),
        'net_profit_rate': float(snap.net_profit_rate or 0),
        'cost_complete': snap.cost_complete,
        'cost_incomplete_skus': snap.cost_incomplete_skus,
        'order_count': snap.order_count, 'order_qty': snap.order_qty,
    }
    set_progress(100, stage='done', result=result, result_ref=str(snap.id))


register_handler(TASK_TYPE, _run_calculate)