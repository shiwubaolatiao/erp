"""导入后台任务 worker — sys_task 表 + 守护线程（组6.3，tasks.md 6.3）

迁移自进程内 _TASKS 注册表：任务状态/batches/进度落 sys_task（跨重启可见
中断态与已完成文件数，可对未完成部分重试）；logs 仅进程内保留（超限截断），
文件级结果以 import_batch 为准（可从 /import/batches 查历史）。

保持不变的对外契约：
  · submit_import(files, store_code, user_id, period_month, db) 签名
  · get_task_status / list_tasks 返回旧任务 dict 形态
    （id/status/total_files/done_files/current/logs/batches/started_at/...）
  · status 枚举扩展 interrupted（进程重启识别）
"""
import threading

from app.core.database import SessionLocal
from app.services.importer.pipeline import ImportTask
from app.workers.task_worker import (
    register_handler, submit_task as _submit, get_task as _get_task,
    list_tasks as _list_tasks,
)

TASK_TYPE = 'import.batch'

# 进程内日志缓存：task_key -> [lines]（跨重启丢失，可接受 —— 批次详情在库）
_LOGS: dict = {}
_LOGS_LOCK = threading.Lock()

MAX_LOG_LINES = 500          # 日志条数上限（超出丢弃最早的 200 条）
FINAL = ('success', 'failed', 'interrupted')


def _append_log(task_key: str, msg: str) -> None:
    with _LOGS_LOCK:
        lines = _LOGS.setdefault(task_key, [])
        lines.append(msg)
        if len(lines) > MAX_LOG_LINES:
            del lines[:200]


def _import_handler(db, task, set_progress):
    """导入任务主体：逐文件解析入库，单文件失败不影响其余（MM-01）"""
    p = task.payload or {}
    files = p.get('files') or []
    store_code, user_id = p.get('store_code'), p.get('user_id')
    period_month = p.get('period_month')
    task_key = task.task_key

    result = {'total_files': len(files), 'done_files': 0, 'current': '', 'batches': []}
    set_progress(0, stage='importing', result=result)

    for idx, (path, name) in enumerate(files):
        result['current'] = name
        set_progress(stage='importing', result=result)
        _append_log(task_key, f"[{idx + 1}/{len(files)}] 开始解析 {name}")

        def progress(msg, _key=task_key):
            _append_log(_key, msg)

        meta = {'id': task_key, 'progress_cb': progress, 'period_month': period_month}
        try:
            t = ImportTask(db, path, name, store_code, user_id, meta)
            batch = t.run()
            result['batches'].append({
                'batch_id': batch.id, 'file_name': name,
                'status': batch.status, 'success': batch.success_rows,
                'failed': batch.failed_rows,
            })
        except Exception as e:
            # 单文件失败：回滚该文件的未提交内容，继续下一个文件
            db.rollback()
            _append_log(task_key, f"文件失败 {name}: {e}")
            result['batches'].append({'file_name': name, 'status': 'failed',
                                      'error': str(e)})
        result['done_files'] = idx + 1
        pct = int(result['done_files'] * 100 / len(files)) if files else 100
        set_progress(pct, stage='importing', result=result)

    result['current'] = ''
    set_progress(100, stage='done', result=result)


register_handler(TASK_TYPE, _import_handler)


def submit_import(files: list[tuple[str, str]], store_code, user_id,
                  period_month, db) -> dict:
    """受理导入任务 → 立即返回任务句柄（旧 dict 形态兼容）；后台线程逐文件解析"""
    t = _submit(TASK_TYPE, {
        'files': [list(f) for f in files], 'store_code': store_code,
        'user_id': user_id, 'period_month': period_month,
    }, created_by=user_id)
    return _to_legacy_dict(t)


def _to_legacy_dict(t: dict) -> dict:
    """sys_task dict → 旧导入任务 dict 形态（前端 /import/tasks 契约不变）"""
    result = t.get('result') or {}
    payload = t.get('payload') or {}
    files = payload.get('files') or []
    with _LOGS_LOCK:
        logs = list(_LOGS.get(t['task_id'], []))
    return {
        'id': t['task_id'],
        'status': t['status'],           # queued/running/success/failed/interrupted
        'total_files': result.get('total_files', len(files)),
        'done_files': result.get('done_files', 0),
        'current': result.get('current', ''),
        'logs': logs,
        'batches': result.get('batches', []),
        'started_at': t.get('started_at'),
        'finished_at': t.get('finished_at'),
        'error': t.get('error'),
    }


def get_task_status(task_id: str) -> dict | None:
    """查询单个任务（缓存优先；跨重启从 sys_task 恢复状态与已完成文件数）"""
    t = _get_task(task_id)
    if t is None or t['task_type'] != TASK_TYPE:
        return None
    return _to_legacy_dict(t)


def list_tasks(limit: int = 30) -> list[dict]:
    """任务列表（按创建倒序，直查 sys_task —— 跨重启可见中断态与历史）"""
    return [_to_legacy_dict(t) for t in _list_tasks(TASK_TYPE, limit)]
