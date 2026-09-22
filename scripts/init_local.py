"""Initialize only startup parameters; all business credentials belong in /settings."""
import secrets
from pathlib import Path
from cryptography.fernet import Fernet

path = Path(".env")
if path.exists():
    raise SystemExit(".env 已存在，未覆盖。首次管理员口令只用于创建数据库账户。")
password = secrets.token_urlsafe(24)
path.write_text(f"PORT=8000\nDATABASE_PATH=data/lsp.sqlite3\nFILE_DIRECTORY=data/files\nCONFIG_MASTER_KEY={Fernet.generate_key().decode()}\nADMIN_INITIAL_PASSWORD={password}\n")
path.chmod(0o600)
print("已生成本机 .env（权限600，已由 .gitignore 排除）。")
print("管理员初始口令保存在 .env 的 ADMIN_INITIAL_PASSWORD；请在本机查看后登录。")
