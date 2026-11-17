"""S4 店铺与站点（TC-API-STO-001~003）"""
import uuid


class TestStoresSites:
    def test_seed_stores(self, client, admin_headers):  # TC-API-STO-001
        r = client.get('/api/v2/stores', headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        items = body['items'] if isinstance(body, dict) else body
        codes = {s['store_code'] for s in items}
        assert {'LMG', 'MGK', 'MGT', 'STX', 'WEL'} <= codes

    def test_sites_list(self, client, admin_headers):  # TC-API-STO-002
        r = client.get('/api/v2/stores/sites', headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        items = body['items'] if isinstance(body, dict) else body
        by_code = {s['site_code']: s for s in items}
        assert 'UK' in by_code and 'DE' in by_code
        assert by_code['UK']['default_currency'] == 'GBP'
        assert by_code['DE']['default_currency'] == 'EUR'
        assert len(items) >= 16

    def test_store_update_delete(self, client, admin_headers):  # TC-API-STO-003
        # 建一个一次性店铺再删除（不污染种子店铺）
        uname = f'TST{uuid.uuid4().hex[:4]}'
        from app.core.database import SessionLocal
        from app.models import Store
        db = SessionLocal()
        try:
            db.add(Store(store_code=uname, store_name='临时测试店铺', status='active'))
            db.commit()
            sid = db.query(Store).filter(Store.store_code == uname).first().id
        finally:
            db.close()
        r = client.put(f'/api/v2/stores/{sid}', headers=admin_headers,
                       json={'store_name': '改名后店铺'})
        assert r.status_code == 200
        r2 = client.delete(f'/api/v2/stores/{sid}', headers=admin_headers)
        assert r2.status_code == 200
