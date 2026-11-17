"""映射审核增强 — 基准快照与操作报告（组1 数据层）

对应设计 `specs/mapping_review_enhance/design.md` 2.3：
  · MappingBaselineItem  07 产品信息表基准条目（幂等重放 + 来源可追溯）
  · MappingOpReport      基准导入 / 批量确认 / 存量重归一 的结构化报告（可查可重建可导出）

★ 维护必读：新增模型后必须在本文件写好后，同步加入 app/models/__init__.py 的 ALL_MODELS，
  否则 `Base.metadata.create_all()` 不会建表。
"""
from sqlalchemy import Column, Integer, BigInteger, String, TIMESTAMP, JSON, Index, UniqueConstraint, Boolean, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base


class MappingBaselineItem(Base):
    """基准条目 — 以店铺月度产品信息表（产品明细表/产品信息表）为 ASIN↔SKU 权威来源

    来源：07 汇总目录各店铺 xlsx 的产品 sheet（文件名前缀判店铺）。
    唯一键 (store_id, asin, sku) 保证重复导入幂等。
    """
    __tablename__ = "mapping_baseline_item"
    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False, comment="店铺（文件名前缀判定）")
    asin = Column(String(50), nullable=False)
    sku = Column(String(100), nullable=False)
    source_file = Column(String(255), nullable=False, comment="来源文件名（可追溯）")
    source_sheet = Column(String(100), nullable=False, comment="来源 sheet 名（产品明细表/产品信息表）")
    imported_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("store_id", "asin", "sku", name="uq_baseline_store_asin_sku"),
        Index("idx_baseline_store_sku", "store_id", "sku"),
    )


class MappingOpReport(Base):
    """映射操作报告 — 基准导入 / 批量确认 / 存量重归一的统一落库载体（append-only）

    `payload` 存三类清单明细，供报告重建与 xlsx 导出（spec 5.4.3 场景 1）。
    """
    __tablename__ = "mapping_op_report"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    op_type = Column(String(30), nullable=False,
                     comment="baseline_import / bulk_confirm / replacement_realign")
    dry_run = Column(Boolean, default=False, comment="是否为预览（预览默认不落库）")
    store_scope = Column(String(20), default="ALL", comment="店铺范围：ALL 或店铺代码")
    summary = Column(JSON, nullable=False, comment="分类计数（计数自洽）")
    payload = Column(JSON, nullable=True, comment="三类清单明细（供重建与导出）")
    operator_user_id = Column(Integer, nullable=True, comment="执行人 sys_user.id")
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        Index("idx_report_op_type_time", "op_type", "created_at"),
    )