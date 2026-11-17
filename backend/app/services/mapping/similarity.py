"""amzn.gr.* 替换件 SKU 相似度归一 — 唯一实现（mapping_review_enhance 组2）

形态：`amzn.gr.` + SKU 码段 + `-` + 亚马逊随机串 + 站点后缀（-GD/-VG/-LN）
实测样例：
    amzn.gr.80-8AQW-0XIM-uD_11QZvu1by0ziX-GD   → 80-8AQW-0XIM
    amzn.gr.LH-PO7F-N6F7-T2usJgtKUZUl5nOR-VG   → LH-PO7F-N6F7
    amzn.gr.64-GLQ2-BL3L-Eh4SD2l7eKSLBBrB-VG   → 64-GLQ2-BL3L
    amzn.gr.B-ZR280-16-B-<随机串>-VG           → B-ZR280-16-B

★ 禁止以固定段数/固定长度作为唯一解析方式（旧 AMZN_GR_SIMPLE 仅支持三段，四段形态会漏解）；
  改用「随机串特征定位」：SKU 码段各段为全大写+数字（长度 2~6），随机串必含小写字母或下划线。
★ 归一目标必须落在该店铺主 SKU 集合内，否则不归类（防误归并）。
★ 性能：主 SKU 集合与单行结果都经 replacements_cache 双层缓存；无 amzn.gr. 前缀立即早退。
"""
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import MappingBaselineItem, SkuReplacement, Store, StoreSkuMapping

AMZN_GR_PREFIX = 'amzn.gr.'
# 站点后缀白名单（实测）；形态判定（2~4 位纯大写字母）作为双保险
SITE_SUFFIX_WHITELIST = {'GD', 'VG', 'LN'}
# replacements_cache 中以该哨兵 key 承载「店铺主 SKU 集合」缓存：key = (store_id, _MAIN_SKUS_KEY)
_MAIN_SKUS_KEY = '__mapping_main_skus__'


def is_amzn_gr(sku: str) -> bool:
    """是否 amzn.gr.* 形态（大小写不敏感）"""
    return bool(sku) and sku.strip().lower().startswith(AMZN_GR_PREFIX)


def _is_random_token(tok: str) -> bool:
    """随机串特征段：长度 >= 8 且（含小写字母 或 含下划线）"""
    return len(tok) >= 8 and (any(c.islower() for c in tok) or '_' in tok)


def strip_site_suffix(body: str) -> str:
    """剥离站点后缀（-GD/-VG/-LN 白名单优先，其次 2~4 位纯大写字母形态判定）"""
    parts = body.split('-')
    if len(parts) >= 2:
        last = parts[-1]
        if last.upper() in SITE_SUFFIX_WHITELIST or (
                2 <= len(last) <= 4 and last.isalpha() and last.isupper()):
            return '-'.join(parts[:-1])
    return body


def extract_sku_code(raw: str) -> str:
    """从 amzn.gr.* 原始值提取 SKU 码段（剥离前缀与随机串/站点后缀）

    未找到随机串或首段即随机串时，返回剥离站点后缀后的整段（兜底）。
    """
    sku = (raw or '').strip()
    if not is_amzn_gr(sku):
        return sku
    body = sku[len(AMZN_GR_PREFIX):]
    parts = body.split('-')
    idx = -1
    for i in range(len(parts) - 1, -1, -1):
        if _is_random_token(parts[i]):
            idx = i
            break
    if idx > 0:
        return '-'.join(parts[:idx])
    # idx == 0（首段即随机串，无码段）或 idx == -1（无随机串特征）→ 兜底整段
    return strip_site_suffix(body)


def score(a: str, b: str) -> float:
    """相似度打分（0~1，大小写不敏感）— 标准库 SequenceMatcher，零新增依赖"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def pick_unique(scored: list, threshold: float, eps: float) -> tuple[Optional[str], str]:
    """唯一命中裁决

    scored: [(sku, score), ...]
    返回 (命中SKU | None, 原因)：ok / no_candidate / below_threshold / ambiguous
    """
    if not scored:
        return None, 'no_candidate'
    ranked = sorted(scored, key=lambda x: x[1], reverse=True)
    top = ranked[0][1]
    if top < threshold:
        return None, 'below_threshold'
    ties = [s for s in ranked if top - s[1] < eps]
    if len(ties) > 1:
        return None, 'ambiguous'
    return ranked[0][0], 'ok'


def main_skus(db: Session, store_id: int, cache: dict | None = None) -> list[str]:
    """店铺主 SKU 集合 = 基准条目 SKU ∪ status='normal' 映射 SKU

    ★ 剔除自身为 amzn.gr.* 的污染项（否则会归并到另一个替换件）。
    结果按 (store_id) 缓存于 cache，避免导入热路径重复查库。
    """
    if cache is not None:
        hit = cache.get((store_id, _MAIN_SKUS_KEY))
        if hit is not None:
            return hit
    skus: set[str] = set()
    for (sku,) in db.query(MappingBaselineItem.sku).filter(
            MappingBaselineItem.store_id == store_id).all():
        if sku and not is_amzn_gr(sku):
            skus.add(sku)
    for (sku,) in db.query(StoreSkuMapping.sku).filter(
            StoreSkuMapping.store_id == store_id,
            StoreSkuMapping.status == 'normal').all():
        if sku and not is_amzn_gr(sku):
            skus.add(sku)
    result = sorted(skus)
    if cache is not None:
        cache[(store_id, _MAIN_SKUS_KEY)] = result
    return result


def _upsert_replacement(db: Session, store_id: int, raw_sku: str, parent_sku: str) -> None:
    """幂等写入归并关系（已存在且父 SKU 相同则跳过；不同则修正并留痕）

    flush 确保同事务内后续查询可见（项目 SessionLocal 为 autoflush=False）。
    """
    existing = db.query(SkuReplacement).filter(
        SkuReplacement.store_id == store_id,
        SkuReplacement.replacement_sku == raw_sku,
    ).first()
    if existing is None:
        db.add(SkuReplacement(store_id=store_id, replacement_sku=raw_sku,
                              parent_sku=parent_sku))
        db.flush()
        return
    if existing.parent_sku == parent_sku:
        return
    existing.prev_parent_sku = existing.parent_sku
    existing.parent_sku = parent_sku
    existing.updated_at = datetime.now()
    db.flush()


def resolve_code_pure(db: Session, raw_sku: str, store_id: int,
                      cache: dict | None = None) -> tuple[str, dict]:
    """纯计算归一（零写库）：非 amzn.gr.* 原样返回；精确命中优先 → 相似度唯一命中 → 保留原值

    ★ 供 realign 的 dry-run 使用（禁止任何写副作用）；写归并记录请用 resolve_amzn_gr。
    返回 (归一后SKU | 原值, 判定明细)：{code, matched(exact/similarity/cache), reason, candidates}
    """
    sku = (raw_sku or '').strip()
    if not is_amzn_gr(sku):
        return sku, {'code': sku, 'matched': None, 'reason': 'not_amzn_gr', 'candidates': []}

    if cache is not None:
        hit = cache.get((store_id, sku))
        if hit is not None:
            # 命中结果缓存（既可能是归一后 SKU，也可能是保留的原值）
            return hit, {'code': extract_sku_code(sku), 'matched': 'cache',
                         'reason': 'cache_hit', 'candidates': []}

    code = extract_sku_code(sku)
    skus = main_skus(db, store_id, cache)
    if not skus:
        detail = {'code': code, 'matched': None, 'reason': 'empty_main_skus', 'candidates': []}
        if cache is not None:
            cache[(store_id, sku)] = sku
        return sku, detail

    # ① 精确命中（大小写不敏感）优先，跳过相似度
    lower_map = {s.lower(): s for s in skus}
    exact = lower_map.get(code.lower())
    if exact is not None:
        detail = {'code': code, 'matched': 'exact', 'reason': 'ok', 'candidates': [exact]}
        if cache is not None:
            cache[(store_id, sku)] = exact
        return exact, detail

    # ② 相似度唯一命中
    scored = [(s, score(code, s)) for s in skus]
    threshold = settings.MAPPING_SIMILARITY_THRESHOLD
    eps = 0.02
    picked, reason = pick_unique(scored, threshold, eps)
    top_candidates = sorted(scored, key=lambda x: x[1], reverse=True)[:5]
    detail = {
        'code': code,
        'matched': 'similarity' if picked else None,
        'reason': reason,
        'candidates': [{'sku': s, 'score': round(sc, 4)} for s, sc in top_candidates],
    }
    if picked:
        if cache is not None:
            cache[(store_id, sku)] = picked
        return picked, detail

    # ③ 无法唯一归类 → 保留原值（不写归并记录）
    if cache is not None:
        cache[(store_id, sku)] = sku
    return sku, detail


def resolve_amzn_gr(db: Session, raw_sku: str, store_id: int,
                    cache: dict | None = None) -> tuple[str, dict]:
    """单行归一（含写归并记录）：用于导入链路（经 normalize_sku 委托）与人工归类

    与 resolve_code_pure 的区别：归类成功时幂等写入 sku_replacement。
    """
    raw = (raw_sku or '').strip()
    parent, detail = resolve_code_pure(db, raw, store_id, cache)
    if detail.get('reason') == 'ok' and parent != raw and is_amzn_gr(raw):
        _upsert_replacement(db, store_id, raw, parent)
    return parent, detail


def realign_replacements(db: Session, store_id: Optional[int] = None,
                         dry_run: bool = True) -> dict:
    """存量重归一：按店铺分片扫描 sku_replacement 与新逻辑重算

    分类（互斥，scanned = unchanged + changed + kept_raw + invalid_target）：
      · unchanged      目标与新逻辑一致
      · invalid_target 旧目标非法（amzn.gr.* 形态 / 不在本店铺主 SKU 集合内）→ 修正
      · changed        旧目标合法但与新逻辑不一致 → 修正
      · kept_raw       新逻辑无法唯一归类 → 保留原值（输出候选）
    dry_run=True 只读不写；否则写 parent_sku + prev_parent_sku + updated_at。
    """
    q = db.query(SkuReplacement)
    if store_id is not None:
        q = q.filter(SkuReplacement.store_id == store_id)
    rows = q.all()

    scanned = unchanged = changed = kept_raw = invalid_target = 0
    changed_detail, unclassified = [], []
    stores: dict[int, str] = {}
    cache: dict = {}

    for row in rows:
        scanned += 1
        sid = row.store_id
        old_parent = row.parent_sku or ''
        new_parent, detail = resolve_code_pure(db, row.replacement_sku, sid, cache)
        skus = main_skus(db, sid, cache)
        old_invalid = is_amzn_gr(old_parent) or (old_parent not in skus)
        store_code = stores.get(sid)
        if store_code is None:
            store_code = _store_code(db, sid)
            stores[sid] = store_code

        if new_parent == old_parent:
            unchanged += 1
            continue
        if new_parent == row.replacement_sku:
            # 新逻辑无法归类 → 保留原值（不改库），输出候选供人工指定
            kept_raw += 1
            unclassified.append({
                'store_code': store_code, 'replacement_sku': row.replacement_sku,
                'candidates': detail.get('candidates', []), 'reason': detail.get('reason'),
            })
            continue
        # 可归类且与新逻辑不一致 → 修正
        if old_invalid:
            invalid_target += 1
        else:
            changed += 1
        changed_detail.append({
            'store_code': store_code, 'replacement_sku': row.replacement_sku,
            'old_parent_sku': old_parent, 'new_parent_sku': new_parent,
        })
        if not dry_run:
            row.prev_parent_sku = old_parent
            row.parent_sku = new_parent
            row.updated_at = datetime.now()

    if not dry_run:
        db.commit()

    return {
        'op_type': 'replacement_realign',
        'dry_run': dry_run,
        'store_scope': 'ALL' if store_id is None else (_store_code(db, store_id)),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'summary': {
            'scanned': scanned, 'unchanged': unchanged, 'changed': changed,
            'kept_raw': kept_raw, 'invalid_target': invalid_target,
        },
        'details': {'changed': changed_detail, 'unclassified': unclassified},
        'warnings': [],
    }


def _store_code(db: Session, store_id: int) -> str:
    """店铺 id → 代码（报告展示用；缺失回退为 id 字符串）"""

    st = db.query(Store).filter(Store.id == store_id).first()
    return st.store_code if st else f'#{store_id}'