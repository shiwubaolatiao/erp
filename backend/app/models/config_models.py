"""配置层 + 预警层 — 汇率 / sheet别名 / 表头别名词典 / 成本缺失预警
文档 04-技术需求/02-数据模型.md 第 5 节 + 2.7
"""
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Numeric, ForeignKey, TIMESTAMP, JSON, Text, Index, UniqueConstraint
from sqlalchemy.sql import func
from app.core.database import Base


class ExchangeRate(Base):
    """汇率 — 统一折 RMB，按月维护，可人工覆盖，预留 API 接入点"""
    __tablename__ = "exchange_rate"
    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=True, comment="店铺专属（NULL=全局默认）")
    country_code = Column(String(10), nullable=False, comment="国家代码（币种归国家）")
    period_month = Column(String(7), nullable=False, comment="生效月份 YYYY-MM")
    rate_to_rmb = Column(Numeric(10, 4), nullable=False, comment="折算 RMB 汇率")
    date_from = Column(DateTime, nullable=True, comment="高级：区间覆盖起点")
    date_to = Column(DateTime, nullable=True, comment="高级：区间覆盖终点")
    source = Column(String(20), default="manual", comment="manual=手工 / api=预留API / report=汇率表导入")
    source_row_id = Column(BigInteger, nullable=True, comment="溯源：raw_sheet_row.id")
    updated_by = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        UniqueConstraint("store_id", "country_code", "period_month", name="uq_rate_store_country_month"),
    )


class ExchangeRateHistory(Base):
    """汇率变更历史 — 仅追加（append-only），不提供删除接口
    三个写入入口（报表导入 / 管理员人工 / 核算维护）统一经 common/rates.upsert_rate_with_audit 留痕
    """
    __tablename__ = "exchange_rate_history"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=True)
    country_code = Column(String(10), nullable=False)
    period_month = Column(String(7), nullable=False)
    old_rate = Column(Numeric(10, 4), nullable=True, comment="变更前汇率（首次写入为 NULL）")
    new_rate = Column(Numeric(10, 4), nullable=True, comment="被拒绝覆盖时记录尝试写入的值")
    source = Column(String(20), default="manual", comment="触发来源 manual/api/report")
    action = Column(String(20), nullable=False, comment="update=正常更新 / reject_overwrite=拒绝覆盖人工值")
    operator_user_id = Column(Integer, nullable=True)
    operator_batch_id = Column(BigInteger, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        Index("idx_rate_hist_lookup", "country_code", "period_month", "created_at"),
    )


class SheetAliasMap(Base):
    """sheet 别名映射 — 标准报表类型 → 各店铺实际 sheet 名，可配置无需改代码"""
    __tablename__ = "sheet_alias_map"
    id = Column(Integer, primary_key=True, autoincrement=True)
    report_type = Column(String(50), nullable=False, comment="标准报表类型 sales/product/ad/storage/returns/inbound/removal/aged_storage/freight_rate/exchange_rate/unrecognized")
    alias_name = Column(String(200), nullable=False, comment="店铺实际 sheet 名（含国家前缀如 UK销售）或模式（前缀匹配）")
    country_hint = Column(String(10), default="", comment="别名中蕴含的国家提示（UK销售→UK）")
    store_id = Column(Integer, nullable=True, comment="适用店铺（NULL=全局）")
    __table_args__ = (
        UniqueConstraint("report_type", "alias_name", "store_id", name="uq_sheet_alias"),
    )


class HeaderAlias(Base):
    """多语言表头别名词典 — 未识别表头自动收集候选，管理员指认"""
    __tablename__ = "header_alias"
    id = Column(Integer, primary_key=True, autoincrement=True)
    canonical_field = Column(String(50), nullable=False, comment="标准字段名 quantity/order_id/product_sales/...")
    lang = Column(String(5), default="", comment="语言 en/de/fr/es/nl/it/sv/pl（可空=未指认）")
    alias = Column(String(200), nullable=False, comment="该语言实际表头名")
    report_type = Column(String(50), default="", comment="适用报表类型（空=通用）")
    store_scope = Column(Integer, nullable=True, comment="适用店铺（NULL=全局）")
    __table_args__ = (
        UniqueConstraint("canonical_field", "alias", "report_type", name="uq_header_alias"),
    )


class CostMissingAlert(Base):
    """成本/运费缺失预警 — 状态流转 + 处理留痕，实体表
    文档 02-功能需求/05-成本与利润核算.md 5.10；四状态 ①正常②沿用历史③成本缺失④运费缺失
    ①②不落表（实时判定），③④落表进入待补录队列
    """
    __tablename__ = "cost_missing_alert"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False)
    asin = Column(String(50), nullable=False)
    sku = Column(String(100), nullable=False)
    country = Column(String(10), default="")
    period = Column(String(7), nullable=False, comment="检出月份 YYYY-MM")
    missing_type = Column(String(20), nullable=False, comment="cost=成本 / freight=运费 / both=两者")
    status = Column(String(20), default="pending", comment="pending=待补录 / resolved=已补录 / ignored=已忽略")
    detected_at = Column(TIMESTAMP, server_default=func.now())
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(Integer, nullable=True)
    __table_args__ = (
        UniqueConstraint("store_id", "asin", "sku", "period", "missing_type", name="uq_alert_item"),
        Index("idx_alert_status", "status"),
    )
