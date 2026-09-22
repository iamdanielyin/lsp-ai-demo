# 安装、启动与维护

本手册对应当前仓库实现，不依赖聊天记录。先读 [需求边界](requirements.md)，准备信息见 [快速配置](configuration-checklist.md)。

## 环境与初次安装

- Python 3.11+，依赖固定在 `requirements.txt`；本地验证环境为 Python 3.14.3/macOS。
- macOS/Linux；`run.py` 使用 `fcntl` 文件锁，Windows 使用 WSL。
- SQLite 由 Python 标准库提供，无独立数据库安装；前端无需 npm 构建。Node 仅用于可选语法检查。
- 需要联网访问对应供应商；公网回调需要部署者提供 HTTPS 域名/隧道。本项目不自动创建公网入口。

```bash
git clone https://github.com/iamdanielyin/lsp-ai-demo.git
cd lsp-ai-demo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/init_local.py
.venv/bin/python run.py
```

默认访问 `http://127.0.0.1:8000/settings`。脚本创建 `.env`（权限600），但不输出管理员口令；用本机编辑器读取 `ADMIN_INITIAL_PASSWORD` 后登录。不要截图/提交/粘贴实际口令到聊天。已有 `.env` 时脚本明确退出，不覆盖。

原开发工作目录使用8127端口，入口 `http://127.0.0.1:8127/settings`；这是该机器的 `.env` 选择，克隆仓库后仍默认8000。浏览器测试用8128，不能把该测试端口当作真实 Demo。

也可手动复制 `.env.example` 为 `.env`，用 `.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 生成主密钥并在本机填入。占位值不能用于启动。

## 启动参数

| 参数 | 默认 / 要求 | 如何填写 |
| --- | --- | --- |
| `PORT` | `8000` | 本机未占用的端口，例如8127；改变后重启，访问同一端口 |
| `DATABASE_PATH` | `data/lsp.sqlite3` | SQLite文件路径，相对仓库工作目录或绝对路径；运行用户可写 |
| `FILE_DIRECTORY` | `data/files` | 上传/缓存素材目录；不要用可公开浏览的静态目录 |
| `CONFIG_MASTER_KEY` | 必填；Fernet key | 初始化脚本随机生成；保存后不要随意轮换，旧数据依赖它解密 |
| `ADMIN_INITIAL_PASSWORD` | 首次必填，至少16字符 | 脚本随机生成；只在数据库无管理员时初始化，后续改变量不会重置已有密码 |

配置文件解析是简单 `KEY=value`，不要添加引号、`export` 或内联注释。进程已存在的同名环境变量优先于 `.env`。业务 Token、模型、URL 和测试账号都通过设置页维护，不放入启动参数。

首次 `run.py` 自动创建目录、SQLite表及索引、管理员哈希；不需要单独建库脚本。旧数据库的 `sync_cursor` 字段有启动兼容迁移。数据库和锁文件应保留在同一持久目录。

## 本机日常启动与停止

在仓库根目录运行 `.venv/bin/python run.py`，前台终端用 Ctrl+C 停止。程序监听 `0.0.0.0`，所以局域网可达性取决于机器防火墙；管理API仍需登录。不要直接公开未加 TLS 的8000/8127端口到互联网。

重启前检查消息页是否有“发送中”任务。取消待发不能撤回已提交请求；提交中崩溃会在重启后标为“结果不明”，需在平台核实，不自动重发。检查当前目录/进程后再停止目标进程，不批量终止机器上的 Python 服务。

不要使用 Flask 开发重载器、多个 Gunicorn/Waitress worker 或多副本共享此库。`run.py` 用文件锁阻止同一数据库启动第二个实例；后台处理采用单 worker。锁报错时检查实际已有进程，不通过删除锁文件绕过。

## 公网部署与 Webhook

1. 在受控主机运行同一 `run.py`，持久保存 `.env`、SQLite与媒体目录。
2. 使用部署环境已有的 HTTPS 反向代理或隧道，将公网域名指向该进程。证书由部署者维护；本仓库不包含生产运维编排。
3. 转发时保留原始 Webhook body 和 Host；不要记录请求体、Cookie、Authorization、完整签名媒体路径，也不要公开数据库/上传目录。
4. 在 Demo 保存 `public_base_url=https://<DEMO_HOST>`，无路径。重新从公网 HTTPS 登录；配置此值后Cookie启用 Secure，普通HTTP入口可能无法持续登录。
5. 将设置页的 `https://<DEMO_HOST>/api/webhooks/freshchat` 填到 Freshchat Webhooks，复制对应公钥回 Demo。详细平台操作见 [平台指引](platform-setup.md)。
6. 生成测试码并用测试账号发送。平台真实签名事件、同一对象历史、原渠道回信全部通过后才做实际验收。

当前服务不信任任意 `X-Forwarded-*`；写接口 Origin 仅接受服务 origin 或已保存公网 origin。HTTPS与 Cookie/Origin 的部署行为必须实测，不要通过取消 CSRF 来解决代理配置错误。代码出站请求直接做公网 DNS/IP 验证与 TLS，不承诺支持需要本地HTTP代理的网络环境。

## 配置和备份

保存设置不会自动订阅 Webhook，也不会自动向客户发消息；“开启识别并自动回复”、确认手动发送及真实调度执行才授权相应副作用。重要配置改变会关闭自动回复、使旧检查和测试码授权失效。

备份前停止应用，复制 `.env`、数据库、可能存在的 `-wal`/`-shm` 文件及媒体目录到受限备份存储。主密钥与数据库需要成套恢复，但应分开保护。不要将备份提交 Git。在线直接复制单个SQLite文件可能缺少WAL数据；不提供在线备份自动化。

恢复使用匹配的原主密钥和文件目录，确认权限后启动同一个进程。外发不明任务仍要人工核实。应用没有管理员找回界面或自动密钥轮换；不要删除数据库来假装密码重置成功。

外部调度建议每分钟调用一次 `/api/internal/jobs/drain`，专用 Token、dry-run/real-run 参数和可复制 curl 见 [调度文档](scheduler.md)。不配置调度也可正常进程内处理消息。

## 常见故障

| 现象 | 核对步骤 |
| --- | --- |
| 地址打不开 / 端口占用 | 确认进程仍运行，读取 `.env` 的PORT；`lsof -nP -iTCP:8000 -sTCP:LISTEN` 仅查询，不直接结束未知服务 |
| 登录失败 / 保存403 | 核对首次口令及已有数据库；刷新登录页，检查HTTPS、Secure Cookie和Origin；不把业务Token当登录密码 |
| RSA公钥未配置503 / 签名401 | 从Freshchat Webhooks复制正确公钥；确认是会话Webhook，代理未改body，不使用Freshdesk自动化载荷 |
| 测试码没反应 | 必须在5分钟内发送整段最新码到已接客服账号；检查订阅、公网可达、验签、公开客户角色和真实来源；见测试手册 |
| 识别成功但启动失败 | 查看失败原因及消息页任务；核对平台读取、坐席权限、OpenAI地址/Key/模型；修复配置后重新识别 |
| 平台403/404 | 403核权限；404核对官方区域主机、真实ID和租户API形态。新版Ticket不能填作Freshchat会话 |
| 自定义AI接口失败 | 必须公网HTTPS443、Responses与严格结构化输出兼容；不支持只提供Chat Completions、URL查询鉴权或API重定向 |
| 媒体不可用 / AV_PENDING | 核对上传状态、用途、渠道、大小及白名单；扫描未完成不能标可发；不反复重发结果不明项 |
| 保存后AI关闭 | 这是配置版本失效规则；重新检查/识别，不能直接改数据库强开 |
| 客户没收到但API成功 | 分别核对工作台、渠道限制和设备，不将HTTP2xx当送达；先查证再人工重试 |
| 本地测试通过但客户平台失败 | 本地测试隔离供应商，真实连接器/权限/媒体/模型需另外验收；记录脱敏证据 |

运行和测试命令不需要真实业务密钥进入命令行。提交前使用 [测试手册](testing-guide.md) 的复现步骤。
