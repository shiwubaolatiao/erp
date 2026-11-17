"""店铺 / 站点 接口

接口前缀: /api/v2/stores
权限：列表读取按用户数据权限过滤；增删改需管理员。

★ 数据权限要点：列表接口用 get_authorized_stores() 而不是 filter_store_by_permission()，
  因为后者在"不指定店铺"时返回 None（语义是"无过滤条件"），
  直接当列表用会导致非管理员看到全部店铺（这是曾经踩过的坑）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import (
    get_current_user, require_admin, filter_store_by_permission, get_authorized_stores,
)
from app.models import SysUser, Store, Site

router = APIRouter()


class StoreCreate(BaseModel):
    """店铺新增请求：store_code 唯一"""
    store_code: str
    store_name: str
    region: str = ""


class StoreUpdate(BaseModel):
    """店铺更新请求（字段可选）"""
    store_name: str | None = None
    region: str | None = None
    status: str | None = None


class SiteCreate(BaseModel):
    """站点新增请求"""
    site_code: str
    country_name: str
    default_currency: str = ""
    region: str = ""


@router.get("")
def list_stores(db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    """店铺列表（按用户授权范围过滤；管理员=全部启用店铺）

    ★ 同时返回 `store_code/store_name`（新）与 `code/name`（旧前端下拉框直接读 code/name）
    """
    stores = get_authorized_stores(user, db)
    return [{"id": s.id, "store_code": s.store_code, "store_name": s.store_name,
             "region": s.region, "status": s.status,
             "code": s.store_code, "name": s.store_name}      # ← 旧前端兼容别名
            for s in stores]


@router.post("")
def create_store(req: StoreCreate, db: Session = Depends(get_db),
                 user: SysUser = Depends(require_admin)):
    """新建店铺；店铺代码唯一"""
    if db.query(Store).filter(Store.store_code == req.store_code).first():
        raise HTTPException(status_code=400, detail="店铺代码已存在")
    s = Store(store_code=req.store_code.upper(), store_name=req.store_name, region=req.region)
    db.add(s)
    db.commit()
    return {"id": s.id}


@router.put("/{store_id}")
def update_store(store_id: int, req: StoreUpdate, db: Session = Depends(get_db),
                 user: SysUser = Depends(require_admin)):
    """更新店铺信息（名称/大区/状态）"""
    s = db.query(Store).get(store_id)
    if not s:
        raise HTTPException(status_code=404, detail="店铺不存在")
    if req.store_name is not None:
        s.store_name = req.store_name
    if req.region is not None:
        s.region = req.region
    if req.status is not None:
        s.status = req.status
    db.commit()
    return {"ok": True}


@router.delete("/{store_id}")
def delete_store(store_id: int, db: Session = Depends(get_db),
                 user: SysUser = Depends(require_admin)):
    """停用店铺（软删除）

    ★ 不物理删除：店铺下挂着 fact/raw/batch 等历史数据，删除会破坏溯源与核算。
    """
    s = db.query(Store).get(store_id)
    if not s:
        raise HTTPException(status_code=404, detail="店铺不存在")
    s.status = "disabled"
    db.commit()
    return {"ok": True}


@router.get("/sites")
def list_sites(db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    """站点/国家列表（全局主数据，所有登录用户可见）

    站点代码约定与报表 marketplace 对应：UK/DE/FR/ES/IT/NL/SE/BE/IE/US/CA/MX/AE/SA/AU/PL。
    注意 UK 对应 ISO 的 GB（报表里两种写法都有）。
    """
    sites = db.query(Site).all()
    return [{"id": s.id, "site_code": s.site_code, "country_name": s.country_name,
             "default_currency": s.default_currency, "region": s.region,
             # 旧前端兼容别名（国家下拉框读 code/name）
             "code": s.site_code, "name": s.country_name} for s in sites]
