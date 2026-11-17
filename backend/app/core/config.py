"""核心配置 — 环境变量驱动"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 加载 backend/.env（与旧系统同目录约定）
# config.py 位于 backend/app/core/ → parents[0]=core, [1]=app, [2]=backend
BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


class Settings:
    """全局配置（简单对象，避免 pydantic-settings 额外依赖行为差异）"""

    # ---- 数据库 ----
    # 新系统使用独立库 lmg_erp，绝不触碰旧库 lmg_platform（仅迁移脚本只读访问）
    DB_HOST: str = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "lmg_erp")
    # 旧库（只读，仅供主数据迁移脚本使用）
    LEGACY_DB_NAME: str = os.getenv("LEGACY_DB_NAME", "lmg_platform")

    # ---- AI（与旧系统相同的环境变量名，便于无缝沿用 .env）----
    # 默认值以此处为唯一权威（组 9.2 D-06 配置单点治理）；api/services 层不得再 os.getenv 重复读取
    AI_API_KEY: str = os.getenv("AI_API_KEY", "")
    AI_BASE_URL: str = os.getenv("AI_BASE_URL", "https://api.deepseek.com/anthropic")
    AI_MODEL: str = os.getenv("AI_MODEL", "deepseek-v4-pro")
    AI_PROVIDER: str = os.getenv("AI_PROVIDER", "anthropic")
    AI_MAX_TOKENS: int = int(os.getenv("AI_MAX_TOKENS", "50000") or 50000)
    AI_THINKING_BUDGET: int = int(os.getenv("AI_THINKING_BUDGET", "32000") or 32000)  # 思考预算，需 < max_tokens
    AI_MODELS: str = os.getenv("AI_MODELS", "deepseek-v4-pro,deepseek-v4-flash")  # 可选模型白名单（逗号分隔）
    ADS_MEMORY_ROUNDS: int = int(os.getenv("ADS_MEMORY_ROUNDS", "40") or 40)
    ADS_REPORTS_ROOT: str = os.getenv("ADS_REPORTS_ROOT", r"E:\项目\ads_agent\data\reports")

    # ---- JWT ----
    # 组11.1（D-05）：无代码默认值 —— 未显式配置时 startup_checks 生成随机密钥
    # 并打 ERROR 告警（令牌跨重启失效；签发/校验逻辑不变）
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "720") or 720)

    # ---- 上传 / 归档 ----
    UPLOAD_DIR: Path = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
    ARCHIVE_DIR: Path = Path(os.getenv("ARCHIVE_DIR", str(BASE_DIR / "archive")))  # 原始 xlsx 归档落盘
    MAX_UPLOAD_FILES: int = int(os.getenv("MAX_UPLOAD_FILES", "30") or 30)

    # ---- 导入任务 ----
    IMPORT_BATCH_ROWS: int = int(os.getenv("IMPORT_BATCH_ROWS", "2000") or 2000)  # 单事务批量行数
    RAW_ROW_MONTHS: int = 36  # raw_sheet_row 按月分区预建月数

    # ---- 通用后台任务（task_worker，组6）----
    # 核算并发信号量：同时最多 2 个月份核算（导入不限流）
    ACCOUNTING_CONCURRENCY: int = int(os.getenv("ACCOUNTING_CONCURRENCY", "2") or 2)
    # 完成态任务保留时长（超时惰性删除，submit 时触发）
    TASK_RETENTION_HOURS: int = int(os.getenv("TASK_RETENTION_HOURS", "24") or 24)
    # sys_task 条数上限（超出删除最早完成态任务）
    TASK_MAX_RECORDS: int = int(os.getenv("TASK_MAX_RECORDS", "500") or 500)

    # ---- 性能（组7）----
    # 请求级结果缓存（纯读聚合接口；数据版本号 = 导入/核算 max id，天然失效）
    CACHE_TTL_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", "60") or 60)
    CACHE_MAX_ENTRIES: int = int(os.getenv("CACHE_MAX_ENTRIES", "512") or 512)
    # /query/sales JOIN 下推开关（true=SQL 端计算净利排序分页；执行计划劣化时置 false 回退）
    PERF_SALES_PUSHDOWN: bool = os.getenv("PERF_SALES_PUSHDOWN", "true").strip().lower() in ("1", "true", "yes")

    # ---- 契约（组8，D-01/D-02）----
    # 旧字段别名注入开关（过渡期默认开；前端全部切换规范字段并冒烟通过后置 false 下线）
    LEGACY_CONTRACT_ALIAS: bool = os.getenv("LEGACY_CONTRACT_ALIAS", "true").strip().lower() in ("1", "true", "yes")

    # ---- 数据治理（组10，D-03）----
    # 费用月份过滤等值匹配（组 10.3）：存量带时间戳值清洗完毕且等值/前缀聚合
    # 一致性验证通过后默认 true；如遇异常可置 false 回退前缀匹配（无需改码）
    MONTH_EXACT_MATCH: bool = os.getenv("MONTH_EXACT_MATCH", "true").strip().lower() in ("1", "true", "yes")

    # ---- 安全（组11，D-05）----
    # CORS 显式白名单（逗号分隔；空配置拒绝启动 fail-fast）
    # 默认含本机前后端端口：5180（本项目 dev）与 5173（vite 缺省）
    CORS_ORIGINS: str = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5180,http://127.0.0.1:5180,"
        "http://localhost:5173,http://127.0.0.1:5173")

    @property
    def CORS_ORIGIN_LIST(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # ---- 映射审核增强（mapping_review_enhance）----
    # 07 基准产品信息表目录（默认用户整理后的桌面临时目录，可用环境变量覆盖）
    MAPPING_BASELINE_DIR: str = os.getenv(
        "MAPPING_BASELINE_DIR", r"C:\Users\五包辣条\Desktop\汇总\07")
    # 映射相似度匹配阈值（0~1，越高越严格；配合唯一命中判定使用）
    MAPPING_SIMILARITY_THRESHOLD: float = float(
        os.getenv("MAPPING_SIMILARITY_THRESHOLD", "0.85") or 0.85)

    @property
    def AI_MODEL_LIST(self) -> list[str]:
        """可选模型白名单（前端模型选择器）；未配置 AI_MODELS 时回退默认单模型"""
        models = [m.strip() for m in self.AI_MODELS.split(",") if m.strip()]
        return models or [self.AI_MODEL]

    @property
    def ADS_MEMORY_LIMIT(self) -> int:
        """会话工作记忆消息条数上限（轮数 × 2：一轮 = 用户 1 条 + AI 1 条）"""
        return self.ADS_MEMORY_ROUNDS * 2

    @property
    def DATABASE_URL(self) -> str:
        """主库连接串（lmg_erp）"""
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    @property
    def LEGACY_DATABASE_URL(self) -> str:
        """旧库连接串（lmg_platform，只读，仅迁移脚本使用）"""
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.LEGACY_DB_NAME}?charset=utf8mb4"
        )


settings = Settings()
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
settings.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
