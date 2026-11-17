# -*- coding: utf-8 -*-
"""
本地 unified 广告报表库 —— 读取 / 筛选 / 聚合（替代紫鸟下载 Agent 的 8010 远程链路）。

【来源说明】本文件**原样移植自旧系统** `lmg-platform/backend/services/report_store.py`，
  未做任何逻辑改动。原因：它只读本地报表目录（ADS_REPORTS_ROOT），与 ERP 数据库零耦合，
  且内部的多币种/拆行/脏值处理是实测沉淀下来的正确性设计，重写风险大于收益。
  ★ 修改前请先理解下面第 1~5 条正确性设计，任何"简化"都可能让指标算错。

数据位置:  {REPORTS_ROOT}/unified/{店铺}/{年}/{月}/{店铺}-{类型}-{YYYY-MM-DD}_{YYYY-MM-DD}.csv
  店铺: Well / STX / LMG / MGT / MGK
  类型: 广告活动(campaigns) / 广告位(placement) / 推广的商品(products) / 投放(targeting) /
        搜索词(search_term) / 受众(audience)

正确性设计（针对“数据不准 / token 爆炸”）:
1. 所有金额/点击/展示的**求和与比率**(ACOS/CPC/CTR/CVR/ROAS)都在这里用 pandas 精确计算，
   模型只读结果文本，绝不心算、绝不读 CSV 原文。
2. 文件按行携带币种(预算货币)且同文件内混 EUR/GBP/USD/MXN/...，因此聚合一律**按币种分组**，
   绝不把不同币种直接相加；单币种时才给“总计”。
3. 广告位(placement)文件是“拆行”结构: `网站或应用程序`空值行只背订单/销售(0花费)、
   `Unknown`行只背花费/点击(0单)。按实体把两类行合并后再分指标求和，行内 0 值不是真实 0。
4. 文件名自带报告期；行内另有“日期范围”。查询区间窄于文件区间时按行级区间做**近似切分**。
5. 清洗 Excel 导出脏值: `="..."` 掩码 / 千分位 / 百分比 / 全角空格。
"""
import os
import re
import logging
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger("ads_agent.report_store")

REPORTS_ROOT = os.getenv("ADS_REPORTS_ROOT", r"E:\项目\ads_agent\data\reports")
UNIFIED_ROOT = os.path.join(REPORTS_ROOT, "unified")

STORE_FOLDERS = ("Well", "STX", "LMG", "MGT", "MGK")

STORE_ALIAS = {
    "well": "Well", "wel": "Well", "welle": "Well",
    "stx": "STX", "stx-us": "STX",
    "lmg": "LMG", "lmg-eu": "LMG",
    "mgt": "MGT", "mgt-eu": "MGT",
    "mgk": "MGK", "mgk-eu": "MGK",
}

TYPE_CN = {
    "search_term": "搜索词",
    "targeting": "投放",
    "products": "推广的商品",
    "placement": "广告位",
    "campaigns": "广告活动",
    "audience": "受众",
}
TYPE_ALIASES = {
    "search_term": {"search_term", "search", "搜索词", "搜词", "term", "搜索"},
    "targeting": {"targeting", "target", "投放", "投放方案", "关键词"},
    "products": {"products", "product", "推广的商品", "推广商品", "商品"},
    "placement": {"placement", "广告位", "广告位置"},
    "campaigns": {"campaigns", "campaign", "广告活动", "广告系列", "campaign report"},
    "audience": {"audience", "受众"},
}

# 各类型默认的“实体名”列
NAME_COL = {
    "search_term": "搜索词",
    "targeting": "投放方案",
    "products": "推广的商品名称",
    "placement": "广告位分类",
    "campaigns": "广告活动名称",
}
# 标准列名 -> 中文列名(仅用于 group_by 映射, 具体靠 load_report 的 _col 归一化)
GROUP_KEYS = {
    "campaign": "广告活动名称",
    "adgroup": "广告组名称",
    "term": "搜索词",
    "targeting": "投放方案",
    "asin": "推广的商品编号",
    "product": "推广的商品名称",
    "placement": "广告位分类",
    "placement_name": "广告位名称",
    "ad_type": "广告产品",
    "marketplace": "推广的商品站点-Advertised product marketplace",
    "currency": "预算货币",
}
# 归一化后的内部列名(load_report 产出)
STD_COLS = {
    "campaign": "_campaign",
    "adgroup": "_adgroup",
    "term": "_term",
    "targeting": "_target",
    "asin": "_asin",
    "product": "_product",
    "placement": "_placement",
    "placement_name": "_placement_name",
    "ad_type": "_ad_type",
    "marketplace": "_marketplace",
    "currency": "_ccy",
}

_FILE_RE = re.compile(
    r"^(?P<store>Well|STX|LMG|MGT|MGK)-(?P<type_cn>.+?)-"
    r"(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})\.csv$"
)


def normalize_store(store: Optional[str]) -> Optional[str]:
    """店铺名归一（大小写/别名 → 标准名，如 Well→WEL）"""
    if not store:
        return None
    s = str(store).strip().lower().replace(" ", "")
    if s in STORE_ALIAS:
        return STORE_ALIAS[s]
    for folder in STORE_FOLDERS:
        if s == folder.lower() or s.startswith(folder.lower()):
            return folder
    return None


def normalize_type(t: Optional[str]) -> Optional[str]:
    """报表类型归一（中文/英文类型名 → 标准类型键）"""
    if not t:
        return None
    key = str(t).strip().lower()
    for canonical, aliases in TYPE_ALIASES.items():
        if key == canonical or key in aliases:
            return canonical
    return None


def _to_float(v) -> float:
    """把报表脏值转成 float（去千分位/百分号/=\"...\" 掩码/全角空格），失败返回 0"""
    if v is None:
        return 0.0
    s = str(v).strip()
    if not s or s in ("-", "--", "N/A", "na", "nan", "None", ""):
        return 0.0
    m = re.fullmatch(r'="(.*)"', s)
    if m:
        s = m.group(1)
    s = s.replace("\u00a0", " ").replace(" ", "").strip()
    if not s:
        return 0.0
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.lstrip("$€£¥￥").rstrip("%").rstrip("$€£¥￥")
    if not s:
        return 0.0
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if s.count(",") == 1 and 1 <= (len(s) - s.rfind(",") - 1) <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        val = float(s)
    except (ValueError, TypeError):
        return 0.0
    return -val if neg else val


def _parse_cn_date(s: str) -> Optional[date]:
    """解析中文日期（YYYY年M月D日 / YYYY-MM-DD / YYYY/M/D）"""
    if not s:
        return None
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", str(s))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _query_window(month, start, end, date_):
    """解析查询区间：优先 start/end，其次 month（整月），再其次 date_（单日）"""
    if date_:
        d = datetime.strptime(str(date_), "%Y-%m-%d").date()
        return d, d
    if start or end:
        s = datetime.strptime(str(start), "%Y-%m-%d").date() if start else None
        e = datetime.strptime(str(end), "%Y-%m-%d").date() if end else None
        return s, e
    if month:
        try:
            y, mo = (int(x) for x in str(month).split("-"))
            s = date(y, mo, 1)
            nxt = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
            return s, nxt - timedelta(days=1)
        except (ValueError, TypeError):
            return None, None
    return None, None


def _qinfo(store_folder, canonical, q_start, q_end):
    """构造查询范围描述字符串（拼进返回文本，让模型知道数据口径）"""
    return {
        "store": store_folder, "type": canonical,
        "start": q_start.isoformat() if q_start else None,
        "end": q_end.isoformat() if q_end else None,
    }


# ============================================================
# 文件发现
# ============================================================

def find_reports(store=None, type_=None, month=None, start=None, end=None,
                 date_=None, limit: int = 200) -> dict:
    """按 店铺/类型/区间 找出覆盖该区间的报表文件，并给出覆盖缺口提示"""
    q_start, q_end = _query_window(month, start, end, date_)
    store_folder = normalize_store(store) if store else None
    canonical = normalize_type(type_) if type_ else None

    results = []
    if not os.path.isdir(UNIFIED_ROOT):
        return {"ok": False, "error": f"本地报表库不存在: {UNIFIED_ROOT}", "files": []}
    for dirpath, _dirs, fnames in os.walk(UNIFIED_ROOT):
        for fn in sorted(fnames):
            if not fn.lower().endswith(".csv") or "NODATA" in fn:
                continue
            m = _FILE_RE.match(fn)
            if not m:
                continue
            f_store = m.group("store")
            f_type_cn = m.group("type_cn")
            if store_folder and f_store != store_folder:
                continue
            if canonical and TYPE_CN[canonical] not in f_type_cn:
                continue
            try:
                f_start = datetime.strptime(m.group("start"), "%Y-%m-%d").date()
                f_end = datetime.strptime(m.group("end"), "%Y-%m-%d").date()
            except ValueError:
                continue
            if q_start and q_end:
                s = max(f_start, q_start)
                e = min(f_end, q_end)
                if s > e:
                    continue
                overlap_days = (e - s).days + 1
            else:
                overlap_days = (f_end - f_start).days + 1
            results.append({
                "file": os.path.join(dirpath, fn),
                "filename": fn,
                "store": f_store,
                "type_cn": f_type_cn,
                "type": canonical or f_type_cn,
                "start": f_start.isoformat(),
                "end": f_end.isoformat(),
                "period_days": (f_end - f_start).days + 1,
                "overlap_days": overlap_days,
                "size": os.path.getsize(os.path.join(dirpath, fn)),
            })

    if not results:
        hint = f"店铺 {store_folder or '全部'} / 类型 {canonical or '全部'}"
        return {"ok": True, "files": [], "error": None,
                "message": f"本地 unified 报表库未找到匹配: {hint}（区间: {q_start} ~ {q_end}）。"
                           "本地只含已归档文件，不触发任何下载。",
                "query": _qinfo(store_folder, canonical, q_start, q_end)}

    results.sort(key=lambda r: (-r["overlap_days"], r["period_days"]))
    files = results[:limit]

    if q_start and q_end:
        q_len = (q_end - q_start).days + 1
        containers = [r for r in files if r["overlap_days"] == q_len]
        if containers:
            keep = [min(containers, key=lambda r: r["period_days"])]
        else:
            keep = []
            for r in files:
                rs, re_ = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
                if any(max(rs, date.fromisoformat(k["start"])) <=
                       min(re_, date.fromisoformat(k["end"])) for k in keep):
                    continue
                keep.append(r)
        files = keep

    return {"ok": True, "files": files, "error": None,
            "query": _qinfo(store_folder, canonical, q_start, q_end)}


def _coverage_gaps(files, q_start, q_end):
    """对比查询区间与文件实际覆盖区间，返回未覆盖的日期段"""
    if not q_start or not q_end or not files:
        return ""
    covered = []
    for f in files:
        s = date.fromisoformat(f["start"])
        e = date.fromisoformat(f["end"])
        if s <= q_end and e >= q_start:
            covered.append((max(s, q_start), min(e, q_end)))
    if not covered:
        return f"⚠ 查询区间 {q_start}~{q_end} 无任何归档文件覆盖。"
    covered.sort()
    merged = []
    for s, e in covered:
        if merged and s <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    gaps, cur = [], q_start
    for s, e in merged:
        if s > cur:
            gaps.append((cur, s - timedelta(days=1)))
        cur = max(cur, e + timedelta(days=1))
    if cur <= q_end:
        gaps.append((cur, q_end))
    if not gaps:
        return ""
    text = "；".join(f"{s.isoformat()} ~ {e.isoformat()} 未归档" for s, e in gaps)
    return f"⚠ 注意：区间 {text} 本地无归档数据（本地库不触发下载）。"


# ============================================================
# 读取与归一化
# ============================================================

def _clean_str(v):
    """清洗单元格字符串（去掩码/空白）"""
    if isinstance(v, str) and v.strip().startswith('="'):
        m = re.fullmatch(r'="(.*)"', v.strip())
        if m:
            return m.group(1)
    return v


def load_report(path: str) -> dict:
    """读取单个 unified CSV，归一化核心列，返回 {df, currency, daterange, type_hint}。"""
    df = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str, keep_default_na=False)
            if df.shape[1] >= 8:
                break
        except Exception:
            df = None
    if df is None:
        raise ValueError(f"CSV 解析失败: {path}")
    df = df.map(_clean_str)

    def find(*keys):
        """按候选列名在表头中查找列（返回列名或 None）"""
        for c in df.columns:
            if str(c) == keys[0]:
                return c
        for c in df.columns:
            if any(k in str(c) for k in keys):
                return c
        return None

    # 核心指标
    spend = find("总成本")
    if not spend:
        raise ValueError(f"缺少核心指标列(总成本): {list(df.columns)[:8]}")
    cur = find("预算货币")
    dr = find("日期范围")

    df["_ccy"] = df[cur].astype(str).str.strip().str.upper() if cur else "?"
    df["_spend"] = df[spend].map(_to_float)
    for key, cn in (("_sales", "销售额"), ("_orders", "购买量"),
                    ("_clicks", "点击量"), ("_impressions", "展示量")):
        c = find(cn)
        df[key] = df[c].map(_to_float) if c else 0.0
    # 实体列
    for key, cn in STD_COLS.items():
        if key == "currency":
            continue
        src = GROUP_KEYS.get(key)
        if src:
            c = find(src)
            if c:
                df[cn] = df[c].astype(str).str.strip()
    return {"df": df, "currency": cur, "daterange": dr}


# ============================================================
# 聚合
# ============================================================

def _make_agg():
    """新建聚合累加器（各指标初值为 0）"""
    return {"spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0, "impressions": 0}


def _fmt_total(rec):
    """把聚合累加器格式化成一行\"总计\"文本（含 ACOS/CPC/CTR/CVR/ROAS）"""
    acos = f"{rec['spend'] / rec['sales'] * 100:.1f}%" if rec["sales"] > 0 else "-"
    cpc = f"{rec['spend'] / rec['clicks']:.2f}" if rec["clicks"] > 0 else "-"
    cvr = f"{rec['orders'] / rec['clicks'] * 100:.1f}%" if rec["clicks"] > 0 else "-"
    return (f"花费 {rec['spend']:,.2f} | 销售 {rec['sales']:,.2f} | 订单 {rec['orders']:,} | "
            f"点击 {rec['clicks']:,} | 展示 {rec['impressions']:,} | ACOS {acos} | "
            f"CPC {cpc} | CVR {cvr}")


def aggregate_to_text(store, type_, month=None, start=None, end=None, date_=None,
                      group_by=None, campaign_contains=None, name_contains=None,
                      asins=None, currency=None, ad_type=None,
                      min_spend=None, only_zero_orders=False,
                      min_orders=3, sort_by="spend", top_n=20, anomalies=True) -> str:
    """★ 核心：把报表文件聚合成模型可读的文本（**按币种分组**，绝不跨币种相加）"""
    canonical = normalize_type(type_)
    store_folder = normalize_store(store)
    if not canonical:
        return ("[参数错误: type 必须为 search_term(搜索词)/targeting(投放)/products(推广的商品)/"
                "placement(广告位)/campaigns(广告活动)/audience(受众) 之一]")
    if not store_folder:
        return (f"[参数错误: store={store} 无法识别。可用店铺: well / stx / lmg / mgt / mgk]")
    if not (month or start or end or date_):
        return "[参数错误: 请提供时间范围 month('YYYY-MM') 或 start/end('YYYY-MM-DD') 或 date]"

    if canonical == "audience":
        found = find_reports(store=store, type_=type_, month=month, start=start, end=end, date_=date_)
        files = (found.get("files") or []) if found.get("ok") else []
        if not files:
            return "本地 unified 库无受众(audience)归档文件。受众定向当前无可用数据，无法做细分受众诊断。"
        return ("本地受众(audience)报表数据极少(每期仅 1 行聚合，如 MGT 仅 EUR 1 行；多数店铺为 NODATA)。"
                "无法做细分受众诊断，请勿编造受众效果结论。")

    found = find_reports(store=store, type_=type_, month=month, start=start, end=end, date_=date_)
    if not found.get("ok"):
        return f"[本地报表库访问失败: {found.get('error')}]"
    files = found.get("files") or []
    if not files:
        return found.get("message") or "[本地报表库无匹配报告]"

    q = found["query"]
    q_start = date.fromisoformat(q["start"]) if q.get("start") else None
    q_end = date.fromisoformat(q["end"]) if q.get("end") else None

    group_key = None
    if group_by:
        gk = str(group_by).strip().lower()
        if gk == "currency":
            group_key = "currency"
        elif gk in GROUP_KEYS:
            group_key = gk
        else:
            return (f"[group_by 无效: {group_by}]。可选: campaign/adgroup/term/targeting/asin/"
                    f"product/placement/placement_name/ad_type/marketplace/currency")

    # ---- 汇总 ----
    total_cur = {}
    group_cur = {}    # (entity, ccy) -> agg
    group_n = {}      # entity -> rows
    row_total = 0
    approx = False
    file_lines = []

    for f in files:
        try:
            rep = load_report(f["file"])
        except Exception as e:
            file_lines.append(f"✗ 读取失败 {f['filename']}: {e}")
            continue
        df = rep["df"]
        f_start, f_end = date.fromisoformat(f["start"]), date.fromisoformat(f["end"])
        # 行级日期近似切分
        if (q_start and q_end and (f_start < q_start or f_end > q_end)
                and rep.get("daterange") and rep["daterange"] in df.columns):
            s_, e_ = [], []
            for raw in df[rep["daterange"]].tolist():
                parts = re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", str(raw))
                a = _parse_cn_date(parts[0]) if parts else None
                b = _parse_cn_date(parts[1]) if len(parts) > 1 else a
                s_.append(a)
                e_.append(b)
            mask = []
            for a, b in zip(s_, e_):
                if a is None or b is None:
                    mask.append(True)
                else:
                    mask.append(a <= q_end and b >= q_start)
            if any(mask) and not all(mask):
                df = df[mask].copy()
                approx = True
        row_total += len(df)

        for _, row in df.iterrows():
            ccy = str(row.get("_ccy", "?")).strip().upper() or "?"
            if currency and ccy != str(currency).strip().upper():
                continue
            if campaign_contains:
                camp = str(row.get("_campaign", "")).strip()
                if campaign_contains.lower() not in camp.lower():
                    continue
            if ad_type:
                at = str(row.get("_ad_type", "")).strip()
                if at and str(ad_type).lower() not in at.lower():
                    continue
            if asins:
                a = str(row.get("_asin", "")).strip()
                if a and a not in [str(x).strip() for x in asins]:
                    continue

            agg = total_cur.setdefault(ccy, _make_agg())
            agg["spend"] += row.get("_spend", 0) or 0
            agg["sales"] += row.get("_sales", 0) or 0
            agg["orders"] += int(row.get("_orders", 0) or 0)
            agg["clicks"] += int(row.get("_clicks", 0) or 0)
            agg["impressions"] += int(row.get("_impressions", 0) or 0)

            if group_key and group_key != "currency":
                col = STD_COLS[group_key]
                entity = str(row.get(col, "")).strip()[:80] or "(空)"
                g = group_cur.setdefault((entity, ccy), _make_agg())
                g["spend"] += row.get("_spend", 0) or 0
                g["sales"] += row.get("_sales", 0) or 0
                g["orders"] += int(row.get("_orders", 0) or 0)
                g["clicks"] += int(row.get("_clicks", 0) or 0)
                g["impressions"] += int(row.get("_impressions", 0) or 0)
                group_n[entity] = group_n.get(entity, 0) + 1
        file_lines.append(f"✓ {f['filename']} ({f['start']}~{f['end']}, {len(df)}行)")

    if not total_cur:
        return "[筛选条件下无任何数据行(检查币种/时间/名称/广告类型过滤条件)]"

    out = [f"报表来源: 本地 unified 库 / {store_folder} / {canonical}({TYPE_CN[canonical]}) / {q_start} ~ {q_end}"]
    out.append("命中文件:")
    out.extend(f"  {x}" for x in file_lines[:10])
    if len(file_lines) > 10:
        out.append(f"  ... 共 {len(file_lines)} 个文件")
    gap = _coverage_gaps(files, q_start, q_end)
    if gap:
        out.append(gap)
    if canonical == "placement":
        out.append("注: 广告位文件为流量行(Unknown)/转化行(空)拆分行结构，已按实体合并计算；行内 0 单/0 花费不是真实 0。")
    if approx:
        out.append("注: 查询区间窄于文件周期，已按行级日期范围近似切分(跨边界行整行计入)。")

    out.append("")
    if len(total_cur) == 1:
        ccy = next(iter(total_cur))
        out.append(f"【区间总览({ccy})】")
        out.append(f"总计 | {ccy} | {_fmt_total(total_cur[ccy])} | 行数 {row_total}")
    else:
        out.append(f"【区间总览(按币种, {len(total_cur)} 种币种, 不做汇率折算)】")
        for ccy in sorted(total_cur, key=lambda c: -total_cur[c]["spend"]):
            out.append(f"总计 | {ccy} | {_fmt_total(total_cur[ccy])}")
        out.append("说明: 不同币种金额不可直接相加; 单站点口径用 currency 参数(GBP=英国/EUR=欧元区/USD=美国等)筛选。")

    # ---- 明细 Top N ----
    if group_key and group_key != "currency":
        out.append("")
        out.append(f"【按 {GROUP_KEYS[group_key]} 分组 Top {top_n}(每币种独立排序)】")
        rows_by_ccy = {}
        for (entity, ccy), g in group_cur.items():
            rows_by_ccy.setdefault(ccy, []).append((entity, g))
        for ccy in sorted(rows_by_ccy, key=lambda c: -total_cur[c]["spend"]):
            items = rows_by_ccy[ccy]
            if name_contains:
                items = [it for it in items if name_contains.lower() in it[0].lower()]
            if min_spend is not None:
                items = [it for it in items if it[1]["spend"] >= float(min_spend)]
            if only_zero_orders:
                items = [it for it in items if it[1]["orders"] == 0]
            kf = "orders" if sort_by == "orders" else ("sales" if sort_by == "sales" else "spend")
            items.sort(key=lambda it: it[1][kf], reverse=True)
            out.append(f"币种 {ccy}:")
            for entity, g in items[:top_n]:
                acos = f"{g['spend'] / g['sales'] * 100:.1f}%" if g["sales"] > 0 else "-"
                cvr = f"{g['orders'] / g['clicks'] * 100:.1f}%" if g["clicks"] > 0 else "-"
                out.append(f"  {entity[:48]} | 花费 {g['spend']:,.2f} | 销售 {g['sales']:,.2f} | "
                           f"订单 {g['orders']:,} | 点击 {g['clicks']:,} | ACOS {acos} | CVR {cvr}")
            if len(items) > top_n:
                out.append(f"  ... 共 {len(items)} 个")

        # ---- 异常/机会清单 ----
        if anomalies:
            ms = float(min_spend) if min_spend is not None else 2.0
            zero = sorted([(e, c, g) for (e, c), g in group_cur.items()
                           if g["orders"] == 0 and g["spend"] >= ms],
                          key=lambda t: -t[2]["spend"])[:5]
            opp = sorted([(e, c, g) for (e, c), g in group_cur.items()
                          if g["orders"] >= int(min_orders) and g["sales"] > 0
                          and (g["spend"] / g["sales"] * 100) < 25],
                         key=lambda t: -t[2]["orders"])[:5]
            out.append("")
            out.append("【异常清单(有花费 0 单, 浪费)】")
            if zero:
                for e, c, g in zero:
                    out.append(f"  {e[:48]} [{c}] 花费 {g['spend']:,.2f} 点击 {g['clicks']:,} 0单")
            else:
                out.append("  无(当前阈值下没有零转化高花费项)")
            out.append("【机会清单(高单量低ACOS, 潜力)】")
            if opp:
                for e, c, g in opp:
                    acos = g["spend"] / g["sales"] * 100
                    out.append(f"  {e[:48]} [{c}] 订单 {g['orders']:,} 销售 {g['sales']:,.2f} ACOS {acos:.1f}%")
            else:
                out.append("  无(当前阈值下没有明显潜力项)")
    return "\n".join(out)


# ============================================================
# 清单工具
# ============================================================

def list_reports_text(store=None, type_=None, month=None, start=None, end=None,
                      date_=None) -> str:
    """列出某店铺/类型/区间的可用报表清单（含覆盖日期）"""
    found = find_reports(store=store, type_=type_, month=month, start=start, end=end, date_=date_, limit=200)
    if not found.get("ok"):
        return f"[本地报表库访问失败: {found.get('error')}]"
    files = found.get("files") or []
    if not files:
        return found.get("message") or "[本地报表库无匹配报告]"
    q = found["query"]
    lines = [f"本地 unified 报表库匹配 {len(files)} 个文件(店铺 {q.get('store') or '全部'} / "
             f"类型 {q.get('type') or '全部'} / {q.get('start') or '?'}~{q.get('end') or '?'}):"]
    for f in files:
        lines.append(f"- {f['store']} | {f['type_cn']} | {f['start']}~{f['end']} | "
                     f"{f['period_days']}天 | {f['filename']}")
    return "\n".join(lines)


def list_all_inventory_text() -> str:
    """列出报表库全部库存（哪些店铺/类型/月份有数据）"""
    if not os.path.isdir(UNIFIED_ROOT):
        return "[本地报表库不存在]"
    inv = {}
    for store in STORE_FOLDERS:
        base = os.path.join(UNIFIED_ROOT, store)
        if not os.path.isdir(base):
            continue
        inv[store] = {}
        for y in sorted(os.listdir(base)):
            yp = os.path.join(base, y)
            if not os.path.isdir(yp) or not y.isdigit():
                continue
            for m in sorted(os.listdir(yp)):
                mp = os.path.join(yp, m)
                if not os.path.isdir(mp):
                    continue
                types = set()
                for fn in os.listdir(mp):
                    mt = _FILE_RE.match(fn)
                    if mt:
                        types.add(mt.group("type_cn"))
                if types:
                    inv[store].setdefault(f"{y}-{int(m):02d}", set()).update(types)
    lines = ["本地 unified 报表库库存(店铺 | 年月 | 已有报表类型):"]
    for store in STORE_FOLDERS:
        if store not in inv:
            lines.append(f"- {store}: (无)")
            continue
        for ym in sorted(inv[store], reverse=True):
            ts = "/".join(sorted(inv[store][ym]))
            lines.append(f"- {store} | {ym} | {ts}")
    return "\n".join(lines)
