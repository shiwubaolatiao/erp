"""LMG ERP v2 后端启动器
读取 .env，建库建表，启动 uvicorn（默认 8020 端口，与旧系统 8000 并行不冲突）
"""
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

if __name__ == '__main__':
    import uvicorn
    host = os.getenv('ERP_HOST', '0.0.0.0')
    port = int(os.getenv('ERP_PORT', '8020'))
    reload_flag = '--reload' in sys.argv
    print(f"启动 LMG ERP v2 → http://{host}:{port}  (接口文档 /docs)")
    uvicorn.run('app.main:app', host=host, port=port, reload=reload_flag,
                log_level='info')
