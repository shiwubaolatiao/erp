# LMG ERP v2（重构版）

> 亚马逊多店铺经营核算系统 · 系统性重构版
> 重构基准：`E:\项目\LMG平台完整包_20260915-134711`（旧系统，备份于 `E:\项目\backups\`）
> 本项目路径：`E:\项目\erp重构`

---

## 一、重构目标与已确认的边界

| 项目 | 决策 |
|---|---|
| 重构方式 | **整体重写**后端（不是补丁式修改） |
| 利润算法 | **完整保留**（`app/services/profit/engine.py` 由旧系统原样移植，公式一字未改） |
| 前端 | **保留 React 技术栈与页面**，叠加新功能页面 |
| 数据库 | **继续 MySQL**（新库 `lmg_erp`，与旧库 `lmg_platform` 完全隔离） |
| 数据来源 | **从原始报表重建**（`C:\Users\五包辣条\Desktop\汇总\01~06`），不信任旧库数据 |
| 旧库处置 | 仅存档备查，不再作为数据源 |
| 验收标准 | **溯源复算基准**：每个汇总数字可下钻到原始报表行、可独立复算（见第五节） |
| 接口前缀 | `/api/v2/*`（旧 `/api/*` 由并行运行的旧后端继续提供） |
| 运维方式 | 沿用一键启停 + 守护进程（端口：新系统 8020/5180，旧系统 8000/5173） |
| AI 功能 | AI 对话、广告 Agent、周报（含审核流）全部保留 |

---

## 二、目录结构

```
E:\项目\erp重构\
├── backend\
│   ├── app\
│   │   ├── core\          配置 / 数据库 / 安全认证（JWT + 权限）
│   │   ├── models\        ODS原始层 / DWD清洗层 / 主数据 / 配置 / 权限 / DWS汇总
│   │   ├── api\v2\        12 个路由模块（78 个接口）
│   │   ├── services\
│   │   │   ├── profit\    ★ 利润引擎（旧系统公式原样移植）
│   │   │   ├── importer\  报表解析引擎（多语言表头/动态表头定位/日期/数值）
│   │   │   ├── mapping\   ASIN-SKU 三元组映射（5 类场景）
│   │   │   ├── accounting\核算 / 快照 / 成本完整性检查
│   │   │   ├── ads\       广告 Agent（报表库读取 + Agent 工具）
│   │   │   ├── ai\        AI 对话与数据查询工具
│   │   │   └── migration\ 旧库主数据迁移脚本
│   │   ├── workers\       导入后台任务（DB 队列 + 线程，刷新不中断）
│   │   └── main.py        FastAPI 入口（启动即建库建表 + 种子数据）
│   ├── tools\             验证与运维工具（见第四节）
│   ├── tests\             利润公式黄金测试
│   └── requirements.txt
├── frontend\              React + Vite + AntD（原页面 + 4 个新页面）
├── scripts\watchdog.ps1   守护进程（隐藏运行、健康探针、自动重启）
├── start.bat / stop.bat / status.bat
└── logs\                  运行日志
```

---

## 三、启动与运维

### 前置条件
- Python 3.10+，依赖：`cd backend && python -m pip install -r requirements.txt`
- Node.js 18+，前端依赖：`cd frontend && npm install`（包内已含 node_modules）
- MySQL 可达（连接参数在 `backend\.env`）

### 一键启动 / 停止 / 体检
```
双击 start.bat    依赖预检 → 数据库连通检查 → 拉起守护进程 → 等待就绪 → 打开浏览器
双击 stop.bat     按端口精确停止（不会误杀其它 python/node），保留 MySQL
双击 status.bat   只读体检：守护进程 / 端口 / 健康探针 / 数据库 / 最近日志
```

- 页面：http://localhost:5180 ｜ 接口文档：http://localhost:8020/docs ｜ 账号 `admin` / `123456`
- 守护进程每 15 秒健康探针（身份校验：`/health` 必须含 `LMG-ERP`），连续 3 次失败先杀占用进程再重启

> **务必保持 `.bat` 为 CRLF 换行 + 纯 ASCII**（旧项目实测：LF + 中文会让 cmd 解析错行）。

---

## 四、工具脚本（backend\tools\）

| 脚本 | 用途 |
|---|---|
| `verify_import.py <文件> <店铺> <月份>` | 单文件端到端验证：导入 → 落库统计 → 表头识别 → 异常行 → 溯源链路 → 核算 |
| `reconcile.py <店铺> <月份>` | ★ **独立复算对账**：绕开管线，直接从 ODS 原始行独立求和，与 fact 表 / 快照三方交叉比对 |
| `bulk_import.py [--months 01,02]` | 批量导入汇总目录全部报表 |
| `reset_data.py [--all]` | 清空业务数据（保留账号/店铺/站点），可重复运行 |

---

## 五、验收：溯源复算（已通过）

**验收方式**：不信任旧库数据，改为验证新系统自身的"数字可信性"——
每个汇总数字都能 (1) 下钻到原始报表的具体 sheet/行号/单元格原值，(2) 被独立复算出来。

实测结果（LMG / 2026-01，10 个站点）：

```
=== 独立复算对账  LMG / 2026-01 ===
ODS 参与复算行数: 4232   fact 行数: 4232

国家     行数(ods/fact)      product_sales      net_amazon      差异
AE        10/10                  1431.24         1140.35       0.00 OK
BE        67/67                  1952.63         1457.39       0.00 OK
DE       626/626                15570.16        12698.79       0.00 OK
ES       197/197                 5355.28         4318.10       0.00 OK
FR       540/540                13961.20        10207.61       0.00 OK
IE        54/54                  2226.61         1778.71       0.00 OK
IT       175/175                 4616.59         3285.09       0.00 OK
NL        30/30                   855.66          681.23       0.00 OK
SA        13/13                  2130.38         1732.96       0.00 OK
UK      2520/2520               53945.28        46318.87       0.00 OK

对账结论: ✓ 全部一致（ODS 独立复算 == fact 表聚合）

与核算快照对比:
  UK  快照净利润=185194.30 净利率=0.3641 net_amazon=46318.87 汇率=9.43   ✓ 差异 0.00
  DE  快照净利润=39239.57  净利率=0.3062 net_amazon=12698.79 汇率=8.23   ✓ 差异 0.00
```

**溯源链路实测**：
```
fact#6158 (type=Refund, asin=B089GY78DV, sku=J6-NF8L-8JZX, 销售额=-41.75)
  → raw_row_id=20277
  → sheet=UK销售 第 11 行（27 列）
  → 单元格原值: type='Refund', order id='202-1337136-6053937',
                sku='J6-NF8L-8JZX', product sales=-41.75, total=-38.87
```

---

## 六、利润口径（与旧系统算法完全一致）

```
net_amazon(原币) = Σ (type ∈ {Order, Refund}) [
      product sales + product sales tax + postage credits + shipping credits tax
    + gift wrap credits + giftwrap credits tax + promotional rebates + promotional rebates tax
    + marketplace withheld tax + selling fees + fba fees ]
  + Adjustment 分桶结果

Adjustment 分桶: order_id 非空 → other 列；否则 → total 按正负分桶

净利润(RMB) = net_amazon × 汇率
            − 采购成本(RMB) − 头程运费(RMB)
            − (广告费 + 仓储费 + 超龄仓储费 + 退货处理费 + 入库配置费 + 移除费) × 汇率

净利率 = 净利润 ÷ (product sales × 汇率)
```

**不计入利润的 type**：`Service Fee` / `Amazon Fees` / `FBA Inventory Fee` /
`Fulfilment by Amazon (FBA) transaction fees` / `Transfer`（Transfer 单独进资金流表
`fact_cashflow_transfer`，不计入利润）。

**汇率回退**（三级，与旧系统一致）：店铺专属(精确月) → 店铺专属(最近月) → 默认汇率表。

---

## 七、新系统相对旧系统的关键改进

### 7.1 数据可溯源（旧系统缺失）
- 新增 ODS 原始层：`raw_sheet_row` 保存**每 sheet 每行的原始表头名 + 原始值**（JSON），导入后不可变
- 每个 fact 记录带 `raw_row_id` 指回原始行，形成 `快照 → fact → 原始行 → 批次 → 文件` 完整链路
- 原始 xlsx 归档落盘（`backend\archive\`），批次记录文件 SHA256、导入人、导入时间

### 7.2 商品建模修正（旧系统 bug 温床）
- 旧系统：`dim_product` 唯一键反复变更，ASIN 从 SKU 前两段截取（`J6-NF8L-8JZX` → `J6`），语义错误
- 新系统：**`(店铺, SKU, ASIN)` 三元组**建模；销售表无 ASIN 列时经映射表反查真实 ASIN
- 5 类边界场景：新ASIN+新SKU → 待确认区；SKU命中+ASIN新 / 单SKU多ASIN → 异常池（旧映射保留不动）
- `amzn.gr.*` 替换件 SKU 归并到父 SKU（实测 LMG 单批次归并 61 条），原始值保留在 ODS

### 7.3 多语言报表解析（旧系统核心缺陷）
旧系统 4408 行导入文件内嵌了多语言表头映射表，但**代码中硬编码、无版本管理**。
新系统继承并扩展为可配置词典（`header_alias` 表 + 内置词典），实测覆盖：

| 站点 | 表头语言 | 实测关键字段 |
|---|---|---|
| UK/IE/AE/SA | 英文 | `sales tax collected`、`Marketplace facilitator tax` |
| DE | 德语 | `Datum/Uhrzeit`、`Umsätze`、`Verkaufsgebühren` |
| FR/FA | 法语 | `date/heure`、`ventes de produits`、`frais de vente` |
| ES | 西班牙语 | `fecha y hora`、`ventas de productos` |
| IT | 意大利语 | `Data/Ora:`、`Vendite` |
| NL | 荷兰语 | `datum/tijd`、`geïnde omzetbelasting` |
| SE | 瑞典语 | `datum/tid`、`Inkasserad moms` |
| BE | 法语 | `Taxe Marketplace Facilitator` |

同时归一**交易类型值**（`Anpassung`→`Adjustment`、`Bestellung`→`Order`、`Erstattung`→`Refund`、`Servicegebühr`→`Service Fee`）
——这一步不做，德语站点的利润会**整体算不出来**。

### 7.4 其他修复
- **日期解析**：实测覆盖 `01.01.2026 14:03:54 UTC`（德语点分）、`3 Jan 2026 5:28:25 AM UTC`（12小时制）、`2026-01-21T17:19:59+05:30`、`31 May 2026 23:04:30` 等
- **表头定位**：动态扫描前 15 行（销售表表头在第 10 行，前半段是多语言说明文字）
- **多国家 sheet**：仓储费/超龄/退货/移除/入库按**行内 `country_code`/`国家` 列**归属国家（旧系统按 sheet 单一国家，跨国会串）
- **费用月份归属**：仓储费/退货费按 `month_of_charge` 整月归属；销售按**原始业务日期**归月（绝不按文件名月份）
- **数据库**：清除重复列与遗留备份表，模型与真实库对齐，36 张表结构由代码统一定义

---

## 八、已知限制与待确认事项

| # | 事项 | 说明 |
|---|---|---|
| 1 | **合并工作簿的广告费无法按月拆分** | `STX/WEL` 的 `01` 目录是"01-03 合并工作簿"，销售数据带日期可正确归月，但**广告表无日期列**，只能整体归入归属月份。**销售/利润不受影响**（按业务日期过滤），但单月广告费/ACOS 会有偏差。需确认：是否要求紫鸟侧按月拆分导出？ |
| 2 | **FA 站点 = 法国** | 旧系统注释确认 `FA = FR`（France 缩写），已沿用 |
| 3 | 旧库主数据迁移未执行 | 旧库 `lmg_platform` 不在当前数据库服务器上（304MB dump 在包内）。经核查：**店铺、站点、汇率、产品成本均可从报表重建**（已实测），仅"历史周报"与"旧账号自定义密码"不可重建。是否需要导入 dump 后迁移，待确认 |
| 4 | 广告费用归月方式 | 目前按"导入批次归属月份"归集无日期广告行；若后续要精确到日，需紫鸟侧提供带日期的广告报表 |
| 5 | 认证方式 | 沿用账号密码 + JWT；未接 LDAP/企业微信（与旧系统一致） |
| 6 | 部分旧接口未 1:1 复刻 | 旧系统 `/api/query/monthly-summary`、`/api/export/monthly-summary` 等已由 `/api/v2/query/sales`、`/api/v2/export/sales` 承接（前端 API 层已映射） |

---

## 九、数据库表清单（36 张）

| 层 | 表 |
|---|---|
| ODS 原始层 | `import_batch` `import_file` `raw_sheet` `raw_sheet_row` `import_row_error` |
| DWD 清洗层 | `fact_sales_transaction` `fact_ad_daily` `fact_fee_storage` `fact_fee_aged_storage` `fact_fee_removal` `fact_fee_inbound_config` `fact_fee_return` `fact_cashflow_transfer` |
| 主数据 | `store` `site` `store_product` `store_sku_mapping` `sku_replacement` `product_cost_history` |
| 配置 | `exchange_rate` `sheet_alias_map` `header_alias` |
| 预警 | `cost_missing_alert` |
| 权限 | `sys_user` `role` `user_role` `permission_grant` `operation_log` |
| DWS 汇总 | `dws_profit_snapshot` `dws_profit_snapshot_item` |
| 业务 | `weekly_reports` `weekly_report_images` `ads_session` `ads_message` `ads_data_record` `ads_action_item` |
