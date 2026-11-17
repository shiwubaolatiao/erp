# -*- coding: utf-8 -*-
"""amzn.gr.* 替换件相似度归一测试（mapping_review_enhance 组2.8）

覆盖 design 2.7.2 全部 16 用例：
  码段提取（三段×2 / 四段）、精确匹配优先、相似度唯一命中、无候选保留、多等价候选保留、
  归并记录幂等、目标合法性、集合为空、amzn.gr. 污染项剔除、存量重归一（变更留痕/失败重算/幂等）、
  归一不写成本表与 ODS。

★ 全部用 ORM 独立构造数据（STX/WEL 店铺，不碰 e2e fixture 的 LMG/MGT 数据）。
"""
import pytest

from app.models import (
    MappingBaselineItem, SkuReplacement, Store, StoreProduct, StoreSkuMapping,
)
from app.services.mapping.similarity import (
    extract_sku_code, main_skus, realign_replacements, resolve_amzn_gr, score,
)

STX = 'STX'
WEL = 'WEL'


@pytest.fixture(scope='module')
def db(client):
    """会话级 TestClient 已建表与种子店铺；本模块用独立 ORM 会话造数"""
    from app.core.database import SessionLocal
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def clean(db):
    """用例前后清理 STX/WEL 的映射相关表，避免跨用例污染"""
    _purge(db)
    yield
    _purge(db)


def _sid(db, code: str) -> int:
    return db.query(Store).filter(Store.store_code == code).first().id


def _purge(db):
    # 丢弃上一轮可能残留的 ORM dirty 状态，避免 bulk delete 后 flush 报 StaleDataError
    db.rollback()
    ids = [st.id for st in db.query(Store).filter(Store.store_code.in_((STX, WEL))).all()]
    if not ids:
        return
    db.query(MappingBaselineItem).filter(MappingBaselineItem.store_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(SkuReplacement).filter(SkuReplacement.store_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(StoreSkuMapping).filter(StoreSkuMapping.store_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(StoreProduct).filter(StoreProduct.store_id.in_(ids)).delete(
        synchronize_session=False)
    db.commit()


def _add_baseline(db, store_id: int, pairs, sheet='产品信息表'):
    """基准条目造数：pairs = [(asin, sku), ...]"""
    for asin, sku in pairs:
        db.add(MappingBaselineItem(store_id=store_id, asin=asin, sku=sku,
                                   source_file='t07.xlsx', source_sheet=sheet))
    db.commit()


# ================= 码段提取（4 用例） =================

def test_extract_code_three_segments():
    """三段形态 1：随机串含下划线"""
    assert extract_sku_code('amzn.gr.80-8AQW-0XIM-uD_11QZvu1by0ziX-GD') == '80-8AQW-0XIM'


def test_extract_code_three_segments_2():
    """三段形态 2：随机串为混合大小写"""
    assert extract_sku_code('amzn.gr.LH-PO7F-N6F7-T2usJgtKUZUl5nOR-VG') == 'LH-PO7F-N6F7'
    assert extract_sku_code('amzn.gr.64-GLQ2-BL3L-Eh4SD2l7eKSLBBrB-VG') == '64-GLQ2-BL3L'


def test_extract_code_four_segments():
    """四段形态（旧三段正则无法解析）"""
    assert extract_sku_code('amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD') == 'B-ZR280-16-B'


def test_extract_code_no_random_token_fallback():
    """无随机串特征 → 剥离站点后缀后整段兜底"""
    assert extract_sku_code('amzn.gr.LH-PO7F-N6F7-VG') == 'LH-PO7F-N6F7'


# ================= 单行归一（8 用例） =================

def test_non_amzn_gr_unchanged(db, clean):
    """普通 SKU 原样返回且无归并记录"""
    sid = _sid(db, STX)
    out, detail = resolve_amzn_gr(db, '9V-YWU1-A3EF', sid)
    assert out == '9V-YWU1-A3EF'
    assert detail['reason'] == 'not_amzn_gr'
    assert db.query(SkuReplacement).filter(SkuReplacement.store_id == sid).count() == 0


def test_exact_match_priority(db, clean):
    """精确命中（大小写不敏感）优先，跳过相似度"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B01MSZT36O', '80-8AQW-0XIM')])
    out, detail = resolve_amzn_gr(db, 'amzn.gr.80-8AQW-0XIM-uD_11QZvu1by0ziX-GD', sid)
    assert out == '80-8AQW-0XIM'
    assert detail['matched'] == 'exact'
    assert db.query(SkuReplacement).filter(
        SkuReplacement.store_id == sid,
        SkuReplacement.replacement_sku == 'amzn.gr.80-8AQW-0XIM-uD_11QZvu1by0ziX-GD',
        SkuReplacement.parent_sku == '80-8AQW-0XIM').count() == 1


def test_similarity_unique_hit(db, clean):
    """精确未命中 → 相似度唯一命中且达阈值 → 归类"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0AAAAAAAA', 'B-ZR280-16-C')])
    out, detail = resolve_amzn_gr(db, 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD', sid)
    assert out == 'B-ZR280-16-C'
    assert detail['matched'] == 'similarity'
    assert detail['reason'] == 'ok'


def test_no_candidate_below_threshold(db, clean):
    """全部候选低于阈值 → 保留原值"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0AAAAAAAA', '9V-YWU1-A3EF')])
    raw = 'amzn.gr.ZZ-9999-9999-uD_11QZvu1by0ziX-GD'
    out, detail = resolve_amzn_gr(db, raw, sid)
    assert out == raw
    assert detail['reason'] == 'below_threshold'
    assert db.query(SkuReplacement).filter(SkuReplacement.store_id == sid).count() == 0


def test_ambiguous_kept(db, clean):
    """两等价候选 → 保留原值（不归类）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0AAAAAAAA', 'B-ZR280-16-C'), ('B0BBBBBBBB', 'B-ZR280-16-D')])
    raw = 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD'
    out, detail = resolve_amzn_gr(db, raw, sid)
    assert out == raw
    assert detail['reason'] == 'ambiguous'


def test_empty_main_skus(db, clean):
    """店铺无主 SKU 集合 → 保留原值并标注原因"""
    sid = _sid(db, WEL)
    raw = 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD'
    out, detail = resolve_amzn_gr(db, raw, sid)
    assert out == raw
    assert detail['reason'] == 'empty_main_skus'
    assert main_skus(db, sid) == []


def test_amzn_gr_pollution_excluded(db, clean):
    """集合含 amzn.gr.* 污染项 → 剔除后再判（不归并到替换件）"""
    sid = _sid(db, WEL)
    _add_baseline(db, sid, [('B0AAAAAAAA', 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD')])
    assert main_skus(db, sid) == []
    raw = 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD'
    out, detail = resolve_amzn_gr(db, raw, sid)
    assert out == raw
    assert detail['reason'] == 'empty_main_skus'


def test_replacement_idempotent(db, clean):
    """重复归并不产生重复记录（唯一键 + 幂等 upsert）"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B01MSZT36O', '80-8AQW-0XIM')])
    raw = 'amzn.gr.80-8AQW-0XIM-uD_11QZvu1by0ziX-GD'
    resolve_amzn_gr(db, raw, sid)
    db.commit()
    resolve_amzn_gr(db, raw, sid)
    db.commit()
    assert db.query(SkuReplacement).filter(
        SkuReplacement.store_id == sid,
        SkuReplacement.replacement_sku == raw).count() == 1


# ================= 存量重归一（3 用例） =================

def test_realign_changed_trace(db, clean):
    """变更留痕：旧目标合法（在主 SKU 集合内）但与新逻辑不一致 → 修正并记录旧值"""
    sid = _sid(db, STX)
    # 集合同时含 B/C，raw 的码段精确命中 B；旧值 C 合法 → 计 changed（非 invalid_target）
    _add_baseline(db, sid, [('B0AAAAAAAA', 'B-ZR280-16-B'), ('B0BBBBBBBB', 'B-ZR280-16-C')])
    raw = 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD'
    db.add(SkuReplacement(store_id=sid, replacement_sku=raw, parent_sku='B-ZR280-16-C'))
    db.commit()

    rpt = realign_replacements(db, store_id=sid, dry_run=True)
    assert rpt['summary']['changed'] == 1
    assert rpt['summary']['invalid_target'] == 0
    assert rpt['details']['changed'][0]['old_parent_sku'] == 'B-ZR280-16-C'
    assert rpt['details']['changed'][0]['new_parent_sku'] == 'B-ZR280-16-B'
    # dry-run 不写库
    row = db.query(SkuReplacement).filter(SkuReplacement.replacement_sku == raw).first()
    assert row.parent_sku == 'B-ZR280-16-C'

    realign_replacements(db, store_id=sid, dry_run=False)
    row = db.query(SkuReplacement).filter(SkuReplacement.replacement_sku == raw).first()
    assert row.parent_sku == 'B-ZR280-16-B'
    assert row.prev_parent_sku == 'B-ZR280-16-C'
    assert row.updated_at is not None


def test_realign_invalid_target_recomputed(db, clean):
    """旧目标非法（amzn.gr.* 形态）→ 剔除后重判并计 invalid_target"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0AAAAAAAA', 'B-ZR280-16-C')])
    raw = 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD'
    db.add(SkuReplacement(store_id=sid, replacement_sku=raw,
                          parent_sku='amzn.gr.X-XXXX-XXXX-polluted'))
    db.commit()
    rpt = realign_replacements(db, store_id=sid, dry_run=True)
    assert rpt['summary']['invalid_target'] == 1
    assert rpt['details']['changed'][0]['new_parent_sku'] == 'B-ZR280-16-C'


def test_realign_idempotent(db, clean):
    """重跑幂等：第二次执行 changed=0、unchanged=1"""
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B0AAAAAAAA', 'B-ZR280-16-B'), ('B0BBBBBBBB', 'B-ZR280-16-C')])
    raw = 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD'
    db.add(SkuReplacement(store_id=sid, replacement_sku=raw, parent_sku='B-ZR280-16-C'))
    db.commit()
    realign_replacements(db, store_id=sid, dry_run=False)
    rpt2 = realign_replacements(db, store_id=sid, dry_run=False)
    assert rpt2['summary']['changed'] == 0
    assert rpt2['summary']['invalid_target'] == 0
    assert rpt2['summary']['unchanged'] == 1


# ================= 硬约束（1 用例） =================

def test_no_cost_or_ods_side_effect(db, clean):
    """归一不写 product_cost_history / raw_sheet（硬约束 1、spec 1.4.3）"""
    from conftest import sql_one
    before_cost = sql_one("SELECT COUNT(*) AS n FROM product_cost_history")['n']
    before_raw = sql_one("SELECT COUNT(*) AS n FROM raw_sheet")['n']
    sid = _sid(db, STX)
    _add_baseline(db, sid, [('B01MSZT36O', '80-8AQW-0XIM'), ('B0AAAAAAAA', 'B-ZR280-16-C')])
    resolve_amzn_gr(db, 'amzn.gr.80-8AQW-0XIM-uD_11QZvu1by0ziX-GD', sid)
    resolve_amzn_gr(db, 'amzn.gr.B-ZR280-16-B-uD_11QZvu1by0ziX-GD', sid)
    db.commit()
    after_cost = sql_one("SELECT COUNT(*) AS n FROM product_cost_history")['n']
    after_raw = sql_one("SELECT COUNT(*) AS n FROM raw_sheet")['n']
    assert after_cost == before_cost
    assert after_raw == before_raw


def test_score_sanity():
    """评分辅助校验：同串 1.0、异串低分、尾号差异低于阈值被挡"""
    assert score('ABC', 'abc') == 1.0
    assert score('WL-ZR280B-16', 'WL-ZR280B-20') < 0.85