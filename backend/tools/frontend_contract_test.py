"""前端契约冒烟测试（组8.5 双闸门之一）— 验证「后端返回」满足「页面读取」的需求

背景：重构后新后端字段名与现有前端页面期望不一致，曾导致：
  · 看板全为空（参数/字段不匹配）
  · 整页白屏（fee-composition 缺 expenses → 展开 undefined 抛 TypeError）

组8 后前后端均切换规范契约，本脚本用**前端真实使用的参数与字段访问方式**
逐接口校验规范键；任何一条不满足即报错 —— 作为"契约漂移"的回归防线。

双闸门用法（别名下线流程，spec 5.5.3 场景 1）：
  1. python tools/frontend_contract_test.py
     —— 别名开（默认过渡态）：规范键齐全 + 别名键在场且与规范值一致（存量脚本不破）
  2. python tools/frontend_contract_test.py --no-alias
     —— 服务以 LEGACY_CONTRACT_ALIAS=false 启动后执行：弃用别名键必须全部缺席，
        此模式全绿 + node check_api_exports.mjs 全绿 = 可下线别名
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

failures = []
checks = 0


def api(base, path, params=None, token=None):
    url = base + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def check(label, cond, detail=''):
    global checks
    checks += 1
    if cond:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        failures.append(f'{label} {detail}')


def has_keys(obj, keys, label):
    """断言 dict 含全部键（前端会直接读这些键）"""
    if not isinstance(obj, dict):
        check(label, False, f'不是对象而是 {type(obj).__name__}')
        return
    missing = [k for k in keys if k not in obj]
    check(label, not missing, f'缺少键 {missing}')


def absent_keys(obj, keys, label):
    """断言 dict 不含任何弃用别名键（--no-alias 闸门）"""
    if not isinstance(obj, dict):
        check(label, False, f'不是对象而是 {type(obj).__name__}')
        return
    leaked = [k for k in keys if k in obj]
    check(label, not leaked, f'弃用别名键泄漏 {leaked}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:8020/api/v2')
    ap.add_argument('--username', default='admin')
    ap.add_argument('--password', default='123456')
    ap.add_argument('--no-alias', action='store_true',
                    help='别名下线闸门：要求服务以 LEGACY_CONTRACT_ALIAS=false 启动，'
                         '断言弃用别名键全部缺席')
    args = ap.parse_args()
    period = {'period_month': '2026-01'}       # 与合成/验证数据一致的月份

    data = urllib.parse.urlencode(
        {'username': args.username, 'password': args.password}).encode()
    req = urllib.request.Request(args.base + '/auth/login', data=data,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    token = json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))['access_token']
    print(f'登录 OK（模式：{"别名关（下线闸门）" if args.no_alias else "别名开（过渡态）"}）\n')

    def get(path, params=None):
        return api(args.base, path, params, token)

    # ============ 看板（Dashboard.jsx 规范键） ============
    print('【看板】页面直接读规范键：')

    # 1) summary → net_profit_rmb / sales_rmb / order_count / previous / change_percent
    s = get('/dashboard/summary', period)
    has_keys(s, ['net_profit_rmb', 'sales_rmb', 'order_count', 'qty', 'net_profit_rate',
                 'ad_spend_rmb', 'storage_fee_rmb', 'refund_qty',
                 'previous', 'change_percent'], 'summary 规范键齐全')
    if not args.no_alias:
        cur = s.get('current') or {}
        has_keys(cur, ['total_net_profit_rmb', 'total_product_sales_rmb',
                       'total_order_count', 'total_order_qty', 'avg_net_profit_rate',
                       'total_ad_spend_rmb', 'total_storage_fee_rmb', 'total_refund_qty'],
                 'summary.current 旧视图键齐全（过渡期）')
        check('summary.current.total_net_profit_rmb == net_profit_rmb',
              cur.get('total_net_profit_rmb') == s.get('net_profit_rmb'))
    else:
        absent_keys(s, ['current'], 'summary 无 current 别名')

    # 2) trend → items[].period/net_profit_rmb/sales_rmb/order_count
    t = get('/dashboard/trend', {'dimension': 'monthly'})
    check('trend 返回 items 数组', isinstance(t.get('items'), list),
          f"实际 {type(t.get('items')).__name__}")
    if t.get('items'):
        has_keys(t['items'][0],
                 ['period', 'net_profit_rmb', 'sales_rmb', 'order_count',
                  'ad_spend_rmb', 'storage_fee_rmb'], 'trend.items[0] 规范键齐全')

    # 3) fee-composition → total_sales_rmb / net_profit_rmb / items
    c = get('/dashboard/fee-composition', period)
    has_keys(c, ['total_sales_rmb', 'net_profit_rmb'], 'fee-composition 汇总规范键')
    check('fee-composition 返回 items 数组', isinstance(c.get('items'), list),
          f"实际 {type(c.get('items')).__name__}")
    if c.get('items'):
        has_keys(c['items'][0], ['name', 'value'], 'fee-composition.items[0] 含 name/value')
    if not args.no_alias:
        check('fee-composition 含 expenses 别名（白屏根因，过渡期保留）',
              c.get('expenses') == c.get('items'), 'expenses != items')
    else:
        absent_keys(c, ['sales', 'expenses', 'net_profit'], 'fee-composition 无弃用别名')

    # 4) product-distribution → items[].name/sales_rmb/net_profit_rmb
    d = get('/dashboard/product-distribution', period)
    check('product-distribution 返回 items 数组', isinstance(d.get('items'), list),
          f"实际 {type(d.get('items')).__name__}")
    if d.get('items'):
        has_keys(d['items'][0], ['name', 'sales_rmb', 'net_profit_rmb'],
                 'product-distribution.items[0] 规范键')

    # 5) refund-ranking → items[].asin/returned/order_count/return_rate_pct
    tr = get('/dashboard/refund-ranking', {**period, 'limit': 10})
    check('refund-ranking 返回 items 数组', isinstance(tr.get('items'), list),
          f"实际 {type(tr.get('items')).__name__}")
    if tr.get('items'):
        has_keys(tr['items'][0], ['asin', 'returned', 'order_count', 'return_rate_pct'],
                 'refund-ranking.items[0] 规范键')

    # 6) store-compare / 7) country-compare → items[]
    for name, path in (('store-compare', '/dashboard/store-compare'),
                       ('country-compare', '/dashboard/country-compare')):
        body = get(path, period)
        check(f'{name} 返回 items 数组', isinstance(body.get('items'), list),
              f"实际 {type(body.get('items')).__name__}")
        if body.get('items'):
            has_keys(body['items'][0], ['sales_rmb', 'net_profit_rmb', 'order_count'],
                     f'{name}.items[0] 规范键')

    # 8) transfer → total / total_rmb / by_country
    tf = get('/dashboard/transfer', period)
    has_keys(tf, ['total', 'total_rmb', 'by_country'], 'transfer 规范键齐全')
    if args.no_alias:
        absent_keys(tf, ['total_usd', 'by_type'], 'transfer 无弃用别名')

    # ============ 数据查询（DataQuery.jsx / Admin.jsx） ============
    print('\n【数据查询】页面读 items / 规范行键：')
    qs = get('/query/sales', {**period, 'group_by': 'country'})
    check('query/sales 含 items 数组 + totals', isinstance(qs.get('items'), list)
          and 'totals' in qs, f"keys={list(qs)[:6]}")
    if qs.get('items'):
        has_keys(qs['items'][0], ['country', 'sales_usd'], 'query/sales.items[0] 规范键')
    prods = get('/query/products', {'limit': 5})
    check('query/products 含 items 数组 + total', isinstance(prods.get('items'), list)
          and 'total' in prods, f"keys={list(prods)[:6]}")
    if prods.get('items'):
        has_keys(prods['items'][0], ['id', 'store_code', 'asin', 'title', 'status', 'skus'],
                 'query/products.items[0] 规范键')
    if args.no_alias:
        absent_keys(qs, ['data'], 'query/sales 无 data 别名')
        absent_keys(prods, ['data'], 'query/products 无 data 别名')

    # ============ 广告分析（Advertising.jsx） ============
    print('\n【广告分析】页面读 total_spend/acos/roas/items：')
    ad = get('/query/ad', period)
    has_keys(ad, ['total_spend', 'total_sales', 'acos', 'roas', 'rows', 'items'],
             'query/ad 规范键齐全')
    if ad.get('items'):
        has_keys(ad['items'][0],
                 ['asin', 'sku', 'country', 'spend', 'sales', 'impressions', 'clicks',
                  'orders', 'acos', 'roas', 'ctr', 'cpc'], 'query/ad.items[0] 规范键')
    if not args.no_alias:
        check('别名值一致（过渡期）',
              ad.get('total_ad_spend') == ad.get('total_spend')
              and ad.get('data') == ad.get('items'))
    else:
        absent_keys(ad, ['total_ad_spend', 'avg_acos', 'avg_roas', 'total', 'data'],
                    'query/ad 无弃用别名')

    # ============ 下拉框 ============
    print('\n【下拉框】所有页面的店铺/国家选择器：')
    st = get('/stores')
    check('stores 是数组', isinstance(st, list))
    check('stores[0] 含 code/name', bool(st) and 'code' in st[0] and 'name' in st[0],
          f"keys={list(st[0])[:8] if st else None}")
    sites = get('/stores/sites')
    check('sites[0] 含 code/name', bool(sites) and 'code' in sites[0] and 'name' in sites[0],
          f"keys={list(sites[0])[:8] if sites else None}")

    # ============ 导入（DataImport.jsx 直连异步任务） ============
    print('\n【导入】页面读任务句柄（task_id/status/progress/batches）：')
    tasks = get('/import/tasks')
    check('import/tasks 返回对象（含 tasks 列表）', isinstance(tasks, dict) or isinstance(tasks, list),
          f'实际 {type(tasks).__name__}')

    print(f'\n{"="*60}')
    mode = '别名下线闸门（--no-alias）' if args.no_alias else '过渡态（别名开）'
    print(f'契约检查[{mode}]: {checks} 项，失败 {len(failures)}')
    for f in failures:
        print('  ✗', f)
    if args.no_alias:
        print('\n双绿判定：本工具 0 失败 + frontend/check_api_exports.mjs 通过'
              ' → 可执行 LEGACY_CONTRACT_ALIAS=false 下线')
    return 0 if not failures else 1


if __name__ == '__main__':
    sys.exit(main())
