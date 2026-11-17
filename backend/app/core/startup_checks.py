# -*- coding: utf-8 -*-
"""启动安全检查（组 11.1 / D-05；platform_startup_enhance 组2 / D9 收敛为 fail-fast）

挂载于 main.lifespan：服务启动时执行。
JWT_SECRET 未显式配置（空白）→ 直接拒绝启动，杜绝"随机密钥导致令牌跨重启失效"
以及"使用公开默认密钥导致令牌可伪造"两类风险。
"""
import logging

from app.core.config import settings

logger = logging.getLogger("startup_checks")


def check_jwt_secret() -> None:
    """未显式配置 JWT_SECRET → 抛出 RuntimeError 拒绝启动

    - 签发/校验逻辑（security.py 读 settings.JWT_SECRET）不变；
    - 不再生成随机密钥（随机值会让全部已签发令牌在重启后失效，且掩盖配置遗漏）；
    - 异常信息只说明配置要求，不包含任何密钥值。
    """
    if settings.JWT_SECRET and settings.JWT_SECRET.strip():
        return
    raise RuntimeError(
        "JWT_SECRET 未配置：请在 backend/.env 中显式配置 JWT_SECRET 后重新启动。"
        "不配置密钥会导致令牌跨重启失效，使用公开默认密钥会导致令牌可被伪造。")


def run_startup_checks() -> None:
    """全部启动检查入口（lifespan 调用）"""
    check_jwt_secret()