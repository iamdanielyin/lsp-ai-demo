# 全部配置参数手册

日期：2026-09-22。以 [配置源码](../lsp/settings.py)、[页面字段](../static/app.js) 的当前实现为准。日常先看 [快速开始](configuration-checklist.md)，启动环境变量见 [运行手册](runbook.md)。本文件不预填真实租户、客户、密钥或附件。

## 填写规则

- 所有业务配置在 `/settings`。常用项在“连接平台和AI”“测试账号与人工接管”“接收新消息”“知识与素材”“跟进工单”“开启自动回复”；剩余项在同页“高级设置”。
- 列表控件输入英文/中文逗号分隔内容；JSON控件必须使用标准JSON双引号；数值按表中单位填写。
- 密钥保存后只回显“已设置”及掩码；留空保持原值，勾选“明确清除已保存凭证”才删除。设置 API 的 `clear_secrets` 是显式清除列表，不是普通持久配置。
- 修改影响行为的配置会增加版本、关闭自动回复、取消旧任务并使旧能力检查/测试绑定失效。`auto_reply_enabled`、`scheduler_token`、`local_retention_days`、`seed_conversation_id`、`seed_user_id` 不增加版本，但关闭自动回复仍取消待发 AI。
- “保存”不向客户发送；“保存并检查OpenAI”有真实模型用量；“监听下一批会话”只产生5分钟脱敏候选，点选后 AI 仍需在单个会话明确恢复。

## 平台连接及身份

| 参数 | 默认 / 必填条件 | 获取与填写方式 / 校验 |
| --- | --- | --- |
| `integration_profile` | `freshchat`，只读 | 当前已实现路线。没有Ticket会话路线开关 |
| `platform_api_base_url` | 空；平台调用必填 | 管理员从实际租户API资料确认区域主机。HTTPS443，**不带 `/v2`**；代码只允许指定Freshchat官方域名后缀，不猜工作台域名 |
| `freshchat_token` | 空；平台调用必填，敏感 | Freshchat `Admin → CONFIGURE → API Tokens → Generate Token`；菜单可能随租户变化。需能读历史、回复并按需上传素材。由后端加Bearer |
| `reply_actor_id` | 空；发送/启动测试必填 | 点击“读取坐席”，从真实 `/v2/agents` 结果选择；推荐专用测试Agent。读列表无权限时由管理员提供ID。不能填昵称/邮箱替代 |
| `source_mapping` | `{}`；自动发送前需有真实来源 | JSON：`{"<OBSERVED_SOURCE>":"WhatsApp"}`。测试码自动把观察到的 `message_source` 与所选渠道关联。`channel_id` 是Topic等平台字段，不据此推断渠道 |
| `allowed_channels` | `[]` | 测试码按已绑定渠道自动维护。手填如 `WhatsApp, WeChat`，必须已经存在来源映射；不能包含unknown |
| `test_identity_allowlist` | `[]` | 测试码自动写入平台ID。手工格式 `user:<PLATFORM_USER_ID>` 或 `conversation:<PLATFORM_CONVERSATION_ID>`。昵称/号码不授权。双渠道还按用户和来源组合检查；空白时仅有效识别码可启动绑定 |
| `seed_conversation_id` | 空，可选 | 手动导入的初始真实Freshchat会话ID；测试码流程无需填写。不是Freshdesk Ticket编号或Demo本地整数ID |
| `seed_user_id` | 空，可选 | 手动导入同用户其他会话时用真实Freshchat user ID；不是requester ID、昵称、手机号或微信号 |

Freshchat/Freshdesk官方域名后缀的当前允许列表见 `lsp/security.py`，主机需实际租户支持。非典型官方主机应先核实再改代码，不能为绕过校验填一个错误域名。

## Webhook

| 参数 | 默认 / 必填条件 | 获取与填写方式 / 校验 |
| --- | --- | --- |
| `public_base_url` | 空；收真实Webhook、自动测试、本地视频抓取必填 | 部署者提供 `https://<DEMO_HOST>`，仅origin，无路径/查询。支持HTTPS443；不能localhost、内网或元数据地址。不是Freshchat域名 |
| `webhook_path` | `/api/webhooks/freshchat`，只读派生字段 | 完整地址由页面拼接复制到Freshchat **设置 → Webhooks**；订阅 `message_create`。不能编辑或PUT保存此字段 |
| `freshchat_public_key` | 空；接收Webhook必填 | Freshchat Webhooks页面提供的RSA公钥，PEM或Base64 DER，至少2048位，最长20000字符。不是私钥、API Token或共享密码 |

验签公钥支持 `BEGIN PUBLIC KEY`、`BEGIN RSA PUBLIC KEY` 的 PEM，以及可换行的 Base64 DER；可原样粘贴平台公钥，无需手工改标头。程序按实际编码解析，只接受至少2048位的 RSA 公钥，拒绝私钥、损坏数据及其他密钥类型。

Freshchat请求应包含 `X-Freshchat-Signature`；程序记录 `X-Freshchat-Payload-Version` 和 `X-Retry-Count`，重试次数不代替消息去重。当前无需、也不显示 `freshdesk_webhook_secret`，因为没有实现Ticket事件路线。首次设置公钥前的回调会503；配置后再发送测试码。

## OpenAI 与客服内容

| 参数 | 默认 / 必填条件 | 获取与填写方式 / 校验 |
| --- | --- | --- |
| `openai_api_key` | 空；AI必填，敏感 | 官方 [API Keys](https://platform.openai.com/api-keys) 创建有额度项目的Key；用自定义服务时填该服务对应Key，后端Bearer认证 |
| `openai_base_url` | `https://api.openai.com/v1` | 首屏可改。接受公网HTTPS443基础地址 `https://gateway.example/proxy/v1` 或完整 `/responses`。基础地址后端追加 `/responses`；仅origin补 `/v1`；留空恢复默认。禁止查询参数、URL凭证、片段、内网DNS、API重定向；仅支持Responses协议 |
| `openai_model` | `gpt-6-astra`；留空恢复 | 项目预设模型ID，历史资料核对见API依据，不意味着当前账号一定有权用，也不持续自动选择最新型号。高级设置可改为本项目可用且支持严格结构化输出的模型，实际小调用检查 |
| `openai_project` | 空，可选 | 项目要求时，从账号项目设置填写真实Project ID；有值才发送 `OpenAI-Project`，不自动发现其他项目 |
| `openai_organization` | 空，可选 | 账号要求时填写真实Organization ID；有值才发送 `OpenAI-Organization`。非组织名称 |
| `system_instructions` | 内置繁体中文停车测试客服说明 | 按获准业务修改，最多20000字符；后端另外追加不可虚构交易/不能执行任意URL/素材选择等边界 |
| `knowledge_text` | 空，可选 | 普通文本，最多100000字符；输入获准停车地址、方案、FAQ和人工联系说明。不上传PDF自动解析，不接向量库 |
| `max_output_tokens` | `4096`；128–32768 | 输出预算，按实际模型调整；包含推理等消耗，过小可能incomplete |
| `context_token_budget` | `12000`；1000–200000 | 输入预算，程序用UTF-8字节保守上界估算，超预算保留较新完整历史；不同于UI读全历史，输出预算单独预留 |
| `model_timeout_seconds` | `60`；5–180秒 | 后端模型请求超时；失败记录任务错误，不给客户发送原始异常 |
| `store` | 固定 `false`，只读 | 每次Responses显式发送；不启用Responses应用状态存储，不承诺供应商零日志留存 |

自定义服务会收到该请求的Key、选中客户上下文和知识内容。填与服务配套的Key，不把官方Key盲目交给不相关主机；这是实际配置目的地，不是浏览器代理。

## AI 策略与运行额度

| 参数 | 默认 / 范围 | 填写说明 |
| --- | --- | --- |
| `auto_reply_enabled` | `false` | 全局总开关。快速测试码流程在历史/模型检查后启用；普通手工绑定需要当前渠道入站、历史、手动文本及OpenAI检查。各测试渠道独立开关控制账号人工暂停，不需要反复编辑此值 |
| `debounce_ms` | `1500`；0–10000毫秒 | 短时间追加消息合并；新消息取消未发旧计划 |
| `max_reply_messages` | `3`；1–10条 | 单个模型计划和手工提交的服务器条数上限；独立任务逐条发送 |
| `ai_requests_per_minute` | `10`；1–120次 | Demo本地限额，非供应商额度；检查、预览和自动生成均占用预算 |
| `daily_token_budget` | `100000`；1000–10000000 | 按UTC日计量。未知用量保留预算预占，超限停止生成；不是计费硬限额承诺 |
| `max_safe_retries` | `2`；0–5次 | 仅明确安全错误（如429，以及历史读取网络失败）限次退避；外发unknown不能自动重试 |

人工恢复方式固定为手动，没有定时恢复参数。新配置、新启用/恢复时记录消息边界；绑定测试码触发的一条问候是明确授权的启动反馈，不是旧历史逐条补发。

## Freshdesk 跟进工单

| 参数 | 默认 / 条件 | 获取与填写方式 |
| --- | --- | --- |
| `freshdesk_domain` | 空；建单必填 | 完整官方HTTPS origin，例如 `https://<TENANT>.freshdesk.com`，不带 `/api/v2`；管理员核对区域及API可用主机 |
| `freshdesk_api_key` | 空；建单必填，敏感 | Freshdesk头像 → Profile settings → API Key；实际角色需有建单/读单权限。后端Basic `<KEY>:X` |
| `requester_mapping` | `{}`；建单必填对应客户 | JSON `{"<FRESHCHAT_USER_ID>":12345}`；12345仅格式示例，须换成真实Freshdesk Contact/requester ID正整数。通过联系人详情/授权联系人API核实，不直接复用Freshchat user ID |
| `ticket_group_id` | `null`，可选 | 管理员提供真实工单组ID正整数；页面留空表示不指定 |
| `priority` | `1` | 1低、2中、3高、4紧急；页面选可读标签 |
| `status` | `2` | 2开放、3待处理、4已解决、5已关闭；以租户流程核实 |
| `ticket_tags` | `["lsp-ai-demo"]` | 页面逗号分隔标签，最多100项，每项1–200字符 |
| `custom_field_mapping` | `{}`，按租户必填规则 | JSON，只允许已创建的 `cf_` 字段和管理员审核固定值，例如 `{"cf_parking_site":"<APPROVED_SITE>"}`。支持字符串/整数/布尔/列表；无任意表达式或模型字段映射 |
| `ticket_policy` | `manual` | 手动；`confirm` 为模型建议后人工创建；`automatic` 为精确规则自动建单。后两者不代表每条消息都建单 |
| `ticket_allowed_reasons` | `[]`；automatic必填 | 逗号分隔已允许的原因文本，模型 `ticket_reason` 需精确匹配；同时需有效requester映射和Freshdesk凭证 |

同会话同跟进事项复用本地已有工单，即使已关闭也不偷偷另建。管理员“明确开启新的跟进事项”才可新建。提交超时标unknown，先核实再关联已有真实工单；不回滚已发客户消息。创建工单无需Freshdesk Webhook。

## 素材、媒体、历史与调度

| 参数 | 默认 / 范围 | 填写说明 |
| --- | --- | --- |
| `media_size_limits` | `{"image":5000000,"video":16000000,"file":20000000}` | 字节，三项必有，每项1–25000000；按当前平台与该渠道较小限额设定。默认是Demo限制，不是各渠道官方保证 |
| `allowed_mime_types` | `image/jpeg, image/png, video/mp4, application/pdf` | 仅支持这四种类型的子集。上传仍校验扩展名、文件签名和实际MIME；HTTP请求体总限制26000000字节 |
| `media_host_allowlist` | `[]` | 下载远程素材/客户历史媒体时填精确小写域名，如 `media.example.com`，无协议/路径/通配符。每个重定向目标也要在表中，DNS不能指内网 |
| `history_page_size` | `50`；1–50 | Freshchat每页条数，初始化遍历全部可访问页，遇到API错误或分页不前进明确失败 |
| `local_retention_days` | `7`；1–365天 | 仅本地历史缓存；受活动任务、自动模式及未核验结果保护，实际可超过保留期。不删除平台历史或自动删除审核素材 |
| `scheduler_token` | 空，调度时必填，敏感 | 部署者生成至少32字符随机值，填高级设置并让调度器使用 `X-Scheduler-Token`。与其他供应商Token必须不同。轮换后同步修改调度器，见调度文档 |

### 审核素材表字段（独立 `/api/assets` 资源）

| 字段 | 如何配置 / 产生 |
| --- | --- |
| `asset_id` / `logical_id` | 管理员填写稳定逻辑ID，如 `parking_entry_map`。同逻辑ID替换文件时新增版本；API返回的实际 `id` 是版本ID，模型与排队任务绑定实际ID |
| `type` / `kind` | 根据真实文件检测为image/video/file；不是任意手填后即可改变 |
| `name` | 素材名称，1–200字符 |
| `purpose` | 用途说明，1–2000字符；写清停车场、可回答问题及适用场景，是模型选择依据 |
| `tags` | 逗号分隔，最多20个标签，每个最多80字符 |
| `channels` | 允许渠道，至少一个且已在允许渠道列表中；跨渠道媒体能力仍分别验收 |
| `file` / `url` | 选本地JPEG/PNG/MP4/PDF文件，或审核HTTPS URL。两者都选时页面优先文件；远程需主机白名单且不允许URL查询鉴权，下载为固定本地版本 |
| `filename` / `mime` / `size` | 文件原名、检测MIME、字节大小，由上传/下载校验生成 |
| `ref` / 平台上传引用 | 后端从真实平台上传响应取得；文件扫描未完成不可发。视频使用版本绑定的24小时签名抓取URL，不发明视频上传API |
| `state` | pending_upload待上传、scanning扫描中、sendable可发送、failed失败、disabled停用；不能靠手工编辑伪造扫描通过 |
| `enabled` | 管理员启用/停用；重新启用后仍按当前实现要求重新确认上传状态 |

只有当前版本可发送、启用且允许该渠道的素材才可进模型目录。图片/视频/PDF各准备一个获准测试文件；不要把真实隐私附件提交代码仓库。

## 测试码与开关的派生参数

这些是运行状态，不是要求用户填写的额外设置项。代码在加密 `meta.test_discovery` 中保存。短时监听只保存脱敏候选，点选前不保存消息正文。

| 字段 / 动作 | 说明 |
| --- | --- |
| `channel` | 开始识别时选择WhatsApp或WeChat；也保留Webchat选项，但不能替代WeChat验收 |
| `next_binding` | 点击“监听下一批会话”选择渠道，5分钟内接收候选；不会自动回复 |
| `candidate` | 候选只含会话 ID、客户 ID、来源和时间；管理员点选后才同步历史并加入白名单 |
| `confirm_auto_reply` | 兼容旧测试码入口；不作为推荐流程 |
| `code` / `expires` | 兼容旧测试码入口；推荐使用 `next_binding` |
| 客户/会话/来源ID | 来自有效签名事件，页面自动显示；每渠道只保留一个绑定 |
| `enabled` | 各渠道AI开关；关闭暂停该账号的未发AI，恢复后不补发历史 |
| `paused` / `status` | 人工暂停与启动状态；后台检查失败显示原因，重启不自动解除人工暂停 |
| “结束所有测试” | 清空全部测试身份和测试码，关闭自动回复；不删除平台消息，不撤回已提交请求 |

完整操作和三层接收核对见 [测试手册](testing-guide.md)。
