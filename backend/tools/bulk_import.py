"""批量导入工具 — 汇总目录 01~06 全部报表

用法:
  python tools/bulk_import.py [--root "C:\\...\\汇总"] [--months 01,02] [--reset]

设计说明（重要）：
  - 每个文件按其所在月份目录推断归属月份（用于成本/汇率/广告的月度归属）
  - STX/WEL 的 01 目录里是"01-03 合并工作簿"：这类文件含 3 个月的销售流水，
    销售按**原始业务日期**归月（正确）；但**广告表无日期列**，只能按归属月份整体归集，
    会在导入报告中列出该限制。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from app.core.database import SessionLocal, ensure_database, engine, Base
import app.models  # noqa
from app.models import Store, ImportBatch, RawSheetRow
from app.services.importer.pipeline import ImportTask
from app.services.importer.parser import parse_store_from_filename
from sqlalchemy import func as sa_func

DEFAULT_ROOT = r'C:\Users\五包辣条\Desktop\汇总'
YEAR = 2026


def main():
    """批量导入汇总目录下所有报表文件（按目录名推断归属月份）"""
    args = sys.argv[1:]
    root = Path(DEFAULT_ROOT)
    if '--root' in args:
        root = Path(args[args.index('--root') + 1])
    only_months = None
    if '--months' in args:
        only_months = set(args[args.index('--months') + 1].split(','))

    ensure_database()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # 收集文件
    tasks = []
    for month_dir in sorted(root.iterdir()):
        if not month_dir.is_dir():
            continue
        mm = month_dir.name.zfill(2)
        if only_months and mm not in only_months:
            continue
        for f in sorted(month_dir.glob('*.xlsx')):
            store, region = parse_store_from_filename(f.name)
            if not store:
                print(f"  [skip] 无法识别店铺: {f.name}")
                continue
            tasks.append((f, store, f"{YEAR}-{mm}"))

    print(f"待导入文件: {len(tasks)} 个\n")
    total_ok = total_bad = 0
    t_start = time.time()
    try:
        for idx, (path, store_code, period) in enumerate(tasks, 1):
            store = db.query(Store).filter(Store.store_code == store_code).first()
            if not store:
                store = Store(store_code=store_code, store_name=store_code, status='active')
                db.add(store)
                db.commit()
            label = f"[{idx}/{len(tasks)}] {period} {store_code:4s} {path.name}"
            t0 = time.time()
            logs = []
            meta = {'id': f'bulk-{idx}', 'progress_cb': logs.append, 'period_month': period}
            task = ImportTask(db, str(path), path.name, store_code, None, meta)
            try:
                batch = task.run()
                total_ok += batch.success_rows or 0
                total_bad += batch.failed_rows or 0
                print(f"{label} → 批次#{batch.id} {batch.status} "
                      f"成功={batch.success_rows} 失败={batch.failed_rows} "
                      f"({time.time()-t0:.1f}s)")
                # 打印未识别/异常的 sheet 提示
                for line in logs:
                    if '跳过' in line or '缺少' in line or '未识别' in line:
                        print(f"      {line}")
            except Exception as e:
                db.rollback()
                print(f"{label} → 失败: {e}")
    finally:
        db.close()
    print(f"\n批量导入完成: 成功 {total_ok} 行 / 失败 {total_bad} 行，"
          f"总耗时 {time.time()-t_start:.1f}s")


if __name__ == '__main__':
    main()
