import os
import fcntl
from pathlib import Path

from waitress import serve
from lsp.app import create_app, load_env


if __name__ == "__main__":
    load_env()
    database = Path(os.getenv("DATABASE_PATH", "data/lsp.sqlite3"))
    database.parent.mkdir(parents=True, exist_ok=True)
    process_lock = open(str(database) + ".lock", "a")
    try:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("同一数据库已有 Demo 进程运行；请勿启动多个 worker。") from None
    app = create_app()
    port = int(os.getenv("PORT", "8000"))
    print(f"LSP-AI Demo: http://127.0.0.1:{port} — Freshchat 实现待租户验证", flush=True)
    # Single process: one persisted queue and one worker. No development reloader.
    serve(app, host="0.0.0.0", port=port, threads=8)
