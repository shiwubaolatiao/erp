"""认证接口 — 登录 / 当前用户 / 改密 / 用户管理

接口前缀: /api/v2/auth
权限：登录与改密为自助；用户增删改需管理员。
审计：登录、建号、改号、停号均写 operation_log。
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import (
    verify_password, get_password_hash, create_access_token,
    get_current_user, require_admin, get_user_permissions,
)
from app.models import SysUser, OperationLog

router = APIRouter()


class TokenResponse(BaseModel):
    """登录响应：token + 用户信息（前端据此渲染菜单与权限）"""
    access_token: str
    token_type: str = "bearer"
    user: dict


class ChangePasswordRequest(BaseModel):
    """改密请求：原密码 + 新密码"""
    old_password: str
    new_password: str


class UserCreateRequest(BaseModel):
    """新建用户请求：permissions 为 JSON 字符串 {stores, modules, fields}"""
    username: str
    password: str
    is_admin: int = 0
    permissions: str | None = None      # JSON 字符串：{stores, modules, fields}
    display_name: str = ""
    department: str = ""                # 运营 / 财务 / 管理


class UserUpdateRequest(BaseModel):
    """用户更新：所有字段可选，只更新传入项"""
    password: str | None = None
    is_admin: int | None = None
    permissions: str | None = None
    display_name: str | None = None
    department: str | None = None
    status: str | None = None


@router.post("/login", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """账号密码登录（OAuth2 表单格式，兼容 Swagger 授权）

    密码哈希沿用旧库迁移过来的 bcrypt 值，老用户无需重置密码。
    """
    user = db.query(SysUser).filter(SysUser.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        # 统一提示，不区分"用户不存在/密码错"，避免账号枚举
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已停用")
    token = create_access_token(data={"sub": user.username})
    db.add(OperationLog(user_id=user.id, username=user.username, action="login",
                        target="auth", result="ok"))
    db.commit()
    return TokenResponse(access_token=token, user={
        "id": user.id, "username": user.username, "is_admin": user.is_admin,
        "display_name": user.display_name, "department": user.department,
        "must_change_password": bool(user.must_change_password),
        "permissions": get_user_permissions(user),
    })


@router.get("/me")
def get_me(current_user: SysUser = Depends(get_current_user)):
    """当前登录用户信息（前端刷新页面后用它恢复登录态与权限）"""
    return {
        "id": current_user.id, "username": current_user.username,
        "is_admin": current_user.is_admin,
        "display_name": current_user.display_name,
        "department": current_user.department,
        "must_change_password": bool(current_user.must_change_password),
        "permissions": get_user_permissions(current_user),
    }


@router.post("/change-password")
def change_password(req: ChangePasswordRequest,
                    current_user: SysUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """自助改密：必须先验证原密码（防 token 被盗后直接改密）"""
    if not verify_password(req.old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")
    current_user.password_hash = get_password_hash(req.new_password)
    current_user.must_change_password = False
    db.add(OperationLog(user_id=current_user.id, username=current_user.username,
                        action="change_password", target=current_user.username, result="ok"))
    db.commit()
    return {"ok": True}


@router.get("/users")
def list_users(current_user: SysUser = Depends(require_admin), db: Session = Depends(get_db)):
    """用户列表（管理员）"""
    users = db.query(SysUser).all()
    return [{
        "id": u.id, "username": u.username, "is_admin": u.is_admin,
        "display_name": u.display_name, "department": u.department,
        "permissions": get_user_permissions(u), "status": u.status,
    } for u in users]


@router.post("/users")
def create_user(req: UserCreateRequest,
                current_user: SysUser = Depends(require_admin),
                db: Session = Depends(get_db)):
    """新建用户（管理员）；用户名唯一"""
    if db.query(SysUser).filter(SysUser.username == req.username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    u = SysUser(username=req.username, password_hash=get_password_hash(req.password),
                is_admin=req.is_admin, permissions=req.permissions,
                display_name=req.display_name, department=req.department, status="active")
    db.add(u)
    db.add(OperationLog(user_id=current_user.id, username=current_user.username,
                        action="create_user", target=req.username, result="ok"))
    db.commit()
    return {"id": u.id, "username": u.username}


@router.put("/users/{user_id}")
def update_user(user_id: int, req: UserUpdateRequest,
                current_user: SysUser = Depends(require_admin),
                db: Session = Depends(get_db)):
    """更新用户（管理员）：改密/改权限/改部门/启停用"""
    u = db.query(SysUser).get(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    if req.password:
        u.password_hash = get_password_hash(req.password)
    if req.is_admin is not None:
        u.is_admin = req.is_admin
    if req.permissions is not None:
        u.permissions = req.permissions
    if req.display_name is not None:
        u.display_name = req.display_name
    if req.department is not None:
        u.department = req.department
    if req.status is not None:
        u.status = req.status
    db.add(OperationLog(user_id=current_user.id, username=current_user.username,
                        action="update_user", target=u.username, result="ok"))
    db.commit()
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int,
                current_user: SysUser = Depends(require_admin),
                db: Session = Depends(get_db)):
    """停用用户（软删除）

    ★ 不物理删除：该用户可能已关联导入批次(import_batch.imported_by)、
      审计日志等历史数据，物理删除会破坏溯源链。
    """
    u = db.query(SysUser).get(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    if u.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    u.status = "disabled"
    db.add(OperationLog(user_id=current_user.id, username=current_user.username,
                        action="disable_user", target=u.username, result="ok"))
    db.commit()
    return {"ok": True}
