# 平台接入及集中设置指引

面向客户管理员和部署人员。字段的默认值、格式及条件见 [全部参数](configuration-reference.md)，按步骤测试见 [测试手册](testing-guide.md)。当前只实现 Freshchat 消息事件；Freshdesk 仅用于跟进工单。

## 先确认真实路线

请客户管理员从已经接通的 WhatsApp 渠道发送一条新消息，同时在 Omni 工作台检查它对应的是 Freshchat 会话还是新版 Omni Ticket。

1. 平台真实事件中包含 `conversation_id`。
2. 使用该租户区域 Freshchat Token，`GET /v2/conversations/{conversation_id}` 能读取同一对象。
3. `GET /v2/conversations/{conversation_id}/messages` 能找到同一客户消息 ID、时间及来源。
4. 专用测试 Agent 向同一对象手动回复，记录 API 消息 ID，并核对原工作台和原客户渠道。

四项未齐备前不能认定路线适配。新版 Ticket 交互若无法由 Freshchat 读取，本交付记 blocked；本代码不含 Ticket 事件/公开回复适配。Freshdesk 跟进建单成功也不能替代原渠道会话回复成功。

## 公网部署及管理权限

- 应用由 `run.py` 运行，反向代理终止 HTTPS 并转发至本机端口。`public_base_url` 填写外部 HTTPS origin，不带路径。应用不自动创建隧道或域名。
- 代理保留真实 Host；本实现不信任任意 `X-Forwarded-*` Header。管理写接口只接受同源或已配置公网 origin。禁止跨站嵌入及开放 CORS。
- 配置公网 origin 后管理员 Cookie 使用 Secure；继续访问普通 HTTP 域名可能无法保留登录，请改用该 HTTPS 入口。本机浏览器对 localhost/127.0.0.1 的 Secure 行为不同，不依赖其生产兼容性。
- SQLite、`.env`、媒体目录及备份只给运行用户读写；不要暴露文件目录或整个仓库为静态目录。不在反向代理日志记录 Authorization、Cookie、请求体或 `/media/` 完整签名路径。
- 本 Demo 为单管理员初始化账户，不含完整账户管理、SSO 或账户找回。

## Freshchat 控制台操作

不同租户界面可能不同，以下按实际管理员设置页面的 API/Webhooks 配置项执行：

1. 在 API 设置中确认区域 API base URL 和可读取/发送会话的 Token；用专用测试 Agent，确认 Agent ID 有权回复目标会话。
2. 在管理设置中的 Webhooks 页面新建订阅，将 Demo `/settings` 展示的完整 URL 填入，例如 `https://<DEMO_HOST>/api/webhooks/freshchat`。
3. 订阅 `message_create`。其他事件直接忽略，不持久化；此 Demo 不用应用 SDK 的 `onMessageCreate` 载荷。
4. 将该 Webhook 配置提供的 RSA 公钥保存至 Demo。支持 PEM 公钥或 Base64 DER。公钥虽不保密，修改仍受管理员鉴权。
5. 平台请求需有 `X-Freshchat-Signature`。Demo 原样保留请求字节进行 SHA256withRSA 验证，并记录 `X-Freshchat-Payload-Version`、`X-Retry-Count`。代理不可重写 JSON body。
6. 保存发送坐席后，不需要配置客户或会话 ID。自己的测试账号发送公开普通消息时，签名 Webhook 会按事件中的坐席归属（若载荷提供）过滤；未提供归属字段时先接收入站会话，由后台会话详情核实；其他坐席的会话停止历史同步并移出列表。会话自动出现在 `/conversations`，并排队读取历史。
7. WhatsApp 和 WeChat 可以各自产生新会话；来源字段会自动映射已知渠道。未知来源会显示为未识别，发送前必须先核实映射。不要凭 `channel_id` 推断 WhatsApp 号码。指定 WeChat 时必须验证实际连接器。
8. 在测试范围内关闭其他机器人、自动回复或重复 Webhook 消费，并核实人工分配。Webhook 是消息到达后的异步通知，不会在平台接收前拦截消息，也无法同步阻止平台内部机器人。

## 第一轮手动闭环

先填写连接信息并选择 Agent，配置公网回调与验签公钥。新消息会自动取得真实会话/客户 ID并同步历史；会话默认 AI 关闭，管理员在消息页单独点击“开启 AI”才启动该会话的新消息自动回复。Webhook 最近事件可查看原始载荷。这里不自动声明手动文本、工作台显示或客户送达通过。

原手工流程仍可展开使用：导入真实会话、同步、绑定，再手动回复。若返回403，记录权限不足；404需核对区域、ID、租户及迁移形态。不要把失败直接解释为渠道不支持。

白名单示例（仅占位符）：

```json
{
  "source_mapping": {"<OBSERVED_SOURCE>": "WhatsApp"},
  "allowed_channels": ["WhatsApp"],
  "test_identity_allowlist": ["user:<VERIFIED_TEST_USER_ID>"]
}
```

自动发现模式下白名单留空，事件按设置页选定坐席归属过滤；没有归属字段的载荷会先展示会话但不会自动触发 AI。填写白名单后可切换为旧的指定客户/会话限制。平台全量订阅的请求仍会到达 Demo，应用过滤不能代替网络入口限流或平台订阅过滤。当前没有全租户吞吐保证。

发送前会显示实际渠道、客户、会话和所有条目。确认发送后逐条观察任务；出现结果不明立即核实平台，不反复点击发送。API 已受理也不代表下游送达；打开原 Omni 工作台和客户设备，核对内容、发送者和附件，再填写任务中的接收证据。

## 素材和扫描

- 图片：`POST /v2/images/upload`，multipart 字段 `image`，返回真实 `url`。
- 文件：`POST /v2/files/upload`，multipart 字段 `file`，最大不能超过25MB且须遵从渠道更小限制。上传返回 `file_hash`，发送转换成 `fileHash` / `fileSource` / `name` / `contentType` 等字段。
- `AV_PENDING` 绝不当成可发送；当前官方公开资料未确认通用扫描轮询端点，本 Demo 不发明接口。页面可重新上传检查，但可能持续返回 pending。租户无法取得 `SAFE_FILE` 时标 blocked，需平台确认可用流程；不能人为改数据库标通过。
- 视频没有 `/videos/upload`。本地 MP4 使用本应用提供的24小时签名链接，需真实公网 HTTPS；远程 MP4 从管理员批准的 HTTPS 地址下载、审核后固定为本地版本，再以该版本的签名链接发送。这样排队内容不会随外部 URL 变化。远程下载检查 MIME、大小和 DNS；URL 查询参数不允许携带凭证。
- 上传文件或审核 URL 是授权素材来源，不是“模型理解了文件内容”。请在用途和知识文本里提供获准的说明。
- 本地文件和平台引用属于特定素材版本。编辑用途/禁用不替换文件；使用相同逻辑 ID 新增时生成另一版本。
- 历史媒体下载只通过管理员授权代理，需将实际媒体存储主机精确加入 `media_host_allowlist`。浏览器不能播放或签名 URL 过期时显示限制，不判定渠道失败。

## OpenAI 与工单

OpenAI 请求地址在连接区域直接配置，支持基础地址及完整 `/responses` 地址；默认 `https://api.openai.com/v1`。自定义服务须兼容 Responses 与结构化输出，并使用其对应的密钥。当前支持公网 HTTPS（443），拒绝 URL 凭证、查询参数、私网 DNS 结果与 API 重定向。更换地址后需重新检查。OpenAI 项目、组织 Header 仅有值时发送，不自动发现项目。检查会产生真实小额用量；失败展示固定脱敏错误，不将模型错误发给客户。默认模型为 `gpt-6-astra`（2026-09-18 官方模型目录核对），实际账号权限仍需检查；无权限时可在高级设置修改。自动策略更改后能力检查可能失效，重新完成所需检查再开启。

Freshdesk API Key 使用 Basic `<API_KEY>:X`。在设置页为每个测试 Freshchat user_id 映射真实整数 requester_id。租户自定义必填字段需要先创建，`custom_field_mapping` 只接受已审核的 `cf_` 字段固定值。工单默认复用现有事项，包括已关闭单也不偷偷另建；管理员明确开启新事项才新建。提交不明可人工输入真实工单编号，服务端读取并验证 requester 后关联，禁止猜测重建。

## 真实验收顺序

每个目标渠道各跑一次文本、图片、视频、PDF、AI 上下文、文字加 PDF、人工接管、工单、失败恢复；再做10条真实自动回复及至少一轮连续追问。AI 验收提问：发位置图、发停车指引视频、发方案 PDF、文字解释方案、先说明再附 PDF。保存真实模型 request_id、上下文范围、计划、逐条平台 ID、耗时和 Token 用量。没有机器回执时由测试人员记录第三级确认，不填写虚构的 delivery receipt。
