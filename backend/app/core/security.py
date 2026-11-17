"""安全与认证 — JWT 签发/校验 + 权限体系

权限模型（与旧系统语义兼容，落在新表上）：
  用户权限存 `sys_user.permissions`（JSON 字符串），结构：
      {"stores": ["LMG","MGK"] | null,   # 可见店铺代码；null = 不限制
       "modules": ["dashboard","import"] | null,  # 可用模块；null = 不限制
       "fields": ["cost","profit"] | null}        # 字段级权限（成本/利润可见性）
  管理员(is_admin=1) 不受任何限制。

数据权限两层：
  1. **是否允许访问某店铺**：filter_store_by_permission()
  2. **列表类接口的店铺范围**：get_authorized_stores() / get_authorized_store_ids()
     ← 这两个是"不指定店铺时"用的；绝不能让非管理员默认看到全部店铺数据

★ 维护提醒：新增列表类接口时，务必调用 _apply_store_scope / get_authorized_store_ids，
  否则非管理员会越权看到全部店铺（旧系统存在此类越权问题）。
"""
import bcrypt
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.database import get_db
from app.models import SysUser

# tokenUrl 指向 v2 登录接口；Swagger 授权按钮依赖它
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v2/auth/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码与 bcrypt 哈希（旧库迁移过来的哈希可直接使用）"""
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception:
        # 哈希格式异常时视为不匹配，不抛异常（避免登录接口 500）
        return False


def get_password_hash(password: str) -> str:
    """生成 bcrypt 哈希"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def create_access_token(data: dict, expires_minutes: Optional[int] = None) -> str:
    """签发 JWT；过期时间默认取配置 JWT_EXPIRE_MINUTES"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=expires_minutes or settings.JWT_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> SysUser:
    """从 Bearer Token 解析当前用户；无效/停用一律 401"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.query(SysUser).filter(SysUser.username == username).first()
    if user is None or user.status != "active":
        raise credentials_exception
    return user


def require_admin(current_user: SysUser = Depends(get_current_user)) -> SysUser:
    """管理员专用依赖（解锁批次、删除商品、用户管理等敏感操作）"""
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return current_user


def get_user_permissions(user: SysUser) -> dict:
    """解析用户权限 JSON

    兼容旧系统格式；解析失败/为空时返回全 None（=不限制），
    避免因脏数据把用户锁死。
    """
    import json
    if not user.permissions:
        return {"stores": None, "modules": None, "fields": None}  # None = 不限制
    try:
        return json.loads(user.permissions)
    except Exception:
        return {"stores": None, "modules": None, "fields": None}


def require_module(module: str):
    """模块权限校验依赖工厂

    用法: @router.get("/x", dependencies=[Depends(require_module("dashboard"))])
     或  def ep(user = Depends(require_module("dashboard")))
    管理员直接放行；permissions.modules 为 null 表示不限模块。
    """
    def checker(user: SysUser = Depends(get_current_user)):
        """实际权限校验函数（由 require_module 闭包生成）"""
        if user.is_admin:
            return user
        perms = get_user_permissions(user)
        modules = perms.get("modules")
        if modules is not None and module not in modules:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"无 {module} 模块权限")
        return user
    return checker


def get_authorized_store_codes(user: SysUser) -> list | None:
    """用户可见店铺代码列表；None=全部（管理员或未限制）"""
    if user.is_admin:
        return None
    perms = get_user_permissions(user)
    stores = perms.get("stores")
    return stores  # None = 不限制


def get_authorized_stores(user: SysUser, db) -> list:
    """返回用户可见的店铺对象列表（管理员=全部启用店铺）

    注意与 filter_store_by_permission 的区别：
      后者在"不指定店铺"时返回 None 表示"无过滤条件"，
      本函数始终返回**具体列表**，专供列表类接口与数据权限过滤使用。
    """
    from app.models import Store
    codes = get_authorized_store_codes(user)
    q = db.query(Store).filter(Store.status == 'active')
    if codes is not None:
        if not codes:
            return []          # 未授权任何店铺 → 空列表（不是全部！）
        q = q.filter(Store.store_code.in_(codes))
    return q.order_by(Store.store_code).all()


def get_authorized_store_ids(user: SysUser, db) -> list | None:
    """用户可见店铺 id 列表；None=全部（用于 fact 表 store_id 过滤）

    ★ 列表类接口拿它做 `model.store_id.in_(ids)`；ids 为空时传 [-1] 保证返回空集。
    """
    codes = get_authorized_store_codes(user)
    if codes is None:
        return None
    from app.models import Store
    rows = db.query(Store.id).filter(Store.store_code.in_(codes or ['__none__'])).all()
    return [r[0] for r in rows]


def filter_store_by_permission(user: SysUser, requested_store: Optional[str], db: Session):
    """按权限解析"指定店铺"参数

    返回：
      - 指定了店铺 → 该 Store 对象（无权限则 403，不存在则 404）
      - 未指定店铺 → None（语义 = 不过滤，调用方需自行用 get_authorized_store_ids 收紧范围）
    """
    from app.models import Store
    authorized = get_authorized_store_codes(user)
    if authorized is None:
        if requested_store:
            store = db.query(Store).filter(Store.store_code == requested_store).first()
            if not store:
                raise HTTPException(status_code=404, detail=f"店铺 {requested_store} 不存在")
            return store
        return None
    if requested_store:
        if requested_store not in authorized:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"无店铺 {requested_store} 数据权限")
        store = db.query(Store).filter(Store.store_code == requested_store).first()
        if not store:
            raise HTTPException(status_code=404, detail=f"店铺 {requested_store} 不存在")
        return store
    return db.query(Store).filter(Store.store_code.in_(authorized)).all()
