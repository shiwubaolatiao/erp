# -*- coding: utf-8 -*-
"""广告 Agent 工具集 —— LLM 通过 tool use 调用，驱动广告分析 Agent

【来源与适配说明】（维护必读）
  本文件**移植自旧系统** `lmg-platform/backend/services/agent_tools.py`，
  保留其全部工具逻辑（报表读取、指标计算、报告生成），仅做以下适配：

   1. 模型引用：旧 `models.DimStore/DimTime/MonthlySummary/DimProduct/DimProductCost/RawAdvertising`
      → 新 `Store/DwsProfitSnapshot/DwsProfitSnapshotItem/FactAdDaily`
      （新版没有 dim_* 维度表，商品主数据是 store_product + store_sku_mapping 三元组）
   2. `_tool_get_stores`：字段 `code/name` → `store_code/store_name`
   3. `_tool_get_latest_metrics`：数据源从旧 `monthly_summary` 改为新 **核算快照**
      `dws_profit_snapshot`（每个 店铺+月份 取最新 version）
   4. 报表读取部分（report_store.py）**未改动** —— 它只读本地 unified 广告报表目录
      （配置 ADS_REPORTS_ROOT），与 ERP 数据库无耦合

【能力清单】
   1. 报表读取：list_amazon_reports / query_amazon_ads（本地报表库，只读，不触发下载）
   2. 数据库查询：query_database（受限 SELECT）/ get_latest_metrics（核算快照）
   3. 文档生成：write_report → Word/Excel/PDF/Markdown 落盘到 backend/reports/ads_agent

★ 报表读取是"周/日粒度"的唯一来源；平台库只有月度粒度 —— 用户问"今天/上周"必须走本地报表库。
"""
import os
import io
import re
import json
import logging
from datetime import datetime, date
from typing import Optional

from sqlalchemy import text, func
from sqlalchemy.orm import Session

from app.models import (
    Store, Site, StoreProduct, StoreSkuMapping, ProductCostHistory,
    RawSheet, RawSheetRow, FactAdDaily, DwsProfitSnapshot, DwsProfitSnapshotItem,
)
from app.services.ads.report_store import (
    aggregate_to_text, list_reports_text,
)

logger = logging.getLogger("ads_agent.tools")

# 报告输出目录
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "ads_agent")
os.makedirs(REPORT_DIR, exist_ok=True)

# 本地 unified 报表库（不再经 8010 紫鸟下载 Agent；只读本地，不触发下载）


# ============================================================
# 工具定义（给 LLM 的 function schema）
# ============================================================

TOOL_DEFINITIONS = [
    {
        "name": "query_database",
        "description": "查询 LMG 平台数据库（MySQL: lmg_platform）。可查销售、利润、广告、库存、成本等数据。支持任意 SQL SELECT 语句。",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL SELECT 语句。可用表: monthly_summary(月度汇总), dim_store(店铺), dim_country(国家), dim_product(产品), dim_product_cost(产品成本), raw_advertising(广告原始数据), dim_time(时间维度)。示例: SELECT s.code, SUM(m.product_sales_rmb) as sales FROM monthly_summary m JOIN dim_store s ON m.store_id=s.id GROUP BY s.code"
                },
                "limit": {
                    "type": "integer",
                    "description": "返回行数限制，默认 100",
                    "default": 100
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "query_inventory",
        "description": "查询库存系统数据库（MySQL: inventory_system）。可查库存、发货、退货赔偿、产品成本等数据。",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL SELECT 语句。可用表: monthly_inventory(月度库存), product_cost(产品成本), asin_sku_mapping(ASIN-SKU对照), return_reimbursement(退货赔偿), shipment(发货记录)。示例: SELECT * FROM monthly_inventory LIMIT 10"
                },
                "limit": {
                    "type": "integer",
                    "description": "返回行数限制，默认 100",
                    "default": 100
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "write_report",
        "description": "将分析报告写入文件并供用户下载，保存到服务器 reports/ads_agent/ 目录。"
                       "支持格式：word(.docx)、excel(.xlsx)、pdf(.pdf)。"
                       "格式选择规则：用户指定格式时严格按用户要求；用户未指定格式时默认输出 word；"
                       "内容以大量数据表格/清单为主时可选 excel；需要打印或对外分发时可选 pdf。"
                       "生成后只告知用户下载链接，不要透露服务器路径，也不要在聊天中粘贴报告正文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "文件名（可含扩展名，如 'Well-US-ACOS分析-2026-08.docx' 或 '....xlsx' / '....pdf'）。不带扩展名时按 format 自动补充。"
                },
                "content": {
                    "type": "string",
                    "description": "报告内容，用 Markdown 格式书写（标题/表格/列表）。后端会自动转换为对应格式的文件。"
                },
                "format": {
                    "type": "string",
                    "enum": ["word", "excel", "pdf"],
                    "description": "输出格式：word=.docx 文件（默认），excel=.xlsx 文件，pdf=.pdf 文件。",
                    "default": "word"
                }
            },
            "required": ["filename", "content"]
        }
    },
    {
        "name": "get_stores",
        "description": "获取所有店铺列表及其代码。在需要知道有哪些店铺可查询时调用。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_latest_metrics",
        "description": "获取指定店铺和月份的关键广告指标（ACOS、ROAS、CTR、CPC、花费、销售等）。不传参数则返回所有店铺最近月份数据。",
        "input_schema": {
            "type": "object",
            "properties": {
                "store_code": {
                    "type": "string",
                    "description": "店铺代码，如 'Well', 'LMG-EU', 'STX-US'"
                },
                "year_month": {
                    "type": "string",
                    "description": "月份，格式 YYYY-MM，如 '2026-07'。不传则取最新月份。"
                }
            },
            "required": []
        }
    },
    {
        "name": "list_amazon_reports",
        "description": "列出本地 unified 广告报表库中已归档的文件。这是查周/日粒度广告数据的**第一步**。"
                       "本地只读、绝不下载；没有就是没有。"
                       "平台只有 5 个店铺：well / stx / lmg / mgt / mgk。"
                       "报表类型(6类)：search_term(搜索词)/targeting(投放)/products(推广的商品)/"
                       "placement(广告位)/campaigns(广告活动)/audience(受众)。",
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {
                    "type": "string",
                    "description": "店铺名：well / stx / lmg / mgt / mgk"
                },
                "type": {
                    "type": "string",
                    "description": "报表类型(可选)：search_term / targeting / products / placement / campaigns / audience"
                },
                "date": {
                    "type": "string",
                    "description": "报告期需覆盖的某一天，格式 YYYY-MM-DD（与 month 二选一）"
                },
                "month": {
                    "type": "string",
                    "description": "报告期与当月有交集，格式 YYYY-MM（可选）"
                }
            },
            "required": []
        }
    },
    {
        "name": "query_amazon_ads",
        "description": "对本地 unified 报表做**服务端筛选+精确聚合**(按币种分别统计)，返回区间总览、按实体TopN、异常/机会清单。"
                       "数字全部由后端算好(求和/ACOS/CPC/CVR)，模型只读结果、不做任何心算，也绝不读 CSV 原文。"
                       "这是分析周/日粒度广告数据的核心工具。分析顺序由粗到细：campaigns(整体) → placement(位置) → "
                       "products(ASIN) → targeting(投放) → search_term(词级下钻)。",
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {
                    "type": "string",
                    "description": "店铺名：well / stx / lmg / mgt / mgk"
                },
                "type": {
                    "type": "string",
                    "description": "报表类型：search_term / targeting / products / placement / campaigns"
                },
                "month": {
                    "type": "string",
                    "description": "整月 YYYY-MM，如 '2026-07'"
                },
                "start": {
                    "type": "string",
                    "description": "起始日 YYYY-MM-DD（查'上周/本周/最近7天'时用它，与 end 配合）"
                },
                "end": {
                    "type": "string",
                    "description": "结束日 YYYY-MM-DD"
                },
                "group_by": {
                    "type": "string",
                    "description": "按哪个实体分组：campaign(广告活动)/adgroup(广告组)/term(搜索词)/targeting(投放方案)/asin/product(商品)/placement(广告位分类)/placement_name(广告位名称)/ad_type(广告产品SP/SB)/marketplace(站点)/currency(币种)。placement 报表看位置效率用 placement；campaigns 对比SP/SB用 ad_type。"
                },
                "currency": {
                    "type": "string",
                    "description": "只看某币种：GBP=英国/EUR=欧元区/USD=美国/CAD=加拿大/AUD=澳大利亚/SEK=瑞典/MXN=墨西哥等"
                },
                "campaign_contains": {
                    "type": "string",
                    "description": "只统计广告活动名称包含该文字的(可选)"
                },
                "name_contains": {
                    "type": "string",
                    "description": "只统计分组实体名包含该文字的，如某搜索词(可选)"
                },
                "asins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只统计这些 ASIN(可选)"
                },
                "ad_type": {
                    "type": "string",
                    "description": "只看广告产品类型：Sponsored Products(商品推广SP)/Sponsored Brands(品牌推广SB)，用于SP vs SB对比"
                },
                "min_spend": {
                    "type": "number",
                    "description": "最低花费阈值，过滤小额噪音(默认2)"
                },
                "only_zero_orders": {
                    "type": "boolean",
                    "description": "true=只看0订单项(找无效花费/否定词)"
                },
                "min_orders": {
                    "type": "integer",
                    "description": "机会清单的最低订单数(默认3)"
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["spend", "sales", "orders"],
                    "description": "排序字段(默认 spend)"
                },
                "top_n": {
                    "type": "integer",
                    "description": "每个币种返回的TopN数量(默认20)"
                }
            },
            "required": ["store", "type"]
        }
    },
]


# ============================================================
# 工具执行器
# ============================================================

def execute_tool(tool_name: str, tool_input: dict, db: Session) -> str:
    """执行一个工具调用，返回结果文本。"""
    try:
        if tool_name == "query_database":
            return _tool_query_database(db, tool_input)
        elif tool_name == "query_inventory":
            return _tool_query_inventory(tool_input)
        elif tool_name == "write_report":
            return _tool_write_report(tool_input)
        elif tool_name == "get_stores":
            return _tool_get_stores(db)
        elif tool_name == "get_latest_metrics":
            return _tool_get_latest_metrics(db, tool_input)
        elif tool_name == "list_amazon_reports":
            return _tool_list_amazon_reports(tool_input)
        elif tool_name == "query_amazon_ads":
            return _tool_query_amazon_ads(tool_input)
        else:
            return f"[未知工具: {tool_name}]"
    except Exception as e:
        logger.exception("工具执行失败: %s", tool_name)
        return f"[工具 {tool_name} 执行失败: {e}]"


# ============================================================
# 本地 unified 报表工具（只读本地，不触发任何下载）
# ============================================================

def _tool_list_amazon_reports(params: dict) -> str:
    """列出本地 unified 报表库已归档文件。"""
    try:
        return list_reports_text(
            store=params.get("store"), type_=params.get("type"),
            month=params.get("month"), start=params.get("start"),
            end=params.get("end"), date_=params.get("date"),
        )
    except Exception as e:
        logger.exception("list_amazon_reports 失败")
        return f"[本地报表库查询失败: {e}]"


def _tool_query_amazon_ads(params: dict) -> str:
    """本地报表筛选+精确聚合。"""
    try:
        return aggregate_to_text(
            store=params.get("store"), type_=params.get("type"),
            month=params.get("month"), start=params.get("start"), end=params.get("end"),
            date_=params.get("date"),
            group_by=params.get("group_by"),
            campaign_contains=params.get("campaign_contains"),
            name_contains=params.get("name_contains"),
            asins=params.get("asins"),
            currency=params.get("currency"),
            ad_type=params.get("ad_type"),
            min_spend=params.get("min_spend"),
            only_zero_orders=params.get("only_zero_orders"),
            min_orders=params.get("min_orders", 3),
            sort_by=params.get("sort_by", "spend"),
            top_n=params.get("top_n", 20),
            anomalies=params.get("anomalies", True),
        )
    except Exception as e:
        logger.exception("query_amazon_ads 失败")
        return f"[本地报表聚合失败: {e}]"


def _tool_query_database(db: Session, params: dict) -> str:
    """查询 lmg_platform 数据库。"""
    sql = params.get("sql", "").strip()
    limit = min(params.get("limit", 100), 500)

    # 安全检查：只允许 SELECT
    sql_upper = sql.upper().lstrip()
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        return "[错误: 只允许 SELECT 或 WITH 语句]"

    # 加 LIMIT
    if "LIMIT" not in sql_upper:
        sql = f"{sql.rstrip(';')} LIMIT {limit}"

    result = db.execute(text(sql))
    columns = list(result.keys())
    rows = result.fetchall()

    if not rows:
        # 广告相关查询为空时，提示改用本地 unified 报表库
        if re.search(r"ad|spend|impression|click|acos|roas|广告", sql, re.I):
            return ("[查询结果为空]（平台库只有已导入的历史月数据；"
                    "周/日粒度广告数据请用 list_amazon_reports / query_amazon_ads 查本地 unified 报表库）")
        return "[查询结果为空]"

    # 格式化为表格文本
    lines = [" | ".join(columns)]
    lines.append("-" * len(lines[0]))
    for row in rows[:limit]:
        vals = [str(v) if v is not None else "" for v in row]
        lines.append(" | ".join(vals))

    return f"[返回 {len(rows)} 行]\n" + "\n".join(lines)


def _tool_query_inventory(params: dict) -> str:
    """查询 inventory_system 数据库。"""
    from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD
    import pymysql

    sql = params.get("sql", "").strip()
    limit = min(params.get("limit", 100), 500)

    sql_upper = sql.upper().lstrip()
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        return "[错误: 只允许 SELECT 或 WITH 语句]"

    if "LIMIT" not in sql_upper:
        sql = f"{sql.rstrip(';')} LIMIT {limit}"

    conn = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database="inventory_system", charset="utf8mb4"
    )
    try:
        cur = conn.cursor()
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return "[查询结果为空]"

    lines = [" | ".join(columns)]
    lines.append("-" * len(lines[0]))
    for row in rows[:limit]:
        vals = [str(v) if v is not None else "" for v in row]
        lines.append(" | ".join(vals))

    return f"[返回 {len(rows)} 行]\n" + "\n".join(lines)


def resolve_report_filename(params: dict) -> str:
    """根据工具入参确定最终文件名与格式（与 _tool_write_report 的归一化规则一致）。

    格式优先级：format 参数 > 文件名扩展名 > 默认 word（用户未指定时输出 Word）。
    """
    filename = params.get("filename") or f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    filename = os.path.basename(filename)
    fmt = (params.get("format") or "").strip().lower()

    # 去掉文件名中可能带有的扩展名，统一按 fmt 决定
    for ext in (".docx", ".xlsx", ".pdf", ".md"):
        if filename.lower().endswith(ext):
            filename = filename[:-len(ext)]
            if not fmt:
                fmt = {"docx": "word", "xlsx": "excel", "pdf": "pdf", "md": "markdown"}[ext[1:]]
            break

    if fmt in ("word", "docx"):
        filename += ".docx"
    elif fmt in ("excel", "xlsx", "xls"):
        filename += ".xlsx"
    elif fmt == "pdf":
        filename += ".pdf"
    elif fmt in ("markdown", "md"):
        filename += ".md"
    else:
        # 用户未指定格式：默认 Word
        filename += ".docx"
    return filename


def _tool_write_report(params: dict) -> str:
    """写入报告文件。"""
    filename = resolve_report_filename(params)
    content = params.get("content", "")

    filepath = os.path.join(REPORT_DIR, filename)
    if filename.endswith(".docx"):
        _write_docx(filepath, content)
    elif filename.endswith(".xlsx"):
        _write_xlsx(filepath, content)
    elif filename.endswith(".pdf"):
        _write_pdf(filepath, content)
    else:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    return (
        f"[报告已保存: {filename}] 下载链接: /api/ads-agent/reports/{filename}，字符数: {len(content)}。"
        f"回复用户时请直接给出该下载链接，不要透露服务器文件路径，也不要在聊天中粘贴报告正文。"
    )


# ============================================================
# Markdown -> Word 转换
# ============================================================

def _set_east_asia(obj, name: str):
    """为样式或 run 设置中文字体（eastAsia），解决 Word 中文乱码/字体回退问题。"""
    try:
        rpr = obj._element.get_or_add_rPr() if hasattr(obj._element, "get_or_add_rPr") else obj._element.rPr
        rfonts = rpr.rFonts
        if rfonts is None:
            from docx.oxml.ns import qn
            rfonts = rpr.get_or_add_rFonts()
        rfonts.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia", name)
    except Exception:
        pass


def _add_inline_runs(paragraph, text: str):
    """解析行内 **加粗** / *斜体* / `行内代码`，写入段落。"""
    import re
    pattern = re.compile(r"(\*\*[^*\n]+\*\*|\*[^*\n]+\*|`[^`\n]+`)")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        seg = m.group(0)
        if seg.startswith("**") and seg.endswith("**"):
            r = paragraph.add_run(seg[2:-2])
            r.bold = True
        elif seg.startswith("`") and seg.endswith("`"):
            r = paragraph.add_run(seg[1:-1])
            r.font.name = "Consolas"
        elif seg.startswith("*") and seg.endswith("*"):
            r = paragraph.add_run(seg[1:-1])
            r.italic = True
        else:
            paragraph.add_run(seg)
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _add_markdown_table(doc, raw_rows):
    """把连续的 Markdown 表格行合并为一张真正的 Word 表格。"""
    from docx.shared import Pt
    from docx.oxml.ns import qn

    parsed = []
    for raw in raw_rows:
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        # 去掉首尾由外侧竖线产生的空元素
        if cells and cells[0].strip() == "":
            cells = cells[1:]
        if cells and cells[-1].strip() == "":
            cells = cells[:-1]
        cells = [c.strip() for c in cells]
        # 跳过对齐分隔行（--- / :---: 等）
        if all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
            continue
        parsed.append(cells)

    if not parsed:
        return

    ncols = max(len(r) for r in parsed)
    table = doc.add_table(rows=len(parsed), cols=ncols)
    try:
        table.style = doc.styles["Table Grid"]
    except KeyError:
        pass

    for ri, cells in enumerate(parsed):
        for ci in range(ncols):
            text = cells[ci] if ci < len(cells) else ""
            cell = table.cell(ri, ci)
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.space_before = Pt(2)
            _add_inline_runs(p, text)
            if ri == 0:
                for run in p.runs:
                    run.bold = True

    # 表头底纹
    try:
        from docx.oxml import OxmlElement
        for ci in range(ncols):
            tcPr = table.cell(0, ci)._tc.get_or_add_tcPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:fill"), "D9E2F3")
            tcPr.append(shd)
    except Exception:
        pass

    doc.add_paragraph()


def _write_docx(filepath: str, markdown_content: str):
    """将 Markdown 内容写入排版规范的 Word 文档。

    - 真正的表格（连续 | 行合并为一张表，表头加粗+底纹，Table Grid 边框）
    - 1~4 级标题、有序/无序列表、引用块、分隔线
    - 中文字体（微软雅黑 eastAsia），避免乱码
    """
    import re
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor
    from docx.oxml.ns import qn

    doc = Document()

    # 全局默认字体：西文 Calibri + 中文微软雅黑
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    _set_east_asia(normal, "微软雅黑")

    for lvl in range(1, 5):
        hs = doc.styles[f"Heading {lvl}"]
        hs.font.name = "Calibri"
        _set_east_asia(hs, "微软雅黑")
        hs.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)

    def add_para(text, **fmt):
        """向 Word 文档添加段落并处理行内格式（粗体/代码）"""
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_after = Pt(4)
        if fmt.get("indent"):
            pf.left_indent = Cm(0.75)
        _add_inline_runs(p, text)
        if fmt.get("quote"):
            for r in p.runs:
                r.italic = True
                r.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
        return p

    lines = markdown_content.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()

        # 表格块：连续以 | 开头的行
        if line.lstrip().startswith("|"):
            table_rows = []
            while i < n and lines[i].strip().startswith("|"):
                table_rows.append(lines[i])
                i += 1
            _add_markdown_table(doc, table_rows)
            continue

        i += 1
        stripped = line.strip()
        if not stripped:
            continue

        if line.startswith("#### "):
            doc.add_heading(line[5:], level=4)
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif re.match(r"^\s*([-*+•])\s+", line):
            add_para("• " + re.sub(r"^\s*[-*+•]\s+", "", line), indent=True)
        elif re.match(r"^\s*\d+[.)]\s+", line):
            m = re.match(r"^\s*(\d+[.)])\s+(.*)$", line)
            add_para(f"{m.group(1)} {m.group(2)}", indent=True)
        elif line.lstrip().startswith(">"):
            add_para(re.sub(r"^\s*>\s?", "", line), quote=True, indent=True)
        elif re.match(r"^\s*(---+|\*\*\*+|___+)\s*$", line):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(6)
            ppr = p._p.get_or_add_pPr()
            from docx.oxml import OxmlElement
            pbdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "BFBFBF")
            pbdr.append(bottom)
            ppr.append(pbdr)
        else:
            add_para(stripped)

    doc.save(filepath)


# ============================================================
# Markdown -> Excel 转换
# ============================================================

def _parse_md_blocks(markdown_content: str):
    """把 Markdown 解析为结构化块列表（与 Word 转换共用同一套解析规则）。

    每块为 ("heading", level, text) / ("para", text) / ("list", text) / ("table", rows)。
    """
    import re
    blocks = []
    lines = markdown_content.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()

        if line.lstrip().startswith("|"):
            table_rows = []
            while i < n and lines[i].strip().startswith("|"):
                raw = lines[i].strip()
                cells = raw.split("|")
                if cells and cells[0].strip() == "":
                    cells = cells[1:]
                if cells and cells[-1].strip() == "":
                    cells = cells[:-1]
                cells = [c.strip() for c in cells]
                if not all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
                    table_rows.append(cells)
                i += 1
            if table_rows:
                blocks.append(("table", table_rows))
            continue

        i += 1
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            blocks.append(("heading", len(m.group(1)), m.group(2)))
        elif re.match(r"^\s*([-*+•])\s+", line):
            blocks.append(("list", "• " + re.sub(r"^\s*[-*+•]\s+", "", line)))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            mm = re.match(r"^\s*(\d+[.)])\s+(.*)$", line)
            blocks.append(("list", f"{mm.group(1)} {mm.group(2)}"))
        elif re.match(r"^\s*(---+|\*\*\*+|___+)\s*$", line):
            continue
        else:
            text = re.sub(r"\*\*([^*]+)\*\*", r"\1", stripped)
            text = re.sub(r"`([^`]+)`", r"\1", text)
            text = re.sub(r"\*([^*]+)\*", r"\1", text)
            if line.lstrip().startswith(">"):
                text = re.sub(r"^\s*>\s?", "", line)
                text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
                blocks.append(("quote", text))
            else:
                blocks.append(("para", text))
    return blocks


def _write_xlsx(filepath: str, markdown_content: str):
    """将 Markdown 内容写入 Excel 工作簿（标题/段落/真实表格）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    blocks = _parse_md_blocks(markdown_content)
    wb = Workbook()
    ws = wb.active
    ws.title = "报告"

    thin = Side(style="thin", color="D0D0D0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor="D9E2F3")
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    row = 1
    for kind, *rest in blocks:
        if kind == "heading":
            level, text = rest
            cell = ws.cell(row=row, column=1, value=text)
            cell.font = Font(bold=True, size=max(11, 15 - level))
            row += 1
        elif kind == "para":
            cell = ws.cell(row=row, column=1, value=rest[0])
            cell.alignment = wrap
            row += 1
        elif kind == "list":
            cell = ws.cell(row=row, column=1, value=rest[0])
            cell.alignment = wrap
            row += 1
        elif kind == "quote":
            cell = ws.cell(row=row, column=1, value="💬 " + rest[0])
            cell.font = Font(italic=True, color="595959")
            cell.alignment = wrap
            row += 1
        elif kind == "table":
            rows = rest[0]
            ncols = max(len(r) for r in rows)
            for ri, cells in enumerate(rows):
                for ci in range(ncols):
                    val = cells[ci] if ci < len(cells) else ""
                    cell = ws.cell(row=row + ri, column=1 + ci, value=val)
                    cell.border = border
                    cell.alignment = wrap
                    if ri == 0:
                        cell.font = bold
                        cell.fill = header_fill
            # 列宽：按内容长度粗略自适应
            for ci in range(ncols):
                max_len = max((len(str(r[ci])) if ci < len(r) else 0) for r in rows)
                ws.column_dimensions[get_column_letter(1 + ci)].width = min(max(max_len * 2 + 4, 10), 60)
            row += len(rows) + 1

    ws.freeze_panes = "A2"
    wb.save(filepath)


# ============================================================
# Markdown -> PDF 转换
# ============================================================

_CJK_FONT_PATH = None
for _cand in (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simsun.ttc"):
    if os.path.isfile(_cand):
        _CJK_FONT_PATH = _cand
        break


def _write_pdf(filepath: str, markdown_content: str):
    """将 Markdown 内容写入 PDF（中文字体 + 标题/段落/列表/表格）。"""
    from fpdf import FPDF

    if not _CJK_FONT_PATH:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        logger.warning("未找到中文字体，PDF 降级为纯文本文件")
        return

    blocks = _parse_md_blocks(markdown_content)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("CJK", "", _CJK_FONT_PATH)
    pdf.add_font("CJK", "B", _CJK_FONT_PATH)

    def cell_text(text, size=10, bold=False, color=None, indent=0, lh=6):
        """提取表格单元格文本（兼容 None 与多行内容）"""
        style = "B" if bold else ""
        if color:
            pdf.set_text_color(*color)
        pdf.set_font("CJK", style, size)
        # 处理 SimHei 字体缺失的字形与特殊空白
        text = (text.replace("\u00a0", " ")
                    .replace("\u2022", "-")
                    .replace("\u00bb", "|"))
        if indent:
            pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(0, lh, text)
        if color:
            pdf.set_text_color(0, 0, 0)
        pdf.ln(1)

    for kind, *rest in blocks:
        if kind == "heading":
            level, text = rest
            size = {1: 16, 2: 13, 3: 11, 4: 10}.get(level, 10)
            pdf.ln(2)
            cell_text(text, size=size, bold=True, color=(31, 58, 95))
        elif kind == "para":
            cell_text(rest[0])
        elif kind == "list":
            cell_text(rest[0], indent=4)
        elif kind == "quote":
            cell_text("» " + rest[0], color=(89, 89, 89), indent=4)
        elif kind == "table":
            rows = rest[0]
            ncols = max(len(r) for r in rows)
            page_w = pdf.w - pdf.l_margin - pdf.r_margin
            col_w = page_w / ncols
            for ri, cells in enumerate(rows):
                if pdf.get_y() > pdf.h - 30:
                    pdf.add_page()
                if ri == 0:
                    pdf.set_font("CJK", "B", 9)
                    pdf.set_fill_color(217, 226, 243)
                else:
                    pdf.set_font("CJK", "", 9)
                    pdf.set_fill_color(255, 255, 255)
                for ci in range(ncols):
                    val = cells[ci] if ci < len(cells) else ""
                    pdf.cell(col_w, 7, val[:120], border=1, fill=True)
                pdf.ln()
            pdf.ln(2)

    pdf.output(filepath)


def _tool_get_stores(db: Session) -> str:
    """获取店铺列表。"""
    stores = db.query(Store).all()
    lines = ["店铺代码 | 店铺名称", "-" * 20]
    for s in stores:
        lines.append(f"{s.store_code} | {s.store_name}")
    return "\n".join(lines)


def _tool_get_latest_metrics(db: Session, params: dict) -> str:
    """获取最新月度指标（新系统来源：核算快照 dws_profit_snapshot）"""
    store_code = params.get("store_code")
    period_month = params.get("year_month") or params.get("period_month")

    q = db.query(
        Store.store_code.label("store"),
        DwsProfitSnapshot.period_month.label("month"),
        DwsProfitSnapshot.ad_spend.label("ad_spend"),
        DwsProfitSnapshot.product_sales_rmb.label("sales_rmb"),
        DwsProfitSnapshot.net_profit_rmb.label("profit_rmb"),
        DwsProfitSnapshot.order_count.label("orders"),
    ).join(Store, DwsProfitSnapshot.store_id == Store.id
           ).filter(DwsProfitSnapshot.period_month.isnot(None))

    if store_code:
        q = q.filter(Store.store_code == store_code)
    if period_month:
        q = q.filter(DwsProfitSnapshot.period_month == period_month)

    rows = q.order_by(DwsProfitSnapshot.period_month.desc()).limit(20).all()
    if not rows:
        return ("[未找到数据]（新系统只有已核算的历史月数据，请先在核算模块执行核算；"
                "周/日粒度广告数据请用 list_amazon_reports / query_amazon_ads 查本地 unified 报表库）")

    lines = ["店铺 | 月份 | 广告花费 | 销售额¥ | 利润¥ | 订单"]
    lines.append("-" * 70)
    for r in rows:
        lines.append(
            f"{r.store} | {r.month} | {float(r.ad_spend or 0):.2f} | "
            f"{float(r.sales_rmb or 0):.0f} | {float(r.profit_rmb or 0):.0f} | "
            f"{int(r.orders or 0)}"
        )

    return "\n".join(lines)
