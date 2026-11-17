"""数据库引擎与会话 — SQLAlchemy 2.0

设计要点（维护必读）：
  1. **autocommit=False + autoflush=False**：事务由业务代码显式 commit。
     注意 autoflush=False 的副作用——`db.add()` 之后**不 flush 就查不到刚加的记录**，
     所以"先查后插"的去重逻辑必须自己维护内存缓存（见 importer/pipeline.py 的成本/汇率处理），
     否则会撞唯一键。
  2. **两个库严格隔离**：
       - `lmg_erp`     新系统主库（读写）
       - `lmg_platform` 旧系统库（**只读**，仅主数据迁移脚本使用，绝不可写入）
  3. engine 配置 `pool_pre_ping=True`：MySQL 空闲断连后自动重连（内网/远程库常见问题）。
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from app.core.config import settings

# 主库连接池：pre_ping 防断连；recycle 一小时回收，避免 MySQL wait_timeout 断链
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI 依赖注入：每请求一个会话，用完必关（防止连接泄漏）"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_legacy_db():
    """旧库(lmg_platform)只读会话 — 仅迁移脚本使用

    ★ 该会话**只允许 SELECT**。新系统任何业务逻辑都不应依赖旧库。
    """
    legacy_engine = create_engine(settings.LEGACY_DATABASE_URL, pool_pre_ping=True)
    LegacySession = sessionmaker(autocommit=False, autoflush=False, bind=legacy_engine)
    db = LegacySession()
    try:
        yield db
    finally:
        db.close()
        legacy_engine.dispose()


def ensure_database():
    """确保目标库存在（幂等）

    先连到服务器（URL 不带库名），执行 CREATE DATABASE IF NOT EXISTS，
    这样首次部署无需手工建库。随后 Base.metadata.create_all() 建表。
    """
    server_url = (
        f"mysql+pymysql://{settings.DB_USER}:{settings.DB_PASSWORD}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/?charset=utf8mb4"
    )
    server_engine = create_engine(server_url, pool_pre_ping=True)
    with server_engine.connect() as conn:
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{settings.DB_NAME}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
        ))
        conn.commit()
    server_engine.dispose()
