"""模型统一导出 — Alembic 自动迁移与 create_all 都从这里导入

★ 维护必读：
  1. 新增模型后**必须**在本文件 import 并加入 ALL_MODELS，
     否则 `Base.metadata.create_all()` 不会建表、Alembic 也检测不到。
  2. 表按三层组织（对应文档 04-技术需求/02-数据模型.md）：
       ODS 原始层 → DWD 清洗层 → DWS 汇总层（+ 主数据/配置/权限/业务）
  3. 全库共 36 张表，均落在 **lmg_erp** 库；旧库 lmg_platform 结构完全不同，
     不可混用（旧库仅作存档与一次性主数据迁移来源）。
"""
# ---- ODS 原始层（全量留痕，不可变，溯源链起点）----
from app.models.master_data import (
    Store, Site, StoreProduct, StoreSkuMapping, SkuReplacement, ProductCostHistory,
)
# ---- 映射审核增强（基准快照 + 操作报告）----
from app.models.mapping_ops import MappingBaselineItem, MappingOpReport
from app.models.ods import (
    ImportBatch, ImportFile, RawSheet, RawSheetRow, ImportRowError,
)
# ---- DWD 清洗层（标准化业务事实，每条带 raw_row_id 指回原始行）----
from app.models.dwd import (
    FactSalesTransaction, FactAdDaily,
    FactFeeStorage, FactFeeAgedStorage, FactFeeRemoval, FactFeeInboundConfig,
    FactFeeReturn, FactCashflowTransfer,
)
# ---- 配置层 + 预警层 ----
from app.models.config_models import (
    ExchangeRate, ExchangeRateHistory, SheetAliasMap, HeaderAlias, CostMissingAlert,
)
# ---- 权限层 + DWS 汇总层 + 业务表（周报 / 广告 Agent）----
from app.models.dws_access import (
    SysUser, Role, UserRole, PermissionGrant, OperationLog, SysTask,
    DwsProfitSnapshot, DwsProfitSnapshotItem,
    WeeklyReport, WeeklyReportImage,
    AdsSession, AdsMessage, AdsDataRecord, AdsActionItem,
)

# Alembic env.py 与建表逻辑统一使用该清单
ALL_MODELS = [
    # 主数据
    Store, Site, StoreProduct, StoreSkuMapping, SkuReplacement, ProductCostHistory,
    # 映射审核增强（基准 + 报告）
    MappingBaselineItem, MappingOpReport,
    # ODS
    ImportBatch, ImportFile, RawSheet, RawSheetRow, ImportRowError,
    # DWD
    FactSalesTransaction, FactAdDaily,
    FactFeeStorage, FactFeeAgedStorage, FactFeeRemoval, FactFeeInboundConfig,
    FactFeeReturn, FactCashflowTransfer,
    # 配置 / 预警
    ExchangeRate, ExchangeRateHistory, SheetAliasMap, HeaderAlias, CostMissingAlert,
    # 权限
    SysUser, Role, UserRole, PermissionGrant, OperationLog,
    # 任务
    SysTask,
    # DWS 汇总
    DwsProfitSnapshot, DwsProfitSnapshotItem,
    # 业务
    WeeklyReport, WeeklyReportImage,
    AdsSession, AdsMessage, AdsDataRecord, AdsActionItem,
]
