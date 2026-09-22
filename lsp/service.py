import html
import hashlib
import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import providers, security
from .security import Problem, identifier, require
from .settings import Settings, tenant_id, test_identity_allowed
from .store import dump


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "invalid_time", "消息时间缺少时区")
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, AttributeError):
        raise Problem("invalid_time", "平台消息时间不正确") from None


def normalize_message(message, cid):
    require(isinstance(message, dict) and message.get("conversation_id", cid) == cid,
            "conversation_mismatch", "历史消息属于其他会话，已拒绝")
    mid = identifier(message.get("id"))
    actor_id = identifier(message.get("actor_id", "unknown"))
    actor = message.get("actor_type", "system")
    actor = actor if actor in ("user", "agent") else "system"
    private = message.get("message_type") != "normal" or bool(message.get("botsPrivateNote")) or bool(message.get("private"))
    parts = []
    require(isinstance(message.get("message_parts", []), list), "message_format", "message_parts 必须为数组")
    for part in message.get("message_parts", []):
        require(isinstance(part, dict), "message_format", "消息片段格式不正确")
        for kind in ("text", "image", "video", "file"):
            val = part.get(kind)
            if not isinstance(val, dict):
                continue
            if kind == "text":
                parts.append({"type": "text", "text": str(val.get("content", ""))[:100000]})
            else:
                parts.append({"type": kind, "url": str(val.get("url", "")), "name": str(val.get("name", val.get("file_name", "媒体"))),
                              "mime": str(val.get("content_type", val.get("contentType", val.get("file_content_type", "")))),
                              "size": val.get("file_size_in_bytes", val.get("file_size")),
                              "security_status": val.get("file_security_status")})
    if not parts:
        parts = [{"type": "unsupported", "text": "平台消息类型暂不支持预览"}]
    return {"platform_id": mid, "actor": actor, "actor_id": actor_id, "created": timestamp(message.get("created_time")),
            "private": int(private), "interaction": str(message.get("interaction_id", ""))[:200], "parts": parts,
            "user_id": str(message.get("user_id") or (actor_id if actor == "user" else "")), "source": str(message.get("message_source", "")),
            "topic_id": str(message.get("channel_id", ""))}


def assigned_agent_id(payload, message):
    """Read common assignment shapes without trusting a display name."""
    objects = [message, payload.get("data", {}).get("conversation"), payload.get("conversation"), payload]
    keys = ("assigned_agent_id", "assignedAgentId")
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for key in keys:
            value = obj.get(key)
            if isinstance(value, dict):
                value = value.get("id")
            if isinstance(value, str) and value:
                return value[:200]
    return ""


class Service:
    def __init__(self, db, files):
        self.db, self.settings = db, Settings(db)
        self.files = Path(files).resolve()
        self.files.mkdir(parents=True, exist_ok=True)
        self.files.chmod(0o700)
        # ponytail: one in-process worker serializes the demo; use per-conversation workers if throughput matters.
        self.worker_lock, self.model_lock = threading.Lock(), threading.Lock()
        self.stop, self.wake = threading.Event(), threading.Event()
        self.recover_after_restart()

    def recover_after_restart(self):
        with self.db.connect() as conn:
            conn.execute("UPDATE jobs SET state='unknown',error_code='restart_inflight',error='进程在提交阶段退出；必须人工核实',updated=? WHERE state='sending'", (time.time(),))
            conn.execute("UPDATE jobs SET state='queued',updated=? WHERE state='generating'", (time.time(),))
            conn.execute("UPDATE tickets SET state='unknown' WHERE job_id IN (SELECT id FROM jobs WHERE state='unknown')")

    def start(self):
        def loop():
            while not self.stop.is_set():
                try:
                    self.drain(20)
                except Exception:
                    # Never log exception repr: provider errors may carry credentials or raw responses.
                    s, _ = self.settings.get()
                    self.db.log(tenant_id(s), None, "worker_error", "后台任务发生内部错误，请查看任务状态")
                self.wake.wait(0.5)
                self.wake.clear()
        threading.Thread(target=loop, daemon=True, name="lsp-worker").start()

    def conv(self, cid):
        require(type(cid) is int and cid > 0, "invalid_conversation_id", "请选择有效的本地会话 ID")
        s, _ = self.settings.get()
        c = self.db.one("SELECT * FROM conversations WHERE id=? AND tenant=?", (cid, tenant_id(s)))
        require(c, "conversation_not_found", "当前租户不存在该会话", 404)
        return c

    def add_conversation(self, platform_id, user_id="", source="", topic=""):
        s, _ = self.settings.get()
        tenant = tenant_id(s)
        binding = self.discovery()["bindings"].get(s["source_mapping"].get(source))
        auto = s["auto_reply_enabled"] and not self.auto_scope_allowed(s) and not (binding and binding["user_id"] == user_id and binding["paused"])
        boundary = self.db.one("SELECT value FROM meta WHERE key='auto_since'")
        self.db.run("INSERT OR IGNORE INTO conversations(tenant,platform_id,user_id,source,channel,topic_id,mode,auto_since,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                    (tenant, identifier(platform_id), user_id, source, s["source_mapping"].get(source, "unknown"), topic,
                     "auto" if auto else "off", boundary["value"] if auto and boundary else "", time.time()))
        c = self.db.one("SELECT * FROM conversations WHERE tenant=? AND platform_id=?", (tenant, platform_id))
        return c

    def insert_message(self, c, m):
        if m["user_id"]:
            identifier(m["user_id"])
            require(not c["user_id"] or c["user_id"] == m["user_id"], "identity_mismatch", "消息客户 ID 与会话不一致，已停止处理", 409)
            self.db.run("UPDATE conversations SET user_id=? WHERE id=?", (m["user_id"], c["id"]))
            c["user_id"] = m["user_id"]
        if m["source"] and m["actor"] == "user":
            require(not c["source"] or c["source"] == m["source"], "source_mismatch", "会话来源发生变化，请核实真实渠道", 409)
            s, _ = self.settings.get()
            self.db.run("UPDATE conversations SET source=?,channel=?,topic_id=? WHERE id=?", (m["source"], s["source_mapping"].get(m["source"], "unknown"), m["topic_id"], c["id"]))
            c["source"], c["channel"] = m["source"], s["source_mapping"].get(m["source"], "unknown")
        with self.db.connect() as conn:
            inserted = conn.execute("INSERT OR IGNORE INTO messages(tenant,conversation,platform_id,actor,actor_id,created,private,interaction,parts,cached_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                    (c["tenant"], c["id"], m["platform_id"], m["actor"], m["actor_id"], m["created"], m["private"], m["interaction"], self.db.seal(m["parts"]), time.time())).rowcount
            if m["actor"] == "user" and not m["private"]:
                latest = conn.execute("SELECT platform_id FROM messages WHERE conversation=? AND actor='user' AND private=0 ORDER BY created DESC,platform_id DESC LIMIT 1", (c["id"],)).fetchone()
                conn.execute("UPDATE conversations SET last_customer=?,updated=? WHERE id=?", (latest[0], time.time(), c["id"]))
        return bool(inserted)

    def enqueue(self, kind, c=None, payload=None, origin="admin", trigger=None, batch=None, seq=0, due=None):
        s, revision = self.settings.get()
        job = uuid.uuid4().hex
        now = time.time()
        self.db.run("INSERT INTO jobs(id,tenant,conversation,kind,origin,state,revision,trigger_id,batch,seq,due,payload,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job, tenant_id(s), c["id"] if c else None, kind, origin, "pending" if kind == "send" else "queued", revision,
                     trigger, batch, seq, due if due is not None else now, self.db.seal(payload or {}), now, now))
        self.wake.set()
        return job

    def cancel_ai(self, cid, reason):
        self.db.run("UPDATE jobs SET state='cancelled',error=?,updated=? WHERE conversation=? AND origin='ai' AND state IN ('queued','generating','pending')", (reason, time.time(), cid))

    def set_mode(self, cid, mode):
        require(mode in ("auto", "manual", "off"), "invalid_mode", "会话模式不正确")
        with self.db.lock:
            c = self.conv(cid)
            s, rev = self.settings.get()
            if mode == "auto":
                self.send_guard(c, s)
                require(c["sync_complete"] and not c["sync_error"], "history_not_ready", "请等待当前会话历史同步完成后恢复 AI", 409)
                self.settings.auto_ready(s, rev, c["channel"])
            self.cancel_ai(cid, "人工接管或会话模式已变化，未发送任务已取消")
            state = self.discovery()
            binding = state["bindings"].get(c["channel"])
            if binding and binding["user_id"] == c["user_id"]:
                binding["paused"] = mode != "auto"
                self.save_discovery(state)
                for row in self.db.all("SELECT id FROM conversations WHERE tenant=? AND user_id=? AND channel=?", (c["tenant"], c["user_id"], c["channel"])):
                    self.cancel_ai(row["id"], "测试账号已切换 AI / 人工模式")
                self.db.run("UPDATE conversations SET mode=?,auto_since=?,updated=? WHERE tenant=? AND user_id=? AND channel=?", (mode, utc(), time.time(), c["tenant"], c["user_id"], c["channel"]))
            self.db.run("UPDATE conversations SET mode=?,auto_since=?,updated=? WHERE id=?", (mode, utc(), time.time(), cid))
            inflight = self.db.one("SELECT count(*) AS n FROM jobs WHERE conversation=? AND state='sending'", (cid,))["n"]
            if binding and binding["user_id"] == c["user_id"]:
                inflight = self.db.one("SELECT count(*) n FROM jobs WHERE state='sending' AND conversation IN (SELECT id FROM conversations WHERE tenant=? AND user_id=? AND channel=?)", (c["tenant"], c["user_id"], c["channel"]))["n"]
            return {"mode": mode, "inflight": inflight, "message": f"已取消待发送 AI 任务；另有 {inflight} 条已提交请求，无法撤回"}

    def ingest(self, payload, version, retries):
        require(isinstance(payload, dict), "event_format", "Webhook 须为 JSON 对象")
        s, rev = self.settings.get()
        action = payload.get("action")
        require(isinstance(action, str), "event_format", "Webhook 缺少 action")
        if action != "message_create":
            return {"status": "ignored", "reason": "non_message_event"}
        require(isinstance(payload.get("data"), dict), "event_format", "Webhook data 必须为对象")
        message = payload["data"].get("message")
        require(isinstance(message, dict), "event_format", "该 payload 版本缺少 data.message")
        cid = identifier(message.get("conversation_id"))
        with self.db.connect():
            captured = self.capture_test_message(message, cid, version, retries)
            if captured:
                return {"status": captured if isinstance(captured, str) else "discovered"}
            c = self.db.one("SELECT * FROM conversations WHERE tenant=? AND platform_id=?", (tenant_id(s), cid))
            uid = message.get("user_id") or (message.get("actor_id", "") if message.get("actor_type") == "user" else "")
            if uid:
                identifier(uid)
            if c and c["user_id"] and uid:
                require(c["user_id"] == uid, "identity_mismatch", "消息客户 ID 与会话不一致，已停止处理", 409)
            uid = uid or (c["user_id"] if c else "")
            auto_capture = self.auto_discovery_allowed(s, message)
            # An explicit assignment mismatch is safe to ignore. Payloads without assignment are accepted because
            # Freshchat message_create variants do not all include the conversation assignee.
            assigned = assigned_agent_id(payload, message)
            if auto_capture and assigned and assigned != s["reply_actor_id"]:
                if c:
                    self.db.run("UPDATE conversations SET assigned_agent_id=? WHERE id=?", (assigned, c["id"]))
                    self.set_mode(c["id"], "manual")
                return {"status": "ignored", "reason": "outside_selected_agent"}
            source = c["source"] if c else ""
            if message.get("actor_type") == "user":
                source = message.get("message_source") or source
            if auto_capture:
                self.settings.observe_source(source)
                s, rev = self.settings.get()
            in_scope = test_identity_allowed(s, cid, uid) or auto_capture or (c and self.auto_scope_allowed(s))
            if not in_scope or not self.quick_scope(uid, source):
                return {"status": "ignored", "reason": "outside_test_scope"}
            m = normalize_message(message, cid)
            c = self.add_conversation(cid, uid, str(message.get("message_source", "")) if message.get("actor_type") == "user" else "")
            if assigned:
                self.db.run("UPDATE conversations SET assigned_agent_id=? WHERE id=?", (assigned, c["id"]))
            duplicate = self.db.one("SELECT id FROM events WHERE tenant=? AND conversation=? AND platform_id=? AND action=?", (c["tenant"], c["id"], m["platform_id"], action))
            if duplicate:
                return {"status": "duplicate"}
            self.insert_message(c, m)
            # Event, message, mode changes and tasks commit together before webhook success.
            if m["actor"] == "agent" and m["actor_id"] != s["reply_actor_id"]:
                self.set_mode(c["id"], "manual")
            c = self.conv(c["id"])
            if not self.db.one("SELECT id FROM jobs WHERE conversation=? AND kind='sync' AND state IN ('queued','generating')", (c["id"],)):
                self.enqueue("sync", c, origin="webhook")
            if m["actor"] == "user" and not m["private"]:
                if c["source"] and c["channel"] != "unknown":
                    self.settings.record(c["channel"], "inbound", "passed", cid, {"message_id": m["platform_id"], "payload_version": version, "signature": "RSA-SHA256 verified"})
                if c["last_customer"] == m["platform_id"]:
                    self.cancel_ai(c["id"], "客户追加消息，旧计划已取消")
                if c["mode"] == "auto" and m["created"] > c["auto_since"] and c["last_customer"] == m["platform_id"]:
                    try:
                        self.send_guard(c, s)
                        self.settings.auto_ready(s, rev, c["channel"])
                        self.enqueue("generate", c, origin="ai", trigger=c["last_customer"], due=time.time() + s["debounce_ms"] / 1000)
                    except Problem as e:
                        self.db.log(c["tenant"], c["id"], "auto_blocked", e.message)
            self.db.run("INSERT INTO events(tenant,conversation,platform_id,action,version,retries,payload,created) VALUES(?,?,?,?,?,?,?,?)", (c["tenant"], c["id"], m["platform_id"], action, version[:80], retries[:20], self.db.seal(payload), time.time()))
            self.db.log(c["tenant"], c["id"], "event_received", f"{m['actor']} / {m['platform_id']}")
        return {"status": "accepted", "conversation_id": c["id"]}

    def auto_discovery_allowed(self, s, message):
        return bool(self.auto_scope_allowed(s)
                    and message.get("actor_type") == "user"
                    and message.get("message_type") == "normal"
                    and not message.get("private") and not message.get("botsPrivateNote"))

    def auto_scope_allowed(self, s):
        return self.settings.auto_discovery_allowed(s)

    def resume_auto_discovery(self):
        with self.db.connect():
            s, _ = self.settings.get()
            require(s["reply_actor_id"], "agent_required", "请先选择发送坐席")
            if s["test_identity_allowlist"] or s["auto_reply_enabled"]:
                self.settings.save({"test_identity_allowlist": [], "auto_reply_enabled": False})
            self.db.run("DELETE FROM meta WHERE key IN ('auto_discovery_paused','test_discovery')")

    def pause_auto_discovery(self):
        with self.db.connect():
            self.settings.save({"test_identity_allowlist": [], "auto_reply_enabled": False})
            self.db.run("DELETE FROM meta WHERE key='test_discovery'")
            s, _ = self.settings.get()
            for c in self.db.all("SELECT id FROM conversations WHERE tenant=?", (tenant_id(s),)):
                self.cancel_ai(c["id"], "已暂停自动收集与 AI")
            self.db.run("UPDATE conversations SET mode='off' WHERE tenant=?", (tenant_id(s),))

    def discovery(self):
        with self.db.lock:
            s, revision = self.settings.get()
            row = self.db.one("SELECT value FROM meta WHERE key='test_discovery'")
            state = self.db.unseal(row["value"]) if row else {}
            state.setdefault("bindings", {})
            state.setdefault("enrollment", None)
            state.setdefault("next_binding", None)
            state.setdefault("candidates", [])
            if state.get("tenant") != tenant_id(s) or state.get("revision") != revision:
                if row:
                    self.db.run("DELETE FROM meta WHERE key='test_discovery'")
                return {"tenant": tenant_id(s), "revision": revision, "bindings": {}, "enrollment": None, "next_binding": None, "candidates": []}
            if state["enrollment"] and state["enrollment"]["expires"] <= time.time():
                state["enrollment"] = None
                self.save_discovery(state)
            if state["next_binding"] and state["next_binding"].get("expires", 0) <= time.time():
                state["next_binding"] = None
                state["candidates"] = []
                self.save_discovery(state)
            return state

    def save_discovery(self, state):
        self.db.run("INSERT OR REPLACE INTO meta VALUES('test_discovery',?)", (self.db.seal(state),))

    def start_discovery(self, channel):
        require(channel in ("WhatsApp", "WeChat", "Webchat"), "invalid_channel", "请选择本次测试的实际渠道")
        with self.db.connect():
            s, _ = self.settings.get()
            require(all(s[k] for k in ("platform_api_base_url", "freshchat_token", "reply_actor_id", "public_base_url", "freshchat_public_key", "openai_api_key", "openai_model")),
                    "discovery_not_ready", "请先保存平台、发送坐席、OpenAI、公网地址和验签公钥，并在平台启用 Webhook", 409)
            state = self.discovery()
            state["enrollment"] = {"status": "waiting", "channel": channel, "code": "LSP-TEST-" + secrets.token_hex(12).upper(), "expires": time.time() + 300}
            self.save_discovery(state)
            return state

    def start_next_binding(self, channel):
        require(channel in ("WhatsApp", "WeChat", "Webchat"), "invalid_channel", "请选择本次测试的实际渠道")
        with self.db.connect():
            s, _ = self.settings.get()
            require(all(s[k] for k in ("platform_api_base_url", "freshchat_token", "reply_actor_id", "public_base_url", "freshchat_public_key")),
                    "binding_not_ready", "请先保存平台、发送坐席、公网地址和验签公钥，并在平台启用 Webhook", 409)
            state = self.discovery()
            state["enrollment"] = None
            state["next_binding"] = {"status": "waiting", "channel": channel, "expires": time.time() + 300}
            state["candidates"] = []
            self.save_discovery(state)
            return state

    def bind_observed_message(self, state, message, cid, version, retries, channel, auto_on_start=False, code_hash=""):
        uid = message.get("user_id")
        require(uid, "identity_missing", "Webhook 消息没有客户 ID，无法绑定测试客户", 409)
        identifier(uid)
        m = normalize_message(message, cid)
        s, _ = self.settings.get()
        source = m["source"]
        require(source and source != "unknown" and len(source) <= 200, "source_missing", "Webhook 消息没有真实渠道来源，无法绑定", 409)
        require(source not in s["source_mapping"] or s["source_mapping"][source] == channel,
                "source_conflict", "此来源已对应其他渠道，请核对 Freshchat 连接器", 409)
        old = state["bindings"].get(channel)
        if old and old.get("local_id"):
            self.set_mode(old["local_id"], "manual")
        state["bindings"][channel] = {"channel": channel, "user_id": uid, "conversation_id": cid, "source": source,
                                      "trigger_id": m["platform_id"], "code_hash": code_hash, "paused": False,
                                      "start_ai": auto_on_start, "status": "starting"}
        bindings = state["bindings"]
        self.settings.save({"source_mapping": {**s["source_mapping"], source: channel}, "allowed_channels": list(bindings),
                            "test_identity_allowlist": list(dict.fromkeys("user:" + b["user_id"] for b in bindings.values())), "auto_reply_enabled": False})
        s, revision = self.settings.get()
        c = self.add_conversation(cid, uid, source, m["topic_id"])
        self.insert_message(c, m)
        self.db.run("UPDATE conversations SET mode='off',auto_since=? WHERE id=?", (m["created"], c["id"]))
        bindings[channel]["local_id"] = c["id"]
        state.update(revision=revision, enrollment=None, next_binding=None, candidates=[])
        state["activation_job"] = self.enqueue("activate_test", c, origin="setup", trigger=m["platform_id"])
        self.save_discovery(state)
        self.db.run("INSERT OR IGNORE INTO events(tenant,conversation,platform_id,action,version,retries,created) VALUES(?,?,?,?,?,?,?)",
                    (c["tenant"], c["id"], m["platform_id"], "message_create", version[:80], retries[:20], time.time()))
        return True

    def capture_test_message(self, message, cid, version, retries):
        state = self.discovery()
        if message.get("actor_type") != "user" or message.get("message_type") != "normal" or message.get("private") or message.get("botsPrivateNote"):
            return False
        pending_next = state.get("next_binding")
        if pending_next and pending_next.get("expires", 0) <= time.time():
            state["next_binding"] = None
            self.save_discovery(state)
            pending_next = None
        if pending_next and pending_next.get("status") == "waiting":
            m = normalize_message(message, cid)
            if m["source"] and m["source"] != "unknown" and message.get("user_id"):
                candidate = {"channel": pending_next["channel"], "conversation_id": cid, "user_id": m["user_id"],
                             "source": m["source"], "trigger_id": m["platform_id"], "version": version[:80],
                             "retries": retries[:20], "observed_at": time.time()}
                if not any(x.get("conversation_id") == cid for x in state["candidates"]):
                    state["candidates"].append(candidate)
                    state["candidates"] = state["candidates"][-20:]
                    self.save_discovery(state)
                return "candidate"
            return False
        parts = message.get("message_parts")
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            return False
        part = parts[0].get("text")
        text = part.get("content") if isinstance(part, dict) else None
        if not isinstance(text, str):
            return False
        code_hash = hashlib.sha256(text.strip().encode()).hexdigest()
        if any(b["code_hash"] == code_hash for b in state["bindings"].values()):
            return True  # Consumed codes cannot bind another account or start another reply.
        pending = state["enrollment"]
        if not pending or pending["status"] != "waiting" or text.strip() != pending["code"]:
            return False
        if not message.get("user_id"):
            return False
        channel = pending["channel"]
        m = normalize_message(message, cid)
        s, _ = self.settings.get()
        source = m["source"]
        if not source or source == "unknown" or len(source) > 200 or (source in s["source_mapping"] and s["source_mapping"][source] != channel):
            pending.update(status="failed", error="来源缺失或与所选渠道冲突，请核对真实连接器后重新识别")
            self.save_discovery(state)
            return True
        return self.bind_observed_message(state, message, cid, version, retries, channel, True, code_hash)

    def select_candidate(self, conversation_id):
        identifier(conversation_id)
        with self.db.connect():
            state = self.discovery()
            candidate = next((x for x in state.get("candidates", []) if x.get("conversation_id") == conversation_id), None)
            require(candidate, "candidate_not_found", "候选会话不存在、已过期或已被选择", 404)
            channel = candidate["channel"]
            old = state["bindings"].get(channel)
            if old and old.get("local_id"):
                self.set_mode(old["local_id"], "manual")
            s, _ = self.settings.get()
            self.settings.save({"source_mapping": {**s["source_mapping"], candidate["source"]: channel},
                                "allowed_channels": list(dict.fromkeys([*s["allowed_channels"], channel])),
                                "test_identity_allowlist": list(dict.fromkeys([*s["test_identity_allowlist"], "user:" + candidate["user_id"]])),
                                "auto_reply_enabled": False})
            s, revision = self.settings.get()
            c = self.add_conversation(candidate["conversation_id"], candidate["user_id"], candidate["source"])
            self.db.run("UPDATE conversations SET mode='off',auto_since=? WHERE id=?", (utc(), c["id"]))
            state["bindings"][channel] = {**candidate, "paused": True, "start_ai": False, "status": "starting", "local_id": c["id"]}
            state["next_binding"] = None
            state["candidates"] = []
            state["revision"] = revision
            state["activation_job"] = self.enqueue("activate_test", c, origin="setup", trigger=candidate["trigger_id"])
            self.save_discovery(state)
            self.db.run("INSERT OR IGNORE INTO events(tenant,conversation,platform_id,action,version,retries,created) VALUES(?,?,?,?,?,?,?)",
                        (c["tenant"], c["id"], candidate["trigger_id"], "message_create", candidate["version"], candidate["retries"], time.time()))
            return {"conversation_id": c["id"], "job_id": state["activation_job"], "channel": channel}

    def quick_scope(self, uid, source):
        bindings = self.discovery()["bindings"]
        return not bindings or any(b["user_id"] == uid and b["source"] == source for b in bindings.values())

    def activate_test(self, job):
        def current():
            state = self.discovery()
            require(state.get("activation_job") == job["id"], "stale_plan", "测试或配置已变化，启动已取消", 409)
            require(self.db.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] != "cancelled", "stale_plan", "测试启动已取消", 409)
            return state
        with self.db.lock:
            state = current()
            if all(b["status"] == "active" for b in state["bindings"].values()):
                return {"status": "active"}
        # A new binding changes configuration: recheck only these explicitly selected accounts.
        for b in state["bindings"].values():
            self.sync(b["local_id"])
            with self.db.lock:
                current()
                self.settings.record(b["channel"], "inbound", "passed", b["conversation_id"],
                                     {"message_id": b["trigger_id"], "signature": "RSA-SHA256 verified", "test_pairing": True})
        self.check_openai()
        with self.db.connect():
            state = current()
            start_ai = any(b.get("start_ai", True) for b in state["bindings"].values())
            if start_ai:
                self.settings.save({"auto_reply_enabled": True})
            boundary = utc()
            for b in state["bindings"].values():
                b["status"] = "active"
                self.db.run("UPDATE conversations SET mode=?,auto_since=? WHERE tenant=? AND user_id=? AND channel=?",
                            ("manual" if b["paused"] or not b.get("start_ai", True) else "auto", boundary, state["tenant"], b["user_id"], b["channel"]))
            self.save_discovery(state)
            c = self.conv(job["conversation"])
            if c["mode"] == "auto":
                self.enqueue("generate", c, {"test_start": c["last_customer"] == job["trigger_id"]}, origin="ai", trigger=c["last_customer"])
            return {"status": "active", "acceptance": "not_verified"}

    def test_mode(self, channel, enabled):
        with self.db.connect():
            state = self.discovery()
            b = state["bindings"].get(channel)
            require(b, "test_not_bound", "此渠道尚未识别测试账号", 409)
            require(not enabled or b["status"] == "active", "test_not_ready", "启动检查尚未完成，请先查看失败原因或重新识别", 409)
            return self.set_mode(b["local_id"], "auto" if enabled else "manual")

    def import_conversation(self, platform_id, expected_user="", expected_tenant=None):
        s, _ = self.settings.get()
        require(expected_tenant is None or expected_tenant == tenant_id(s), "config_changed", "导入期间租户已变化，请重试", 409)
        detail = providers.conversation(s, platform_id)
        with self.db.lock:
            current, _ = self.settings.get()
            require(tenant_id(s) == tenant_id(current), "config_changed", "租户配置已变化，请重试", 409)
            c = self.add_conversation(platform_id, expected_user, str(detail.get("message_source", "")), str(detail.get("channel_id", "")))
            if expected_user:
                require(not c["user_id"] or c["user_id"] == expected_user, "identity_mismatch", "会话客户关联不一致")
            job = self.enqueue("sync", c)
            return {"conversation_id": c["id"], "job_id": job}

    def sync(self, cid, full=False):
        c = self.conv(cid)
        s, revision = self.settings.get()
        try:
            detail = providers.conversation(s, c["platform_id"])
            with self.db.lock:
                require(self.settings.get()[1] == revision, "config_changed", "配置变化，历史同步已中止", 409)
                assigned = assigned_agent_id(detail, {})
                if "assigned_agent_id" in detail or assigned:
                    self.db.run("UPDATE conversations SET assigned_agent_id=? WHERE id=?", (assigned, cid))
                    c = self.conv(cid)
                if self.auto_scope_allowed(s) and assigned and assigned != s["reply_actor_id"]:
                    self.set_mode(cid, "manual")
                    raise Problem("outside_selected_agent", "会话已分配给其他坐席，已停止历史读取与 AI", 409)
            # The cursor must come from the last successful API sync, never a newer webhook message.
            start = c["sync_cursor"] if c["sync_complete"] and c["sync_cursor"] and not full else None
            cursor = c["sync_cursor"]
            count = 0
            for page in providers.history_pages(s, c["platform_id"], start):
                with self.db.lock:
                    current, rev = self.settings.get()
                    require(rev == revision and tenant_id(current) == c["tenant"], "config_changed", "配置变化，历史同步已中止", 409)
                    c = self.conv(cid)
                    for row in page:
                        m = normalize_message(row, c["platform_id"])
                        cursor = max(cursor, m["created"])
                        count += self.insert_message(c, m)
                        if m["actor"] == "agent" and m["actor_id"] != s["reply_actor_id"] and m["created"] > c["auto_since"]:
                            binding = self.discovery()["bindings"].get(c["channel"], {})
                            if c["mode"] == "auto" or (binding.get("status") == "starting" and binding.get("user_id") == c["user_id"]):
                                self.set_mode(cid, "manual")
            with self.db.lock:
                current, rev = self.settings.get()
                require(rev == revision and tenant_id(current) == c["tenant"], "config_changed", "配置变化，历史同步已中止", 409)
                self.db.run("UPDATE conversations SET sync_complete=1,sync_error=NULL,synced_at=?,sync_cursor=? WHERE id=?", (time.time(), cursor, cid))
                c = self.conv(cid)
            if c["channel"] != "unknown":
                self.settings.record(c["channel"], "history", "passed", c["platform_id"], {"new_messages": count, "scope": "所有可访问页；平台已删除记录不包含"}, revision=revision, tenant=c["tenant"])
            self.db.log(c["tenant"], cid, "history_synced", f"新增 {count} 条；所有可访问页已读取")
            return {"new_messages": count, "complete": True}
        except Problem as e:
            self.db.run("UPDATE conversations SET sync_error=?,sync_complete=0 WHERE id=?", (e.message, cid))
            self.db.log(c["tenant"], cid, "history_failed", e.message)
            raise

    def send_guard(self, c, s):
        require(c["tenant"] == tenant_id(s), "tenant_changed", "当前租户已变化", 409)
        require(s["reply_actor_id"] and s["freshchat_token"] and s["platform_api_base_url"], "send_not_configured", "平台发送配置不完整", 409)
        require(not self.auto_scope_allowed(s) or not c.get("assigned_agent_id") or c["assigned_agent_id"] == s["reply_actor_id"],
                "outside_selected_agent", "会话已分配给其他坐席", 409)
        require(self.quick_scope(c["user_id"], c["source"]), "identity_denied", "该账号未绑定当前测试渠道", 403)
        require(c["channel"] != "unknown" and c["channel"] in s["allowed_channels"], "channel_denied", "当前来源未映射或渠道未列入白名单", 409)
        require(test_identity_allowed(s, c["platform_id"], c["user_id"]) or self.auto_scope_allowed(s),
                "identity_denied", "当前客户或会话未列入测试白名单", 403)

    def asset(self, aid):
        require(isinstance(aid, str) and 0 < len(aid) <= 260, "invalid_asset_id", "素材 ID 格式不正确")
        s, _ = self.settings.get()
        a = self.db.one("SELECT * FROM assets WHERE id=? AND tenant=?", (aid, tenant_id(s)))
        require(a, "asset_not_found", "素材不存在或不属于当前租户", 404)
        a["ref"] = self.db.unseal(a["ref"]) if a["ref"] else None
        a["channels"], a["tags"] = json.loads(a["channels"]), json.loads(a["tags"])
        return a

    def validate_plan(self, plan, c, automatic=False):
        s, revision = self.settings.get()
        require(isinstance(plan, dict) and set(plan) == {"messages", "needs_human", "ticket_reason"}, "invalid_plan", "回复计划字段不正确")
        require(type(plan["needs_human"]) is bool and (plan["ticket_reason"] is None or isinstance(plan["ticket_reason"], str) and len(plan["ticket_reason"]) <= 500), "invalid_plan", "人工接管或工单建议格式不正确")
        require(isinstance(plan["messages"], list) and 0 <= len(plan["messages"]) <= s["max_reply_messages"] and (plan["messages"] or plan["needs_human"]), "invalid_plan", "回复为空或条数超过限制")
        for m in plan["messages"]:
            require(isinstance(m, dict) and set(m) == {"type", "text", "asset_id"} and m["type"] in ("text", "image", "video", "file"), "invalid_plan", "回复类型或字段不正确")
            if m["type"] == "text":
                require(isinstance(m["text"], str) and m["text"].strip() and len(m["text"]) <= 10000 and m["asset_id"] is None, "invalid_plan", "文本须非空、不超过10000字符，且不能附带 asset_id")
            else:
                require(m["text"] is None and isinstance(m["asset_id"], str), "invalid_plan", "媒体需指定审核素材 ID，text 为 null")
                a = self.asset(m["asset_id"])
                require(a["kind"] == m["type"] and a["state"] == "sendable" and a["enabled"] and a["ref"] and c["channel"] in a["channels"], "asset_not_sendable", "素材类型、版本、可发送状态或渠道授权不符合要求")
                require(a["size"] <= s["media_size_limits"][a["kind"]] and a["mime"] in s["allowed_mime_types"], "asset_limit", "素材超出当前媒体限制")
                if automatic:
                    require(self.settings.passed(c["channel"], m["type"], s, revision), "media_unverified", "当前渠道尚未通过该媒体类型三层验证", 409)
        return plan

    def manual_send(self, cid, messages):
        with self.db.connect():
            c = self.conv(cid)
            s, _ = self.settings.get()
            self.send_guard(c, s)
            plan = self.validate_plan({"messages": messages, "needs_human": False, "ticket_reason": None}, c)
            mode = self.set_mode(cid, "manual")
            batch = uuid.uuid4().hex
            jobs = [self.enqueue("send", c, m, batch=batch, seq=i) for i, m in enumerate(plan["messages"])]
            return {"batch": batch, "job_ids": jobs, **mode}

    def preview(self, cid):
        c = self.conv(cid)
        require(not self.db.one("SELECT id FROM jobs WHERE conversation=? AND kind='generate' AND origin='preview' AND state IN ('queued','generating')", (cid,)), "preview_busy", "该会话已有预览任务", 409)
        return {"job_id": self.enqueue("generate", c, origin="preview", trigger=c["last_customer"])}

    def model_call(self, s, payload, context):
        with self.model_lock:
            now = time.time()
            tenant = tenant_id(s)
            day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            calls = self.db.one("SELECT count(*) AS n FROM usage WHERE tenant=? AND created>?", (tenant, now - 60))["n"]
            spent = self.db.one("SELECT coalesce(sum(coalesce(tokens,reserved)),0) AS n FROM usage WHERE tenant=? AND created>=?", (tenant, day))["n"]
            reserve = context["estimated_token_upper_bound"] + s["max_output_tokens"]
            require(calls < s["ai_requests_per_minute"], "local_rate_limit", "已达到 Demo 每分钟 AI 请求限制", 429)
            require(spent + reserve <= s["daily_token_budget"], "daily_budget", "每日 Token 预算不足，已暂停生成", 429)
            usage_id = self.db.run("INSERT INTO usage(tenant,created,reserved) VALUES(?,?,?)", (tenant, now, reserve))
            data, request_id = providers.openai(s, payload)
            elapsed = round((time.time() - now) * 1000)
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            tokens = usage.get("total_tokens")
            if type(tokens) is not int or tokens < 0:
                tokens = None
            self.db.run("UPDATE usage SET tokens=?,request_id=?,elapsed_ms=? WHERE id=?", (tokens, request_id[:200], elapsed, usage_id))
            return providers.parse_response(data), {"request_id": request_id[:200], "response_id": str(data.get("id", ""))[:200],
                                                    "model": s["openai_model"], "elapsed_ms": elapsed,
                                                    "usage": {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens") if type(usage.get(k)) is int}, "context": context}

    def check_openai(self):
        s, revision = self.settings.get()
        payload, ctx = providers.response_payload(s, [{"id": "connection-check", "actor": "user", "text": "請回覆一條簡短測試文字。"}], [])
        try:
            plan, result = self.model_call(s, payload, ctx)
            require(plan.get("messages") and all(m.get("type") == "text" for m in plan["messages"]), "model_check_plan", "检查请求应返回文本计划")
            self.validate_plan(plan, {"channel": "*"})
            self.settings.record("*", "openai", "passed", s["openai_model"], result, revision=revision, tenant=tenant_id(s))
            return result
        except Problem as e:
            self.settings.record("*", "openai", "blocked" if e.status in (409, 429) else "failed", s["openai_model"], {"code": e.code, "error": e.message}, revision=revision, tenant=tenant_id(s))
            raise

    def generate(self, job):
        cid = job["conversation"]
        self.assert_current(job, self.conv(cid))
        self.sync(cid)
        c = self.conv(cid)
        s, _ = self.settings.get()
        self.assert_current(job, c)
        rows = self.db.all("SELECT * FROM messages WHERE conversation=? AND private=0 AND actor IN ('user','agent') ORDER BY created,platform_id", (cid,))
        history = []
        for row in rows:
            parts = self.db.unseal(row["parts"])
            visible = [{k: v for k, v in p.items() if k in ("type", "text", "name", "mime", "size")} for p in parts]
            history.append({"id": row["platform_id"], "actor": row["actor"], "time": row["created"], "parts": visible})
        require(history, "history_empty", "会话没有可提供给模型的公开客户历史", 409)
        assets = []
        for a in self.db.all("SELECT id,kind,name,purpose,tags,channels FROM assets WHERE tenant=? AND state='sendable' AND enabled=1", (c["tenant"],)):
            if c["channel"] in json.loads(a["channels"]) and (job["origin"] != "ai" or self.settings.passed(c["channel"], a["kind"])):
                assets.append({"asset_id": a["id"], "type": a["kind"], "name": a["name"], "purpose": a["purpose"], "tags": json.loads(a["tags"])})
        payload, context = providers.response_payload(s, history, assets)
        if self.db.unseal(job["payload"]).get("test_start"):
            payload["instructions"] += "\n這一輪是測試帳號啟用通知。請簡短說明 AI 客服測試已啟用並邀請客戶提出停車問題，不重複識別碼，不宣稱渠道驗收通過。"
        plan, result = self.model_call(s, payload, context)
        result["plan"] = plan
        self.db.run("UPDATE jobs SET result=?,updated=? WHERE id=?", (self.db.seal(result), time.time(), job["id"]))
        self.db.log(c["tenant"], cid, "model_completed", f"{s['openai_model']} / {result['elapsed_ms']} ms")
        with self.db.connect():
            c = self.conv(cid)
            self.assert_current(job, c)
            self.validate_plan(plan, c, job["origin"] == "ai")
            self.db.log(c["tenant"], cid, "plan_validated", f"{len(plan['messages'])} 条独立回复")
            if job["origin"] == "ai":
                for i, m in enumerate(plan["messages"]):
                    self.enqueue("send", c, m, origin="ai", trigger=job["trigger_id"], batch=job["id"], seq=i)
                if plan["needs_human"]:
                    # Hand off immediately: suggested messages remain visible in the preview, never fight a human agent.
                    self.set_mode(cid, "manual")
                if plan["ticket_reason"] and s["ticket_policy"] == "automatic" and plan["ticket_reason"] in s["ticket_allowed_reasons"]:
                    try:
                        self.create_ticket(cid, plan["ticket_reason"], automatic=True)
                    except Problem as e:
                        self.db.log(c["tenant"], cid, "ticket_blocked", e.message)
            self.db.run("UPDATE jobs SET state='completed',result=?,updated=? WHERE id=? AND state!='cancelled'", (self.db.seal(result), time.time(), job["id"]))
        return result

    def assert_current(self, job, c):
        s, rev = self.settings.get()
        state = self.db.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"]
        require(state != "cancelled" and rev == job["revision"] and c["tenant"] == tenant_id(s), "stale_plan", "配置或任务状态已变化，旧计划已取消", 409)
        if job["origin"] == "ai":
            self.send_guard(c, s)
            self.settings.auto_ready(s, rev, c["channel"])
            require(c["mode"] == "auto" and c["last_customer"] == job["trigger_id"], "stale_plan", "客户追加消息或人工接管，旧计划已取消", 409)

    def submit(self, job):
        with self.db.lock:
            c = self.conv(job["conversation"])
            self.assert_current(job, c)
            s, _ = self.settings.get()
            self.send_guard(c, s)
            message = self.db.unseal(job["payload"])
            self.validate_plan({"messages": [message], "needs_human": False, "ticket_reason": None}, c, job["origin"] == "ai")
            a = self.asset(message["asset_id"]) if message["asset_id"] else None
            if a and a["kind"] == "video" and a["ref"].get("local_video"):
                require(s["public_base_url"], "public_url_missing", "本地视频需部署公网 HTTPS 才能被平台抓取", 409)
                a["ref"] = {"url": self.media_url(a["id"], s)}
            self.db.run("UPDATE jobs SET state='sending',attempts=attempts+1,updated=? WHERE id=?", (time.time(), job["id"]))
        result = providers.send_message(s, c["platform_id"], message, a)
        mid = result.get("id") if isinstance(result, dict) else None
        require(isinstance(mid, str) and mid, "acceptance_unknown", "平台响应未提供消息 ID，需人工核实外发结果", 502)
        identifier(mid)
        event = self.db.one("SELECT created FROM events WHERE tenant=? AND conversation=? AND platform_id=? AND action='message_create'", (c["tenant"], c["id"], job["trigger_id"])) if job["trigger_id"] else None
        result = {"accepted": True, "message_id": mid, "delivery": "not_confirmed", "accepted_at": utc(),
                  "send_queue_to_acceptance_ms": round((time.time() - job["created"]) * 1000),
                  "inbound_to_acceptance_ms": round((time.time() - event["created"]) * 1000) if event else None}
        self.db.run("UPDATE jobs SET state='accepted',platform_id=?,result=?,updated=? WHERE id=?", (mid, self.db.seal(result), time.time(), job["id"]))
        self.db.log(c["tenant"], c["id"], "platform_accepted", mid)
        return None

    def drain(self, limit=20):
        require(self.worker_lock.acquire(blocking=False), "job_locked", "已有任务正在处理", 409)
        counts = {"selected": 0, "processed": 0, "failed": 0}
        try:
            for _ in range(limit):
                job = self.db.one("SELECT * FROM jobs WHERE state IN ('queued','pending') AND due<=? ORDER BY created,seq LIMIT 1", (time.time(),))
                if not job:
                    break
                counts["selected"] += 1
                if job["batch"] and job["seq"]:
                    previous = self.db.one("SELECT state FROM jobs WHERE batch=? AND seq=?", (job["batch"], job["seq"] - 1))
                    if not previous or previous["state"] not in ("accepted", "delivered"):
                        self.db.run("UPDATE jobs SET state='paused',error='前一条未被平台受理，后续已暂停',updated=? WHERE id=?", (time.time(), job["id"]))
                        continue
                try:
                    s, revision = self.settings.get()
                    require(job["tenant"] == tenant_id(s) and revision == job["revision"], "stale_plan", "配置已变化，任务取消", 409)
                    if job["origin"] == "webhook":
                        c = self.conv(job["conversation"])
                        require(test_identity_allowed(s, c["platform_id"], c["user_id"]) or self.auto_scope_allowed(s),
                                "stale_plan", "已移出测试范围，后台同步取消", 409)
                    if job["kind"] not in ("send", "ticket"):
                        self.db.run("UPDATE jobs SET state='generating',attempts=attempts+1,updated=? WHERE id=? AND state='queued'", (time.time(), job["id"]))
                    if job["kind"] == "sync":
                        result = self.sync(job["conversation"], self.db.unseal(job["payload"]).get("full", False))
                    elif job["kind"] == "activate_test":
                        result = self.activate_test(job)
                    elif job["kind"] == "generate":
                        result = self.generate(job)
                    elif job["kind"] == "send":
                        result = self.submit(job)
                    elif job["kind"] == "ticket":
                        result = self.submit_ticket(job)
                    else:
                        raise Problem("invalid_job", "未知任务类型")
                    if result is not None:
                        self.db.run("UPDATE jobs SET state='completed',result=?,updated=? WHERE id=? AND state!='cancelled'", (self.db.seal(result), time.time(), job["id"]))
                    counts["processed"] += 1
                except Problem as e:
                    self.fail_job(job, e)
                    counts["failed"] += 1
                except Exception:
                    self.fail_job(job, Problem("internal_error", "任务内部错误；外发状态需核实", 500))
                    counts["failed"] += 1
            return counts
        finally:
            self.worker_lock.release()

    def fail_job(self, job, error):
        current = self.db.one("SELECT state,attempts FROM jobs WHERE id=?", (job["id"],))
        s, _ = self.settings.get()
        uncertain = error.code in ("network_unknown", "upstream_unknown", "invalid_response", "acceptance_unknown", "internal_error", "response_too_large", "invalid_id")
        state = "unknown" if current["state"] == "sending" and uncertain else "failed"
        if current["state"] == "cancelled" or error.code == "stale_plan":
            state = "cancelled"
        safe = error.code == "rate_limited" or job["kind"] == "sync" and error.code in ("network_failed", "upstream_unavailable")
        if safe and state != "cancelled" and current["attempts"] <= s["max_safe_retries"]:
            state = "pending" if job["kind"] == "send" else "queued"
        self.db.run("UPDATE jobs SET state=?,error_code=?,error=?,due=?,updated=? WHERE id=?",
                    (state, error.code, error.message, time.time() + max(error.retry_after, 2 ** current["attempts"]), time.time(), job["id"]))
        if job["kind"] == "ticket":
            self.db.run("UPDATE tickets SET state=? WHERE job_id=?", (state, job["id"]))
        if job["kind"] == "activate_test" and state in ("failed", "unknown"):
            with self.db.lock:
                discovery = self.discovery()
                if discovery.get("activation_job") == job["id"]:
                    for b in discovery["bindings"].values():
                        if b["status"] == "starting":
                            b.update(status="failed", error=error.message)
                    self.save_discovery(discovery)
        if state in ("failed", "unknown", "cancelled") and job["batch"]:
            self.db.run("UPDATE jobs SET state='paused',error='同一计划前一条失败或结果不明，需人工处理',updated=? WHERE batch=? AND seq>? AND state IN ('pending','queued')", (time.time(), job["batch"], job["seq"]))
        self.db.log(job["tenant"], job["conversation"], "job_" + state, error.message)

    def save_asset(self, metadata, data, filename, claimed_mime="", remote_url=""):
        s, _ = self.settings.get()
        require(isinstance(metadata, dict), "asset_format", "素材信息须为对象")
        logical = identifier(metadata.get("asset_id"))
        name, purpose = metadata.get("name", ""), metadata.get("purpose", "")
        require(isinstance(name, str) and 0 < len(name) <= 200 and isinstance(purpose, str) and 0 < len(purpose) <= 2000,
                "asset_metadata", "素材名称和用途必填，长度分别不超过200和2000字符")
        channels, tags = metadata.get("channels", []), metadata.get("tags", [])
        require(isinstance(channels, list) and channels and all(isinstance(x, str) and x in s["allowed_channels"] for x in channels), "asset_channels", "素材至少选择一个已允许的真实渠道")
        require(isinstance(tags, list) and len(tags) <= 20 and all(isinstance(x, str) and len(x) <= 80 for x in tags), "asset_tags", "素材标签格式不正确")
        kind, mime = security.detect_file(filename, data, claimed_mime)
        require(mime in s["allowed_mime_types"] and len(data) <= s["media_size_limits"][kind], "asset_limit", "文件超过对应媒体大小限制或 MIME 未获允许")
        with self.db.lock:
            prev = self.db.one("SELECT coalesce(max(version),0) AS v FROM assets WHERE tenant=? AND logical_id=?", (tenant_id(s), logical))
            version = prev["v"] + 1
            aid = logical + "_v" + str(version) + "_" + uuid.uuid4().hex[:8]
            path = self.files / (uuid.uuid4().hex + Path(filename).suffix.lower())
            path.write_bytes(data)
            path.chmod(0o600)
            state = "sendable" if kind == "video" and s["public_base_url"] else "pending_upload"
            # Send the reviewed bytes, including remote downloads, through a version-bound signed URL.
            ref = {"local_video": True} if kind == "video" and s["public_base_url"] else None
            self.db.run("INSERT INTO assets(id,tenant,logical_id,version,kind,name,purpose,tags,filename,mime,size,path,remote_url,ref,state,channels,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, tenant_id(s), logical, version, kind, name, purpose, dump(tags), Path(filename).name[:200], mime, len(data), str(path), remote_url, self.db.seal(ref) if ref else None, state, dump(channels), time.time()))
        return self.public_asset(self.asset(aid))

    def remote_asset(self, metadata):
        s, revision = self.settings.get()
        url = metadata.get("url", "")
        p = security.url_parts(url, s["media_host_allowlist"])
        require(not p.query, "credential_url", "审核素材 URL 不允许查询参数或凭证；可上传本地文件")
        raw, headers = security.request(url, hosts=s["media_host_allowlist"], redirects=3, limit=max(s["media_size_limits"].values()))
        filename = metadata.get("filename") or Path(p.path).name
        with self.db.lock:
            current, rev = self.settings.get()
            require(rev == revision and tenant_id(current) == tenant_id(s), "config_changed", "下载期间配置已变化，请重新审核素材", 409)
            return self.save_asset(metadata, raw, filename, headers.get("content-type", ""), url)

    def upload_asset(self, aid):
        a = self.asset(aid)
        s, revision = self.settings.get()
        require(a["enabled"] and a["path"], "asset_disabled", "素材已停用或原文件不存在", 409)
        if a["kind"] == "video":
            require(s["public_base_url"], "public_url_missing", "视频使用审核后的本地版本，需要配置本应用公网 HTTPS 地址", 409)
            ref, state = {"local_video": True}, "sendable"
        else:
            require(s["platform_api_base_url"] and s["freshchat_token"], "platform_not_configured", "请先配置平台 Token 和区域地址", 409)
            try:
                ref, state = providers.upload(s, a, Path(a["path"]).read_bytes())
            except Problem as e:
                self.db.run("UPDATE assets SET state='failed',error=? WHERE id=?", (e.message, aid))
                raise
        with self.db.lock:
            current, rev = self.settings.get()
            require(rev == revision and tenant_id(current) == a["tenant"], "config_changed", "配置已变化，请重新上传", 409)
            self.db.run("UPDATE assets SET ref=?,state=?,error=NULL WHERE id=?", (self.db.seal(ref), state, aid))
        return self.public_asset(self.asset(aid))

    def public_asset(self, a):
        return {k: a[k] for k in ("id", "logical_id", "version", "kind", "name", "purpose", "tags", "filename", "mime", "size", "state", "enabled", "channels", "error", "created")} | {
            "preview_url": "/api/assets/" + a["id"] + "/content", "has_platform_reference": bool(a["ref"]),
            "platform_reference": {k: a["ref"][k] for k in ("id", "file_hash", "file_security_status") if a["ref"] and k in a["ref"]},
            "source": "审核 URL" if a["remote_url"] else "本地上传"}

    def edit_asset(self, aid, data):
        a = self.asset(aid)
        require(isinstance(data, dict) and set(data) <= {"name", "purpose", "tags", "enabled", "channels"}, "asset_edit", "仅可编辑名称、用途、标签、渠道及启用状态；替换文件需新版本")
        updated = {**a, **data}
        s, _ = self.settings.get()
        require(type(updated["enabled"]) in (bool, int) and updated["enabled"] in (0, 1), "asset_edit", "启用状态格式不正确")
        require(isinstance(updated["name"], str) and 0 < len(updated["name"]) <= 200 and isinstance(updated["purpose"], str) and 0 < len(updated["purpose"]) <= 2000, "asset_edit", "名称或用途格式不正确")
        require(isinstance(updated["channels"], list) and all(ch in s["allowed_channels"] for ch in updated["channels"]), "asset_edit", "素材渠道未获允许")
        require(isinstance(updated["tags"], list) and all(isinstance(t, str) and len(t) <= 80 for t in updated["tags"]), "asset_edit", "标签格式不正确")
        state = "disabled" if not updated["enabled"] else (a["state"] if a["state"] != "disabled" else "pending_upload")
        self.db.run("UPDATE assets SET name=?,purpose=?,tags=?,enabled=?,channels=?,state=? WHERE id=?", (updated["name"], updated["purpose"], dump(updated["tags"]), int(updated["enabled"]), dump(updated["channels"]), state, aid))
        return self.public_asset(self.asset(aid))

    def media_url(self, aid, s):
        # Fernet supplies authenticated expiry; token grants this one asset, never admin access.
        token = self.db.cipher.encrypt(dump({"purpose": "platform_video", "asset": aid, "tenant": tenant_id(s)}).encode()).decode()
        return s["public_base_url"] + "/media/" + token

    def media_token(self, token):
        try:
            data = json.loads(self.db.cipher.decrypt(token.encode(), ttl=86400))
        except Exception:
            raise Problem("media_token", "媒体访问地址无效或已过期", 403) from None
        s, _ = self.settings.get()
        require(data.get("purpose") == "platform_video" and data.get("tenant") == tenant_id(s), "media_token", "媒体访问授权无效", 403)
        a = self.asset(data.get("asset"))
        require(a["kind"] == "video" and a["enabled"] and a["state"] == "sendable", "media_disabled", "该视频已停用", 403)
        return a

    def create_ticket(self, cid, reason, new_matter=False, automatic=False):
        require(isinstance(reason, str) and 0 < len(reason.strip()) <= 500, "ticket_reason", "请填写不超过500字符的跟进事项")
        c = self.conv(cid)
        s, _ = self.settings.get()
        self.send_guard(c, s)
        require(s["freshdesk_domain"] and s["freshdesk_api_key"], "freshdesk_not_configured", "请配置 Freshdesk 凭证", 409)
        requester = s["requester_mapping"].get(c["user_id"])
        require(type(requester) is int and requester > 0, "requester_missing", "请将该 Freshchat 客户绑定至真实 requester_id", 409)
        if automatic:
            require(s["ticket_policy"] == "automatic" and reason in s["ticket_allowed_reasons"], "ticket_policy", "原因未获准自动建单", 409)
        # One active follow-up per conversation by default; only an explicit new matter opens another slot.
        with self.db.connect():
            existing = self.db.one("SELECT * FROM tickets WHERE tenant=? AND conversation=? ORDER BY id DESC LIMIT 1", (c["tenant"], cid))
            if existing and not new_matter:
                return existing
            require(not existing or existing["state"] != "unknown", "ticket_unknown", "已有工单提交结果不明，请先核实后处理", 409)
            matter = uuid.uuid4().hex if new_matter else "followup"
            job = self.enqueue("ticket", c, {"reason": reason, "matter": matter, "requester_id": requester}, origin="ticket")
            self.db.run("INSERT INTO tickets(tenant,conversation,matter,job_id,state) VALUES(?,?,?,?,?)", (c["tenant"], cid, matter, job, "queued"))
            return self.db.one("SELECT * FROM tickets WHERE job_id=?", (job,))

    def submit_ticket(self, job):
        with self.db.lock:
            c = self.conv(job["conversation"])
            self.assert_current(job, c)
            s, _ = self.settings.get()
            self.send_guard(c, s)
            p = self.db.unseal(job["payload"])
            require(s["requester_mapping"].get(c["user_id"]) == p["requester_id"], "requester_changed", "客户工单映射已变化")
            description = "<p>" + html.escape(p["reason"]) + "</p><p>来源：Freshchat 会话 " + html.escape(c["platform_id"]) + "；渠道 " + html.escape(c["channel"]) + "</p><p>本地映射，不代表 Freshchat 原生双向关联。未搬入完整聊天。</p>"
            payload = {"requester_id": p["requester_id"], "subject": "LSP 停车客服跟进：" + p["reason"][:100], "description": description,
                       "priority": s["priority"], "status": s["status"], "tags": s["ticket_tags"], "custom_fields": s["custom_field_mapping"]}
            if s["ticket_group_id"]:
                payload["group_id"] = s["ticket_group_id"]
            self.db.run("UPDATE jobs SET state='sending',attempts=attempts+1,updated=? WHERE id=?", (time.time(), job["id"]))
            self.db.run("UPDATE tickets SET state='sending' WHERE job_id=?", (job["id"],))
        ticket = providers.freshdesk(s, "/tickets", "POST", payload)
        require(isinstance(ticket, dict) and type(ticket.get("id")) is int and ticket["id"] > 0, "acceptance_unknown", "建单未返回有效 ID，需核实是否创建", 502)
        result = {"ticket_id": ticket["id"], "status": ticket.get("status"), "url": s["freshdesk_domain"] + "/a/tickets/" + str(ticket["id"])}
        self.db.run("UPDATE tickets SET ticket_id=?,status=?,url=?,state='accepted' WHERE job_id=?", (result["ticket_id"], result["status"], result["url"], job["id"]))
        self.settings.record(c["channel"], "ticket", "passed", c["platform_id"], result, job["id"], job["revision"], c["tenant"])
        return result

    def verify_outbound(self, job_id, data):
        job = self.public_job(job_id)
        require(job["kind"] == "send" and job["state"] in ("accepted", "delivered"), "not_accepted", "仅能核对已由 API 受理并返回消息 ID 的消息", 409)
        require(data.get("omni_visible") is True and data.get("customer_received") is True and isinstance(data.get("evidence"), str) and 3 <= len(data["evidence"]) <= 2000,
                "evidence_required", "需人工确认原工作台发送者正确、客户实际收到且可打开，并填写脱敏证据")
        c = self.conv(job["conversation"])
        evidence = {"api_message_id": job["platform_id"], "omni_visible": True, "customer_received": True,
                    "confirmation": "manual", "evidence": data["evidence"], "confirmed_at": utc()}
        result = job["result"] or {}
        result["verification"] = evidence
        self.db.run("UPDATE jobs SET result=?,updated=? WHERE id=?", (self.db.seal(result), time.time(), job_id))
        kind = job["payload"]["type"]
        capability = ("ai_text" if kind == "text" else "ai_media") if job["origin"] == "ai" else ("manual_text" if kind == "text" else kind)
        self.settings.record(c["channel"], capability, "passed", c["platform_id"], evidence, job_id, job["revision"], job["tenant"])
        self.settings.record(c["channel"], "omni_visible", "passed", c["platform_id"], evidence, job_id, job["revision"], job["tenant"])
        return {"status": "recorded", "delivery": "人工确认收到；无机器送达回执，任务仍为平台已受理"}

    def public_job(self, job_id):
        identifier(job_id)
        s, _ = self.settings.get()
        job = self.db.one("SELECT * FROM jobs WHERE id=? AND tenant=?", (job_id, tenant_id(s)))
        require(job, "job_not_found", "当前租户不存在该任务", 404)
        job["payload"] = self.db.unseal(job["payload"])
        job["result"] = self.db.unseal(job["result"]) if job["result"] else None
        return job

    def resolve_job(self, job_id, data):
        with self.db.lock:
            job = self.public_job(job_id)
            require(job["state"] in ("unknown", "failed", "paused"), "job_not_resolvable", "该任务无需人工处理", 409)
            action = data.get("action")
            evidence = data.get("evidence", "")
            require(isinstance(evidence, str) and 3 <= len(evidence) <= 2000, "evidence_required", "请填写核实依据或人工重发原因")
            if action == "cancel":
                self.db.run("UPDATE jobs SET state='cancelled',error='管理员确认取消',updated=? WHERE id=?", (time.time(), job_id))
                if job["kind"] == "ticket":
                    self.db.run("UPDATE tickets SET state='cancelled' WHERE job_id=?", (job_id,))
            elif action == "link_existing" and job["kind"] == "ticket":
                ticket_id = data.get("platform_id")
                require(isinstance(ticket_id, str) and ticket_id.isdigit(), "invalid_id", "需填写真实工单编号")
                s, _ = self.settings.get()
                ticket = providers.freshdesk(s, "/tickets/" + ticket_id)
                require(ticket.get("id") == int(ticket_id) and ticket.get("requester_id") == job["payload"].get("requester_id"), "ticket_mismatch", "工单编号或 requester 不匹配")
                url = s["freshdesk_domain"] + "/a/tickets/" + ticket_id
                self.db.run("UPDATE tickets SET state='accepted',ticket_id=?,status=?,url=? WHERE job_id=?", (int(ticket_id), ticket.get("status"), url, job_id))
                self.db.run("UPDATE jobs SET state='completed',platform_id=?,updated=? WHERE id=?", (ticket_id, time.time(), job_id))
            elif action == "retry":
                require(data.get("ack_duplicate_risk") is True, "duplicate_risk", "必须明确确认可能重复外发的风险")
                c = self.conv(job["conversation"])
                s, rev = self.settings.get()
                self.send_guard(c, s)
                self.set_mode(c["id"], "manual")
                require(job["kind"] in ("send", "ticket", "sync"), "no_retry", "请重新生成预览，不能重试旧 AI 生成任务")
                if job["kind"] == "ticket":
                    require(job["payload"]["requester_id"] == s["requester_mapping"].get(c["user_id"]), "requester_changed", "客户映射已变化")
                self.db.run("UPDATE jobs SET state=?,origin='admin',revision=?,due=?,error=NULL,error_code=NULL,updated=? WHERE id=?", ("pending" if job["kind"] == "send" else "queued", rev, time.time(), time.time(), job_id))
            else:
                raise Problem("invalid_resolution", "请选择取消、人工确认重试或关联已核实工单")
            result = job["result"] or {}
            result["manual_resolution"] = {"action": action, "evidence": evidence, "at": utc(), "duplicate_risk_acknowledged": data.get("ack_duplicate_risk") is True}
            self.db.run("UPDATE jobs SET result=? WHERE id=?", (self.db.seal(result), job_id))
            self.wake.set()
            return {"status": "recorded", "action": action}

    def cleanup(self, limit, dry_run):
        s, _ = self.settings.get()
        cutoff = time.time() - s["local_retention_days"] * 86400
        # Keep full context for active or unverified conversations; cleanup never touches platform data.
        protected = set()
        for job in self.db.all("SELECT conversation,state,result FROM jobs WHERE conversation IS NOT NULL"):
            verified = job["state"] == "accepted" and job["result"] and self.db.unseal(job["result"]).get("verification")
            if job["state"] not in ("completed", "cancelled", "delivered") and not verified:
                protected.add(job["conversation"])
        candidates = self.db.all("SELECT id,conversation FROM messages WHERE cached_at<? AND conversation IN (SELECT id FROM conversations WHERE mode!='auto') ORDER BY id", (cutoff,))
        rows = [row for row in candidates if row["conversation"] not in protected][:limit]
        if not dry_run:
            with self.db.connect() as conn:
                for row in rows:
                    conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))
                    conn.execute("UPDATE conversations SET sync_complete=0,sync_error='本地缓存已清理，请重新同步平台历史' WHERE id=?", (row["conversation"],))
                conn.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
                conn.execute("DELETE FROM logs WHERE id IN (SELECT id FROM logs WHERE created<? LIMIT ?)", (cutoff, limit))
                conn.execute("DELETE FROM usage WHERE created<?", (min(cutoff, time.time() - 86400),))
        return len(rows)

    def scheduler(self, body):
        require(isinstance(body, dict) and set(body) <= {"tasks", "limit", "dry_run", "allow_external_effects"}, "invalid_body", "调度请求字段不正确")
        tasks, limit = body.get("tasks", ["recover_pending", "cleanup_cache"]), body.get("limit", 20)
        dry, effects = body.get("dry_run", True), body.get("allow_external_effects", False)
        require(isinstance(tasks, list) and tasks and all(t in ("recover_pending", "cleanup_cache") for t in tasks) and len(tasks) == len(set(tasks)), "invalid_tasks", "任务仅允许 recover_pending、cleanup_cache 且不得重复")
        require(type(limit) is int and 1 <= limit <= 100 and type(dry) is bool and type(effects) is bool, "invalid_body", "limit 须为1–100整数，执行标记须为布尔值")
        require(dry or effects, "external_effects_required", "真实执行必须显式 allow_external_effects:true")
        pending = self.db.one("SELECT count(*) AS n FROM jobs WHERE state IN ('queued','pending') AND due<=?", (time.time(),))["n"] if "recover_pending" in tasks else 0
        unknown = self.db.one("SELECT count(*) AS n FROM jobs WHERE state='unknown'")["n"]
        result = {"status": "ok", "request_id": uuid.uuid4().hex, "dry_run": dry, "selected": min(pending, limit), "processed": 0, "failed": 0, "skipped_unknown": unknown}
        if not dry and "recover_pending" in tasks:
            result.update(self.drain(limit))
        if "cleanup_cache" in tasks:
            remaining = max(0, limit - result["selected"])
            require(self.worker_lock.acquire(blocking=False), "job_locked", "已有任务正在处理", 409)
            try:
                with self.db.connect():
                    count = self.cleanup(remaining, dry)
            finally:
                self.worker_lock.release()
            result["selected"] += count
            if not dry:
                result["processed"] += count
        return result
