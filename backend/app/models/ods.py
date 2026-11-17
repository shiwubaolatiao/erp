"""ODS 原始层 — 导入批次 / 文件归档 / sheet 元信息 / 原始行全量留痕
溯源链路终点：dws → fact_*.raw_row_id → raw_sheet_row → batch → file
文档 04-技术需求/02-数据模型.md 第 3 节
"""
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Numeric, ForeignKey, TIMESTAMP, JSON, Text, Index, Boolean, UniqueConstraint
from sqlalchemy.sql import func, text
from app.core.database import Base


class ImportBatch(Base):
    """导入批次"""
    __tablename__ = "import_batch"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    file_name = Column(String(255), nullable=False, comment="文件名")
    file_hash = Column(String(64), nullable=False, comment="文件 SHA256（查重）")
    store_id = Column(Integer, ForeignKey("store.id"), nullable=True)
    report_type = Column(String(50), comment="标准化报表类型 workbook/folder/...")
    imported_by = Column(Integer, ForeignKey("sys_user.id"), nullable=True)
    imported_at = Column(TIMESTAMP, server_default=func.now())
    period_month = Column(String(7), nullable=True,
                          comment="报表归属月份 YYYY-MM（导入时指定；用于无日期数据的按月归集）")
    period_source = Column(String(20), default="user_specified",
                           comment="归月来源 user_specified=上传时指定 / default_current=未指定按当前月")
    warnings = Column(JSON, nullable=True,
                      comment="批次告警（kind+message，如合并工作簿归月偏差；非失败，不计数）")
    original_path = Column(String(1000), default="")
    success_rows = Column(Integer, default=0)
    failed_rows = Column(Integer, default=0)
    status = Column(String(20), default="running", comment="running/success/partial/failed")
    is_locked = Column(Boolean, default=False, comment="批次锁结")
    error_summary = Column(Text, default="", comment="失败摘要（明细在 import_row_error）")
    __table_args__ = (
        Index("idx_batch_hash", "file_hash"),
        Index("idx_batch_store_time", "store_id", "imported_at"),
    )


class ImportFile(Base):
    """原始文件归档（workbook/folder 导入可能含多个文件）"""
    __tablename__ = "import_file"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_hash = Column(String(64), nullable=False)
    original_path = Column(String(1000), default="")
    stored_path = Column(String(1000), comment="归档落盘路径（原样保存 xlsx）")
    imported_by = Column(Integer, nullable=True)
    imported_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("batch_id", "file_hash", name="uq_file_batch_hash"),
    )


class RawSheet(Base):
    """sheet 元信息"""
    __tablename__ = "raw_sheet"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False)
    file_id = Column(BigInteger, ForeignKey("import_file.id"), nullable=True)
    sheet_name = Column(String(200), nullable=False, comment="原 sheet 名")
    report_type = Column(String(50), comment="识别出的标准类型（sales/product/ad/freight_rate/...；未识别=unrecognized）")
    detected_header_row = Column(Integer, comment="定位到的表头行号")
    country = Column(String(10), comment="识别出的国家/站点")
    currency = Column(String(10), comment="识别出的币种")
    row_count = Column(Integer, default=0)
    processed = Column(Boolean, nullable=False, default=False, server_default=text("0"),
                       comment="sheet 处理终态（1=ODS+DWD 完成；断点续导依据）")
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        Index("idx_sheet_batch", "batch_id"),
    )


class RawSheetRow(Base):
    """原始行 — 全量留痕核心表，不可变，按 biz_date 逻辑月份查询
    说明：MySQL 分区表需要建表 DDL 单独处理（Alembic 迁移中生成 PARTITION BY RANGE），
    ORM 层保持普通表映射，biz_date 上建索引；月分区在建表 SQL 里补充。
    """
    __tablename__ = "raw_sheet_row"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(Integer, nullable=True)
    batch_id = Column(BigInteger, ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False)
    sheet_id = Column(BigInteger, ForeignKey("raw_sheet.id", ondelete="CASCADE"), nullable=False)
    sheet_name = Column(String(200), nullable=False)
    row_no = Column(Integer, nullable=False, comment="Excel 原始行号（1-based）")
    biz_date = Column(DateTime, nullable=True, comment="原始业务日期（解析所得；无法解析为 NULL）")
    headers_json = Column(JSON, nullable=False, comment="原字段名数组")
    values_json = Column(JSON, nullable=False, comment="原值数组（顺序与 headers 对应）")
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        Index("idx_rawrow_batch", "batch_id"),
        Index("idx_rawrow_bizdate", "biz_date"),
        Index("idx_rawrow_store_date", "store_id", "biz_date"),
    )


class ImportRowError(Base):
    """导入异常池 — 校验失败行，不阻塞其他行"""
    __tablename__ = "import_row_error"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("import_batch.id", ondelete="CASCADE"), nullable=False)
    sheet_id = Column(BigInteger, ForeignKey("raw_sheet.id"), nullable=True)
    sheet_name = Column(String(200), default="")
    row_no = Column(Integer, nullable=True)
    error_type = Column(String(50), comment="missing_column/bad_number/bad_date/unknown_country/...")
    error_message = Column(String(500))
    raw_values_json = Column(JSON, nullable=True, comment="原始行值快照")
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        Index("idx_err_batch", "batch_id"),
    )
