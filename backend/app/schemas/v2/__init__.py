"""v2 接口规范契约模型（组8.1，D-01）

用途：**契约文档与回归测试基准**（test_contract_regression.py 按模型断言响应键）。
不挂 response_model——别名键由 contract_alias 动态注入（LEGACY_CONTRACT_ALIAS 开关），
静态模型过滤会削掉它们；规范字段以本模型为准。

规范字段命名原则：保持现有"新口径"命名（sales_usd / net_profit_rmb / items），
仅下线重复别名（items↔data、sales_usd↔sales、current、total_ad_spend 等，
映射关系见 services/common/contract_alias.py）。
"""
from typing import Optional
from pydantic import BaseModel, Field


# ============ dashboard ============

class PreviousPeriod(BaseModel):
    """对比期（上月/上年）指标 — 规范命名（旧 total_* 键由别名层不再生成）"""
    net_profit_rmb: float
    sales_rmb: float
    order_count: int
    net_profit_rate: float
    refund_qty: int


class SummaryResponse(BaseModel):
    period_month: Optional[str]
    mode: str
    start: str
    end: str
    sales_usd: float
    sales_rmb: float
    qty: int
    refund_qty: int
    net_qty: int
    order_count: int
    ad_spend: float
    ad_spend_rmb: float
    ad_sales: float
    acos: float
    roas: float
    storage_fee: float
    storage_fee_rmb: float
    aged_storage_fee: float
    returns_fee: float
    removal_fee: float
    inbound_fee: float
    net_profit_rmb: float
    net_profit_rate: float
    cost_complete: Optional[bool]
    pending_cost_alerts: int
    previous: PreviousPeriod
    change_percent: dict


class TrendItem(BaseModel):
    period: str
    sales_rmb: float
    qty: int
    order_count: int
    net_profit_rmb: Optional[float]
    ad_spend_rmb: float
    storage_fee_rmb: float


class TrendResponse(BaseModel):
    items: list[TrendItem]
    dimension: str


class ProductDistributionItem(BaseModel):
    asin: str
    name: str
    sales_rmb: float
    qty: int
    share: float
    net_profit_rmb: Optional[float]
    has_snapshot: bool


class ProductDistributionResponse(BaseModel):
    period_month: Optional[str]
    total_sales_rmb: float
    items: list[ProductDistributionItem]


class FeeItem(BaseModel):
    name: str
    value: float


class FeeCompositionResponse(BaseModel):
    period_month: Optional[str]
    items: list[FeeItem]
    total_sales_rmb: float
    net_profit_rmb: float


class RefundRankingItem(BaseModel):
    asin: str
    sku: str
    sold: int
    returned: int
    refund_rate: Optional[float]
    refund_amount: float
    order_count: int
    return_rate_pct: float


class RefundRankingResponse(BaseModel):
    period_month: Optional[str]
    items: list[RefundRankingItem]


class StoreCompareItem(BaseModel):
    store_code: str
    store_name: str
    sales_rmb: float
    net_profit_rmb: float
    order_count: int
    profit_rate: float


class StoreCompareResponse(BaseModel):
    period_month: Optional[str]
    items: list[StoreCompareItem]


class CountryCompareItem(BaseModel):
    country: str
    sales_rmb: float
    order_count: int
    net_profit_rmb: Optional[float]
    net_profit_rate: Optional[float]
    has_snapshot: bool


class CountryCompareResponse(BaseModel):
    period_month: Optional[str]
    items: list[CountryCompareItem]


class TransferCountryRow(BaseModel):
    country: str
    usd: float
    rmb: float
    count: int


class TransferResponse(BaseModel):
    period_month: Optional[str]
    total: float
    count: int
    by_country: list[TransferCountryRow]
    total_rmb: float


# ============ query ============

class SalesRow(BaseModel):
    asin: str = ''
    sku: str = ''
    country: str = ''
    product_name: str = ''
    qty: int
    net_qty: int
    refund_qty: int
    refund_amount: float
    order_count: int
    sales_usd: float
    product_sales_rmb: float
    commission_usd: float
    fba_fee_usd: float
    ad_spend_usd: float
    storage_fee_usd: float
    aged_storage_fee_usd: float
    returns_fee_usd: float
    inbound_fee_usd: float
    removal_fee_usd: float
    product_cost_rmb: float
    freight_cost_rmb: float
    cost_rmb: Optional[float]
    freight_per_unit: Optional[float]
    net_amazon_usd: float
    net_profit_rmb: float
    net_profit_rate: float
    ad_spend_pct: float
    storage_fee_pct: float
    return_rate: float


class SalesTotals(BaseModel):
    net_qty: int
    product_sales_usd: float
    product_sales_rmb: float
    commission_usd: float
    fba_fee_usd: float
    ad_spend_usd: float
    net_profit_rmb: float


class SalesResponse(BaseModel):
    total: int
    items: list[SalesRow]
    totals: SalesTotals


class ProductSku(BaseModel):
    sku: str
    status: str
    note: str


class ProductRow(BaseModel):
    id: int
    store_code: str
    asin: str
    title: str
    status: str
    skus: list[ProductSku]


class ProductListResponse(BaseModel):
    total: int
    items: list[ProductRow]


class AdRow(BaseModel):
    asin: str
    sku: str
    country: str
    sales: float
    spend: float
    impressions: int
    clicks: int
    orders: int
    acos: float
    roas: float
    ctr: float
    cpc: float


class AdSummaryResponse(BaseModel):
    total_spend: float
    total_sales: float
    acos: float
    roas: float
    impressions: int
    clicks: int
    orders: int
    rows: int
    items: list[AdRow]


class FeeSummaryResponse(BaseModel):
    period_month: str
    storage_fee: float
    aged_storage_fee: float
    returns_fee: float
    removal_fee: float
    inbound_fee: float
    counts: dict


# ============ accounting ============

class TaskAccepted(BaseModel):
    """POST /accounting/calculate 受理响应（组6 异步契约）"""
    task_id: str
    status: str = Field(description="queued/running/success/failed/interrupted")
    poll_url: str


class TaskStatus(BaseModel):
    task_id: str
    task_type: str
    status: str
    stage: Optional[str] = Field(None, description="importing/fetching/calculating/saving/done")
    progress: int
    result: Optional[dict] = None
    snapshot: Optional[dict] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None