"""LMG ERP v2 — FastAPI 入口
API 前缀 /api/v2/*；旧系统 /api/* 由并行运行的旧后端继续提供。
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import engine, Base, ensure_database
import app.models  # noqa: F401 注册全部模型
from app.core.security import get_password_hash, verify_password
from app.models import SysUser, Store, Site
from app.core.database import SessionLocal


def _seed():
    """建库建表 + 种子账号/店铺/站点（幂等）"""
    ensure_database()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        admin = db.query(SysUser).filter(SysUser.username == "admin").first()
        if not admin:
            db.add(SysUser(username="admin", password_hash=get_password_hash("123456"),
                           is_admin=1, display_name="管理员", status="active",
                           must_change_password=True))
        elif verify_password("123456", admin.password_hash):
            # 存量库兜底：初始密码仍未改过 → 补强制改密标记（改密成功后不再触发）
            admin.must_change_password = True
        for code, name in [("LMG", "LMG"), ("MGK", "MGK"), ("MGT", "MGT"), ("STX", "STX"), ("WEL", "WEL")]:
            if not db.query(Store).filter(Store.store_code == code).first():
                db.add(Store(store_code=code, store_name=name, status="active"))
        CURRENCY = {'UK': 'GBP', 'US': 'USD', 'DE': 'EUR', 'FR': 'EUR', 'ES': 'EUR', 'IT': 'EUR',
                    'NL': 'EUR', 'SE': 'SEK', 'BE': 'EUR', 'IE': 'EUR', 'CA': 'CAD', 'MX': 'MXN',
                    'AE': 'AED', 'SA': 'SAR', 'AU': 'AUD', 'PL': 'PLN'}
        NAMES = {'US': '美国', 'UK': '英国', 'DE': '德国', 'FR': '法国', 'ES': '西班牙', 'IT': '意大利',
                 'NL': '荷兰', 'SE': '瑞典', 'BE': '比利时', 'IE': '爱尔兰', 'CA': '加拿大',
                 'MX': '墨西哥', 'AE': '阿联酋', 'SA': '沙特', 'AU': '澳大利亚', 'PL': '波兰'}
        EU = {'UK', 'DE', 'FR', 'ES', 'IT', 'NL', 'SE', 'BE', 'IE', 'PL', 'SA', 'AE'}
        for code, name in NAMES.items():
            if not db.query(Site).filter(Site.site_code == code).first():
                db.add(Site(site_code=code, country_name=name,
                            default_currency=CURRENCY.get(code, ''),
                            region=('EU' if code in EU else 'NA')))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：安全检查 + 建库建表 + 种子 + 任务恢复扫描（遗留 running → interrupted，组6）"""
    from app.core.startup_checks import run_startup_checks
    run_startup_checks()
    _seed()
    from app.workers.task_worker import recover_interrupted
    recover_interrupted()
    yield


app = FastAPI(title="LMG ERP", version="2.0", lifespan=lifespan)


# ---- CORS（组11.2 D-05）：显式白名单，空配置拒绝启动 fail-fast ----
from app.core.config import settings  # noqa: E402

if not settings.CORS_ORIGIN_LIST:
    raise RuntimeError(
        "CORS_ORIGINS 未配置或为空：请在 .env 设置显式白名单（逗号分隔，"
        "如 http://localhost:5180,http://127.0.0.1:5180）后重启")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGIN_LIST,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# ---- 路由注册 ----
from app.api.v2 import (  # noqa: E402
    auth, stores, import_data, query, accounting, trace, admin, dashboard, export,
    ai, weekly_report, ads_agent, products, mapping_review,
)

app.include_router(auth.router, prefix="/api/v2/auth", tags=["认证"])
app.include_router(stores.router, prefix="/api/v2/stores", tags=["店铺/站点"])
app.include_router(import_data.router, prefix="/api/v2/import", tags=["导入"])
app.include_router(query.router, prefix="/api/v2/query", tags=["查询"])
app.include_router(accounting.router, prefix="/api/v2/accounting", tags=["核算/快照"])
app.include_router(trace.router, prefix="/api/v2/trace", tags=["溯源"])
app.include_router(admin.router, prefix="/api/v2/admin", tags=["管理"])
app.include_router(products.router, prefix="/api/v2/admin", tags=["管理·商品与成本"])
app.include_router(mapping_review.router, prefix="/api/v2/admin/mapping-review",
                  tags=["管理·映射审核增强"])
app.include_router(dashboard.router, prefix="/api/v2/dashboard", tags=["看板"])
app.include_router(export.router, prefix="/api/v2/export", tags=["导出"])
app.include_router(ai.router, prefix="/api/v2/ai", tags=["AI"])
app.include_router(weekly_report.router, prefix="/api/v2/weekly-reports", tags=["周报"])
app.include_router(ads_agent.router, prefix="/api/v2/ads-agent", tags=["广告Agent"])


@app.middleware("http")
async def deprecation_header(request, call_next):
    """组8.3：旧时间参数 year/month 过渡期提示 —— 命中即响应头带 Deprecation"""
    response = await call_next(request)
    if request.url.path.startswith("/api/v2") and (
            "year" in request.query_params or "month" in request.query_params):
        response.headers["Deprecation"] = 'version="v2", reason="use period_month instead"'
    return response


@app.get("/health")
def health():
    """健康探针：守护进程用它判断服务是否存活（必须含 \"LMG-ERP\" 以便身份识别）"""
    return {"status": "ok", "app": "LMG-ERP", "version": "2.0"}
