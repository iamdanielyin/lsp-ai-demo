# 外部调度 API

唯一入口：`POST /api/internal/jobs/drain`。建议每分钟调用一次。用于有界恢复和本地缓存清理，正常入站由进程内 worker 处理。

## 鉴权与参数

设置页“历史与调度”填写至少32字符的独立 `scheduler_token`。请求 Header 为 `X-Scheduler-Token`。不得复用管理员 Cookie、Freshchat Token 或 OpenAI API Key。轮换后立即更新外部调度中心，旧 Token 失效。

| 字段 | 默认 | 边界 |
| --- | --- | --- |
| tasks | recover_pending、cleanup_cache | 固定枚举，非空、不得重复 |
| limit | 20 | 1–100 整数，布尔值不接受 |
| dry_run | true | 布尔值 |
| allow_external_effects | false | dry_run=false 时必须显式 true |

未知字段、任意操作名或路径均拒绝。

## 可复制请求

只读检查：

```bash
curl -X POST 'https://<DEMO_HOST>/api/internal/jobs/drain' \
  -H 'Content-Type: application/json' \
  -H 'X-Scheduler-Token: <SCHEDULER_TOKEN>' \
  -d '{"tasks":["recover_pending","cleanup_cache"],"limit":20,"dry_run":true,"allow_external_effects":false}'
```

实际执行：

```bash
curl -X POST 'https://<DEMO_HOST>/api/internal/jobs/drain' \
  -H 'Content-Type: application/json' \
  -H 'X-Scheduler-Token: <SCHEDULER_TOKEN>' \
  -d '{"tasks":["recover_pending","cleanup_cache"],"limit":20,"dry_run":false,"allow_external_effects":true}'
```

响应仅包含状态、追踪 ID 和计数：

```json
{"status":"ok","request_id":"<REQUEST_ID>","dry_run":true,"selected":4,"processed":0,"failed":0,"skipped_unknown":1}
```

`selected` 是本次所选任务/缓存行数量，`processed` 是执行成功数量，`failed` 是处理失败数量；任务失败可能已按规则延后重试，应由调度中心同时查看 `failed`。`skipped_unknown` 表示未触碰的结果不明任务总数。dry-run 只读取数据，不领取、不上传、不调用模型、不清理。

## 错误码

| HTTP | error | 含义 |
| --- | --- | --- |
| 400 | invalid_body / invalid_tasks / external_effects_required | 参数、任务范围或实际执行 opt-in 不合法 |
| 401 | scheduler_unauthorized | 未提供或错误 Token |
| 409 | job_locked | 同一 worker 正在处理，稍后再调度 |
| 503 | scheduler_not_configured / database_unavailable | 调度 Token 未配置或数据库不可用 |

错误 JSON 只返回固定错误码和脱敏说明，不包含原始客户内容、媒体、供应商响应或凭证。

## 恢复、幂等与保留

- 单进程 worker 使用同一领取锁，启动脚本额外独占数据库文件锁。SQLite 唯一约束防重复事件与计划条目。不要用多个应用进程共享数据库。
- `queued` / `pending` 且到期的任务可恢复，包括只读客户名称同步 `profile` 和随机码绑定后的 `activate_test`：后者重新核实当前测试授权，读取已绑定账号历史并检查模型；结束测试或配置变更后旧启动任务不能恢复自动回复。启动时 `generating` 恢复排队；提交阶段 `sending` 转为 `unknown`。生成计划与待发送条目原子保存。
- `unknown` 不会由调度器自动重发；部分失败的后续条目暂停，需在消息页人工处理。网络超时后再次调用调度入口不会重新提交结果不明条目。
- 只有明确429或读取类网络失败支持限次退避；客户端超时并不能证明外发未创建。人工重新外发须确认重复风险并记录依据。
- 缓存清理按 `local_retention_days` 清理本地历史及客户名称缓存，保留活动任务、自动模式会话和未核验外发依赖；只读名称任务不阻止历史缓存清理。清理后将会话标记需重新全量同步；不删除平台历史。
- 去重事件 ID、任务审计元数据、工单映射和审核素材是验证记录，不按历史缓存期限删除。审核素材采用显式停用，当前不自动删素材文件，避免误删在途/未核验任务依赖；部署者可在完成验收并安全归档后移除整个独立 Demo 数据目录。
- `limit` 限制本次处理条目数，不是毫秒时限。一条生成任务可包含历史分页和最长配置的模型等待；建议调度中心设置足够超时，出现409或客户端超时时在下一周期重试，不并发洪泛调用。
