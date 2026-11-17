"""S22 任务生命周期回归（组6，tasks.md 6.5）

覆盖：
  · 四态流转 queued→running→success / failed（核算任务真实跑通）
  · 任务失败保留原因且无半成品快照（事务回滚）
  · async=false 同步兼容（旧调用方响应结构）
  · 进程重启 interrupted 识别（recover_interrupted）
  · 注册表 TTL（TASK_RETENTION_HOURS）与条数上限（TASK_MAX_RECORDS）
  · 并发核算信号量（ACCOUNTING_CONCURRENCY=2）
  · 导入任务旧 dict 形态兼容（sys_task 迁移后字段不缺）
"""
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import SysTask
from app.workers.task_worker import (
    submit_task, get_task, recover_interrupted, cleanup_finished, register_handler,
)
from app.workers.import_worker import get_task_status, list_tasks

from conftest import calc_and_wait, sql_one

FINAL = ('success', 'failed', 'interrupted')


def _wait_final(task_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = get_task(task_id)
        if t['status'] in FINAL:
            return t
        time.sleep(0.1)
    raise TimeoutError(f'任务 {task_id} 未达终态')


def _insert_task(**kw) -> str:
    db = SessionLocal()
    try:
        t = SysTask(**kw)
        db.add(t)
        db.commit()
        return t.task_key
    finally:
        db.close()


class TestTaskLifecycle:
    def test_success_flow(self, client, admin_headers, e2e):
        """核算任务 queued→running→success：progress=100、stage=done、result_ref=snapshot_id"""
        r = client.post('/api/v2/accounting/calculate', headers=admin_headers,
                        json={'store': 'LMG', 'country': 'UK', 'period_month': '2026-03'})
        assert r.status_code == 200, r.text
        accepted = r.json()
        assert accepted['task_id'] and accepted['poll_url'].endswith(accepted['task_id'])
        assert accepted['status'] in ('queued', 'running')

        t = _wait_final(accepted['task_id'])
        assert t['status'] == 'success'
        assert t['progress'] == 100 and t['stage'] == 'done'
        snap_id = int(t['result_ref'])
        assert t['result']['snapshot_id'] == snap_id
        # 任务详情接口（前端轮询契约）
        d = client.get(f"/api/v2/accounting/tasks/{accepted['task_id']}",
                       headers=admin_headers).json()
        assert d['status'] == 'success' and d['snapshot_id'] == snap_id
        assert d['snapshot']['snapshot_id'] == snap_id

    def test_failed_keeps_error_no_snapshot(self, client, admin_headers, e2e):
        """非法月份 → 任务 failed 保留原因；该月无半成品快照（事务回滚）"""
        r = client.post('/api/v2/accounting/calculate', headers=admin_headers,
                        json={'store': 'LMG', 'country': 'UK', 'period_month': 'bad-month'})
        assert r.status_code == 200, r.text          # 受理成功，后台失败
        task_id = r.json()['task_id']
        t = _wait_final(task_id)
        assert t['status'] == 'failed'
        assert t['error'], '失败原因必须保留'
        d = client.get(f"/api/v2/accounting/tasks/{task_id}",
                       headers=admin_headers).json()
        assert d['status'] == 'failed' and d['snapshot_id'] is None
        assert sql_one("SELECT id FROM dws_profit_snapshot WHERE period_month='bad-month'") is None

    def test_sync_mode_compat(self, client, admin_headers, e2e):
        """async=false（Deprecated）：同步等待完成，响应结构与旧版一致"""
        r = client.post('/api/v2/accounting/calculate', headers=admin_headers,
                        json={'store': 'LMG', 'country': 'DE', 'period_month': '2026-01',
                              'async': False})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['status'] == 'done' and body['task_id']
        for field in ('snapshot_id', 'version', 'net_profit_rmb', 'product_sales_rmb',
                      'net_amazon', 'exchange_rate', 'cost_complete',
                      'order_count', 'order_qty'):
            assert field in body, f'同步响应缺旧字段 {field}'

    def test_recover_interrupted(self, client):
        """遗留 queued/running → recover_interrupted 全部标记 interrupted（spec 5.4.3 场景2）"""
        k1 = _insert_task(task_key='t_intr_run', task_type='accounting.calculate',
                          status='running', payload={})
        k2 = _insert_task(task_key='t_intr_q', task_type='import.batch',
                          status='queued', payload={})
        try:
            n = recover_interrupted()
            assert n >= 2
            assert get_task(k1)['status'] == 'interrupted'
            assert get_task(k2)['status'] == 'interrupted'
            assert '中断' in (get_task(k1)['error'] or '')
        finally:
            db = SessionLocal()
            try:
                db.query(SysTask).filter(SysTask.task_key.in_((k1, k2))).delete(
                    synchronize_session=False)
                db.commit()
            finally:
                db.close()

    def test_import_task_dict_shape(self, client, e2e):
        """导入任务旧 dict 形态兼容：sys_task 迁移后字段齐全 + batches 可见

        ★ 必须排在 cleanup 测试之前：本用例依赖 e2e 产生的导入任务仍在
        sys_task 中，而后面的 cleanup 用例会全库清理完成态任务。
        """
        items = list_tasks(10)
        assert items, 'e2e 已跑，导入任务列表不应为空'
        t = items[0]
        assert {'id', 'status', 'total_files', 'done_files', 'current', 'logs',
                'batches', 'started_at', 'finished_at', 'error'} <= set(t)
        assert t['total_files'] >= 1 and t['done_files'] == t['total_files']
        assert t['batches'], '已完成文件数跨重启可见（batches 落库）'
        single = get_task_status(t['id'])
        assert single and single['id'] == t['id']

    def test_cleanup_retention_hours(self, client, monkeypatch):
        """超保留期的完成态任务被删除；running 态绝不删除"""
        monkeypatch.setattr(settings, 'TASK_RETENTION_HOURS', 0)
        # finished_at 取 2 秒前：明确构造"已过期"数据（retention=0 时 now 附近的
        # 值受 MySQL DATETIME 秒级舍入影响，边界不稳定）
        old = _insert_task(task_key='t_old_done', task_type='accounting.calculate',
                           status='success', payload={},
                           finished_at=datetime.now() - timedelta(seconds=2))
        keep = _insert_task(task_key='t_keep_run', task_type='accounting.calculate',
                            status='running', payload={})
        try:
            cleanup_finished()
            assert get_task(old) is None, '过期完成态任务应被删除'
            assert get_task(keep) is not None, 'running 态任务不得被清理'
        finally:
            db = SessionLocal()
            try:
                db.query(SysTask).filter(SysTask.task_key.in_((old, keep))).delete(
                    synchronize_session=False)
                db.commit()
            finally:
                db.close()

    def test_cleanup_max_records(self, client, monkeypatch):
        """条数超上限：按 id 删最早的完成态，保留最新与进行中"""
        monkeypatch.setattr(settings, 'TASK_MAX_RECORDS', 2)
        keys = [_insert_task(task_key=f't_cap_{i}', task_type='accounting.calculate',
                             status='success', payload={},
                             finished_at=datetime.now()) for i in range(4)]
        try:
            cleanup_finished()
            remain = [get_task(k) for k in keys]
            assert sum(1 for r in remain if r) <= 2, '完成态条数应裁剪到上限内'
            assert remain[-1] is not None, '最新完成态应保留'
        finally:
            db = SessionLocal()
            try:
                db.query(SysTask).filter(SysTask.task_key.in_(keys)).delete(
                    synchronize_session=False)
                db.commit()
            finally:
                db.close()

    def test_accounting_concurrency_limit(self, client):
        """并发核算信号量：4 个核算任务并发峰值 ≤ ACCOUNTING_CONCURRENCY"""
        peak = {'cur': 0, 'max': 0}
        lock = threading.Lock()

        def slow_handler(db, task, set_progress):
            with lock:
                peak['cur'] += 1
                peak['max'] = max(peak['max'], peak['cur'])
            time.sleep(0.4)
            with lock:
                peak['cur'] -= 1
            set_progress(100, stage='done')

        register_handler('accounting.test_slow', slow_handler)
        tids = [submit_task('accounting.test_slow', {'i': i})['task_id'] for i in range(4)]
        for tid in tids:
            assert _wait_final(tid)['status'] == 'success'
        assert peak['max'] <= settings.ACCOUNTING_CONCURRENCY, \
            f"并发峰值 {peak['max']} 超过限流 {settings.ACCOUNTING_CONCURRENCY}"

