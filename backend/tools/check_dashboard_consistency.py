"""看板数据一致性核对 — summary / trend / fee-composition 三处同月数字必须一致

用法: python tools/check_dashboard_consistency.py [--year 2026]
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = 'http://127.0.0.1:8020/api/v2'
TOL = 0.01


def api(path, params, token):
    url = BASE + path + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    year = 2026
    if '--year' in sys.argv:
        year = int(sys.argv[sys.argv.index('--year') + 1])
    data = urllib.parse.urlencode({'username': 'admin', 'password': '123456'}).encode()
    req = urllib.request.Request(BASE + '/auth/login', data=data,
                                headers={'Content-Type': 'application/x-www-form-urlencoded'})
    token = json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))['access_token']

    trend = api('/dashboard/trend', {'dimension': 'monthly'}, token)
    tmap = {d['label']: d for d in trend['data']}

    print(f"{'月份':9s} {'来源':6s} {'销售额':>16s} {'净利润':>15s} {'广告费':>14s} {'仓储费':>13s}")
    print('-' * 78)
    bad = 0
    for m in range(1, 13):
        pm = f'{year}-{m:02d}'
        if pm not in tmap:
            continue
        s = api('/dashboard/summary', {'year': year, 'month': m}, token)
        f = api('/dashboard/fee-composition', {'year': year, 'month': m}, token)
        t = tmap[pm]
        c = s['current']
        rows = [
            ('summary', c['total_product_sales_rmb'], c['total_net_profit_rmb'],
             c['total_ad_spend_rmb'], c['total_storage_fee_rmb']),
            ('trend', t['sales'], t['net_profit'], t['ad_spend'], t['storage_fee']),
            ('fee', f['sales'], f['net_profit'], None, None),
        ]
        for name, sales, profit, ad, st in rows:
            print(f"{pm:9s} {name:6s} {sales:16,.2f} {profit:15,.2f} "
                  f"{(ad if ad is not None else float('nan')):14,.2f} "
                  f"{(st if st is not None else float('nan')):13,.2f}")
        # 三处销售额/净利润必须一致
        for label, idx in (('销售额', 0), ('净利润', 1)):
            vals = [r[idx + 1] for r in rows]
            if max(vals) - min(vals) > TOL:
                bad += 1
                print(f"   ✗ {label} 不一致: {vals}")
        # 广告/仓储 summary vs trend
        for label, si, ti in (('广告费', 3, 3), ('仓储费', 4, 4)):
            a, b = rows[0][si], rows[1][ti]
            if abs(a - b) > TOL:
                bad += 1
                print(f"   ✗ {label} summary={a:,.2f} trend={b:,.2f} 不一致")
        print()

    print('=' * 78)
    print(f'一致性核对完成：不一致项 {bad}')
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
