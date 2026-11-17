"""通用后台任务执行器 — "线程 + DB 任务表"模式（组6，tasks.md 6.1）

为什么不用 Celery/Redis：本系统是局域网单机部署，核算/导入并发需求低；
DB 任务表让任务状态跨重启可见（interrupted 识别、已完成文件数、历史审计），
进程内缓存仅用于加速轮询。

设计要点：
  1. 任务函数注册制：register_handler(task_type, fn)，fn 签名
     (db, task_row, set_progress)；set_progress(progress, stage, result, result_ref)
     每次调用即落库（跨重启可见增量，如导入的已完成文件清单）
  2. 核算并发信号量 ACCOUNTING_CONCURRENCY=2（导入不限流）
  3. 完成态惰性清理：TASK_RETENTION_HOURS=24 超时删除 + TASK_MAX_RECORDS=500
     条数上限（绝不删除 queued/running）；submit 时触发，5 分钟节流
  4. 进程启动 recover_interrupted()：遗留 queued/running → interrupted
     （杜绝重启后假成功，spec 5.4.3 场景 2）
"""
import threading
import traceback
import uuid
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import SysTask

# task_type -> handler(db, task_row, set_progress)
_HANDLERS: dict = {}

# 进程内轮询缓存：task_key -> 任务 dict（执行线程实时更新；完成态跨进程也可查库）
_TASK_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CLEANUP_LOCK = threading.Lock()
_LAST_CLEANUP = {'at': datetime.min}
_ACCOUNTING_SEM = threading.Semaphore(settings.ACCOUNTING_CONCURRENCY)

FINAL_STATUSES = ('success', 'failed', 'interrupted')


def register_handler(task_type: str, fn) -> None:
    """注册任务处理函数（业务模块 import 时调用一次）"""
    _HANDLERS[task_type] = fn


def _row_to_dict(t: SysTask) -> dict:
    return {
        'task_id': t.task_key, 'task_type': t.task_type, 'status': t.status,
        'progress': t.progress or 0, 'stage': t.stage or '',
        'payload': t.payload, 'result': t.result, 'result_ref': t.result_ref or '',
        'error': t.error, 'created_by': t.created_by,
        'created_at': t.created_at.isoformat() if t.created_at else None,
        'started_at': t.started_at.isoformat() if t.started_at else None,
        'finished_at': t.finished_at.isoformat() if t.finished_at else None,
    }


def _cache_put(d: dict) -> None:
    with _CACHE_LOCK:
        _TASK_CACHE[d['task_id']] = d


def _cache_update(task_key: str, **fields) -> None:
    with _CACHE_LOCK:
        d = _TASK_CACHE.get(task_key)
        if d is not None:
            d.update(fields)


def _cache_drop(task_keys: list[str]) -> None:
    with _CACHE_LOCK:
        for k in task_keys:
            _TASK_CACHE.pop(k, None)


def _get_row(db, task_key: str):
    return db.query(SysTask).filter(SysTask.task_key == task_key).first()


def get_task(task_key: str) -> dict | None:
    """查询单个任务（缓存优先；进行态 miss 时查库不缓存，保证跨进程新鲜度）"""
    with _CACHE_LOCK:
        if task_key in _TASK_CACHE:
            return dict(_TASK_CACHE[task_key])
    db = SessionLocal()
    try:
        t = _get_row(db, task_key)
        if not t:
            return None
        d = _row_to_dict(t)
        if t.status in FINAL_STATUSES:
            _cache_put(d)
        return d
    finally:
        db.close()


def list_tasks(task_type: str | None = None, limit: int = 30) -> list[dict]:
    """任务列表（按创建倒序，直查 DB —— 跨重启可见中断态与历史）"""
    db = SessionLocal()
    try:
        q = db.query(SysTask)
        if task_type:
            q = q.filter(SysTask.task_type == task_type)
        return [_row_to_dict(t) for t in
                q.order_by(SysTask.id.desc()).limit(limit).all()]
    finally:
        db.close()


def submit_task(task_type: str, payload: dict, created_by: int | None = None) -> dict:
    """受理任务 → 立即返回任务句柄（1 秒内）；后台线程执行

    任务行先落库（status=queued）再启动线程 —— 即使线程启动失败任务也可见。
    """
    if task_type not in _HANDLERS:
        raise ValueError(f"未注册的任务类型: {task_type}")
    task_key = uuid.uuid4().hex[:16]
    db = SessionLocal()
    try:
        db.add(SysTask(task_key=task_key, task_type=task_type, status='queued',
                       payload=payload, created_by=created_by))
        db.commit()
    finally:
        db.close()
    _maybe_cleanup()
    d = {'task_id': task_key, 'task_type': task_type, 'status': 'queued',
         'progress': 0, 'stage': '', 'payload': payload, 'result': None,
         'result_ref': '', 'error': None, 'created_by': created_by,
         'created_at': datetime.now().isoformat(),
         'started_at': None, 'finished_at': None}
    _cache_put(d)
    threading.Thread(target=_execute, args=(task_key, task_type), daemon=True,
                     name=f"task-{task_key}").start()
    return d


def _execute(task_key: str, task_type: str) -> None:
    """后台线程主体：queued → running → success/failed（信号量限流 accounting.*）"""
    handler = _HANDLERS[task_type]
    sem = _ACCOUNTING_SEM if task_type.startswith('accounting') else None
    if sem:
        sem.acquire()
    db = SessionLocal()
    try:
        t = _get_row(db, task_key)
        if t is None:
            return
        started = datetime.now()
        t.status, t.started_at, t.progress = 'running', started, max(t.progress or 0, 5)
        db.commit()
        _cache_update(task_key, status='running', progress=t.progress,
                      started_at=started.isoformat())

        def set_progress(progress: int | None = None, stage: str | None = None,
                         result: dict | None = None, result_ref: str | None = None) -> None:
            """任务进度回调（handler 调用；每次立即落库，跨重启可见）"""
            if progress is not None:
                t.progress = int(progress)
            if stage is not None:
                t.stage = stage
            if result is not None:
                t.result = result
            if result_ref is not None:
                t.result_ref = str(result_ref)
            db.commit()
            _cache_update(task_key, progress=t.progress or 0, stage=t.stage or '',
                          result=t.result, result_ref=t.result_ref or '')

        try:
            handler(db, t, set_progress)
            t.status, t.progress, t.finished_at = 'success', 100, datetime.now()
            if not t.stage:
                t.stage = 'done'
            db.commit()
        except Exception as e:
            # 二次异常防护：即使状态落库失败也不能让任务线程无痕崩溃（卡死 running）
            try:
                db.rollback()
                t2 = _get_row(db, task_key)
                if t2 is not None:
                    t2.status = 'failed'
                    t2.error = f"{e}\n{traceback.format_exc()[:2000]}"
                    t2.finished_at = datetime.now()
                    db.commit()
            except Exception:
                pass
        finally:
            try:
                fresh = _get_row(db, task_key)
                if fresh is not None:
                    _cache_put(_row_to_dict(fresh))
            except Exception:
                pass
    finally:
        if sem:
            sem.release()
        db.close()


def recover_interrupted() -> int:
    """进程启动扫描：遗留 queued/running 全部标记 interrupted（可重试）"""
    db = SessionLocal()
    try:
        n = (db.query(SysTask)
             .filter(SysTask.status.in_(('queued', 'running')))
             .update({SysTask.status: 'interrupted',
                      SysTask.finished_at: datetime.now(),
                      SysTask.error: '进程重启，任务中断（可重试）'},
                     synchronize_session=False))
        db.commit()
        if n:
            _cache_drop([k for k in list(_TASK_CACHE) if _TASK_CACHE[k]['status']
                         in ('queued', 'running')])
        return n
    finally:
        db.close()


def cleanup_finished() -> int:
    """完成态清理：超保留期删除 + 条数上限裁剪（只删完成态，返回删除条数）

    绝不删除 queued/running；本函数无节流（惰性触发节流在 _maybe_cleanup 层）。
    """
    deleted_keys: list[str] = []
    db = SessionLocal()
    try:
        cutoff = datetime.now() - timedelta(hours=settings.TASK_RETENTION_HOURS)
        expired = db.query(SysTask).filter(
            SysTask.status.in_(FINAL_STATUSES),
            SysTask.finished_at.isnot(None),
            SysTask.finished_at < cutoff).all()
        for t in expired:
            deleted_keys.append(t.task_key)
            db.delete(t)
        db.commit()

        total = db.query(SysTask).count()
        if total > settings.TASK_MAX_RECORDS:
            excess = db.query(SysTask).filter(
                SysTask.status.in_(FINAL_STATUSES)
            ).order_by(SysTask.id).limit(total - settings.TASK_MAX_RECORDS).all()
            for t in excess:
                deleted_keys.append(t.task_key)
                db.delete(t)
            db.commit()
    finally:
        db.close()
    if deleted_keys:
        _cache_drop(deleted_keys)
    return len(deleted_keys)


def _maybe_cleanup() -> None:
    """submit 时惰性触发清理（5 分钟节流，避免每次提交都扫表）"""
    now = datetime.now()
    if (now - _LAST_CLEANUP['at']).total_seconds() < 300:
        return
    with _CLEANUP_LOCK:
        _LAST_CLEANUP['at'] = now
        try:
            cleanup_finished()
        except Exception:
            pass  # 清理失败不影响任务受理