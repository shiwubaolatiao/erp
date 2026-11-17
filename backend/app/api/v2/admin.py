"""管理 — 汇率 / 别名映射 / 表头词典 / 店铺站点 / 审计日志"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import require_admin, get_current_user, require_module
from app.models import (
    ExchangeRate, SheetAliasMap, HeaderAlias, Store, Site, OperationLog,
    StoreProduct, StoreSkuMapping, SkuReplacement, CostMissingAlert,
)
from app.services.common.rates import upsert_rate_with_audit
from app.services.accounting.recalc import recalc_affected, verify_snapshots

router = APIRouter()


# ================= 汇率 =================
class RateUpsert(BaseModel):
    """汇率新增/更新请求

    兼容旧前端 Admin 页的传参方式：
      · 国家可用 `country_code`（新，如 'UK'）或 `country_id`（旧，站点主键 id）
      · 月份可用 `period_month`（新，'2026-06'）或 `year_month`（旧）
      · 汇率值可用 `rate_to_rmb`（新）或 `rate`（旧）
      · store 为空表示全局汇率
    """
    store: str | None = None
    country_code: str | None = None
    country_id: int | None = None
    period_month: str | None = None
    year_month: str | None = None
    rate_to_rmb: float | None = None
    rate: float | None = None


@router.get("/exchange-rates")
def list_rates(store: str | None = None, country_code: str | None = None,
               period_month: str | None = None, year_month: str | None = None,
               db: Session = Depends(get_db), user=Depends(require_module("admin"))):
    """汇率列表

    ★ 返回**裸数组**：旧前端 Admin 页 `setRates(r.data || [])` 后按数组过滤。
      每行同时给出新字段（rate_to_rmb/period_month/store_code）与旧字段（rate/year_month/store）。
    """
    q = db.query(ExchangeRate)
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    if store:
        s = db.query(Store).filter(Store.store_code == store).first()
        if not s:
            raise HTTPException(status_code=404, detail="店铺不存在")
        q = q.filter(ExchangeRate.store_id == s.id)
    pm = period_month or year_month
    if pm:
        q = q.filter(ExchangeRate.period_month == pm)
    if country_code:
        q = q.filter(ExchangeRate.country_code == country_code)
    rows = q.order_by(ExchangeRate.period_month.desc()).limit(2000).all()
    return [{
        'id': r.id,
        'store_code': stores.get(r.store_id, '') if r.store_id else '全局',
        'country_code': r.country_code, 'period_month': r.period_month,
        'rate_to_rmb': float(r.rate_to_rmb or 0), 'source': r.source,
        # 旧前端兼容别名
        'store': stores.get(r.store_id, '') if r.store_id else '',
        'year_month': r.period_month,
        'rate': float(r.rate_to_rmb or 0),
    } for r in rows]


@router.post("/exchange-rates")
def upsert_rate(req: RateUpsert, db: Session = Depends(get_db),
                user=Depends(require_admin)):
    """新增或更新汇率（同 店铺+国家+月份 覆盖）— 管理员

    ★ 兼容旧前端：country_id → country_code；year_month → period_month；rate → rate_to_rmb
    """
    store_obj = None
    if req.store:
        store_obj = db.query(Store).filter(Store.store_code == req.store).first()
        if not store_obj:
            raise HTTPException(status_code=404, detail="店铺不存在")
    # 国家：优先 code，其次用 id 反查站点代码
    code = req.country_code
    if not code and req.country_id is not None:
        site = db.query(Site).get(req.country_id)
        if not site:
            raise HTTPException(status_code=404, detail=f"站点 id={req.country_id} 不存在")
        code = site.site_code
    if not code:
        raise HTTPException(status_code=400, detail="必须提供 country_code 或 country_id")
    pm = req.period_month or req.year_month
    if not pm:
        raise HTTPException(status_code=400, detail="必须提供 period_month 或 year_month")
    rate_val = req.rate_to_rmb if req.rate_to_rmb is not None else req.rate
    if rate_val is None:
        raise HTTPException(status_code=400, detail="必须提供 rate_to_rmb 或 rate")

    sid = store_obj.id if store_obj else None
    # B-08：人工汇率同样走统一写入入口（history 留痕 old→new；操作人入 updated_by）
    result = upsert_rate_with_audit(db, sid, code, pm, rate_val, source='manual',
                                    operator_user_id=user.id)
    db.add(OperationLog(user_id=user.id, username=user.username, action="upsert_rate",
                        target=f"{req.store or '全局'}/{code}/{pm}",
                        result="ok",
                        detail=f"rate={rate_val}"
                               + (f", old={float(result.old_rate)}" if result.old_rate is not None else '')))
    db.commit()
    return {'ok': True, 'conflict': result.conflict,
            'rate': float(result.new_rate) if result.new_rate is not None else None}


# ================= sheet 别名 =================
class SheetAliasUpsert(BaseModel):
    """sheet 别名新增请求：report_type → 店铺实际 sheet 名"""
    report_type: str
    alias_name: str
    country_hint: str = ""
    store: str | None = None


@router.get("/sheet-aliases")
def list_sheet_aliases(db: Session = Depends(get_db), user=Depends(require_module("admin"))):
    """sheet 别名词典列表（解析器按此识别各店铺 sheet）"""
    rows = db.query(SheetAliasMap).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    return {'items': [{
        'id': r.id, 'report_type': r.report_type, 'alias_name': r.alias_name,
        'country_hint': r.country_hint,
        'store_code': stores.get(r.store_id, '') if r.store_id else '全局',
    } for r in rows]}


@router.post("/sheet-aliases")
def upsert_sheet_alias(req: SheetAliasUpsert, db: Session = Depends(get_db),
                       user=Depends(require_admin)):
    """新增/更新 sheet 别名 — 管理员；新增后无需改代码即生效"""
    store_obj = None
    if req.store:
        store_obj = db.query(Store).filter(Store.store_code == req.store).first()
    sid = store_obj.id if store_obj else None
    existing = db.query(SheetAliasMap).filter(
        SheetAliasMap.report_type == req.report_type,
        SheetAliasMap.alias_name == req.alias_name,
        SheetAliasMap.store_id == sid,
    ).first()
    if existing:
        existing.country_hint = req.country_hint
    else:
        db.add(SheetAliasMap(report_type=req.report_type, alias_name=req.alias_name,
                             country_hint=req.country_hint, store_id=sid))
    db.commit()
    return {'ok': True}


@router.delete("/sheet-aliases/{alias_id}")
def delete_sheet_alias(alias_id: int, db: Session = Depends(get_db),
                       user=Depends(require_admin)):
    """删除 sheet 别名 — 管理员"""
    r = db.query(SheetAliasMap).get(alias_id)
    if not r:
        raise HTTPException(status_code=404, detail="别名不存在")
    db.delete(r)
    db.commit()
    return {'ok': True}


# ================= 表头词典 =================
class HeaderAliasUpsert(BaseModel):
    """表头别名新增请求：canonical_field ← 各语言实际表头名"""
    canonical_field: str
    alias: str
    lang: str = ""
    report_type: str = ""


@router.get("/header-aliases")
def list_header_aliases(db: Session = Depends(get_db), user=Depends(require_module("admin"))):
    """多语言表头词典列表"""
    rows = db.query(HeaderAlias).limit(2000).all()
    return {'items': [{
        'id': r.id, 'canonical_field': r.canonical_field, 'alias': r.alias,
        'lang': r.lang, 'report_type': r.report_type,
    } for r in rows]}


@router.post("/header-aliases")
def upsert_header_alias(req: HeaderAliasUpsert, db: Session = Depends(get_db),
                        user=Depends(require_admin)):
    """新增/更新表头别名 — 管理员（应对报表表头变更，无需改代码）"""
    existing = db.query(HeaderAlias).filter(
        HeaderAlias.canonical_field == req.canonical_field,
        HeaderAlias.alias == req.alias,
        HeaderAlias.report_type == req.report_type,
    ).first()
    if existing:
        existing.lang = req.lang
    else:
        db.add(HeaderAlias(canonical_field=req.canonical_field, alias=req.alias,
                           lang=req.lang, report_type=req.report_type))
    db.commit()
    return {'ok': True}


@router.delete("/header-aliases/{alias_id}")
def delete_header_alias(alias_id: int, db: Session = Depends(get_db),
                        user=Depends(require_admin)):
    """删除表头别名 — 管理员"""
    r = db.query(HeaderAlias).get(alias_id)
    if not r:
        raise HTTPException(status_code=404, detail="别名不存在")
    db.delete(r)
    db.commit()
    return {'ok': True}


# ================= 商品主档 / 映射审核 =================
@router.get("/mappings/pending")
def pending_mappings(store: str | None = None, db: Session = Depends(get_db),
                     user=Depends(require_module("admin"))):
    """待确认区 + 异常池"""
    q = db.query(StoreSkuMapping).filter(StoreSkuMapping.status.in_(['pending', 'abnormal']))
    if store:
        s = db.query(Store).filter(Store.store_code == store).first()
        if s:
            q = q.filter(StoreSkuMapping.store_id == s.id)
    rows = q.limit(500).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    return {'items': [{
        'id': m.id, 'store_code': stores.get(m.store_id, ''),
        'sku': m.sku, 'asin': m.asin, 'status': m.status, 'note': m.note,
        'first_seen_at': m.first_seen_at.isoformat() if m.first_seen_at else None,
    } for m in rows]}


class MappingDecision(BaseModel):
    """映射审核决定：action = confirm（转正式）/ reject（驳回）"""
    action: str  # confirm / reject


@router.post("/mappings/{mapping_id}/decide")
def decide_mapping(mapping_id: int, req: MappingDecision, db: Session = Depends(get_db),
                   user=Depends(require_admin)):
    """人工审核：确认转正式（normal）/ 驳回（保留留痕）

    确认时校验：该 (store, sku) 是否已有 normal 映射（SKU 不允许跨 ASIN 漂移）
    """
    m = db.query(StoreSkuMapping).get(mapping_id)
    if not m:
        raise HTTPException(status_code=404, detail="映射不存在")
    if req.action == 'confirm':
        conflict = db.query(StoreSkuMapping).filter(
            StoreSkuMapping.store_id == m.store_id,
            StoreSkuMapping.sku == m.sku,
            StoreSkuMapping.status == 'normal',
            StoreSkuMapping.id != m.id,
        ).first()
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"该 SKU 已绑定 ASIN {conflict.asin}（SKU 不允许跨 ASIN 漂移），"
                       f"请先处理既有映射")
        m.status = 'normal'
        m.confirmed_by = user.id
        m.confirmed_at = datetime.now()
        # 同步主档状态
        if m.store_product_id:
            p = db.query(StoreProduct).get(m.store_product_id)
            if p and p.status == 'pending':
                p.status = 'normal'
    else:
        m.status = 'rejected'
        m.confirmed_by = user.id
        m.confirmed_at = datetime.now()
        m.note = (m.note or '') + f" | 已驳回 by {user.username}"
    db.add(OperationLog(user_id=user.id, username=user.username,
                        action=f"mapping_{req.action}", target=f"{m.sku}/{m.asin}", result="ok"))
    db.commit()
    return {'ok': True, 'status': m.status}


@router.get("/replacements")
def list_replacements(db: Session = Depends(get_db), user=Depends(require_module("admin"))):
    """amzn.gr.* 替换件归并记录"""
    rows = db.query(SkuReplacement).limit(1000).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    return {'items': [{
        'id': r.id, 'store_code': stores.get(r.store_id, ''),
        'replacement_sku': r.replacement_sku, 'parent_sku': r.parent_sku,
    } for r in rows]}


# ================= 审计日志 =================
@router.get("/operation-logs")
def operation_logs(limit: int = 200, action: str | None = None,
                   db: Session = Depends(get_db), user=Depends(require_admin)):
    """审计日志（登录/导入/锁结/解锁/导出/授权等敏感操作留痕）"""
    q = db.query(OperationLog)
    if action:
        q = q.filter(OperationLog.action == action)
    rows = q.order_by(OperationLog.created_at.desc()).limit(limit).all()
    return {'items': [{
        'id': r.id, 'username': r.username, 'action': r.action, 'target': r.target,
        'result': r.result, 'detail': r.detail,
        'created_at': r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}


# ================= 存量快照重算（tasks.md 5.1，B-01 修复回填） =================
class RecalcRequest(BaseModel):
    """存量快照重算请求：对受 B-01 影响的旧版店铺级快照（country='' 且 rate_source 为空）逐月重算

    M1 阶段同步执行（受影响月份有限）；组 6 任务执行器上线后改走异步任务。
    """
    store: str | None = None                 # 店铺代码（空=全部店铺）
    period_months: list[str] | None = None   # 限定月份 YYYY-MM（空=全部受影响月份）
    dry_run: bool = False                    # 只列受影响快照不重算


@router.post("/snapshots/recalculate")
def recalc_legacy_snapshots(req: RecalcRequest, db: Session = Depends(get_db),
                            user=Depends(require_admin)):
    """存量店铺级快照批量重算 — 管理员

    逐月调用修复版 calculate_snapshot 生成新 version（旧版本永久保留可追溯）；
    逐月独立事务，单月失败回滚该月并继续其余月份；返回重算报告。
    """
    if req.store and not db.query(Store).filter(Store.store_code == req.store).first():
        raise HTTPException(status_code=404, detail="店铺不存在")
    report = recalc_affected(db, stores=[req.store] if req.store else None,
                             months=req.period_months, dry_run=req.dry_run)
    db.add(OperationLog(user_id=user.id, username=user.username, action="snapshot_recalc",
                        target=req.store or "全部店铺",
                        result="ok" if report['ok'] else "partial",
                        detail=f"affected={report['affected']} success={report['success']}"
                               f" failed={report['failed']} dry_run={req.dry_run}"))
    db.commit()
    return report


@router.get("/snapshots/verify")
def verify_legacy_snapshots(store: str | None = None, db: Session = Depends(get_db),
                            user=Depends(require_admin)):
    """全量校验：各店各月最新店铺级快照 == Σ 同期各站点级快照（差异 ≤ 0.01）— 管理员

    仅校验店铺级为新版口径（rate_source 非空）且有站点级快照的组合，其余记 skip。
    """
    return verify_snapshots(db, stores=[store] if store else None)


# ================= 数据维护 =================
class GovernanceRequest(BaseModel):
    """月份格式治理请求（组 10.2）— dry_run 默认开，建议先预览再执行"""
    dry_run: bool = True
    reconcile: bool = False  # 执行前后跑 acceptance 三方对账（全量核算，耗时数分钟）


@router.post("/governance/month-format")
def governance_month_format(req: GovernanceRequest, db: Session = Depends(get_db),
                            user=Depends(require_admin)):
    """费用月份格式治理（组 10.2 / D-03）— 管理员

    幂等清洗 fact_fee_storage/fact_fee_return.month_of_charge 与
    fact_fee_aged_storage.snapshot_date 的带时间戳存量值 → YYYY-MM。
    GovernanceResult 契约：dry_run / tables[{table_name, column, dirty_rows,
    cleaned_rows}] / consistent / reconciliation；与 CLI（tools/govern_month_format.py）
    行为一致（核心同为 app/services/governance.py）。
    """
    from pathlib import Path as _Path
    from app.core.config import settings
    from app.services.governance import run_governance

    reconcile_fn = None
    if req.reconcile and not req.dry_run:
        def reconcile_fn():
            import re as _re
            import subprocess as _sp
            import sys as _sys
            acc = _Path(__file__).resolve().parents[3] / "tools" / "acceptance.py"
            r = _sp.run([_sys.executable, str(acc)], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=3600)
            out = r.stdout or ""
            m = _re.search(r"独立复算对账:\s*(\d+) 个检查点，不一致 (\d+)", out)
            s = _re.search(r"核算快照一致性: 成功 (\d+)，失败 (\d+)", out)
            return {"ok": r.returncode == 0,
                    "checks": int(m.group(1)) if m else None,
                    "mismatches": int(m.group(2)) if m else None,
                    "snapshots_failed": int(s.group(2)) if s else None}

    result = run_governance(
        db, dry_run=req.dry_run,
        backup_dir=None if req.dry_run else _Path(settings.ARCHIVE_DIR) / "governance",
        reconcile_fn=reconcile_fn)

    from app.models import OperationLog
    total_dirty = sum(t["dirty_rows"] for t in result["tables"])
    total_cleaned = sum(t["cleaned_rows"] for t in result["tables"])
    db.add(OperationLog(
        user_id=user.id, username=user.username,
        action="governance.month_format",
        target=f"fee-month-format dry_run={req.dry_run}",
        detail=f"dirty={total_dirty} cleaned={total_cleaned} "
               f"consistent={result['consistent']} aborted={result['aborted']}"))
    db.commit()
    return result


@router.get("/stats")
def stats(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """数据概览（首页/诊断用）"""
    from sqlalchemy import func as sa_func
    from app.models import (ImportBatch, RawSheetRow, FactSalesTransaction, FactAdDaily,
                            FactFeeStorage, FactFeeReturn, FactFeeRemoval,
                            FactFeeInboundConfig, FactFeeAgedStorage, ProductCostHistory)
    def c(model):
        """统计单表行数（数据概览用）"""
        return db.query(sa_func.count(model.id)).scalar() or 0
    return {
        'stores': c(Store), 'sites': c(Site),
        'batches': c(ImportBatch), 'raw_rows': c(RawSheetRow),
        'sales_transactions': c(FactSalesTransaction), 'ad_rows': c(FactAdDaily),
        'storage_fees': c(FactFeeStorage), 'returns_fees': c(FactFeeReturn),
        'removal_fees': c(FactFeeRemoval), 'inbound_fees': c(FactFeeInboundConfig),
        'aged_storage_fees': c(FactFeeAgedStorage),
        'cost_records': c(ProductCostHistory),
        'products': c(StoreProduct), 'mappings': c(StoreSkuMapping),
        'pending_alerts': db.query(sa_func.count(CostMissingAlert.id)).filter(
            CostMissingAlert.status == 'pending').scalar() or 0,
    }
