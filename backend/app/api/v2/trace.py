"""数据溯源 — 汇总数字 → fact 记录 → 原始行 → 批次 → 文件/导入人
这是新系统的核心验收能力：任意一个汇总数字都能下钻还原到 Excel 原始行与单元格值。

溯源链路（文档 04-技术需求/02-数据模型.md 第 8 节）：
  dws_profit_snapshot_item → fact_*.raw_row_id → raw_sheet_row(sheet_name+row_no+values_json)
  → import_batch → import_file(file_name+file_hash+imported_by+imported_at+original_path)
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import cast, String as SAString
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import get_current_user, require_module
from app.models import (
    RawSheetRow, RawSheet, ImportBatch, ImportFile, ImportRowError,
    FactSalesTransaction, DwsProfitSnapshot, DwsProfitSnapshotItem,
)

router = APIRouter()


@router.get("/batch/{batch_id}")
def trace_batch(batch_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """批次全景：文件 / sheets / 行数 / 状态 / 异常"""
    batch = db.query(ImportBatch).get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    files = db.query(ImportFile).filter(ImportFile.batch_id == batch_id).all()
    sheets = db.query(RawSheet).filter(RawSheet.batch_id == batch_id).all()
    errors = db.query(ImportRowError).filter(ImportRowError.batch_id == batch_id).limit(200).all()
    return {
        'batch': {
            'id': batch.id, 'file_name': batch.file_name, 'file_hash': batch.file_hash,
            'store_id': batch.store_id, 'status': batch.status,
            'success_rows': batch.success_rows, 'failed_rows': batch.failed_rows,
            'is_locked': batch.is_locked,
            'imported_at': batch.imported_at.isoformat() if batch.imported_at else None,
            'imported_by': batch.imported_by, 'original_path': batch.original_path,
            'error_summary': batch.error_summary,
        },
        'files': [{'id': f.id, 'file_name': f.file_name, 'stored_path': f.stored_path} for f in files],
        'sheets': [{
            'id': s.id, 'sheet_name': s.sheet_name, 'report_type': s.report_type,
            'detected_header_row': s.detected_header_row, 'country': s.country,
            'currency': s.currency, 'row_count': s.row_count,
        } for s in sheets],
        'errors': [{
            'sheet_name': e.sheet_name, 'row_no': e.row_no,
            'error_type': e.error_type, 'error_message': e.error_message,
        } for e in errors],
    }


@router.get("/batches")
def list_batches(limit: int = 50, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """批次列表（按导入时间倒序）"""
    batches = db.query(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(limit).all()
    return {'items': [{
        'id': b.id, 'file_name': b.file_name, 'store_id': b.store_id, 'status': b.status,
        'success_rows': b.success_rows, 'failed_rows': b.failed_rows, 'is_locked': b.is_locked,
        'imported_at': b.imported_at.isoformat() if b.imported_at else None,
    } for b in batches]}


@router.get("/raw-row/{raw_row_id}")
def trace_raw_row(raw_row_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """原始行详情：sheet + 行号 + 完整 headers/values（单元格级溯源）"""
    row = db.query(RawSheetRow).get(raw_row_id)
    if not row:
        raise HTTPException(status_code=404, detail="原始行不存在")
    sheet = db.query(RawSheet).get(row.sheet_id)
    batch = db.query(ImportBatch).get(row.batch_id)
    headers = row.headers_json or []
    values = row.values_json or []
    cells = [{'header': headers[i] if i < len(headers) else f'col{i}',
              'value': values[i] if i < len(values) else None}
             for i in range(max(len(headers), len(values)))]
    return {
        'raw_row_id': row.id,
        'sheet_name': row.sheet_name,
        'row_no': row.row_no,
        'biz_date': row.biz_date.isoformat() if row.biz_date else None,
        'cells': cells,
        'batch': {
            'id': batch.id, 'file_name': batch.file_name, 'file_hash': batch.file_hash,
            'imported_at': batch.imported_at.isoformat() if batch.imported_at else None,
            'imported_by': batch.imported_by, 'is_locked': batch.is_locked,
            'original_path': batch.original_path,
        } if batch else None,
        'sheet': {
            'id': sheet.id, 'report_type': sheet.report_type, 'country': sheet.country,
            'currency': sheet.currency, 'detected_header_row': sheet.detected_header_row,
        } if sheet else None,
    }


@router.get("/fact/{fact_type}/{fact_id}")
def trace_fact(fact_type: str, fact_id: int, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    """fact 记录 → 其原始行链路"""
    model_map = {'sales': FactSalesTransaction}
    if fact_type not in model_map:
        raise HTTPException(status_code=400, detail=f"不支持的 fact 类型: {fact_type}")
    rec = db.query(model_map[fact_type]).get(fact_id)
    if not rec:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {
        'fact_type': fact_type, 'fact_id': rec.id,
        'raw_row_id': rec.raw_row_id,
        'biz_date': rec.biz_date.isoformat() if getattr(rec, 'biz_date', None) else None,
        'transaction_type': getattr(rec, 'transaction_type', None),
        'trace_url': f"/api/v2/trace/raw-row/{rec.raw_row_id}",
    }


@router.get("/snapshot-item/{item_id}")
def trace_snapshot_item(item_id: int, db: Session = Depends(get_db),
                        user=Depends(require_module("accounting"))):
    """快照明细 → 该 ASIN/SKU 在区间内的全部原始交易行（可逐条核对）"""
    item = db.query(DwsProfitSnapshotItem).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="快照明细不存在")
    snap = db.query(DwsProfitSnapshot).get(item.snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="快照不存在")
    q = db.query(FactSalesTransaction).filter(
        FactSalesTransaction.store_id == snap.store_id,
        FactSalesTransaction.asin == item.asin,
    )
    if snap.period_month:
        y, m = int(snap.period_month[:4]), int(snap.period_month[5:7])
        start = datetime(y, m, 1)
        end = datetime(y + (m == 12), (m % 12) + 1, 1)
        q = q.filter(FactSalesTransaction.biz_date >= start, FactSalesTransaction.biz_date < end)
    # 站点过滤：新版明细（country 非空）按明细行站点精确下钻；旧版按快照站点
    if item.country:
        q = q.filter(FactSalesTransaction.country == item.country)
    elif snap.country:
        q = q.filter(FactSalesTransaction.country == snap.country)
    # SKU 精确匹配（明细 sku 为归一后 SKU）
    if item.sku:
        q = q.filter(FactSalesTransaction.normalized_sku == item.sku)
    facts = q.limit(500).all()
    return {
        'item': {
            'asin': item.asin, 'sku': item.sku, 'country': item.country or '',
            'exchange_rate': float(item.exchange_rate) if item.exchange_rate is not None else None,
            'net_profit_rmb': float(item.net_profit_rmb or 0),
            'net_amazon': float(item.net_amazon or 0), 'quantity': item.quantity,
            'cost_status': item.cost_status, 'cost_inherited_from': item.cost_inherited_from,
            'source_batch_ids': item.source_batch_ids,
        },
        'snapshot': {
            'id': snap.id, 'store_id': snap.store_id, 'country': snap.country,
            'period_month': snap.period_month, 'version': snap.version,
            'exchange_rate': float(snap.exchange_rate or 0),
        },
        'source_facts': [{
            'fact_id': f.id, 'raw_row_id': f.raw_row_id,
            'biz_date': f.biz_date.isoformat() if f.biz_date else None,
            'type': f.transaction_type, 'quantity': f.quantity,
            'product_sales': float(f.product_sales or 0),
            'selling_fee': float(f.selling_fee or 0),
            'fba_fee': float(f.fba_fee or 0),
            'sku': f.sku, 'normalized_sku': f.normalized_sku,
            'trace_url': f"/api/v2/trace/raw-row/{f.raw_row_id}",
        } for f in facts],
        'fact_count': len(facts),
    }


@router.get("/search")
def search_raw(keyword: str = Query(..., min_length=1), limit: int = 50,
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    """全库原始行搜索（按订单号 / SKU / ASIN / 任意文本）"""
    like = f"%{keyword}%"
    rows = db.query(RawSheetRow).filter(
        cast(RawSheetRow.values_json, SAString).like(like)
    ).limit(limit).all()
    return {'keyword': keyword, 'count': len(rows), 'items': [{
        'raw_row_id': r.id, 'sheet_name': r.sheet_name, 'row_no': r.row_no,
        'biz_date': r.biz_date.isoformat() if r.biz_date else None,
        'batch_id': r.batch_id,
    } for r in rows]}
