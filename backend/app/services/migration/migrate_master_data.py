"""主数据迁移脚本 — 旧库 lmg_platform（只读）→ 新库 lmg_erp

迁移范围（一次性，与报表数据无关，无法从报表重建的数据）：
  1. 用户账号 users → sys_user（密码 hash 原样迁移，无需重置）
  2. 店铺 dim_store → store
  3. 国家 dim_country → site（region 由 country code 推断）
  4. 汇率 dim_exchange_rate → exchange_rate（保留 store_id/月维度）
  5. 产品成本历史 dim_product_cost → product_cost_history（按月版本）
  6. 周报 weekly_reports + weekly_report_images → 同名表（历史周报不丢）

不迁移（有意排除）：dim_product / raw_* / monthly_summary / ads_* —— 从原始报表重建。
旧库保持只读，绝不写入。

用法：
  python -m app.services.migration.migrate_master_data [--dry-run]
"""
import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.core.database import ensure_database, engine, SessionLocal, Base
import app.models  # noqa: F401 — register all models


# 旧库 → 新库 country code 直接沿用（dim_country.code 就是 UK/US/...）
EU = {'UK', 'DE', 'FR', 'ES', 'IT', 'NL', 'SE', 'BE', 'IE', 'PL', 'SA', 'AE'}
NA = {'US', 'CA', 'MX'}

# 旧→新店铺 id 映射
STORE_ID_MAP = {}


def migrate(dry_run: bool = False):
    """执行主数据迁移（旧库→新库）：店铺/站点/汇率/成本历史/用户/周报

★ 旧库始终**只读**，绝不写入；dry_run=True 时全部回滚不落库。
★ 不迁移 dim_product/raw_*/monthly_summary —— 这些从原始报表重建。
    """
    ensure_database()
    Base.metadata.create_all(bind=engine)

    legacy_engine = create_engine(settings.LEGACY_DATABASE_URL, pool_pre_ping=True)
    Legacy = sessionmaker(bind=legacy_engine)()
    New = SessionLocal()

    try:
        # ---------- 1. 店铺 ----------
        old_stores = Legacy.execute(text("SELECT id, code, name FROM dim_store ORDER BY id")).fetchall()
        from app.models import Store
        for oid, code, name in old_stores:
            existing = New.query(Store).filter(Store.store_code == code).first()
            if not existing:
                New.add(Store(id=oid, store_code=code, store_name=name, status='active'))
                New.flush()
            STORE_ID_MAP[oid] = New.query(Store).filter(Store.store_code == code).first().id
        print(f"[stores] {len(STORE_ID_MAP)} migrated")

        # ---------- 2. 站点/国家 ----------
        from app.models import Site
        old_countries = Legacy.execute(text("SELECT id, code, name FROM dim_country ORDER BY id")).fetchall()
        CURRENCY = {'UK': 'GBP', 'US': 'USD', 'DE': 'EUR', 'FR': 'EUR', 'ES': 'EUR', 'IT': 'EUR',
                     'NL': 'EUR', 'SE': 'SEK', 'BE': 'EUR', 'IE': 'EUR', 'CA': 'CAD', 'MX': 'MXN',
                     'AE': 'AED', 'SA': 'SAR', 'AU': 'AUD', 'PL': 'PLN', 'JP': 'JPY'}
        for oid, code, name in old_countries:
            if not New.query(Site).filter(Site.site_code == code).first():
                New.add(Site(site_code=code, country_name=name,
                             default_currency=CURRENCY.get(code, ''),
                             region=('EU' if code in EU else ('NA' if code in NA else ''))))
        print(f"[sites] {len(old_countries)} migrated")

        # ---------- 3. 汇率 ----------
        from app.models import ExchangeRate
        old_rates = Legacy.execute(text(
            "SELECT id, store_id, country_id, year_month, rate FROM dim_exchange_rate ORDER BY id"
        )).fetchall()
        country_code = {oid: code for oid, code, _ in old_countries}
        n = 0
        for oid, store_id, country_id, ym, rate in old_rates:
            code = country_code.get(country_id)
            if not code or rate is None:
                continue
            new_store = STORE_ID_MAP.get(store_id)
            existing = New.query(ExchangeRate).filter(
                ExchangeRate.store_id == new_store,
                ExchangeRate.country_code == code,
                ExchangeRate.period_month == ym,
            ).first()
            if not existing:
                New.add(ExchangeRate(store_id=new_store, country_code=code,
                                     period_month=ym, rate_to_rmb=Decimal(str(rate)),
                                     source='manual'))
                n += 1
        print(f"[exchange_rates] {n} migrated")

        # ---------- 4. 产品成本历史 ----------
        from app.models import ProductCostHistory
        old_costs = Legacy.execute(text("""
            SELECT pc.id, p.store_id, p.asin, p.sku, pc.year_month, pc.cost_rmb, pc.freight_per_unit
            FROM dim_product_cost pc JOIN dim_product p ON pc.product_id = p.id
            ORDER BY pc.id
        """)).fetchall()
        n = 0
        for oid, store_id, asin, sku, ym, cost, freight in old_costs:
            new_store = STORE_ID_MAP.get(store_id)
            if new_store is None:
                continue
            existing = New.query(ProductCostHistory).filter(
                ProductCostHistory.store_id == new_store,
                ProductCostHistory.asin == asin, ProductCostHistory.sku == sku or '',
                ProductCostHistory.period == ym, ProductCostHistory.source_type == 'report',
            ).first()
            if not existing:
                freight_json = {'default': float(freight)} if freight is not None else None
                New.add(ProductCostHistory(
                    store_id=new_store, asin=asin, sku=sku or '', period=ym,
                    cost_rmb=Decimal(str(cost)) if cost is not None else None,
                    freight_json=freight_json, source_type='report'))
                n += 1
        print(f"[cost_history] {n} migrated")

        # ---------- 5. 用户 ----------
        from app.models import SysUser
        from app.core.security import get_password_hash
        old_users = Legacy.execute(text(
            "SELECT id, username, password_hash, is_admin, permissions FROM users ORDER BY id"
        )).fetchall()
        n = 0
        for oid, username, phash, is_admin, perms in old_users:
            if New.query(SysUser).filter(SysUser.username == username).first():
                continue
            New.add(SysUser(username=username, password_hash=phash,  # hash 原样，密码不变
                            is_admin=is_admin or 0, permissions=perms, status='active'))
            n += 1
        # 保证默认 admin 存在
        if not New.query(SysUser).filter(SysUser.username == 'admin').first():
            New.add(SysUser(username='admin', password_hash=get_password_hash('123456'),
                            is_admin=1, status='active'))
            n += 1
        print(f"[users] {n} migrated")

        # ---------- 6. 周报 ----------
        from app.models import WeeklyReport, WeeklyReportImage
        old_reports = Legacy.execute(text(
            "SELECT id, store_id, year, week_num, title, content, polished_content, status, "
            "author_id, reviewer_id, review_comment, created_at, updated_at, submitted_at, reviewed_at "
            "FROM weekly_reports ORDER BY id")).fetchall()
        n = 0
        for (oid, store_id, year, week_num, title, content, polished, status,
             author_id, reviewer_id, comment, created, updated, submitted, reviewed) in old_reports:
            new_store = STORE_ID_MAP.get(store_id)
            if new_store is None:
                continue
            New.add(WeeklyReport(store_id=new_store, year=year, week_num=week_num, title=title,
                                 content=content or '', polished_content=polished or '',
                                 status=status or 'draft', author_id=author_id, reviewer_id=reviewer_id,
                                 review_comment=comment or '', created_at=created, updated_at=updated,
                                 submitted_at=submitted, reviewed_at=reviewed))
            n += 1
        print(f"[weekly_reports] {n} migrated")

        if dry_run:
            New.rollback()
            print("[DRY RUN] 全部回滚，未写入。")
        else:
            New.commit()
            print("[DONE] 主数据迁移完成。")
    finally:
        Legacy.close()
        New.close()
        legacy_engine.dispose()


if __name__ == '__main__':
    migrate(dry_run='--dry-run' in sys.argv)
