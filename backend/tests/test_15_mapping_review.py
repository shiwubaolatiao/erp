# -*- coding: utf-8 -*-
"""映射审核增强测试（mapping_review_enhance 组3/4/5/6）

覆盖 design 2.7.1 用例清单：
  基准解析 10（双 sheet 名 / 双 SKU 列名 / 文件名判店铺 / 空值行 / 去重 / 坏文件 /
              无产品 sheet / 表头不识别 / 店铺不可判 / 导入幂等）
  批量确认 9（完全匹配转正 / dry-run 零写库 / 未覆盖保留 / 冲突不自动确认 / 漂移红线 /
              排除 abnormal / 未导入基准 400 / 幂等重跑 / skipped_already_normal）
  报告 3（计数自洽 / 可查可导出 / 审计留痕）
  接口层 2（既有 decide 兼容 + 409 / 非管理员 403）

★ 全部用 ORM 独立构造数据（STX/WEL 店铺，不碰 e2e fixture 的 LMG/MGT 数据）。
"""
import pytest
from openpyxl import load_workbook

from app.models import (
    MappingBaselineItem, MappingOpReport, Store, StoreProduct, StoreSkuMapping,
)
from app.services.mapping.baseline import import_baseline, parse_baseline_dir
from app.services.mapping.bulk_confirm import bulk_confirm

STX = 'STX'
WEL = 'WEL'


@pytest.fixture(scope='module')
def db(client):
    from app.core.database import SessionLocal
    s = SessionLocal()
    yield s
    s.close()


def _sid(db, code: str) -> int:
    return db.query(Store).filter(Store.store_code == code).first().id


def _purge(db):
    db.rollback()
    ids = [st.id for st in db.query(Store).filter(Store.store_code.in_((STX, WEL))).all()]
    if not ids:
        return
    db.query(MappingBaselineItem).filter(MappingBaselineItem.store_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(StoreSkuMapping).filter(StoreSkuMapping.store_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(StoreProduct).filter(StoreProduct.store_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(MappingOpReport).delete(synchronize_session=False)
    db.commit()


@pytest.fixture
def clean(db):
    _purge(db)
    yield
    _purge(db)


def _xlsx(tmp_path, name, rows, sheet='产品信息表'):
    from conftest import build_xlsx
    return build_xlsx(tmp_path / name, [(sheet, rows)])


def _add_baseline(db, sid, pairs):
    for asin, sku in pairs:
        db.add(MappingBaselineItem(store_id=sid, asin=asin, sku=sku,
                                   source_file='t07.xlsx', source_sheet='产品信息表'))
    db.commit()


def _add_pending(db, sid, sku, asin, product=None):
    m = StoreSkuMapping(store_id=sid, sku=sku, asin=asin, status='pending')
    if product is not None:
        m.store_product_id = product.id
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _add_normal(db, sid, sku, asin):
    m = StoreSkuMapping(store_id=sid, sku=sku, asin=asin, status='normal')
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


# ================= 基准解析（10 用例） =================

def test_parse_dual_sheet_names(tmp_path):
    """「产品明细表」与「产品信息表」两种 sheet 名均解析出条目"""
    _xlsx(tmp_path, 'STX-EU07.xlsx', [['ASIN', 'SKU'], ['B0STX1', 'SKU-A']], sheet='产品明细表')
    _xlsx(tmp_path, 'WEL-EU07.xlsx', [['ASIN', 'SKU'], ['B0WEL1', 'SKU-B']], sheet='产品信息表')
    report, keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert report['summary']['files_ok'] == 2
    assert ('B0STX1', 'SKU-A') in keys[STX]
    assert ('B0WEL1', 'SKU-B') in keys[WEL]


def test_parse_dual_sku_columns(tmp_path):
    """SKU 列名兼容：精确 SKU 与 "Amazon SKU"（WEL-NA 实测）"""
    _xlsx(tmp_path, 'STX-NA07.xlsx', [['ASIN', 'SKU'], ['B0STX1', 'SKU-A']])
    _xlsx(tmp_path, 'WEL-NA07.xlsx', [['ASIN', 'Amazon SKU'], ['B0WEL1', 'WL-001']])
    report, keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert ('B0STX1', 'SKU-A') in keys[STX]
    assert ('B0WEL1', 'WL-001') in keys[WEL]


def test_parse_store_from_filename(tmp_path):
    """文件名前缀判店铺：EU/NA 同店铺聚合"""
    _xlsx(tmp_path, 'LMG-EU07.xlsx', [['ASIN', 'SKU'], ['B0L1', 'LG-EU']])
    _xlsx(tmp_path, 'LMG-NA07.xlsx', [['ASIN', 'SKU'], ['B0L2', 'LG-NA']])
    report, keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert set(keys.keys()) == {'LMG'}
    assert keys['LMG'] == {('B0L1', 'LG-EU'), ('B0L2', 'LG-NA')}


def test_parse_empty_rows(tmp_path):
    """空 ASIN / 空 SKU 行跳过并计数 empty_rows（完全空行不计）"""
    _xlsx(tmp_path, 'STX-EU07.xlsx',
          [['ASIN', 'SKU'], ['B0STX1', 'SKU-A'], [None, 'SKU-B'], ['B0STX2', ''], ['', '']])
    report, keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert report['summary']['empty_rows'] == 2
    assert keys[STX] == {('B0STX1', 'SKU-A')}


def test_parse_dedup(tmp_path):
    """同 (store, asin, sku) 多行去重合并并标注 dedup_merged"""
    _xlsx(tmp_path, 'STX-EU07.xlsx',
          [['ASIN', 'SKU'], ['B0STX1', 'SKU-A'], ['B0STX1', 'SKU-A'], ['B0STX1', 'SKU-A']])
    report, keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert report['summary']['items_total'] == 1
    assert report['summary']['dedup_merged'] == 2


def test_parse_bad_file_isolated(tmp_path):
    """损坏文件计 failed，其余文件正常解析（单文件隔离）"""
    (tmp_path / 'STX-OK07.xlsx').write_bytes(b'\xd0\xcf not an excel')
    _xlsx(tmp_path, 'WEL-EU07.xlsx', [['ASIN', 'SKU'], ['B0WEL1', 'SKU-B']])
    report, keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert report['summary']['files_failed'] == 1
    assert report['summary']['files_ok'] == 1
    assert keys[WEL] == {('B0WEL1', 'SKU-B')}


def test_parse_no_product_sheet(tmp_path):
    """无产品 sheet → 跳过 + 告警，不影响其余文件"""
    _xlsx(tmp_path, 'STX-EU07.xlsx', [['日期', '类型'], ['x', 'y']], sheet='UK销售')
    _xlsx(tmp_path, 'WEL-EU07.xlsx', [['ASIN', 'SKU'], ['B0WEL1', 'SKU-B']])
    report, _keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert report['summary']['files_skipped'] == 1
    assert report['summary']['files_ok'] == 1
    assert any('no product sheet' in w for w in report['warnings'])


def test_parse_header_unrecognized(tmp_path):
    """表头不识别（无 ASIN/SKU 列）→ 跳过，禁止猜测取数"""
    _xlsx(tmp_path, 'STX-EU07.xlsx', [['型号', '颜色'], ['M1', '白']])
    report, _keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert report['summary']['files_skipped'] == 1
    assert any('header' in w for w in report['warnings'])


def test_parse_store_unidentifiable(tmp_path):
    """店铺不可判 → 跳过，禁止默认归属"""
    _xlsx(tmp_path, '汇总07.xlsx', [['ASIN', 'SKU'], ['B0X1', 'SKU-X']])
    report, keys = parse_baseline_dir(dir_path=str(tmp_path))
    assert report['summary']['files_skipped'] == 1
    assert keys == {}


def test_import_idempotent(db, clean, tmp_path):
    """连续导入两次 → 条目数不变（幂等，spec 5.1.8）"""
    _xlsx(tmp_path, 'STX-EU07.xlsx',
          [['ASIN', 'SKU'], ['B0STX1', 'SKU-A'], ['B0STX2', 'SKU-B']])
    r1 = import_baseline(db, dir_path=str(tmp_path), dry_run=False)
    assert r1['summary']['items_inserted'] == 2
    n1 = db.query(MappingBaselineItem).count()
    r2 = import_baseline(db, dir_path=str(tmp_path), dry_run=False)
    assert r2['summary']['items_inserted'] == 0
    assert db.query(MappingBaselineItem).count() == n1


# ================= 批量确认（9 用例） =================

def test_bulk_confirm_exact_match(db, clean):
    """完全匹配转正：normal + confirmed_by/at 非空 + 主档同步（spec 5.2.1）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0STX1', 'SKU-A')])
    product = StoreProduct(store_id=sid, asin='B0STX1', status='pending')
    db.add(product)
    db.commit()
    db.refresh(product)
    m = _add_pending(db, sid, 'SKU-A', 'B0STX1', product=product)

    rpt = bulk_confirm(db, store=STX, dry_run=False,
                       operator_user_id=1, operator_username='admin')
    assert rpt['summary']['confirmed'] == 1
    m.refresh_from_db() if hasattr(m, 'refresh_from_db') else db.refresh(m)
    assert m.status == 'normal'
    assert m.confirmed_by == 1
    assert m.confirmed_at is not None
    db.refresh(product)
    assert product.status == 'normal'


def test_bulk_confirm_dry_run_zero_write(db, clean):
    """dry-run 零写库：状态不变、报告含三类计数、不落报告（spec 5.2.2）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0STX1', 'SKU-A'), ('B0STX2', 'SKU-X2')])
    _add_pending(db, sid, 'SKU-A', 'B0STX1')           # 可确认
    _add_pending(db, sid, 'SKU-ZZ', 'B0ZZZ')           # 未覆盖
    _add_pending(db, sid, 'SKU-X2', 'B0OTHER')         # 冲突（同SKU不同ASIN）
    before = db.query(MappingOpReport).count()

    rpt = bulk_confirm(db, store=STX, dry_run=True)
    s = rpt['summary']
    assert s['total_pending'] == 3
    assert s['confirmed'] == 1 and s['uncovered'] == 1 and s['conflict'] == 1
    assert rpt['report_id'] is None
    assert db.query(MappingOpReport).count() == before           # 不落报告
    assert db.query(StoreSkuMapping).filter(
        StoreSkuMapping.store_id == sid,
        StoreSkuMapping.status == 'normal').count() == 0          # 零写库


def test_bulk_confirm_uncovered_kept(db, clean):
    """基准外 pending 保留 pending 并进未覆盖清单（spec 5.2.3）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0STX1', 'SKU-A')])
    m = _add_pending(db, sid, 'SKU-OUT', 'B0OUT')
    rpt = bulk_confirm(db, store=STX, dry_run=False)
    assert rpt['summary']['uncovered'] == 1
    assert rpt['details']['uncovered'][0]['sku'] == 'SKU-OUT'
    db.refresh(m)
    assert m.status == 'pending'


def test_bulk_confirm_conflict_not_confirmed(db, clean):
    """冲突（同 SKU 不同 ASIN）不自动确认，清单携带双方值（spec 5.2.4a）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0STX1', 'SKU-A'), ('B0STX9', 'SKU-A9')])
    m = _add_pending(db, sid, 'SKU-A', 'B0OTHER')     # 基准 SKU-A → B0STX1，pending 是 B0OTHER
    rpt = bulk_confirm(db, store=STX, dry_run=False)
    assert rpt['summary']['conflict'] == 1
    c = rpt['details']['conflict'][0]
    assert c['conflict_type'] == 'same_sku_diff_asin'
    assert c['baseline_asin'] == ['B0STX1']
    db.refresh(m)
    assert m.status == 'pending'


def test_bulk_confirm_drift_redline(db, clean):
    """漂移红线：库内已有 normal + 新 pending 指向异 ASIN → sku_drift 冲突（spec 5.2.5）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0DRIFT2', 'SKU-D1')])
    _add_normal(db, sid, 'SKU-D1', 'B0DRIFT1')        # 既有 normal
    m = _add_pending(db, sid, 'SKU-D1', 'B0DRIFT2')   # 基准支持的新 ASIN
    rpt = bulk_confirm(db, store=STX, dry_run=False)
    assert rpt['summary']['conflict'] == 1
    assert rpt['details']['conflict'][0]['conflict_type'] == 'sku_drift'
    db.refresh(m)
    assert m.status == 'pending'                        # 不覆盖既有 normal
    normals = db.query(StoreSkuMapping).filter(
        StoreSkuMapping.store_id == sid, StoreSkuMapping.sku == 'SKU-D1',
        StoreSkuMapping.status == 'normal').count()
    assert normals == 1                                 # 无双 normal


def test_bulk_confirm_excludes_abnormal(db, clean):
    """abnormal 记录不参与批量确认（spec 5.2.6）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0AB1', 'SKU-AB')])
    m = StoreSkuMapping(store_id=sid, sku='SKU-AB', asin='B0AB1', status='abnormal',
                        note='异常池')
    db.add(m)
    db.commit()
    db.refresh(m)
    rpt = bulk_confirm(db, store=STX, dry_run=False)
    assert rpt['summary']['total_pending'] == 0        # abnormal 不计入
    db.refresh(m)
    assert m.status == 'abnormal'


def test_bulk_confirm_no_baseline_400(db, clean, client, admin_headers):
    """未导入基准即执行 → 400 且无状态变更（spec 5.2.3 场景 1）"""
    sid = _sid(db, STX)
    m = _add_pending(db, sid, 'SKU-A', 'B0STX1')       # 有 pending 但无基准
    r = client.post('/api/v2/admin/mapping-review/bulk-confirm',
                    headers=admin_headers, json={'dry_run': True})
    assert r.status_code == 400
    assert '基准' in r.json()['detail']
    db.refresh(m)
    assert m.status == 'pending'


def test_bulk_confirm_idempotent_rerun(db, clean):
    """正式执行后重跑：仅处理剩余 pending（幂等续跑，spec 4.2.2）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0STX1', 'SKU-A'), ('B0STX2', 'SKU-B')])
    _add_pending(db, sid, 'SKU-A', 'B0STX1')
    _add_pending(db, sid, 'SKU-ZZ', 'B0ZZZ')           # 未覆盖，保持 pending
    r1 = bulk_confirm(db, store=STX, dry_run=False)
    assert r1['summary']['confirmed'] == 1
    r2 = bulk_confirm(db, store=STX, dry_run=False)
    assert r2['summary']['total_pending'] == 1         # 只剩未覆盖项
    assert r2['summary']['confirmed'] == 0
    assert db.query(StoreSkuMapping).filter(
        StoreSkuMapping.store_id == sid,
        StoreSkuMapping.status == 'normal').count() == 1   # 不重复确认


def test_bulk_confirm_skipped_already_normal(db, clean):
    """库内已有同 (store, sku, asin) normal → pending 计 skipped 不确认（幂等跳过）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0SK1', 'SKU-SK')])
    _add_normal(db, sid, 'SKU-SK', 'B0SK1')
    m = _add_pending(db, sid, 'SKU-SK', 'B0SK1')       # 同元组 pending（历史残留形态）
    rpt = bulk_confirm(db, store=STX, dry_run=False)
    assert rpt['summary']['skipped_already_normal'] == 1
    assert rpt['summary']['confirmed'] == 0
    db.refresh(m)
    assert m.status == 'pending'


# ================= 报告与审计（3 用例） =================

def test_report_invariant(db, clean):
    """计数自洽：总数 = 各分类之和，清单行数与计数一致（spec 5.4.1）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0STX1', 'SKU-A')])
    _add_pending(db, sid, 'SKU-A', 'B0STX1')
    _add_pending(db, sid, 'SKU-ZZ', 'B0ZZZ')
    _add_pending(db, sid, 'SKU-A', 'B0OTHER')
    rpt = bulk_confirm(db, store=STX, dry_run=False)
    s = rpt['summary']
    assert s['total_pending'] == (s['confirmed'] + s['uncovered'] + s['conflict']
                                  + s['failed'] + s['skipped_already_normal'])
    assert len(rpt['details']['confirmed']) == s['confirmed']
    assert len(rpt['details']['uncovered']) == s['uncovered']
    assert len(rpt['details']['conflict']) == s['conflict']


def test_report_queryable_and_exportable(db, clean, client, admin_headers):
    """报告可查可导出：详情同构 + xlsx 可打开（spec 5.4.2）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0STX1', 'SKU-A')])
    _add_pending(db, sid, 'SKU-A', 'B0STX1')
    _add_pending(db, sid, 'SKU-ZZ', 'B0ZZZ')
    rpt = bulk_confirm(db, store=STX, dry_run=False)
    rid = rpt['report_id']
    assert rid

    detail = client.get(f'/api/v2/admin/mapping-review/reports/{rid}',
                        headers=admin_headers).json()
    assert detail['op_type'] == 'bulk_confirm'
    assert detail['summary']['confirmed'] == 1
    assert detail['details']['confirmed'][0]['sku'] == 'SKU-A'

    r = client.get(f'/api/v2/admin/mapping-review/reports/{rid}/export',
                   headers=admin_headers, params={'section': 'uncovered'})
    assert r.status_code == 200
    assert 'spreadsheetml' in r.headers['content-type']
    import io
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][0] == '店铺'
    assert any('SKU-ZZ' in str(row) for row in rows)

    lst = client.get('/api/v2/admin/mapping-review/reports',
                     headers=admin_headers, params={'op_type': 'bulk_confirm'}).json()
    assert any(x['report_id'] == rid for x in lst['items'])


def test_audit_trail(db, clean):
    """审计留痕：operation_log 有 action + 转正记录确认人非空（spec 5.4.3）"""
    from app.models import OperationLog
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0STX1', 'SKU-A')])
    m = _add_pending(db, sid, 'SKU-A', 'B0STX1')
    bulk_confirm(db, store=STX, dry_run=False, operator_user_id=1, operator_username='admin')
    log = db.query(OperationLog).filter(
        OperationLog.action == 'mapping_bulk_confirm',
        OperationLog.username == 'admin').order_by(OperationLog.id.desc()).first()
    assert log is not None
    assert log.target == 'STX'
    assert '确认 1' in (log.detail or '')
    db.refresh(m)
    assert m.confirmed_by == 1


# ================= 接口层与兼容（2 用例） =================

def test_legacy_decide_compat(db, clean, client, admin_headers):
    """既有单条 decide 接口行为不变：confirm 转正 + 漂移 409（硬约束 3）"""
    sid = _sid(db, STX)
    m = _add_pending(db, sid, 'SKU-LEG', 'B0LEG1')
    r = client.post(f'/api/v2/admin/mappings/{m.id}/decide',
                    headers=admin_headers, json={'action': 'confirm'})
    assert r.status_code == 200
    db.rollback()   # REPEATABLE READ：释放旧快照才能看到 API 另一 session 的提交
    db.refresh(m)
    assert m.status == 'normal'

    _add_normal(db, sid, 'SKU-LEG2', 'B0LEG2')
    m2 = _add_pending(db, sid, 'SKU-LEG2', 'B0LEG9')
    r2 = client.post(f'/api/v2/admin/mappings/{m2.id}/decide',
                     headers=admin_headers, json={'action': 'confirm'})
    assert r2.status_code == 409


def test_bulk_confirm_permission_403(db, clean, client, limited):
    """非管理员调用批量确认 → 403（spec 4.3.1）"""
    r = client.post('/api/v2/admin/mapping-review/bulk-confirm',
                    headers=limited['headers'], json={'dry_run': True})
    assert r.status_code == 403