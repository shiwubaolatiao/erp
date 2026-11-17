"""导出 — Excel 导出（快照/销售明细/原始溯源），受字段级权限与二次确认约束"""
import io
import json
from datetime import datetime
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from openpyxl import Workbook
from app.core.database import get_db
from app.core.security import get_current_user, require_module, filter_store_by_permission
from app.models import (
    Store, FactSalesTransaction, DwsProfitSnapshot, DwsProfitSnapshotItem,
    RawSheetRow, OperationLog, SysUser, ProductCostHistory,
)

router = APIRouter()

PROFIT_TYPES = ('Order', 'Refund')


def _xlsx_response(wb: Workbook, filename: str) -> StreamingResponse:
    """把 openpyxl 工作簿包成带中文文件名的下载响应（RFC 5987 百分号编码）"""
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _month_bounds(pm: str):
    """YYYY-MM → [月初, 次月初) 的半开区间（非法格式返回 400 而非 500）"""
    try:
        y, m = int(pm[:4]), int(pm[5:7])
        if not (1 <= m <= 12) or len(pm) != 7:
            raise ValueError(pm)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="period_month 格式应为 YYYY-MM")
    return datetime(y, m, 1), datetime(y + (m == 12), (m % 12) + 1, 1)


@router.get("/snapshot/{snapshot_id}")
def export_snapshot(snapshot_id: int, confirm: bool = Query(False, description="敏感操作二次确认"),
                    db: Session = Depends(get_db),
                    user: SysUser = Depends(require_module("export"))):
    """导出核算快照（含成本利润明细）— 敏感操作需 confirm=true"""
    if not confirm:
        raise HTTPException(status_code=400, detail="导出利润数据为敏感操作，需 confirm=true 二次确认")
    snap = db.query(DwsProfitSnapshot).get(snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="快照不存在")
    store = db.query(Store).get(snap.store_id)
    items = db.query(DwsProfitSnapshotItem).filter(
        DwsProfitSnapshotItem.snapshot_id == snapshot_id).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "核算汇总"
    ws.append(["店铺", store.store_code if store else "", "站点", snap.country or "全部"])
    ws.append(["口径", snap.period_month or f"{snap.date_from} ~ {snap.date_to}"])
    ws.append(["版本", snap.version, "汇率", float(snap.exchange_rate or 0)])
    # rate_detail 为逐国汇率 dict（B-01 后结构化）——Excel 单元格只收标量，序列化为 JSON
    rate_detail_cell = snap.rate_detail or ""
    if isinstance(rate_detail_cell, dict):
        rate_detail_cell = json.dumps(rate_detail_cell, ensure_ascii=False)
    ws.append(["汇率来源", snap.rate_source or "", "汇率明细", rate_detail_cell])
    ws.append([])
    ws.append(["指标", "数值"])
    for label, val in [
        ("net_amazon(原币)", float(snap.net_amazon or 0)),
        ("销售额(RMB)", float(snap.product_sales_rmb or 0)),
        ("采购成本(RMB)", float(snap.product_cost_rmb or 0)),
        ("头程运费(RMB)", float(snap.freight_cost_rmb or 0)),
        ("广告费(原币)", float(snap.ad_spend or 0)),
        ("仓储费(原币)", float(snap.storage_fee or 0)),
        ("超龄仓储费(原币)", float(snap.aged_storage_fee or 0)),
        ("退货处理费(原币)", float(snap.returns_fee or 0)),
        ("入库配置费(原币)", float(snap.inbound_fee or 0)),
        ("移除费(原币)", float(snap.removal_fee or 0)),
        ("净利润(RMB)", float(snap.net_profit_rmb or 0)),
        ("净利率", float(snap.net_profit_rate or 0)),
        ("订单数", snap.order_count), ("净销量", snap.order_qty),
        ("成本完整性", "完整" if snap.cost_complete else f"不完整（{snap.cost_incomplete_skus} SKU 缺成本）"),
    ]:
        ws.append([label, val])

    ws2 = wb.create_sheet("明细")
    ws2.append(["ASIN", "SKU", "站点", "汇率", "net_amazon", "净销量", "采购成本RMB", "运费RMB",
                "广告费", "仓储费", "超龄仓储费", "退货费", "入库费", "移除费",
                "净利润RMB", "成本状态", "沿用自"])
    for it in items:
        ws2.append([
            it.asin, it.sku, it.country or '',
            float(it.exchange_rate) if it.exchange_rate is not None else '',
            float(it.net_amazon or 0), it.quantity,
            float(it.product_cost_rmb or 0), float(it.freight_cost_rmb or 0),
            float(it.ad_spend or 0), float(it.storage_fee or 0),
            float(it.aged_storage_fee or 0), float(it.returns_fee or 0),
            float(it.inbound_fee or 0), float(it.removal_fee or 0),
            float(it.net_profit_rmb or 0), it.cost_status, it.cost_inherited_from,
        ])
    db.add(OperationLog(user_id=user.id, username=user.username, action="export_snapshot",
                        target=str(snapshot_id), result="ok"))
    db.commit()
    fname = f"核算快照_{store.store_code if store else ''}_{snap.period_month or 'range'}_v{snap.version}.xlsx"
    return _xlsx_response(wb, fname)


@router.get("/sales")
def export_sales(store: str, period_month: str, country: str | None = None, confirm: bool = Query(False),
                 db: Session = Depends(get_db),
                 user: SysUser = Depends(require_module("export"))):
    """导出销售明细（含 raw_row_id 溯源列）"""
    if not confirm:
        raise HTTPException(status_code=400, detail="导出需 confirm=true 二次确认")
    store_obj = filter_store_by_permission(user, store, db)
    if not store_obj:
        raise HTTPException(status_code=404, detail="店铺不存在")
    start, end = _month_bounds(period_month)
    q = db.query(FactSalesTransaction).filter(
        FactSalesTransaction.store_id == store_obj.id,
        FactSalesTransaction.biz_date >= start, FactSalesTransaction.biz_date < end,
    )
    if country:
        q = q.filter(FactSalesTransaction.country == country)
    rows = q.order_by(FactSalesTransaction.biz_date).limit(100000).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "销售明细"
    ws.append(["日期", "类型", "站点", "订单号", "SKU", "归一SKU", "ASIN", "数量",
               "商品销售额", "销售税", "运费收入", "运费税", "礼品包装", "礼品税",
               "促销返点", "返点税", "代扣税", "佣金", "FBA费", "其他", "合计",
               "币种", "原始行ID(溯源)"])
    for r in rows:
        ws.append([
            r.biz_date.strftime('%Y-%m-%d %H:%M:%S') if r.biz_date else '',
            r.transaction_type, r.country, r.order_id, r.sku, r.normalized_sku, r.asin,
            r.quantity, float(r.product_sales or 0), float(r.product_sales_tax or 0),
            float(r.postage_credits or 0), float(r.shipping_credits_tax or 0),
            float(r.gift_wrap_credits or 0), float(r.giftwrap_credits_tax or 0),
            float(r.promo_rebate or 0), float(r.promo_rebate_tax or 0),
            float(r.marketplace_withheld_tax or 0), float(r.selling_fee or 0),
            float(r.fba_fee or 0), float(r.other_amount or 0), float(r.line_total or 0),
            r.currency, r.raw_row_id,
        ])
    db.add(OperationLog(user_id=user.id, username=user.username, action="export_sales",
                        target=f"{store}/{period_month}", result="ok"))
    db.commit()
    return _xlsx_response(wb, f"销售明细_{store}_{period_month}.xlsx")


@router.get("/raw-rows")
def export_raw_rows(batch_id: int, confirm: bool = Query(False),
                    db: Session = Depends(get_db),
                    user: SysUser = Depends(require_module("export"))):
    """导出某批次的原始行（全字段还原，供人工核对）"""
    if not confirm:
        raise HTTPException(status_code=400, detail="导出需 confirm=true 二次确认")
    rows = db.query(RawSheetRow).filter(RawSheetRow.batch_id == batch_id).limit(200000).all()
    if not rows:
        raise HTTPException(status_code=404, detail="该批次无原始行")
    wb = Workbook()
    ws = wb.active
    ws.title = "原始行"
    headers = rows[0].headers_json or []
    ws.append(["sheet", "行号", "业务日期"] + [str(h) for h in headers])
    for r in rows:
        vals = r.values_json or []
        ws.append([r.sheet_name, r.row_no,
                   r.biz_date.strftime('%Y-%m-%d %H:%M:%S') if r.biz_date else '']
                  + [vals[i] if i < len(vals) else '' for i in range(len(headers))])
    db.add(OperationLog(user_id=user.id, username=user.username, action="export_raw_rows",
                        target=str(batch_id), result="ok"))
    db.commit()
    return _xlsx_response(wb, f"原始行_批次{batch_id}.xlsx")


@router.get("/transfer")
def export_transfer(store: str | None = None, period_month: str | None = None,
                    confirm: bool = Query(False),
                    db: Session = Depends(get_db),
                    user: SysUser = Depends(require_module("export"))):
    """导出资金流（Transfer 提款，不计入利润）"""
    from app.models import FactCashflowTransfer
    if not confirm:
        raise HTTPException(status_code=400, detail="导出需 confirm=true 二次确认")
    q = db.query(FactCashflowTransfer)
    if store:
        s = db.query(Store).filter(Store.store_code == store).first()
        if not s:
            raise HTTPException(status_code=404, detail="店铺不存在")
        q = q.filter(FactCashflowTransfer.store_id == s.id)
    if period_month:
        start, end = _month_bounds(period_month)
        q = q.filter(FactCashflowTransfer.biz_date >= start, FactCashflowTransfer.biz_date < end)
    rows = q.order_by(FactCashflowTransfer.biz_date).limit(100000).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    wb = Workbook()
    ws = wb.active
    ws.title = "资金流提款"
    ws.append(["店铺", "站点", "业务日期", "结算单号", "金额(原币)", "币种", "说明"])
    total = 0.0
    for r in rows:
        amt = float(r.amount or 0)
        total += amt
        ws.append([stores.get(r.store_id, ''), r.country or '',
                   r.biz_date.strftime('%Y-%m-%d %H:%M:%S') if r.biz_date else '',
                   r.settlement_id or '', amt, r.currency or '', r.description or ''])
    ws.append([])
    ws.append(["合计", "", "", "", round(total, 2)])
    db.add(OperationLog(user_id=user.id, username=user.username, action="export_transfer",
                        target=f"{store or 'all'}/{period_month or 'all'}", result="ok"))
    db.commit()
    return _xlsx_response(wb, f"资金流_提款_{store or 'all'}_{period_month or 'all'}.xlsx")


@router.get("/missing-skus")
def export_missing_skus(store: str | None = None, period_month: str | None = None,
                        confirm: bool = Query(False),
                        db: Session = Depends(get_db),
                        user: SysUser = Depends(require_module("export"))):
    """导出成本缺失 SKU 清单（旧接口名兼容）"""
    from app.models import CostMissingAlert
    if not confirm:
        raise HTTPException(status_code=400, detail="导出需 confirm=true 二次确认")
    q = db.query(CostMissingAlert).filter(CostMissingAlert.status == 'pending')
    if store:
        s = db.query(Store).filter(Store.store_code == store).first()
        if s:
            q = q.filter(CostMissingAlert.store_id == s.id)
    if period_month:
        q = q.filter(CostMissingAlert.period == period_month)
    rows = q.limit(100000).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    wb = Workbook()
    ws = wb.active
    ws.title = "成本缺失清单"
    ws.append(["店铺", "ASIN", "SKU", "站点", "月份", "缺失项", "状态"])
    for r in rows:
        ws.append([stores.get(r.store_id, ''), r.asin, r.sku, r.country or '',
                   r.period, r.missing_type, r.status])
    return _xlsx_response(wb, f"成本缺失清单_{store or 'all'}_{period_month or 'all'}.xlsx")


@router.get("/costs")
def export_costs(store: str | None = None, period_month: str | None = None,
                 db: Session = Depends(get_db),
                 user: SysUser = Depends(require_module("export"))):
    """导出成本版本表"""
    q = db.query(ProductCostHistory)
    if store:
        s = db.query(Store).filter(Store.store_code == store).first()
        if s:
            q = q.filter(ProductCostHistory.store_id == s.id)
    if period_month:
        q = q.filter(ProductCostHistory.period == period_month)
    rows = q.limit(200000).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    wb = Workbook()
    ws = wb.active
    ws.title = "成本版本"
    ws.append(["店铺", "ASIN", "SKU", "月份", "成本RMB", "运费JSON", "来源", "来源批次"])
    for r in rows:
        ws.append([stores.get(r.store_id, ''), r.asin, r.sku, r.period,
                   float(r.cost_rmb) if r.cost_rmb is not None else None,
                   str(r.freight_json or ''), r.source_type, r.source_batch_id])
    return _xlsx_response(wb, f"成本版本_{store or 'all'}_{period_month or 'all'}.xlsx")
