"""权限层（RBAC + 数据权限）+ DWS 汇总层 + 周报/广告Agent 等业务表
文档 04-技术需求/02-数据模型.md 第 6/7 节
"""
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Numeric, ForeignKey, TIMESTAMP, JSON, Text, Index, UniqueConstraint, Boolean
from sqlalchemy.sql import func
from app.core.database import Base


# ============ 权限层 ============

class SysUser(Base):
    """系统用户（本地账号密码；密码哈希与旧库兼容，迁移后无需重置）"""
    __tablename__ = "sys_user"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    display_name = Column(String(100), default="")
    department = Column(String(50), default="", comment="运营/财务/管理")
    is_admin = Column(Integer, default=0)
    status = Column(String(20), default="active", comment="active/disabled")
    must_change_password = Column(Boolean, default=False,
                                  comment="种子/管理员重置的初始密码，登录后强制改密")
    permissions = Column(Text, nullable=True, comment="JSON 兼容旧格式：{stores, modules, fields}")
    created_at = Column(TIMESTAMP, server_default=func.now())


class Role(Base):
    """角色（RBAC 角色定义）"""
    __tablename__ = "role"
    id = Column(Integer, primary_key=True, autoincrement=True)
    role_name = Column(String(50), unique=True, nullable=False)
    role_code = Column(String(50), unique=True, nullable=False)
    comment = Column(String(200), default="")


class UserRole(Base):
    """用户-角色关联（多对多，权限取并集）"""
    __tablename__ = "user_role"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)
    role_id = Column(Integer, ForeignKey("role.id", ondelete="CASCADE"), nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)


class PermissionGrant(Base):
    """主管授权/回收留痕 — 主管只能转授自己拥有的权限子集"""
    __tablename__ = "permission_grant"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    grantor_id = Column(Integer, ForeignKey("sys_user.id"), nullable=False, comment="主管")
    grantee_id = Column(Integer, ForeignKey("sys_user.id"), nullable=False, comment="下属")
    perm_code = Column(String(100), nullable=False, comment="权限码（module:field / store:CODE 等）")
    action = Column(String(10), nullable=False, comment="grant=授予 / revoke=回收")
    expires_at = Column(DateTime, nullable=True, comment="有效期（NULL=长期）")
    granted_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        Index("idx_grant_grantee", "grantee_id", "perm_code"),
    )


class OperationLog(Base):
    """审计留痕"""
    __tablename__ = "operation_log"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True)
    username = Column(String(50), default="")
    action = Column(String(50), nullable=False, comment="login/import/lock/unlock/export/grant/confirm/...")
    target = Column(String(200), default="", comment="操作对象（批次号/店铺/用户名...）")
    scope = Column(String(200), default="")
    result = Column(String(20), default="ok", comment="ok/fail")
    detail = Column(Text, default="")
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        Index("idx_oplog_time", "created_at"),
        Index("idx_oplog_user", "user_id"),
    )


class SysTask(Base):
    """通用后台任务 — 进程内"线程 + DB 任务表"模式（单机，不引入 Celery/Redis）

    状态机：queued → running → success / failed；进程重启时启动扫描把
    遗留 queued/running 标记 interrupted（杜绝假成功，spec 5.4.3 场景 2）。
    task_key 为对外句柄（uuid hex，兼容旧导入任务 id 形态）。
    """
    __tablename__ = "sys_task"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_key = Column(String(32), unique=True, nullable=False, comment="对外任务句柄")
    task_type = Column(String(50), nullable=False,
                       comment="accounting.calculate / import.batch / ...（注册制）")
    status = Column(String(20), nullable=False, default="queued",
                    comment="queued/running/success/failed/interrupted")
    progress = Column(Integer, default=0, comment="0-100")
    stage = Column(String(50), default="", comment="fetching/calculating/saving/done")
    payload = Column(JSON, nullable=True, comment="任务参数")
    result = Column(JSON, nullable=True, comment="任务结果（核算快照摘要 / 导入 batches 等）")
    result_ref = Column(String(200), default="", comment="结果快捷引用（如 snapshot_id）")
    error = Column(Text, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_task_status_created", "status", "created_at"),
        Index("idx_task_type", "task_type", "created_at"),
    )


# ============ DWS 汇总层 ============

class DwsProfitSnapshot(Base):
    """核算快照 — (店铺, 区间, version) 冻结不可改；重算产生新版本
    文档 02-功能需求/05-成本与利润核算.md 5.5~5.8
    """
    __tablename__ = "dws_profit_snapshot"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False)
    country = Column(String(10), default="", comment="站点（空=店铺全站汇总）")
    period_month = Column(String(7), comment="自然月口径 YYYY-MM（默认）")
    date_from = Column(DateTime, nullable=True, comment="自定义区间起点（高级）")
    date_to = Column(DateTime, nullable=True, comment="自定义区间终点（高级）")
    version = Column(Integer, nullable=False, default=1, comment="同一区间版本号递增")
    snapshot_name = Column(String(200), default="", comment="命名快照，如 UK-2026年6月结算-终版")
    is_frozen = Column(Boolean, default=True, comment="冻结后不可改")
    # ---- 汇总指标（明细在 dws_profit_snapshot_item）----
    net_amazon = Column(Numeric(14, 2), default=0, comment="原币 net_amazon")
    exchange_rate = Column(Numeric(10, 4), default=0)
    product_cost_rmb = Column(Numeric(14, 2), default=0)
    freight_cost_rmb = Column(Numeric(14, 2), default=0)
    ad_spend = Column(Numeric(14, 2), default=0)
    storage_fee = Column(Numeric(14, 2), default=0)
    aged_storage_fee = Column(Numeric(14, 2), default=0)
    returns_fee = Column(Numeric(14, 2), default=0)
    inbound_fee = Column(Numeric(14, 2), default=0)
    removal_fee = Column(Numeric(14, 2), default=0)
    net_profit_rmb = Column(Numeric(14, 2), default=0)
    net_profit_rate = Column(Numeric(10, 4), default=0)
    product_sales_rmb = Column(Numeric(14, 2), default=0)
    order_count = Column(Integer, default=0)
    order_qty = Column(Integer, default=0)
    cost_complete = Column(Boolean, default=True, comment="成本完整性：False=含缺失成本SKU（口径不完整）")
    cost_incomplete_skus = Column(Integer, default=0, comment="缺失成本的 SKU 数")
    rate_source = Column(String(20), nullable=True,
                         comment="汇率来源粒度：per_country=逐站点折算 / single=单汇率 / default_fallback=有站点回退默认值")
    rate_detail = Column(JSON, nullable=True,
                         comment="站点级汇率明细 {站点: 汇率, '_missing': [回退默认值的站点]}")
    created_by = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("store_id", "country", "period_month", "date_from", "date_to", "version", name="uq_snapshot_version"),
        Index("idx_snapshot_store", "store_id", "period_month"),
        # 组7 P-02：latest_snapshots 按粒度（country + 期间 + 版本去重）热路径索引
        Index("idx_snapshot_country_period", "country", "period_month", "version"),
    )


class DwsProfitSnapshotItem(Base):
    """快照明细项 — 按产品维度，含溯源信息"""
    __tablename__ = "dws_profit_snapshot_item"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id = Column(BigInteger, ForeignKey("dws_profit_snapshot.id", ondelete="CASCADE"), nullable=False)
    asin = Column(String(50), nullable=False)
    sku = Column(String(100), default="")
    net_amazon = Column(Numeric(14, 2), default=0)
    quantity = Column(Integer, default=0)
    order_count = Column(Integer, default=0)
    product_cost_rmb = Column(Numeric(14, 2), default=0)
    freight_cost_rmb = Column(Numeric(14, 2), default=0)
    ad_spend = Column(Numeric(14, 2), default=0)
    storage_fee = Column(Numeric(14, 2), default=0)
    aged_storage_fee = Column(Numeric(14, 2), default=0)
    returns_fee = Column(Numeric(14, 2), default=0)
    inbound_fee = Column(Numeric(14, 2), default=0)
    removal_fee = Column(Numeric(14, 2), default=0)
    net_profit_rmb = Column(Numeric(14, 2), default=0)
    cost_status = Column(String(20), default="normal", comment="normal=正常 / inherited=沿用历史 / missing=成本缺失")
    cost_inherited_from = Column(String(7), default="", comment="沿用自 YYYY-MM（状态=inherited 时）")
    country = Column(String(10), nullable=True, default=None,
                     comment="站点（B-01 修复后明细承载站点；NULL/''=旧版未分站点明细）")
    exchange_rate = Column(Numeric(10, 4), nullable=True,
                           comment="该明细行折算汇率（站点级；旧版明细为 NULL）")
    source_batch_ids = Column(JSON, nullable=True, comment="数据来源批次清单（溯源用）")
    __table_args__ = (
        Index("idx_snapitem_snapshot", "snapshot_id"),
        Index("idx_snapitem_asin", "asin"),
    )


# ============ 周报 + 广告Agent 业务表（沿用旧结构，接口不变） ============

class WeeklyReport(Base):
    """周报（含审核流：draft → submitted → approved/rejected）"""
    __tablename__ = "weekly_reports"
    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False)
    year = Column(Integer, nullable=False)
    week_num = Column(Integer, nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text, default="")
    polished_content = Column(Text, default="")
    status = Column(String(20), default="draft", comment="draft/submitted/approved/rejected")
    author_id = Column(Integer, nullable=True)
    reviewer_id = Column(Integer, nullable=True)
    review_comment = Column(Text, default="")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    submitted_at = Column(DateTime, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)


class WeeklyReportImage(Base):
    """周报配图（图片文件落在 backend/uploads/weekly/）"""
    __tablename__ = "weekly_report_images"
    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(Integer, ForeignKey("weekly_reports.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    description = Column(Text, default="")
    sort_order = Column(Integer, default=0)
    uploaded_at = Column(TIMESTAMP, server_default=func.now())


class AdsSession(Base):
    """广告 Agent 会话（data_record_ids 记录本会话上传的数据）"""
    __tablename__ = "ads_session"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True)
    title = Column(String(200), default="新会话")
    data_record_ids = Column(Text)
    status = Column(String(20), default="active")
    message_count = Column(Integer, default=0)
    created_at = Column(TIMESTAMP, server_default=func.now())
    last_active_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())


class AdsMessage(Base):
    """广告 Agent 消息（保留最近 N 轮作为工作记忆）"""
    __tablename__ = "ads_message"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("ads_session.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    message_index = Column(Integer, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())


class AdsDataRecord(Base):
    """广告 Agent 上传数据记录（指标与摘要以 JSON 存下来，供后续追问）"""
    __tablename__ = "ads_data_record"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("ads_session.id", ondelete="CASCADE"), nullable=False)
    fingerprint = Column(String(64))
    report_type = Column(String(50))
    date_start = Column(DateTime)
    date_end = Column(DateTime)
    row_count = Column(Integer)
    metrics_json = Column(Text)
    summary_json = Column(Text)
    original_filename = Column(String(500))
    uploaded_by = Column(Integer, nullable=True)
    uploaded_at = Column(TIMESTAMP, server_default=func.now())


class AdsActionItem(Base):
    """广告 Agent 生成的行动项（否定词/调价/加预算等，可导出）"""
    __tablename__ = "ads_action_item"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("ads_session.id", ondelete="CASCADE"), nullable=False)
    action_type = Column(String(50))
    target_type = Column(String(50))
    target_name = Column(String(500))
    current_value = Column(Numeric(12, 4))
    suggested_value = Column(Numeric(12, 4))
    reason = Column(Text)
    expected_savings = Column(Numeric(12, 2))
    status = Column(String(20), default="pending")
    created_at = Column(TIMESTAMP, server_default=func.now())
