# LMG 平台逆向重构需求文档

> 文档类型：技术专项（逆向分析 → 重构需求）
> 分析对象：`LMG平台完整包_20260915-134711`（lmg-platform / ads_agent / ziniao_automation + 3 份 MySQL 导出）
> 用途：指导 ERP 进阶版重构，**继承 LMG 已验证的业务口径，规避其已实证的缺陷**
> 关联文档：`02-功能需求/01-报表导入管理.md`（MM-01）、`02-功能需求/05-成本与利润核算.md`（MM-05，**算法权威口径**）、`04-技术需求/02-数据模型.md`、`04-技术需求/03-报表解析规范.md`
> 最后更新：2026-10-15
> 状态：📄 初稿（待评审）

---

## 1. 文档定位与 LMG 现状速览

### 1.1 文档目的

LMG 平台是一套**已在真实业务数据上运行过**的亚马逊多店铺利润核算系统。本 ERP 的核心业务问题（多店铺报表导入、利润核算、汇率折算、成本补录）与 LMG 高度同源。本文档回答三个问题：

1. **导入脚本该怎么做**——把 LMG 已跑通的解析/清洗逻辑提炼为可实施的脚本级规范（第 3 章）；
2. **利润算法该怎么算**——以实测数据逐字固化算法口径，作为重构的最高优先级规范（第 4 章，**最重要**）；
3. **前端能不能留用**——对 LMG React 前端做 Bug 清单与剥离可行性评估，给出路线建议（第 8 章）。

**关系约定**：本文档与 MM-01 / MM-05 冲突时，**业务口径以 MM-05 为准，工程实现细节以本文档为准**。本文档不重复 MM 文档的功能清单，只补充"LMG 实证经验 → ERP 落地规范"。

### 1.2 LMG 平台组成速览

| 子系统 | 技术栈 | 端口 | 职责 | 重构取舍 |
|---|---|---|---|---|
| lmg-platform/backend | Python + FastAPI + SQLAlchemy + MySQL | 8000 | 报表导入、利润核算、查询/看板/导出、用户权限 | **核心参照**，本文档第 3–6 章 |
| lmg-platform/frontend | React 18 + Antd 5 + ECharts + Vite 5（纯 JS） | 5173 | 登录、导入、查询、看板、导出、周报 | 评估剥离（第 8 章） |
| ads_agent | FastAPI + AI Agent（审计会话/消息/行动项） | 8010 | 广告审计对话助手 | Phase 2，不在本期 |
| ziniao_automation | 紫鸟浏览器 RPA + 企微/飞书机器人 | 8001 | 自动下载报表、通知推送 | Phase 2（MM-01 F-01-18 预留） |
| MySQL 数据导出 ×3 | 23 表 | — | dim_* 主数据 7 表 + raw_* 原始层 7 表 + monthly_summary + users/周报/广告审计 8 表 | 第 5 章给出 → ERP PG 模型映射 |

### 1.3 LMG 数据流（逆向还原）

```
运营手工导出报表(xlsx)
   → 前端上传 /import/workbook|/import/transaction|/import/storage|...
   → import_data.py 解析（表头定位/多语言列映射/类型归一）
   → raw_transactions / raw_storage_fee / raw_advertising / raw_returns / raw_inbound / raw_long_term_storage / raw_removal_fee
   → 聚合写 monthly_summary（产品 × 店铺 × 国家 × 月）
   → profit.py 计算净利润（唯一公式入口）
   → 前端 dashboard / query / export 展示
```

ERP 将其重构为 **ODS → DWD → DWS 三层**（见 `04-技术需求/02-数据模型.md`），并把"导入"统一为**单一引擎**而非 LMG 的 8 个独立端点。

---

## 2. LMG 缺陷清单与重构规避措施

> 以下缺陷均已在 LMG 代码/数据/日志中实证（含行号证据）。**每一项都映射为 ERP 的强制设计约束**，验收时逐项核对。

| # | 缺陷 | LMG 证据 | 根因 | ERP 强制规避措施 |
|---|---|---|---|---|
| D1 | **仓储费重复导入累积 5–6 倍** | `raw_storage_fee` 无唯一约束，幂等仅靠"先删后插"；某次删除失败后数据放大。作者后期补丁（import_data.py L1565/L1712）把累计改成累加，掩盖了数据错误 | 数据库层无防重 | **费用类 fact 表必须建业务唯一约束**（`UNIQUE(store_id, country, period, sku/asin, fee_type)`），写入用 `INSERT ... ON CONFLICT` 明确语义（更新或跳过，禁止无约束累加）。导入完成后执行**行数对账断言**：本次写入行数 + 库中原有行数 = 预期行数，超预期立即告警回滚 |
| D2 | **长期仓储费与普通仓储费互相覆盖** | 两者共用 `summary.storage_fee_usd` 一个字段，先后导入互相覆盖，金额丢失 | 汇总字段粒度不足 | `dws_profit_snapshot` 中 **`storage_fee` 与 `long_term_storage_fee` 必须独立字段**；扣减项分别列出可下钻 |
| D3 | **workbook 导入清空口径不一致** | raw 层按"店铺"全量清、summary 层按"店铺+月"清 → 重导 1 个月数据时其他月的原始行被删、汇总却还在（或反之），永久错位 | 清空范围两条代码路径各写各的 | **清空口径唯一化**：定义函数 `resolve_scope(store, report_type, period) → 数据范围`，ODS/DWD/DWS 三层的删除/覆盖**必须复用同一函数**推导范围，禁止各层自行计算；覆盖操作包裹在同一事务 |
| D4 | **仓储费月份归属错位** | EU 店铺 raw 行 `month_of_charge=2026-04`，summary 却记入 2026-05 | 归属月份用了"导入上下文月份"而非"费用账期字段" | 月度费用（仓储/退货处理/入库配置等）**归属月份只取报表内的账期字段**（如 `month_of_charge`），缺账期字段才允许降级为业务日期推导并**标记推断来源**；月中归属规则 = 费用区间与自然月有交集即计入（见项目约束） |
| D5 | **汇率静默降级** | `profit.py get_exchange_rate()`：店铺精确月 → 店铺任意最新月 → **硬编码 DEFAULT_RATES（US 6.8 / UK 9.0 / 欧元区 7.5…）**，全程无告警 | 缺失被当作可容忍 | **禁三级降级**：取数顺序仅两级（店铺+月 → 全局+月）；两级都缺 → **报错并阻断核算**（或仅按用户显式确认的"缺省汇率继续"开关放行，且结果强制打上 `exchange_rate_missing` 标记）。默认汇率表只能作为**种子数据**入库维护，不允许出现在代码常量里参与计算 |
| D6 | **利润公式符号脆弱** | `compute_net_profit()` 把 `selling_fee/fba_fee/adjustment` 直接相加，依赖"报表原值为负"这一隐式约定；无任何符号校验（profit.py L71–83） | 隐式约定无断言 | **行级符号断言**：DWD 入库时校验 `selling_fee ≤ 0`、`fba_fee ≤ 0`（正常业务语义），出现正值 → 该行进异常池并告警"符号约定破坏，请人工确认报表格式变更"；`net_amazon` 聚合前再做一次分桶符号审计（4.9 节） |
| D7 | **权限模型落地不完整** | 店铺级过滤 `filter_store_by_permission` 仅 weekly_report.py 调用；dashboard / advertising / query / export 全无店铺校验；字段级仅 export.py L339 过滤成本利润，query/dashboard 明文返回 | 权限做成"可选装饰"而非架构强制 | **所有业务路由强制经过统一依赖注入**（`Depends(require_scoped_store)` / `Depends(field_acl)`），店铺级 + 账期级 + 字段级三层在**路由层统一挂载**，任何绕过直接过不了 code review 清单：grep 无 `require_scoped_store` 的业务路由 = 不合入 |
| D8 | **认证与安全缺陷** | JWT `SECRET_KEY` 硬编码且仅 28 字节（日志出现 InsecureKeyLengthWarning）；auth.py `import jwt`（PyJWT）但 requirements.txt 只写 python-jose → 换机 ImportError；CORS `allow_origins=["*"]` 且 `allow_credentials=True`；默认账号 admin/123456；token 无失效机制 | 安全基线缺失 | 密钥等敏感配置**全部环境变量注入**（.env + Docker secrets），长度 ≥ 48 字节；依赖清单与实际 import **逐一对账**（CI 加 import 检查）；CORS 白名单显式列出前端 origin；首启强制改默认密码；登出/改密后旧 token 失效（黑名单或短有效期+refresh） |
| D9 | **自动化层脆弱** | ziniao RPA fail_shots 目录 15 张失败截图；`read_apikey.js` 已删但调用残留（.apikey_err）；mfa_code.txt 明文；曾因 10048 端口占用启动失败 | RPA 无契约、无健康检查 | 本期**不开发自动化**（Phase 2 仅留 Watch Folder / HTTP 接口约定，见 MM-01 F-01-18）；所有子服务端口由 docker-compose 统一编排 + 启动前端口探测；密钥文件禁止落盘明文 |
| D10 | **工程卫生问题** | 仓库根目录 40+ 一次性 calc_/diag_/audit_ 脚本；lmg.db、死库 lmg_db（7 张 fact_* 表）、旧构建 dist 混入包内；requirements 与实际依赖不符 | 缺乏工程约束 | ERP 仓库约定：`scripts/` 目录一次性脚本用完即删或归档 `scripts/archive/`；数据库 schema 单一来源（Alembic 迁移），禁止并行出现第二套 schema；CI 校验依赖清单 |

**验收总则**：ERP 联调阶段用 LMG 当时的真实报表（含 2026-05/06 UK/US/EU 数据）回放导入，`net_amazon` 与净利润须与 MM-05 5.4 手工复算一致；仓储费重导 3 次，行数与金额必须不增不减（对应 D1）。

---

## 3. 核心导入引擎详细规范

> 对应 MM-01（F-01-1 ~ F-01-17）。LMG 的导入是 **8 个独立端点各自为政**（transaction / product-info / advertising / storage / returns / inbound / workbook / folder），解析逻辑复制粘贴多份且口径漂移。ERP 收敛为**一个统一引擎 + 一张配置驱动的报表规格表**。

### 3.1 总体流程（11 阶段）

```
[阶段0] 上传受理        hash 查重 → 建 batch → 立即返回 batch_id
[阶段1] 元数据解析      文件名 → 店铺/大区（容忍噪声）
[阶段2] Sheet 类型识别  别名词典 → 标准报表类型；未识别仍全量入 ODS
[阶段3] 动态表头定位    扫描前 N 行找"像表头"的行（销售 sheet 实测第 10 行）
[阶段4] 列映射          多语言别名词典 → 标准字段
[阶段5] ODS 全量落库    每行每列原样入库 raw_sheet_row（按月分区，不可变）
[阶段6] DWD 清洗        类型归一/数值/日期解析 → fact_*；失败行入异常池不阻塞
[阶段7] 幂等与覆盖      唯一约束 + 跳过/覆盖/并存（默认跳过）；锁结禁止覆盖
[阶段8] 后台任务与进度  上传即返回；进度可查询；刷新不中断
[阶段9] 导入报告        行级成败 + 成本缺失区块（4 态，联动 MM-05）
[阶段10] 锁结           batch.is_locked；解锁二次确认留痕
```

### 3.2 报表规格表（引擎的配置中心）

引擎不写死任何一种报表，全部行为由**报表规格表**驱动（管理员可扩展，对应 MM-01 F-01-4）：

```python
# sheet_spec：标准报表类型 → 解析规则（DB 表 sheet_alias_map + 配置项，可页面维护）
SHEET_SPECS = {
    "销售": SheetSpec(
        aliases=["UK销售", "US销售", "DE销售", "FA销售", "CA销售",
                 "US 销售明细", "CA 4-5销售明细", ...],      # 持续登记
        header_scan_rows=30,            # 实测表头在第 10 行，扫描上限放宽到 30
        biz_date_field="date/time",
        fact_target="fact_sales_transaction",
        dedup_keys=["store", "country", "biz_date", "order_id", "type", "sku", "line_total"],
    ),
    "广告": SheetSpec(aliases=["UK广告", "US广告", "US广告费", ...],
        fact_target="fact_ad_daily", dedup_keys=["store", "country", "biz_date", "asin", "sku"]),
    "仓储费": SheetSpec(aliases=["仓储费"], period_field="month_of_charge",
        fact_target="fact_fee_storage",
        dedup_keys=["store", "country", "period", "sku"]),   # D1 对策
    "超龄仓储费": SheetSpec(aliases=["超龄仓储费"], period_field="month_of_charge",
        fact_target="fact_fee_storage", fee_kind="long_term"),  # D2 对策：同表不同 fee_kind
    "退货处理费": SheetSpec(aliases=["退回处理费", "退货处理费"], period_field="month_of_charge",
        fact_target="fact_fee_returns", ...),
    "移除费": SheetSpec(aliases=["移除费"], ...),
    "入库配置费": SheetSpec(aliases=["入库配置费"], ...),
    "产品表": SheetSpec(aliases=["产品信息表", "产品明细表"],
        fact_target=None,   # 不进 fact，写主数据 + product_cost_history
        cost_fields={"成本RMB": "cost_rmb", "运费": "freight_json"}),
    "汇率": SheetSpec(aliases=["汇率"], fact_target=None),   # 写 exchange_rate
}
```

规则：
- 别名匹配用**归一化相等**（去空格/全半角/大小写）+ 可配置正则兜底；
- 未命中任何规格 → `report_type="未识别"`，**仍然全量入 ODS**（F-01-17），报告提示人工归类；
- 新别名由管理员页面增删，**不改代码即可生效**。

### 3.3 阶段 0：上传受理与 hash 查重

```python
@router.post("/api/imports/files")
async def upload_files(files: list[UploadFile], strategy: Literal["skip","overwrite","coexist"]="skip",
                       force: bool = False, user = Depends(require_scoped_store("import"))):
    accepted = []
    for f in files:
        content = await f.read()
        file_hash = sha256(content).hexdigest()

        # hash 查重（F-01-11）
        if existing := batch_repo.find_by_hash(file_hash):
            if not force:
                raise HTTPException(409, detail={
                    "code": "DUPLICATE_FILE",
                    "message": f"疑似重复导入：{f.filename} 与批次 {existing.batch_id} hash 相同",
                    "existing_batch": existing.batch_id,
                    "hint": "如确需重导请勾选强制导入（将记录审计日志）",
                })
            audit.log(user, "IMPORT_FORCE_DUPLICATE", batch_id=existing.batch_id)

        store = parse_store_from_filename(f.filename)     # 阶段1
        batch = batch_repo.create(
            file_name=f.filename, file_hash=file_hash, store_id=store,
            report_type=None,       # 引擎内按 sheet 判定
            strategy=strategy, imported_by=user.id,
            status="进行中",
        )
        file_repo.archive(batch.batch_id, content)         # 原样归档 xlsx（import_file）
        task_queue.enqueue(run_import_pipeline, batch.batch_id)
        accepted.append({"batch_id": batch.batch_id, "file_name": f.filename, "status": "已受理"})
    return accepted        # 立即返回，不等待解析（F-01-15）
```

要点：
- 上传接口**只做受理**，解析全部在后台任务（阶段 8）；
- `require_scoped_store`：店铺级数据权限在路由层强制（D7 对策），运营只能传本部门店铺的报表；
- 原始文件**先落盘归档**（`import_file.stored_path`），解析失败也有据可查。

### 3.4 阶段 1：文件名元数据解析

```python
def parse_store_from_filename(filename: str) -> str | None:
    """实测样例：
       MGK-EU-汇总06.xlsx → MGK / EU
       MGT-EU 06.xlsx     → MGT / EU（含空格）
       LMG-NA-...xlsx     → LMG / NA
    """
    stem = Path(filename).stem
    for sep in ("_", "-", " "):
        stem = stem.replace(sep, "|")
    tokens = [t for t in stem.split("|") if t and t != "汇总"]
    store = next((t for t in tokens if t.upper() in STORE_CODES), None)   # LMG/MGK/MGT/STX/WEL
    region = next((t for t in tokens if t.upper() in ("NA", "EU")), None)
    return store      # region 存入 batch 元数据；解析失败 → store_id=NULL 标记"店铺待确认"，人工归属
```

### 3.5 阶段 2：Sheet 类型识别

```python
def classify_sheet(sheet_name: str) -> str:
    for type_code, spec in SHEET_SPECS.items():
        for alias in spec.aliases:
            if normalize(sheet_name) == normalize(alias):
                return type_code
    for type_code, spec in SHEET_SPECS.items():        # 正则兜底
        if spec.pattern and spec.pattern.match(sheet_name):
            return type_code
    return "未识别"
```

### 3.6 阶段 3：动态表头定位（关键）

实测结论（MM-01 5.3）：销售类 sheet **前 9 行为多语言说明文字**（含法文 `Définitions` 等），**真正表头在第 10 行**，且不同国家站说明行数可能不同 → **绝不能写死行号**。

```python
def locate_header_row(rows: list[list], field_alias_set: set[str],
                      max_scan: int = 30) -> tuple[int, dict] | None:
    """逐行打分：命中'已知字段别名'的列数越多越像表头。
    返回 (表头行号, {列名→列索引})；找不到返回 None。"""
    best = None
    for i, row in enumerate(rows[:max_scan]):
        non_empty = [c for c in row if c not in (None, "")]
        if len(non_empty) < 4:                       # 说明文字行通常列很少
            continue
        hits = {c: j for j, c in enumerate(row)
                if c and normalize_to_field(c, field_alias_set)}   # 阶段4 的反向映射
        score = len(hits)
        if score >= max(3, int(0.4 * len(non_empty))):
            if best is None or score > best[2]:
                best = (i, hits, score)
    return (best[0], best[1]) if best else None
```

- 识别失败 → 该 sheet 标记"表头无法识别"，**仍全量入 ODS**，报告提示；
- 同一 workbook 内不同 sheet 表头行不同，**每个 sheet 独立定位**。

### 3.7 阶段 4：列映射（多语言别名词典）

LMG 的经验（import_data.py 内嵌多语言 type 字典：order/refund/adjustment 的西/德/法/意/荷/瑞典/波兰语形式）证明必须支持多语言。ERP 把它升级为**两级词典**：

```python
# ① 列名 → 标准字段（销售 29 列，完整清单见 4.2）
COLUMN_ALIASES = {
    "product sales":              "product_sales",
    "product sales tax":          "product_sales_tax",
    "postage credits":            "postage_credits",       # 即 Shipping credits
    "shipping credits tax":       "shipping_credits_tax",
    "gift wrap credits":          "gift_wrap_credits",
    "giftwrap credits tax":       "giftwrap_credits_tax",
    "promotional rebates":        "promo_rebate",
    "promotional rebates tax":    "promo_rebate_tax",
    "marketplace withheld tax":   "marketplace_withheld_tax",
    "selling fees":               "selling_fee",
    "fba fees":                   "fba_fee",
    "other":                      "other_amount",
    "other transaction fees":     "other_transaction_fees",   # 保留字段，不计入利润
    "total":                      "line_total",
    "type":                       "transaction_type",
    "quantity":                   "quantity",
    "order id":                   "order_id",
    "order-id":                   "order_id",                  # LMG 兼容过两种写法
    "settlement id":              "settlement_id",
    "date/time":                  "biz_date",
    ...
}

# ② type 值 → 标准交易类型（多语言，源自 LMG 实测字典）
TYPE_ALIASES = {
    "order": "Order", "refund": "Refund", "adjustment": "Adjustment",
    "pedido": "Order", "reembolso": "Refund", "ajuste": "Adjustment",       # 西
    "bestellung": "Order", "erstattung": "Refund", "anpassung": "Adjustment", # 德
    "commande": "Order", "remboursement": "Refund", "ajustement": "Adjustment", # 法
    "ordine": "Order", "rimborso": "Refund", "aggiustamento": "Adjustment",  # 意
    "bestelling": "Order", "terugbetaling": "Refund", "aanpassing": "Adjustment", # 荷
    "beställning": "Order", "återbetalning": "Refund", "justering": "Adjustment", # 瑞
    "zamówienie": "Order", "zwrot": "Refund", "korekta": "Adjustment",      # 波
    "service fee": "Service Fee", "transfer": "Transfer",
    "fba inventory fee": "FBA Inventory Fee",
    "fulfilment by amazon (fba) transaction fees": "FBA Transaction Fees",
    ...
}
```

- 词典存 DB（`sheet_alias_map` + `column_alias` 配置），支持页面维护与热生效；
- 未映射列**不丢弃**——原样留在 ODS（headers_json/values_json），DWD 层报告"未映射列清单"。

### 3.8 阶段 5：ODS 全量落库

```python
def write_ods_rows(batch, sheet_meta, rows, header_row_idx):
    headers = rows[header_row_idx]
    for row_no, row in enumerate(rows[header_row_idx + 1:], start=header_row_idx + 2):
        biz_date = try_parse_date(row, sheet_meta)   # 解析不出→NULL，进 DWD 异常池提示
        ods_repo.insert(batch_id=batch.batch_id,
                        sheet_id=sheet_meta.sheet_id,
                        sheet_name=sheet_meta.name,
                        row_no=row_no,
                        biz_date=biz_date,
                        headers_json=headers,        # 原字段名数组
                        values_json=row)             # 原值数组（原样，不转类型）
```

- 表 `raw_sheet_row` 按 `biz_date` **RANGE 按月分区**（`raw_row_2026_06`）；
- **不可变原则**：导入后不允许 UPDATE/DELETE，只能通过新批次覆盖（MM-01 5.8）；
- 任意单元格可通过 `(store, batch, sheet, row_no, col_idx)` 精确回查——这是全部对账争议的最终仲裁依据。

### 3.9 阶段 6：DWD 清洗与异常池

```python
def clean_sales_row(raw) -> CleanResult:
    errs = []
    m = {alias: raw.values[idx] for alias, idx in raw.header_map.items()}   # 列映射结果

    # ① type 归一
    txn_type = TYPE_ALIASES.get(str(m.get("transaction_type", "")).strip().lower())
    if txn_type is None:
        errs.append(f"未知交易类型: {m.get('transaction_type')!r}")

    # ② 数值列 → Decimal（禁 float）
    for f in NUMERIC_FIELDS:
        try: m[f] = to_decimal(m.get(f))
        except: errs.append(f"{f} 非数值: {m[f]!r}")

    # ③ 日期解析（三种实测格式）
    biz_date = parse_biz_date(m.get("biz_date"))
    if biz_date is None: errs.append("日期无法解析")

    # ④ 符号断言（D6 对策）
    if txn_type in ("Order", "Refund"):
        if dec(m, "selling_fee") > 0: errs.append("selling fees 出现正值，破坏负值约定")
        if dec(m, "fba_fee") > 0:     errs.append("fba fees 出现正值，破坏负值约定")

    # ⑤ 国家/币种识别（marketplace 列 → 兜底 sheet 名 → 兜底币种符号）
    country, currency = detect_country_currency(m, raw.sheet_meta)

    if errs:
        return CleanResult(ok=False, errors=errs, raw_row_id=raw.id)   # → 异常池
    return CleanResult(ok=True, fact=FactSalesTransaction(..., raw_row_id=raw.id))
```

- 异常池 = `import_error` 表（batch_id, sheet, row_no, 原值快照, 原因），**不阻塞同 sheet 其他行**（F-01-10）；
- 异常行在报告中可单条"修复重提"或"忽略"；
- `amzn.gr.*` 替换件 SKU：原样入库，DWD 层额外写 `replacement_sku → parent_sku` 关联（数据模型 2.6）。

### 3.10 阶段 7：幂等与覆盖策略（D1/D3 对策核心）

**这是 LMG 最痛的教训，以下三条为强制规范：**

**规范一：数据库唯一约束兜底，代码幂等只是第一道防线。**

```sql
-- 费用类：业务唯一键（防止重复导入金额翻倍——LMG raw_storage_fee 事故的直接对策）
CREATE UNIQUE INDEX uq_fee_storage
  ON fact_fee_storage (store_id, country, period, sku, fee_kind);   -- fee_kind: normal / long_term

-- 销售类：同一批次内行唯一（batch 内 row_no 唯一由 ODS 保证；
-- 业务重复由"hash 查重 + 批次策略"防，不做跨批全局唯一——并存策略需要允许同范围多批）
CREATE UNIQUE INDEX uq_ods_row
  ON raw_sheet_row (batch_id, sheet_id, row_no);
```

```python
# 费用类写入：ON CONFLICT 显式语义
INSERT INTO fact_fee_storage (...) VALUES (...)
ON CONFLICT (store_id, country, period, sku, fee_kind)
DO UPDATE SET amount = EXCLUDED.amount, raw_row_id = EXCLUDED.raw_row_id
   -- 覆盖策略：同键取后值；禁止 DO UPDATE SET amount = amount + EXCLUDED.amount（累加即 LMG 事故复刻）
```

**规范二：清空/覆盖口径唯一化（D3 对策）。**

```python
def resolve_scope(store_id, report_type, period_range) -> Scope:
    """唯一的范围推导函数：覆盖/清空/锁结校验全部经过它，
    ODS、DWD、DWS 三层删除范围由它一次推导，禁止各层自行计算。"""
    return Scope(store_id=store_id, report_type=report_type,
                 period_range=period_range, locked_batches=[...])

def apply_strategy(scope: Scope, strategy: str):
    if scope.locked_batches:                       # 锁结批次禁止覆盖（F-01-12）
        raise LockedBatchError(scope.locked_batches)
    if strategy == "skip":
        raise DuplicateScopeError("该范围已有数据，请选择覆盖或并存")
    if strategy == "overwrite":
        with db.transaction():                     # 三层同事务
            ods.delete(scope); dwd.delete(scope); dws.delete(scope)
    # coexist：不删，直接新 batch 写入；查询层按 batch 生效时间取"最新批次"口径
```

**规范三：导入后行数对账断言。**

```python
def assert_row_balance(batch, expected_per_sheet: dict):
    for sheet_id, expected in expected_per_sheet.items():
        actual_ods = count(raw_sheet_row.where(batch_id, sheet_id))
        actual_dwd = count(fact_*  .where(raw_row_id in ods_ids))
        if actual_ods != expected.data_rows:
            raise ImportIntegrityError(f"ODS 行数不符: sheet={sheet_id} {actual_ods}≠{expected}")
        if actual_dwd + err_count(sheet_id) != actual_ods:
            raise ImportIntegrityError("DWD+异常池 ≠ ODS，存在静默丢行")
```

### 3.11 阶段 8：后台任务与进度

- 部署形态（局域网 + Docker Compose）：**进程内任务队列**（FastAPI BackgroundTasks / 内置 worker 线程池）起步即可，接口层抽象 `task_queue`，未来可平滑换 Celery/ARQ；
- 进度落库（非内存）：`task_progress(task_id, batch_id, stage, done, total, message, updated_at)` —— 前端轮询 `GET /api/imports/batches/{batch_id}/progress`，**刷新页面/换浏览器任务不中断**（F-01-15 验收标准）；
- 每完成一个 sheet 更新一次进度；失败不影响后续 sheet，最终状态 = 成功 / 部分失败 / 失败。

### 3.12 阶段 9：导入报告

报告结构（对应 MM-01 5.13 + MM-05 5.10）：

```
导入报告 batch_20261015_001（MGK-EU-汇总06.xlsx）
├─ 概览：总行数 / 成功 / 失败 / 未识别 sheet 数
├─ 分 sheet 明细：类型识别结果 / 表头行号 / 国家/币种 / 成功失败行数
├─ 异常池明细：row_no + 原因 + 原值快照 + [修复重提][忽略]
├─ 成本缺失区块（仅销售报表触发）：
│    店铺 / ASIN / SKU / 国家 / 销量 / 销售额 / 缺失项(成本|运费|两者) / 状态(③|④)
│    → [一键跳转成本待补录清单]
├─ 沿用历史提示（状态②）：SKU → "成本沿用自 2026-04" [查看依据]
└─ 对账结果：行数平衡 ✓ / 唯一约束冲突数 0 ✓
```

成本完整性检查在**每次销售报表导入完成后自动执行**（触发逻辑 = MM-05 5.10 四态判定），结果写 `cost_missing_alert`（③④ → 待补录）。

### 3.13 阶段 10：批次锁结

- 锁结 = `import_batch.is_locked = true`：该批次关联的 ODS/DWD 数据禁止修改删除，覆盖策略拒绝执行；
- 解锁：敏感操作 → **二次确认弹窗（需输入批次号确认）** + `operation_log` 留痕（操作人/时间/IP/原因）；
- 解锁 → 补导 → 重算 → 重新锁结，核算侧自动生成新版本快照（v2），v1 保留可对比（MM-05 5.7/5.8）。

### 3.14 LMG 导入代码问题 → ERP 规范点对照

| LMG 问题（实证位置） | ERP 规范点 |
|---|---|
| 8 个导入端点各自解析，口径漂移 | 单一引擎 + 报表规格表（3.2） |
| raw_storage_fee 无唯一约束，靠先删后插 | 业务唯一约束 + ON CONFLICT 显式语义（3.10 规范一） |
| workbook 清空：raw 按店铺、summary 按店铺+月 | resolve_scope 唯一推导 + 同事务（3.10 规范二） |
| 长期/普通仓储费混写字段 | fee_kind 区分 + 独立汇总字段（3.2 / D2） |
| 月份归属用导入上下文而非账期字段 | period_field 显式声明（3.2），只信报表账期字段（D4） |
| 表头/列名/type 映射硬编码在函数内 | 两级词典入库可配置（3.6/3.7） |
| 一次性整文件解析无进度 | 阶段化任务 + 进度落库（3.11） |
| 无行数对账，丢行/重行不可知 | assert_row_balance 强制断言（3.10 规范三） |

---

## 4. 利润算法规范（最重要）

> **本节为全文档最高优先级章节，口径逐字对齐 MM-05 5.4（实测 2026-06 UK销售 29 列）**。实现必须**单一入口**（一个模块、一个函数族），禁止像 LMG 那样在导入累计、重算 SQL、看板查询三条路径各算一遍。

### 4.1 算法总览

```
第 1 步  行级明细聚合：从 fact_sales_transaction 取数
         金额列聚合范围 = type ∈ {Order, Refund}
         Adjustment 单独分桶
第 2 步  net_amazon（原币）= Σ(Order/Refund 各金额列) + Adjustment 分桶结果
第 3 步  费用聚合：广告/仓储/超龄/退货处理/入库配置/移除（原币，来自独立费用 fact 表）
第 4 步  汇率：月度汇率表取数（两级查找，缺失即报错，见 4.7）
第 5 步  净利润(RMB) = net_amazon×汇率 − 采购成本(RMB) − 头程运费(RMB)
                      − (广告+仓储+退货处理+入库配置+移除)×汇率
第 6 步  成本完整性标注：4 态判定 → 双口径输出（见 4.8）
```

**计算粒度**：`(店铺, 国家, 产品[ASIN/SKU], 月份/区间)` —— 与 DWS 快照粒度一致；门店/国家/月度汇总由产品粒度结果再聚合，**不允许从原币金额与混合汇率二次推导**。

### 4.2 字段映射（标准字段 → 交易报表实际列，29 列口径）

| 标准字段 | 报表实际列 | 计入 net_amazon | 说明 |
|---|---|---|---|
| `product_sales` | `product sales` | ✅ | 商品销售额 |
| `product_sales_tax` | `product sales tax` | ✅ | 销售税 |
| `postage_credits` | `postage credits` | ✅ | 运费收入 |
| `shipping_credits_tax` | `shipping credits tax` | ✅ | 运费税 |
| `gift_wrap_credits` | `gift wrap credits` | ✅ | 礼品包装费 |
| `giftwrap_credits_tax` | `giftwrap credits tax` | ✅ | 礼品包装税 |
| `promo_rebate` | `promotional rebates` | ✅ | 促销返点 |
| `promo_rebate_tax` | `promotional rebates tax` | ✅ | 促销返点税 |
| `marketplace_withheld_tax` | `marketplace withheld tax` | ✅ | 平台代扣税 |
| `selling_fee` | `selling fees` | ✅ | 销售佣金（**负值**，断言 ≤0） |
| `fba_fee` | `fba fees` | ✅ | FBA 配送费（**负值**，断言 ≤0） |
| `other_amount` | `other` | Adjustment **有 order_id** 时用 | 实测 Adjustment 的 other == total |
| `line_total` | `total` | Adjustment **无 order_id** 时按正负分桶 | 行合计 |
| `other_transaction_fees` | `other transaction fees` | ❌ | 忽略，不计入 |
| `transaction_type` | `type` | — | 分桶/过滤关键 |
| `quantity` | `quantity` | — | 分桶时调整数量 |
| `order_id` | `order id` | — | 判空 → 分桶依据 |
| `biz_date` | `date/time` | — | 账期过滤唯一依据（**不信文件名月份**） |

### 4.3 net_amazon 公式

```
net_amazon(原币) = Σ (type ∈ {Order, Refund} 的行) [
      product_sales + product_sales_tax
    + postage_credits + shipping_credits_tax
    + gift_wrap_credits + giftwrap_credits_tax
    + promo_rebate + promo_rebate_tax
    + marketplace_withheld_tax
    + selling_fee            # 负值，直接相加
    + fba_fee                # 负值，直接相加
  ]
  + Adjustment 分桶结果（4.4）
```

### 4.4 Adjustment 分桶规则（逐字保留）

```
type = Adjustment:
  若 order_id 非空:
      adj_with_order    += other_amount          # 用 other 列！
      qty_with_order    += quantity
  否则 (无 order_id):
      total > 0 : adj_no_order_pos += line_total ; qty_no_order += |quantity|
      total < 0 : adj_no_order_neg += line_total ; qty_no_order -= |quantity|
      total = 0 : 忽略（记录计数）

adjustment_total = adj_with_order + adj_no_order_pos + adj_no_order_neg
```

数量口径（对齐 LMG 重算 SQL 实测逻辑）：

```
order_qty = Σ|quantity| (type=Order)
          + Σ|quantity| (type=Adjustment, 无order_id, total>0)
          − Σ|quantity| (type=Adjustment, 无order_id, total<0)
```

### 4.5 不参与利润核算的 type（实测 UK 2026-06）

| type | 行数 | total 合计 | 处理 | 理由 |
|---|---|---|---|---|
| Order | 3102 | +52,934.45 | ✅ 计入 | 销售收入 |
| Refund | 152 | −3,112.83 | ✅ 计入 | 退款 |
| Adjustment（有单/无单） | 17 / 18 | +249.95 / +79.06 | ✅ 分桶计入 | 库存调整 |
| Service Fee | 17 | −7,779.87 | ❌ 不计入 | ≈当月广告费（UK 广告报表 7,409.35，差额 −370.52 为月租等）→ 再计即重复 |
| Amazon Fees | 4/2 | −93.81 / −825.18 | ❌ 不计入 | 与订单行 selling fees 重复 |
| FBA Inventory Fee | 11/12 | −70.48 / −18.32 | ❌ 不计入 | 与独立仓储费报表重复（用户已确认"不重复"） |
| FBA transaction fees | 4 | −465.12 | ❌ 不计入 | 与订单行 fba fees 重复 |
| Transfer | 4 | −32,523.42 | ❌ 不计入利润；**单独资金流统计** | 亚马逊提款（资金流出） |

**风险登记（MM-05 Q15）**：Amazon Fees / FBA transaction fees 若在订单行无对应值则漏算（UK 站约 −1,384/月）。ERP 处理：这些 type 的金额**全部进"资金流对账"报表**（`fact_cashflow_transfer` 及同类）单独展示，供核查，但不参与利润。

### 4.6 净利润与净利率

```
净利润(RMB) = net_amazon × 汇率
            − 采购成本(RMB) − 头程运费(RMB)                    # 已是 RMB，不乘汇率！
            − (广告费 + 仓储费 + 退货处理费 + 入库配置费 + 移除费) × 汇率

净利率 = 净利润 ÷ product_sales_rmb
其中 product_sales_rmb = product_sales × 汇率
```

扣减项取数来源与粒度：

| 扣减项 | 来源 | 版本规则 |
|---|---|---|
| 采购成本 `cost_rmb` | 产品表 `成本RMB` → `product_cost_history(period=月份)` | 该月无记录 → 沿用最近版本并标注"沿用自 YYYY-MM"（状态②） |
| 头程运费 `freight_rmb` | 产品表运费列（通用型 `产品运费/台` 或按国家拆分型）→ `freight_json` | 同上；**已锁结区间的运费覆盖禁止修改** |
| 广告费 | `fact_ad_daily.spend` 聚合（MM-04） | 按区间聚合 |
| 仓储费 / 超龄仓储费 | `fact_fee_storage`（fee_kind=normal / long_term，**两个独立字段**，D2） | `month_of_charge` 整月归属 |
| 退货处理费 | `fact_fee_returns` | `month_of_charge` 整月归属 |
| 入库配置费 / 移除费 | `fact_fee_inbound` / `fact_fee_removal` | 账期字段/业务日期归属 |

> **费用归属规则**：有 `month_of_charge` 类账期字段的费用 → **整月归属**，与完整自然月结算天然对齐；区间与月份有交集即计入（月中归属，项目既定约束）。销售/广告按 `biz_date` 过滤。

### 4.7 汇率规则（D5 对策，禁静默降级）

```
查找顺序（仅两级）：
  ① exchange_rate WHERE country=站点 AND period=核算月 AND store_id=店铺   （店铺专属月度）
  ② exchange_rate WHERE country=站点 AND period=核算月 AND store_id IS NULL （全局月度）
  ①② 均缺失 → 抛 ExchangeRateMissingError
```

- **禁止** LMG 的第三级降级（任意最新月 → 硬编码 DEFAULT_RATES）；
- `ExchangeRateMissingError` 的处理：核算任务失败并在 UI 明确提示"站点 X 缺 YYYY-MM 汇率，请先在 [汇率管理] 导入"；不产生任何部分结果；
- 可选逃生口：用户显式勾选"使用手工指定汇率继续"（输入值 + 留痕），结果打 `exchange_rate_source=manual` 标记，报表展示"⚠ 手工汇率"角标；
- 汇率数据来源：汇率 sheet 导入 + 管理页面维护；种子数据仅作为初始值入库，**不出现在代码常量**。

### 4.8 成本完整性 4 态与双口径（联动 MM-05 5.10）

```
核算时对每个 (店铺, ASIN, SKU, 国家)：
  ① 该月有成本记录        → 正常使用
  ② 该月无、历史有         → 沿用最近版本，标注"沿用自 YYYY-MM"
  ③ 历史从无成本           → 成本缺失（cost_missing_alert: 待补录）
  ④ 有成本缺运费（或反之）  → 运费缺失（cost_missing_alert: 待补录）
```

输出口径（**必须同时输出，禁止静默按 0**）：

```
利润(完整)   = 仅由 ①② SKU 构成的净利润汇总
利润(含缺失) = 全部 SKU 的净利润汇总（缺失 SKU 的成本/运费按 0 参与计算）
每个缺失 SKU 的利润行 → 标记"成本不完整"
管理层总览/财务核算台 → 红色角标"存在 N 个 SKU 成本缺失，利润可能不准确"→ 直达待补录清单
```

**注意（项目既定约束）**：成本补入仅在**销售数据中出现该产品时**触发——销售表未出现的产品不参与利润核算、不触发预警或补入。检查集合 = 本区间销售事实中的 (店铺, ASIN, SKU, 国家)。

### 4.9 符号校验与对账断言（D6 对策）

```python
def assert_sign_convention(rows):
    """DWD 入库与聚合前各执行一次"""
    for r in rows:
        if r.type in ("Order", "Refund"):
            assert r.selling_fee <= 0, f"selling fees 应为负值: {r.raw_row_id}"
            assert r.fba_fee <= 0,     f"fba fees 应为负值: {r.raw_row_id}"
    # 若上游报表格式变更导致符号翻转 → 行进异常池 + 告警"符号约定破坏"，绝不静默相加
```

快照落库前的**复算对账**：

```
snapshot.net_amazon  == Σ(fact 行级聚合结果)          # 一致性
abs(snapshot.净利润) ≤ Σ|行级金额| × 汇率 × 1.1        # 量级合理性（防单位/汇率错位）
利润(完整) ≤ 利润(含缺失) 或 缺失项均为支出方向          # 方向合理性
```

### 4.10 参考实现（单一入口，伪代码级）

```python
# services/profit_engine.py —— ERP 唯一利润公式入口（对应 LMG profit.py，修复 D5/D6/双口径缺失）
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass, field

INCLUDED_TYPES = {"Order", "Refund"}
AMOUNT_FIELDS = [
    "product_sales", "product_sales_tax", "postage_credits", "shipping_credits_tax",
    "gift_wrap_credits", "giftwrap_credits_tax", "promo_rebate", "promo_rebate_tax",
    "marketplace_withheld_tax", "selling_fee", "fba_fee",
]

@dataclass
class Bucket:
    net_amazon: Decimal = Decimal("0")
    adj_with_order: Decimal = Decimal("0")
    adj_pos: Decimal = Decimal("0")
    adj_neg: Decimal = Decimal("0")
    order_qty: int = 0
    missing_flags: set = field(default_factory=set)   # cost_missing / freight_missing

def compute_snapshot(store_id, country, period, session) -> ProfitSnapshot:
    q2 = lambda d: d.quantize(Decimal("0.01"), ROUND_HALF_UP)

    # ---- 第 1 步：行级聚合（一次遍历，Order/Refund + Adjustment 分桶）----
    b = Bucket()
    for r in session.query(FactSalesTransaction).filter_by(
            store_id=store_id, country=country).in_period(period):
        if r.type in INCLUDED_TYPES:
            for f in AMOUNT_FIELDS:
                b.net_amazon += getattr(r, f) or 0
        elif r.type == "Adjustment":
            if r.order_id:
                b.adj_with_order += r.other_amount or 0          # 有单 → other 列
            elif (r.line_total or 0) > 0:
                b.adj_pos += r.line_total; b.order_qty += abs(r.quantity or 0)
            elif (r.line_total or 0) < 0:
                b.adj_neg += r.line_total; b.order_qty -= abs(r.quantity or 0)
        # 其余 type（Service Fee / Amazon Fees / FBA Inventory Fee / FBA Transaction Fees /
        #           Transfer）→ 不计入；Transfer 另写资金流表（4.5）
    net_amazon = b.net_amazon + b.adj_with_order + b.adj_pos + b.adj_neg

    # ---- 第 2 步：汇率（两级查找，缺失即报错，D5）----
    rate = get_exchange_rate_strict(session, country, period)   # 4.7 规则；无第三级降级

    # ---- 第 3 步：费用聚合（原币）----
    fees = aggregate_fees(session, store_id, country, period)   # 返回各项原币金额
    # fees.storage_fee / fees.long_term_storage_fee 独立字段（D2）

    # ---- 第 4 步：成本/运费（RMB，按月版本化 + 4 态）----
    cost_detail = resolve_cost(session, store_id, period, sold_skus)  # 4.8 规则
    #   返回 per-SKU: cost_rmb, freight_rmb, state(①②③④), carried_from(②时)
    #   ③④ → 写/更新 cost_missing_alert + snapshot 标记

    # ---- 第 5 步：净利润（双口径）----
    def net_for(skus, complete_only: bool):
        cost_rmb  = sum(s.cost_rmb   for s in skus if not (complete_only and s.state in (3, 4)))
        freight   = sum(s.freight_rmb for s in skus if not (complete_only and s.state in (3, 4)))
        return q2(net_amazon * rate - cost_rmb - freight
                  - (fees.ad + fees.storage + fees.long_term_storage + fees.returns
                     + fees.inbound + fees.removal) * rate)

    return ProfitSnapshot(
        net_amazon=q2(net_amazon),
        exchange_rate=rate,
        net_profit_complete=net_for(cost_detail, complete_only=True),    # 利润(完整)
        net_profit_with_missing=net_for(cost_detail, complete_only=False), # 利润(含缺失)
        missing_sku_count=..., missing_flags=...,
        fee_detail=fees, cost_detail=cost_detail,     # 全部扣减项可下钻溯源（MM-05 验收标准）
    )
```

> 导入/重算/看板**全部调用此函数**，不得出现第二份公式实现。快照冻结后数值不再随新导入变化（MM-05 5.5）。

### 4.11 LMG profit.py 与 ERP 规范差异对照

| 项 | LMG 实现（profit.py 实测） | ERP 规范 |
|---|---|---|
| 汇率 | 三级降级到硬编码 DEFAULT_RATES（US 6.8 / UK 9.0 / DE 7.5…），无告警 | 两级查找，缺失报错阻断；禁代码常量汇率（4.7） |
| 符号 | selling_fee/fba_fee/adjustment 直接相加，靠报表负值约定，无校验 | 行级符号断言 + 聚合前审计（4.9） |
| Adjustment | 有 order_id 用 other、无 order_id 按 total 正负分桶（**口径正确，保留**） | 同口径固化（4.4），并补 quantity 分桶口径 |
| 仓储费 | 普通与长期共用 `storage_fee_usd`，互相覆盖 | 独立字段（4.6/D2） |
| 成本缺失 | 无 4 态检查，缺失静默按 0 | 4 态判定 + 双口径输出（4.8） |
| 实现位置 | 导入累计 + 重算 SQL 两条路径并存 | **单一入口 profit_engine**（4.10） |
| 精度 | Decimal + quantize 0.01（**保留**） | 同，统一 ROUND_HALF_UP |
| 粒度 | monthly_summary（产品×店铺×国家×月） | 同粒度快照 + 版本化 v1/v2（MM-05 5.5–5.8） |

---

## 5. 数据模型映射表（LMG MySQL → ERP PostgreSQL 分层）

> ERP 建模详见 `04-技术需求/02-数据模型.md`。本表说明 LMG 每张表的**去向**与**改造要点**。

| LMG 表（MySQL） | 职责 | ERP 去向 | 改造要点 |
|---|---|---|---|
| `dim_store` | 店铺 | 主数据层 `store` | 增加部门归属（多部门权限并集） |
| `dim_country` | 国家/站点 | `site` | 增 default_currency、region |
| `dim_exchange_rate` | 汇率（店铺+月） | `exchange_rate` | 保留店铺级+全局级两级；缺失报错（D5） |
| `dim_product` | 产品（SKU 主键） | `store_product` + `store_sku_mapping` | **店铺隔离**（store_id+asin 联合唯一）；SKU-ASIN 映射独立表 + 部分唯一索引 |
| `dim_product_cost` | 成本 | `product_cost_history` | 按月版本化 + source_type（报表导入/人工补录）+ freight_json |
| `dim_freight` | 运费 | `product_cost_history.freight_json` / `freight_rule`、`freight_override` | 运费覆盖锁结校验 |
| `dim_time` | 日期维表 | **废弃** | 用 `biz_date` 原生日期 + 按月分区替代 |
| `raw_transactions` | 交易原始行 | ODS `raw_sheet_row` + DWD `fact_sales_transaction` | 全量留痕（headers/values JSONB）+ 标准化事实表；type 口径按 4.2 |
| `raw_advertising` | 广告原始行 | ODS + `fact_ad_daily` | 按日聚合事实 + 指标列 |
| `raw_storage_fee` | 仓储费 | ODS + `fact_fee_storage(fee_kind=normal)` | **业务唯一约束**（D1）；月归属只信账期字段（D4） |
| `raw_long_term_storage` | 长期仓储费 | `fact_fee_storage(fee_kind=long_term)` | 与普通费**同表分 kind**，汇总独立字段（D2） |
| `raw_returns` | 退货处理费 | ODS + `fact_fee_returns` | 别名：退货处理费/退回处理费 |
| `raw_inbound` | 入库配置费 | ODS + `fact_fee_inbound` | — |
| `raw_removal_fee` | 移除费 | ODS + `fact_fee_removal` | — |
| `monthly_summary` | 月度汇总（产品×店铺×国家×月，含利润字段） | DWS `dws_profit_snapshot`(+version) | 拆出双口径利润字段；快照冻结+版本；锁结联动 |
| `users` | 用户 | `sys_user` + `department` + `role`/`permission`/`user_role`/`data_scope` | RBAC + 店铺级/账期级/字段级数据权限（D7） |
| `weekly_reports` / `weekly_report_images` | 周报 | Phase 2（如需保留） | 非本期核心 |
| `ads_session` / `ads_message` / `ads_data_record` / `ads_action_item` | 广告审计 Agent | Phase 2 | 对应 ads_agent 子系统 |
| —（LMG 无对应） | 异常池 | `import_error` | 新增 |
| —（LMG 无对应） | 成本预警 | `cost_missing_alert` | 新增（MM-05 5.10） |
| —（LMG 无对应） | 批次/文件/Sheet 元数据 | `import_batch` / `import_file` / `raw_sheet` | 新增（hash 查重、锁结、进度） |
| —（LMG 无对应） | 资金流 | `fact_cashflow_transfer` | 新增（Transfer 单独统计，4.5） |

---

## 6. API 契约对照（LMG 端点 → ERP 端点）

> ERP 采用 REST + 统一前缀 `/api`。所有业务端点**强制挂载数据权限依赖**（D7）。

| LMG 端点 | ERP 端点 | 变化要点 |
|---|---|---|
| `POST /auth/login` `GET /auth/me` `POST /auth/change-password` | `POST /api/auth/login` 等 | JWT 密钥环境变量；token 失效机制；首启强制改密（D8） |
| `GET/POST/PUT/DELETE /auth/users*` | `/api/admin/users`、`/api/admin/roles`、`/api/admin/departments` | 完整 RBAC；部门管理员管理本部门用户；超管定角色 |
| `POST /import/transaction` `POST /import/advertising` `POST /import/storage` `POST /import/returns` `POST /import/inbound` | `POST /api/imports/files`（统一入口） | **单一引擎**，报表类型自动识别；店铺权限校验 |
| `POST /import/product-info` | `POST /api/imports/files`（类型=产品表） | 写主数据 + product_cost_history，触发 4 态检查 |
| `POST /import/workbook` `POST /import/folder` | `POST /api/imports/files`（多文件）/ `POST /api/imports/folder`（服务端目录扫描，Phase 2 预留） | 上传即返回 + 进度查询（3.11） |
| `GET /import/supported` | `GET /api/imports/templates` | 返回报表规格表 + 别名词典（可配置） |
| `POST /import/recalculate` `GET /import/recalculate/status/{task_id}` | `POST /api/profit/snapshots` `GET /api/tasks/{task_id}` | 快照版本化 v1/v2；解锁重算留痕 |
| `GET /query/country-summary` `GET /query/monthly-summary` `GET /query/product-sales` `GET /query/sales` `POST /query/sales/batch` | `/api/query/*` 同语义 | **补店铺级数据权限过滤**（LMG 缺失）；成本/利润字段按字段级 ACL（D7） |
| `GET /query/product-cost` | `/api/master/product-costs` | 4 态状态标注 + 版本来源月份 |
| `GET /dashboard/summary` `/trend` `/cost-breakdown` `/product-distribution` `/store-comparison` `/country-comparison` `/top-returns` `/transfer-summary` | `/api/dashboard/*` 同语义 | 补权限过滤；成本类图表走字段 ACL；transfer-summary 对应资金流对账 |
| `GET /export/monthly-summary` `/country-summary` `/transfer` `/missing-skus` | `/api/export/*` | 字段级 ACL（LMG 仅此处实现）+ **导出利润报表二次确认留痕**（MM-05 5.9） |
| `GET /advertising/summary` `/detail` | `/api/ads/summary` `/detail` | 补店铺权限（LMG 缺失） |
| `* /weekly-report/*` | Phase 2 | — |
| `* /ads-agent/*` `POST /ai/chat` | Phase 2 | ads_agent / AI 模块暂不迁移 |
| — | `GET /api/imports/batches/{id}/progress` | 新增：进度轮询（3.11） |
| — | `POST /api/imports/batches/{id}/lock` `POST .../unlock` | 新增：锁结/解锁（二次确认） |
| — | `GET/POST /api/alerts/cost-missing` | 新增：成本待补录队列（一键补录） |
| — | `GET/POST /api/config/exchange-rates` `sheet-alias-map` | 新增：配置管理页面支撑 |

---

## 7. 重构里程碑

| 里程碑 | 内容 | 关键验收（对应本文档） |
|---|---|---|
| **M1 数据层与导入引擎** | PG 建模（ODS/DWD/主数据/配置层 + 分区）+ 统一导入引擎 + 后台任务/进度 + 异常池 | 3 章全部；D1/D3 规范三条落地；真实报表回放：行数对账通过、重导金额不翻倍 |
| **M2 主数据与权限** | store/site/product/cost_history + RBAC + 三级数据权限 + 审计日志 | 所有业务路由挂载 scope 依赖（grep 检查为零遗漏）；D7/D8 修复项逐条核对 |
| **M3 核算引擎** | profit_engine 单一入口 + 快照/锁结/解锁重算/版本 + 成本 4 态 + 双口径 | 4 章全部；UK 2026-06 实测数据复算：net_amazon 与 MM-05 5.4 手工值一致（Order 52,934.45 / Refund −3,112.83 / Adjustment +329.01）；汇率缺失必须报错（D5） |
| **M4 查询/看板/导出** | query / dashboard / export + 权限联动 + 资金流对账报表 | 6 章契约；字段级 ACL 生效（运营员工看不到成本利润） |
| **M5 前端** | 按第 8 章选定路线实施 | 第 8 章 Bug 清单逐条修复验证（路由守卫 / fail-closed / 401 体验 / ECharts dispose） |
| **M6 部署与验收** | Docker Compose 编排 + 种子数据 + 回归脚本 | D8 安全基线（密钥注入/CORS/默认密码）；D9/D10 工程卫生 |

里程碑 M1–M3 为 P0 主干；M4 依赖 M2 权限层；M5 前端可在 M1 完成后并行启动（先对接导入进度与批次接口）。

---

## 8. 前端评估：Bug 清单与剥离可行性

### 8.1 现状

- 技术栈：React 18 + Ant Design 5 + ECharts + Vite 5，**纯 JavaScript 无 TS**，`node_modules` 已随包内置（离线可跑）；
- 调用方式：全部 `/api/*` 相对路径 + vite 代理到 8000，**无硬编码后端地址**——技术上自包含；
- 规模：`Dashboard.jsx` 约 1153 行巨石组件，无代码分割（无 React.lazy）。

### 8.2 Bug 清单（实证）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| F1 | **路由无模块权限守卫**：只查"是否登录"，不校验模块权限；菜单隐藏 ≠ 防护，直敲 URL 可进入任意页面 | `App.jsx` | 越权访问（配合后端 D7 缺陷为真实风险） |
| F2 | **字段权限 fail-open**：`hasField` 缺省 `?? true`，与后端 DEFAULT_PERMISSIONS（cost/freight 默认 false）矛盾 | `AuthContext.jsx` L46 | 无权限用户可能看到成本/利润字段 |
| F3 | **401 拦截器全页刷新**：`window.location.href='/login'` 导致登录失败错误信息被刷掉（Login.jsx 有 `.login-error` 容器但来不及显示） | `api/index.js` L18–27 | 登录体验缺陷，错误提示不可见 |
| F4 | **ECharts 实例不 dispose**：7 处 `echarts.init` 无一处销毁 | `Dashboard.jsx`（grep 证实） | 内存泄漏、组件重挂载时 canvas 告警 |
| F5 | SSE 流 `JSON.parse` 失败静默 continue | `AdsAgent.jsx` L287 | 异常数据无感知 |
| F6 | token 存 localStorage（XSS 可窃取）；Login 引 Google Fonts CDN（**局域网离线环境失效**，加载阻塞） | `AuthContext.jsx` / `Login.jsx` | 安全 + 离线可用性 |
| F7 | 无代码分割；巨型单文件组件难维护 | `Dashboard.jsx` 1153 行 | 工程质量 |

### 8.3 剥离可行性结论

**可以剥离**：LMG 前端自包含（相对路径 + 代理），可整体拷出独立运行，作为"可参照的已实现 UI 资产"。

**不能直接搬进 ERP**：ERP 既定选型为 **Vue 3 + TypeScript + Element Plus**（项目硬约束），React 代码无法移植；且 F1/F2 两个权限类 Bug 恰好对应 LMG 后端权限缺陷（D7），整套搬运会把"权限只是装饰"的问题一并继承。

### 8.4 两条路线与建议

| | 路线 A：保留 React 前端 | 路线 B（**推荐**）：Vue 3 重建，React 版作交互参照 |
|---|---|---|
| 做法 | 修复 F1–F6 后作为 ERP 前端；后端按 LMG API 契约开发 | React 版仅当**原型/交互参照**；按 ERP 既定选型 Vue3+TS+Element Plus 重建 |
| 优点 | 起步最快，UI 已被业务方见过、接受过 | 技术栈符合项目约束与既有 docs；TS 类型可把第 4 章算法字段、6 章契约固化；权限模型前后端一致 fail-closed |
| 代价 | 放弃既定技术选型；纯 JS 无类型，算法字段（29 列映射）易写错；F1–F6 需逐一修 | 前端需重写（可参照 LMG 页面结构：登录/导入/查询/看板/导出/配置） |
| 权限 | 需自补路由守卫 + 字段 ACL（F1/F2） | 与后端 RBAC 同期设计，天然一致 |

**建议**：采用**路线 B**。理由：
1. ERP 的安全与权限设计（D7/D8）是本期重心，前端权限模型需与后端**同期同语义**实现，React 版 fail-open 逻辑（F2）反而是反面教材；
2. 多语言列名映射、利润字段、双口径等强类型契约用 TS 固定收益明显；
3. LMG 前端的页面布局与交互流程（导入向导、看板卡片、导出二次确认弹窗）可作为现成参照，**重建成本低、风险可控**；
4. 局域网部署要求去除一切 CDN 依赖（F6），重建时天然解决。

无论哪条路线，第 8.2 节 7 项缺陷都必须在 ERP 前端验收时逐条核对为"已规避"。

---

## 附录 A：本文档引用的 LMG 实证位置速查

| 位置 | 内容 |
|---|---|
| `lmg-platform/backend/services/profit.py` | DEFAULT_RATES 硬编码（L9–13）、三级汇率降级（L16–36）、net_profit 公式无符号校验（L71–83） |
| `lmg-platform/backend/routers/import_data.py` | 多语言 type 字典（L802–809）、Adjustment 分桶（L891–907）、重算 SQL 口径（L4011–4020：金额 IN ('Order','Refund')）、storage 补丁（L1565/L1712） |
| `lmg-platform/backend/routers/` | weekly_report 之外的路由均无店铺级过滤；export.py L339 唯一字段级过滤 |
| `lmg-platform/frontend/src/` | App.jsx（路由无守卫）、AuthContext.jsx L46（fail-open）、api/index.js L18–27（401 刷新）、Dashboard.jsx（echarts 7 处 init） |
| MySQL 导出 | raw_storage_fee 行数 5–6 倍膨胀；summary.storage_fee_usd 混写长期仓储费；EU 仓储费 month_of_charge 与 summary 月份错位 |
| 运行日志 | InsecureKeyLengthWarning；10048 端口占用；.apikey_err；fail_shots 15 张 |

## 附录 B：待确认问题（继承 + 新增）

1. MM-05 遗留：成本版本化回退规则的书面确认；"其他成本"范围；跨期退款归属；缺失忽略策略。
2. **新增 Q-A**：费用类唯一约束冲突时的默认行为（覆盖 or 报错人工介入）——建议默认"覆盖 + 报告中列出冲突清单"。
3. **新增 Q-B**：并存策略下查询口径——建议默认"最新批次生效"，历史批次仅可追溯查询。
4. **新增 Q-C**：前端路线 A/B 的最终拍板（本文档建议 B）。
