# 知识库 + 人工协同验证

本 Demo 使用 OpenAI 托管的 Vector Store 和 Responses `file_search`，不在本地搭建向量数据库。设置页的“OpenAI 与客服知识”分组中管理知识库。

## 快速验证

1. 在设置页填写 OpenAI API Key 和请求地址。默认地址为 `https://api.openai.com/v1`；自定义地址必须同时支持 Files、Vector Stores 和 Responses `file_search`。
2. 点击“创建知识库”。首次上传也会自动创建名为 `LSP Demo Knowledge Base` 的知识库。
3. 点击“上传文档”，选择一个合成的停车 FAQ 或方案文件。支持 `DOCX`、`PDF`、`TXT`、`Markdown`、`CSV`、`JSON`，单文件不超过 20 MB。
4. 文件状态变为“可用”后，在 `/conversations` 打开一个真实会话，确认历史同步完成，再开启该会话的 AI。
5. 发送知识库中明确存在的问题，查看 AI 回复；在会话详情中可以看到是否命中文件。
6. 发送知识库没有覆盖的问题。没有文件引用时，Demo 不发送猜测内容，会把会话切换为人工模式，并显示“需要人工协助”。人工坐席可以直接在同一聊天输入框回复。

## 管理行为

- “刷新状态”读取 OpenAI Vector Store 文件状态。索引中的文件显示“处理中”，完成后显示“可用”。
- 删除单个文件会先删除 Vector Store 关联，再删除 OpenAI File；失败会保留“失败”状态和脱敏错误。
- 删除知识库会删除 Vector Store、本地映射和已记录的 OpenAI File。它不会删除 Freshchat/Freshdesk 会话或本地聊天历史。
- 文档正文只发送给所配置的 OpenAI 地址，不写入仓库、运行日志或前端接口响应。

## 交互边界

- 知识库命中判断使用 Responses 返回的 `file_search_call` 结果和 `file_citation`。只有模型响应包含文件证据才允许按知识库回答。
- 知识库无命中、索引尚未完成、模型拒绝或 OpenAI 请求失败时，当前轮不外发 AI 内容；失败任务可在详情中查看，人工回复不受影响。
- 开启 AI 只作用于当前会话，从开启后的新客户消息开始；人工发送或知识库无命中会关闭当前会话 AI。点击会话顶部开关可重新开启。
- `DOC`、图片、视频、音频和扫描件不在本期知识库上传范围内；图片/视频仍属于独立的渠道素材功能，不会自动进入知识库。

## 本地接口

管理接口均要求管理员登录、CSRF 和合法来源：

```text
GET    /api/knowledge
POST   /api/knowledge                 {"name":"..."}
DELETE /api/knowledge
POST   /api/knowledge/files           multipart field: file
DELETE /api/knowledge/files/<local_id>
```

这些接口调用 OpenAI 会产生真实外部副作用。测试时使用不含客户隐私的合成文档，确认索引状态后再测试聊天。若使用代理，沿用现有 `https_proxy=http://127.0.0.1:7897` 配置；代理只用于 OpenAI 请求。
