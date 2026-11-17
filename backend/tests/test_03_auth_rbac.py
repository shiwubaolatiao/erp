"""S3 认证与权限（TC-SEC-001~016）— JWT / 停用拦截 / RBAC / 店铺数据权限"""
import json
import uuid


def _login(client, username='admin', password='123456'):
    return client.post('/api/v2/auth/login', data={'username': username, 'password': password})


class TestLogin:
    def test_login_ok(self, client):  # TC-SEC-001
        r = _login(client)
        assert r.status_code == 200
        body = r.json()
        assert body['access_token'] and body['token_type'] == 'bearer'
        assert body['user']['username'] == 'admin'
        assert body['user']['is_admin'] == 1

    def test_login_wrong_password(self, client):  # TC-SEC-002
        assert _login(client, password='wrong').status_code == 401

    def test_login_unknown_user_same_message(self, client):  # TC-SEC-003 防账号枚举
        r = _login(client, username='nosuchuser_xyz')
        assert r.status_code == 401
        assert r.json()['detail'] == '用户名或密码错误'

    def test_me_requires_token(self, client):  # TC-SEC-004
        assert client.get('/api/v2/auth/me').status_code == 401

    def test_me_fake_token(self, client):  # TC-SEC-005
        r = client.get('/api/v2/auth/me', headers={'Authorization': 'Bearer fake-token'})
        assert r.status_code == 401

    def test_me_tampered_token(self, client, admin_headers):  # TC-SEC-006
        token = admin_headers['Authorization'].split()[1]
        bad = token[:-3] + ('aaa' if token[-3:] != 'aaa' else 'bbb')
        r = client.get('/api/v2/auth/me', headers={'Authorization': f'Bearer {bad}'})
        assert r.status_code == 401

    def test_me_ok(self, client, admin_headers):  # TC-SEC-007
        r = client.get('/api/v2/auth/me', headers=admin_headers)
        assert r.status_code == 200
        perms = r.json()['permissions']
        assert set(perms.keys()) == {'stores', 'modules', 'fields'}


class TestUserLifecycle:
    def test_disabled_account(self, client, admin_headers):  # TC-SEC-008
        uname = f't_dis_{uuid.uuid4().hex[:6]}'
        r = client.post('/api/v2/auth/users', headers=admin_headers, json={
            'username': uname, 'password': 'Test#123456'})
        assert r.status_code == 200
        uid = r.json()['id']
        # 先登录拿 token
        tok = _login(client, uname, 'Test#123456').json()['access_token']
        # 停用
        r = client.put(f'/api/v2/auth/users/{uid}', headers=admin_headers,
                       json={'status': 'disabled'})
        assert r.status_code == 200
        # 旧 token 失效
        assert client.get('/api/v2/auth/me',
                          headers={'Authorization': f'Bearer {tok}'}).status_code == 401
        # 重新登录被拒
        assert _login(client, uname, 'Test#123456').status_code == 403

    def test_full_lifecycle(self, client, admin_headers):  # TC-SEC-009
        uname = f't_life_{uuid.uuid4().hex[:6]}'
        uid = client.post('/api/v2/auth/users', headers=admin_headers, json={
            'username': uname, 'password': 'Test#123456', 'display_name': '生命周期'}).json()['id']
        assert _login(client, uname, 'Test#123456').status_code == 200
        r = client.put(f'/api/v2/auth/users/{uid}', headers=admin_headers,
                       json={'display_name': '已改名'})
        assert r.status_code == 200
        users = {u['username']: u for u in
                 client.get('/api/v2/auth/users', headers=admin_headers).json()}
        assert users[uname]['display_name'] == '已改名'
        assert client.delete(f'/api/v2/auth/users/{uid}',
                             headers=admin_headers).status_code == 200
        # 软删除：账号转 disabled，登录被拒（设计行为，见 auth.py delete_user 文档字符串）
        r = _login(client, uname, 'Test#123456')
        assert r.status_code == 403 and '停用' in r.json()['detail']

    def test_non_admin_cannot_create_user(self, client, limited):  # TC-SEC-009 权限分支
        r = client.post('/api/v2/auth/users', headers=limited['headers'], json={
            'username': 'should_fail', 'password': 'X#12345678'})
        assert r.status_code == 403


class TestRbac:
    def test_module_guard(self, client, limited):  # TC-SEC-010 无 import 模块权限
        # 上传路由挂 require_module("import")，模块守卫先于文件校验触发
        r = client.post('/api/v2/import/upload', headers=limited['headers'],
                        files={'files': ('a.txt', b'x', 'text/plain')})
        assert r.status_code == 403
        assert 'import' in r.json()['detail']

    def test_store_scope_guard(self, client, limited):  # TC-SEC-011 无 MGK 店铺权限
        r = client.get('/api/v2/query/sales',
                       headers=limited['headers'], params={'store': 'MGK'})
        assert r.status_code == 403

    def test_list_scope_narrowed(self, client, limited, e2e):  # TC-SEC-012 只见 LMG
        r = client.get('/api/v2/query/sales', headers=limited['headers'],
                       params={'period_month': '2026-01'})
        assert r.status_code == 200
        # LMG 数据可见（有销售聚合）
        assert r.json()['total'] >= 1

    def test_empty_scope_empty_result(self, client, empty_scope_user, e2e):  # TC-SEC-013
        r = client.get('/api/v2/query/sales', headers=empty_scope_user['headers'],
                       params={'period_month': '2026-01'})
        assert r.status_code == 200
        assert r.json()['total'] == 0

    def test_non_admin_admin_api(self, client, limited):  # TC-SEC-016
        r = client.get('/api/v2/auth/users', headers=limited['headers'])
        assert r.status_code == 403
        r2 = client.post('/api/v2/import/batches/1/lock', headers=limited['headers'])
        assert r2.status_code == 403


class TestChangePassword:
    def test_wrong_old_password(self, client, admin_headers):  # TC-SEC-014
        r = client.post('/api/v2/auth/change-password', headers=admin_headers,
                        json={'old_password': 'nope', 'new_password': 'X#123456789'})
        assert r.status_code == 400

    def test_change_and_restore(self, client):  # TC-SEC-015
        uname = f't_pwd_{uuid.uuid4().hex[:6]}'
        admin = client.post('/api/v2/auth/login', data={'username': 'admin',
                                                        'password': '123456'}).json()
        ah = {'Authorization': f"Bearer {admin['access_token']}"}
        client.post('/api/v2/auth/users', headers=ah, json={
            'username': uname, 'password': 'Old#123456'})
        tok = _login(client, uname, 'Old#123456').json()['access_token']
        h = {'Authorization': f'Bearer {tok}'}
        assert client.post('/api/v2/auth/change-password', headers=h, json={
            'old_password': 'Old#123456', 'new_password': 'New#123456'}).status_code == 200
        assert _login(client, uname, 'Old#123456').status_code == 401
        assert _login(client, uname, 'New#123456').status_code == 200
    def test_must_change_password_flow(self, client):  # 组11.3 种子账号强制改密
        from conftest import sql_one
        # 1) 种子 admin 登录：初始密码 → 标志为 True（前端据此拦截到强制改密页）
        body = _login(client).json()
        assert body['user']['must_change_password'] is True
        # 2) /me 同样带标志（刷新页面恢复登录态的路径）
        h = {'Authorization': f"Bearer {body['access_token']}"}
        me = client.get('/api/v2/auth/me', headers=h).json()
        assert me['must_change_password'] is True
        # 3) 改密成功 → 标志清除 + 审计留痕
        assert client.post('/api/v2/auth/change-password', headers=h, json={
            'old_password': '123456', 'new_password': 'New#123456'}).status_code == 200
        assert sql_one("SELECT id FROM operation_log "
                       "WHERE username='admin' AND action='change_password' ORDER BY id DESC")
        body2 = _login(client, password='New#123456').json()
        assert body2['user']['must_change_password'] is False
        # 4) 改回初始密码（不污染同会话其他用例）；改密后标志保持 False
        h2 = {'Authorization': f"Bearer {body2['access_token']}"}
        assert client.post('/api/v2/auth/change-password', headers=h2, json={
            'old_password': 'New#123456', 'new_password': '123456'}).status_code == 200
        assert _login(client).json()['user']['must_change_password'] is False

    def test_admin_created_user_not_flagged(self, client, admin_headers):  # 组11.3 边界
        # 管理员建号时密码非初始密码，不应触发强制改密
        uname = f't_noflag_{uuid.uuid4().hex[:6]}'
        client.post('/api/v2/auth/users', headers=admin_headers, json={
            'username': uname, 'password': 'Test#123456'})
        assert _login(client, uname, 'Test#123456').json()['user']['must_change_password'] is False
