"""最新快照读取 — 唯一实现（语义对齐 dashboard._latest_snapshots 并收口 D-04 翻倍风险）

★ 快照两种粒度并存（漏过滤会把"店铺汇总"和"站点明细"加起来，利润直接翻倍）：
    · 店铺级：country=''（该店铺全部站点汇总）
    · 站点级：country='UK' 等
  因此 **country 参数必传**（''=店铺级；None=调用错误，直接 TypeError），
  杜绝调用方漏加粒度过滤。

★ 同一 (店铺, 月份, 站点) 多版本时只取 version 最大的（与原实现逐字一致：
  去重键 = (store_id, period_month, country)）。
"""
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models import DwsProfitSnapshot


def latest_snapshots(db: Session, store_ids: Optional[Iterable[int]], country: str,
                     period: Optional[str] = None) -> list[DwsProfitSnapshot]:
    """取指定口径下的最新版本快照列表

    参数：
      store_ids: 店铺 id 集合（IN 批量）；None=不限店铺（管理员全店视角）
      country:   ★必传★；''=店铺级汇总快照；'UK' 等=站点级快照
      period:    'YYYY-MM' 精确月 / 'YYYY' 整年（前缀） / None=不限期间
    """
    if country is None:
        raise TypeError("latest_snapshots() 缺少必传参数 country（''=店铺级，禁止 None）"
                        "——漏加粒度过滤会导致利润翻倍")
    q = db.query(DwsProfitSnapshot)
    if store_ids is not None:
        ids = list(store_ids)
        q = q.filter(DwsProfitSnapshot.store_id.in_(ids or [-1]))
    q = q.filter(DwsProfitSnapshot.country == country)
    if period:
        if len(period) == 7:
            q = q.filter(DwsProfitSnapshot.period_month == period)
        else:
            q = q.filter(DwsProfitSnapshot.period_month.like(f"{period}-%"))
    # 同键多版本只取 version 最大（快照量级小——每店每月每站点一条，Python 去重
    # 与原实现行为逐字一致：同 version 时保留先出现者）
    best: dict = {}
    for s in q.all():
        k = (s.store_id, s.period_month, s.country or '')
        if k not in best or s.version > best[k].version:
            best[k] = s
    return list(best.values())