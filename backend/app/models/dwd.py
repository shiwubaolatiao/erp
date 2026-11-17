"""DWD 清洗层 — 销售交易 / 广告 / 各类费用 / 资金流提款
每条记录带 raw_row_id 指回原始行 — 溯源链路中环
文档 04-技术需求/02-数据模型.md 第 4 节；利润口径 02-功能需求/05-成本与利润核算.md 5.4
"""
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Numeric, ForeignKey, TIMESTAMP, JSON, Text, Index, Boolean
from sqlalchemy.sql import func
from app.core.database import Base

# 通用字段前缀，减少重复


class _FactBase:
    """fact 表公共列 mixin"""
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("store.id"), nullable=False)
    country = Column(String(10), nullable=False, comment="站点代码 UK/US/DE...")
    raw_row_id = Column(BigInteger, ForeignKey("raw_sheet_row.id"), nullable=False, comment="溯源 → raw_sheet_row")
    batch_id = Column(BigInteger, nullable=False, comment="冗余批次，便于按批次清理/锁结")
    source_batch_id = Column(BigInteger, nullable=True, comment="兼容字段：本数据所属的有效批次（费用覆盖导入时与 batch_id 区分）")
    currency = Column(String(10), default="", comment="币种（从表头/数据解析）")


class FactSalesTransaction(Base, _FactBase):
    """销售交易（Order/Refund/Adjustment + 不计入利润的类型全部保留，利润层按 type 过滤）"""
    __tablename__ = "fact_sales_transaction"
    biz_date = Column(DateTime, nullable=False, comment="原始业务日期 — 账期过滤唯一依据")
    transaction_type = Column(String(80), nullable=False, comment="type 列：Order/Refund/Adjustment/Service Fee/Amazon Fees/FBA Inventory Fee/Fulfilment by Amazon (FBA) transaction fees/Transfer/...")
    has_order_id = Column(Boolean, nullable=False, default=False, comment="order id 是否非空（Adjustment 分桶依据）")
    order_id = Column(String(50), default="")
    settlement_id = Column(String(50), default="")
    sku = Column(String(100), default="", comment="原始 SKU（amzn.gr.* 未归一原值）")
    normalized_sku = Column(String(100), default="", comment="归一后 SKU（amzn.gr.* → 父SKU）")
    asin = Column(String(50), default="")
    quantity = Column(Integer, default=0)
    marketplace = Column(String(30), default="")
    fulfilment = Column(String(30), default="")
    product_sales = Column(Numeric(12, 2), default=0)
    product_sales_tax = Column(Numeric(12, 2), default=0)
    postage_credits = Column(Numeric(12, 2), default=0)
    shipping_credits_tax = Column(Numeric(12, 2), default=0)
    gift_wrap_credits = Column(Numeric(12, 2), default=0)
    giftwrap_credits_tax = Column(Numeric(12, 2), default=0)
    promo_rebate = Column(Numeric(12, 2), default=0)
    promo_rebate_tax = Column(Numeric(12, 2), default=0)
    marketplace_withheld_tax = Column(Numeric(12, 2), default=0)
    selling_fee = Column(Numeric(12, 2), default=0, comment="销售佣金（负值）")
    fba_fee = Column(Numeric(12, 2), default=0, comment="FBA 配送费（负值）")
    other_amount = Column(Numeric(12, 2), default=0, comment="other 列（Adjustment 有 order_id 时分桶用此列）")
    line_total = Column(Numeric(12, 2), default=0, comment="total 列（Adjustment 无 order_id 时按正负分桶）")
    other_transaction_fees = Column(Numeric(12, 2), default=0, comment="other transaction fees — 忽略不计入")
    __table_args__ = (
        Index("idx_fst_store_date", "store_id", "biz_date"),
        Index("idx_fst_country_date", "country", "biz_date"),
        Index("idx_fst_type", "transaction_type"),
        Index("idx_fst_sku", "normalized_sku"),
        Index("idx_fst_asin", "asin"),
        # 组7 P-02：热路径主查询（store + 类型过滤 + 日期区间）复合索引
        Index("idx_fst_store_type_date", "store_id", "transaction_type", "biz_date"),
    )


class FactAdDaily(Base, _FactBase):
    """广告日数据

    ★ 归月说明（重要）：亚马逊广告报表**没有日期列**，因此 `biz_date` 恒为 NULL，
      必须靠 `period_month`（导入时由使用者指定的报表归属月份）来按月归集。
      没有这一列就完全无法回答"6 月广告费是多少"。
    """
    __tablename__ = "fact_ad_daily"
    biz_date = Column(DateTime, nullable=True)
    period_month = Column(String(7), nullable=True, index=True,
                          comment="报表归属月份 YYYY-MM（广告表无日期列，靠它归月）")
    product_field = Column(String(200), default="", comment="广告报表'商品'列原始值")
    asin = Column(String(50), default="")
    sku = Column(String(100), default="")
    ad_type = Column(String(50), default="")
    status_val = Column(String(50), default="")
    eligibility = Column(String(100), default="")
    sales = Column(Numeric(12, 2), default=0)
    spend = Column(Numeric(12, 2), default=0)
    roas = Column(Numeric(10, 4), default=0)
    acos = Column(Numeric(10, 4), default=0)
    ctr = Column(Numeric(10, 4), default=0)
    cpc = Column(Numeric(10, 4), default=0)
    conversion_rate = Column(Numeric(10, 4), default=0)
    impressions = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    orders = Column(Integer, default=0)
    ntb_orders = Column(Integer, default=0)
    ntb_order_pct = Column(Numeric(10, 4), default=0)
    ntb_sales = Column(Numeric(12, 2), default=0)
    new_to_brand_sales_pct = Column(Numeric(10, 4), default=0)
    visible_impressions = Column(Integer, default=0)
    __table_args__ = (
        Index("idx_fad_store_date", "store_id", "biz_date"),
        Index("idx_fad_asin", "asin"),
        # 组7 P-02：广告按 (店铺, 归属月) 聚合（admonth 三级归月）复合索引
        Index("idx_fad_store_month", "store_id", "period_month"),
    )


class FactFeeStorage(Base, _FactBase):
    """仓储费"""
    __tablename__ = "fact_fee_storage"
    asin = Column(String(50), default="")
    fnsku = Column(String(50), default="")
    product_name = Column(String(500), default="")
    fulfillment_center = Column(String(20), default="")
    month_of_charge = Column(String(20), default="", comment="收费月份 — 整月归属依据")
    estimated_monthly_storage_fee = Column(Numeric(12, 4), default=0)
    __table_args__ = (
        Index("idx_ffs_store_month", "store_id", "month_of_charge"),
        Index("idx_ffs_asin", "asin"),
    )


class FactFeeAgedStorage(Base, _FactBase):
    """超龄仓储费"""
    __tablename__ = "fact_fee_aged_storage"
    snapshot_date = Column(String(30), default="")
    sku = Column(String(100), default="")
    fnsku = Column(String(50), default="")
    asin = Column(String(50), default="")
    product_name = Column(String(500), default="")
    condition_val = Column(String(50), default="")
    qty_charged = Column(Integer, default=0)
    amount_charged = Column(Numeric(12, 4), default=0)
    surcharge_age_tier = Column(String(50), default="")
    __table_args__ = (
        Index("idx_ffas_store_snapshot", "store_id", "snapshot_date"),
    )


class FactFeeRemoval(Base, _FactBase):
    """移除费"""
    __tablename__ = "fact_fee_removal"
    request_date = Column(DateTime, nullable=True)
    order_id = Column(String(50), default="")
    order_type = Column(String(50), default="")
    service_speed = Column(String(50), default="")
    sku = Column(String(100), default="")
    fnsku = Column(String(50), default="")
    asin = Column(String(50), default="")
    disposition = Column(String(50), default="")
    shipped_quantity = Column(Integer, default=0)
    removal_fee = Column(Numeric(12, 4), default=0)
    __table_args__ = (
        Index("idx_ffr_store_date", "store_id", "request_date"),
        Index("idx_ffr_sku", "sku"),
    )


class FactFeeInboundConfig(Base, _FactBase):
    """入库配置费"""
    __tablename__ = "fact_fee_inbound_config"
    transaction_date = Column(DateTime, nullable=True)
    inbound_plan_id = Column(String(100), default="")
    fba_shipment_id = Column(String(50), default="")
    fnsku = Column(String(50), default="")
    asin = Column(String(50), default="")
    planned_inbound_service = Column(String(100), default="")
    actual_fee_segment = Column(String(100), default="")
    actual_received_qty = Column(Integer, default=0)
    total_fee = Column(Numeric(12, 4), default=0)
    __table_args__ = (
        Index("idx_ffic_store_date", "store_id", "transaction_date"),
    )


class FactFeeReturn(Base, _FactBase):
    """退货/退回处理费"""
    __tablename__ = "fact_fee_return"
    asin = Column(String(50), default="")
    fnsku = Column(String(50), default="")
    product_name = Column(String(500), default="")
    sku_sizetier = Column(String(50), default="")
    month_of_shipment = Column(String(20), default="")
    asin_shipped_units = Column(Integer, default=0)
    asin_returned_units = Column(Integer, default=0)
    sku_returned_units_charged = Column(Integer, default=0)
    sku_fee_per_unit = Column(Numeric(12, 4), default=0)
    sku_returns_fee = Column(Numeric(12, 4), default=0)
    month_of_charge = Column(String(20), default="", comment="收费月份 — 整月归属依据")
    __table_args__ = (
        Index("idx_ffr_store_month", "store_id", "month_of_charge"),
        Index("idx_ffr_asin", "asin"),
    )


class FactCashflowTransfer(Base, _FactBase):
    """资金流：提款（Transfer）— 不计入利润，单独统计"""
    __tablename__ = "fact_cashflow_transfer"
    biz_date = Column(DateTime, nullable=False)
    settlement_id = Column(String(50), default="")
    amount = Column(Numeric(14, 2), nullable=False, comment="提款金额（原币，负值=资金流出）")
    description = Column(String(500), default="")
    __table_args__ = (
        Index("idx_fct_store_date", "store_id", "biz_date"),
    )
