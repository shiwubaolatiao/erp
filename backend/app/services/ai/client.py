"""AI 服务（迁移自旧系统 routers/ai.py + services/agent_tools.py）
保持与旧系统一致的 AI_PROVIDER=anthropic 兼容调用方式（httpx → AI_BASE_URL）。
"""
import json
import httpx
from typing import Optional
from sqlalchemy.orm import Session
from app.core.config import settings


def _headers() -> dict:
    """构造 AI 请求头（Anthropic 与 OpenAI 兼容两种协议）"""
    if settings.AI_PROVIDER == 'anthropic':
        return {
            'x-api-key': settings.AI_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }
    return {
        'Authorization': f'Bearer {settings.AI_API_KEY}',
        'content-type': 'application/json',
    }


def _endpoint() -> str:
    """拼接 AI 服务端点（Anthropic 用 /v1/messages，其它用 /chat/completions）"""
    base = settings.AI_BASE_URL.rstrip('/')
    if settings.AI_PROVIDER == 'anthropic':
        return f"{base}/v1/messages" if not base.endswith('/v1/messages') else base
    return f"{base}/chat/completions"


async def chat(messages: list[dict], system: str = "", max_tokens: Optional[int] = None) -> str:
    """统一 AI 调用入口；返回文本。失败抛异常由调用方处理。"""
    if not settings.AI_API_KEY:
        raise RuntimeError("未配置 AI_API_KEY")
    mt = max_tokens or settings.AI_MAX_TOKENS or 4096
    if settings.AI_PROVIDER == 'anthropic':
        payload = {
            'model': settings.AI_MODEL,
            'max_tokens': mt,
            'messages': messages,
        }
        if system:
            payload['system'] = system
        if settings.AI_THINKING_BUDGET:
            payload['thinking'] = {'type': 'enabled', 'budget_tokens': settings.AI_THINKING_BUDGET}
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(_endpoint(), headers=_headers(), json=payload)
            r.raise_for_status()
            data = r.json()
            parts = []
            for blk in data.get('content', []):
                if blk.get('type') == 'text':
                    parts.append(blk.get('text', ''))
            return ''.join(parts)
    else:
        payload = {'model': settings.AI_MODEL, 'max_tokens': mt, 'messages': messages}
        if system:
            payload['messages'] = [{'role': 'system', 'content': system}] + messages
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(_endpoint(), headers=_headers(), json=payload)
            r.raise_for_status()
            data = r.json()
            return data['choices'][0]['message']['content']


# ============ Agent 工具（数据查询工具集，供广告 Agent / AI 对话使用） ============

TOOL_DEFINITIONS = [
    {
        'name': 'query_sales_summary',
        'description': '查询指定店铺/月份的销售汇总（销售额、销量、订单数）',
        'input_schema': {
            'type': 'object',
            'properties': {
                'store': {'type': 'string', 'description': '店铺代码 LMG/MGK/MGT/STX/WEL'},
                'period_month': {'type': 'string', 'description': '月份 YYYY-MM'},
                'country': {'type': 'string', 'description': '站点代码如 UK/US'},
            },
            'required': ['store', 'period_month'],
        },
    },
    {
        'name': 'query_profit',
        'description': '查询指定店铺/月份的利润核算结果（净利润、净利率、成本是否完整）',
        'input_schema': {
            'type': 'object',
            'properties': {
                'store': {'type': 'string'},
                'period_month': {'type': 'string'},
            },
            'required': ['store', 'period_month'],
        },
    },
    {
        'name': 'query_ad_performance',
        'description': '查询广告花费与效果（花费、销售额、ACOS、ROAS）',
        'input_schema': {
            'type': 'object',
            'properties': {
                'store': {'type': 'string'},
                'period_month': {'type': 'string'},
                'country': {'type': 'string'},
            },
            'required': ['store', 'period_month'],
        },
    },
    {
        'name': 'query_cost_alerts',
        'description': '查询成本/运费缺失预警清单',
        'input_schema': {
            'type': 'object',
            'properties': {'store': {'type': 'string'}, 'period_month': {'type': 'string'}},
            'required': [],
        },
    },
]


def execute_tool(db: Session, name: str, args: dict) -> dict:
    """执行数据查询工具（只读）"""
    from app.models import Store, FactSalesTransaction, DwsProfitSnapshot, FactAdDaily, CostMissingAlert
    from sqlalchemy import func as sa_func
    from datetime import datetime
    from decimal import Decimal

    def month_bounds(pm):
        """YYYY-MM → [月初, 次月初) 的半开区间"""
        y, m = int(pm[:4]), int(pm[5:7])
        return datetime(y, m, 1), datetime(y + (m == 12), (m % 12) + 1, 1)

    store_obj = None
    if args.get('store'):
        store_obj = db.query(Store).filter(Store.store_code == args['store'].upper()).first()
        if not store_obj:
            return {'error': f"店铺 {args['store']} 不存在"}

    if name == 'query_sales_summary':
        start, end = month_bounds(args['period_month'])
        q = db.query(FactSalesTransaction).filter(
            FactSalesTransaction.biz_date >= start, FactSalesTransaction.biz_date < end,
            FactSalesTransaction.transaction_type.in_(('Order', 'Refund')))
        if store_obj:
            q = q.filter(FactSalesTransaction.store_id == store_obj.id)
        if args.get('country'):
            q = q.filter(FactSalesTransaction.country == args['country'])
        rows = q.all()
        sales = sum((Decimal(str(r.product_sales or 0)) if r.transaction_type == 'Order'
                     else -abs(Decimal(str(r.product_sales or 0))) for r in rows), Decimal(0))
        qty = sum(r.quantity or 0 for r in rows if r.transaction_type == 'Order')
        refund_qty = sum(abs(r.quantity or 0) for r in rows if r.transaction_type == 'Refund')
        orders = len({r.order_id for r in rows if r.order_id})
        return {'period_month': args['period_month'], 'sales': float(sales), 'qty': qty,
                'refund_qty': refund_qty, 'order_count': orders, 'rows': len(rows)}

    if name == 'query_profit':
        q = db.query(DwsProfitSnapshot).filter(
            DwsProfitSnapshot.period_month == args['period_month'])
        if store_obj:
            q = q.filter(DwsProfitSnapshot.store_id == store_obj.id)
        snap = q.order_by(DwsProfitSnapshot.version.desc()).first()
        if not snap:
            return {'error': '该月尚未核算，请先执行核算'}
        return {
            'period_month': args['period_month'], 'version': snap.version,
            'net_profit_rmb': float(snap.net_profit_rmb or 0),
            'net_profit_rate': float(snap.net_profit_rate or 0),
            'product_sales_rmb': float(snap.product_sales_rmb or 0),
            'cost_complete': snap.cost_complete,
            'cost_incomplete_skus': snap.cost_incomplete_skus,
        }

    if name == 'query_ad_performance':
        from app.models import ImportBatch
        start, end = month_bounds(args['period_month'])
        q = db.query(FactAdDaily)
        if store_obj:
            q = q.filter(FactAdDaily.store_id == store_obj.id)
        if args.get('country'):
            q = q.filter(FactAdDaily.country == args['country'])
        batch_ids = [b.id for b in db.query(ImportBatch).filter(
            sa_func.date_format(ImportBatch.imported_at, '%Y-%m') == args['period_month']).all()]
        rows = [r for r in q.all()
                if (r.biz_date and start <= r.biz_date < end)
                or (not r.biz_date and r.batch_id in batch_ids)]
        spend = sum((Decimal(str(r.spend or 0)) for r in rows), Decimal(0))
        sales = sum((Decimal(str(r.sales or 0)) for r in rows), Decimal(0))
        return {
            'period_month': args['period_month'], 'spend': float(spend), 'sales': float(sales),
            'acos': float((spend / sales).quantize(Decimal('0.0001'))) if sales else 0,
            'roas': float((sales / spend).quantize(Decimal('0.0001'))) if spend else 0,
        }

    if name == 'query_cost_alerts':
        q = db.query(CostMissingAlert).filter(CostMissingAlert.status == 'pending')
        if store_obj:
            q = q.filter(CostMissingAlert.store_id == store_obj.id)
        if args.get('period_month'):
            q = q.filter(CostMissingAlert.period == args['period_month'])
        alerts = q.limit(100).all()
        return {'count': len(alerts), 'items': [
            {'asin': a.asin, 'sku': a.sku, 'country': a.country,
             'period': a.period, 'missing_type': a.missing_type} for a in alerts]}

    return {'error': f'未知工具: {name}'}
