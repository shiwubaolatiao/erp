"""接口别名契约集中管理（组8.1，D-01/D-02）

原则：**规范字段计算一次，别名由规范字段机械映射**——各接口不再手写双份 dict。
旧字段别名（items↔data、sales_usd↔sales、current/previous、total_ad_spend 等）
全部收敛到本模块；`LEGACY_CONTRACT_ALIAS=false` 时别名不再注入（下线开关，
spec 4.5 R2 三阶段渐进的最后一步）。

用法：
    from app.services.common.contract_alias import alias_row, inject_legacy

    # 行级：row 组装只写规范字段，别名机械补齐
    row = alias_row({'asin': a, 'sales_usd': 1.0, 'country': 'UK'}, 'query/sales_row')

    # 响应级：payload 只含规范字段，旧结构视图由别名层生成
    return inject_legacy({'items': items}, 'dashboard/trend')
"""
from app.core.config import settings


# ============================================================
# 行级别名规范：{别名键: 规范键 或 派生函数(row)->值}
# ============================================================
ROW_ALIASES: dict[str, dict] = {
    # /query/sales 明细行（DataQuery 页旧字段）
    'query/sales_row': {
        'sales': 'sales_usd',
        'product_sales_usd': 'sales_usd',
        'country_code': 'country',
        'country_name': 'country',
    },
    # /query/products 行（Admin 页旧字段）
    'query/product_row': {
        'code': 'store_code',
        'name': lambda r: r.get('title') or r.get('asin') or '',
    },
}


def alias_row(row: dict, spec: str) -> dict:
    """就地给行注入别名字段（开关关闭时原样返回）"""
    if not settings.LEGACY_CONTRACT_ALIAS:
        return row
    for key, src in ROW_ALIASES.get(spec, {}).items():
        if callable(src):
            row[key] = src(row)
        elif src in row:
            row[key] = row[src]
    return row


def alias_rows(rows: list[dict], spec: str) -> list[dict]:
    """批量行别名注入"""
    for r in rows:
        alias_row(r, spec)
    return rows


# ============================================================
# 响应级别名规范：payload（规范字段）→ 旧结构视图键
# ============================================================
def _r2(v) -> float:
    return round(float(v), 2)


def _summary(p: dict) -> dict:
    """dashboard/summary：current/previous/change_percent 旧视图 ← 规范字段（total_* 旧命名）"""
    prev = p.get('previous') or {}
    chg = p.get('change_percent') or {}
    return {
        'current': {
            'total_net_profit_rmb': p['net_profit_rmb'],
            'total_product_sales_rmb': p['sales_rmb'],
            'total_order_count': p['order_count'],
            'total_order_qty': p['qty'],
            'avg_net_profit_rate': p['net_profit_rate'],
            'total_ad_spend_rmb': p['ad_spend_rmb'],
            'total_storage_fee_rmb': p['storage_fee_rmb'],
            'total_refund_qty': p['refund_qty'],
        },
        'previous': {
            'total_net_profit_rmb': prev['net_profit_rmb'],
            'total_product_sales_rmb': prev['sales_rmb'],
            'total_order_count': prev['order_count'],
            'avg_net_profit_rate': prev['net_profit_rate'],
            'total_refund_qty': prev['refund_qty'],
        },
        'change_percent': {
            'total_net_profit_rmb': chg.get('net_profit_rmb'),
            'total_product_sales_rmb': chg.get('sales_rmb'),
            'total_order_count': chg.get('order_count'),
            'avg_net_profit_rate': chg.get('net_profit_rate'),
            'total_refund_qty': chg.get('refund_qty'),
        },
    }


def _trend(p: dict) -> dict:
    """dashboard/trend：data 视图 ← items（label/net_profit/sales/orders/...）"""
    return {'data': [{
        'label': it['period'],
        'net_profit': it['net_profit_rmb'] or 0,
        'sales': it['sales_rmb'],
        'orders': it['order_count'],
        'ad_spend': it['ad_spend_rmb'],
        'storage_fee': it['storage_fee_rmb'],
    } for it in p.get('items', [])]}


def _product_distribution(p: dict) -> dict:
    """dashboard/product-distribution：data 视图 ← items"""
    return {'data': [{
        'name': it['name'], 'asin': it['asin'], 'sales_rmb': it['sales_rmb'],
        'order_count': it['qty'], 'net_profit': it['net_profit_rmb'],
    } for it in p.get('items', [])]}


def _fee_composition(p: dict) -> dict:
    """dashboard/fee-composition：sales/expenses/net_profit ← 规范字段
    ★ expenses 键缺失会导致旧看板整页白屏（历史教训），务必保留映射。
    """
    return {
        'sales': p['total_sales_rmb'],
        'expenses': p['items'],
        'net_profit': p['net_profit_rmb'],
    }


def _refund_ranking(p: dict) -> dict:
    """dashboard/refund-ranking：data 视图 ← items"""
    return {'data': [{
        'name': it['asin'], 'asin': it['asin'], 'sku': it['sku'],
        'refund_amount': _r2(it['refund_amount']),
        'refund_count': it['order_count'], 'refund_qty': it['returned'],
        'order_count': it['order_count'], 'return_rate': it['return_rate_pct'],
        'qty_per_order': round(it['returned'] / max(it['order_count'], 1), 2),
    } for it in p.get('items', [])]}


def _store_compare(p: dict) -> dict:
    """dashboard/store-compare：data 视图 ← items（code/name/net_profit 旧命名）"""
    return {'data': [{
        'code': it['store_code'], 'name': it['store_name'],
        'sales_rmb': it['sales_rmb'], 'net_profit': it['net_profit_rmb'],
        'order_count': it['order_count'], 'profit_rate': it['profit_rate'],
    } for it in p.get('items', [])]}


def _country_compare(p: dict) -> dict:
    """dashboard/country-compare：data 视图 ← items"""
    return {'data': [{
        'code': it['country'], 'name': it['country'],
        'sales_rmb': it['sales_rmb'], 'net_profit': it['net_profit_rmb'],
        'order_count': it['order_count'],
        'profit_rate': round(it['net_profit_rate'] * 100, 1)
        if it['net_profit_rate'] is not None else None,
    } for it in p.get('items', [])]}


def _transfer(p: dict) -> dict:
    """dashboard/transfer：total_usd/by_type ← 规范字段（旧资金流卡片）"""
    return {'total_usd': _r2(p['total']), 'by_type': []}


def _query_sales(p: dict) -> dict:
    """query/sales：data 视图 ← items（DataQuery 页读 res.data.data）"""
    return {'data': p['items']}


def _query_products(p: dict) -> dict:
    """query/products：data 视图 ← items（Admin 页读 res.data.data）"""
    return {'data': p['items']}


def _query_ad(p: dict) -> dict:
    """query/ad：total_ad_spend/avg_acos/avg_roas/total/data ← 规范字段
    （Advertising 页直接读这三个汇总别名与 data 行列表）
    """
    return {
        'total_ad_spend': p['total_spend'],
        'avg_acos': round(p['acos'] * 100, 2),
        'avg_roas': round(p['roas'], 2),
        'total': p['rows'],
        'data': p['items'],
    }


PAYLOAD_ALIASES: dict[str, callable] = {
    'dashboard/summary': _summary,
    'dashboard/trend': _trend,
    'dashboard/product-distribution': _product_distribution,
    'dashboard/fee-composition': _fee_composition,
    'dashboard/refund-ranking': _refund_ranking,
    'dashboard/store-compare': _store_compare,
    'dashboard/country-compare': _country_compare,
    'dashboard/transfer': _transfer,
    'query/sales': _query_sales,
    'query/products': _query_products,
    'query/ad': _query_ad,
}


def inject_legacy(payload: dict, spec: str) -> dict:
    """给响应 payload 注入旧字段别名（开关关闭时原样返回——下线即删别名）"""
    fn = PAYLOAD_ALIASES.get(spec)
    if fn is None or not settings.LEGACY_CONTRACT_ALIAS:
        return payload
    payload.update(fn(payload))
    return payload