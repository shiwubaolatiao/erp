"""存量快照批量重算 CLI（tasks.md 5.1）— 核心逻辑在 app/services/accounting/recalc.py

用法（backend 目录下执行）:
  python tools/recalc_snapshots.py --dry-run                 # 只列出受影响快照
  python tools/recalc_snapshots.py                           # 全部受影响月份重算
  python tools/recalc_snapshots.py --stores LMG,MGK          # 限定店铺
  python tools/recalc_snapshots.py --months 2026-01,2026-06  # 限定月份
  python tools/recalc_snapshots.py --verify                  # 店铺级==Σ站点级 全量校验

逐月独立事务：单月失败回滚该月并继续其余月份（部分成功不打折整体）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from app.core.database import SessionLocal
from app.services.accounting.recalc import recalc_affected, verify_snapshots


def main():
    ap = argparse.ArgumentParser(description='存量快照批量重算（B-01 修复回填）')
    ap.add_argument('--stores', help='店铺代码逗号分隔（默认全部）')
    ap.add_argument('--months', help='月份逗号分隔 YYYY-MM（默认全部受影响月份）')
    ap.add_argument('--dry-run', action='store_true', help='只列出受影响快照，不重算')
    ap.add_argument('--verify', action='store_true',
                    help='校验模式：店铺级快照 == Σ同期站点级快照（差异 0.00）')
    args = ap.parse_args()

    stores = [s.strip() for s in args.stores.split(',')] if args.stores else None
    months = [m.strip() for m in args.months.split(',')] if args.months else None
    db = SessionLocal()
    try:
        if args.verify:
            r = verify_snapshots(db, stores)
            for s in r['skips']:
                print(f"  [skip] {s['store']} {s['period_month']}: {s['detail']}")
            for f in r['failures']:
                print(f"  [FAIL] {f['store']} {f['period_month']}: {f['detail']}")
            print(f"\n校验完成：{r['passed']}/{r['checked']} 通过，"
                  f"{r['skipped']} 组跳过（旧版/无站点级快照）")
            sys.exit(0 if r['ok'] else 1)

        r = recalc_affected(db, stores, months, dry_run=args.dry_run)
        if r['affected'] == 0:
            print('未发现受 B-01 影响的旧版店铺级快照（country=\'\' 且 rate_source 为空）')
            sys.exit(0)
        print(f"受影响快照：{r['affected']} 条（去重后 {r['targets']} 个 店铺×月份）")
        for it in r['items']:
            if it['status'] == 'dry_run':
                print(f"  [dry-run] {it['store']} {it['period_month']}: "
                      f"v{it['old_version']} net_profit_rmb={it['old_net_profit_rmb']}")
            elif it['status'] == 'ok':
                diff = round(it['new_net_profit_rmb'] - it['old_net_profit_rmb'], 2)
                print(f"  {it['store']} {it['period_month']}: "
                      f"v{it['old_version']} → v{it['new_version']}  "
                      f"net_profit_rmb {it['old_net_profit_rmb']} → "
                      f"{it['new_net_profit_rmb']}（差异 {diff:+.2f}）"
                      f"  rate_source={it['rate_source']}")
            else:
                print(f"  {it['store']} {it['period_month']}: "
                      f"重算失败（已回滚，继续其余月份）: {it['error']}")
        print(f"\n重算完成：成功 {r['success']}/{r['targets']}"
              f"（旧版本全部保留，可按 version 追溯）")
        sys.exit(0 if r['ok'] else 1)
    finally:
        db.close()


if __name__ == '__main__':
    main()