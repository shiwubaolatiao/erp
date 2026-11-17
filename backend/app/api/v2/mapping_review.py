# -*- coding: utf-8 -*-
"""映射审核增强管理 API（mapping_review_enhance 组6，DD1：独立 router，admin.py 不动）

前缀 /api/v2/admin/mapping-review，全部 require_admin（spec 4.3.1）。
端点：
  POST /baseline/import          基准导入（dir_path / file_paths 二选一，dry_run 可选）
  GET  /baseline/summary         基准概览
  POST /bulk-confirm             pending 批量确认（dry_run 默认 true）
  POST /replacement/realign      存量 amzn.gr.* 重归一（dry_run 默认 true）
  POST /replacement/assign       单条人工归类（DD8 可选能力）
  GET  /reports                  报告列表
  GET  /reports/{id}             报告详情（重建）
  GET  /reports/{id}/export      清单 xlsx 导出
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v2.export import _xlsx_response
from app.core.database import get_db
from app.core.security import require_admin
from app.models import SkuReplacement, Store, SysUser
from app.services.mapping import report as report_svc
from app.services.mapping.baseline import baseline_summary, import_baseline
from app.services.mapping.bulk_confirm import (
    BaselineNotImported, bulk_confirm, rollback_by_report,
)
from app.services.mapping.similarity import main_skus, realign_replacements

router = APIRouter()


class BaselineImportRequest(BaseModel):
    dir_path: str | None = None
    file_paths: list[str] | None = None
    dry_run: bool = False


class BulkConfirmRequest(BaseModel):
    store: str | None = None
    dry_run: bool = True


class ReplacementRealignRequest(BaseModel):
    store: str | None = None
    dry_run: bool = True


class ReplacementAssignRequest(BaseModel):
    store: str
    replacement_sku: str
    parent_sku: str


class RollbackRequest(BaseModel):
    report_id: int


def _store_id_or_404(db: Session, store: str | None):
    if not store:
        return None
    st = db.query(Store).filter(Store.store_code == store.upper()).first()
    if not st:
        raise HTTPException(status_code=404, detail=f"店铺不存在: {store}")
    return st.id


@router.post("/baseline/import")
def baseline_import(req: BaselineImportRequest, db: Session = Depends(get_db),
                    user: SysUser = Depends(require_admin)):
    """基准导入：目录或显式文件清单（二者至少一项且路径有效）"""
    if not req.dir_path and not req.file_paths:
        raise HTTPException(status_code=400, detail="请提供 dir_path 或 file_paths")
    if req.file_paths:
        missing = [p for p in req.file_paths if not os.path.isfile(p)]
        if missing:
            raise HTTPException(status_code=400, detail=f"文件不存在: {missing[0]}")
    elif not os.path.isdir(req.dir_path):
        raise HTTPException(status_code=400, detail=f"目录不存在: {req.dir_path}")
    elif not any(f.lower().endswith(('.xlsx', '.xlsm')) and not f.startswith('~$')
                 for f in os.listdir(req.dir_path)):
        raise HTTPException(status_code=404, detail="目录内没有任何 xlsx 文件")

    result = import_baseline(db, dir_path=req.dir_path, file_paths=req.file_paths,
                             dry_run=req.dry_run,
                             operator_user_id=user.id, operator_username=user.username)
    if not result['details']['files']:
        raise HTTPException(status_code=404, detail="未匹配到任何基准文件")
    return result


@router.get("/baseline/summary")
def get_baseline_summary(db: Session = Depends(get_db),
                         user: SysUser = Depends(require_admin)):
    return baseline_summary(db)


@router.post("/bulk-confirm")
def post_bulk_confirm(req: BulkConfirmRequest, db: Session = Depends(get_db),
                      user: SysUser = Depends(require_admin)):
    """pending 批量确认（默认 dry-run 预览；正式执行须显式 dry_run=false）"""
    _store_id_or_404(db, req.store)
    try:
        return bulk_confirm(db, store=req.store, dry_run=req.dry_run,
                            operator_user_id=user.id, operator_username=user.username)
    except BaselineNotImported as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/bulk-confirm/rollback")
def post_bulk_confirm_rollback(req: RollbackRequest, db: Session = Depends(get_db),
                               user: SysUser = Depends(require_admin)):
    """按执行报告回滚批量确认（安全网，design 2.5.4）"""
    try:
        n = rollback_by_report(db, req.report_id,
                               operator_user_id=user.id, operator_username=user.username)
        return {'ok': True, 'rolled_back': n, 'report_id': req.report_id}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/replacement/realign")
def post_realign(req: ReplacementRealignRequest, db: Session = Depends(get_db),
                 user: SysUser = Depends(require_admin)):
    """存量 amzn.gr.* 替换件重归一（默认 dry-run）"""
    store_id = _store_id_or_404(db, req.store)
    result = realign_replacements(db, store_id=store_id, dry_run=req.dry_run)
    if not req.dry_run:
        result['report_id'] = report_svc.save_report(db, result, user.id)
        report_svc.log_mapping_operation(
            db, 'mapping_realign', result['store_scope'],
            f"修正 {result['summary']['changed']} / 保留 {result['summary']['kept_raw']} / "
            f"旧目标非法 {result['summary']['invalid_target']}",
            user.id, user.username)
        db.commit()
    else:
        result['report_id'] = None
    return result


@router.post("/replacement/assign")
def post_assign(req: ReplacementAssignRequest, db: Session = Depends(get_db),
                user: SysUser = Depends(require_admin)):
    """单条人工归类（DD8）：parent_sku 必须在主 SKU 集合内；replacement_sku 须为 amzn.gr.*"""
    sid = _store_id_or_404(db, req.store)
    raw = (req.replacement_sku or '').strip()
    if not raw.lower().startswith('amzn.gr.'):
        raise HTTPException(status_code=400, detail="replacement_sku 不是 amzn.gr.* 形态")
    skus = main_skus(db, sid)
    if req.parent_sku not in skus:
        raise HTTPException(status_code=400, detail="parent_sku 不在该店铺主 SKU 集合内")
    existing = db.query(SkuReplacement).filter(
        SkuReplacement.store_id == sid,
        SkuReplacement.replacement_sku == raw).first()
    prev = None
    if existing is None:
        db.add(SkuReplacement(store_id=sid, replacement_sku=raw, parent_sku=req.parent_sku))
    else:
        prev = existing.parent_sku
        existing.prev_parent_sku = prev
        existing.parent_sku = req.parent_sku
    report_svc.log_mapping_operation(
        db, 'mapping_realign', f"assign {raw}",
        f"人工归类 {raw} -> {req.parent_sku}（旧值 {prev}）",
        user.id, user.username)
    db.commit()
    return {'ok': True, 'prev_parent_sku': prev}


@router.get("/reports")
def get_reports(op_type: str | None = None, limit: int = 20,
                db: Session = Depends(get_db), user: SysUser = Depends(require_admin)):
    try:
        return {'items': report_svc.list_reports(db, op_type=op_type, limit=limit)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/reports/{report_id}")
def get_report_detail(report_id: int, db: Session = Depends(get_db),
                      user: SysUser = Depends(require_admin)):
    r = report_svc.get_report(db, report_id)
    if not r:
        raise HTTPException(status_code=404, detail="报告不存在")
    return r


@router.get("/reports/{report_id}/export")
def get_report_export(report_id: int, section: str = 'all',
                      db: Session = Depends(get_db),
                      user: SysUser = Depends(require_admin)):
    try:
        wb, fname = report_svc.export_report(db, report_id, section)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _xlsx_response(wb, fname)