import copy
import hashlib
import re
import time

from .security import api_url, identifier, public_key, require, url_parts


SECRETS = {"freshchat_token", "freshdesk_api_key", "openai_api_key", "scheduler_token"}
CAPABILITIES = ["inbound", "history", "manual_text", "image", "video", "file", "ai_text", "ai_media", "omni_visible", "ticket", "quote_bubble"]
DEFAULTS = {
    "integration_profile": "freshchat", "platform_api_base_url": "", "freshchat_token": "", "reply_actor_id": "",
    "allowed_channels": [], "source_mapping": {}, "test_identity_allowlist": [],
    "seed_conversation_id": "", "seed_user_id": "", "public_base_url": "", "freshchat_public_key": "",
    "freshdesk_domain": "", "freshdesk_api_key": "", "requester_mapping": {}, "ticket_group_id": None,
    "priority": 1, "status": 2, "ticket_tags": ["lsp-ai-demo"], "custom_field_mapping": {},
    "ticket_policy": "manual", "ticket_allowed_reasons": [],
    "openai_api_key": "", "openai_base_url": "https://api.openai.com/v1", "openai_model": "gpt-6-astra",
    "openai_project": "", "openai_organization": "",
    "system_instructions": "你是 LSP 停車客服測試助理，使用繁體中文，回答簡潔、準確。不確定時請轉人工。",
    "knowledge_text": "", "max_output_tokens": 4096, "context_token_budget": 12000,
    "model_timeout_seconds": 60, "store": False, "auto_reply_enabled": False,
    "debounce_ms": 1500, "max_reply_messages": 3, "ai_requests_per_minute": 10,
    "daily_token_budget": 100000, "max_safe_retries": 2,
    "media_size_limits": {"image": 5_000_000, "video": 16_000_000, "file": 20_000_000},
    "allowed_mime_types": ["image/jpeg", "image/png", "video/mp4", "application/pdf"],
    "media_host_allowlist": [], "history_page_size": 50, "local_retention_days": 7, "scheduler_token": "",
}
BOUNDS = {"priority": (1, 4), "status": (2, 5), "max_output_tokens": (128, 32768),
          "context_token_budget": (1000, 200000), "model_timeout_seconds": (5, 180),
          "debounce_ms": (0, 10000), "max_reply_messages": (1, 10), "ai_requests_per_minute": (1, 120),
          "daily_token_budget": (1000, 10000000), "max_safe_retries": (0, 5),
          "history_page_size": (1, 50), "local_retention_days": (1, 365)}


def tenant_id(s):
    # Rotating platform credentials intentionally requires re-import and re-verification.
    return hashlib.sha256((s["platform_api_base_url"] + "\0" + s["freshchat_token"]).encode()).hexdigest()[:24]


def test_identity_allowed(s, conversation_id, user_id):
    return ("conversation:" + conversation_id in s["test_identity_allowlist"] or
            bool(user_id) and "user:" + user_id in s["test_identity_allowlist"])


class Settings:
    def __init__(self, db):
        self.db = db
        if not db.one("SELECT key FROM meta WHERE key='settings'"):
            db.run("INSERT INTO meta VALUES('settings',?)", (db.seal({"revision": 1, "values": DEFAULTS}),))
        elif not self.get()[0]["openai_model"].strip():
            # Fill the previously empty default through normal invalidation; preserve explicit model choices.
            self.save({"openai_model": DEFAULTS["openai_model"]})

    def get(self):
        record = self.db.unseal(self.db.one("SELECT value FROM meta WHERE key='settings'")["value"])
        return record["values"], record["revision"]

    def public(self):
        s, revision = self.get()
        result = copy.deepcopy(s)
        for key in SECRETS:
            result[key] = ""
        return {"values": result, "secrets": {k: bool(s[k]) for k in SECRETS}, "revision": revision,
                "webhook_path": "/api/webhooks/freshchat", "implemented_route": "Freshchat（待租户验证）",
                "missing": [k for k in ("platform_api_base_url", "freshchat_token", "reply_actor_id", "freshchat_public_key") if not s[k]]}

    def save(self, patch):
        require(isinstance(patch, dict), "settings_invalid", "设置必须为对象")
        require(set(patch) <= set(DEFAULTS) | {"clear_secrets"}, "unknown_setting", "包含未知配置字段")
        clear = patch.get("clear_secrets", [])
        require(isinstance(clear, list) and all(k in SECRETS for k in clear), "invalid_clear", "清除凭证字段不正确")
        with self.db.lock:
            old, revision = self.get()
            s = copy.deepcopy(old)
            for k, v in patch.items():
                if k in DEFAULTS and (k not in SECRETS or v):
                    s[k] = v
            for k in clear:
                s[k] = ""
            self.validate(s)
            if tenant_id(s) != tenant_id(old) and "test_identity_allowlist" not in patch:
                s["test_identity_allowlist"] = []
            changed = {k for k in s if s[k] != old[k]}
            if not changed:
                return self.public()
            impactful = changed - {"auto_reply_enabled", "scheduler_token", "local_retention_days", "seed_conversation_id", "seed_user_id"}
            if impactful:
                revision += 1
                s["auto_reply_enabled"] = False
            if s["auto_reply_enabled"] and not old["auto_reply_enabled"]:
                self.auto_ready(s, revision)
            with self.db.connect() as conn:
                conn.execute("UPDATE meta SET value=? WHERE key='settings'", (self.db.seal({"revision": revision, "values": s}),))
                if impactful or not s["auto_reply_enabled"]:
                    conn.execute("UPDATE jobs SET state='cancelled',error='配置更新，待发送任务已取消',updated=? WHERE origin='ai' AND state IN ('queued','generating','pending')", (time.time(),))
                if impactful:
                    conn.execute("UPDATE jobs SET state='cancelled',error='配置版本已变化',updated=? WHERE state IN ('queued','pending')", (time.time(),))
                    conn.execute("UPDATE conversations SET mode=CASE WHEN mode='manual' THEN 'manual' ELSE 'off' END")
                    conn.execute("UPDATE tickets SET state='cancelled' WHERE job_id IN (SELECT id FROM jobs WHERE state='cancelled') AND state IN ('queued','pending')")
                    for source, channel in s["source_mapping"].items():
                        conn.execute("UPDATE conversations SET channel=? WHERE tenant=? AND source=?", (channel, tenant_id(s), source))
                    conn.execute("UPDATE conversations SET channel='unknown' WHERE tenant=? AND source NOT IN (SELECT key FROM json_each(?))", (tenant_id(s), __import__('json').dumps(s["source_mapping"])))
                elif s["auto_reply_enabled"] and not old["auto_reply_enabled"]:
                    from datetime import datetime, timezone
                    boundary = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    conn.execute("INSERT OR REPLACE INTO meta VALUES('auto_since',?)", (boundary,))
                    conn.execute("UPDATE conversations SET mode='auto',auto_since=? WHERE tenant=? AND mode='off'", (boundary, tenant_id(s)))
            return self.public()

    def validate(self, s):
        for key, default in DEFAULTS.items():
            if default is None:
                require(s[key] is None or type(s[key]) is int and s[key] > 0, "invalid_setting", f"{key} 需为正整数或空")
            else:
                require(type(s[key]) is type(default), "invalid_setting", f"{key} 类型不正确")
        s["openai_model"] = s["openai_model"].strip() or DEFAULTS["openai_model"]
        s["openai_base_url"] = s["openai_base_url"].strip() or DEFAULTS["openai_base_url"]
        require(s["integration_profile"] == "freshchat" and s["store"] is False, "unsupported_setting", "仅实现 Freshchat，store 必须为 false")
        for key, (lo, hi) in BOUNDS.items():
            require(lo <= s[key] <= hi, "invalid_setting", f"{key} 必须在 {lo}–{hi} 之间")
        for key in SECRETS | {"reply_actor_id", "openai_model", "openai_project", "openai_organization"}:
            require(len(s[key]) <= 4096 and not re.search(r"[\r\n\x00]", s[key]), "invalid_setting", f"{key} 含非法字符或过长")
        require(not s["scheduler_token"] or len(s["scheduler_token"]) >= 32, "weak_token", "调度 Token 至少32个字符")
        require(not s["scheduler_token"] or all(s["scheduler_token"] != s[k] for k in SECRETS - {"scheduler_token"}), "shared_token", "调度 Token 必须独立，不能复用供应商凭证")
        for key, kind in [("platform_api_base_url", "freshchat"), ("freshdesk_domain", "freshdesk"), ("openai_base_url", "openai")]:
            if s[key]:
                s[key] = api_url(s[key], kind)
        if s["public_base_url"]:
            p = url_parts(s["public_base_url"])
            require(p.path in ("", "/") and not p.query, "invalid_url", "公网地址仅填写 origin")
            s["public_base_url"] = s["public_base_url"].rstrip("/")
        if s["freshchat_public_key"]:
            require(len(s["freshchat_public_key"]) <= 20000, "invalid_public_key", "公钥长度超过限制")
            public_key(s["freshchat_public_key"])
        for key in ("reply_actor_id", "seed_conversation_id", "seed_user_id"):
            if s[key]:
                identifier(s[key])
        for key in ("allowed_channels", "test_identity_allowlist", "ticket_tags", "ticket_allowed_reasons", "allowed_mime_types", "media_host_allowlist"):
            require(len(s[key]) <= 100 and all(isinstance(v, str) and 0 < len(v) <= 200 for v in s[key]), "invalid_setting", f"{key} 需为文本列表")
        for entry in s["test_identity_allowlist"]:
            prefix, _, value = entry.partition(":")
            require(prefix in ("user", "conversation") and value, "invalid_allowlist", "白名单格式：user:平台ID 或 conversation:平台ID")
            identifier(value)
        require("unknown" not in s["allowed_channels"], "unknown_channel", "未知渠道不能加入外发白名单")
        require(all(isinstance(k, str) and isinstance(v, str) and v and v != "unknown" for k, v in s["source_mapping"].items()), "invalid_mapping", "来源映射必须为实际 message_source → 渠道名称")
        require(set(s["allowed_channels"]) <= set(s["source_mapping"].values()), "invalid_mapping", "允许渠道必须先配置实际来源映射")
        require(all(type(v) is int and v > 0 for v in s["requester_mapping"].values()), "invalid_requester", "requester_mapping 的值须为真实 Freshdesk requester_id 正整数")
        require(s["ticket_policy"] in ("manual", "confirm", "automatic"), "invalid_policy", "工单策略不正确")
        require(all(re.fullmatch(r"cf_[a-zA-Z0-9_]+", k) and isinstance(v, (str, int, bool, list)) for k, v in s["custom_field_mapping"].items()), "invalid_fields", "自定义字段仅允许已创建的 cf_ 字段和固定审核值")
        if s["ticket_policy"] == "automatic":
            require(s["ticket_allowed_reasons"] and s["requester_mapping"] and s["freshdesk_domain"] and s["freshdesk_api_key"], "ticket_not_ready", "规则自动建单需要允许原因、客户映射及 Freshdesk 凭证")
        require(set(s["media_size_limits"]) == {"image", "video", "file"} and all(type(v) is int and 1 <= v <= 25_000_000 for v in s["media_size_limits"].values()), "invalid_media_limit", "每类媒体限制须为1–25000000字节，并按渠道较小限制设置")
        require(set(s["allowed_mime_types"]) <= {"image/png", "image/jpeg", "video/mp4", "application/pdf"}, "invalid_mime", "一期仅支持 JPEG/PNG/MP4/PDF")
        for host in s["media_host_allowlist"]:
            require(re.fullmatch(r"[a-z0-9.-]+", host) and url_parts("https://" + host).hostname == host, "invalid_host", "媒体白名单需为完整小写主机名，不支持通配符")
        require(len(s["knowledge_text"]) <= 100000 and len(s["system_instructions"]) <= 20000, "text_too_large", "知识或客服说明过长")

    def passed(self, channel, capability, s=None, revision=None):
        if s is None:
            s, revision = self.get()
        row = self.db.one("SELECT status FROM checks WHERE tenant=? AND revision=? AND channel=? AND capability=? ORDER BY id DESC LIMIT 1", (tenant_id(s), revision, channel, capability))
        return bool(row and row["status"] == "passed")

    def auto_ready(self, s, revision, channel=None):
        require(all(s[k] for k in ("platform_api_base_url", "freshchat_token", "reply_actor_id", "freshchat_public_key", "openai_api_key", "openai_model", "public_base_url")), "auto_not_ready", "自动回复所需平台、Webhook 或 OpenAI 配置不完整")
        require(s["allowed_channels"] and s["test_identity_allowlist"], "auto_not_ready", "请先设置允许渠道和测试客户白名单")
        require(self.passed("*", "openai", s, revision), "auto_not_ready", "当前配置尚未通过真实 OpenAI 检查")
        for ch in [channel] if channel else s["allowed_channels"]:
            for capability in ("inbound", "history", "manual_text"):
                if capability == "manual_text" and self.quick_test_authorized(s, revision, ch):
                    continue  # Explicit per-channel account testing does not claim manual/channel acceptance.
                require(self.passed(ch, capability, s, revision), "auto_not_ready", f"{ch} 的 {capability} 尚未通过当前配置验证")

    def quick_test_authorized(self, s, revision, channel):
        row = self.db.one("SELECT value FROM meta WHERE key='test_discovery'")
        state = self.db.unseal(row["value"]) if row else {}
        bindings = state.get("bindings", {})
        binding = bindings.get(channel)
        return (binding and binding.get("status") in ("active", "starting") and state.get("tenant") == tenant_id(s)
                and state.get("revision") == revision and channel in s["allowed_channels"]
                and "user:" + binding.get("user_id", "") in s["test_identity_allowlist"])


    def record(self, channel, capability, status, target, evidence, job_id=None, revision=None, tenant=None):
        s, rev = self.get()
        return self.db.run("INSERT INTO checks(tenant,revision,channel,capability,status,target,evidence,job_id,created) VALUES(?,?,?,?,?,?,?,?,?)", (tenant or tenant_id(s), revision or rev, channel, capability, status, target, self.db.seal(evidence), job_id, time.time()))
