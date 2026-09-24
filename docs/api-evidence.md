# API 实施依据与已知限制

查询日期：2026-09-18。本地实现用 Context7 查询 Freshchat、Freshdesk、OpenAI，Freshchat 关键字段另回到官方 API 原文及 Webhook 帮助原文核对。API 参考页未提供明确更新时间，日期不可考。官方文档存在不能代替客户租户/连接器实测。

| 官方来源 | 核对结果及本地实现 |
| --- | --- |
| https://developers.freshchat.com/api/#list_all_agents | GET /v2/agents，按分页读取，排除停用/删除坐席，只返回 ID 与显示名称供选择 |
| https://developers.freshchat.com/api/#send_message_to_conversation | POST 原会话 messages；normal / agent / actor_id / message_parts；多条独立回复逐次 POST |
| https://developers.freshchat.com/api/#list_messages | page、items_per_page 最大50；from_time 含边界；本实现遍历空页，按 ID 去重，保留成功同步游标 |
| https://developers.freshchat.com/api/#retrieve_all_conversation_for_a_user | 同一 user_id 其他会话；没有杜撰全租户会话列表接口 |
| https://developers.freshchat.com/api/#upload_an_image | multipart 字段 image，成功返回 url |
| https://developers.freshchat.com/api/#upload_a_file | 单文件25MB上限；file_hash、file_security_status；SAFE_FILE 可发送，AV_PENDING 扫描中 |
| https://developers.freshchat.com/api/#message_part_object | image.url；video.url/content_type；fileHash、fileSource、name、contentType、file_size_in_bytes；上传与发送字段分别映射 |
| https://support.freshchat.com/support/solutions/articles/239404-freshchat-webhooks-payload-structure-and-authentication | 官方页面标注2022-06-06更新；action=message_create，data.message，Base64 签名 + SHA256withRSA；本实现验证原始字节 |
| https://developers.freshdesk.com/api/#create_ticket | POST /api/v2/tickets，Basic API_KEY:X，支持 requester_id 或 unique_external_id；后者没有对应联系人时由平台创建。2026-09-24 经 Context7 和官方原文重核，手动建单使用此流程，返回真实 requester_id |
| https://developers.openai.com/api/docs/models/gpt-6-astra | 当前官方旗舰模型 `gpt-6-astra` 支持 Responses 和 Structured Outputs；作为默认值，账号权限仍需真实检查 |
| https://developers.openai.com/api/docs/guides/structured-outputs | Responses text.format JSON schema strict；单独处理 refusal / incomplete；遍历输出而非假设 output[0] |
| https://developers.openai.com/api/reference/resources/responses/methods/create | store:false、模型 ID、输出预算、usage 和请求 ID；不用 previous_response_id |

Context7 返回的聚合示例可能混入新建会话或特定 SDK 示例；实现以用户指定路线和官方原文为准，不将新建会话的 messages 数组用作已有会话批量回复。

2026-09-24 通过 Context7 与 Freshchat 官方原文复核历史卡片：`message_parts` / `reply_parts` 支持 `url_button.url/label`、`collection.sub_parts`、`quick_reply_button`、`callback`，以及 `template_content.type/sections[].parts`（carousel、carousel_card_default、quick_reply_dropdown）。管理页按结构显示链接、图文卡片、横向轮播和客户侧选项；客户回调不在管理页执行。机器人 `help_text` 显示文本，输入/上传请求和 FAQ 引用显示提示，不虚构文章地址。官方参考页无明确更新时间，日期不可考。

当前租户另有把 `text:`、`type: button`、`action.type: link`、`action.text`、`action.url` 写入纯文本的历史消息。这是实际连接器的兼容处理，不宣称为官方原生 part 格式；只有完整匹配的非客户文本转换成卡片，无法可靠识别时保留原文。旧文本缓存直接兼容，原生字段若曾被旧版忽略，可点击“同步历史”补齐；不会新增重复消息或补发回复。

待真实租户核对：

1. 当前 Omni 新交互是否仍可由 Freshchat 读取及回复。用户提供的2026年7月迁移说明是路线识别依据，但本次没有客户租户可实测。
2. WhatsApp 与第二指定连接器的实际 `message_source`、Agent 权限、事件版本和平台窗口/模板规则。Webchat 成功不代表 WeChat 成功。
3. Freshchat 文件 `SAFE_FILE` 返回与可发送引用。官方上传示例常返回 AV_PENDING，本实现没有凭空添加扫描状态端点，也不能保证重新上传可推进扫描。文件 part 若需要租户额外 URL 字段，需用真实响应补齐并重新验收。
4. 视频 HTTPS 引用能否形成该连接器原生视频，不能把文本链接成功记作视频通过。浏览器受限与客户接收失败分别记录。
5. 引用气泡、原生 Ticket 双向关联、机器 delivery receipts 未实现；能力矩阵保留独立项目或说明，不以本地映射/人工确认替代。
6. 默认媒体大小是可配置的 Demo 上限，必须按实际平台与渠道较小限制校准，不当成平台全部渠道配额。
7. OpenAI 账号模型支持、用量、上下文长度和数据控制需通过真实账号验证；`store:false` 不等于全部供应商日志零留存。
