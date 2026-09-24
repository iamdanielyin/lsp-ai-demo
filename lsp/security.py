"""Trust boundaries: validated API hosts, pinned public DNS, bounded downloads, RSA."""
import base64
import http.client
import ipaddress
import json
import re
import socket
import ssl
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class Problem(Exception):
    def __init__(self, code, message, status=400, retry_after=0):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


def require(ok, code, message, status=400):
    if not ok:
        raise Problem(code, message, status)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,200}", value),
            "invalid_id", "平台 ID 格式不正确")
    return value


def url_parts(url, hosts=None):
    try:
        p = urlsplit(url)
        port = p.port
    except (ValueError, TypeError):
        raise Problem("invalid_url", "URL 格式不正确") from None
    require(p.scheme == "https" and p.hostname and not p.username and not p.password
            and not p.fragment and port in (None, 443), "invalid_url", "仅允许不含凭证的 HTTPS 地址（443）")
    host = p.hostname.lower()
    require(not re.search(r"[\s\\\x00-\x1f]", url), "invalid_url", "URL 含非法字符")
    if hosts is not None:
        require(host in hosts, "host_denied", "目标主机不在允许列表中")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        require("." in host and host != "localhost" and not host.endswith((".local", ".internal", ".localhost")),
                "private_host", "禁止本机或内网主机")
    else:
        require(ip.is_global, "private_host", "禁止私网、回环或元数据地址")
    return p


def api_url(url, kind):
    p = url_parts(url)
    host = p.hostname.lower()
    if kind == "openai":
        require(not p.query, "invalid_openai_url", "OpenAI API 地址不能包含查询参数，请将 Key 填入密钥栏")
        path = p.path.rstrip("/")
        require(not path.endswith("/chat/completions"), "invalid_openai_url", "本 Demo 使用 Responses API，请填写基础地址或以 /responses 结尾的地址")
        if not path:
            path = "/v1"
        return p._replace(path=path).geturl()
    if kind == "freshchat":
        allowed = host.endswith((".freshchat.com", ".freshchat.eu", ".freshchat.in", ".freshchat.com.au"))
    elif kind == "freshdesk":
        allowed = host.endswith((".freshdesk.com", ".freshdesk.eu", ".freshdesk.in", ".freshdesk.com.au"))
    else:
        allowed = False
    require(allowed and not p.query and p.path in ("", "/"),
            "api_host_denied", "请填写受支持的 Freshchat / Freshdesk 官方区域 API 主机")
    return url.rstrip("/")


def public_addresses(host):
    try:
        addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError:
        raise Problem("dns_failed", "目标主机解析失败", 502) from None
    require(addresses and all(ipaddress.ip_address(a).is_global for a in addresses),
            "private_host", "DNS 指向非公网地址，已拒绝请求")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, *, timeout=30, context=None, proxy=None):
        super().__init__(host, timeout=timeout, context=context)
        self.proxy = None
        if proxy:
            try:
                p = urlsplit(proxy)
                port = p.port if p.port is not None else 80
            except ValueError:
                raise Problem("invalid_proxy", "OpenAI 代理地址或端口格式不正确") from None
            require(p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1")
                    and 1 <= port <= 65535 and not p.username and not p.password
                    and p.path in ("", "/") and not p.query and not p.fragment
                    and not re.search(r"[\s\\\x00-\x1f]", proxy),
                    "invalid_proxy", "OpenAI 代理须为本机 HTTP 代理地址，例如 http://127.0.0.1:7897")
            self.proxy = ("127.0.0.1" if p.hostname == "localhost" else p.hostname, port)

    def connect(self):
        # Only the fixed official OpenAI host delegates DNS to the trusted loopback proxy.
        # Local DNS can be polluted or use fake IPs; custom hosts still require a pinned public IP.
        target = self.host if self.proxy and self.host == "api.openai.com" else public_addresses(self.host)[0]
        if self.proxy:
            tunnel = http.client.HTTPConnection(*self.proxy, timeout=self.timeout)
            try:
                tunnel.set_tunnel(target, 443)
                tunnel.connect()
                self.sock, tunnel.sock = tunnel.sock, None
            except (OSError, http.client.HTTPException):
                raise Problem("proxy_unavailable", "本地 OpenAI 代理连接失败，请检查代理服务、端口和 CONNECT 转发", 502) from None
            finally:
                tunnel.close()
        else:
            self.sock = socket.create_connection((target, 443), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def request(url, method="GET", headers=None, body=None, timeout=30, limit=8_000_000, hosts=None, redirects=0, proxy=None):
    p = url_parts(url, hosts)
    conn = PinnedHTTPS(p.hostname, timeout=timeout, context=ssl.create_default_context(), proxy=proxy)
    try:
        conn.request(method, p.path + ("?" + p.query if p.query else ""), body=body, headers=headers or {})
        response = conn.getresponse()
        status, rh = response.status, {k.lower(): v for k, v in response.getheaders()}
        if 300 <= status < 400:
            require(method == "GET" and redirects > 0, "redirect_denied", "请求重定向已拒绝", 502)
            return request(urljoin(url, rh.get("location", "")), hosts=hosts, timeout=timeout,
                           limit=limit, redirects=redirects - 1, proxy=proxy)
        raw = response.read(limit + 1)
        require(len(raw) <= limit, "response_too_large", "响应或文件超过限制", 502)
        if status == 429:
            retry = rh.get("retry-after", "2")
            raise Problem("rate_limited", "供应商限流（HTTP 429）", 429,
                          min(3600, max(1, int(retry))) if retry.isdigit() else 60)
        if status >= 500:
            raise Problem("upstream_unknown" if method != "GET" else "upstream_unavailable",
                          f"供应商 HTTP {status}；外发请求需核实是否创建", 502)
        require(200 <= status < 300, "upstream_rejected", f"供应商拒绝请求（HTTP {status}）；请核对权限、字段及渠道", 502)
        return raw, rh
    except (OSError, http.client.HTTPException):
        raise Problem("network_unknown" if method != "GET" else "network_failed",
                      "网络失败或超时；已提交的外发结果不明，请先核实", 502) from None
    finally:
        conn.close()


def json_request(url, method="GET", headers=None, data=None, timeout=30, proxy=None):
    raw, rh = request(url, method, {"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
                      json.dumps(data, ensure_ascii=False).encode() if data is not None else None, timeout, proxy=proxy)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Problem("invalid_response", "供应商未返回有效 JSON；外发结果需核实", 502) from None
    require(isinstance(result, (dict, list)), "invalid_response", "供应商返回结构不正确", 502)
    return result, rh


def public_key(value):
    try:
        value = value.strip()
        key = (serialization.load_pem_public_key(value.encode()) if value.startswith("-----BEGIN ")
               else serialization.load_der_public_key(base64.b64decode("".join(value.split()), validate=True)))
        require(isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048, "invalid_public_key", "需要 RSA 公钥（至少2048位）")
        return key
    except (ValueError, TypeError):
        raise Problem("invalid_public_key", "验签公钥格式不正确（PEM 或 Base64 DER）") from None


def verify_signature(key, raw, signature):
    try:
        public_key(key).verify(base64.b64decode(signature, validate=True), raw, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise Problem("invalid_signature", "Webhook 签名无效", 401) from None


MIMES = {".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"),
         ".jpeg": ("image", "image/jpeg"), ".mp4": ("video", "video/mp4"), ".pdf": ("file", "application/pdf")}


def is_freshchat_media_host(host):
    """Recognize Freshchat's generated public S3 media hosts only."""
    return bool(isinstance(host, str) and re.fullmatch(
        r"fc-[a-z0-9-]+-(?:pics|files|audio)-bkt-[a-z0-9-]+\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host.lower()))


def media_kind(part):
    """Freshchat also represents uploaded pictures/videos as file parts."""
    if part["type"] == "file":
        mime = part.get("mime", "").split(";")[0].strip().lower()
        for kind, supported_mime in MIMES.values():
            if mime == supported_mime:
                return kind
        if mime in ("", "application/octet-stream"):
            return MIMES.get(Path(part.get("name", "")).suffix.lower(), ("file", ""))[0]
    return part["type"]


def detect_file(filename, data, claimed_mime=""):
    ext = Path(filename).suffix.lower()
    require(ext in MIMES, "file_type", "仅支持 PNG、JPEG、MP4、PDF")
    kind, mime = MIMES[ext]
    valid = {"image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
             "image/jpeg": data.startswith(b"\xff\xd8\xff"),
             "application/pdf": data.startswith(b"%PDF-"),
             "video/mp4": len(data) >= 12 and data[4:8] == b"ftyp"}[mime]
    require(valid and (not claimed_mime or claimed_mime.split(";")[0] in (mime, "application/octet-stream")),
            "mime_mismatch", "扩展名、文件签名与 MIME 不一致")
    require(len(data) > 12, "empty_file", "文件为空或不完整")
    return kind, mime


def multipart(field, filename, mime, data):
    boundary = "lsp_" + uuid.uuid4().hex
    # Only generated filenames enter HTTP headers.
    name = uuid.uuid4().hex + "." + filename.rsplit(".", 1)[-1]
    raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
           f'Content-Type: {mime}\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return raw, "multipart/form-data; boundary=" + boundary
