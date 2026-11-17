"""导入主流程 — workbook/folder → 批次 → ODS 全量落库 → DWD 清洗（带 raw_row_id 溯源）

流程（文档 MM-01）：
  上传 → hash查重 → 归档落盘 → 创建批次 → 逐 sheet:
    识别类型/国家/币种 → 定位表头 → 全量行入 raw_sheet_row（ODS）
    → 按 fact 表清洗入库（DWD，每条带 raw_row_id）→ 失败行入异常池
  → 产品表写 product_cost_history → 销售表触发三元组映射 + 成本完整性检查
  → 更新批次统计
"""
import hashlib
import json
import os
import re
import shutil
from collections import namedtuple
from datetime import datetime
from decimal import Decimal
from typing import Optional
import openpyxl
from sqlalchemy.orm import Session
from app.core.config import settings
from app.models import (
    ImportBatch, ImportFile, RawSheet, RawSheetRow, ImportRowError,
    FactSalesTransaction, FactAdDaily,
    FactFeeStorage, FactFeeAgedStorage, FactFeeRemoval, FactFeeInboundConfig,
    FactFeeReturn, FactCashflowTransfer,
    Store, StoreProduct, ProductCostHistory, ExchangeRate,
    StoreSkuMapping,
)
from app.services.importer.parser import (
    parse_date, parse_decimal, parse_int,
    detect_country_from_sheet_name, detect_country_from_row, detect_currency_from_header,
    detect_header_row, detect_report_type, parse_store_from_filename,
    map_header_to_canonical, find_col, find_col_like,
    normalize_header, normalize_transaction_type, normalize_period_month,
)
from app.services.mapping.service import resolve_mapping, normalize_sku
from app.services.common.rates import upsert_rate_with_audit


def file_sha256(path: str) -> str:
    """计算文件 SHA256（分块读取，避免大文件一次性载入内存）

    用途：① 重复导入检测（同 hash 提示"疑似重复"）
         ② 归档文件名前缀，便于人工核对"这份数据来自哪个原始文件"
    """
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):   # 每次 1MB
            h.update(chunk)
    return h.hexdigest()


# DWD 处理器的轻量行引用（id + 原始行号）——ODS 分片 flush 后即释放 ORM 对象，
# 只保留这两个字段供 fact.raw_row_id / 异常记录 row_no 使用（内存与总行数解耦）
_RowRef = namedtuple('_RowRef', ['id', 'row_no'])

# 断点续导需清理/回退计数的 fact 表（均带 raw_row_id + batch_id）
_FACT_MODELS = (
    FactSalesTransaction, FactAdDaily, FactFeeStorage, FactFeeAgedStorage,
    FactFeeRemoval, FactFeeInboundConfig, FactFeeReturn, FactCashflowTransfer,
)


class _BulkSaver:
    """分片 bulk 落库缓冲（组7 7.7：内存峰值与 sheet 总行数解耦，spec 5.4.1 R4）

    bulk_save_objects 不经过 Session identity map（executemany 直插），
    缓冲满一片即落库并清空 —— 内存中同时驻留的 ORM 对象恒 ≤ 单片行数。
    注意：flush 需在 SAVEPOINT 内调用（_clean_sheet 已包），保证 sheet 级
    失败整体回滚语义不变。
    """

    def __init__(self, db: Session):
        self.db = db
        self.buf: list = []
        self.size = settings.IMPORT_BATCH_ROWS or 2000

    def add(self, obj) -> None:
        self.buf.append(obj)
        if len(self.buf) >= self.size:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            self.db.bulk_save_objects(self.buf)
            self.buf.clear()


class ImportTask:
    """单个报表文件的导入任务

    一个 ImportTask 对应一个文件、生成一个 import_batch；内部逐 sheet 处理：
        sheet 识别 → ODS 全量落库 → DWD 清洗（分派到 _process_* 处理器）→ 统计
    线程安全：一个任务一个 DB 会话，由 worker 串行调度（见 workers/import_worker.py）。
    """

    def __init__(self, db: Session, file_path: str, original_name: str,
                 store_code: Optional[str], user_id: Optional[int], task_meta: dict):
        """
        db:           数据库会话（调用方负责关闭）
        file_path:    已落盘的临时文件绝对路径
        original_name: 原始文件名（用于店铺识别与批次记录）
        store_code:   指定店铺；None 时从文件名解析（MGK-EU_01.xlsx → MGK）
        user_id:      导入人（写批次用于审计）
        task_meta:    worker 进度句柄 {id, progress_cb, period_month}
                      period_month 决定成本表/汇率表/广告表按哪个月归集
        """
        self.db = db
        self.file_path = file_path
        self.original_name = original_name
        self.requested_store_code = store_code
        self.user_id = user_id
        self.task = task_meta  # {id, progress_cb, status, logs} — worker 侧进度句柄

    # ---------- 店铺 ----------
    def _resolve_store(self) -> Optional[Store]:
        """确定本文件属于哪个店铺

        优先用调用方指定的店铺代码，否则从文件名解析（容忍噪声：
        "MGK-EU_01.xlsx" / "MGT-EU 06.xlsx" / "WEL-NA01-03.xlsx" 都能识别）。
        识别不出返回 None —— 此时 ODS 仍全量落库，但不产 fact（不猜店铺，避免串店）。
        """
        code = self.requested_store_code
        if not code:
            code, _ = parse_store_from_filename(self.original_name)
        if not code:
            return None
        return self.db.query(Store).filter(Store.store_code == code.upper()).first()

    # ---------- 主入口 ----------
    def run(self) -> ImportBatch:
        """执行导入，返回批次记录

        步骤：
          1. 计算文件 hash 并查重（重复**允许导入**，只在批次里记 dup_of_batch）
          2. 归档原始文件到 backend/archive/（溯源时可比对原文件）
          3. 建 import_batch + import_file
          4. 逐 sheet 处理（内部对每个 sheet 独立 SAVEPOINT，互不阻塞）
          5. 汇总批次状态：success(全成) / partial(部分失败) / failed(全失败)

        ★ 断点续导（7.7）：同 hash 存在 status='running' 的批次（进程中断遗留）
          → 恢复该批次而非新建：processed=True 的 sheet 直接跳过（已入库 raw_row
          不动），未完成的 sheet 清理残留后重做 —— 幂等，失败重跑不产生重复行。
        """
        db = self.db
        # 1. hash + 查重（重复由 API 层提示，这里允许强制导入并记日志）
        fhash = file_sha256(self.file_path)
        # ★ 断点续导：优先识别上次中断的未完成批次
        resume = db.query(ImportBatch).filter(
            ImportBatch.file_hash == fhash, ImportBatch.status == 'running').first()
        if resume:
            batch = resume
            store = db.query(Store).filter(Store.id == batch.store_id).first() \
                if batch.store_id else None
            done_sheets = {rs.sheet_name for rs in db.query(RawSheet).filter(
                RawSheet.batch_id == batch.id, RawSheet.processed.is_(True))}
            self._log(f"批次 {batch.id} 断点续导：已完成 sheet {len(done_sheets)} 个，"
                      f"未完成部分清理残留后重做")
        else:
            dup = db.query(ImportBatch).filter(
                ImportBatch.file_hash == fhash, ImportBatch.status != 'failed').first()
            # 2. 归档落盘（文件名带时间戳+hash 前缀，避免同名覆盖）
            stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{fhash[:12]}_{self.original_name}"
            stored_path = settings.ARCHIVE_DIR / stored_name
            shutil.copy2(self.file_path, stored_path)
            # 3. 批次
            store = self._resolve_store()
            # 归月参数规范化：''/空白 与 None 同义（未指定）
            pm_specified = (self.task.get('period_month') if self.task else None) or None
            batch = ImportBatch(
                file_name=self.original_name, file_hash=fhash,
                store_id=store.id if store else None,
                report_type='workbook',
                imported_by=self.user_id,
                original_path=str(stored_path),
                status='running',
                # 报表归属月份（用于广告等无日期数据的按月归集，以及溯源时确认"这份数据是哪个月的报表"）
                period_month=pm_specified,
                # B-06：归月来源标注 —— 未指定时各处理器内部回退当前月，报告需可见该事实
                period_source='user_specified' if pm_specified else 'default_current',
                error_summary=f"dup_of_batch:{dup.id}" if dup else "",
            )
            db.add(batch)
            db.flush()
            db.add(ImportFile(batch_id=batch.id, file_name=self.original_name, file_hash=fhash,
                              original_path=self.file_path, stored_path=str(stored_path),
                              imported_by=self.user_id))
            db.commit()
            self._log(f"批次 {batch.id} 创建，店铺={store.store_code if store else '未识别'}"
                      + ("（疑似重复导入，已强制）" if dup else ""))
            done_sheets = set()

        # 4. 逐 sheet 处理（read_only 模式省内存；data_only=True 取公式计算值）
        wb = openpyxl.load_workbook(self.file_path, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                if resume:
                    if ws.title in done_sheets:
                        # ★ 已 processed 的 sheet：ODS+DWD 均完成，直接跳过（断点续导核心）
                        self._log(f"sheet[{ws.title}] 已完成（processed），跳过")
                        continue
                    # 未完成：清理上次的半成品残留后全新重做（幂等）
                    self._drop_sheet_residue(batch, ws.title)
                # 注：不跳过任何 sheet —— 未识别/隐藏 sheet 也全量入 ODS，保证"不丢数据"
                self._process_sheet(batch, store, ws)
        finally:
            wb.close()

        # 5. 汇总批次状态
        ok = batch.success_rows
        bad = batch.failed_rows
        batch.status = 'success' if bad == 0 and ok > 0 else ('partial' if ok > 0 else 'failed')
        db.commit()
        self._log(f"批次 {batch.id} 完成：成功 {ok} 行 / 失败 {bad} 行 → {batch.status}")
        return batch

    def _drop_sheet_residue(self, batch: ImportBatch, sheet_name: str) -> None:
        """清理指定 sheet 的未完成残留（断点续导幂等重做：先删后插）

        残留 = 上次中断时已随 sheet 级 commit 落库的 ODS raw_sheet_row
        + 由它派生的 fact 行 + 异常行 + sheet 元信息；清理后 _process_sheet
        全新重做该 sheet，保证失败重跑不产生重复行。
        ★ ProductCostHistory / ExchangeRate 不清理：前者"后值覆盖"语义天然幂等
          （重做命中已有行更新而非新增），后者 upsert 幂等；仅计数可能重复累加
          （行数级偏差，崩溃恢复场景可接受）。
        """
        db = self.db
        rs = db.query(RawSheet).filter(
            RawSheet.batch_id == batch.id, RawSheet.sheet_name == sheet_name).first()
        if not rs:
            return
        row_ids = [r[0] for r in db.query(RawSheetRow.id).filter(RawSheetRow.sheet_id == rs.id)]
        old_facts = 0
        if row_ids:
            for m in _FACT_MODELS:
                old_facts += db.query(m).filter(m.raw_row_id.in_(row_ids)).count()
                db.query(m).filter(m.raw_row_id.in_(row_ids)).delete(synchronize_session=False)
        old_errs = db.query(ImportRowError).filter(ImportRowError.sheet_id == rs.id).count()
        db.query(ImportRowError).filter(ImportRowError.sheet_id == rs.id).delete(synchronize_session=False)
        db.query(RawSheetRow).filter(RawSheetRow.sheet_id == rs.id).delete(synchronize_session=False)
        db.query(RawSheet).filter(RawSheet.id == rs.id).delete(synchronize_session=False)
        # 回退该 sheet 在批次计数中的贡献（fact/异常行可精确回退；崩溃前的中间计数不可信部分就此校正）
        batch.success_rows = max(0, (batch.success_rows or 0) - old_facts)
        batch.failed_rows = max(0, (batch.failed_rows or 0) - old_errs)
        db.flush()
        self._log(f"sheet[{sheet_name}] 上次未完成，已清理残留 {len(row_ids)} 原始行"
                  f"（fact {old_facts} / 异常 {old_errs}）后重做")

    def _log(self, msg: str):
        """向 worker 的进度回调写日志（task 未提供回调时静默忽略）"""
        if self.task and 'progress_cb' in self.task and self.task['progress_cb']:
            self.task['progress_cb'](msg)

    # ---------- 单 sheet ----------
    def _process_sheet(self, batch: ImportBatch, store: Optional[Store], ws):
        """处理一个 sheet：识别 → ODS 全量落库 → DWD 清洗

        处理链：
          1. 空 sheet 判定（整表无业务数据 → 只登记元信息）
          2. sheet 类型识别（report_type）+ 国家/币种识别
          3. 动态表头定位（前 15 行扫描，销售表表头实际在第 10 行）
          4. ODS：**每行每列原始值**入 raw_sheet_row（不可变，溯源链的终点）
          5. 表头归一后分派给对应 _process_* 处理器写 DWD

        ★ 步骤 5 包在 SAVEPOINT 里：单个 sheet 清洗失败只回滚该 sheet 的 fact，
          ODS 原始行与其它 sheet 不受影响（"失败不丢数据、不阻塞其他 sheet"）。
        ★ ODS 里保存的是**原始表头**（未归一），保证溯源时看到的就是 Excel 里的原样。
        """
        db = self.db
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(list(r) if r else [])
        if not rows:
            return
        sheet_name = ws.title

        # ★ 空 sheet 判定：整表除「国家」等少量列外无任何业务数据
        #   （实测大量报表存在空占位 sheet，如 WEL-EU 的 NL/BE/IE广告费、STX-EU_05 的 BE销售）
        non_empty_rows = [r for r in rows if r and any(c is not None and str(c).strip() for c in r)]
        is_empty_sheet = len(non_empty_rows) <= 1

        # 类型 + 国家识别
        report_type, country_hint = detect_report_type(sheet_name)
        # 表头定位（前 15 行内）
        header_idx = detect_header_row(rows)
        if header_idx < 0:
            report_type = report_type or 'unrecognized'
            header_idx = 0
            header = rows[0] if rows else []
        else:
            header = rows[header_idx]
        data_rows = rows[header_idx + 1:]

        if is_empty_sheet:
            self._log(f"sheet[{sheet_name}] 空 sheet（无业务数据），仅登记元信息")
            data_rows = []

        # 国家：sheet 名 → 数据行 → 表头币种
        country = country_hint or detect_country_from_sheet_name(sheet_name)
        currency = detect_currency_from_header(header)
        if not country and data_rows:
            country = detect_country_from_row(header, data_rows[0])
        if not country:
            country = country_hint  # 可能是 None

        raw_sheet = RawSheet(
            batch_id=batch.id, sheet_name=sheet_name, report_type=report_type,
            detected_header_row=header_idx + 1, country=country, currency=currency,
            row_count=len(data_rows),
        )
        db.add(raw_sheet)
        db.flush()

        # ---- ODS：全量行落库（不丢数据，不可变） ----
        # ★ 分片 flush（7.7）：每 IMPORT_BATCH_ROWS 行 add_all+flush 一次，随后只保留
        #   (id, row_no) 轻量元组、释放 ORM 对象引用（Session identity map 为弱引用），
        #   内存峰值与 sheet 总行数解耦；DWD 侧经 _RowRef 仍按 .id/.row_no 访问。
        raw_rows: list[_RowRef] = []
        buf: list[RawSheetRow] = []
        for i, row in enumerate(data_rows):
            values = [None if c is None else (c.isoformat() if isinstance(c, (datetime,)) else c) for c in row]
            # 序列化 Decimal 等
            values = [str(c) if isinstance(c, Decimal) else c for c in values]
            biz = None
            if report_type == 'sales' and values:
                biz = self._row_biz_date(header, values)
            buf.append(RawSheetRow(
                store_id=store.id if store else None,
                batch_id=batch.id, sheet_id=raw_sheet.id, sheet_name=sheet_name,
                row_no=header_idx + 2 + i, biz_date=biz,
                headers_json=[str(h) if h is not None else '' for h in header],
                values_json=values,
            ))
            if len(buf) >= settings.IMPORT_BATCH_ROWS:
                db.add_all(buf)
                db.flush()
                raw_rows.extend(_RowRef(o.id, o.row_no) for o in buf)
                buf = []
        if buf:
            db.add_all(buf)
            db.flush()
            raw_rows.extend(_RowRef(o.id, o.row_no) for o in buf)
            buf = []
        self._log(f"sheet[{sheet_name}] 类型={report_type} 国家={country} 币种={currency} 表头行={header_idx+1} 数据行={len(data_rows)}")

        # ★ 表头多语言归一（Datum/Uhrzeit → date/time）后再做列映射；
        #   ODS 里仍保存**原始表头**以保证溯源真实性
        norm_header = normalize_header(header)

        # ---- DWD：按类型清洗 ----
        # 空 sheet 直接跳过（避免产生"缺少必需列"这类误导性的报告信息）
        if is_empty_sheet or not data_rows:
            raw_sheet.processed = True
            db.commit()
            return

        # ★ DWD 清洗包在 SAVEPOINT 内：单个 sheet 清洗失败只回滚该 sheet 的 fact，
        #   已写入的 ODS 原始行保留（满足"失败不丢数据、不阻塞其他 sheet"的要求）
        try:
            with db.begin_nested():
                self._clean_sheet(batch, raw_sheet, store, report_type, norm_header,
                                  data_rows, raw_rows, country, currency)
        except Exception as e:
            self._log(f"sheet[{sheet_name}] DWD 清洗异常（ODS 已保留）: {type(e).__name__}: {e}")
            db.add(ImportRowError(
                batch_id=batch.id, sheet_id=raw_sheet.id, sheet_name=sheet_name,
                row_no=None, error_type='sheet_failed',
                error_message=f"{type(e).__name__}: {str(e)[:400]}"))
        # ★ 处理终态标记（断点续导依据）：DWD 成功或"记录异常后继续"均视为该 sheet 已处理
        raw_sheet.processed = True
        db.commit()

    def _clean_sheet(self, batch, raw_sheet, store, report_type, norm_header,
                     data_rows, raw_rows, country, currency):
        """按 sheet 类型分派 DWD 清洗（由 _process_sheet 在 SAVEPOINT 中调用）"""
        if report_type == 'aged_storage':
            self._process_fee_aged_storage(batch, raw_sheet, store, norm_header, data_rows, raw_rows, country)
        elif report_type in ('sales', 'unrecognized', None) and norm_header:
            # unrecognized 也尝试按销售列解析（可能别名未登记）；解析不出列就不产 fact
            self._process_sales(batch, raw_sheet, store, norm_header, data_rows, raw_rows,
                                is_sales=(report_type == 'sales'))
        elif report_type == 'ad':
            self._process_ad(batch, raw_sheet, store, norm_header, data_rows, raw_rows, country, currency)
        elif report_type == 'storage':
            self._process_fee_storage(batch, raw_sheet, store, norm_header, data_rows, raw_rows, country)
        elif report_type == 'removal':
            self._process_fee_removal(batch, raw_sheet, store, norm_header, data_rows, raw_rows, country)
        elif report_type == 'inbound':
            self._process_fee_inbound(batch, raw_sheet, store, norm_header, data_rows, raw_rows, country)
        elif report_type == 'returns':
            self._process_fee_return(batch, raw_sheet, store, norm_header, data_rows, raw_rows, country)
        elif report_type == 'exchange_rate':
            self._process_exchange_rate(batch, raw_sheet, store, norm_header, data_rows, raw_rows)
        elif report_type == 'product':
            self._process_product(batch, raw_sheet, store, norm_header, data_rows, raw_rows)

    def _row_biz_date(self, header, values) -> Optional[datetime]:
        """从原始行取业务日期（写入 raw_sheet_row.biz_date，供按日期检索）"""
        idx = find_col(header, 'date/time', '日期/时间', '日期 时间', 'date time')
        if idx < 0:
            idx = find_col_like(header, 'date', '日期')     # 兜底模糊匹配
        if 0 <= idx < len(values):
            return parse_date(values[idx])
        return None

    def _err(self, batch, raw_sheet, sheet_name, row_no, etype, msg, values):
        """记录一条异常行（进异常池，累加批次失败计数）

        ★ 异常行不会中断导入：同一 sheet 其余行照常入库（符合 MM-01 "失败行入异常池、
          不阻塞其他行"的要求）。原始值快照截断到 40 列 × 100 字符，避免异常表膨胀。
        """
        self.db.add(ImportRowError(batch_id=batch.id, sheet_id=raw_sheet.id if raw_sheet else None,
                                   sheet_name=sheet_name, row_no=row_no,
                                   error_type=etype, error_message=str(msg)[:500],
                                   raw_values_json=[str(v)[:100] if v is not None else None for v in (values or [])][:40]))
        batch.failed_rows = (batch.failed_rows or 0) + 1

    def _append_error_summary(self, batch, note: str):
        """向批次失败摘要追加一条说明（不重复追加相同内容）"""
        cur = batch.error_summary or ''
        if note in cur:
            return
        batch.error_summary = (cur + ('；' if cur else '') + note)[:2000]

    def _add_batch_warning(self, batch, kind: str, message: str):
        """批次级告警（非失败、不计数）— 落 import_batch.warnings，报告接口透出

        ★ JSON 列必须整体重新赋值才会触发变更检测（原地 append 不会标 dirty）。
        """
        warns = list(batch.warnings or [])
        if any(w.get('message') == message for w in warns):
            return
        warns.append({'kind': kind, 'message': message})
        batch.warnings = warns

    # ---------- 销售 sheet ----------
    def _process_sales(self, batch, raw_sheet, store, header, data_rows, raw_rows, is_sales: bool):
        """销售交易清洗 → fact_sales_transaction

        关键口径（决定利润是否正确，改动前务必看 tests/test_profit_golden.py）：
          · type 需经 normalize_transaction_type 多语言归一（Anpassung→Adjustment …）
          · 日期需能解析（否则入异常池）—— 日期错会直接导致跨月错配
          · 国家：优先 sheet 识别，行内 marketplace 兜底
          · SKU→ASIN：销售表没有 ASIN 列，需经三元组映射表反查（resolve_asin）
          · Transfer 类型不进本表，单独写 fact_cashflow_transfer（不计入利润）
          · 每一列都映射到标准字段：佣金/FBA费为负值，Adjustment 的分桶在核算层做
        """
        db = self.db
        cmap = map_header_to_canonical(header)

        def col(field):
            """标准字段 → 列索引；-1 表示该列不存在"""
            return cmap.get(field, -1)

        # 必需列校验：销售表至少要有 date/time 与 type，否则无法计算
        required = ['date_time', 'transaction_type'] if is_sales else ['transaction_type']
        for f in required:
            if col(f) < 0:
                if is_sales:
                    self._log(f"sheet[{raw_sheet.sheet_name}] 缺少必需列 {f}，跳过 DWD 清洗（ODS 已保留）")
                return
        store_id = store.id if store else None
        if store_id is None:
            self._log(f"sheet[{raw_sheet.sheet_name}] 店铺未识别，跳过 DWD 清洗（ODS 已保留）")
            return

        country = raw_sheet.country
        repl_cache = {}
        # ★ SKU → ASIN 解析缓存
        #   实测：销售表只有 sku 列，没有 asin 列；ASIN 需经产品表的三元组映射反查
        asin_cache = {}
        # ★ 分片 bulk 落库（7.7）：Transfer 与销售行共用一个缓冲（bulk_save_objects
        #   按类型分组 executemany），内存峰值恒 ≤ IMPORT_BATCH_ROWS 个待插对象
        saver = _BulkSaver(db)

        def resolve_asin(raw_sku: str, norm_sku: str, inline_asin: str) -> str:
            """解析该行的真实 ASIN

            实测：销售表**只有 sku 列没有 asin 列**（旧系统曾用 "SKU 前两段"
            冒充 ASIN，如 J6-NF8L-8JZX → J6，导致商品维度错误）。
            正确做法：经三元组映射表 (store_id, sku) 反查真实 ASIN。
            查不到返回空串 —— 该行仍入库，但商品维度缺失（成本完整性检查会体现出来）。
            结果按 SKU 缓存，避免逐行查库（大表 3000+ 行时的关键优化）。
            """
            if inline_asin:
                return inline_asin
            key = norm_sku or raw_sku
            if not key:
                return ''
            if key in asin_cache:
                return asin_cache[key]
            m = db.query(StoreSkuMapping).filter(
                StoreSkuMapping.store_id == store_id,
                StoreSkuMapping.sku == key,
                StoreSkuMapping.status.in_(('normal', 'pending')),   # 待确认商品也参与统计
            ).first()
            result = m.asin if m else ''
            asin_cache[key] = result
            return result

        for i, row in enumerate(data_rows):
            # ★ 跳过完全空行（实测报表尾部/中间存在全空行，不应计为异常）
            if not row or all(c is None or str(c).strip() == '' for c in row):
                continue

            def gv(field, default=None):
                """按标准字段名取当前行的值（列不存在返回 default）"""
                idx = col(field)
                if idx < 0 or idx >= len(row):
                    return default
                return row[idx]

            ttype_raw = str(gv('transaction_type') or '').strip()
            ttype = normalize_transaction_type(ttype_raw)   # ★ 多语言类型归一（Anpassung→Adjustment）
            biz_date = parse_date(gv('date_time'))
            if not ttype:
                self._err(batch, raw_sheet, raw_sheet.sheet_name, raw_rows[i].row_no, 'missing_column', 'type 列为空', row)
                continue
            if biz_date is None:
                self._err(batch, raw_sheet, raw_sheet.sheet_name, raw_rows[i].row_no, 'bad_date', '日期无法解析', row)
                continue

            raw_sku = str(gv('sku') or '').strip()
            inline_asin = str(gv('asin') or '').strip()
            n_sku = normalize_sku(raw_sku, store_id, db, repl_cache) if raw_sku else ''
            asin = resolve_asin(raw_sku, n_sku, inline_asin)
            row_country = country or detect_country_from_row(header, row)
            if not row_country:
                self._err(batch, raw_sheet, raw_sheet.sheet_name, raw_rows[i].row_no, 'unknown_country', '无法识别国家', row)
                continue

            order_id = str(gv('order_id') or '').strip()
            # Transfer 单独资金流
            # B-07 修复：金额取 total 列实际值。原写法
            #   `gv('line_total') if col('total') >= 0 else gv('total')`
            # 中 canonical 字段名是 'total'（无 'line_total' 表头别名），
            #   total 列存在时恒取 None → 金额恒 0（死分支）。
            if ttype == 'Transfer':
                amt = parse_decimal(gv('total'), None)
                if col('total') < 0 or amt is None:
                    self._err(batch, raw_sheet, raw_sheet.sheet_name, raw_rows[i].row_no,
                              'missing_column', 'Transfer 行 total 列缺失或金额为空', row)
                    continue
                saver.add(FactCashflowTransfer(
                    store_id=store_id, country=row_country, raw_row_id=raw_rows[i].id, batch_id=batch.id,
                    currency=raw_sheet.currency or '', biz_date=biz_date,
                    settlement_id=str(gv('settlement_id') or ''),
                    amount=amt,
                    description=str(gv('description') or ''),
                ))
                batch.success_rows = (batch.success_rows or 0) + 1
                continue

            saver.add(FactSalesTransaction(
                store_id=store_id, country=row_country, raw_row_id=raw_rows[i].id, batch_id=batch.id,
                currency=raw_sheet.currency or '', biz_date=biz_date,
                transaction_type=ttype,
                has_order_id=bool(order_id),
                order_id=order_id, settlement_id=str(gv('settlement_id') or ''),
                sku=raw_sku[:100], normalized_sku=n_sku[:100], asin=asin[:50],
                quantity=parse_int(gv('quantity'), 0),
                marketplace=str(gv('marketplace') or '')[:30], fulfilment=str(gv('fulfillment') or '')[:30],
                product_sales=parse_decimal(gv('product_sales'), Decimal(0)),
                product_sales_tax=parse_decimal(gv('product_sales_tax'), Decimal(0)),
                postage_credits=parse_decimal(gv('postage_credits'), Decimal(0)),
                shipping_credits_tax=parse_decimal(gv('shipping_credits_tax'), Decimal(0)),
                gift_wrap_credits=parse_decimal(gv('gift_wrap_credits'), Decimal(0)),
                giftwrap_credits_tax=parse_decimal(gv('giftwrap_credits_tax'), Decimal(0)),
                promo_rebate=parse_decimal(gv('promotional_rebates'), Decimal(0)),
                promo_rebate_tax=parse_decimal(gv('promotional_rebates_tax'), Decimal(0)),
                marketplace_withheld_tax=parse_decimal(gv('marketplace_withheld_tax'), Decimal(0)),
                selling_fee=parse_decimal(gv('selling_fees'), Decimal(0)),
                fba_fee=parse_decimal(gv('fba_fees'), Decimal(0)),
                other_amount=parse_decimal(gv('other'), Decimal(0)),
                line_total=parse_decimal(gv('total'), Decimal(0)),
                other_transaction_fees=parse_decimal(gv('other_transaction_fees'), Decimal(0)),
            ))
            batch.success_rows = (batch.success_rows or 0) + 1
        saver.flush()

    # ---------- 广告 sheet ----------
    def _process_ad(self, batch, raw_sheet, store, header, data_rows, raw_rows, country, currency):
        """广告数据清洗 → fact_ad_daily（花费/销售额/CTR/ACOS 等指标）

实测三种「商品」列形态都要兼容：
  ① 单列 ASIN；② 合并表头两列（ASIN|SKU）；③ 单列 "ASIN-SKU" 组合值
★ 广告表没有日期列，biz_date 留空，核算时按导入批次月份归集。
★ 花费列用模糊匹配（花费/支出/spend/cost），覆盖多币种表头。
        """
        db = self.db
        store_id = store.id if store else None
        if store_id is None:
            self._log(f"sheet[{raw_sheet.sheet_name}] 店铺未识别，跳过 DWD 清洗")
            return
        saver = _BulkSaver(db)
        # 广告归属月份：取导入任务指定的报表月份（广告表无日期列，只能靠它归月）
        ad_period = (self.task.get('period_month') if self.task else None) \
            or datetime.now().strftime('%Y-%m')
        # ★ 合并工作簿告警：名称含多月区间特征（如"01-03月"/"Q1"）时提示归月偏差风险
        #   （广告表无日期列，多月合导会导致单月广告费虚高 — 数据源问题，代码只能提示）
        # ★ 4.4：告警同时落批次报告（import_batch.warnings），不再只进运行日志
        name_src = f"{self.original_name or ''} {raw_sheet.sheet_name or ''}".lower()
        if re.search(r'\d{1,2}\s*[-~至到]\s*\d{1,2}\s*月', name_src) or re.search(r'\bq[1-4]\b', name_src):
            warn_msg = (f"疑似多月合并工作簿（{self.original_name or raw_sheet.sheet_name}）："
                        f"广告费整批归入归属月份 {ad_period}，单月统计将偏大。"
                        f"建议上游按月拆分后重新导入。")
            self._log(f"⚠ {warn_msg}")
            self._add_batch_warning(batch, 'merged_workbook', warn_msg)
        # 动态识别花费/销售额列（模糊匹配，覆盖 USD/CAD/MX/EUR/GBP/SEK/AUD — 旧系统实测规则）
        spend_idx = find_col_like(header, '花费', '支出', 'spend', 'cost')
        sales_idx = find_col_like(header, '销售额', '销售(', 'sales')
        if spend_idx < 0:
            self._log(f"sheet[{raw_sheet.sheet_name}] 未识别花费列，跳过 DWD（ODS 已保留）")
            return

        def g(row, idx):
            """按列索引安全取值"""
            return row[idx] if 0 <= idx < len(row) else None

        # ★ 实测：广告表「商品」是合并表头，占两列 — 第 1 列 ASIN，第 2 列 SKU
        #   形如: 商品 | (空) | 状态 | ...
        #         B0D9YBK7G5 | LH-PO7F-N6F7 | ...
        product_cols = [j for j, h in enumerate(header)
                        if str(h or '').strip().lower() in ('商品', '商品(entity)', 'product')]
        asin_col = product_cols[0] if product_cols else find_col_like(header, 'asin')
        sku_col = -1
        if len(product_cols) >= 2:
            sku_col = product_cols[1]           # 合并表头的第二列即 SKU
        elif product_cols:
            # 单列「商品」：值可能是 ASIN 或 SKU，按形态判断
            sku_col = -1
        explicit_asin = find_col(header, 'asin', 'asin1')
        if explicit_asin >= 0:
            asin_col = explicit_asin
        explicit_sku = find_col(header, 'sku', 'msku')
        if explicit_sku >= 0:
            sku_col = explicit_sku

        def looks_like_asin(v: str) -> bool:
            """ASIN 形态判断（B0 + 8 位字母数字，共 10 位）

            用于"单列商品值到底是 ASIN 还是 SKU"的分流判断。
            """
            return bool(re.match(r'^B0[A-Z0-9]{8}$', v or ''))

        # ★ 实测第三种形态（STX 广告表）：「商品」单列，值是 "ASIN-SKU"
        #   形如 B0DG2YBBYV-UK-B-CLIP → asin=B0DG2YBBYV, sku=UK-B-CLIP
        combined_re = re.compile(r'^(B0[A-Z0-9]{8})[-_](.+)$')

        for i, row in enumerate(data_rows):
            if not row or all(c is None or str(c).strip() == '' for c in row):
                continue
            # 广告无日期列时 biz_date 为空（按月归属由导入批次月份决定）
            asin = str(g(row, asin_col) or '').strip() if asin_col >= 0 else ''
            sku = str(g(row, sku_col) or '').strip() if sku_col >= 0 else ''
            # 单列商品列时按形态分流
            if asin and sku and not looks_like_asin(asin) and looks_like_asin(sku):
                asin, sku = sku, asin
            # ASIN-SKU 合并值拆分
            if asin and not sku:
                m = combined_re.match(asin)
                if m:
                    asin, sku = m.group(1), m.group(2)
            # ★ 行级国家（合并工作簿的广告表带「国家」列）
            row_country = _row_country(header, row, country) or (country or '')
            saver.add(FactAdDaily(
                store_id=store_id, country=row_country, raw_row_id=raw_rows[i].id, batch_id=batch.id,
                currency=currency or raw_sheet.currency or '',
                biz_date=None,
                # ★ 广告表无日期列 → 必须写入报表归属月份，否则无法按月归集广告费
                period_month=ad_period,
                product_field=(asin or sku)[:200], asin=asin[:50], sku=sku[:100],
                sales=parse_decimal(g(row, sales_idx), Decimal(0)),
                spend=parse_decimal(g(row, spend_idx), Decimal(0)),
                roas=parse_decimal(g(row, find_col_like(header, 'roas')), Decimal(0)),
                acos=parse_decimal(g(row, find_col_like(header, 'acos')), Decimal(0)),
                ctr=parse_decimal(g(row, find_col_like(header, 'ctr')), Decimal(0)),
                cpc=parse_decimal(g(row, find_col_like(header, 'cpc')), Decimal(0)),
                conversion_rate=parse_decimal(g(row, find_col_like(header, '转化率', 'conversion')), Decimal(0)),
                impressions=parse_int(g(row, find_col_like(header, 'ad impressions', '展示次数',
                                                          '展示量', '曝光量', 'impressions')), 0),
                clicks=parse_int(g(row, find_col_like(header, 'ad clicks', '点击量',
                                                      '点击次数', 'clicks')), 0),
                orders=parse_int(g(row, find_col_like(header, 'ad orders', '订单数量', '订单数', '订单', 'orders')), 0),
                ntb_orders=parse_int(g(row, find_col_like(header, 'ad ntb orders', '新买家订单数',
                                                          'ntb 订单数', 'ntb订单数')), 0),
                ntb_sales=parse_decimal(g(row, find_col_like(header, 'ad ntb sales', '新买家销售额',
                                                             'ntb 销售额', 'ntb销售额')), Decimal(0)),
                ntb_order_pct=parse_decimal(g(row, find_col_like(header, '新买家订单比', 'ntb 订单比')), Decimal(0)),
                visible_impressions=parse_int(g(row, find_col_like(header, 'ad visible impressions',
                                                                  '可见展示量')), 0),
                status_val=str(g(row, find_col_like(header, '状态', 'status')) or '')[:50],
                eligibility=str(g(row, find_col_like(header, 'ad eligibility', '资格')) or '')[:100],
                ad_type=str(g(row, find_col_like(header, 'ad type')) or '')[:50],
            ))
            batch.success_rows = (batch.success_rows or 0) + 1
        saver.flush()

    # ---------- 费用 sheets ----------
    def _process_fee_storage(self, batch, raw_sheet, store, header, data_rows, raw_rows, country):
        """月度仓储费清洗 → fact_fee_storage

按 month_of_charge 整月归属；国家从行内 country_code 读取（多国 sheet）。
        """
        db = self.db
        store_id = store.id if store else None
        if store_id is None:
            return
        saver = _BulkSaver(db)
        for i, row in enumerate(data_rows):
            saver.add(FactFeeStorage(
                store_id=store_id, country=_row_country(header, row, country), raw_row_id=raw_rows[i].id, batch_id=batch.id,
                currency=str(_pick(row, header, 'currency', '币种', '货币') or '')[:10],
                asin=str(_pick(row, header, 'asin') or '')[:50],
                fnsku=str(_pick(row, header, 'fnsku') or '')[:50],
                product_name=str(_pick(row, header, 'product name', '产品名称') or '')[:500],
                fulfillment_center=str(_pick(row, header, 'fulfillment center') or '')[:20],
                month_of_charge=normalize_period_month(_pick(row, header, 'month of charge', '收费月份')),
                estimated_monthly_storage_fee=parse_decimal(
                    _pick(row, header, 'estimated monthly storage fee', '预计月度仓储费'), Decimal(0)),
            ))
            batch.success_rows = (batch.success_rows or 0) + 1
        saver.flush()

    def _process_fee_aged(self, batch, raw_sheet, store, header, data_rows, raw_rows, country):
        """（历史遗留占位）超龄仓储费实际由 _process_fee_aged_storage 处理"""
        pass

    def _process_fee_aged_storage(self, batch, raw_sheet, store, header, data_rows, raw_rows, country):
        """超龄仓储费清洗 → fact_fee_aged_storage

按 snapshot_date（快照日期）归属月份；核算时计入"仓储费类"扣减项。
        """
        db = self.db
        store_id = store.id if store else None
        if store_id is None:
            return
        saver = _BulkSaver(db)
        for i, row in enumerate(data_rows):
            saver.add(FactFeeAgedStorage(
                store_id=store_id, country=_row_country(header, row, country), raw_row_id=raw_rows[i].id, batch_id=batch.id,
                currency=str(_pick(row, header, 'currency', '币种') or '')[:10],
                snapshot_date=normalize_period_month(_pick(row, header, 'snapshot date', '快照日期')),
                sku=str(_pick(row, header, 'sku') or '')[:100],
                fnsku=str(_pick(row, header, 'fnsku') or '')[:50],
                asin=str(_pick(row, header, 'asin') or '')[:50],
                qty_charged=parse_int(_pick(row, header, 'quantity charged', '收取数量'), 0),
                amount_charged=parse_decimal(_pick(row, header, 'amount charged', '收取金额'), Decimal(0)),
                surcharge_age_tier=str(_pick(row, header, 'surcharge age tier') or '')[:50],
            ))
            batch.success_rows = (batch.success_rows or 0) + 1
        saver.flush()

    def _process_fee_removal(self, batch, raw_sheet, store, header, data_rows, raw_rows, country):
        """移除费清洗 → fact_fee_removal

按 request_date（请求日期）归属；核算时按业务日期落在区间内过滤。
        """
        db = self.db
        store_id = store.id if store else None
        if store_id is None:
            return
        saver = _BulkSaver(db)
        for i, row in enumerate(data_rows):
            saver.add(FactFeeRemoval(
                store_id=store_id, country=_row_country(header, row, country), raw_row_id=raw_rows[i].id, batch_id=batch.id,
                currency=str(_pick(row, header, 'currency', '币种') or '')[:10],
                request_date=parse_date(_pick(row, header, 'request date', '请求日期')),
                order_id=str(_pick(row, header, 'order id', '订单编号') or '')[:50],
                order_type=str(_pick(row, header, 'order type') or '')[:50],
                service_speed=str(_pick(row, header, 'service speed') or '')[:50],
                sku=str(_pick(row, header, 'sku') or '')[:100],
                fnsku=str(_pick(row, header, 'fnsku') or '')[:50],
                asin=str(_pick(row, header, 'asin') or '')[:50],
                disposition=str(_pick(row, header, 'disposition') or '')[:50],
                shipped_quantity=parse_int(_pick(row, header, 'shipped quantity', '已运送数量'), 0),
                removal_fee=parse_decimal(_pick(row, header, 'removal fee', '移除费'), Decimal(0)),
            ))
            batch.success_rows = (batch.success_rows or 0) + 1
        saver.flush()

    def _process_fee_inbound(self, batch, raw_sheet, store, header, data_rows, raw_rows, country):
        """入库配置费清洗 → fact_fee_inbound_config

按 transaction_date（交易日期）归属区间。
        """
        db = self.db
        store_id = store.id if store else None
        if store_id is None:
            return
        saver = _BulkSaver(db)
        for i, row in enumerate(data_rows):
            saver.add(FactFeeInboundConfig(
                store_id=store_id, country=_row_country(header, row, country), raw_row_id=raw_rows[i].id, batch_id=batch.id,
                currency=str(_pick(row, header, 'currency', '币种', '货币') or '')[:10],
                transaction_date=parse_date(_pick(row, header, 'transaction date', '交易日期', '日期')),
                inbound_plan_id=str(_pick(row, header, 'inbound plan id', '入库计划编号') or '')[:100],
                fba_shipment_id=str(_pick(row, header, 'fba shipment id', '货件编号') or '')[:50],
                fnsku=str(_pick(row, header, 'fnsku') or '')[:50],
                asin=str(_pick(row, header, 'asin') or '')[:50],
                planned_inbound_service=str(_pick(row, header, 'planned inbound service') or '')[:100],
                actual_fee_segment=str(_pick(row, header, 'actual fee segment', '实际费用分段') or '')[:100],
                actual_received_qty=parse_int(_pick(row, header, 'actual received quantity', '实际接收数量'), 0),
                total_fee=parse_decimal(_pick(row, header, 'total fee', '费用总计'), Decimal(0)),
            ))
            batch.success_rows = (batch.success_rows or 0) + 1
        saver.flush()

    def _process_fee_return(self, batch, raw_sheet, store, header, data_rows, raw_rows, country):
        """退货/退回处理费清洗 → fact_fee_return

按 month_of_charge 整月归属（与自然月结算天然对齐）。
        """
        db = self.db
        store_id = store.id if store else None
        if store_id is None:
            return
        saver = _BulkSaver(db)
        for i, row in enumerate(data_rows):
            saver.add(FactFeeReturn(
                store_id=store_id, country=_row_country(header, row, country), raw_row_id=raw_rows[i].id, batch_id=batch.id,
                currency=str(_pick(row, header, 'currency', '币种', '货币') or '')[:10],
                asin=str(_pick(row, header, 'asin') or '')[:50],
                fnsku=str(_pick(row, header, 'fnsku') or '')[:50],
                product_name=str(_pick(row, header, 'product name', '产品名称') or '')[:500],
                sku_sizetier=str(_pick(row, header, 'sku size tier', 'size tier') or '')[:50],
                month_of_shipment=normalize_period_month(_pick(row, header, 'month of shipment', '发货月份')),
                asin_shipped_units=parse_int(_pick(row, header, 'asin shipped units', '已发货数量'), 0),
                asin_returned_units=parse_int(_pick(row, header, 'asin returned units', '已退回数量'), 0),
                sku_returned_units_charged=parse_int(
                    _pick(row, header, 'sku returned units - charged', '已收取退回数量'), 0),
                sku_fee_per_unit=parse_decimal(_pick(row, header, 'fee per unit', '单位费用'), Decimal(0)),
                sku_returns_fee=parse_decimal(_pick(row, header, 'returns fee', '退回处理费'), Decimal(0)),
                month_of_charge=normalize_period_month(_pick(row, header, 'month of charge', '收费月份')),
            ))
            batch.success_rows = (batch.success_rows or 0) + 1
        saver.flush()

    # ---------- 汇率 sheet ----------
    def _process_exchange_rate(self, batch, raw_sheet, store, header, data_rows, raw_rows):
        """汇率表清洗 → exchange_rate

        维度 (店铺, 国家, 月份)；同月同国重复行以后出现的值为准。
        ★ 归属月份取自导入参数 period_month（汇率表本身无月份列）。
        ★ B-08：写入统一走 upsert_rate_with_audit —— 人工值(source='manual')
          不被报表静默覆盖，冲突写批次告警 + history 留痕（reject_overwrite）。
        """
        db = self.db
        c_idx = find_col_like(header, '国家', 'country')
        r_idx = find_col_like(header, '汇率', 'rate')
        if c_idx < 0 or r_idx < 0:
            self._log(f"sheet[{raw_sheet.sheet_name}] 汇率表缺列，跳过")
            return
        # 汇率归属月份：按导入时选择的月份（task meta 传入）；未指定用当前月
        period = self.task.get('period_month') if self.task else None
        if not period:
            period = datetime.now().strftime('%Y-%m')
        store_id = store.id if store else None
        # 同 sheet 内同 (店铺,国家,月份) 重复行聚合取最后值（后值优先，与原语义一致；
        #   也避免 autoflush 关闭时未 flush 新增行对 upsert 查询不可见导致重复插入）
        last: dict[tuple, tuple] = {}
        for i, row in enumerate(data_rows):
            if c_idx >= len(row) or r_idx >= len(row):
                continue
            country_code = str(row[c_idx] or '').strip().upper()
            rate = parse_decimal(row[r_idx], None)
            if not country_code or rate is None:
                continue
            last[(store_id, country_code, period)] = (rate, i)
        for (sid, country_code, pm), (rate, i) in last.items():
            result = upsert_rate_with_audit(
                db, sid, country_code, pm, rate, source='report',
                operator_batch_id=batch.id)
            if result.conflict:
                # 人工值保护命中：不覆盖 + 批次告警留痕（history 已记录 reject_overwrite）
                note = f"汇率冲突已保留人工值 {country_code}/{pm}：报表值 {rate} 未生效"
                self._err(batch, raw_sheet, raw_sheet.sheet_name, raw_rows[i].row_no,
                          'rate_conflict', note, data_rows[i])
                self._append_error_summary(batch, note)
                self._log(f"⚠ {note}")
            else:
                batch.success_rows = (batch.success_rows or 0) + 1

    # ---------- 产品表 sheet ----------
    def _process_product(self, batch, raw_sheet, store, header, data_rows, raw_rows):
        """产品表清洗 → 三元组映射 + 成本版本

两项产出：
  ① store_product / store_sku_mapping：新品自动入"待确认区"
  ② product_cost_history：采购成本 + 头程运费（按月版本化）
★ 运费列兼容"通用运费"与"按国家拆分运费"（英国站运费/爱尔兰站运费…），
  统一存进 freight_json。
★ 同一 (asin,sku) 在一个 sheet 内可能重复出现，用内存缓存合并去重
  （Session 关闭 autoflush，只靠 query 看不到未 flush 的新增行，会撞唯一键）。
        """
        db = self.db
        store_id = store.id if store else None
        if store_id is None:
            self._log(f"sheet[{raw_sheet.sheet_name}] 店铺未识别，跳过产品成本入库")
            return
        period = self.task.get('period_month') if self.task else None
        if not period:
            period = datetime.now().strftime('%Y-%m')
        sku_idx = find_col_like(header, 'sku', '卖家sku')
        asin_idx = find_col_like(header, 'asin')
        cost_idx = find_col_like(header, '成本')
        freight_cols = []  # (国家代码或'default', idx) — 通用运费 + 国家专属运费
        for j, h in enumerate(header):
            hl = str(h or '').strip()
            if '运费' not in hl and 'freight' not in hl.lower():
                continue
            # 中文站点名/英文代码 → 国家代码（实测列名：英国站点运费 / 爱尔兰站运费 / 运费）
            cc = 'default'
            for kw, code in FREIGHT_COUNTRY_HINTS.items():
                if kw in hl:
                    cc = code
                    break
            freight_cols.append((cc, j))
        title_idx = find_col_like(header, '产品名称', 'product name', '标题')
        if sku_idx < 0 and asin_idx < 0:
            self._log(f"sheet[{raw_sheet.sheet_name}] 产品表缺 SKU/ASIN 列，跳过")
            return

        # ★ 表内去重缓存：同 sheet 可能重复出现同一 (asin, sku)（多国运费分行等情况）。
        #   由于 Session 关闭了 autoflush，仅靠 query 无法看到本事务内未 flush 的新增行，
        #   会撞 product_cost_history 的唯一键 → 这里用内存缓存先合并，再写库。
        cost_cache: dict[tuple, ProductCostHistory] = {}

        for i, row in enumerate(data_rows):
            sku = str(row[sku_idx] or '').strip() if 0 <= sku_idx < len(row) else ''
            asin = str(row[asin_idx] or '').strip() if 0 <= asin_idx < len(row) else ''
            if not sku and not asin:
                continue
            title = str(row[title_idx] or '')[:500] if 0 <= title_idx < len(row) else ''
            # 三元组映射（新品自动入待确认区）
            if asin and sku:
                resolve_mapping(db, store_id, sku, asin, title)
                # 回填主档标题
                prod = db.query(StoreProduct).filter(
                    StoreProduct.store_id == store_id, StoreProduct.asin == asin).first()
                if prod and title and not prod.title:
                    prod.title = title
            # 成本/运费 → product_cost_history（按月版本）
            cost = parse_decimal(row[cost_idx] if 0 <= cost_idx < len(row) else None, None)
            freight_json = None
            for cc, j in freight_cols:
                v = parse_decimal(row[j] if j < len(row) else None, None)
                if v is not None:
                    freight_json = freight_json or {}
                    freight_json[cc] = float(v)

            key = (store_id, asin, sku, period)
            existing = cost_cache.get(key)
            if existing is None:
                existing = db.query(ProductCostHistory).filter(
                    ProductCostHistory.store_id == store_id,
                    ProductCostHistory.asin == asin, ProductCostHistory.sku == sku,
                    ProductCostHistory.period == period, ProductCostHistory.source_type == 'report',
                ).first()
            if existing is not None:
                # 同名行后出现的值覆盖先出现的（同月同品应只有一个成本版本）
                if cost is not None:
                    existing.cost_rmb = cost
                if freight_json:
                    merged = dict(existing.freight_json or {})
                    merged.update(freight_json)
                    existing.freight_json = merged
                existing.source_batch_id = batch.id
                existing.source_row_id = raw_rows[i].id
            else:
                rec = ProductCostHistory(
                    store_id=store_id, asin=asin, sku=sku, period=period,
                    cost_rmb=cost, freight_json=freight_json,
                    source_batch_id=batch.id, source_type='report',
                    source_row_id=raw_rows[i].id,
                )
                db.add(rec)
                cost_cache[key] = rec
            batch.success_rows = (batch.success_rows or 0) + 1


def _row_country(header, row, sheet_country) -> str:
    """行级国家：优先行内 country_code/国家 列（多国家 sheet），否则用 sheet 级识别结果"""
    from app.services.importer.parser import detect_country_from_code_column
    c = detect_country_from_code_column(header, row)
    return c or sheet_country or ''


# 运费列名中的国家线索（中文站点名/英文代码 → 站点代码）
FREIGHT_COUNTRY_HINTS = {
    '英国': 'UK', 'UK': 'UK', 'GB': 'UK',
    '爱尔兰': 'IE', 'IE': 'IE',
    '德国': 'DE', 'DE': 'DE',
    '法国': 'FR', 'FR': 'FR',
    '西班牙': 'ES', 'ES': 'ES',
    '意大利': 'IT', 'IT': 'IT',
    '荷兰': 'NL', 'NL': 'NL',
    '瑞典': 'SE', 'SE': 'SE',
    '比利时': 'BE', 'BE': 'BE',
    '美国': 'US', 'US': 'US',
    '加拿大': 'CA', 'CA': 'CA',
    '墨西哥': 'MX', 'MX': 'MX',
    '阿联酋': 'AE', 'AE': 'AE',
    '沙特': 'SA', 'SA': 'SA',
    '澳大利亚': 'AU', '澳洲': 'AU', 'AU': 'AU',
    '波兰': 'PL', 'PL': 'PL',
}


def _pick(row, header, *names):
    """按列名（含/精确）取行值"""
    for name in names:
        idx = find_col(header, name)
        if idx < 0:
            idx = find_col_like(header, name)
        if 0 <= idx < len(row):
            return row[idx]
    return None
