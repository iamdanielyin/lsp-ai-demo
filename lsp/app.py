import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, make_response, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from . import providers, security
from .security import Problem, identifier, require
from .service import Service
from .settings import CAPABILITIES, tenant_id
from .store import Store, dump


def load_env(path=".env"):
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def create_app(config=None, start_worker=True):
    root = Path(__file__).resolve().parent.parent
    app = Flask(__name__, static_folder=str(root / "static"), template_folder=str(root / "templates"))
    app.config.update(MAX_CONTENT_LENGTH=26_000_000, DATABASE_PATH=os.getenv("DATABASE_PATH", "data/lsp.sqlite3"),
                      FILE_DIRECTORY=os.getenv("FILE_DIRECTORY", "data/files"), CONFIG_MASTER_KEY=os.getenv("CONFIG_MASTER_KEY", ""),
                      ADMIN_INITIAL_PASSWORD=os.getenv("ADMIN_INITIAL_PASSWORD", ""))
    if config:
        app.config.update(config)
    require(app.config["CONFIG_MASTER_KEY"] and not app.config["CONFIG_MASTER_KEY"].startswith("<"), "master_key_missing", "请先运行 python scripts/init_local.py 或配置启动主密钥")
    db = Store(app.config["DATABASE_PATH"], app.config["CONFIG_MASTER_KEY"])
    if not db.one("SELECT key FROM meta WHERE key='password'"):
        pwd = app.config["ADMIN_INITIAL_PASSWORD"]
        require(len(pwd) >= 16 and not pwd.startswith("<"), "admin_password_missing", "首次管理员口令至少16字符，请设置 ADMIN_INITIAL_PASSWORD")
        db.run("INSERT INTO meta VALUES('password',?)", (generate_password_hash(pwd),))
    service = Service(db, app.config["FILE_DIRECTORY"])
    app.extensions["service"] = service
    login_attempts, login_lock = [], threading.Lock()

    def body():
        data = request.get_json(silent=True)
        require(isinstance(data, dict), "invalid_body", "请求须为 JSON 对象")
        return data

    def secure_cookie():
        local = urlsplit(request.host_url).hostname in ("localhost", "127.0.0.1", "::1")
        return request.is_secure or not local

    @app.before_request
    def auth():
        if request.path.startswith("/api/"):
            if request.path == "/api/webhooks/freshchat" or request.path == "/api/internal/jobs/drain":
                return None
            if request.path in ("/api/auth", "/api/login"):
                return None
            sid = request.cookies.get("lsp_session", "")
            row = db.one("SELECT * FROM sessions WHERE id=? AND expires>?", (sid, time.time()))
            require(row, "login_required", "请先登录管理员账户", 401)
            g.session = row
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                require(hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), row["csrf"]), "csrf_invalid", "CSRF 校验失败，请刷新页面", 403)
                check_origin()

    def check_origin():
        origin = request.headers.get("Origin")
        s, _ = service.settings.get()
        # TLS ends at the tunnel; Host still identifies the browser's destination.
        # Do not trust X-Forwarded-* or tie the admin entry to the webhook URL.
        https_origin = "https://" + request.host.removesuffix(":443")
        allowed = (request.host_url.rstrip("/"), https_origin, s["public_base_url"])
        require(not origin or origin in allowed, "origin_invalid", "请求来源不允许，请从本地地址或已配置的公网地址打开；隧道须保留 Host", 403)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(Problem)
    def problem(error):
        return jsonify(error=error.code, message=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error="http_error", message={413: "文件或请求体超过26MB", 404: "接口不存在", 405: "请求方法不允许"}.get(error.code, "请求格式不正确")), error.code

    @app.errorhandler(Exception)
    def internal_error(error):
        # Raw tracebacks may contain input or provider responses. Tests still fail on the returned 500.
        if isinstance(error, sqlite3.Error):
            return jsonify(error="database_unavailable", message="数据库暂不可用，未确认持久化"), 503
        return jsonify(error="internal_error", message="服务器内部错误，请核对配置或查看任务状态"), 500

    @app.get("/")
    @app.get("/settings")
    @app.get("/conversations")
    def page():
        return render_template("index.html")

    @app.get("/api/auth")
    def auth_status():
        row = db.one("SELECT * FROM sessions WHERE id=? AND expires>?", (request.cookies.get("lsp_session", ""), time.time()))
        if row:
            return jsonify(authenticated=True, csrf=row["csrf"])
        nonce = secrets.token_urlsafe(32)
        response = jsonify(authenticated=False, csrf=nonce)
        response.set_cookie("lsp_login", nonce, httponly=True, secure=secure_cookie(), samesite="Strict", max_age=600)
        return response

    @app.post("/api/login")
    def login():
        check_origin()
        require(request.cookies.get("lsp_login") and hmac.compare_digest(request.cookies["lsp_login"], request.headers.get("X-CSRF-Token", "")), "csrf_invalid", "请刷新登录页面后重试", 403)
        data = body()
        pwd = data.get("password")
        require(isinstance(pwd, str) and len(pwd) <= 1000, "invalid_password", "请输入管理员口令")
        with login_lock:
            login_attempts[:] = [t for t in login_attempts if t > time.time() - 60]
            require(len(login_attempts) < 10, "login_rate_limit", "登录尝试过多，请一分钟后重试", 429)
            login_attempts.append(time.time())
        require(check_password_hash(db.one("SELECT value FROM meta WHERE key='password'")["value"], pwd), "login_failed", "管理员口令不正确", 401)
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.run("INSERT INTO sessions VALUES(?,?,?)", (sid, csrf, time.time() + 12 * 3600))
        response = jsonify(authenticated=True, csrf=csrf)
        response.set_cookie("lsp_session", sid, httponly=True, secure=secure_cookie(), samesite="Strict", max_age=12 * 3600)
        response.delete_cookie("lsp_login")
        return response

    @app.post("/api/logout")
    def logout():
        db.run("DELETE FROM sessions WHERE id=?", (g.session["id"],))
        response = jsonify(status="ok")
        response.delete_cookie("lsp_session")
        return response

    @app.route("/api/settings", methods=["GET", "PUT"])
    def settings():
        if request.method == "GET":
            return jsonify(service.settings.public())
        patch = body()
        with db.connect():
            service.settings.save(patch)
            if patch.get("reply_actor_id") and not patch.get("test_identity_allowlist"):
                service.resume_auto_discovery()
            return jsonify(service.settings.public())

    @app.get("/api/agents")
    def agents():
        s, _ = service.settings.get()
        rows = providers.agents(s)
        require(tenant_id(s) == tenant_id(service.settings.get()[0]), "config_changed", "读取期间租户已变化，请重新获取坐席", 409)
        return jsonify(agents=rows)

    @app.post("/api/conversations/<int:cid>/test-access")
    def test_access(cid):
        data = body()
        require(set(data) <= {"channel", "scope"} and isinstance(data.get("channel"), str), "invalid_channel", "请选择实际测试渠道")
        scope = data.get("scope", "conversation")
        require(scope in ("conversation", "customer"), "invalid_scope", "测试范围只能为会话或单个客户")
        channel = data["channel"].strip()
        require(0 < len(channel) <= 100 and channel != "unknown", "invalid_channel", "请填写有效的实际渠道名称")
        with db.lock:
            c = service.conv(cid)
            s, _ = service.settings.get()
            require(c["source"] and c["source"] != "unknown", "source_missing", "尚未读取到真实来源，请先同步历史或接收客户消息", 409)
            require(c["source"] not in s["source_mapping"] or s["source_mapping"][c["source"]] == channel,
                    "source_conflict", "此来源已对应其他渠道；请核实后在高级设置修改", 409)
            if scope == "customer":
                require(c["user_id"] and c["sync_complete"], "customer_not_verified", "请先同步所选会话历史，取得真实客户 ID 后再绑定", 409)
                identifier(c["user_id"])
            return jsonify(service.settings.save({
                "source_mapping": {**s["source_mapping"], c["source"]: channel},
                "allowed_channels": list(dict.fromkeys([*s["allowed_channels"], channel])),
                "test_identity_allowlist": (["user:" + c["user_id"]] if scope == "customer" else
                                            list(dict.fromkeys([*s["test_identity_allowlist"], "conversation:" + c["platform_id"]]))),
                "seed_conversation_id": c["platform_id"],
            }))

    @app.get("/api/status")
    def status():
        s, revision = service.settings.get()
        tenant = tenant_id(s)
        return jsonify(route="freshchat", tenant_verified=False, revision=revision, auto_reply_enabled=s["auto_reply_enabled"],
                       conversations=db.one("SELECT count(*) AS n FROM conversations WHERE tenant=?", (tenant,))["n"],
                       assets=db.one("SELECT count(*) AS n FROM assets WHERE tenant=? AND conversation IS NULL AND state='sendable' AND enabled=1", (tenant,))["n"],
                       pending=db.one("SELECT count(*) AS n FROM jobs WHERE tenant=? AND state IN ('queued','pending','generating','sending')", (tenant,))["n"],
                       attention=db.one("SELECT count(*) AS n FROM jobs WHERE tenant=? AND state IN ('failed','unknown','paused')", (tenant,))["n"])

    @app.post("/api/checks")
    def checks():
        data = body()
        kind = data.get("kind")
        if kind == "openai":
            return jsonify(service.check_openai())
        if kind == "platform_read":
            s, revision = service.settings.get()
            cid = data.get("conversation_id") or s["seed_conversation_id"]
            identifier(cid)
            try:
                detail = providers.conversation(s, cid)
                result = {"conversation_id": cid, "status": detail.get("status"), "checked_at": time.time()}
                service.settings.record("*", "platform_read", "passed", cid, result, revision=revision, tenant=tenant_id(s))
                return jsonify(result)
            except Problem as e:
                service.settings.record("*", "platform_read", "failed", cid, {"code": e.code, "error": e.message}, revision=revision, tenant=tenant_id(s))
                raise
        if kind == "send":
            require(data.get("confirm_send") is True, "confirm_required", "请明确确认外发目标和内容")
            return jsonify(service.manual_send(data.get("conversation_id"), data.get("messages")))
        if kind == "verify_outbound":
            return jsonify(service.verify_outbound(data.get("job_id"), data))
        if kind == "record":
            require(data.get("status") in ("not_tested", "failed", "blocked", "unsupported"), "evidence_status", "通过状态必须来自真实 API 和发送核验，不能手工直接标通过")
            s, _ = service.settings.get()
            require(data.get("channel") in s["allowed_channels"] and data.get("capability") in CAPABILITIES, "invalid_capability", "渠道或能力不正确")
            require(isinstance(data.get("evidence"), str) and 3 <= len(data["evidence"]) <= 2000, "evidence_required", "请填写脱敏证据；403 只能说明权限不足")
            return jsonify(id=service.settings.record(data["channel"], data["capability"], data["status"], str(data.get("target", ""))[:200], {"note": data["evidence"]}))
        raise Problem("unknown_check", "未知检查类型")

    @app.get("/api/capabilities")
    def capabilities():
        s, revision = service.settings.get()
        rows = db.all("SELECT * FROM checks WHERE tenant=? ORDER BY id DESC LIMIT 500", (tenant_id(s),))
        for r in rows:
            r["evidence"] = db.unseal(r["evidence"])
            r["stale"] = r["revision"] != revision
        channels = sorted(set(s["allowed_channels"]) | {r["channel"] for r in rows if r["channel"] != "*"})
        return jsonify(channels=channels, capabilities=CAPABILITIES, checks=rows, revision=revision)

    @app.get("/api/events")
    def events():
        s, _ = service.settings.get()
        rows = db.all("SELECT id,conversation,platform_id,action,version,retries,payload,created FROM events WHERE tenant=? ORDER BY id DESC LIMIT 50", (tenant_id(s),))
        for row in rows:
            row["payload"] = db.unseal(row["payload"]) if row["payload"] else None
        return jsonify(events=rows)

    @app.post("/api/conversations/import")
    def import_conversation():
        data = body()
        s, _ = service.settings.get()
        uid = data.get("user_id", "")
        cid = data.get("conversation_id", "") or ("" if uid else s["seed_conversation_id"])
        if cid:
            return jsonify(service.import_conversation(identifier(cid)))
        uid = uid or s["seed_user_id"]
        identifier(uid)
        ids = providers.user_conversations(s, uid)
        require(len(ids) <= 200, "import_limit", "该用户超过200个会话，请按会话 ID 分批导入")
        return jsonify(imported=[service.import_conversation(cid, uid, tenant_id(s)) for cid in ids])

    @app.route("/api/test-discovery", methods=["GET", "POST", "DELETE"])
    def test_discovery():
        if request.method == "POST":
            data = body()
            require(set(data) == {"channel", "confirm_auto_reply"} and data["confirm_auto_reply"] is True,
                    "confirm_required", "请确认识别后向该测试账号启动 AI 回复")
            state = service.start_discovery(data["channel"])
        else:
            if request.method == "DELETE":
                require(not body(), "invalid_body", "此接口只接受空 JSON 对象")
                service.pause_auto_discovery()
            state = service.discovery()
        s, _ = service.settings.get()
        result = {"auto_discovery": service.auto_scope_allowed(s), "enrollment": state["enrollment"], "next_binding": state.get("next_binding"),
                  "candidates": [{k: c[k] for k in ("channel", "conversation_id", "user_id", "source", "observed_at")}
                                 for c in state.get("candidates", [])], "bindings": []}
        for b in state["bindings"].values():
            c = service.conv(b["local_id"])
            result["bindings"].append({k: v for k, v in b.items() if k not in ("code_hash", "trigger_id")})
            result["bindings"][-1]["enabled"] = bool(not b["paused"] and c["mode"] == "auto")
        return jsonify(result)

    @app.post("/api/test-discovery/bind-next")
    def bind_next():
        data = body()
        require(set(data) == {"channel"} and isinstance(data["channel"], str), "invalid_body", "请指定要监听的实际渠道")
        state = service.start_next_binding(data["channel"])
        return jsonify(next_binding=state["next_binding"], candidates=[])

    @app.post("/api/test-discovery/select")
    def select_candidate():
        data = body()
        require(set(data) == {"conversation_id"} and isinstance(data["conversation_id"], str), "invalid_body", "请指定候选会话 ID")
        return jsonify(service.select_candidate(data["conversation_id"]))

    @app.put("/api/test-discovery/mode")
    def test_mode():
        data = body()
        require(set(data) == {"channel", "enabled"} and isinstance(data["channel"], str) and type(data["enabled"]) is bool, "invalid_body", "请指定渠道和布尔值 enabled")
        return jsonify(service.test_mode(data["channel"], data["enabled"]))

    @app.get("/api/conversations")
    def conversations():
        s, _ = service.settings.get()
        rows = db.all("SELECT * FROM conversations WHERE tenant=? ORDER BY updated DESC", (tenant_id(s),))
        if service.auto_scope_allowed(s):
            rows = [r for r in rows if not r["assigned_agent_id"] or r["assigned_agent_id"] == s["reply_actor_id"]]
        query = request.args.get("q", "").casefold()
        channel = request.args.get("channel", "")
        rows = [r for r in rows if not channel or channel == r["channel"]]
        for r in rows:
            r["customer_name"] = service.customer_name(r, refresh=True)
            r["mode"] = "auto" if r["mode"] == "auto" else "manual"
            latest = db.one("SELECT parts,created FROM messages WHERE conversation=? AND private=0 AND actor IN ('user','agent') ORDER BY created DESC,platform_id DESC LIMIT 1", (r["id"],))
            parts = db.unseal(latest["parts"]) if latest else []
            r["preview"] = " · ".join(p.get("text") or {"image": "[图片]", "video": "[视频]", "file": "[附件]"}.get(security.media_kind(p), "[消息]") for p in parts)[:120]
            r["preview_time"] = latest["created"] if latest else ""
        rows = [r for r in rows if not query or query in (r["customer_name"] + " " + r["preview"] + " " + ("AI" if r["mode"] == "auto" else "人工")).casefold()]
        rows.sort(key=lambda r: r["preview_time"], reverse=True)
        return jsonify(conversations=rows)

    @app.get("/api/conversations/<int:cid>/messages")
    def messages(cid):
        c = service.conv(cid)
        c["customer_name"] = service.customer_name(c)
        c["mode"] = "auto" if c["mode"] == "auto" else "manual"
        try:
            before, size = int(request.args.get("before", "0")), min(100, max(1, int(request.args.get("limit", "50"))))
        except ValueError:
            raise Problem("invalid_page", "分页参数不正确") from None
        rows = db.all("SELECT * FROM messages WHERE conversation=? ORDER BY created DESC,platform_id DESC LIMIT ? OFFSET ?", (cid, size, before))
        s, _ = service.settings.get()
        for r in rows:
            r["parts"] = db.unseal(r["parts"])
            r["role"] = "system" if r["actor"] == "system" else "private" if r["private"] else "customer" if r["actor"] == "user" else "agent"
            if r["actor_id"] == s["reply_actor_id"]:
                own = db.one("SELECT origin FROM jobs WHERE tenant=? AND conversation=? AND platform_id=? AND kind='send'", (c["tenant"], cid, r["platform_id"]))
                r["role"] = "ai" if own and own["origin"] == "ai" else r["role"]
            for i, part in enumerate(r["parts"]):
                if "url" in part:
                    part["type"] = security.media_kind(part)
                    original = part.pop("url")
                    part["url"] = f"/api/conversations/{cid}/media/{r['id']}/{i}" if original else ""
                    part["preview_note"] = "预览受来源白名单、有效期和浏览器支持限制；不能据此判定渠道发送失败"
        total = db.one("SELECT count(*) AS n,min(created) AS earliest,max(created) AS latest FROM messages WHERE conversation=?", (cid,))
        jobs = [service.public_job(r["id"]) for r in db.all("SELECT id FROM jobs WHERE conversation=? ORDER BY created DESC,seq DESC LIMIT 60", (cid,))]
        tickets = db.all("SELECT * FROM tickets WHERE tenant=? AND conversation=? ORDER BY id DESC", (c["tenant"], cid))
        logs = db.all("SELECT event,detail,created FROM logs WHERE conversation=? ORDER BY id DESC LIMIT 30", (cid,))
        return jsonify(conversation=c, messages=list(reversed(rows)), total=total["n"], earliest=total["earliest"], latest=total["latest"],
                       next_before=before + len(rows) if before + len(rows) < total["n"] else None, jobs=jobs, tickets=tickets, logs=logs)

    @app.post("/api/conversations/<int:cid>/sync")
    def sync(cid):
        return jsonify(job_id=service.enqueue("sync", service.conv(cid), {"full": True}))

    @app.post("/api/conversations/<int:cid>/messages")
    def send(cid):
        data = body()
        require(data.get("confirm_send") is True, "confirm_required", "请确认目标和发送内容")
        return jsonify(service.manual_send(cid, data.get("messages")))

    @app.put("/api/conversations/<int:cid>/mode")
    def mode(cid):
        requested = body().get("mode")
        require(requested in ("manual", "auto"), "invalid_mode", "请选择人工回复或 AI 回复")
        return jsonify(service.set_mode(cid, requested))

    @app.post("/api/conversations/<int:cid>/attachments")
    def attachment(cid):
        file = request.files.get("file")
        require(file and file.filename, "file_missing", "请选择图片、文件或视频")
        return jsonify(service.save_attachment(cid, file.read(25_000_001), file.filename, file.mimetype, request.form.get("type"))), 201

    @app.post("/api/conversations/<int:cid>/ai-preview")
    def preview(cid):
        return jsonify(service.preview(cid))

    @app.post("/api/conversations/<int:cid>/ticket")
    def ticket(cid):
        data = body()
        require(type(data.get("new_matter", False)) is bool, "invalid_body", "新事项标记须为布尔值")
        return jsonify(service.create_ticket(cid, data.get("reason"), data.get("new_matter", False)))

    @app.route("/api/knowledge", methods=["GET", "POST", "DELETE"])
    def knowledge():
        if request.method == "GET":
            return jsonify(service.refresh_knowledge())
        if request.method == "POST":
            data = body()
            require(set(data) <= {"name"}, "invalid_body", "知识库创建只接受 name")
            return jsonify(service.create_knowledge(data.get("name")))
        return jsonify(service.delete_knowledge())

    @app.post("/api/knowledge/files")
    def knowledge_file_upload():
        file = request.files.get("file")
        require(file and file.filename, "file_missing", "请选择知识库文档")
        return jsonify(service.upload_knowledge_file(file.read(20_000_001), file.filename, file.mimetype)), 201

    @app.delete("/api/knowledge/files/<int:file_id>")
    def knowledge_file_delete(file_id):
        return jsonify(service.delete_knowledge_file(file_id))

    @app.get("/api/jobs/<job_id>")
    def job(job_id):
        return jsonify(service.public_job(job_id))

    @app.post("/api/jobs/<job_id>/resolve")
    def resolve(job_id):
        return jsonify(service.resolve_job(job_id, body()))

    @app.route("/api/assets", methods=["GET", "POST"])
    def assets():
        if request.method == "GET":
            s, _ = service.settings.get()
            return jsonify(assets=[service.public_asset(service.asset(a["id"])) for a in db.all("SELECT id FROM assets WHERE tenant=? AND conversation IS NULL ORDER BY created DESC", (tenant_id(s),))])
        if request.is_json:
            return jsonify(service.remote_asset(body())), 201
        file = request.files.get("file")
        require(file and file.filename, "file_missing", "请选择素材文件")
        try:
            metadata = json.loads(request.form.get("metadata", "{}"))
        except ValueError:
            raise Problem("asset_metadata", "素材信息 JSON 不正确") from None
        return jsonify(service.save_asset(metadata, file.read(25_000_001), file.filename, file.mimetype)), 201

    @app.patch("/api/assets/<aid>")
    def edit_asset(aid):
        return jsonify(service.edit_asset(aid, body()))

    @app.post("/api/assets/<aid>/upload")
    def upload_asset(aid):
        return jsonify(service.upload_asset(aid))

    def asset_content(a):
        require(a["path"] and Path(a["path"]).is_file(), "file_missing", "本地文件不存在", 404)
        return send_file(a["path"], mimetype=a["mime"], download_name=a["filename"], as_attachment=a["kind"] == "file", conditional=True)

    @app.get("/api/assets/<aid>/content")
    def content(aid):
        return asset_content(service.asset(aid))

    @app.get("/media/<token>")
    def media(token):
        return asset_content(service.media_token(token))

    @app.get("/api/conversations/<int:cid>/media/<int:mid>/<int:part>")
    def inbound_media(cid, mid, part):
        service.conv(cid)
        row = db.one("SELECT parts FROM messages WHERE id=? AND conversation=?", (mid, cid))
        require(row, "media_not_found", "媒体不存在", 404)
        parts = db.unseal(row["parts"])
        require(0 <= part < len(parts) and parts[part]["type"] in ("image", "video", "file"), "media_not_found", "媒体片段不存在", 404)
        p = parts[part]
        s, _ = service.settings.get()
        kind = security.media_kind(p)
        require(p.get("security_status") in (None, "", "SAFE_FILE"), "media_scan", "媒体尚未通过平台安全扫描", 409)
        media_url = p.get("url", "")
        media_host = security.url_parts(media_url).hostname
        media_hosts = list(s["media_host_allowlist"])
        if security.is_freshchat_media_host(media_host):
            media_hosts.append(media_host.lower())
        raw, headers = security.request(media_url, hosts=media_hosts, redirects=3, limit=s["media_size_limits"][kind])
        filename = Path(str(p.get("name", "")).replace("\\", "/")).name
        filename = "".join(c for c in filename if c.isprintable())[:200]
        if kind == "file":
            # Unrecognized attachments are downloads only, never active inline content.
            return send_file(io.BytesIO(raw), mimetype="application/octet-stream", download_name=filename or "attachment",
                             as_attachment=True, conditional=True)
        mime = headers.get("content-type", "").split(";")[0].strip().lower()
        if mime in ("", "application/octet-stream"):
            declared = p.get("mime", "").split(";")[0].strip().lower()
            mime = declared if declared not in ("", "application/octet-stream") else security.MIMES.get(Path(filename).suffix.lower(), ("", ""))[1]
        require(mime in s["allowed_mime_types"], "media_mime", "媒体 MIME 不允许在管理页打开", 409)
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "video/mp4": ".mp4", "application/pdf": ".pdf"}[mime]
        detected_kind, _ = security.detect_file("media" + extension, raw, mime)
        require(detected_kind == kind, "media_mime", "媒体内容与类型不一致", 409)
        return send_file(io.BytesIO(raw), mimetype=mime, download_name=filename if filename and filename != "媒体" else "media" + extension,
                         conditional=True)

    @app.post("/api/webhooks/freshchat")
    def webhook():
        raw = request.get_data(cache=False)
        require(len(raw) <= 2_000_000, "event_too_large", "Webhook 请求体过大", 413)
        with db.lock:
            s, _ = service.settings.get()
            require(s["freshchat_public_key"], "webhook_not_configured", "Webhook 验签公钥尚未配置", 503)
            security.verify_signature(s["freshchat_public_key"], raw, request.headers.get("X-Freshchat-Signature", ""))
            try:
                payload = json.loads(raw)
            except ValueError:
                raise Problem("invalid_json", "Webhook JSON 格式不正确") from None
            return jsonify(service.ingest(payload, request.headers.get("X-Freshchat-Payload-Version", "unknown"), request.headers.get("X-Retry-Count", "0")))

    @app.post("/api/internal/jobs/drain")
    def scheduler():
        s, _ = service.settings.get()
        require(s["scheduler_token"], "scheduler_not_configured", "调度凭证尚未配置", 503)
        require(hmac.compare_digest(request.headers.get("X-Scheduler-Token", ""), s["scheduler_token"]), "scheduler_unauthorized", "调度鉴权失败", 401)
        return jsonify(service.scheduler(body()))

    if start_worker:
        service.start()
    return app
