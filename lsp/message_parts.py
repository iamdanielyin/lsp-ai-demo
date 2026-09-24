"""Freshchat display parts; customer actions are never replayed by the admin UI."""
import re
from urllib.parse import urlsplit

from .security import require


def link_url(value):
    if not isinstance(value, str) or len(value) > 8192 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        return ""
    try:
        url = urlsplit(value)
        if url.scheme in ("https", "http") and url.hostname and not url.username and not url.password:
            _ = url.port
            return value
    except ValueError:
        pass
    return ""


def legacy_card(text):
    # Some connectors store their template as text instead of native message parts.
    if not text.startswith("text:"):
        return None
    chunks = re.split(r"\r?\n[ \t]*type:[ \t]*button[ \t]*\r?\n", text)
    if not 2 <= len(chunks) <= 21 or not chunks[0][5:].strip():
        return None
    buttons = []
    for block in chunks[1:]:
        match = re.fullmatch(r"action\.type:[ \t]*link[ \t]*\r?\n"
                             r"action\.text:[ \t]*([^\r\n]+)\r?\n"
                             r"action\.url:[ \t]*([^\r\n]+)\s*", block)
        if not match:
            return None
        buttons.append({"type": "link", "text": match[1].strip(), "href": link_url(match[2].strip())})
    return {"type": "card", "parts": [{"type": "text", "text": chunks[0][5:].strip()}, *buttons]}


def display_parts(parts, allow_legacy=False):
    """Also render previously cached connector text without a history reimport."""
    return [(legacy_card(p["text"]) or p) if allow_legacy and p["type"] == "text" else p for p in parts]


def normalize_parts(raw, depth=0):
    require(isinstance(raw, list), "message_format", "消息片段必须为数组")
    if depth > 6:
        return [{"type": "unsupported", "text": "嵌套消息过深，请在原工作台查看"}]
    parts = []
    for part in raw[:200]:
        require(isinstance(part, dict), "message_format", "消息片段格式不正确")
        before = len(parts)
        for kind in ("text", "help_text", "image", "video", "file", "url_button", "quick_reply_button", "callback", "collection", "template_content", "reference", "text_input", "attachment_input"):
            val = part.get(kind)
            if not isinstance(val, dict):
                continue
            if kind in ("text", "help_text"):
                parts.append({"type": "text", "text": str(val.get("content", ""))[:100000]})
            elif kind in ("image", "video", "file"):
                parts.append({"type": kind, "url": str(val.get("url", "")), "name": str(val.get("name", val.get("file_name", "媒体"))),
                              "mime": str(val.get("content_type", val.get("contentType", val.get("file_content_type", "")))),
                              "size": val.get("file_size_in_bytes", val.get("file_size")), "security_status": val.get("file_security_status")})
            elif kind == "url_button":
                parts.append({"type": "link", "text": str(val.get("label", "打开链接"))[:1000], "href": link_url(val.get("url"))})
            elif kind in ("quick_reply_button", "callback"):
                parts.append({"type": "option", "text": str(val.get("label", "选项"))[:1000]})
            elif kind in ("reference", "text_input", "attachment_input"):
                if kind == "reference":
                    text = str(val.get("label") or "FAQ 文章")[:1000] + "（请在原工作台查看）"
                else:
                    text = "客户侧附件上传" if kind == "attachment_input" else "客户侧输入"
                    hint = val.get("placeholderText") or val.get("inputType") or ""
                    text += "：" + str(hint)[:1000] if hint else ""
                parts.append({"type": "notice", "text": text})
            elif kind == "collection":
                parts.append({"type": "group", "parts": normalize_parts(val.get("sub_parts", []), depth + 1)})
            else:
                sections = val.get("sections", [])
                require(isinstance(sections, list), "message_format", "卡片 sections 必须为数组")
                children = []
                for section in sections[:200]:
                    require(isinstance(section, dict), "message_format", "卡片 section 格式不正确")
                    children.append({"type": "section", "name": str(section.get("name", ""))[:100],
                                     "parts": normalize_parts(section.get("parts", []), depth + 1)})
                kind = {"carousel": "carousel", "carousel_card_default": "card", "quick_reply_dropdown": "options"}.get(str(val.get("type", "")), "group")
                parts.append({"type": kind, "parts": children})
        if len(parts) == before:
            parts.append({"type": "unsupported", "text": "此消息片段暂不支持预览，请在原工作台查看"})
    if len(raw) > 200:
        parts.append({"type": "unsupported", "text": "消息片段过多，其余内容请在原工作台查看"})
    return parts


def walk_parts(parts, prefix=""):
    for index, part in enumerate(parts):
        path = f"{prefix}.{index}" if prefix else str(index)
        if "parts" in part:
            yield from walk_parts(part["parts"], path)
        else:
            yield path, part


def part_preview(parts):
    return " · ".join(p.get("text") or {"image": "[图片]", "video": "[视频]", "file": "[附件]"}.get(p["type"], "[消息]")
                      for _, p in walk_parts(parts))[:120]
