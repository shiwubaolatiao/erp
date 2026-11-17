# -*- coding: utf-8 -*-
"""JWT_SECRET 启动阻断测试（platform_startup_enhance 组2 / D9）

覆盖：
  · 已配置（非空白）→ check_jwt_secret() 正常返回
  · 空字符串 / 纯空白 → RuntimeError 拒绝启动
  · 失败后不生成随机密钥、不残留任何回退值
  · 异常信息不包含密钥内容、不含随机生成逻辑痕迹
  · pytest 专用密钥下 TestClient 可进入 lifespan（不影响既有 219 用例）
"""
import pytest

from app.core.config import settings
from app.core.startup_checks import check_jwt_secret


def test_configured_secret_passes(monkeypatch):
    monkeypatch.setattr(settings, 'JWT_SECRET', 'x' * 48)
    check_jwt_secret()  # 不应抛出


def test_empty_secret_rejected_and_no_random_fallback(monkeypatch):
    monkeypatch.setattr(settings, 'JWT_SECRET', '')
    with pytest.raises(RuntimeError):
        check_jwt_secret()
    # 关键：不再回退生成随机密钥（旧行为会让令牌跨重启失效）
    assert settings.JWT_SECRET == ''


def test_whitespace_secret_rejected(monkeypatch):
    monkeypatch.setattr(settings, 'JWT_SECRET', '   \t  ')
    with pytest.raises(RuntimeError):
        check_jwt_secret()


def test_error_message_mentions_config_without_secret_value(monkeypatch):
    monkeypatch.setattr(settings, 'JWT_SECRET', '')
    with pytest.raises(RuntimeError) as exc:
        check_jwt_secret()
    msg = str(exc.value)
    assert 'JWT_SECRET' in msg
    assert '.env' in msg
    # 不应再出现随机生成相关实现痕迹
    assert 'token_urlsafe' not in msg


def test_pytest_dedicated_secret_is_set():
    """conftest 已在导入 app.* 前注入 pytest 专用密钥，保证测试环境可启动"""
    assert settings.JWT_SECRET
    assert settings.JWT_SECRET.strip()


def test_testclient_lifespan_starts(client):
    """TestClient 进入 lifespan 后 /health 正常（JWT 已配置时行为不变）"""
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json()['app'] == 'LMG-ERP'
    assert r.json()['status'] == 'ok'