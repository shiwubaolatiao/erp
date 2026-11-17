"""LMG ERP v2 系统测试 — 会话级 fixtures（测试文档 docs/测试文档.md 的实施载体）

环境隔离策略：
  - DB_NAME 强制指向 lmg_erp_test（conftest 导入 app 前设置），绝不触碰生产库 lmg_erp
  - 会话开始：drop/create 测试库 → TestClient 启动建表/种子
  - 会话结束：dispose 引擎 → drop 测试库
合成数据（单一数据源）：文件名/表头/行值与期望值常量全部在此定义，
期望值由人工独立推导（不引用被测代码），实现"独立复算"验收方法。
"""
import json
import os
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# ★ 必须在 import app.* 之前设置（load_dotenv 不覆盖已存在的环境变量）
os.environ.setdefault("DB_NAME", "lmg_erp_test")
TEST_DB_NAME = os.environ["DB_NAME"]

# ★ pytest 专用 JWT 密钥（platform_startup_enhance 组2 / D9）：
#   后端 JWT_SECRET 空白即 fail-fast，测试环境必须在导入 app.* 前显式提供。
#   仅当未显式配置（空白）时补齐，绝不覆盖有效显式配置；不依据 PYTEST_CURRENT_TEST 延迟判断。
if not os.environ.get("JWT_SECRET", "").strip():
    os.environ["JWT_SECRET"] = "pytest-only-jwt-secret-not-for-production"

import pytest
import pymysql
from openpyxl import Workbook

# ================= 合成测试数据（TMPL-LMG-2026-01.xlsx 设计基准） =================

UK_HEADER = ['date/time', 'type', 'order id', 'sku', 'asin', 'quantity',
             'product sales', 'product sales tax', 'shipping credits',
             'selling fees', 'fba fees', 'other', 'total']

UK_SALES_ROWS = [
    # net_amazon = ps + pst + ship + sf + fba
    ['2026-01-05 10:00:00 UTC', 'Order', '202-AAA', 'SKU-A', 'B0AAAAAAA1',
     2, 100.00, 0, 5.00, -15.00, -8.00, 0, 82.00],
    ['2026-01-08 12:30:00 UTC', 'Order', '202-BBB', 'SKU-B', 'B0BBBBBBB2',
     1, 50.50, 2.50, 3.00, -7.50, -4.00, 0, 44.50],
    ['2026-01-15 09:00:00 UTC', 'Refund', '202-AAA', 'SKU-A', 'B0AAAAAAA1',
     -1, -50.00, 0, -2.50, -7.50, -4.00, 0, -64.00],
    # 坏日期行 → 应进异常池（TC-IMP-006），不得进 fact
    ['not-a-date', 'Order', '202-CCC', 'SKU-C', 'B0CCCCCCC3',
     1, 10.00, 0, 0, -1.00, 0, 0, 9.00],
]

DE_HEADER = ['Datum/Uhrzeit', 'type', 'order id', 'sku', 'asin',
             'Menge', 'Umsätze', 'Verkaufsgebühren', 'total']
DE_SALES_ROWS = [
    # 德语：Bestellung→Order；点分日期；Umsätze→product sales；Verkaufsgebühren→selling fees
    ['01.01.2026 14:03:54 UTC', 'Bestellung', '202-DDD', 'SKU-D', 'B0DDDDDDD4',
     1, 200.00, -20.00, 180.00],
]

PRODUCT_HEADER = ['asin', 'sku', '成本rmb', '产品运费/台']
PRODUCT_ROWS = [
    ['B0AAAAAAA1', 'SKU-A', 10, 2],
    ['B0BBBBBBB2', 'SKU-B', 5, 1],
    ['B0DDDDDDD4', 'SKU-D', 20, 3],
]
RATE_HEADER = ['国家', '汇率']
RATE_ROWS = [['UK', 9.0], ['DE', 8.5]]

# 期望值（人工独立推导）
EXP = {
    'uk_net_amazon': 62.50,          # 82 + 44.5 - 64
    'de_net_amazon': 180.00,         # 200 - 20
    'lmg_sales_usd': 300.50,         # 100 + 50.5 - 50 + 200
    'lmg_refund_amount': 50.00,
    'uk_net_profit': 544.50,         # 62.5×9 - (10+2) - (5+1)（SKU-A 净量1、SKU-B 净量1）
    'de_net_profit': 1507.00,        # 180×8.5 - (20+3)（SKU-D 净量1）
    'uk_ps_rmb': 904.50,             # (100+50.5-50)×9
    'de_ps_rmb': 1700.00,            # 200×8.5
    'uk_profit_rate': 0.6020,        # 544.5/904.5
    'de_profit_rate': 0.8865,        # 1507/1700
    'mgt_net_amazon': 33.00,         # 40 - 4 - 3
    'feb_net_amazon': 68.00,         # 80 - 8 - 4
}

PERIOD = '2026-01'
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


# ================= 底层工具 =================

def sql(stmt: str, **params):
    """测试内直查隔离库（只读用途为主）"""
    from sqlalchemy import text
    from app.core.database import engine
    with engine.connect() as c:
        return [dict(m) for m in c.execute(text(stmt), params).mappings().all()]


def sql_one(stmt: str, **params):
    rows = sql(stmt, **params)
    return rows[0] if rows else None


def build_xlsx(path: Path, sheets: list[tuple[str, list]]):
    """sheets: [(sheet名, 行列表)]；空表头行由调用方自行拼装"""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


def upload_and_wait(client, headers, path: Path, name: str, store: str,
                    period: str | None, timeout: float = 180) -> dict:
    """上传并轮询后台任务直到结束（R3 时序竞争对策）"""
    with open(path, 'rb') as f:
        r = client.post('/api/v2/import/upload', headers=headers,
                        files={'files': (name, f, XLSX_MIME)},
                        data={'store': store, 'period_month': period or ''})
    assert r.status_code == 200, f"上传失败 {r.status_code}: {r.text}"
    task_id = r.json()['task_id']
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f'/api/v2/import/tasks/{task_id}', headers=headers).json()
        if st.get('status') in ('success', 'failed'):
            return st
        time.sleep(0.4)
    raise TimeoutError(f'导入任务 {task_id} 超时未完成')


def calc_and_wait(client, headers, body: dict, timeout: float = 120) -> dict:
    """发起核算（默认异步）并轮询任务直到完成，返回快照结果 dict

    组6 契约：POST /accounting/calculate 返回 {task_id, status, poll_url}；
    本 helper 等待终态后合并任务 result（快照字段）+ task_id/status（与旧同步响应结构对齐）。
    """
    r = client.post('/api/v2/accounting/calculate', headers=headers, json=body)
    assert r.status_code == 200, f"核算受理失败 {r.status_code}: {r.text}"
    accepted = r.json()
    assert accepted.get('task_id'), accepted
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/v2/accounting/tasks/{accepted['task_id']}",
                        headers=headers).json()
        if st['status'] in ('success', 'failed', 'interrupted'):
            break
        time.sleep(0.2)
    assert st['status'] == 'success', f"核算任务失败: {st.get('error')}"
    return {**(st['snapshot'] or {}), 'task_id': st['task_id'], 'status': 'done'}


# ================= 会话级 fixtures =================

@pytest.fixture(scope='session', autouse=True)
def _test_db():
    """会话开始：重建隔离测试库；会话结束：销毁（KEEP_TEST_DB=1 可保留排查）"""
    from app.core.config import settings
    conf = settings
    conn = pymysql.connect(host=conf.DB_HOST, port=conf.DB_PORT,
                           user=conf.DB_USER, password=conf.DB_PASSWORD,
                           charset='utf8mb4', autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
            cur.execute(
                f"CREATE DATABASE {TEST_DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        conn.close()
    yield
    from app.core.database import engine
    engine.dispose()
    if os.environ.get('KEEP_TEST_DB') != '1':
        conn = pymysql.connect(host=conf.DB_HOST, port=conf.DB_PORT,
                               user=conf.DB_USER, password=conf.DB_PASSWORD,
                               charset='utf8mb4', autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
        finally:
            conn.close()


@pytest.fixture(autouse=True)
def _clear_agg_cache():
    """函数级清请求聚合缓存 — 测试直接 ORM 造数不推进数据版本号，
    cached_aggregate 会跨测试命中旧键（如超龄费 7.77 泄漏进下一用例），必须隔离"""
    from app.core.cache import _cache
    _cache.clear()
    yield
    _cache.clear()


@pytest.fixture(scope='session')
def client(_test_db):
    """进程内 TestClient（同 FastAPI 应用，指向隔离库）"""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope='session')
def admin_headers(client):
    r = client.post('/api/v2/auth/login', data={'username': 'admin', 'password': '123456'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope='session')
def limited(client, admin_headers):
    """受限用户：仅 LMG 店铺、仅 query/dashboard 模块（RBAC 用例基准）"""
    import uuid
    uname = f't_limited_{uuid.uuid4().hex[:6]}'
    perms = json.dumps({"stores": ["LMG"], "modules": ["query", "dashboard"],
                        "fields": ["cost", "profit"]})
    r = client.post('/api/v2/auth/users', headers=admin_headers, json={
        'username': uname, 'password': 'Test#123456', 'is_admin': 0,
        'permissions': perms, 'display_name': '受限测试账号', 'department': '运营'})
    assert r.status_code == 200, r.text
    r2 = client.post('/api/v2/auth/login', data={'username': uname, 'password': 'Test#123456'})
    assert r2.status_code == 200, r2.text
    return {'username': uname, 'user_id': r.json()['id'],
            'headers': {'Authorization': f"Bearer {r2.json()['access_token']}"}}


@pytest.fixture(scope='session')
def empty_scope_user(client, admin_headers):
    """零授权店铺用户（TC-SEC-013）"""
    import uuid
    uname = f't_empty_{uuid.uuid4().hex[:6]}'
    perms = json.dumps({"stores": [], "modules": ["query"]})
    r = client.post('/api/v2/auth/users', headers=admin_headers, json={
        'username': uname, 'password': 'Test#123456', 'is_admin': 0, 'permissions': perms})
    assert r.status_code == 200, r.text
    r2 = client.post('/api/v2/auth/login', data={'username': uname, 'password': 'Test#123456'})
    return {'user_id': r.json()['id'],
            'headers': {'Authorization': f"Bearer {r2.json()['access_token']}"}}


@pytest.fixture(scope='session')
def e2e(client, admin_headers, tmp_path_factory):
    """★ 核心集成数据：一次性导入 3 个合成文件，返回全部后续测试依赖的 id 与结果"""
    tmp = tmp_path_factory.mktemp('import')
    # 文件1：LMG 主文件（UK 4 行 / DE 1 行 / 产品 3 行 / 汇率 2 行）
    f1 = build_xlsx(tmp / 'LMG-EU_2026-01.xlsx', [
        ('UK销售', [UK_HEADER] + UK_SALES_ROWS),
        ('DE销售', [DE_HEADER] + DE_SALES_ROWS),
        ('产品', [PRODUCT_HEADER] + PRODUCT_ROWS),
        ('汇率', [RATE_HEADER] + RATE_ROWS),
    ])
    st1 = upload_and_wait(client, admin_headers, f1, f1.name, 'LMG', PERIOD)
    # 文件2：MGT（表头在第 10 行，前面 9 行噪声说明）
    noise = [[f'多语言说明文字第{i}行'] for i in range(9)]
    f2 = build_xlsx(tmp / 'MGT-2026-01.xlsx', [
        ('UK销售', noise + [UK_HEADER] +
         [['2026-01-20 10:00:00 UTC', 'Order', '202-FFF', 'SKU-F', 'B0FFFFFFF6',
           1, 40.00, 0, 0, -4.00, -3.00, 0, 33.00]]),
    ])
    st2 = upload_and_wait(client, admin_headers, f2, f2.name, 'MGT', PERIOD)
    # 文件3：LMG 2026-02（无产品表 → 成本缺失场景）
    f3 = build_xlsx(tmp / 'LMG-2026-02.xlsx', [
        ('UK销售', [UK_HEADER] +
         [['2026-02-10 10:00:00 UTC', 'Order', '202-EEE', 'SKU-E', 'B0EEEEEEE5',
           1, 80.00, 0, 0, -8.00, -4.00, 0, 68.00]]),
    ])
    st3 = upload_and_wait(client, admin_headers, f3, f3.name, 'LMG', '2026-02')

    assert st1['status'] == 'success', st1.get('error')
    assert st2['status'] == 'success', st2.get('error')
    assert st3['status'] == 'success', st3.get('error')

    # 采集批次/行 id（供溯源与一致性用例使用）
    lmg_batches = {b['file_name']: b for b in st1['batches']}
    state = {
        'task_lmg': st1, 'task_mgt': st2, 'task_feb': st3,
        'lmg_batches': lmg_batches,
        'lmg_batch_ids': [b['batch_id'] for b in st1['batches']],
        'mgt_batch_id': st2['batches'][0]['batch_id'],
        'feb_batch_id': st3['batches'][0]['batch_id'],
    }
    row = sql_one("SELECT id FROM fact_sales_transaction WHERE order_id='202-AAA' "
                  "AND transaction_type='Order' LIMIT 1")
    assert row, 'LMG UK fact 未落库'
    state['uk_fact_id'] = row['id']
    row = sql_one("SELECT id FROM fact_sales_transaction WHERE order_id='202-DDD' LIMIT 1")
    state['de_fact_id'] = row['id']
    return state


@pytest.fixture(scope='session')
def calc(client, admin_headers, e2e):
    """★ 初始核算快照：LMG/UK 与 LMG/DE 2026-01 各 v1（核算/溯源/一致性用例共用）"""
    out = {}
    for country, key in (('UK', 'uk'), ('DE', 'de')):
        out[key] = calc_and_wait(client, admin_headers, {
            'store': 'LMG', 'country': country, 'period_month': PERIOD})
    return out
