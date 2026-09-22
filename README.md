# LSP-AI 客服 Demo

可运行的 Freshchat → OpenAI Responses → 原会话回复 Demo，配套一个集中设置页和一个消息验证页。

**交付状态：Freshchat 路线实现待租户验证。没有客户凭证，没有真实客户消息或渠道接收证据，核心能力未全部通过。** 新版 Freshdesk Omni Ticket 路线未实现，也没有无效的路线切换开关。客户若已迁移到 Ticket 交互，需要先确认当前租户支持的事件和回复接口，再替换该路线；不能据此宣称新版 Omni 已适配。

## 文档入口

| 想了解什么 | 文档 |
| --- | --- |
| 原始目标、后续需求变更、当前范围及未实现项 | [需求与交付范围](docs/requirements.md) |
| 源码在哪里、技术栈、数据表、API和处理流程 | [技术与代码说明](docs/architecture.md) |
| 新机器安装、启动、登录、公网回调、备份及排错 | [运行手册](docs/runbook.md) |
| 先向管理员要哪些信息 | [快速配置准备单](docs/configuration-checklist.md) |
| 每个参数的默认值、含义、获取和校验 | [全部配置参数手册](docs/configuration-reference.md) |
| Freshchat/Freshdesk上怎么操作 | [平台配置指引](docs/platform-setup.md) |
| WhatsApp/WeChat双账号、AI/人工切换、媒体、工单怎么测 | [测试步骤与本地过程](docs/testing-guide.md) |
| 已完成哪些验证、哪些仍未通过 | [验收报告](docs/acceptance-report.md) / [本地证据](docs/local-evidence.json) |
| 外部调度如何调用 | [调度API](docs/scheduler.md) |

源码随仓库交付，文档链接直接指向对应文件。文档整理日期：2026-09-22；历史官方资料核对日期与当前真实测试状态分别注明。

## 运行

要求 Python 3.11+（本次使用 macOS / Python 3.14.3）。Linux / macOS 支持单进程文件锁；Windows 请使用 WSL。无需 Node 构建、Redis、消息中间件或独立数据库服务。

```bash
git clone https://github.com/iamdanielyin/lsp-ai-demo.git
cd lsp-ai-demo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/init_local.py
.venv/bin/python run.py
```

首次脚本生成 `.env`，权限为 `600`，已有文件不会覆盖。用本机文本编辑器查看其中的 `ADMIN_INITIAL_PASSWORD`，访问 `http://127.0.0.1:8000` 登录。脚本不输出密码。也可复制 `.env.example`，自行填写启动参数；Fernet 主密钥可用以下命令生成：

```bash
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

本次工作目录的本机实例使用 **http://127.0.0.1:8127**，因为 8000 已被其他服务占用。本机 `.env` 已初始化，业务凭证仍为空。后续端口以 `.env` 的 `PORT` 为准。

首次启动自动初始化 SQLite 表，无需手工迁移命令。默认数据库 `data/lsp.sqlite3`、媒体目录 `data/files`。启动脚本使用 Waitress，只有一个后台 worker；相同数据库的第二个进程会被文件锁拒绝。请通过 `run.py` 启动，不使用多 worker WSGI 命令。

启动参数只有端口、数据库路径、文件目录、配置加密主密钥和首次管理员口令。业务 API Key、OpenAI 请求地址、Agent、模型、素材、白名单等均在 `/settings` 配置。首次管理员口令创建后保存为 scrypt 哈希；修改环境变量不会重置已有账户。备份数据库时必须同时安全保管加密主密钥，丢失后无法解密原配置。

## 使用顺序

先看 [快速配置准备单](docs/configuration-checklist.md)。模型、限额、分页、重试等放在高级设置；Webhook、知识素材、工单按测试阶段展开。旧配置中空白模型会补齐默认值，已有非空模型保持不变。

1. **快速连接。** 登录 `/settings`，先填真实官方区域 Freshchat API 地址、Token 和 OpenAI API Key，其余参数已默认，模型为 `gpt-6-astra`。点击“读取坐席”选择专用 Agent，不用另查 ID。先由管理员确认当前新客户交互能由 Freshchat API 读取。如果只能在新 Omni Ticket API 找到它，本 Demo 的租户验证应标记 blocked。
2. **接收事件。** 按 [平台配置指引](docs/platform-setup.md) 设置公网 HTTPS 回调、订阅 Freshchat `message_create`，将验签公钥填入 Demo。保存 Token 不会自动订阅。若平台全量投递，其他客户请求仍会到达验签层，但不会存聊天、读历史或调用 AI。
3. **发送测试码。** 在“测试账号与人工接管”选择 WhatsApp，点击“开启识别并自动回复”。用自己的 WhatsApp 向已接客服账号发送整段随机码，5分钟内有效。Demo 验签后自动读取会话/客户 ID、绑定该账号，读取真实历史并检查 OpenAI，通过后生成第一条 AI 测试问候。失败显示原因，不假标能力通过。无需手工查询 ID；无 Webhook 时仍可展开手动导入。
4. **同时测试两个渠道。** WhatsApp 绑定后，选择 WeChat 生成新码，再用自己的 WeChat 发送。每渠道一个账号，独立保存历史和 AI/人工开关。新增绑定会重新检查已绑定渠道，期间暂缓 AI。开关关闭或坐席回复时取消该账号待发 AI，另一渠道继续运行；恢复只处理新消息。结束所有测试会清空范围。随机码是临时授权口令，不能公开或发给真实客户；固定昵称和固定标记不能绑定。
5. **手动文本。** 在 `/conversations` 输入文本，可连续加入多条，再预览确认渠道、会话和内容后发送。每条独立 POST、独立任务。手动提交进入人工模式。API 返回消息 ID 后，在任务中分别记录原 Omni 工作台可见及客户原渠道收到的脱敏证据。
6. **媒体。** 在同一个设置页上传 JPEG/PNG、MP4、PDF，填写素材 ID、用途、渠道授权。图片/文件需点击上传平台；文件仅 `SAFE_FILE` 可发送，`AV_PENDING` 保持扫描中。视频不使用不存在的上传接口。审核 HTTPS URL 会先下载为固定本地版本，与上传视频一起通过应用公网的24小时签名地址发送，避免远程内容被替换。远程 URL 下载及历史媒体预览需要精确主机白名单。
7. **模型预览。** 默认模型已按2026-09-18官方文档填为 `gpt-6-astra`。连接区域可修改 OpenAI API 请求地址，默认 `https://api.openai.com/v1`，自定义服务须支持 Responses 和结构化输出。点击“保存并检查 OpenAI”（真实小额调用）；账号无权限时在高级设置换成可用模型。配置繁体中文客服知识，点击会话中的“生成预览”。预览只保存计划，绝不外发。模型输入不包含内部备注或媒体二进制，不执行 OCR、视频理解或文件理解。
8. **自动回复与验收。** 测试码流程是管理员主动授权的限定账号试运行：真实入站、历史和 OpenAI 检查通过后自动启动文本回复，不再要求先完成手动文本验收。矩阵仍保留手动文本、工作台显示、客户送达的真实未验证状态；普通手工绑定流程仍要求这些检查。历史不逐条补发，AI 媒体仍须逐渠道通过对应类型三层验证。
9. **工单。** 设置 Freshdesk 官方域名/API Key、真实 `requester_id` 映射及自定义必填字段。在会话中创建或复用工单。默认每个会话复用当前跟进事项；明确选择新事项才另建。规则自动建单还需模型原因精确匹配允许原因。工单只保存本地映射与摘要，不承诺原生双向关联。

## 实现和验证边界

- 前端为原生 HTML/CSS/JS，后端为 Flask + SQLite；无打包链、无通用插件体系。设置、消息片段、素材平台引用、任务计划和证据使用 Fernet 加密保存。平台 ID 等索引元数据及本地文件本身不进行整库/磁盘加密；部署应限制目录权限并使用磁盘加密。
- 管理接口要求管理员 Cookie 会话，写操作要求 CSRF Header 和合法 Origin。会话 Cookie 为 HttpOnly / SameSite=Strict；配置公网地址后启用 Secure。公网部署必须 HTTPS，先配置真实公网入口再在该入口重新登录。不要把真实凭证填入聊天、命令行或浏览器持久化存储。
- Freshchat Webhook 按原始 body 字节进行 RSA PKCS#1 v1.5 / SHA-256 验签。入站记录、去重、人工模式变化与待处理任务在一次 SQLite 事务中提交后返回成功，网络调用在后台执行。拒绝签名失败或无法持久化的请求。
- 验签后先按测试身份过滤，非消息事件及范围外消息直接返回忽略，不写入事件/消息/会话/任务表。已知会话的坐席回调可用已核实客户关联匹配；身份冲突拒绝。手动导入仍是管理员明确发起的读取，不会自动放开测试范围。
- 自动任务发送前重新检查配置版本、白名单、会话模式及最新客户消息。人工接管取消待发任务，已处于提交阶段的请求无法撤回。
- 外发超时、HTTP 5xx、无消息 ID、进程提交后退出标为 `unknown`，绝不自动重发。明确 HTTP 429 可限次退避；平台明确拒绝则失败。计划第二条失败不重发第一条，后续暂停。人工重试必须记录核实依据并承认重复风险。
- 历史初始化遍历至空页并按消息 ID 去重；增量游标来自上次成功 API 同步，不能由最新 Webhook 消息推进。页面显示同步范围、错误和分页；完整是“平台保留且当前凭证可访问”，不是恢复已删除历史。
- 模型使用 `POST /v1/responses`、`store:false` 和严格 `text.format.json_schema`，读取所有有效输出片段，处理 refusal/incomplete/非法结果。上下文按 UTF-8 字节做保守 Token 上界估计，优先保留最新完整消息，显示范围和截断；可能比精确 tokenizer 更早截断。输出预算独立预留，账号模型实际上下文上限仍需实测。
- API 受理、Omni 工作台可见和客户原渠道收到分层记录；人工确认不会把任务伪装为“机器回执送达”。未实现 Freshchat 渠道专有 delivery receipt 事件，因此正常成功状态为“平台已受理”。
- 素材上传校验扩展名、文件魔数、MIME 与大小；允许 JPEG/PNG/MP4/PDF，不允许 HTML/SVG 脚本附件执行。魔数校验不等于恶意文件扫描，PDF 等仍依赖平台扫描和管理员审核。浏览器预览只走授权同源接口；媒体网络请求校验主机、DNS 及每次重定向，并固定连接已验证的公网 IP。
- 视频签名链接24小时有效，绑定单个素材及当前租户，停用后失效，不含任何平台/API 凭证。平台在此期间抓取，不代表其转发渠道一定支持视频。
- 生产并发不在本 Demo 范围：单进程单 worker 全局顺序处理，慢模型会阻塞其他会话任务，但不会阻塞 Webhook 持久化。后续有实际吞吐需求再改每会话串行、跨会话并行。
- Freshchat/Freshdesk API 主机仍限制为代码列出的官方域名后缀。OpenAI 请求地址支持管理员配置的公网 HTTPS（443）基础地址或完整 `/responses` 地址；仅填写域名时补 `/v1`，留空恢复官方默认。请求前校验全部 DNS 结果并固定公网 IP，拒绝 URL 凭证、查询参数、私网与 API 重定向。密钥及模型上下文会发给所配置服务，应使用该服务对应的 Key。自定义服务需兼容 Responses 和严格结构化输出，不能用仅支持 Chat Completions 的接口替代。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
.venv/bin/python -m compileall -q lsp run.py scripts tests
```

自动化测试使用临时数据库和合成供应商响应，不读取本机业务配置、不访问真实供应商。覆盖核心成功/错误分支和回归项，详见 [验收报告](docs/acceptance-report.md)。Node 只用于可选 JS 语法检查，运行应用不需要 Node。

可选的独立 UI 测试服务器：

```bash
.venv/bin/python tests/browser_app.py
```

该脚本只监听 `127.0.0.1:8128`，使用临时数据库、`LOCAL_TEST_` 会话和模型替身，密码 `LOCAL-BROWSER-TEST-ONLY`。**不是产品演示数据，不得用其截图或结果冒充真实 API 验收。** 正常 `run.py` 不加载该测试文件，也不提供模拟模式。浏览器验证应使用已有工具，本项目不自动安装浏览器。

双渠道测试码/人工接管专用夹具为 `.venv/bin/python tests/pairing_browser_app.py`，也占用8128，两者不能同时启动。如何生成合成签名消息、核对独立开关与实际本地测试过程见 [测试手册](docs/testing-guide.md)。

## 交付文件

- [提前准备哪些配置、在哪里获取](docs/configuration-checklist.md)
- [平台和设置配置步骤](docs/platform-setup.md)
- [中文调度接口文档](docs/scheduler.md)
- [验收报告与 T01–T17 矩阵](docs/acceptance-report.md)
- [官方资料与实现取舍](docs/api-evidence.md)
- [可填的脱敏验收记录](docs/tenant-evidence-template.json)

数据库、上传文件、启动密钥、浏览器会话及本机截图均不应提交到版本库；`.gitignore` 已排除相关目录。文档证据只写平台 ID 占位符、状态、耗时、Token 数及脱敏截图引用，不放原始客户聊天或凭证。
