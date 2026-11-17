"""利润计算引擎 — 从旧系统 services/profit.py 原样移植（公式一字不改）

★ 这是本次重构的"不动核心"：公式、字段口径、汇率三级回退逻辑与旧系统完全一致。
★ 黄金测试（tests/test_profit_golden.py）用真实报表数值锁定该公式，任何改动必须先过测试。

移植说明（仅适配，不改语义）：
- Decimal 精度/quantize 精度与旧版完全一致（0.01 / 0.0001）
- DEFAULT_RATES 默认汇率表逐字保留
- get_exchange_rate 三级回退：店铺专属(精确月) → 店铺专属(最近月) → 默认值
  ★ 注意：新系统汇率表(exchange_rate)按月维护，回退逻辑保留"任意最近月"行为以兼容历史数据
"""
from decimal import Decimal

# 默认汇率（与旧系统完全一致，逐字保留）
DEFAULT_RATES = {
    'US': '6.8', 'UK': '9.0', 'DE': '7.5', 'FR': '7.5', 'ES': '7.5', 'IT': '7.5',
    'NL': '7.5', 'SE': '0.65', 'BE': '7.5', 'IE': '7.5', 'CA': '5.0', 'MX': '0.4',
    'AE': '1.85', 'SA': '1.81', 'AU': '4.8', 'PL': '1.75',
}


def get_exchange_rate(db, country_code, store_id=None, period_month=None, ExchangeRateModel=None) -> Decimal:
    """获取汇率（三级回退，语义与旧版一致）

    店铺专属（精确月份）→ 店铺专属（任意月份，取最近）→ 全局（精确月份）→ 默认值

    兼容说明：
    - 新系统汇率表维度为 (store_id, country_code, period_month)；
    - 旧版对"店铺专属(任意月份)"取 year_month 降序第一条，此处保留该行为；
    - 新增"全局精确月份"档位（旧系统的全局汇率不在表中，新系统支持无店铺的全局月度汇率）。
    """
    ym = period_month
    if ym and store_id and ExchangeRateModel is not None:
        er = db.query(ExchangeRateModel).filter(
            ExchangeRateModel.country_code == country_code,
            ExchangeRateModel.period_month == ym,
            ExchangeRateModel.store_id == store_id,
        ).first()
        if er and er.rate_to_rmb and Decimal(str(er.rate_to_rmb)) != 0:
            return Decimal(str(er.rate_to_rmb))
    # 全局（无店铺）精确月份
    if ym and ExchangeRateModel is not None:
        er = db.query(ExchangeRateModel).filter(
            ExchangeRateModel.country_code == country_code,
            ExchangeRateModel.period_month == ym,
            ExchangeRateModel.store_id.is_(None),
        ).first()
        if er and er.rate_to_rmb and Decimal(str(er.rate_to_rmb)) != 0:
            return Decimal(str(er.rate_to_rmb))
    # 店铺专属任意月份（最近）
    if store_id and ExchangeRateModel is not None:
        er = db.query(ExchangeRateModel).filter(
            ExchangeRateModel.country_code == country_code,
            ExchangeRateModel.store_id == store_id,
        ).order_by(ExchangeRateModel.period_month.desc()).first()
        if er and er.rate_to_rmb and Decimal(str(er.rate_to_rmb)) != 0:
            return Decimal(str(er.rate_to_rmb))
    return Decimal(DEFAULT_RATES.get(country_code, '6.8'))


def compute_net_profit(
    product_sales: Decimal,
    product_sales_tax: Decimal,
    postage_credits: Decimal,
    shipping_credits_tax: Decimal,
    gift_wrap_credits: Decimal,
    giftwrap_credits_tax: Decimal,
    promo_rebate: Decimal,
    promo_rebate_tax: Decimal,
    marketplace_withheld_tax: Decimal,
    selling_fee: Decimal,
    fba_fee: Decimal,
    adjustment: Decimal,
    exchange_rate: Decimal,
    product_cost_rmb: Decimal,
    freight_cost_rmb: Decimal,
    ad_spend_usd: Decimal,
    storage_fee_usd: Decimal,
    returns_fee_usd: Decimal,
    inbound_fee_usd: Decimal,
    removal_fee_usd: Decimal,
) -> Decimal:
    """统一利润公式（唯一入口）— 与旧版逐字一致

    net_amazon = product_sales + product_sales_tax + postage_credits + shipping_credits_tax
               + gift_wrap_credits + giftwrap_credits_tax + promo_rebate + promo_rebate_tax
               + marketplace_withheld_tax + selling_fee + fba_fee + adjustment

    net_profit = net_amazon * exchange_rate
               - product_cost_rmb - freight_cost_rmb
               - ad_spend * er - storage * er - returns * er - inbound * er - removal * er
    """
    net_amazon = (
        product_sales + product_sales_tax + postage_credits + shipping_credits_tax
        + gift_wrap_credits + giftwrap_credits_tax + promo_rebate + promo_rebate_tax
        + marketplace_withheld_tax + selling_fee + fba_fee + adjustment
    )
    net = (
        net_amazon * exchange_rate
        - product_cost_rmb - freight_cost_rmb
        - ad_spend_usd * exchange_rate - storage_fee_usd * exchange_rate
        - returns_fee_usd * exchange_rate - inbound_fee_usd * exchange_rate
        - removal_fee_usd * exchange_rate
    ).quantize(Decimal("0.01"))
    return net


def compute_net_amazon(
    product_sales: Decimal,
    product_sales_tax: Decimal,
    postage_credits: Decimal,
    shipping_credits_tax: Decimal,
    gift_wrap_credits: Decimal,
    giftwrap_credits_tax: Decimal,
    promo_rebate: Decimal,
    promo_rebate_tax: Decimal,
    marketplace_withheld_tax: Decimal,
    selling_fee: Decimal,
    fba_fee: Decimal,
    adjustment: Decimal,
) -> Decimal:
    """net_amazon 原币合计（从公式中抽出，供溯源/复算展示）"""
    return (
        product_sales + product_sales_tax + postage_credits + shipping_credits_tax
        + gift_wrap_credits + giftwrap_credits_tax + promo_rebate + promo_rebate_tax
        + marketplace_withheld_tax + selling_fee + fba_fee + adjustment
    )
