# 快速开始：只准备核心信息

开发机设置入口：http://127.0.0.1:8127/settings 。新克隆默认8000，实际以PORT为准。密钥直接填写设置页，不发到聊天或版本库。配置更新：2026-09-22；官方资料核对日期：2026-09-18。每项参数完整说明见 [参数手册](configuration-reference.md)，安装见 [运行手册](runbook.md)。

## 先找三项信息

| 设置页字段 | 配置参数 | 去哪里获取 |
| --- | --- | --- |
| 区域 API 地址 | `platform_api_base_url` | 客户 Freshchat 管理员提供实际官方区域 API 地址，HTTPS，不带 `/v2`；不根据工作台网址猜测 |
| Freshchat API Token | `freshchat_token` | Freshchat **Admin → CONFIGURE → API Tokens → Generate Token**；需要会话读取/发送与素材上传权限 |
| OpenAI API Key | `openai_api_key` | [OpenAI API keys](https://platform.openai.com/api-keys)，在有可用 API 额度的项目创建普通应用密钥 |

**OpenAI API 请求地址**也在连接区域，可直接修改 `openai_base_url`：默认 `https://api.openai.com/v1`；可填写自定义公网 HTTPS 基础地址，如 `https://gateway.example/v1`，也可直接填写 `https://gateway.example/v1/responses`。仅填写域名时补 `/v1`，留空恢复默认，结尾斜杠自动去除。请配套使用该服务的 API Key，并确认它支持 Responses 与严格结构化输出。地址更换后检查失效、自动回复关闭，需要重新检查。

填完后可以直接“保存并检查 OpenAI”。模型默认 **`gpt-6-astra`**，来自当前[官方模型目录](https://developers.openai.com/api/docs/models)；[模型页](https://developers.openai.com/api/docs/models/gpt-6-astra)列出 Responses 和 Structured Outputs 支持。该默认值是本次核对版本，不会在后台自动追逐新模型。实际账号权限仍由一次小额检查确认；如没有权限，在高级设置修改模型。

**路线前提：当前租户的新消息必须可由 Freshchat conversation API 读取。** 当前 Demo 尚未实现新版 Omni Ticket 会话路线；该区别需客户管理员确认。

## 先配置 Webhook，再用测试码自动绑定

1. 点击 **读取坐席**，选择有权回复测试会话的 Agent。
2. 部署人员提供公网 HTTPS 地址，填入 `public_base_url`。在 Freshchat **设置 → Webhooks** 中填写 Demo 生成的 `https://<DEMO_HOST>/api/webhooks/freshchat`，启用 `message_create`；将平台公钥填入 `freshchat_public_key`。菜单以租户版本为准，不能使用 Freshdesk 工单自动化代替。
3. 在 **测试账号与人工接管** 选择 WhatsApp，点击 **开启识别并自动回复**。用自己的 WhatsApp 向客户已接通的客服账号发送页面显示的随机码，5分钟内有效。
4. Demo 验签后自动获取客户和会话 ID，绑定该账号、同步历史、真实检查 OpenAI，然后生成 AI 测试问候。无需手填两个 ID。失败在同一区域显示；不会把失败当作启用成功。
5. 再选择 WeChat 生成新码，用自己的 WeChat 发送。两个渠道各一个账号，可同时测试；不合并客户 ID 或历史。每次生成的新码替换尚未使用的旧码，已绑定的其他渠道账号保留。
6. 每个渠道都有 **AI 自动回复** 开关，关闭即人工接管；也可直接在消息页手动回复，或由 Omni 坐席回复触发暂停。另一渠道继续运行。恢复后只处理新消息，已提交平台的请求无法撤回。
7. **结束所有测试** 会清空全部测试账号、停止自动回复和待启动任务。新增或替换账号会重新检查已绑定渠道，期间暂缓 AI；配置变更使原检查和测试授权失效，需重新识别。

随机码是一次性临时授权口令，只发给自己的测试账号。昵称、电话号码、固定 `LSP-DEMO-TEST-001` 标记都不作为绑定授权。只有公开客户消息可匹配；坐席、私有备注、错码、过期码和未验签事件不能绑定。码使用后仅保存摘要，其他账号复制已使用的码也不能绑定。

快速测试允许限定账号在尚未完成手动文本/客户送达验收前试运行，能力矩阵不会因此自动标通过；图片、视频和文件的自动发送仍需该渠道对应媒体验收。WeChat 必须使用实际连接器验证。

未开启识别且无已绑定账号时，所有事件忽略。其他客户的网络请求若由平台全量投递，仍会到达 Demo 并验签；应用过滤不等于网络隔离或已通过压力测试。测试前协调目标会话上的既有机器人和自动回复。

无 Webhook 时可展开 **已有会话？手动导入或绑定**，由管理员提供 `conversation_id`；客户 ID 可以留空。该旧流程的“只用此客户测试”会替换整个范围，不用于同时保留两个渠道。

## 媒体和工单，测到时再准备

- **媒体和业务知识：** 获准使用的 JPEG/PNG 位置图、MP4 指引、PDF 方案，以及停车地址/FAQ 文本。在“知识与素材”填写；上传 PDF 不会自动解析成知识。当前视频发送需要上述公网 HTTPS 地址。远程素材或客户媒体预览的实际主机在高级设置中授权。
- **工单：** 只需 Freshdesk 官方域名、API Key、测试客户 requester 映射。Key 位于 **右上角头像 → Profile settings → API Key**；requester ID 从真实 Contact / 联系人 API 核对，不能用 Freshchat user ID 代替。租户额外必填字段在高级设置填写。
- **外部调度：** 测试恢复/清理 API 时才生成独立 `scheduler_token`，见 [调度说明](scheduler.md)。正常消息处理无需先配调度中心。

## 已填好的默认值

| 项目 | 默认值 |
| --- | --- |
| 接口路线 / OpenAI 地址 | Freshchat / `https://api.openai.com/v1` |
| 模型 | `gpt-6-astra`；旧配置未填模型时自动补齐，已指定的模型保留 |
| 客服说明 | 繁体中文停车客服测试规则；不虚构交易或媒体内容 |
| OpenAI 项目 / 组织 | 留空，使用 API Key 所属权限范围 |
| 自动回复 / 工单策略 | 自动回复关闭 / 手动建单 |
| 防抖 / 最多回复条数 | 1500 毫秒 / 3 条 |
| 输入 / 输出预算 / 超时 | 12000 / 4096 tokens / 60 秒 |
| AI 频率 / 每日预算 | 每分钟10次 / 每日100000 tokens |
| 历史分页 / 本地保留 | 50 条 / 7 天 |
| MIME / 媒体大小 | JPEG、PNG、MP4、PDF；图片5 MB、视频16 MB、文件20 MB，真实渠道更小限制优先 |
| 工单优先级 / 状态 / 标签 | 低 / 开放 / `lsp-ai-demo` |
| Responses 存储 | 固定 `store:false` |

OpenAI 请求地址在连接区域直接展示，其余运行参数在折叠的高级设置中保留，不要求你提前研究。模型留空保存也会恢复默认值。参数变化仍会使相关检查失效并关闭自动回复。

不需要额外获取 Meta App Secret、WhatsApp Cloud API Token 或微信开发者密钥；原渠道继续由 Omni 接入。本机 `.env` 启动参数已经初始化，无需为业务配置更换加密主密钥。

官方获取依据：[Freshchat API](https://developers.freshchat.com/api/) · [Freshchat Webhook](https://support.freshchat.com/support/solutions/articles/239404-freshchat-webhooks-payload-structure-and-authentication) · [Freshdesk API](https://developers.freshdesk.com/api/) · [OpenAI Quickstart](https://developers.openai.com/api/docs/quickstart)。已通过 Context7 与官方原文核对；菜单随租户形态可能不同，真实租户能力仍待验证。
