"""AI 对话 API（迁移自旧系统 routers/ai.py）— 带数据查询工具调用，可溯源
"""
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import get_current_user, require_module
from app.services.ai.client import chat, TOOL_DEFINITIONS, execute_tool

router = APIRouter()

SYSTEM_PROMPT = """你是 LMG 亚马逊多店铺 ERP 的数据分析助手。

规则：
1. 涉及具体数据时，必须调用工具查询，不要凭空猜测数字。
2. 回答用中文，简洁专业，给出关键数字。
3. 若数据缺失（如未核算、成本缺失），明确说明，不要臆造。
4. 利润口径：净利润 = net_amazon × 汇率 − 采购成本 − 头程运费 − (广告+仓储+退货+入库+移除)×汇率。
"""


class ChatRequest(BaseModel):
    """AI 对话请求：message 当前提问 + history 历史消息 + 页面筛选上下文"""
    message: str
    history: list[dict] = []
    store: str = ''
    country: str = ''
    year: int | None = None
    month: int | None = None


@router.post("/chat")
async def ai_chat(req: ChatRequest, db: Session = Depends(get_db),
                  user=Depends(require_module("ai"))):
    """AI 对话（单轮工具调用 + 汇总回答）"""
    messages = list(req.history) + [{"role": "user", "content": req.message}]
    # 页面筛选上下文 → 工具调用的默认参数（LLM 未指定时补齐）
    period_month = f"{req.year}-{req.month:02d}" if req.year and req.month else None
    ctx = []
    if req.store:
        ctx.append(f"店铺={req.store}")
    if req.country:
        ctx.append(f"站点={req.country}")
    if period_month:
        ctx.append(f"月份={period_month}")
    ctx_line = (f"\n\n用户当前筛选：{'，'.join(ctx)}。"
                "工具调用时若用户未明确指定店铺/月份，默认使用该筛选。") if ctx else ""
    try:
        # 第一轮：带工具让模型决定是否查询
        tools_desc = "\n".join(
            f"- {t['name']}: {t['description']} 参数: {json.dumps(t['input_schema'].get('properties', {}), ensure_ascii=False)}"
            for t in TOOL_DEFINITIONS)
        system = SYSTEM_PROMPT + f"\n\n可用工具：\n{tools_desc}\n\n" \
            "如需数据，请只输出一行 JSON：{\"tool\": \"工具名\", \"args\": {...}}；" \
            "如果不需要数据，直接回答。" + ctx_line
        first = await chat(messages, system=system, max_tokens=2000)
        tool_result = None
        text = first.strip()
        # 解析工具调用
        if '"tool"' in text:
            try:
                start = text.index('{')
                end = text.rindex('}') + 1
                call = json.loads(text[start:end])
                # 补默认参数：LLM 未指定时使用页面筛选上下文
                args = dict(call.get('args', {}) or {})
                if req.store and not args.get('store'):
                    args['store'] = req.store
                if req.country and not args.get('country'):
                    args['country'] = req.country
                if period_month and not args.get('period_month'):
                    args['period_month'] = period_month
                tool_result = execute_tool(db, call.get('tool', ''), args)
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content":
                                 f"工具 {call.get('tool')} 返回：\n{json.dumps(tool_result, ensure_ascii=False)}\n\n请基于此数据用中文回答用户问题。"})
                answer = await chat(messages, system=SYSTEM_PROMPT, max_tokens=4000)
                return {'answer': answer, 'tool_called': call.get('tool'),
                        'tool_result': tool_result}
            except (ValueError, json.JSONDecodeError):
                pass
        return {'answer': first, 'tool_called': None, 'tool_result': None}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 服务调用失败: {e}")


@router.get("/tools")
def list_tools(user=Depends(get_current_user)):
    """列出 AI 可调用的数据查询工具（前端展示用）"""
    return {'tools': [{'name': t['name'], 'description': t['description']}
                      for t in TOOL_DEFINITIONS]}
