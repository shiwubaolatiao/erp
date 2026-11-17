"""管理 · 商品与成本 — 商品主档 CRUD / 成本版本 CRUD / 站点 CRUD
（为兼容旧前端 Admin 页面的接口调用而补全；均按新模型 store_product + product_cost_history 实现）
"""
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import require_admin, get_current_user, require_module
from app.models import (
    Store, Site, StoreProduct, StoreSkuMapping, ProductCostHistory, OperationLog, SysUser,
    ExchangeRate,
)
from app.services.common.rates import upsert_rate_with_audit

router = APIRouter()


# ================= 站点（国家）CRUD =================
class SiteCreate(BaseModel):
    """站点新增请求"""
    site_code: str
    country_name: str
    default_currency: str = ""
    region: str = ""


class SiteUpdate(BaseModel):
    """站点更新请求（字段可选）"""
    country_name: str | None = None
    default_currency: str | None = None
    region: str | None = None


@router.post("/countries")
def create_site(req: SiteCreate, db: Session = Depends(get_db), user: SysUser = Depends(require_admin)):
    """新增站点/国家 — 管理员"""
    code = req.site_code.strip().upper()
    if db.query(Site).filter(Site.site_code == code).first():
        raise HTTPException(status_code=400, detail=f"站点 {code} 已存在")
    s = Site(site_code=code, country_name=req.country_name,
             default_currency=req.default_currency, region=req.region)
    db.add(s)
    db.add(OperationLog(user_id=user.id, username=user.username, action="create_site",
                        target=code, result="ok"))
    db.commit()
    return {"id": s.id, "site_code": s.site_code}


@router.put("/countries/{site_code}")
def update_site(site_code: str, req: SiteUpdate, db: Session = Depends(get_db),
                user: SysUser = Depends(require_admin)):
    """更新站点信息 — 管理员"""
    s = db.query(Site).filter(Site.site_code == site_code.upper()).first()
    if not s:
        raise HTTPException(status_code=404, detail="站点不存在")
    if req.country_name is not None:
        s.country_name = req.country_name
    if req.default_currency is not None:
        s.default_currency = req.default_currency
    if req.region is not None:
        s.region = req.region
    db.commit()
    return {"ok": True}


@router.delete("/countries/{site_code}")
def delete_site(site_code: str, db: Session = Depends(get_db), user: SysUser = Depends(require_admin)):
    """删除站点 — 管理员（站点为全局主数据，删除前确认无引用）"""
    s = db.query(Site).filter(Site.site_code == site_code.upper()).first()
    if not s:
        raise HTTPException(status_code=404, detail="站点不存在")
    db.delete(s)
    db.add(OperationLog(user_id=user.id, username=user.username, action="delete_site",
                        target=site_code, result="ok"))
    db.commit()
    return {"ok": True}


# ================= 汇率 更新/删除 =================
class RateUpdate(BaseModel):
    """汇率值更新请求"""
    rate_to_rmb: float


@router.put("/exchange-rates/{rate_id}")
def update_rate(rate_id: int, req: RateUpdate, db: Session = Depends(get_db),
                user: SysUser = Depends(require_admin)):
    """按 id 更新汇率值 — 管理员（统一走 upsert_rate_with_audit，history 留痕，组11.4）"""
    r = db.query(ExchangeRate).get(rate_id)
    if not r:
        raise HTTPException(status_code=404, detail="汇率记录不存在")
    result = upsert_rate_with_audit(db, r.store_id, r.country_code, r.period_month,
                                    req.rate_to_rmb, source='manual',
                                    operator_user_id=user.id)
    db.add(OperationLog(user_id=user.id, username=user.username, action="update_rate",
                        target=str(rate_id), result="ok",
                        detail=f"{r.country_code}/{r.period_month}: "
                               f"{result.old_rate} -> {result.new_rate}"))
    db.commit()
    return {"ok": True}


@router.delete("/exchange-rates/{rate_id}")
def delete_rate(rate_id: int, db: Session = Depends(get_db), user: SysUser = Depends(require_admin)):
    """按 id 删除汇率记录 — 管理员"""
    r = db.query(ExchangeRate).get(rate_id)
    if not r:
        raise HTTPException(status_code=404, detail="汇率记录不存在")
    db.delete(r)
    db.add(OperationLog(user_id=user.id, username=user.username, action="delete_rate",
                        target=str(rate_id), result="ok"))
    db.commit()
    return {"ok": True}


# ================= 商品主档 CRUD =================
class ProductCreate(BaseModel):
    """商品主档新增请求；给 sku 时同时建立三元组映射"""
    store: str
    asin: str
    title: str = ""
    brand: str = ""
    sku: str | None = None
    status: str = "normal"


class ProductUpdate(BaseModel):
    """商品主档更新请求（字段可选）"""
    title: str | None = None
    brand: str | None = None
    status: str | None = None


@router.post("/products")
def create_product(req: ProductCreate, db: Session = Depends(get_db),
                   user: SysUser = Depends(require_admin)):
    """新增商品主档（店铺内 ASIN 唯一）— 管理员"""
    st = db.query(Store).filter(Store.store_code == req.store.upper()).first()
    if not st:
        raise HTTPException(status_code=404, detail=f"店铺 {req.store} 不存在")
    asin = req.asin.strip()
    if not asin:
        raise HTTPException(status_code=400, detail="ASIN 不能为空")
    p = db.query(StoreProduct).filter(
        StoreProduct.store_id == st.id, StoreProduct.asin == asin).first()
    if not p:
        p = StoreProduct(store_id=st.id, asin=asin, title=req.title,
                         brand=req.brand, status=req.status)
        db.add(p)
        db.flush()
    else:
        if req.title:
            p.title = req.title
        if req.brand:
            p.brand = req.brand
    # 若给了 SKU，同时建立映射
    if req.sku:
        m = db.query(StoreSkuMapping).filter(
            StoreSkuMapping.store_id == st.id, StoreSkuMapping.sku == req.sku,
            StoreSkuMapping.asin == asin).first()
        if not m:
            db.add(StoreSkuMapping(store_id=st.id, sku=req.sku, asin=asin,
                                   store_product_id=p.id, status='normal',
                                   confirmed_by=user.id, confirmed_at=datetime.now()))
    db.add(OperationLog(user_id=user.id, username=user.username, action="create_product",
                        target=f"{st.store_code}/{asin}", result="ok"))
    db.commit()
    return {"id": p.id, "asin": p.asin}


@router.put("/products/{product_id}")
def update_product(product_id: int, req: ProductUpdate, db: Session = Depends(get_db),
                   user: SysUser = Depends(require_admin)):
    """更新商品主档（标题/品牌/状态）— 管理员"""
    p = db.query(StoreProduct).get(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="商品不存在")
    if req.title is not None:
        p.title = req.title
    if req.brand is not None:
        p.brand = req.brand
    if req.status is not None:
        p.status = req.status
    db.commit()
    return {"ok": True}


@router.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db),
                   user: SysUser = Depends(require_admin)):
    """删除商品主档及其映射 — 管理员"""
    p = db.query(StoreProduct).get(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="商品不存在")
    db.query(StoreSkuMapping).filter(StoreSkuMapping.store_product_id == p.id).delete()
    db.delete(p)
    db.add(OperationLog(user_id=user.id, username=user.username, action="delete_product",
                        target=p.asin, result="ok"))
    db.commit()
    return {"ok": True}


# ================= 成本版本 CRUD =================
def _resolve_product(db: Session, product_id: int):
    """取商品主档与其所属店铺（不存在则 404）"""
    p = db.query(StoreProduct).get(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="商品不存在")
    st = db.query(Store).get(p.store_id)
    return p, st


@router.get("/products/{product_id}/costs")
def list_product_costs(product_id: int, db: Session = Depends(get_db),
                       user: SysUser = Depends(require_module("admin"))):
    """成本版本列表（按月份倒序）"""
    p, st = _resolve_product(db, product_id)
    if not st:
        raise HTTPException(status_code=404, detail="店铺不存在")
    # 该 ASIN 下所有 SKU 的成本版本
    skus = [m.sku for m in db.query(StoreSkuMapping).filter(
        StoreSkuMapping.store_id == st.id, StoreSkuMapping.asin == p.asin).all()]
    q = db.query(ProductCostHistory).filter(
        ProductCostHistory.store_id == st.id,
        ProductCostHistory.asin == p.asin)
    if skus:
        q = q.filter(ProductCostHistory.sku.in_(skus))
    rows = q.order_by(ProductCostHistory.period.desc()).limit(500).all()
    # ★ 返回裸数组：旧前端 Admin 页 `setCosts(res.data || [])` 后直接作为表格数据源
    return [{
        "id": r.id, "asin": r.asin, "sku": r.sku, "period": r.period,
        "cost_rmb": float(r.cost_rmb) if r.cost_rmb is not None else None,
        "freight_json": r.freight_json, "source_type": r.source_type,
        "source_batch_id": r.source_batch_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


class CostCreate(BaseModel):
    """成本新增请求：成本与运费至少填一项，写入 manual 版本"""
    sku: str = ""
    period: str
    cost_rmb: float | None = None
    freight: float | None = None


@router.post("/products/{product_id}/costs")
def create_product_cost(product_id: int, req: CostCreate, db: Session = Depends(get_db),
                        user: SysUser = Depends(require_admin)):
    """新增成本版本（来源=人工补录）— 管理员"""
    p, st = _resolve_product(db, product_id)
    if not st:
        raise HTTPException(status_code=404, detail="店铺不存在")
    if req.cost_rmb is None and req.freight is None:
        raise HTTPException(status_code=400, detail="成本与运费至少填一项")
    sku = req.sku.strip()
    if not sku:
        m = db.query(StoreSkuMapping).filter(
            StoreSkuMapping.store_id == st.id, StoreSkuMapping.asin == p.asin).first()
        sku = m.sku if m else ''
    existing = db.query(ProductCostHistory).filter(
        ProductCostHistory.store_id == st.id, ProductCostHistory.asin == p.asin,
        ProductCostHistory.sku == sku, ProductCostHistory.period == req.period,
        ProductCostHistory.source_type == 'manual',
    ).first()
    freight_json = {"default": req.freight} if req.freight is not None else None
    if existing:
        if req.cost_rmb is not None:
            existing.cost_rmb = req.cost_rmb
        if freight_json is not None:
            existing.freight_json = freight_json
        cost_id = existing.id
    else:
        rec = ProductCostHistory(store_id=st.id, asin=p.asin, sku=sku, period=req.period,
                                 cost_rmb=req.cost_rmb, freight_json=freight_json,
                                 source_type='manual', created_by=user.id)
        db.add(rec)
        db.flush()
        cost_id = rec.id
    db.add(OperationLog(user_id=user.id, username=user.username, action="create_cost",
                        target=f"{st.store_code}/{p.asin}/{sku}/{req.period}", result="ok"))
    db.commit()
    return {"id": cost_id}


class CostUpdate(BaseModel):
    """成本更新请求"""
    cost_rmb: float | None = None
    freight: float | None = None


@router.put("/products/{product_id}/costs/{cost_id}")
def update_product_cost(product_id: int, cost_id: int, req: CostUpdate,
                        db: Session = Depends(get_db), user: SysUser = Depends(require_admin)):
    """更新成本版本并标记来源=manual — 管理员"""
    _resolve_product(db, product_id)
    r = db.query(ProductCostHistory).get(cost_id)
    if not r:
        raise HTTPException(status_code=404, detail="成本记录不存在")
    if req.cost_rmb is not None:
        r.cost_rmb = req.cost_rmb
    if req.freight is not None:
        r.freight_json = {"default": req.freight}
    r.source_type = 'manual'
    db.add(OperationLog(user_id=user.id, username=user.username, action="update_cost",
                        target=str(cost_id), result="ok"))
    db.commit()
    return {"ok": True}


@router.delete("/products/{product_id}/costs/{cost_id}")
def delete_product_cost(product_id: int, cost_id: int, db: Session = Depends(get_db),
                        user: SysUser = Depends(require_admin)):
    """删除成本版本 — 管理员"""
    _resolve_product(db, product_id)
    r = db.query(ProductCostHistory).get(cost_id)
    if not r:
        raise HTTPException(status_code=404, detail="成本记录不存在")
    db.delete(r)
    db.add(OperationLog(user_id=user.id, username=user.username, action="delete_cost",
                        target=str(cost_id), result="ok"))
    db.commit()
    return {"ok": True}
