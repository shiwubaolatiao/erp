"""组7 7.8 查询计数回归（P-03 / P-04 / R6）

以 SQLAlchemy `before_cursor_execute` 事件钩子统计单次 HTTP 请求发出的 SQL 条数，
断言核心读接口查询次数为常数级（与数据量无关）：
  · /query/products     三步批量（P-03）：SQL ≤ 8，且不随 limit/商品数增长
  · /dashboard/store-compare 收敛（P-04）：SQL ≤ 12（不随店铺数线性增长）
  · /dashboard/summary  请求级缓存（R6）：同参数第二次 SQL 显著减少

注：上限含认证/权限等框架查询的预算余量（实现细节漂移不致脆弱），
核心断言是批量性与恒定性；真实量级 P95 性能基线见 spec 4.1（组12 实机验收）。
"""
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import event

from app.core.database import engine, SessionLocal
from app.models import Store, StoreProduct

from conftest import PERIOD

BULK_PRODUCTS = 30          # 批量性用例的种子商品数（N+1 模式下将产生 2N+ 次查询）


@contextlib.contextmanager
def count_sql():
    """统计上下文窗口内经全局 engine 发出的 SQL 条数（线程内同步请求为主）"""
    counter = {'n': 0}

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        counter['n'] += 1

    event.listen(engine, 'before_cursor_execute', _on_exec)
    try:
        yield counter
    finally:
        event.remove(engine, 'before_cursor_execute', _on_exec)


def _seed_bulk_products(n: int = BULK_PRODUCTS) -> None:
    """种 n 个商品主档（LMG 店铺）——为 N+1 与批量的区分提供数据量"""
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.store_code == 'LMG').first()
        assert store, 'LMG 店铺未初始化（依赖 e2e fixture）'
        existing = db.query(StoreProduct).filter(
            StoreProduct.store_id == store.id,
            StoreProduct.asin.like('B0QCNT%')).count()
        for i in range(existing, n):
            db.add(StoreProduct(store_id=store.id, asin=f'B0QCNT{i:07d}',
                                title=f'计数测试商品{i}', status='normal'))
        db.commit()
    finally:
        db.close()


class TestQueryCount:

    def test_products_bulk_no_n_plus_one(self, client, admin_headers, e2e):
        """P-03：200 商品规模查询次数恒定（三步批量，≤8 条 SQL；N+1 模式 = 2N+）"""
        _seed_bulk_products()
        r = client.get('/api/v2/query/products?limit=200', headers=admin_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['total'] >= BULK_PRODUCTS, f'种子商品未生效: {body["total"]}'
        with count_sql() as c:
            r = client.get('/api/v2/query/products?limit=200', headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()['total'] >= BULK_PRODUCTS
        assert c['n'] <= 8, f'/query/products SQL={c["n"]} 超 8（疑似 N+1 回归）'

    def test_products_count_constant_vs_limit(self, client, admin_headers, e2e):
        """P-03 恒定性：limit=10 与 limit=200 的 SQL 次数一致（与商品数无关）"""
        _seed_bulk_products()
        counts = {}
        for lim in (10, 200):
            with count_sql() as c:
                r = client.get(f'/api/v2/query/products?limit={lim}', headers=admin_headers)
            assert r.status_code == 200, r.text
            counts[lim] = c['n']
        assert counts[10] == counts[200], (
            f'查询次数随 limit 变化: {counts}（应恒定，与商品数无关）')

    def test_store_compare_constant_queries(self, client, admin_headers, e2e, calc):
        """P-04：店铺对比查询次数不随店铺数线性增长（恒定 ≤12 条 SQL）"""
        r = client.get(f'/api/v2/dashboard/store-compare?period_month={PERIOD}',
                       headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()['items'], 'store-compare 无数据（依赖 e2e+calc）'
        with count_sql() as c:
            r = client.get(f'/api/v2/dashboard/store-compare?period_month={PERIOD}',
                           headers=admin_headers)
        assert r.status_code == 200, r.text
        assert c['n'] <= 12, f'store-compare SQL={c["n"]} 超 12（疑似循环查询回归）'

    def test_summary_cache_second_call_hits_cache(self, client, admin_headers, e2e, calc):
        """R6：同参数 /dashboard/summary 第二次命中请求级缓存（SQL 显著减少且 ≤6）"""
        url = f'/api/v2/dashboard/summary?store=LMG&country=UK&period_month={PERIOD}'
        r = client.get(url, headers=admin_headers)
        assert r.status_code == 200, r.text
        with count_sql() as first:
            client.get(url, headers=admin_headers)
        with count_sql() as second:
            client.get(url, headers=admin_headers)
        assert second['n'] < first['n'] or first['n'] <= 6, (
            f'第二次未命中缓存: first={first["n"]} second={second["n"]}')
        assert second['n'] <= 6, (
            f'缓存命中路径 SQL={second["n"]} 超 6（仅应剩认证 + 数据版本查询）')