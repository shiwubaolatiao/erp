"""主数据层 — 店铺 / 站点 / 产品主档（店铺隔离）/ SKU映射三元组 / 成本版本
对应文档 04-技术需求/02-数据模型.md 第 2 节
"""
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Numeric, ForeignKey, TIMESTAMP, JSON, Text, Index, UniqueConstraint, Boolean
from sqlalchemy.sql import func
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from app.core.database import Base


class Store(Base):
    """店铺（LMG/MGK/MGT/STX/WEL）"""
    __tablename__ = "store"
    id = Column(Integer, primary_key=True, autoincrement=True)
    store_code = Column(String(50), unique=True, nullable=False, comment="店铺代码")
    store_name = Column(String(100), nullable=False, comment="店铺名称")
    region = Column(String(10), comment="NA/EU 大区（冗余，便于筛选；实际站点归 site 管）")
    status = Column(String(20), default="active", comment="active/disabled")
    created_at = Column(TIMESTAMP, server_default=func.now())


class Site(Base):
    """站点/国家"""
    __tablename__ = "site"
    id = Column(Integer, primary_key=True, autoincrement=True)
    site_code = Column(String(5), unique=True, nullable=False, comment="国家代码 UK/US/DE...")
    country_name = Column(String(50), nullable=False)
    default_currency = Column(String(10), default="")
    region = Column(String(10), comment="NA/EU")


class StoreProduct(Base):
    """店铺产品主档 — 店铺隔离，UNIQUE(store_id, asin)，不跨店共享
    文档 02-功能需求/02-商品与ASIN-SKU映射.md 5.1
    """
    __tablename__ = "store_product"
    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False)
    asin = Column(String(50), nullable=False, comment="ASIN（店铺内唯一）")
    title = Column(String(500), default="")
    brand = Column(String(200), default="")
    image = Column(String(1000), default="")
    status = Column(String(20), default="pending", comment="normal/pending/abnormal 正常/待确认/异常")
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("store_id", "asin", name="uq_store_asin"),
        Index("idx_store_status", "store_id", "status"),
    )


class StoreSkuMapping(Base):
    """店铺 SKU-ASIN 映射 — (店铺, SKU, ASIN) 三元组核心表
    约束：UNIQUE(store_id, sku) 仅对 status='normal' 生效（MySQL 用逻辑约束 + 应用层保证）
    文档 02-功能需求/02-商品与ASIN-SKU映射.md 5.2/5.3 五类场景
    """
    __tablename__ = "store_sku_mapping"
    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False)
    sku = Column(String(100), nullable=False, comment="SKU（amzn.gr.* 已归一后的父SKU）")
    asin = Column(String(50), nullable=False)
    store_product_id = Column(Integer, ForeignKey("store_product.id"), nullable=True)
    status = Column(String(20), default="pending", comment="normal/pending/abnormal 正常/待审/异常")
    first_seen_at = Column(TIMESTAMP, server_default=func.now())
    confirmed_by = Column(Integer, ForeignKey("sys_user.id"), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    note = Column(String(500), default="", comment="审核备注（异常原因等）")
    __table_args__ = (
        Index("idx_mapping_store_sku", "store_id", "sku"),
        Index("idx_mapping_status", "status"),
    )


class SkuReplacement(Base):
    """替换件 SKU 归并：amzn.gr.* → 父 SKU；原始值保留在原始层
    文档 02-功能需求/02-商品与ASIN-SKU映射.md 5.6
    """
    __tablename__ = "sku_replacement"
    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False)
    replacement_sku = Column(String(200), nullable=False, comment="原始 amzn.gr.* 形式 SKU")
    parent_sku = Column(String(100), nullable=False, comment="归并到的父 SKU")
    prev_parent_sku = Column(String(100), nullable=True,
                             comment="存量重归一修正前的父 SKU（首次归并为 NULL，禁止静默改写）")
    updated_at = Column(DateTime, nullable=True, comment="归并关系最近修正时间")
    first_seen_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("store_id", "replacement_sku", name="uq_replacement_store_sku"),
    )


class ProductCostHistory(Base):
    """成本/运费按月版本
    主来源=报表产品表（成本RMB+运费列），手工维护降级为补录入口
    文档 04-技术需求/02-数据模型.md 2.5
    """
    __tablename__ = "product_cost_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False)
    asin = Column(String(50), nullable=False)
    sku = Column(String(100), nullable=False)
    period = Column(String(7), nullable=False, comment="归属月份 YYYY-MM")
    cost_rmb = Column(Numeric(12, 2), nullable=True, comment="采购成本 RMB（缺失=None，不与0混淆）")
    freight_json = Column(JSON, nullable=True, comment="头程运费：{'default': x} 或 {'UK': x, 'IE': y}")
    source_batch_id = Column(BigInteger, ForeignKey("import_batch.id"), nullable=True)
    source_type = Column(String(20), default="report", comment="report=报表导入 / manual=人工补录")
    source_row_id = Column(BigInteger, nullable=True, comment="溯源：raw_sheet_row.id")
    created_by = Column(Integer, ForeignKey("sys_user.id"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        # 同 (店铺, asin, sku, period) 只保留一条有效版本（报表覆盖=新版本替换）
        UniqueConstraint("store_id", "asin", "sku", "period", "source_type", name="uq_cost_version"),
        Index("idx_cost_lookup", "store_id", "period"),
    )
