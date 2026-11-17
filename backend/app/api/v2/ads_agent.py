"""广告分析 Agent —— 会话管理 + 对话（含流式）+ 文件上传解析 + 数据查询

接口前缀: /api/v2/ads-agent

【来源与适配说明】（维护必读）
  本文件**移植自旧系统** `lmg-platform/backend/routers/ads_agent.py`（1079 行），
  保留其全部业务能力与 SYSTEM_PROMPT（广告操盘手提示词是长期调优结果，勿随意改动），
  仅做以下适配：

   1. 模型引用：旧 `DimStore/DimTime/MonthlySummary/DimProduct/DimProductCost` →
      新 `Store/DwsProfitSnapshot`（新版无 dim_* 维度表）
   2. `_query_lmg_data()`：数据源从旧 `monthly_summary` 改为新**核算快照**
      `dws_profit_snapshot`（每个 店铺+月份 取最新版本），并新增"成本缺失"提示
   3. 导入路径：`services.agent_tools` → `app.services.ads.agent_tools`；
      `services.file_parser` → `app.services.ads.file_parser`
   4. 鉴权：`routers.auth` → `app.core.security`（require_module / get_current_user）

【涉及的数据表】
  ads_session / ads_message / ads_data_record / ads_action_item
  （会话、消息、上传记录、行动项；均在 lmg_erp 库）

【会话记忆】保留最近 ADS_MEMORY_ROUNDS 轮（默认 40 轮 = 80 条消息），见 .env。
"""
import os
import io
import json
import asyncio
import hashlib
import csv as csv_mod
import logging
from datetime import datetime, date

import httpx
import pandas as pd

from app.services.ads.agent_tools import TOOL_DEFINITIONS, execute_tool, resolve_report_filename

logger = logging.getLogger("ads_agent")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setLevel(logging.DEBUG)
    _h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s | %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_h)
from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File, Form, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel

from app.core.database import get_db
from app.core.config import settings
from app.models import (
    AdsSession, AdsMessage, AdsDataRecord, AdsActionItem,
    Store, Site, DwsProfitSnapshot, DwsProfitSnapshotItem, ProductCostHistory,
)
from app.core.security import require_module, get_current_user

router = APIRouter()
_MOD = [Depends(require_module("ads_agent"))]

# ===== LLM 配置（组 9.2 D-06：统一从 config.settings 读取，禁止模块级 os.getenv 重复读取）=====


def _resolve_model(requested: str = None) -> str:
    """校验用户请求的模型，不在白名单内则回退默认模型。"""
    if requested and requested in settings.AI_MODEL_LIST:
        return requested
    return settings.AI_MODEL

SYSTEM_PROMPT = """你是 LMG 跨境电商数据平台的资深亚马逊广告操盘手，服务跨境卖家。
你懂亚马逊广告机制（SP/SB、竞价、匹配、广告位），懂利润结构，能用数据快速定位浪费并给出可执行动作。
回复专业、直接、有数据支撑、可执行。

## 决策原则（刻在骨子里）
1. 止损优先：高花费零转化/亏损单元先止血（否定/降竞价/暂停），再谈优化。
2. 看累计不看单日：用一周/一月的累计数据判断，不被单日波动带节奏。
3. 广告效率服从利润：ACOS 高低要对比盈亏平衡线（目标ACOS≈毛利率），不是机械比大小。
4. 先结论后细节：每段先给结论，再给证据，最后给动作。

## 数据获取（两条来源，只用工具，不做任何心算）
- 平台库（query_database / get_latest_metrics）：**只有月度汇总**（按自然月），用于月度/趋势/利润/环比。
- 本地 unified 广告报表库（list_amazon_reports / query_amazon_ads）：**周/日粒度的唯一来源**，按周归档。
- **硬规则：用户问今天/昨天/上周/本周/最近7天等周/日粒度时，必须用本地 unified 报表库，不得用平台库月度汇总冒充。**
- **本地只读、绝不下载**：没有归档就如实说“本地最新只到 X”，不得触发或声称任何下载。

## 分析流程（由粗到细，按需下钻）
1. list_amazon_reports 先确认本地有哪些店/类型/时间的归档；
2. query_amazon_ads 先查 campaigns（整体分级/总览）；
3. 按问题下钻：位置效率→placement(group_by=placement)；ASIN表现→products；投放结构→targeting；烧钱词/否定词→search_term；
4. 涉及多币种：工具已按币种分组，直接引用分币种指标；GBP=英国/EUR=欧元区/USD=美国等，单站点用 currency 参数筛。
5. 结论格式：每个发现都按【问题】【证据】【影响】【动作】写；动作用 P0立即/P1今天/P2本周 分优先级。

## 关键数据口径（务必遵守）
- 所有求和/ACOS/CPC/CVR/ROAS 由 query_amazon_ads 服务端算好，你只读结果、绝不自己算，绝不读 CSV 原文。
- 不同币种金额**不可直接相加**；如需合并口径，只能按币种分别给出。
- 广告位(placement)文件是“拆行”结构：0 单行不一定没单、0 花费行不一定没花；工具已按实体合并，解释时不要拿单行 0 值说事。
- 受众(audience)本地几乎无数据（多数 NODATA / 每期仅 1 行聚合），受众类问题如实说“暂无有效数据”，不要编造。
- 光环/长期销售列不计入主 ACOS（主口径=7 天归因的销售额）；如需可单列为“附加价值”备注。
- 周度 TACoS/自然销售/利润无周粒度数据源，只答月度口径（平台库）。

## 分档阈值（诊断时统一用）
- 活动/词/ASIN 按 ROAS 分档：>6 优秀 / 4~6 合格 / 2~4 预警 / <2 低效 / 有花费0单=无效。
- “0转化浪费”默认单周单实体花费 ≥ 2 才算；低于该线不报为浪费项。

## 输出格式（决策化，不吐原始表）
- TL;DR 一句话核心结论（整体花费/销售/订单/ACOS 是否健康）；
- 问题清单：按浪费金额降序，每条=目标名+浪费金额+原因+动作；
- 机会清单：按增量潜力排序，每条=目标名+预计提升+动作；
- 可执行动作清单：可直接复制到亚马逊后台（否定词/调价/加预算/暂停），必要时用 write_report 生成 Word/Excel 供下载。

## 回答要求
- Markdown；数字带单位；对比用表格；中文。
- 数据不足先取数（list_amazon_reports/query_amazon_ads），不要直接说“数据不足”。
- 需要分析文档时用 write_report（默认 Word），只给下载链接不粘正文。
- 上传文件分析：用户上传 CSV/Excel 要求分析时，对话必须包含完整分析正文（概览表 + 问题 + P0/P1/P2 建议），不得只回“已生成报告”。
"""


# ===== 多语言列名映射 =====
COLUMN_MAP = {
    'search_term':    ['search term', '搜索词', 'suchbegriff', 'terme de recherche', 'término de búsqueda'],
    'campaign_name':  ['campaign name', '广告活动', 'kampagne', 'campagne', 'campaña'],
    'ad_group_name':  ['ad group name', '广告组', 'anzeigengruppe'],
    'keyword':        ['keyword', '关键词', 'mot-clé', 'palabra clave'],
    'match_type':     ['match type', '匹配类型', 'übereinstimmungstyp'],
    'clicks':         ['clicks', '点击量', '点击', 'klicks', 'clics'],
    'impressions':    ['impressions', '展示量', '展示', 'impressionen', 'impresiones'],
    'spend':          ['spend', '花费', '支出', 'ausgaben', 'dépenses', 'gasto'],
    'sales':          ['7 day total sales', '销售额', '7天总销售额', 'verkäufe', 'ventes', 'ventas', 'sales'],
    'orders':         ['7 day total orders', '订单数', '7天总订单数', 'bestellungen', 'commandes', 'pedidos', '# of orders', 'orders'],
    'asin':           ['asin', '投放的asin', '投放的asins'],
    'placement':      ['placement', '广告位'],
    'daily_budget':   ['daily budget', '日预算', '预算'],
    'bid':            ['bid', '竞价', '建议竞价'],
    'ctr':            ['ctr', '点击率'],
    'cpc':            ['cpc', '每次点击费用'],
    'acos':           ['acos', 'aco'],
    'roas':           ['roas'],
}

FILE_TYPE_DETECT = {
    'search_term':    ['search_term', 'spend'],
    'campaign':       ['campaign_name', 'spend'],
    'keyword':        ['keyword', 'spend'],
    'placement':      ['placement', 'spend'],
    'product_target': ['asin', 'spend'],
}


# ============================================================
# 辅助函数
# ============================================================

def _system_with_date() -> str:
    """在系统提示词前注入当前日期，帮助模型推算"今天/昨天/上周/最近"等时间。"""
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    now = datetime.now()
    return f"今天是 {now.strftime('%Y-%m-%d')}（{weekdays[now.weekday()]}）。\n" + SYSTEM_PROMPT


async def _run_tool(tool_name: str, tool_input: dict, db) -> str:
    """在后台线程执行工具，避免长耗时工具（ensure/ask 轮询可达 150 秒）阻塞事件循环，
    否则服务在下载期间会卡死所有请求（健康检查、其他会话都超时）。"""
    if not db:
        return "[数据库不可用]"
    return await asyncio.to_thread(execute_tool, tool_name, tool_input, db)


async def _call_llm(system: str, messages: list, db: Session = None, model: str = None) -> str:
    """调用 LLM，支持 tool use（函数调用）多轮循环。"""
    if not settings.AI_API_KEY:
        return "AI 未配置，请在 .env 中设置 AI_API_KEY"

    model = _resolve_model(model)
    tools = TOOL_DEFINITIONS if db is not None else []
    max_tool_rounds = 20
    last_text = ""
    logger.info(f"LLM 调用开始: model={model} tools={len(tools)} msgs={len(messages)} provider={settings.AI_PROVIDER}")

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            for _round in range(max_tool_rounds):
                if settings.AI_PROVIDER == "anthropic":
                    payload = {
                        "model": model,
                        "max_tokens": settings.AI_MAX_TOKENS,
                        "system": system,
                        "messages": messages,
                    }
                    # 只在第一轮开启 thinking；tool use 后续轮次关闭，避免 DeepSeek 只思考不输出
                    if _round == 0:
                        payload["thinking"] = {"type": "enabled", "budget_tokens": settings.AI_THINKING_BUDGET}
                    if tools:
                        payload["tools"] = [{
                            "name": t["name"],
                            "description": t["description"],
                            "input_schema": t["input_schema"],
                        } for t in tools]

                    resp = await client.post(
                        f"{settings.AI_BASE_URL}/v1/messages",
                        headers={"x-api-key": settings.AI_API_KEY, "Content-Type": "application/json",
                                 "anthropic-version": "2023-06-01"},
                        json=payload,
                    )
                    data = resp.json()

                    # 检查 API error
                    if "error" in data:
                        err = data["error"]
                        logger.error(f"API ERROR: {json.dumps(err, ensure_ascii=False)[:300]}")
                        return f"AI 调用失败：{err.get('message', err)}"

                    # 检查是否有 tool_use
                    tool_calls = []
                    text_parts = []
                    for block in data.get("content", []):
                        if block.get("type") == "tool_use":
                            tool_calls.append(block)
                        elif block.get("type") == "text":
                            text_parts.append(block.get("text", ""))

                    logger.info(f"round={_round} tool_calls={len(tool_calls)} text_parts={len(text_parts)} stop={data.get('stop_reason')}")
                    if text_parts:
                        last_text = text_parts[0]

                    if not tool_calls:
                        # 没有工具调用，返回文本
                        if text_parts:
                            return text_parts[0]
                        all_types = [b.get("type") for b in data.get("content", [])]
                        logger.warning(f"无 tool_calls 也无 text，content types: {all_types} stop={data.get('stop_reason')}")
                        if data.get("stop_reason") == "max_tokens":
                            return "（AI 思考超时，请重试）"
                        return "AI 返回为空"

                    # 构造下一轮 assistant 消息：
                    # - 保留 text 块（模型边思考边调工具的过渡文本）
                    # - 去掉 thinking 块（避免 API 报错）
                    # - 保留 tool_use 块
                    assistant_content = []
                    for block in data.get("content", []):
                        if block.get("type") == "text":
                            assistant_content.append({"type": "text", "text": block.get("text", "")})
                        elif block.get("type") == "tool_use":
                            assistant_content.append({
                                "type": "tool_use",
                                "id": block["id"],
                                "name": block["name"],
                                "input": block.get("input", {}),
                            })
                    messages.append({"role": "assistant", "content": assistant_content})

                    # 所有 tool_result 必须放在紧随其后的一条 user 消息里
                    # （DeepSeek Anthropic 兼容端点严格校验：每个 tool_use 的下一条消息必须含对应 tool_result）
                    result_blocks = []
                    for tc in tool_calls:
                        tool_name = tc["name"]
                        tool_input = tc.get("input", {})
                        logger.info(f"工具调用: {tool_name} input={json.dumps(tool_input, ensure_ascii=False)[:200]}")

                        tool_result = await _run_tool(tool_name, tool_input, db)
                        logger.info(f"工具结果: {tool_result[:200]}")

                        result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": tool_result[:8000],
                        })
                    messages.append({"role": "user", "content": result_blocks})

                    # 继续下一轮，让 LLM 看到工具结果后继续
                    continue

                else:
                    # OpenAI compatible
                    payload = {
                        "model": model,
                        "messages": [{"role": "system", "content": system}] + messages,
                        "max_tokens": settings.AI_MAX_TOKENS,
                    }
                    if tools:
                        payload["tools"] = [{
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "description": t["description"],
                                "parameters": t["input_schema"],
                            }
                        } for t in tools]

                    resp = await client.post(
                        f"{settings.AI_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {settings.AI_API_KEY}", "Content-Type": "application/json"},
                        json=payload,
                    )
                    data = resp.json()
                    msg = data.get("choices", [{}])[0].get("message", {})
                    tool_calls = msg.get("tool_calls", [])

                    if not tool_calls:
                        return msg.get("content", "AI 返回为空")

                    messages.append(msg)
                    for tc in tool_calls:
                        func = tc["function"]
                        tool_name = func["name"]
                        tool_input = json.loads(func["arguments"])
                        logger.info(f"工具调用: {tool_name}")

                        tool_result = await _run_tool(tool_name, tool_input, db)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_result[:8000],
                        })
                    continue

            # 轮次上限：优先返回模型最后一段文本
            if last_text:
                return last_text
            return "（工具调用轮次已达上限，请简化请求）"

    except Exception as e:
        logger.exception("LLM 调用失败")
        return f"AI 调用失败：{e}"


async def _call_llm_stream(system: str, messages: list, db: Session = None, model: str = None):
    """流式调用 LLM（SSE 事件），支持 tool use 多轮循环。

    每个事件为 dict，序列化为 SSE 的 data 行：
      {"type": "thinking", "delta": "..."}       思考过程增量
      {"type": "text", "delta": "..."}           回答文本增量
      {"type": "tool_call", "name": ..., "input": ...}   模型请求调用工具
      {"type": "tool_result", "name": ..., "preview": ...} 工具执行完成
      {"type": "report", "filename": ..., "url": ..., "content": ...} 报告已生成
      {"type": "done", "reply": ..., "usage": ...}        整体结束
      {"type": "error", "message": "..."}
    """
    if not settings.AI_API_KEY:
        yield {"type": "error", "message": "AI 未配置，请在 .env 中设置 AI_API_KEY"}
        return

    model = _resolve_model(model)
    tools = TOOL_DEFINITIONS if db is not None else []
    max_tool_rounds = 20
    full_text = []          # 所有轮次的文本增量（含过渡说明），最终 reply
    last_usage = {}

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            for _round in range(max_tool_rounds):
                payload = {
                    "model": model,
                    "max_tokens": settings.AI_MAX_TOKENS,
                    "system": system,
                    "messages": messages,
                    "stream": True,
                }
                # 只在第一轮开启 thinking 预算；后续轮次关闭，避免 DeepSeek 只思考不输出
                if _round == 0:
                    payload["thinking"] = {"type": "enabled", "budget_tokens": settings.AI_THINKING_BUDGET}
                if tools:
                    payload["tools"] = [{
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["input_schema"],
                    } for t in tools]

                logger.info(f"流式 LLM round={_round} msgs={len(messages)}")

                blocks = []       # 本轮 content blocks（thinking/text/tool_use）
                stop_reason = None

                async with client.stream(
                    "POST",
                    f"{settings.AI_BASE_URL}/v1/messages",
                    headers={
                        "x-api-key": settings.AI_API_KEY,
                        "Content-Type": "application/json",
                        "anthropic-version": "2023-06-01",
                    },
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        try:
                            err = json.loads(body).get("error", {})
                            msg = err.get("message", body[:300])
                        except Exception:
                            msg = body[:300]
                        logger.error(f"API ERROR: {msg}")
                        yield {"type": "error", "message": f"AI 调用失败：{msg}"}
                        return

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        try:
                            ev = json.loads(raw)
                        except Exception:
                            continue
                        etype = ev.get("type")

                        if etype == "content_block_start":
                            cb = ev.get("content_block", {})
                            blocks.append({
                                "type": cb.get("type"),
                                "id": cb.get("id"),
                                "name": cb.get("name"),
                                "input": cb.get("input") or {},
                                "input_json": "",
                                "text": "",
                            })
                        elif etype == "content_block_delta":
                            delta = ev.get("delta", {})
                            dt = delta.get("type")
                            if not blocks:
                                continue
                            cur = blocks[-1]
                            if dt == "thinking_delta":
                                cur["text"] += delta.get("thinking", "")
                                yield {"type": "thinking", "delta": delta.get("thinking", "")}
                            elif dt == "text_delta":
                                cur["text"] += delta.get("text", "")
                                full_text.append(delta.get("text", ""))
                                yield {"type": "text", "delta": delta.get("text", "")}
                            elif dt == "input_json_delta":
                                cur["input_json"] += delta.get("partial_json", "")
                            # signature_delta 忽略
                        elif etype == "message_delta":
                            stop_reason = ev.get("delta", {}).get("stop_reason")
                            if ev.get("usage"):
                                last_usage = ev["usage"]
                        elif etype == "error":
                            yield {"type": "error", "message": ev.get("error", {}).get("message", "流式错误")}
                            return

                tool_calls = [b for b in blocks if b["type"] == "tool_use"]
                text_parts = [b["text"] for b in blocks if b["type"] == "text"]
                logger.info(f"stream round={_round} tool_calls={len(tool_calls)} "
                            f"text_chars={sum(len(t) for t in text_parts)} stop={stop_reason}")

                if not tool_calls:
                    reply = "".join(full_text).strip()
                    yield {"type": "done", "reply": reply, "usage": last_usage}
                    return

                # 构造下一轮 assistant 消息：保留 text，去掉 thinking，保留 tool_use
                assistant_content = []
                for b in blocks:
                    if b["type"] == "text":
                        assistant_content.append({"type": "text", "text": b["text"]})
                    elif b["type"] == "tool_use":
                        try:
                            inp = json.loads(b["input_json"]) if b["input_json"].strip() else (b["input"] or {})
                        except Exception:
                            inp = b["input"] or {}
                        assistant_content.append({
                            "type": "tool_use",
                            "id": b["id"],
                            "name": b["name"],
                            "input": inp,
                        })
                messages.append({"role": "assistant", "content": assistant_content})

                # 所有 tool_result 必须放在紧随其后的一条 user 消息里
                result_blocks = []
                for tc in tool_calls:
                    try:
                        tc_input = json.loads(tc["input_json"]) if tc["input_json"].strip() else (tc["input"] or {})
                    except Exception:
                        tc_input = tc["input"] or {}
                    logger.info(f"工具调用: {tc['name']} input={json.dumps(tc_input, ensure_ascii=False)[:200]}")
                    yield {"type": "tool_call", "name": tc["name"], "input": tc_input}

                    tool_result = await _run_tool(tc["name"], tc_input, db)
                    logger.info(f"工具结果: {tool_result[:200]}")
                    yield {"type": "tool_result", "name": tc["name"], "preview": tool_result[:300]}

                    # 生成报告后，向前端推送可下载文件信息（只输出文件，不展示正文）
                    if tc["name"] == "write_report" and tool_result.startswith("[报告已保存"):
                        try:
                            fname = resolve_report_filename(tc_input)
                            yield {"type": "report", "filename": fname,
                                   "url": f"/api/ads-agent/reports/{fname}"}
                        except Exception:
                            logger.exception("解析报告文件名失败")

                    result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": tool_result[:8000],
                    })
                messages.append({"role": "user", "content": result_blocks})
                continue

            yield {"type": "error", "message": "（工具调用轮次已达上限，请简化请求）"}

    except Exception as e:
        logger.exception("LLM 流式调用失败")
        yield {"type": "error", "message": f"AI 调用失败：{e}"}


def _read_file(file_bytes: bytes, filename: str):
    """读取 CSV/Excel 文件，返回 DataFrame"""
    if filename.lower().endswith('.csv'):
        for enc in ['utf-8-sig', 'utf-8', 'gbk', 'latin1']:
            try:
                return pd.read_csv(io.BytesIO(file_bytes), encoding=enc)
            except Exception:
                continue
    elif filename.lower().endswith(('.xlsx', '.xls')):
        return pd.read_excel(io.BytesIO(file_bytes))
    return None


def _normalize_columns(df):
    """多语言列名归一化，返回 {标准名: 实际列名}"""
    mapping = {}
    cols_lower = {str(c).lower().strip(): c for c in df.columns}
    for std_name, aliases in COLUMN_MAP.items():
        for alias in aliases:
            alias_l = alias.lower()
            for col_l, col_orig in cols_lower.items():
                if alias_l in col_l and std_name not in mapping:
                    mapping[std_name] = col_orig
                    break
    return mapping


def _detect_file_type(col_mapping):
    """根据表头识别上传的广告报表类型（search_term/campaign/keyword/placement/product_target）"""
    identified = set(col_mapping.keys())
    for ftype, required in FILE_TYPE_DETECT.items():
        if all(r in identified for r in required):
            return ftype
    return 'unknown'


def _safe_num(val):
    """把值安全转成数字（NaN/空值 → 0）"""
    try:
        v = float(val)
        return v if v == v else 0.0  # NaN check
    except (ValueError, TypeError):
        return 0.0


def _to_numeric(series):
    """将 Series 转换为数值，处理货币符号 / 千位分隔符 / 欧洲格式"""
    if series.dtype in ('int64', 'float64'):
        return series
    s = series.astype(str)
    s = s.str.replace(r'[$€£¥]', '', regex=True)
    s = s.str.replace(',', '', regex=False)
    s = s.str.strip()
    return pd.to_numeric(s, errors='coerce').fillna(0)


def _calc_metrics(df, col_map):
    """计算基础指标"""
    spend = float(_to_numeric(df[col_map['spend']]).sum()) if 'spend' in col_map else 0
    sales = float(_to_numeric(df[col_map['sales']]).sum()) if 'sales' in col_map else 0
    clicks = int(_to_numeric(df[col_map['clicks']]).sum()) if 'clicks' in col_map else 0
    impressions = int(_to_numeric(df[col_map['impressions']]).sum()) if 'impressions' in col_map else 0
    orders = int(_to_numeric(df[col_map['orders']]).sum()) if 'orders' in col_map else 0
    result = {
        'total_spend': round(spend, 2),
        'total_sales': round(sales, 2),
        'total_clicks': clicks,
        'total_impressions': impressions,
        'total_orders': orders,
        'overall_acos': round(spend / sales * 100, 1) if sales > 0 else 0,
        'avg_cpc': round(spend / clicks, 2) if clicks > 0 else 0,
        'overall_ctr': round(clicks / impressions * 100, 2) if impressions > 0 else 0,
        'overall_cvr': round(orders / clicks * 100, 2) if clicks > 0 else 0,
    }
    # 调试：存储原始列名和识别结果
    result['_columns'] = [str(c) for c in df.columns]
    result['_col_map'] = {k: str(v) for k, v in col_map.items()}
    return result


def _build_summary(df, col_map, file_type):
    """构建数据摘要（Top N + 异常项），用于 LLM 分析"""
    summary = {}
    name_col = col_map.get('search_term') or col_map.get('keyword') or col_map.get('campaign_name') or col_map.get('placement') or col_map.get('asin')

    work = df.copy()
    if name_col is not None:
        work['_name'] = work[name_col].astype(str)
    for std, orig in col_map.items():
        if orig in work.columns:
            if std in ('spend', 'sales', 'clicks', 'impressions', 'orders', 'bid', 'daily_budget'):
                work[std] = _to_numeric(work[orig])
            else:
                work[std] = work[orig]

    if 'spend' in work.columns:
        # Top 10 花费
        top = work.nlargest(10, 'spend')[['_name', 'spend', 'sales', 'clicks', 'orders']].head(10)
        summary['top_spend'] = top.to_dict('records')

        # 零转化高花费
        if 'orders' in work.columns:
            zero = work[(work['orders'].fillna(0) == 0) & (work['spend'] >= 10)].nlargest(10, 'spend')
            if len(zero) > 0:
                summary['zero_conversion'] = zero[['_name', 'spend', 'clicks']].to_dict('records')

        # 高 ACOS
        if 'sales' in work.columns:
            work['_acos'] = work.apply(
                lambda r: r['spend'] / r['sales'] * 100 if _safe_num(r.get('sales')) > 0 else 9999, axis=1)
            high = work[(work['_acos'] < 9999) & (work['spend'] >= 5)].nlargest(10, '_acos')
            if len(high) > 0:
                summary['high_acos'] = high[['_name', 'spend', 'sales', '_acos', 'clicks', 'orders']].to_dict('records')

        # 高转化机会
        if 'orders' in work.columns and 'sales' in work.columns:
            good = work[(work['orders'].fillna(0) >= 3) & (work['_acos'] < 20) & (work['spend'] > 0)]
            if len(good) > 0:
                summary['scaling_opportunity'] = good[['_name', 'spend', 'sales', '_acos', 'orders']].nlargest(5, 'orders').to_dict('records')

    # 序列化（处理 NaN）
    return json.dumps(summary, default=str, ensure_ascii=False)


def _query_lmg_data(db, user_message, store_code=None):
    """根据用户消息关键词查询 LMG ERP 数据库（新系统来源：核算快照 dws_profit_snapshot）"""
    parts = []
    msg_lower = user_message.lower()

    def latest_snapshots():
        """每个 (店铺, 月份, 站点) 取最新版本快照"""
        q = db.query(DwsProfitSnapshot).filter(DwsProfitSnapshot.period_month.isnot(None))
        if store_code:
            s = db.query(Store).filter(Store.store_code == store_code).first()
            if s:
                q = q.filter(DwsProfitSnapshot.store_id == s.id)
        snaps = q.order_by(DwsProfitSnapshot.version.desc()).all()
        seen = {}
        for sn in snaps:
            key = (sn.store_id, sn.period_month, sn.country or '')
            if key not in seen:
                seen[key] = sn
        return list(seen.values())

    # 查询销售/利润/TACoS
    if any(kw in msg_lower for kw in ['销售', '利润', 'tacos', 'sale', 'profit', '自然', 'organic']):
        snaps = latest_snapshots()
        if snaps:
            sales = sum(float(s.product_sales_rmb or 0) for s in snaps)
            profit = sum(float(s.net_profit_rmb or 0) for s in snaps)
            orders = sum(int(s.order_count or 0) for s in snaps)
            ad = sum(float(s.ad_spend or 0) * float(s.exchange_rate or 0) for s in snaps)
            parts.append("=== LMG ERP 核算快照汇总（各店铺各月最新版本）===")
            parts.append(f"总销售额: ¥{sales:,.2f}")
            parts.append(f"净利润: ¥{profit:,.2f}")
            parts.append(f"总订单: {orders}")
            parts.append(f"广告费: ¥{ad:,.2f}")
            if sales > 0:
                parts.append(f"TACoS: {ad/sales*100:.1f}%")
                parts.append(f"利润率: {profit/sales*100:.1f}%")
            periods = sorted({s.period_month for s in snaps})
            parts.append(f"覆盖月份: {', '.join(periods)}")
            incomplete = [s for s in snaps if not s.cost_complete]
            if incomplete:
                parts.append(f"警告: 有 {len(incomplete)} 个快照存在成本缺失，利润口径可能不准确")
        else:
            parts.append("=== LMG ERP ===\n[暂无核算快照] 请先在核算模块执行月度核算。")

    # 查询成本
    if any(kw in msg_lower for kw in ['成本', 'cost', '运费', 'freight']):
        snaps = latest_snapshots()
        if snaps:
            cost = sum(float(s.product_cost_rmb or 0) for s in snaps)
            freight = sum(float(s.freight_cost_rmb or 0) for s in snaps)
            parts.append("=== 成本数据（来自核算快照）===")
            parts.append(f"总采购成本: ¥{cost:,.2f}")
            parts.append(f"总头程运费: ¥{freight:,.2f}")

    # 列出可用店铺
    if any(kw in msg_lower for kw in ['店铺', 'store', '有哪些']):
        stores = db.query(Store).all()
        parts.append("=== 可用店铺 ===")
        parts.append(", ".join(f"{s.store_code}({s.store_name})" for s in stores))

    return "\n".join(parts) if parts else ""


def _build_context(db, session, user_message):
    """构建对话上下文：上传文件原文 + 数据库查询"""
    parts = []
    record_ids = json.loads(session.data_record_ids or "[]")
    if record_ids:
        records = db.query(AdsDataRecord).filter(AdsDataRecord.id.in_(record_ids)).all()
        for r in records:
            summary_data = json.loads(r.summary_json) if r.summary_json else {}
            text = summary_data.get('text', '')
            if text:
                parts.append(f"=== 文件: {r.original_filename} ===\n{text[:15000]}")

    db_ctx = _query_lmg_data(db, user_message)
    if db_ctx:
        parts.append(db_ctx)

    return "\n".join(parts) if parts else "（暂无上传文件）"


def _serialize_session(s, db):
    """会话对象 → 前端 JSON（含消息与关联数据记录）"""
    return {
        "id": s.id, "title": s.title, "status": s.status,
        "message_count": s.message_count,
        "created_at": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "",
        "last_active_at": s.last_active_at.strftime("%Y-%m-%d %H:%M") if s.last_active_at else "",
    }


# ============================================================
# 会话管理
# ============================================================

@router.get("/sessions", dependencies=_MOD)
def list_sessions(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """当前用户的广告 Agent 会话列表"""
    q = db.query(AdsSession).filter(AdsSession.status == "active")
    if not user.is_admin:
        q = q.filter(AdsSession.user_id == user.id)
    sessions = q.order_by(AdsSession.last_active_at.desc()).all()
    return [_serialize_session(s, db) for s in sessions]


@router.post("/sessions", dependencies=_MOD)
def create_session(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """新建会话"""
    s = AdsSession(user_id=user.id, title="新会话", message_count=0)
    db.add(s)
    db.commit()
    db.refresh(s)
    return _serialize_session(s, db)


@router.get("/sessions/{session_id}", dependencies=_MOD)
def get_session(session_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """会话详情（含消息历史与上传记录）"""
    s = db.query(AdsSession).filter(AdsSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    if not user.is_admin and s.user_id != user.id:
        raise HTTPException(403, "无权访问")
    data = _serialize_session(s, db)
    msgs = db.query(AdsMessage).filter(
        AdsMessage.session_id == session_id
    ).order_by(AdsMessage.message_index).all()
    data["messages"] = [{"role": m.role, "content": m.content} for m in msgs]
    return data


@router.delete("/sessions/{session_id}", dependencies=_MOD)
def delete_session(session_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """删除会话（级联删除其消息与数据记录）"""
    s = db.query(AdsSession).filter(AdsSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    if not user.is_admin and s.user_id != user.id:
        raise HTTPException(403, "无权删除")
    db.query(AdsMessage).filter(AdsMessage.session_id == session_id).delete()
    db.query(AdsDataRecord).filter(AdsDataRecord.session_id == session_id).delete()
    db.query(AdsActionItem).filter(AdsActionItem.session_id == session_id).delete()
    db.delete(s)
    db.commit()
    return {"message": "已删除"}


# ============================================================
# 对话
# ============================================================

@router.post("/sessions/{session_id}/chat", dependencies=_MOD)
async def chat(session_id: int, body: dict = Body(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    """非流式对话：组装上下文（上传文件摘要 + 平台库数据）→ 调模型 → 落库消息"""
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "消息不能为空")
    model = body.get("model") or None

    s = db.query(AdsSession).filter(AdsSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    if not user.is_admin and s.user_id != user.id:
        raise HTTPException(403, "无权访问")

    # 保存用户消息
    idx = s.message_count or 0
    db.add(AdsMessage(session_id=session_id, role="user", content=message, message_index=idx))
    s.message_count = idx + 1

    # 工作记忆：最近 N 轮对话（用户+AI 共 2N 条）
    history = db.query(AdsMessage).filter(
        AdsMessage.session_id == session_id
    ).order_by(AdsMessage.message_index.desc()).limit(settings.ADS_MEMORY_LIMIT).all()
    history.reverse()

    # 构建上下文
    context = _build_context(db, s, message)
    system = _system_with_date() + "\n\n当前上下文：\n" + context

    # 构建消息列表
    messages = [{"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
                for m in history]

    # 调用 LLM（支持 tool use）
    reply = await _call_llm(system, messages, db=db, model=model)

    # 保存 Agent 回复
    db.add(AdsMessage(session_id=session_id, role="assistant", content=reply, message_index=idx + 1))
    s.message_count = idx + 2
    s.last_active_at = datetime.now()
    if idx == 0:
        s.title = message[:50]
    db.commit()

    return {"reply": reply, "message_index": idx + 1}


@router.post("/sessions/{session_id}/chat/stream", dependencies=_MOD)
async def chat_stream(session_id: int, body: dict = Body(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    """流式对话：SSE 实时推送思考过程、工具调用状态与回答文本。"""
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "消息不能为空")
    model = body.get("model") or None

    s = db.query(AdsSession).filter(AdsSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    if not user.is_admin and s.user_id != user.id:
        raise HTTPException(403, "无权访问")

    # 保存用户消息（提前 commit，避免长时间流式期间占用写锁）
    idx = s.message_count or 0
    db.add(AdsMessage(session_id=session_id, role="user", content=message, message_index=idx))
    s.message_count = idx + 1
    db.commit()

    # 工作记忆：最近 N 轮对话（用户+AI 共 2N 条）
    history = db.query(AdsMessage).filter(
        AdsMessage.session_id == session_id
    ).order_by(AdsMessage.message_index.desc()).limit(settings.ADS_MEMORY_LIMIT).all()
    history.reverse()

    # 构建上下文
    context = _build_context(db, s, message)
    system = _system_with_date() + "\n\n当前上下文：\n" + context

    messages = [{"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
                for m in history]

    async def event_gen():
        """SSE 事件生成器：把模型的流式输出逐块推给前端"""
        reply = ""
        error_msg = None
        try:
            async for ev in _call_llm_stream(system, messages, db=db, model=model):
                if ev.get("type") == "done":
                    reply = ev.get("reply", "")
                elif ev.get("type") == "text":
                    # 边流式边累积正文：中途断开时保留已生成内容，避免切换会话丢失回复
                    reply += ev.get("delta", "")
                elif ev.get("type") == "error":
                    error_msg = ev.get("message", "")
                    if not reply:
                        reply = error_msg
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            # 无论客户端是否断开，都尽量保存已收到的回复
            try:
                if not reply:
                    reply = error_msg or "（连接中断，未收到完整回复）"
                db.add(AdsMessage(session_id=session_id, role="assistant", content=reply, message_index=idx + 1))
                s2 = db.query(AdsSession).filter(AdsSession.id == session_id).first()
                if s2:
                    s2.message_count = max(s2.message_count or 0, idx + 2)
                    s2.last_active_at = datetime.now()
                    if idx == 0:
                        s2.title = message[:50]
                db.commit()
            except Exception:
                logger.exception("流式回复保存失败")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ============================================================
# 文件上传 + 自动分析
# ============================================================

@router.post("/sessions/{session_id}/upload", dependencies=_MOD)
async def upload_report(
    session_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """上传广告报表文件：解析指标 + 摘要，落 ads_data_record 供后续追问"""
    s = db.query(AdsSession).filter(AdsSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    if not user.is_admin and s.user_id != user.id:
        raise HTTPException(403, "无权操作")

    content = await file.read()

    # 通用文件解析：提取全部文本，原样给 AI
    from app.services.ads.file_parser import extract_text
    file_text = extract_text(content, file.filename or "upload.txt", max_chars=40000)

    if not file_text or file_text.startswith("[解析失败"):
        raise HTTPException(400, f"文件解析失败: {file_text}")

    # 保存数据记录（文本原样存储）
    fp_hash = hashlib.sha256(file_text.encode()).hexdigest()[:16]
    record = AdsDataRecord(
        session_id=session_id,
        fingerprint=fp_hash,
        report_type=os.path.splitext(file.filename or "")[1].lstrip('.'),
        row_count=file_text.count('\n') + 1,
        metrics_json=json.dumps({'char_count': len(file_text)}, ensure_ascii=False),
        summary_json=json.dumps({'text': file_text}, ensure_ascii=False),
        original_filename=file.filename,
        uploaded_by=user.id,
    )
    db.add(record)
    db.flush()  # 确保 record.id 分配后再加入 record_ids

    # 关联到会话
    record_ids = json.loads(s.data_record_ids or "[]")
    record_ids.append(record.id)
    s.data_record_ids = json.dumps(record_ids)

    # 保存用户上传消息
    idx = s.message_count or 0
    db.add(AdsMessage(session_id=session_id, role="user", content=f"📎 已上传: {file.filename}", message_index=idx))
    s.message_count = idx + 1

    # 构建LLM上下文：文件原文 + 数据库数据
    context_parts = [f"=== 文件: {file.filename} ===\n{file_text}"]
    db_ctx = _query_lmg_data(db, "分析上传的广告数据")
    if db_ctx:
        context_parts.append(db_ctx)

    system = _system_with_date() + "\n\n用户上传了文件，请自动分析。当前上下文：\n" + "\n".join(context_parts)

    analysis_prompt = (
        f"用户上传了文件: {file.filename}\n"
        f"文本长度: {len(file_text)} 字符\n\n"
        "请用 Markdown 格式给出分析结果，包括：\n"
        "1. 数据概览（关键指标表格）\n"
        "2. 发现的问题（按严重程度分类）\n"
        "3. 可执行的建议（按优先级排序）"
    )

    llm_messages = [{"role": "user", "content": analysis_prompt}]

    reply = await _call_llm(system, llm_messages, db=db)

    db.add(AdsMessage(session_id=session_id, role="assistant", content=reply, message_index=idx + 1))
    s.message_count = idx + 2
    s.last_active_at = datetime.now()
    if idx == 0:
        s.title = f"{file.filename[:40]}"
    db.commit()

    return {"analysis": reply, "file_type": record.report_type, "char_count": len(file_text)}


from app.services.ads.file_parser import extract_text


# ============================================================
# 发送消息 + 附件（一次发送，不逐文件触发 LLM）
# ============================================================

@router.post("/sessions/{session_id}/send", dependencies=_MOD)
async def send_with_files(
    session_id: int,
    message: str = Form(""),
    files: list[UploadFile] = File([]),
    model: str = Form(None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """发送消息 + 多个附件文件，一次性解析+分析"""
    s = db.query(AdsSession).filter(AdsSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "会话不存在")
    if not user.is_admin and s.user_id != user.id:
        raise HTTPException(403, "无权操作")

    # 解析并存储所有上传文件
    file_names = []
    for f in files:
        content = await f.read()
        text = extract_text(content, f.filename or "upload.txt", max_chars=40000)
        if text.startswith("[解析失败"):
            continue
        fp_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        record = AdsDataRecord(
            session_id=session_id,
            fingerprint=fp_hash,
            report_type=os.path.splitext(f.filename or "")[1].lstrip('.'),
            row_count=text.count('\n') + 1,
            metrics_json=json.dumps({'char_count': len(text)}, ensure_ascii=False),
            summary_json=json.dumps({'text': text}, ensure_ascii=False),
            original_filename=f.filename,
            uploaded_by=user.id,
        )
        db.add(record)
        db.flush()  # 确保 record.id 分配后再加入 record_ids
        record_ids = json.loads(s.data_record_ids or "[]")
        record_ids.append(record.id)
        s.data_record_ids = json.dumps(record_ids)
        file_names.append(f.filename)

    # 获取历史对话（不含当前轮）
    idx = s.message_count or 0
    history = db.query(AdsMessage).filter(
        AdsMessage.session_id == session_id
    ).order_by(AdsMessage.message_index.desc()).limit(settings.ADS_MEMORY_LIMIT).all()
    history.reverse()
    llm_messages = [{"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
                    for m in history]

    # 保存用户消息（显示版）
    user_display = message.strip()
    if file_names:
        attach = f"📎 附件: {', '.join(file_names)}"
        user_display = f"{user_display}\n{attach}" if user_display else attach
    db.add(AdsMessage(session_id=session_id, role="user", content=user_display, message_index=idx))
    s.message_count = idx + 1

    # LLM 版用户消息
    user_llm = message.strip()
    if file_names and not user_llm:
        user_llm = f"请分析以下文件: {', '.join(file_names)}"
    elif file_names:
        user_llm += f"\n\n[附带文件: {', '.join(file_names)}]"
    llm_messages.append({"role": "user", "content": user_llm})

    # 构建上下文（_build_context 从 DB 读取所有文件原文）
    context_parts = []
    file_ctx = _build_context(db, s, message)
    if file_ctx and "暂无上传文件" not in file_ctx:
        context_parts.append(file_ctx)
    db_ctx = _query_lmg_data(db, message)
    if db_ctx:
        context_parts.append(db_ctx)

    system = _system_with_date()
    if context_parts:
        system += "\n\n当前上下文：\n" + "\n".join(context_parts)

    reply = await _call_llm(system, llm_messages, db=db, model=model)

    # 保存 Agent 回复
    db.add(AdsMessage(session_id=session_id, role="assistant", content=reply, message_index=idx + 1))
    s.message_count = idx + 2
    s.last_active_at = datetime.now()
    if idx == 0:
        s.title = (message or (file_names[0] if file_names else "新会话"))[:50]
    db.commit()

    return {"reply": reply}


# ============================================================
# 操作清单
# ============================================================

@router.get("/sessions/{session_id}/actions", dependencies=_MOD)
def list_actions(session_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """会话的行动项列表（否定词/调价/加预算等建议）"""
    actions = db.query(AdsActionItem).filter(AdsActionItem.session_id == session_id).all()
    return [{
        "id": a.id, "action_type": a.action_type, "target_type": a.target_type,
        "target_name": a.target_name, "current_value": float(a.current_value or 0),
        "suggested_value": float(a.suggested_value or 0), "reason": a.reason,
        "expected_savings": float(a.expected_savings or 0), "status": a.status,
    } for a in actions]


@router.post("/sessions/{session_id}/actions", dependencies=_MOD)
def add_action(session_id: int, body: dict = Body(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    """新增行动项（人工补充或从模型回答中沉淀）"""
    a = AdsActionItem(
        session_id=session_id,
        action_type=body.get("action_type", ""),
        target_type=body.get("target_type", ""),
        target_name=body.get("target_name", ""),
        current_value=body.get("current_value", 0),
        suggested_value=body.get("suggested_value", 0),
        reason=body.get("reason", ""),
        expected_savings=body.get("expected_savings", 0),
    )
    db.add(a)
    db.commit()
    return {"id": a.id, "message": "已添加"}


@router.get("/sessions/{session_id}/actions/export", dependencies=_MOD)
def export_actions(session_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """导出行动项为 Excel（可直接照着在亚马逊后台操作）"""
    actions = db.query(AdsActionItem).filter(AdsActionItem.session_id == session_id).all()
    output = io.StringIO()
    writer = csv_mod.writer(output)
    writer.writerow(['动作类型', '目标类型', '目标名称', '当前值', '建议值', '原因', '预期节省', '状态'])
    for a in actions:
        writer.writerow([a.action_type, a.target_type, a.target_name,
                         a.current_value or '', a.suggested_value or '', a.reason,
                         a.expected_savings or '', a.status])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename=actions_{session_id}.csv"},
    )


# ============================================================
# 报告管理（LLM 通过 write_report 工具生成的报告）
# ============================================================

from app.services.ads.agent_tools import REPORT_DIR

@router.get("/reports", dependencies=_MOD)
def list_reports():
    """列出所有已生成的分析报告。"""
    reports = []
    if os.path.isdir(REPORT_DIR):
        for fname in sorted(os.listdir(REPORT_DIR), reverse=True):
            fpath = os.path.join(REPORT_DIR, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                reports.append({
                    "filename": fname,
                    "size": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
                    "url": f"/api/ads-agent/reports/{fname}",
                })
    return reports


@router.get("/reports/{filename}", dependencies=_MOD)
def download_report(filename: str):
    """下载分析报告。"""
    from fastapi.responses import FileResponse
    safe_name = os.path.basename(filename)
    fpath = os.path.join(REPORT_DIR, safe_name)
    if not os.path.isfile(fpath):
        raise HTTPException(404, "报告不存在")
    media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if safe_name.endswith(".docx") else "text/markdown"
    return FileResponse(fpath, media_type=media_type, filename=safe_name)
