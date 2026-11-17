"""组8.5 契约回归测试 — LEGACY_CONTRACT_ALIAS 两态响应键断言（D-01/D-02 别名下线闸门）

背景：8.2/8.4 完成后，前端只读规范键；旧别名由 contract_alias 集中注入。
本文件是"别名下线"的放行闸门（spec 5.5.3 场景 1）：
  · 开关=true（默认过渡态）：规范键与别名键同时在场，且别名值与规范值一致
    ——保证存量未切换页面（如第三方脚本）在过渡期不白屏；
  · 开关=false（下线态）：响应不得再出现任何弃用别名键
    ——证明关闭开关即可安全下线，无残留读取方。

期望值全部直接对照前端 8.4 切换后的读取方式（页面源码 dataIndex/解构键），
不引用被测实现，避免"实现验证实现"。
"""
import pytest

from conftest import PERIOD

from app.core.config import settings

# ============================================================
# 断言矩阵：(路径, 参数, 规范键集合〔两态恒在〕, 顶层弃用别名键〔仅 true 态在场〕)
# ============================================================
RESPONSE_CASES = [
    ('/dashboard/summary', {'store': 'LMG', 'period_month': PERIOD},
     {'net_profit_rmb', 'sales_rmb', 'order_count', 'qty', 'net_profit_rate',
      'ad_spend_rmb', 'storage_fee_rmb', 'refund_qty', 'previous', 'change_percent'},
     {'current'}),
    ('/dashboard/trend', {'store': 'LMG', 'dimension': 'monthly'},
     {'items', 'dimension'}, {'data'}),
    ('/dashboard/product-distribution', {'store': 'LMG', 'period_month': PERIOD},
     {'items'}, {'data'}),
    ('/dashboard/fee-composition', {'store': 'LMG', 'period_month': PERIOD},
     {'total_sales_rmb', 'net_profit_rmb', 'items'},
     {'sales', 'expenses', 'net_profit'}),      # expenses 缺失曾致整页白屏
    ('/dashboard/refund-ranking', {'store': 'LMG', 'period_month': PERIOD, 'limit': 10},
     {'items'}, {'data'}),
    ('/dashboard/store-compare', {'period_month': PERIOD},
     {'items'}, {'data'}),
    ('/dashboard/country-compare', {'period_month': PERIOD},
     {'items'}, {'data'}),
    ('/dashboard/transfer', {'period_month': PERIOD},
     {'total', 'total_rmb', 'by_country'},
     {'total_usd', 'by_type'}),
    ('/query/sales', {'store': 'LMG', 'period_month': PERIOD},
     {'total', 'items', 'totals'}, {'data'}),
    ('/query/products', {'store': 'LMG'},
     {'total', 'items'}, {'data'}),
    ('/query/ad', {'store': 'LMG', 'period_month': PERIOD},
     {'total_spend', 'total_sales', 'acos', 'roas', 'rows', 'items'},
     {'total_ad_spend', 'avg_acos', 'avg_roas', 'total', 'data'}),
]

# 行级别名：[(路径, 参数, 行列表取值键, 规范行键集合, 别名行键集合)]
ROW_CASES = [
    ('/query/sales', {'store': 'LMG', 'period_month': PERIOD, 'page_size': 50}, 'items',
     {'asin', 'sales_usd', 'country'}, {'sales', 'product_sales_usd', 'country_code', 'country_name'}),
    ('/query/products', {'store': 'LMG'}, 'items',
     {'id', 'store_code', 'asin', 'title', 'status', 'skus'}, {'code', 'name'}),
]


def _get(client, headers, path, params):
    r = client.get(f'/api/v2{path}', headers=headers, params=params)
    assert r.status_code == 200, f'{path} {r.status_code}: {r.text[:200]}'
    return r.json()


@pytest.mark.usefixtures('calc')
class TestAliasOn:
    """开关=true：双契约过渡态 —— 规范键与别名键并存，别名值=规范值"""

    def test_response_keys_both_present(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(settings, 'LEGACY_CONTRACT_ALIAS', True)
        for path, params, canonical, alias in RESPONSE_CASES:
            body = _get(client, admin_headers, path, params)
            missing = canonical - body.keys()
            assert not missing, f'{path} 缺规范键: {sorted(missing)}'
            miss_alias = alias - body.keys()
            assert not miss_alias, f'{path} 开关开但缺别名键: {sorted(miss_alias)}'

    def test_row_keys_both_present(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(settings, 'LEGACY_CONTRACT_ALIAS', True)
        for path, params, list_key, canonical, alias in ROW_CASES:
            items = _get(client, admin_headers, path, params)[list_key]
            assert items, f'{path} 无数据行，无法断言行级键'
            missing = canonical - items[0].keys()
            assert not missing, f'{path} 行缺规范键: {sorted(missing)}'
            miss_alias = alias - items[0].keys()
            assert not miss_alias, f'{path} 开关开但行缺别名键: {sorted(miss_alias)}'

    def test_alias_values_equal_canonical(self, client, admin_headers, monkeypatch):
        """别名值必须由规范值机械映射（同一来源，无二次计算漂移）"""
        monkeypatch.setattr(settings, 'LEGACY_CONTRACT_ALIAS', True)

        s = _get(client, admin_headers, '/dashboard/summary',
                 {'store': 'LMG', 'period_month': PERIOD})
        cur = s['current']
        assert cur['total_net_profit_rmb'] == s['net_profit_rmb']
        assert cur['total_product_sales_rmb'] == s['sales_rmb']
        assert cur['total_order_count'] == s['order_count']
        assert cur['total_order_qty'] == s['qty']
        assert cur['total_ad_spend_rmb'] == s['ad_spend_rmb']
        assert cur['total_storage_fee_rmb'] == s['storage_fee_rmb']
        assert cur['total_refund_qty'] == s['refund_qty']

        f = _get(client, admin_headers, '/dashboard/fee-composition',
                 {'store': 'LMG', 'period_month': PERIOD})
        assert f['expenses'] == f['items']            # 白屏根因键：值必须同源
        assert f['sales'] == f['total_sales_rmb']
        assert f['net_profit'] == f['net_profit_rmb']

        t = _get(client, admin_headers, '/dashboard/trend',
                 {'store': 'LMG', 'dimension': 'monthly'})
        if t['items']:
            assert len(t['data']) == len(t['items'])
            assert t['data'][0]['label'] == t['items'][0]['period']
            assert t['data'][0]['net_profit'] == (t['items'][0]['net_profit_rmb'] or 0)

        tf = _get(client, admin_headers, '/dashboard/transfer',
                  {'period_month': PERIOD})
        assert tf['total_usd'] == round(float(tf['total']), 2)

        rows = _get(client, admin_headers, '/query/sales',
                    {'store': 'LMG', 'period_month': PERIOD, 'page_size': 50})['items']
        if rows:
            assert rows[0]['sales'] == rows[0]['sales_usd']
            assert rows[0]['product_sales_usd'] == rows[0]['sales_usd']
            assert rows[0]['country_code'] == rows[0]['country']

        prods = _get(client, admin_headers, '/query/products',
                     {'store': 'LMG'})['items']
        if prods:
            assert prods[0]['code'] == prods[0]['store_code']
            assert prods[0]['name'] == (prods[0]['title'] or prods[0]['asin'] or '')

        ad = _get(client, admin_headers, '/query/ad',
                  {'store': 'LMG', 'period_month': PERIOD})
        assert ad['total_ad_spend'] == ad['total_spend']
        assert ad['avg_acos'] == round(ad['acos'] * 100, 2)
        assert ad['avg_roas'] == round(ad['roas'], 2)
        assert ad['total'] == ad['rows']
        assert ad['data'] == ad['items']


@pytest.mark.usefixtures('calc')
class TestAliasOff:
    """开关=false：下线态 —— 规范键恒在，弃用别名键必须全部缺席"""

    def test_response_keys_alias_absent(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(settings, 'LEGACY_CONTRACT_ALIAS', False)
        for path, params, canonical, alias in RESPONSE_CASES:
            body = _get(client, admin_headers, path, params)
            missing = canonical - body.keys()
            assert not missing, f'{path} 缺规范键: {sorted(missing)}'
            leaked = alias & body.keys()
            assert not leaked, f'{path} 开关关但别名键泄漏: {sorted(leaked)}'

    def test_row_keys_alias_absent(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(settings, 'LEGACY_CONTRACT_ALIAS', False)
        for path, params, list_key, canonical, alias in ROW_CASES:
            items = _get(client, admin_headers, path, params)[list_key]
            assert items, f'{path} 无数据行，无法断言行级键'
            missing = canonical - items[0].keys()
            assert not missing, f'{path} 行缺规范键: {sorted(missing)}'
            leaked = alias & items[0].keys()
            assert not leaked, f'{path} 开关关但行别名键泄漏: {sorted(leaked)}'

    def test_frontend_reads_only_canonical_keys(self):
        """前端源码不得再读取弃用别名键（8.4 切换完成的静态验证）"""
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent.parent / 'frontend' / 'src'
        assert src.exists(), f'前端源码目录不存在: {src}'
        # 弃用别名的读取形态（精确匹配，避免 React ref 的 .current / 分页 current 误报）：
        # summary.current.total_* 旧视图 / fee-composition.expenses / 广告旧汇总三键 /
        # transfer.total_usd、by_type / DataQuery 的 res.data.data 二层包装
        patterns = [
            r'current\?*\.\s*total_', r'\.expenses\b',
            r'total_ad_spend\b', r'avg_acos\b', r'avg_roas\b',
            r'total_usd\b', r'by_type\b',
            r'\.data\?\.data\b', r'res\.data\.data\b',
        ]
        hits = []
        for p in src.rglob('*.jsx'):
            text = p.read_text(encoding='utf-8', errors='replace')
            for pat in patterns:
                for m in re.finditer(pat, text):
                    line_no = text[:m.start()].count('\n') + 1
                    hits.append(f'{p.relative_to(src.parent)}:{line_no} 匹配 {pat}')
        assert not hits, '前端仍读取弃用别名键:\n' + '\n'.join(hits[:20])