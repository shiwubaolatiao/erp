"""请求级结果缓存 — TTL + LRU + 线程安全（spec 5.3.1 R6 / tasks 7.5）

设计要点（针对 P-07"模块级缓存无失效、跨会话污染"的根治手段）：
  · 键 = (接口, 参数序列化, 用户 id, 数据版本号)；
    用户 id 入键 → 权限隔离（A 用户缓存绝不被 B 用户命中）。
  · 数据版本号 = (max import_batch.id, max dws_profit_snapshot.id)：
    导入/核算完成 → 版本号天然变化 → 缓存自动失效，无需额外失效表。
    max(id) 走主键索引，代价毫秒级。
  · TTL 兜底（汇率修改等不改变版本号的写操作，靠 TTL 过期）；
  · 容量上限 LRU 淘汰 → 杜绝无界增长。
  · 仅覆盖纯读聚合接口（dashboard summary / fee-composition / trend）；
    写操作与管理类接口不缓存。

★ 禁止缓存不可 JSON 序列化对象（Decimal 等）——调用方返回 dict 前自行 float()。
"""
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ImportBatch, DwsProfitSnapshot


class TTLCache:
    """线程安全的 TTL + LRU 缓存（容量与过期双重上限）"""

    def __init__(self, max_entries: int, ttl_seconds: int):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: OrderedDict = OrderedDict()   # key -> (expires_at, value)
        self._lock = threading.Lock()

    def get(self, key) -> Optional[Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if now >= expires_at:
                del self._store[key]          # 惰性过期清除
                return None
            self._store.move_to_end(key)      # LRU 触碰
            return value

    def put(self, key, value: Any) -> None:
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            self._store[key] = (expires_at, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)   # 淘汰最久未使用

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


_cache = TTLCache(max_entries=settings.CACHE_MAX_ENTRIES, ttl_seconds=settings.CACHE_TTL_SECONDS)


def data_version(db: Session) -> tuple:
    """数据版本号 = (max import_batch.id, max dws_profit_snapshot.id)

    导入 / 核算落库 → 自增主键推进 → 版本号变化 → 旧键全部自然失效。
    """
    mi = db.query(sa_func.max(ImportBatch.id)).scalar() or 0
    ms = db.query(sa_func.max(DwsProfitSnapshot.id)).scalar() or 0
    return (mi, ms)


def cached_aggregate(endpoint: str, params: dict, user_id, db: Session,
                     compute: Callable[[], dict]) -> dict:
    """纯读聚合接口的缓存入口 — compute() 仅在 miss 时执行

    endpoint: 接口标识（如 'dashboard/summary'）
    params:   影响结果的查询参数（store/country/period_month/...）
    user_id:  用户 id（权限隔离维度，绝不能省）
    """
    version = data_version(db)
    key = (endpoint, json.dumps(params, sort_keys=True, default=str), user_id, version)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    value = compute()
    _cache.put(key, value)
    return value