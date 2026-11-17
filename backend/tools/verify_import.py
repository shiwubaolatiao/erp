"""端到端导入 + 溯源 + 核算验证脚本
用法: python tools/verify_import.py <report_file> <store_code> <period_month>
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from app.core.database import SessionLocal, ensure_database, engine, Base
import app.models  # noqa
from app.models import (
    ImportBatch, RawSheet, RawSheetRow, ImportRowError, FactSalesTransaction,
    FactAdDaily, FactFeeStorage, FactFeeReturn, FactFeeRemoval, FactFeeInboundConfig,
    FactFeeAgedStorage, ProductCostHistory, StoreProduct, StoreSkuMapping, ExchangeRate,
    SkuReplacement, Store,
)
from app.services.importer.pipeline import ImportTask
from app.services.accounting.service import calculate_snapshot
from sqlalchemy import func as sa_func


def main():
    """单文件端到端验证：导入 → 统计 → 表头识别 → 异常行 → 溯源链路 → 核算"""
    if len(sys.argv) < 4:
        print("usage: verify_import.py <file> <store_code> <period_month>")
        return 1
    path, store_code, period = sys.argv[1], sys.argv[2].upper(), sys.argv[3]

    ensure_database()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # 清理同批次残留（可重复运行）
        store = db.query(Store).filter(Store.store_code == store_code).first()
        if not store:
            store = Store(store_code=store_code, store_name=store_code, status='active')
            db.add(store)
            db.commit()
        print(f"店铺: {store.store_code} (id={store.id})  归属月份: {period}")

        t0 = time.time()
        meta = {'id': 'verify', 'progress_cb': (lambda m: print('  [imp]', m)), 'period_month': period}
        task = ImportTask(db, path, Path(path).name, store_code, None, meta)
        batch = task.run()
        print(f"\n导入耗时: {time.time()-t0:.1f}s")
        print(f"批次 #{batch.id} 状态={batch.status} 成功={batch.success_rows} 失败={batch.failed_rows}")

        # ---- 各表计数 ----
        bid = batch.id
        print("\n=== 本批次落库统计 ===")
        for label, model in [
            ('raw_sheet_row (ODS原始行)', RawSheetRow),
            ('fact_sales_transaction', FactSalesTransaction),
            ('fact_ad_daily', FactAdDaily),
            ('fact_fee_storage', FactFeeStorage),
            ('fact_fee_return', FactFeeReturn),
            ('fact_fee_removal', FactFeeRemoval),
            ('fact_fee_inbound_config', FactFeeInboundConfig),
            ('fact_fee_aged_storage', FactFeeAgedStorage),
        ]:
            n = db.query(sa_func.count(model.id)).filter(model.batch_id == bid).scalar() or 0
            print(f"  {label:32s} {n}")

        print("\n=== sheets 识别 ===")
        sheets = db.query(RawSheet).filter(RawSheet.batch_id == bid).all()
        for s in sheets:
            print(f"  {s.sheet_name:24s} type={s.report_type or '-':14s} country={s.country or '-':5s} "
                  f"cur={s.currency or '-':5s} header_row={s.detected_header_row} rows={s.row_count}")

        print("\n=== 异常行（前10）===")
        for e in db.query(ImportRowError).filter(ImportRowError.batch_id == bid).limit(10):
            print(f"  {e.sheet_name} r{e.row_no}: [{e.error_type}] {e.error_message}")

        # ---- 映射/成本 ----
        print("\n=== 三元组映射 ===")
        print("  store_product:", db.query(sa_func.count(StoreProduct.id)).filter(StoreProduct.store_id == store.id).scalar())
        for st in ('normal', 'pending', 'abnormal', 'rejected'):
            n = db.query(sa_func.count(StoreSkuMapping.id)).filter(
                StoreSkuMapping.store_id == store.id, StoreSkuMapping.status == st).scalar() or 0
            if n:
                print(f"  mapping.{st}: {n}")
        print("  替换件归并:", db.query(sa_func.count(SkuReplacement.id)).filter(SkuReplacement.store_id == store.id).scalar())
        print("  成本版本:", db.query(sa_func.count(ProductCostHistory.id)).filter(ProductCostHistory.store_id == store.id).scalar())
        print("  汇率:", db.query(sa_func.count(ExchangeRate.id)).filter(ExchangeRate.period_month == period).scalar())

        # ---- 溯源验证：抽一条 fact 走完链路 ----
        print("\n=== 溯源链路验证 ===")
        sample = db.query(FactSalesTransaction).filter(FactSalesTransaction.batch_id == bid).first()
        if sample:
            print(f"  fact#{sample.id} type={sample.transaction_type} asin={sample.asin} sku={sample.sku} "
                  f"biz_date={sample.biz_date} sales={sample.product_sales}")
            print(f"   → raw_row_id={sample.raw_row_id}")
            rr = db.query(RawSheetRow).get(sample.raw_row_id)
            if rr:
                hdr = rr.headers_json or []
                val = rr.values_json or []
                print(f"   → sheet={rr.sheet_name} row_no={rr.row_no} 列数={len(hdr)}")
                # 找出关键列对比
                for key in ('type', 'order id', 'sku', 'product sales', 'total'):
                    if key in [str(h).lower() for h in hdr]:
                        idx = [str(h).lower() for h in hdr].index(key)
                        print(f"      {key!r} = {val[idx]!r}")
        else:
            print("  [警告] 无销售 fact")

        # ---- 核算 ----
        print("\n=== 核算（利润）===")
        for country in ('UK', 'DE'):
            snap = calculate_snapshot(db, store.id, country, period, created_by=None,
                                      snapshot_name=f'verify-{country}')
            print(f"  {country}: 净利润={snap.net_profit_rmb} 净利率={snap.net_profit_rate} "
                  f"销售额RMB={snap.product_sales_rmb} 汇率={snap.exchange_rate} "
                  f"成本完整={snap.cost_complete} 缺失SKU={snap.cost_incomplete_skus}")
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
