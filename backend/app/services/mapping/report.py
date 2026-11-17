# -*- coding: utf-8 -*-
"""映射操作统一报告 — 契约 / 落库 / 查询 / 导出（mapping_review_enhance 组5）

三类操作（基准导入 / 批量确认 / 存量重归一）共用同一报告契约：
  {op_type, dry_run, store_scope, generated_at, summary{计数}, details{清单}, warnings[]}
落库 mapping_op_report（append-only，不覆盖历史），payload 存三类清单明细，
供报告重建（spec 5.4.3 场景 1）与 xlsx 导出（spec 5.4.2）。
计数自洽不满足则拒绝落库（防静默错账）。
"""
from datetime import datetime

from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.models import MappingOpReport, OperationLog

VALID_OP_TYPES = ('baseline_import', 'bulk_confirm', 'replacement_realign')

# 计数自洽：总数字段 = 各分类字段之和（不满足则拒绝落库）
INVARIANTS = {
    'baseline_import': ('files_total', ('files_ok', 'files_skipped', 'files_failed')),
    'bulk_confirm': ('total_pending',
                     ('confirmed', 'uncovered', 'conflict', 'failed', 'skipped_already_normal')),
    'replacement_realign': ('scanned',
                            ('unchanged', 'changed', 'kept_raw', 'invalid_target')),
}


def _check_invariant(op_type: str, summary: dict) -> None:
    total_field, parts = INVARIANTS[op_type]
    total = summary.get(total_field, 0)
    s = sum(summary.get(p, 0) for p in parts)
    if total != s:
        raise ValueError(f"报告计数不自洽: {total_field}={total} 但各分类之和={s} ({op_type})")


def save_report(db: Session, report: dict, operator_user_id: int | None = None) -> int:
    """报告落库（append-only）→ 返回 report_id

    dry_run 报告默认不落库（调用方决定）；本函数只负责持久化与自洽校验。
    """
    op_type = report.get('op_type')
    if op_type not in VALID_OP_TYPES:
        raise ValueError(f"未知操作类型: {op_type}")
    _check_invariant(op_type, report.get('summary') or {})
    row = MappingOpReport(
        op_type=op_type,
        dry_run=bool(report.get('dry_run')),
        store_scope=report.get('store_scope') or 'ALL',
        summary=report.get('summary') or {},
        payload=report.get('details') or {},
        operator_user_id=operator_user_id,
    )
    db.add(row)
    db.flush()
    return row.id


def log_mapping_operation(db: Session, action: str, target: str, detail: str,
                          user_id: int | None = None, username: str = '') -> None:
    """统一审计留痕（组4.5）：action ∈ mapping_baseline_import / mapping_bulk_confirm / mapping_realign"""
    db.add(OperationLog(user_id=user_id, username=username,
                        action=action, target=target, result='ok', detail=detail))


def list_reports(db: Session, op_type: str | None = None, limit: int = 20) -> list[dict]:
    """报告列表（新→旧）"""
    q = db.query(MappingOpReport)
    if op_type:
        if op_type not in VALID_OP_TYPES:
            raise ValueError(f"未知操作类型: {op_type}")
        q = q.filter(MappingOpReport.op_type == op_type)
    rows = q.order_by(MappingOpReport.id.desc()).limit(min(limit, 100)).all()
    return [{
        'report_id': r.id, 'op_type': r.op_type, 'dry_run': bool(r.dry_run),
        'store_scope': r.store_scope or 'ALL', 'summary': r.summary or {},
        'generated_at': r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


def get_report(db: Session, report_id: int) -> dict | None:
    """按 id 重建完整报告对象（与执行响应同构）"""
    r = db.query(MappingOpReport).get(report_id)
    if not r:
        return None
    return {
        'report_id': r.id, 'op_type': r.op_type, 'dry_run': bool(r.dry_run),
        'store_scope': r.store_scope or 'ALL',
        'generated_at': r.created_at.isoformat() if r.created_at else None,
        'summary': r.summary or {},
        'details': r.payload or {},
        'warnings': [],
    }


# ------------------------------------------------------------- xlsx 导出 ----

_EXPORT_SECTIONS = ('all', 'uncovered', 'conflict', 'unclassified', 'changed', 'files')


def _sheet_rows(section: str, report: dict) -> tuple[str, list[str], list[list]]:
    """按 section 生成 (sheet 名, 表头, 行数据)"""
    details = report.get('details') or {}
    op = report['op_type']
    if op == 'baseline_import':
        files = details.get('files') or []
        conflicts = details.get('conflicts') or []
        if section in ('all', 'files') and files:
            return ('文件清单', ['文件', '店铺', 'sheet', '有效行', '条目', '状态', '说明'],
                    [[f.get('file', ''), f.get('store_code') or '', f.get('sheet_name') or '',
                      f.get('rows', 0), f.get('items', 0), f.get('status', ''),
                      f.get('message', '')] for f in files])
        if conflicts:
            return ('基准内冲突', ['店铺', 'SKU', 'ASIN集合'],
                    [[c.get('store_code', ''), c.get('sku', ''), ','.join(c.get('asins') or [])]
                     for c in conflicts])
        return ('文件清单', ['文件', '店铺', 'sheet', '有效行', '条目', '状态', '说明'], [])
    if op == 'bulk_confirm':
        if section in ('all', 'uncovered'):
            rows = details.get('uncovered') or []
            return ('未覆盖', ['店铺', 'SKU', 'ASIN', '原因'],
                    [[m.get('store_code', ''), m.get('sku', ''), m.get('asin', ''),
                      m.get('reason', '')] for m in rows])
        if section == 'conflict':
            rows = details.get('conflict') or []
            return ('冲突', ['店铺', 'SKU', 'ASIN', '基准SKU', '基准ASIN', '冲突类型'],
                    [[m.get('store_code', ''), m.get('sku', ''), m.get('asin', ''),
                      ','.join(m.get('baseline_skus') or []),
                      ','.join(m.get('baseline_asin') or []) or (m.get('baseline_asin') or ''),
                      m.get('conflict_type', '')] for m in rows])
        rows = details.get('confirmed') or []
        return ('已确认', ['店铺', 'SKU', 'ASIN'],
                [[m.get('store_code', ''), m.get('sku', ''), m.get('asin', '')] for m in rows])
    # replacement_realign
    if section in ('all', 'changed'):
        rows = details.get('changed') or []
        return ('变更明细', ['店铺', '原始值', '旧父SKU', '新父SKU'],
                [[c.get('store_code', ''), c.get('replacement_sku', ''),
                  c.get('old_parent_sku', ''), c.get('new_parent_sku', '')] for c in rows])
    rows = details.get('unclassified') or []
    return ('待归类', ['店铺', '原始值', '候选SKU', '相似度', '原因'],
            [[u.get('store_code', ''), u.get('replacement_sku', ''),
              ','.join(f"{c.get('sku', '')}({c.get('score', '')})" for c in (u.get('candidates') or [])[:3]),
              '', u.get('reason', '')] for u in rows])


def export_report(db: Session, report_id: int, section: str = 'all') -> tuple[Workbook, str]:
    """导出清单 xlsx → (Workbook, 文件名)；由 API 层复用 _xlsx_response 输出"""
    report = get_report(db, report_id)
    if not report:
        raise ValueError("报告不存在")
    if section not in _EXPORT_SECTIONS:
        raise ValueError(f"未知清单类型: {section}")
    sheet_name, header, rows = _sheet_rows(section, report)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]
    ws.append(header)
    for r in rows:
        ws.append([str(c) if c is not None else '' for c in r])
    fname = f"映射报告_{report['op_type']}_{report_id}.xlsx"
    return wb, fname