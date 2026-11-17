"""导入 API — 文件上传 → 后台任务 → 进度查询 → 批次管理与锁结

接口前缀: /api/v2/import

工作流（对应文档 MM-01）：
    上传(多文件) → 立即返回 task_id → 后台线程逐文件解析
      → 每个文件生成一个 import_batch，逐 sheet 全量入 ODS + 按类型清洗入 DWD
      → 前端轮询 /tasks/{id} 拿进度与失败清单
      → 导入报告 /batches/{id}/report 含"成本缺失"区块
      → 核对无误后 /batches/{id}/lock 锁结（锁后不可改）；解锁需管理员

★ 设计取舍：
  - 上传接口**立刻返回**，不阻塞页面（大报表单文件可达 20-30 秒）
  - 任务进度存在进程内注册表（刷新页面不中断）；进程重启会丢失"进度"，
    但**已入库的数据与批次记录不受影响**（批次结果落 import_batch）
  - period_month 仅用于"无日期列"的数据按月归属（成本表/汇率表/广告表）；
    销售数据一律按**原始业务日期**归月，与该参数无关
"""
import os
import shutil
import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.database import get_db
from app.core.security import require_module, get_current_user, require_admin
from app.models import ImportBatch, ImportFile, RawSheet, ImportRowError, OperationLog, SysUser, Store
from app.workers.import_worker import submit_import, get_task_status, list_tasks

router = APIRouter()

# 允许上传的报表文件类型
ALLOWED_EXT = {'.xlsx', '.xlsm', '.csv'}


@router.post("/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    store: str | None = Form(None),
    period_month: str | None = Form(None, description="报表归属月份 YYYY-MM（成本/汇率/广告归月依据）"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(require_module("import")),
):
    """批量上传报表 → 立即返回 task_id（后台异步解析）

    每个文件会：
      1. 落盘到 backend/uploads/（临时）
      2. 复制一份到 backend/archive/（永久归档，用于事后溯源核对原始文件）
      3. 计算 SHA256 查重（疑似重复会记录在批次 error_summary，但允许强制导入）
    """
    if len(files) > settings.MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"单次最多 {settings.MAX_UPLOAD_FILES} 个文件")
    saved = []
    for f in files:
        ext = os.path.splitext(f.filename or '')[1].lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(status_code=400, detail=f"不支持的文件类型: {f.filename}（仅 xlsx/csv）")
        # 用 uuid 命名临时文件，避免中文/重名导致的落盘问题
        dest = settings.UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
        with open(dest, 'wb') as out:
            shutil.copyfileobj(f.file, out)
        saved.append((str(dest), f.filename))
    if not saved:
        raise HTTPException(status_code=400, detail="未收到文件")
    task = submit_import(saved, store_code=store, user_id=user.id,
                         period_month=period_month, db=db)
    return {'task_id': task['id'], 'file_count': len(saved),
            'message': f'已受理 {len(saved)} 个文件，后台解析中'}


@router.get("/tasks")
def tasks(limit: int = 30, user: SysUser = Depends(get_current_user)):
    """最近的后台导入任务列表（含进度与每个文件的结果）"""
    return {'items': list_tasks(limit)}


@router.get("/tasks/{task_id}")
def task_status(task_id: str, user: SysUser = Depends(get_current_user)):
    """单个任务的状态与日志（前端轮询此接口显示进度）"""
    t = get_task_status(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    return t


@router.get("/batches")
def batches(limit: int = 50, db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    """批次列表（按导入时间倒序，含成功/失败行数与锁结状态）"""
    rows = db.query(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(limit).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    return {'items': [{
        'id': b.id, 'file_name': b.file_name, 'store_code': stores.get(b.store_id, ''),
        'report_type': b.report_type, 'status': b.status,
        'success_rows': b.success_rows, 'failed_rows': b.failed_rows,
        'is_locked': b.is_locked,
        'imported_at': b.imported_at.isoformat() if b.imported_at else None,
        'error_summary': b.error_summary,
    } for b in rows]}


@router.get("/batches/{batch_id}/report")
def batch_report(batch_id: int, db: Session = Depends(get_db),
                 user: SysUser = Depends(get_current_user)):
    """导入报告 — 一个批次到底导了什么、哪些行有问题

    返回三块：
      sheets 各 sheet 的识别结果（类型/国家/币种/表头行/行数）
      errors 异常行明细（行号 + 原因），最多 500 条
      cost_missing 成本/运费缺失清单（联动 MM-05 成本完整性检查）
    """
    batch = db.query(ImportBatch).get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    sheets = db.query(RawSheet).filter(RawSheet.batch_id == batch_id).all()
    errors = db.query(ImportRowError).filter(ImportRowError.batch_id == batch_id).limit(500).all()
    # 成本缺失区块（该批次涉及店铺/月份）—— 缺失清单在核算时写入 cost_missing_alert
    cost_block = []
    try:
        from app.models import CostMissingAlert
        q = db.query(CostMissingAlert).filter(CostMissingAlert.status == 'pending')
        if batch.store_id:
            q = q.filter(CostMissingAlert.store_id == batch.store_id)
        cost_block = [{'asin': a.asin, 'sku': a.sku, 'country': a.country,
                       'period': a.period, 'missing_type': a.missing_type}
                      for a in q.limit(200).all()]
    except Exception:
        # 报告接口不应因附属信息查询失败而整体失败
        pass
    return {
        'batch_id': batch.id, 'file_name': batch.file_name, 'status': batch.status,
        'success_rows': batch.success_rows, 'failed_rows': batch.failed_rows,
        # 归月依据（B-06）：未指定归属月份时可见回退标注，而非静默按当前月
        'period_month': batch.period_month,
        'period_source': batch.period_source,
        'period_source_note': ('未指定归属月份，成本/汇率/广告已按当前月归集'
                               if batch.period_source == 'default_current' else None),
        # 批次告警（4.4：合并工作簿归月偏差等，非失败不计数）
        'warnings': list(batch.warnings or []),
        'sheets': [{'sheet_name': s.sheet_name, 'report_type': s.report_type,
                    'country': s.country, 'currency': s.currency,
                    'header_row': s.detected_header_row, 'row_count': s.row_count} for s in sheets],
        'errors': [{'sheet_name': e.sheet_name, 'row_no': e.row_no,
                    'error_type': e.error_type, 'error_message': e.error_message} for e in errors],
        'cost_missing': cost_block,
        'cost_missing_count': len(cost_block),
    }


@router.post("/batches/{batch_id}/lock")
def lock_batch(batch_id: int, db: Session = Depends(get_db),
               user: SysUser = Depends(require_module("import"))):
    """批次锁结 — 锁后该批次数据视为已定稿（前端禁止覆盖导入，操作留痕）

    对应文档"锁结策略：批次锁结 + 核算快照两者兼有"。
    """
    b = db.query(ImportBatch).get(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="批次不存在")
    b.is_locked = True
    db.add(OperationLog(user_id=user.id, username=user.username, action="lock_batch",
                        target=str(batch_id), result="ok"))
    db.commit()
    return {'ok': True, 'is_locked': True}


@router.post("/batches/{batch_id}/unlock")
def unlock_batch(batch_id: int, db: Session = Depends(get_db),
                 user: SysUser = Depends(require_admin)):
    """解锁批次 — 敏感操作

    ★ 仅管理员可执行，且必须留痕（用于"解锁 → 补导 → 重算 → 重新锁结"流程）。
    """
    b = db.query(ImportBatch).get(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="批次不存在")
    b.is_locked = False
    db.add(OperationLog(user_id=user.id, username=user.username, action="unlock_batch",
                        target=str(batch_id), result="ok"))
    db.commit()
    return {'ok': True, 'is_locked': False}


@router.get("/supported")
def supported():
    """支持的报表类型与别名（前端展示用）"""
    from app.services.importer.parser import BUILTIN_HEADER_ALIASES, STORE_CODES
    return {
        'stores': STORE_CODES,
        'report_types': ['sales', 'product', 'ad', 'storage', 'aged_storage', 'removal',
                         'returns', 'inbound', 'exchange_rate'],
        'header_fields': sorted(BUILTIN_HEADER_ALIASES.keys()),
        'max_files': settings.MAX_UPLOAD_FILES,
    }
