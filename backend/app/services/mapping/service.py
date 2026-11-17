"""ASIN/SKU 三元组映射服务 — 5 类场景判定（文档 MM-02 5.3，已定稿）

场景表：
| 单 ASIN 多 SKU   | 正常，多条映射                        | normal   |
| 单 SKU 多 ASIN   | 拦截入异常池，旧映射保留不动            | abnormal |
| 新 ASIN + 新 SKU | 自动创建，待确认新品                    | pending  |
| ASIN 命中+SKU 新 | 该 ASIN 下新增 SKU                     | pending  |
| SKU 命中+ASIN 新 | 拦截入异常池，不覆盖原 ASIN，旧映射保留  | abnormal |
| 待确认产品       | 照常参与业务统计，列表标黄              | -        |

SKU 不允许跨 ASIN 漂移：UNIQUE(store_id, sku) 仅对 status='normal' 生效（应用层保证）。
"""
import re
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from app.models import StoreProduct, StoreSkuMapping, SkuReplacement, Store

AMZN_GR_PATTERN = re.compile(r'^amzn\.gr\.(.+?)(?:-.{8,}-.{8,}(?:-LN)?)?$', re.IGNORECASE)
# 旧系统 _extract_real_sku 实测格式：amzn.gr.LH-PO7F-N6F7-xU2UoIigxi3373PV-LN → LH-PO7F-N6F7
AMZN_GR_SIMPLE = re.compile(r'^amzn\.gr\.([A-Z0-9]{2}-[A-Z0-9]{4}-[A-Z0-9]{4})', re.IGNORECASE)


def normalize_sku(raw_sku: str, store_id: int, db: Session, replacements_cache: dict | None = None) -> str:
    """amzn.gr.* 归一到父 SKU；记录归并关系到 sku_replacement；原始值保留在原始层。
    归不出的（无法解析父SKU）原样返回。

    ★ 组2 改造：签名与调用方（pipeline.py:526）不变。
      · 旧 AMZN_GR_SIMPLE 三段正则保留为**快速路径**，命中即直接采用；
      · 未速配时委托 similarity.resolve_amzn_gr（精确匹配优先 → 相似度唯一命中 → 保留原值）。
    """
    sku = (raw_sku or '').strip()
    if not sku or not sku.lower().startswith('amzn.gr.'):
        return sku
    cache = replacements_cache if replacements_cache is not None else {}
    cache_key = (store_id, sku)
    if cache_key in cache:
        return cache[cache_key]

    # 快速路径：旧三段正则命中且不依赖主 SKU 集合，直接采用
    m = AMZN_GR_SIMPLE.match(sku)
    if m:
        parent = m.group(1)
        existing = db.query(SkuReplacement).filter(
            SkuReplacement.store_id == store_id,
            SkuReplacement.replacement_sku == sku,
        ).first()
        if not existing:
            db.add(SkuReplacement(store_id=store_id, replacement_sku=sku, parent_sku=parent))
            db.flush()   # autoflush=False：同事务内后续查询需可见
        cache[cache_key] = parent
        return parent

    # 慢路径：随机串特征定位 + 相似度唯一命中（组2 修正）
    from app.services.mapping.similarity import resolve_amzn_gr
    parent, _detail = resolve_amzn_gr(db, sku, store_id, replacements_cache)
    return parent


class MappingResult:
    """单行映射判定结果"""
    def __init__(self, product: StoreProduct, mapping: StoreSkuMapping, scenario: str, is_new: bool):
        """保存单行映射判定结果"""
        self.product = product          # 店铺产品主档
        self.mapping = mapping          # SKU-ASIN 映射
        self.scenario = scenario        # hit / multi_sku / new_both / asin_hit_sku_new / sku_hit_asin_new
        self.is_new = is_new            # 是否本次导入新建


def resolve_mapping(db: Session, store_id: int, sku: str, asin: str,
                    title: str = "") -> MappingResult:
    """导入时对一行 (store, sku, asin) 做命中/新增/异常判定

    判定流程（文档 MM-02 5.3）：
      查 (store, sku) normal 映射 →
        无 → 查 asin 主档 →
               无 → 场景3 新ASIN+新SKU → 创建(pending)
               有 → 场景4 ASIN命中+SKU新 → 创建映射(pending)
        有 → ASIN 一致 → 命中(normal)
             不一致 → 场景5 SKU命中+ASIN新 → 异常池(abnormal pending)，旧映射保留不动
    """
    sku = (sku or '').strip()
    asin = (asin or '').strip()
    if not asin and not sku:
        raise ValueError("sku 与 asin 均为空")

    # 1. 查该 SKU 的正常映射
    existing = db.query(StoreSkuMapping).filter(
        StoreSkuMapping.store_id == store_id,
        StoreSkuMapping.sku == sku,
        StoreSkuMapping.status == 'normal',
    ).first()

    if existing:
        if existing.asin == asin:
            # 命中 — 顺带回填 product
            product = db.query(StoreProduct).filter(StoreProduct.id == existing.store_product_id).first()
            if not product:
                product = db.query(StoreProduct).filter(
                    StoreProduct.store_id == store_id, StoreProduct.asin == asin).first()
            return MappingResult(product, existing, 'hit', False)
        # 场景5：SKU 命中 + ASIN 新 → 拦截入异常池，旧映射保留不动
        # 创建/更新一条 abnormal 待审记录（同 store+sku+asin 幂等）
        abnormal = db.query(StoreSkuMapping).filter(
            StoreSkuMapping.store_id == store_id,
            StoreSkuMapping.sku == sku,
            StoreSkuMapping.asin == asin,
            StoreSkuMapping.status == 'abnormal',
        ).first()
        if not abnormal:
            abnormal = StoreSkuMapping(store_id=store_id, sku=sku, asin=asin, status='abnormal',
                                       note=f"SKU原绑ASIN {existing.asin}，本行出现新ASIN，挂起人工审核")
            db.add(abnormal)
            db.flush()
        product = db.query(StoreProduct).filter(
            StoreProduct.store_id == store_id, StoreProduct.asin == asin).first()
        return MappingResult(product, abnormal, 'sku_hit_asin_new', True)

    # 2. SKU 无正常映射 → 查 ASIN 主档（含 pending/abnormal 映射的 SKU 也要防止重复建）
    product = db.query(StoreProduct).filter(
        StoreProduct.store_id == store_id, StoreProduct.asin == asin).first()

    if product:
        # 场景4：ASIN 命中 + SKU 新 → 该 ASIN 下新增 SKU
        # 同 (store, sku, asin) 的 pending 映射已存在则复用
        pending = db.query(StoreSkuMapping).filter(
            StoreSkuMapping.store_id == store_id,
            StoreSkuMapping.sku == sku,
            StoreSkuMapping.asin == asin,
        ).first()
        if pending:
            return MappingResult(product, pending, 'asin_hit_sku_new', False)
        mapping = StoreSkuMapping(store_id=store_id, sku=sku, asin=asin,
                                  store_product_id=product.id, status='pending')
        db.add(mapping)
        db.flush()
        return MappingResult(product, mapping, 'asin_hit_sku_new', True)

    # 场景3：新 ASIN + 新 SKU → 创建主档(pending) + 映射(pending)
    product = StoreProduct(store_id=store_id, asin=asin, title=title, status='pending')
    db.add(product)
    db.flush()
    mapping = StoreSkuMapping(store_id=store_id, sku=sku, asin=asin,
                              store_product_id=product.id, status='pending')
    db.add(mapping)
    db.flush()
    return MappingResult(product, mapping, 'new_both', True)
