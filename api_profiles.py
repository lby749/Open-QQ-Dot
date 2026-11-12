"""Named external API connections, with per-profile credentials and no vendor lock-in.

Only nonsecret configuration is stored in friend.json. Credential identifiers refer
exclusively to Windows DPAPI-protected files managed by api_backend.py.
"""
from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit

MAX_PROFILES = 32
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
PROTOCOLS = ("openai-completions", "openai-responses", "anthropic-messages")
# Official service URLs are conveniences; generic presets never assume a specific host.
PRESETS = {
    "generic_openai": ("通用 OpenAI 兼容", "openai-completions", "", ""),
    "generic_responses": ("通用 Responses 兼容", "openai-responses", "", ""),
    "generic_anthropic": ("通用 Anthropic 兼容", "anthropic-messages", "", ""),
    "openai": ("OpenAI", "openai-responses", "https://api.openai.com/v1", ""),
    "anthropic": ("Anthropic", "anthropic-messages", "https://api.anthropic.com", ""),
    "deepseek": ("DeepSeek", "openai-completions", "https://api.deepseek.com", "deepseek-chat"),
    "ollama": ("Ollama / vLLM 本地兼容", "openai-completions", "http://127.0.0.1:11434/v1", ""),
}


def validate_url(url: str) -> str:
    """Never send API keys via unencrypted remote HTTP, URL user-info or query parts."""
    url = str(url or "").strip().rstrip("/")
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        _ = p.port  # Validate malformed ports.
    except ValueError as exc:
        raise ValueError("API 地址无效") from exc
    if p.scheme not in {"http", "https"} or not host or p.username or p.password or p.query or p.fragment:
        raise ValueError("API 地址必须是无用户名、密码和查询参数的 HTTP(S) Base URL")
    if p.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("外部 API 必须使用 HTTPS；HTTP 只允许本机地址")
    if "\\" in url or any(segment in {"..", "."} for segment in p.path.split("/")):
        raise ValueError("API 地址不允许路径穿越")
    return url


def clean_connection(data: dict, *, identifier: str | None = None) -> dict:
    if not isinstance(data, dict):
        raise ValueError("API 连接必须是对象")
    identifier = identifier or str(data.get("api_key_id") or "")
    if not ID_PATTERN.fullmatch(identifier):
        raise ValueError("API 连接 ID 无效")
    protocol = str(data.get("api_protocol") or "openai-completions")
    if protocol not in PROTOCOLS:
        raise ValueError("不支持的 API 协议")
    base = str(data.get("api_base_url") or "").strip()
    if base:
        base = validate_url(base)
    label = str(data.get("label") or identifier).strip()[:64]
    model = str(data.get("model") or "").strip()[:160]
    effort = str(data.get("api_effort") or "low")
    if effort not in {"off", "none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("无效的思考档位")
    # Allowlisted fields prevent API keys from ever entering the plain-text profile.
    return {"label": label or identifier, "api_key_id": identifier,
            "api_protocol": protocol, "api_base_url": base, "model": model, "api_effort": effort}


def load_connections(profile: dict) -> tuple[dict[str, dict], str]:
    api = profile.get("model_api") or {}
    active = str(api.get("api_key_id") or "chat")
    raw = profile.get("api_profiles") or {}
    connections = {}
    if isinstance(raw, dict):
        for identifier, value in list(raw.items())[:MAX_PROFILES]:
            try:
                connections[identifier] = clean_connection(value, identifier=identifier)
            except (ValueError, TypeError):
                continue
    if active not in connections:
        if not ID_PATTERN.fullmatch(active):
            active = "chat"
        connections[active] = clean_connection({**api, "label": "默认模型 API"}, identifier=active)
    return connections, active


def new_connection(label: str, preset: str = "generic_openai") -> dict:
    if preset not in PRESETS:
        raise ValueError("未知 API 预设")
    name, protocol, base, model = PRESETS[preset]
    return clean_connection({"label": label.strip() or name, "api_protocol": protocol,
                             "api_base_url": base, "model": model, "api_effort": "low"},
                            identifier="api_" + uuid.uuid4().hex)


def activate(profile: dict, identifier: str, connections: dict[str, dict]) -> dict:
    """Return a copy with only the selected connection's nonsecret settings active."""
    if identifier not in connections:
        raise ValueError("找不到指定的 API 连接")
    selected = clean_connection(connections[identifier], identifier=identifier)
    result = dict(profile)
    api = dict(result.get("model_api") or {})
    api.update({field: selected[field] for field in (
        "api_key_id", "api_protocol", "api_base_url", "model", "api_effort")})
    api["provider"] = "custom"
    result["model_api"] = api
    result["api_profiles"] = {key: clean_connection(value, identifier=key)
                              for key, value in connections.items()}
    return result

class NoCredentialRedirects:
    """A redirect must not carry a model API credential to another destination."""

    @staticmethod
    def opener():
        import urllib.request

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ValueError("模型列表接口发生重定向，已阻止发送 API Key")

        return urllib.request.build_opener(NoRedirect())


def discover_models(base_url: str, protocol: str, key: str, timeout: int = 10) -> list[str]:
    """Optional provider capability, not required for manually entering model IDs.

    No silent fake fallback and no HTTP redirects with Authorization headers.
    """
    import json
    import urllib.request
    base = validate_url(base_url)
    if protocol not in PROTOCOLS:
        raise ValueError("无效 API 协议")
    if not key.strip():
        raise ValueError("请先填写或保存此连接的 API Key")
    # User controls Base URL. /models is the common convention, not universal.
    url = base + "/models" if base.endswith("/v1") else base + "/v1/models"
    headers = {"User-Agent": "Open-QQ-Dot", "Accept": "application/json"}
    if protocol == "anthropic-messages":
        headers.update({"x-api-key": key.strip(), "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = "Bearer " + key.strip()
    req = urllib.request.Request(url, headers=headers)
    with NoCredentialRedirects.opener().open(req, timeout=timeout) as response:
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("模型列表返回内容过大")
        data = json.loads(raw.decode("utf-8"))
    rows = data.get("data") or data.get("models") or []
    if not isinstance(rows, list):
        raise ValueError("模型列表格式无法识别")
    ids = list(dict.fromkeys(str(row.get("id") or row.get("name") or "").strip()
                             for row in rows if isinstance(row, dict)))
    ids = [x for x in ids if x and len(x) <= 160]
    if not ids:
        raise ValueError("此服务不支持标准模型列表，请手动填写模型 ID")
    return ids[:300]
