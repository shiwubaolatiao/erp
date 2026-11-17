# -*- coding: utf-8 -*-
"""07 基准产品信息表解析与存储（mapping_review_enhance 组3）

基准 = 各店铺月度汇总 xlsx 的产品 sheet（产品明细表/产品信息表），
以 (store, asin, sku) 为 ASIN↔SKU 权威来源，供批量确认比对。

设计要点（design 2.4.1）：
  · 纯函数复用 parser.py：parse_store_from_filename / detect_report_type /
    detect_header_row / find_col(_like)，不写死行号与列名；
  · ★ 不复用 _process_product（会写 product_cost_history，违反 spec 1.4.3 禁止项）；
  · ★ 不产生 ImportBatch/RawSheet——基准导入不是报表导入，不污染 ODS/DWD；
  · 单文件失败隔离（try/except 计 failed 继续）；店铺不可判/无产品 sheet/表头不识别
    → 跳过 + 告警，禁止默认归属与猜测取数（spec 5.1.3）。
"""
import os
from dataclasses import dataclass
from datetime import datetime

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from app.models import MappingBaselineItem, MappingOpReport, Store
from app.services.importer.parser import (
    detect_header_row, detect_report_type, find_col, find_col_like,
    parse_store_from_filename,
)
from app.services.mapping import report as report_svc

BASELINE_EXTS = ('.xlsx', '.xlsm')
PERSIST_CHUNK = 500


@dataclass
class FileParseOutcome:
    file: str
    store_code: str | None = None
    sheet_name: str = ''
    rows: int = 0          # 有效条目行数
    items: int = 0         # 本文件条目数（去重前）
    empty_rows: int = 0    # ASIN/SKU 任一为空的行
    status: str = 'ok'     # ok / skipped / failed
    message: str = ''


def _cell(row: list, idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ''
    v = row[idx]
    return str(v).strip() if v is not None else ''


def parse_baseline_file(path: str) -> tuple[FileParseOutcome, list[tuple[str, str]]]:
    """解析单个文件的产品 sheet → (结果, [(asin, sku), ...])"""
    fname = os.path.basename(path)
    outcome = FileParseOutcome(file=fname)
    store_code, _region = parse_store_from_filename(fname)
    if not store_code:
        outcome.status = 'skipped'
        outcome.message = 'store not identifiable from filename (no default assignment)'
        return outcome, []
    outcome.store_code = store_code

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # 损坏/占用（spec 5.1.3 场景 1）
        outcome.status = 'failed'
        outcome.message = f'file unreadable: {type(exc).__name__}'
        return outcome, []
    try:
        candidates = [sn for sn in wb.sheetnames
                      if detect_report_type(sn)[0] == 'product']  # 产品明细表/产品信息表均命中
        if not candidates:
            outcome.status = 'skipped'
            outcome.message = 'no product sheet found (产品明细表/产品信息表)'
            return outcome, []
        sheet_name = candidates[0]
        outcome.sheet_name = sheet_name
        ws = wb[sheet_name]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        header_idx = detect_header_row(rows, max_scan=15)
        if header_idx < 0:
            outcome.status = 'skipped'
            outcome.message = 'header row not recognizable'
            return outcome, []
        header = [str(h or '') for h in rows[header_idx]]
        asin_idx = find_col(header, 'asin')
        if asin_idx < 0:
            asin_idx = find_col_like(header, 'asin')
        # SKU 列判定顺序：精确 sku → 精确 amazon sku → 模糊（兼容 WEL-NA 的 "Amazon SKU"）
        sku_idx = find_col(header, 'sku')
        if sku_idx < 0:
            sku_idx = find_col(header, 'amazon sku')
        if sku_idx < 0:
            sku_idx = find_col_like(header, 'sku')
        if asin_idx < 0 or sku_idx < 0:
            outcome.status = 'skipped'
            outcome.message = 'header missing ASIN or SKU column'
            return outcome, []

        entries: list[tuple[str, str]] = []
        for row in rows[header_idx + 1:]:
            if not row or all(c is None or str(c).strip() == '' for c in row):
                continue
            asin, sku = _cell(row, asin_idx), _cell(row, sku_idx)
            if not asin or not sku:
                outcome.empty_rows += 1        # spec 5.1.5
                continue
            entries.append((asin, sku))
        outcome.rows = len(entries)
        outcome.items = len(entries)
        return outcome, entries
    finally:
        wb.close()


def _collect_files(dir_path: str | None, file_paths: list[str] | None) -> list[str]:
    if file_paths:
        return [p for p in file_paths if str(p).strip()]
    if not dir_path or not os.path.isdir(dir_path):
        return []
    files = []
    for name in sorted(os.listdir(dir_path)):
        if name.startswith('~$'):
            continue
        if name.lower().endswith(BASELINE_EXTS):
            files.append(os.path.join(dir_path, name))
    return files


def parse_baseline_dir(dir_path: str | None = None,
                       file_paths: list[str] | None = None
                       ) -> tuple[dict, dict[str, set[tuple[str, str]]]]:
    """解析基准目录/文件清单 → (导入报告, {store_code: {(asin, sku), ...}})

    报告与条目分离：报告可序列化落库，条目集合供 persist 幂等落库。
    """
    files = _collect_files(dir_path, file_paths)
    items: dict[tuple[str, str, str], int] = {}
    keys_by_store: dict[str, set[tuple[str, str]]] = {}
    per_file: list[dict] = []
    warnings: list[str] = []
    empty_rows = 0

    for path in files:
        outcome, entries = parse_baseline_file(path)
        empty_rows += outcome.empty_rows
        for asin, sku in entries:
            key = (outcome.store_code, asin, sku)
            items[key] = items.get(key, 0) + 1
            keys_by_store.setdefault(outcome.store_code, set()).add((asin, sku))
        per_file.append({
            'file': outcome.file, 'store_code': outcome.store_code,
            'sheet_name': outcome.sheet_name, 'rows': outcome.rows,
            'items': outcome.items, 'status': outcome.status, 'message': outcome.message,
        })
        if outcome.status != 'ok':
            warnings.append(f"{outcome.file}: {outcome.message}")

    # 基准内冲突（spec 5.1.7）：同 (store, sku) 对应多个 ASIN → 批量确认时禁止自动确认
    sku2asins: dict[tuple[str, str], set[str]] = {}
    for (store_code, asin, sku) in items:
        sku2asins.setdefault((store_code, sku), set()).add(asin)
    conflicts = [{'store_code': sc, 'sku': sku, 'asins': sorted(asins)}
                 for (sc, sku), asins in sorted(sku2asins.items()) if len(asins) > 1]

    files_ok = sum(1 for f in per_file if f['status'] == 'ok')
    files_skipped = sum(1 for f in per_file if f['status'] == 'skipped')
    files_failed = sum(1 for f in per_file if f['status'] == 'failed')
    dedup_merged = sum(c - 1 for c in items.values())

    report = {
        'op_type': 'baseline_import',
        'dry_run': True,  # 由编排层按实际覆盖
        'store_scope': 'ALL',
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'summary': {
            'files_total': len(per_file), 'files_ok': files_ok,
            'files_skipped': files_skipped, 'files_failed': files_failed,
            'items_total': len(items), 'dedup_merged': dedup_merged,
            'empty_rows': empty_rows, 'baseline_conflicts': len(conflicts),
        },
        'details': {'files': per_file, 'conflicts': conflicts, 'failed': [
            f for f in per_file if f['status'] == 'failed']},
        'warnings': warnings,
    }
    return report, keys_by_store


def persist_baseline(db: Session, keys_by_store: dict[str, set[tuple[str, str]]],
                     report: dict) -> int:
    """幂等落库：按 uq(store_id, asin, sku) 只插入缺失项 → 返回新增条数

    分片提交（500 条/批），中断后重跑仅补剩余（幂等续跑，spec 4.2）。
    """
    codes = [c for c in keys_by_store if keys_by_store[c]]
    if not codes:
        return 0
    store_map = {s.store_code: s.id for s in
                 db.query(Store).filter(Store.store_code.in_(codes)).all()}
    file_by_store = {f['store_code']: f['file'] for f in report['details']['files']
                     if f['store_code'] and f['status'] == 'ok'}
    sheet_by_store = {f['store_code']: f['sheet_name'] for f in report['details']['files']
                      if f['store_code'] and f['status'] == 'ok'}

    to_insert: list[tuple[int, str, str, str, str]] = []
    for code, keys in keys_by_store.items():
        sid = store_map.get(code)
        if not sid:
            continue
        for asin, sku in keys:
            to_insert.append((sid, asin, sku,
                              file_by_store.get(code, ''), sheet_by_store.get(code, '')))

    existing = set()
    sids = {t[0] for t in to_insert}
    if sids:
        for sid in sids:
            for (asin, sku) in db.query(MappingBaselineItem.asin, MappingBaselineItem.sku).filter(
                    MappingBaselineItem.store_id == sid).all():
                existing.add((sid, asin, sku))
    to_insert = [t for t in to_insert if (t[0], t[1], t[2]) not in existing]

    inserted = 0
    for i in range(0, len(to_insert), PERSIST_CHUNK):
        chunk = to_insert[i:i + PERSIST_CHUNK]
        for sid, asin, sku, src_f, src_s in chunk:
            db.add(MappingBaselineItem(store_id=sid, asin=asin, sku=sku,
                                       source_file=src_f, source_sheet=src_s))
        db.commit()
        inserted += len(chunk)
    return inserted


def build_baseline_index(db: Session, store_ids: list[int] | None = None) -> dict[int, dict]:
    """内存索引（批量确认一次性构建，design 2.4.2）：
    {store_id: {exact: set[(sku, asin)], sku2asin, asin2sku, conflicts: set[sku]}}
    """
    q = db.query(MappingBaselineItem)
    if store_ids:
        q = q.filter(MappingBaselineItem.store_id.in_(store_ids))
    index: dict[int, dict] = {}
    for row in q.all():
        d = index.setdefault(row.store_id,
                             {'exact': set(), 'sku2asin': {}, 'asin2sku': {}, 'conflicts': set()})
        d['exact'].add((row.sku, row.asin))
        d['sku2asin'].setdefault(row.sku, set()).add(row.asin)
        d['asin2sku'].setdefault(row.asin, set()).add(row.sku)
    for d in index.values():
        for sku, asins in d['sku2asin'].items():
            if len(asins) > 1:
                d['conflicts'].add(sku)
    return index


def baseline_summary(db: Session) -> dict:
    """基准概览：总数 + 按店铺聚合 + 最近导入报告 id"""
    rows = db.query(MappingBaselineItem).all()
    stores = {s.id: s.store_code for s in db.query(Store).all()}
    by_store: dict[str, dict] = {}
    for r in rows:
        code = stores.get(r.store_id, f"#{r.store_id}")
        d = by_store.setdefault(code, {'store_code': code, 'items': 0,
                                       'source_files': set(), 'imported_at': None})
        d['items'] += 1
        if r.source_file:
            d['source_files'].add(r.source_file)
        if r.imported_at and (not d['imported_at'] or r.imported_at > d['imported_at']):
            d['imported_at'] = r.imported_at
    for d in by_store.values():
        d['source_files'] = sorted(d['source_files'])
        d['imported_at'] = d['imported_at'].isoformat() if d['imported_at'] else None

    last_row = (db.query(MappingOpReport)
                .filter(MappingOpReport.op_type == 'baseline_import')
                .order_by(MappingOpReport.id.desc()).first())
    return {
        'items_total': len(rows),
        'by_store': sorted(by_store.values(), key=lambda x: x['store_code']),
        'last_import_report_id': last_row.id if last_row else None,
    }


def import_baseline(db: Session, dir_path: str | None = None,
                    file_paths: list[str] | None = None, dry_run: bool = False,
                    operator_user_id: int | None = None,
                    operator_username: str = '') -> dict:
    """基准导入编排：解析 →（非 dry-run）幂等落库 + 报告落库 + 审计"""
    report, keys_by_store = parse_baseline_dir(dir_path, file_paths)
    report['report_id'] = None
    if not report['details']['files']:
        report['warnings'] = report['warnings'] + ['no xlsx file matched']
        return report
    report['dry_run'] = dry_run
    if dry_run:
        return report

    inserted = persist_baseline(db, keys_by_store, report)
    report['summary']['items_inserted'] = inserted

    db.commit()
    report['report_id'] = report_svc.save_report(db, report, operator_user_id)
    report_svc.log_mapping_operation(
        db, 'mapping_baseline_import', f"files={report['summary']['files_total']}",
        f"新增基准 {inserted} 条；冲突 {report['summary']['baseline_conflicts']} 组",
        operator_user_id, operator_username)
    db.commit()
    return report