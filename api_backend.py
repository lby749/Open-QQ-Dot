"""Custom model APIs and per-friend Windows-protected API keys.

2026-10-12 user: configured like DeepSeek Harness's custom providers (dsh-llm-pi-ai): a base URL, a protocol
(openai-completions / openai-responses / anthropic-messages), a model id and a key, so any of these APIs works.
The program runs the tool calls itself (topic library, memory, phone), as Codex does through MCP. The default
reasoning effort is low. A friend saved before this (provider "deepseek") keeps working on DeepSeek's endpoint."""
from __future__ import annotations

import base64
import hashlib
import os
import ctypes
import ctypes.wintypes
import json
from workspace_tools import instructions as computer_instructions
import re
import time
import urllib.error
import urllib.request


def _open(request, timeout):
    """Route external API traffic through the operator's proxy when available."""
    try:
        import network_guard
        proxy = network_guard.proxy_url()
        if proxy and network_guard.should_proxy_url(request.full_url):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            return opener.open(request, timeout=timeout)
    except Exception:
        pass
    return urllib.request.urlopen(request, timeout=timeout)
from pathlib import Path

import round_clock
from prompt_cache_layout import split_prompt, official_host
from api_profiles import validate_url


DEFAULT_BASE_URL = "https://api.deepseek.com"
MODELS = ("DeepSeek-V41-Flash", "DeepSeek-V4-Pro")  # DeepSeek's recognized models
PROTOCOLS = ("openai-completions", "openai-responses", "anthropic-messages")
EFFORTS = ("off", "none", "low", "medium", "high", "xhigh", "max")
THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 16384, "xhigh": 32768}  # anthropic-messages
REASONING = ("reasoning_effort", "reasoning", "thinking")  # dropped once when a model rejects them
STEPS = 6
_retry_until: dict[str, float] = {}
PROVIDER_DEFAULTS = {
    'dsh': {
        'name': 'DeepSeek Harness (DeepSeek 账号)',
        'base_url': 'https://api.deepseek.com',
        'model': 'DeepSeek-V41-Flash',
        'models': [
            'DeepSeek-V41-Flash',
            'DeepSeek-V4-Pro',
            'deepseek-chat',
            'deepseek-reasoner',
        ],
        'protocol': 'openai-completions',
        'efforts': ['off', 'low', 'medium', 'high', 'max'],
        'key_id': 'dsh',
    },
}


def fetch_provider_models(provider_key: str, base_url: str | None = None, api_key: str | None = None) -> list[str]:
    cfg = PROVIDER_DEFAULTS.get(provider_key, {})
    url = (base_url or cfg.get('base_url', '')).strip().rstrip('/')
    fallback_models = cfg.get('models', list(MODELS))
    if not url or not api_key:
        return fallback_models
    try:
        endpoint = url + '/models' if url.endswith('/v1') else url + '/v1/models'
        req = urllib.request.Request(endpoint, headers={
            'Authorization': f'Bearer {api_key.strip()}',
            'User-Agent': 'Open-QQ-Dot',
        })
        with _open(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            models = [item['id'] for item in data.get('data', []) if 'id' in item and isinstance(item['id'], str)]
            if models:
                return models
    except Exception:
        pass
    return fallback_models


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('API Key 的安全加密存储使用 Windows DPAPI；当前平台请改用环境变量或系统密钥环')
    source = ctypes.create_string_buffer(data)
    input_blob = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _Blob()
    crypt32 = ctypes.windll.crypt32
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.restype = ctypes.wintypes.BOOL
    operation.argtypes = ([ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
                           ctypes.POINTER(_Blob)] if decrypt else
                          [ctypes.POINTER(_Blob), ctypes.wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
                           ctypes.POINTER(_Blob)])
    if not operation(ctypes.byref(input_blob), None, None, None, None, 0,
                     ctypes.byref(output_blob)):
        raise OSError("无法使用当前 Windows 用户保护或读取 API Key")
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(output_blob.data, ctypes.c_void_p))


def key_path(directory: Path, purpose=None) -> Path:
    directory = Path(directory)
    if purpose is None:
        try:
            purpose = (json.loads((directory / 'friend.json').read_text('utf-8')).get('model_api') or {}).get('api_key_id') or 'chat'
        except (OSError, ValueError):
            purpose = 'chat'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', purpose):
        raise ValueError('API credential identifier is invalid')
    path = directory / ('api-key.bin' if purpose == 'chat' else f'api-{purpose}-key.bin')
    legacy = directory / 'deepseek-key.bin'
    return legacy if purpose == 'chat' and not path.exists() and legacy.exists() else path


def save_key(directory: Path, key: str, purpose=None):
    value = key.strip()
    if not value:
        raise ValueError('API Key 不能为空')
    path = key_path(directory, purpose)
    if path.name == 'deepseek-key.bin':
        path = Path(directory) / 'api-key.bin'
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(_protect(value.encode('utf-8')))
    temporary.replace(path)


def load_key(directory: Path, purpose=None) -> str:
    path = key_path(directory, purpose)
    if not path.exists():
        raise RuntimeError('当前 API 尚未设置密钥')
    return _protect(path.read_bytes(), decrypt=True).decode('utf-8')


def settings(api: dict) -> tuple[str, str, str, str]:
    """(protocol, base URL, model, effort) of a friend's custom API, checked."""
    protocol = api.get("api_protocol") or PROTOCOLS[0]
    base = (api.get("api_base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    model = str(api.get("model") or MODELS[0]).strip()
    effort = api.get("api_effort") or "low"
    if protocol not in PROTOCOLS:
        raise ValueError("API 协议无效")
    base = validate_url(base)
    if not model:
        raise ValueError("请填写模型名称")
    if effort not in EFFORTS:
        raise ValueError("思考档位无效")
    return protocol, base, model, effort


def _tools(directory=None):
    import phone_tools
    from topic_library import model_tools
    from task_runtime import work_task
    return model_tools(directory) + ([] if work_task() else phone_tools.TOOLS)


def _run_tool(directory: Path, name: str, arguments) -> str:
    import phone_tools
    from topic_library import call_tool
    from task_runtime import current_execution
    execution=current_execution.get()
    if execution:
        execution.check()
    try:
        if isinstance(arguments, str):
            arguments = json.loads(arguments or "{}")
        if name in phone_tools.NAMES:
            output = phone_tools.call_tool(directory, name, arguments)
        else:
            output = call_tool(directory, name, arguments)
    except (InterruptedError, TimeoutError):
        raise
    except Exception as exc:  # the model sees the failure and answers without it
        output = {"error": str(exc) or "查询工具或参数无效"}
    return json.dumps(output, ensure_ascii=False)


def _post(url: str, headers: dict, payload: dict, retry_key: str) -> dict:
    from task_runtime import current_execution
    execution = current_execution.get()
    for _ in range(2):
        if execution:
            execution.before_call(json.dumps(payload, ensure_ascii=False))
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with _open(request, timeout=round_clock.clamp(180)) as response:
                data = json.load(response)
                if execution:
                    execution.after_call(json.dumps(data, ensure_ascii=False), usage=data.get('usage'))
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and any(key in payload for key in REASONING):
                for key in REASONING:  # a model without a reasoning switch; ask once more without it
                    payload.pop(key, None)
                continue
            _retry_until[retry_key] = time.monotonic() + 30
            raise RuntimeError(f"模型 API 返回 HTTP {exc.code}；本条未发送") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            _retry_until[retry_key] = time.monotonic() + 30
            raise RuntimeError("模型 API 暂时不可用；本条未发送") from exc
    raise RuntimeError("model API refuse; itemsSend")


# Each adapter yields one round of tool calls at a time ({"id", "function": {"name", "arguments"}}; the caller
# fills "output") and returns the final text.

def _normalize_endpoint_model(base: str, model: str) -> str:
    if "api.deepseek.com" in base:
        m = (model or "").lower()
        if "pro" in m or "reasoner" in m:
            return "deepseek-reasoner"
        if "flash" in m or "chat" in m:
            return "deepseek-chat"
    return model


def _completions(base, key, model, effort, prompt, image, tools, post, *, cache_layout=None, cache_key=None):
    target_model = _normalize_endpoint_model(base, model)
    content = prompt if not image else [{"type": "text", "text": prompt},
                                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}}]
    messages = [{"role": "user", "content": content}]
    if cache_layout and cache_layout.enabled:
        # Preserve the original USER trust boundary. A forged section header
        # in untrusted text must never be promoted into a system role.
        stable = '\n\n'.join(filter(None, (cache_layout.system, cache_layout.stable_user)))
        messages = [{"role": "user", "content": stable}]
        if cache_layout.dynamic_user or image:
            messages.append({"role": "user", "content": (
                cache_layout.dynamic_user if not image else [
                    {"type": "text", "text": "\n\n" + cache_layout.dynamic_user},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}},
                ])})
    payload = {"model": target_model, "messages": messages, "stream": False}
    if cache_key and official_host(base) == 'api.openai.com':
        payload['prompt_cache_key'] = cache_key
    if effort and effort not in ("none", "off"):
        payload["reasoning_effort"] = effort
    if tools:
        payload["tools"] = [{"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                                              "parameters": tool["inputSchema"]}} for tool in tools]
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    for step in range(STEPS):
        if tools and step == STEPS - 1:
            payload["tool_choice"] = "none"
        choice = post(base + "/chat/completions", headers, payload)["choices"][0]
        message = choice["message"]
        calls = message.get("tool_calls") or []
        if not (tools and calls and choice["finish_reason"] == "tool_calls"):
            if choice["finish_reason"] != "stop":
                raise RuntimeError("模型回复未完整生成；本条未发送")
            return message["content"]
        turn = {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
        if message.get("reasoning_content"):  # thinking models need it back during tool calls
            turn["reasoning_content"] = message["reasoning_content"]
        payload["messages"].append(turn)
        yield calls
        payload["messages"] += [{"role": "tool", "tool_call_id": call["id"], "content": call["output"]}
                                for call in calls]
    raise RuntimeError("模型查询工具调用轮数过多；本条未发送")


def _responses(base, key, model, effort, prompt, image, tools, post, *, cache_layout=None, cache_key=None):
    target_model = _normalize_endpoint_model(base, model)
    content = [{"type": "input_text", "text": prompt}]
    if image:
        content.append({"type": "input_image", "image_url": "data:image/png;base64," + image})
    inputs = [{"role": "user", "content": content}]
    if cache_layout and cache_layout.enabled:
        stable = '\n\n'.join(filter(None, (cache_layout.system, cache_layout.stable_user)))
        stable_block = {"type": "input_text", "text": stable}
        # Official GPT-5.6+ Responses models support reusable boundaries.
        # Never send these new fields to generic OpenAI-compatible proxies.
        if official_host(base) == 'api.openai.com' and re.match(r'^(?:gpt-6|gpt-5\.(?:[6-9]|[1-9]\d))', target_model.lower()):
            stable_block['prompt_cache_breakpoint'] = {"mode": "explicit"}
        inputs = [{"role": "user", "content": [stable_block]}]
        if cache_layout.dynamic_user or image:
            dynamic_content = [{"type": "input_text", "text": "\n\n" + cache_layout.dynamic_user}]
            if image:
                dynamic_content.append({"type": "input_image", "image_url": "data:image/png;base64," + image})
            inputs.append({"role": "user", "content": dynamic_content})
    payload = {"model": target_model, "input": inputs, "store": False}
    if cache_key and official_host(base) == 'api.openai.com':
        payload['prompt_cache_key'] = cache_key
    if effort and effort not in ("none", "off"):
        payload["reasoning"] = {"effort": effort}
    if tools:
        payload["tools"] = [{"type": "function", "name": tool["name"], "description": tool["description"],
                             "parameters": tool["inputSchema"]} for tool in tools]
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    for step in range(STEPS):
        if tools and step == STEPS - 1:
            payload["tool_choice"] = "none"
        result = post(base + "/responses", headers, payload)
        output = result["output"]
        calls = [item for item in output if item.get("type") == "function_call"]
        if not (tools and calls):
            if result.get("status", "completed") != "completed":
                raise RuntimeError("模型回复未完整生成；本条未发送")
            return "".join(part["text"] for item in output if item.get("type") == "message"
                           for part in item["content"] if part.get("type") == "output_text")
        payload["input"] += [{name: call[name] for name in ("type", "call_id", "name", "arguments")} for call in calls]
        named = [{"id": call["call_id"], "function": {"name": call["name"], "arguments": call["arguments"]}}
                 for call in calls]
        yield named
        payload["input"] += [{"type": "function_call_output", "call_id": call["id"], "output": call["output"]}
                             for call in named]
    raise RuntimeError("模型查询工具调用轮数过多；本条未发送")


def _anthropic(base, key, model, effort, prompt, image, tools, post, *, cache_layout=None, cache_key=None):
    content = [{"type": "text", "text": prompt}]
    if image:
        content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}})
    budget = THINKING_BUDGET.get(effort)
    payload = {"model": model, "max_tokens": 8192 + (budget or 0), "messages": [{"role": "user", "content": content}]}
    if cache_layout and cache_layout.enabled:
        stable = '\n\n'.join(filter(None, (cache_layout.system, cache_layout.stable_user)))
        block = {"type": "text", "text": stable}
        if official_host(base) == 'api.anthropic.com':
            block['cache_control'] = {"type": "ephemeral"}
        new_content = [block]
        if cache_layout.dynamic_user:
            new_content.append({"type": "text", "text": "\n\n" + cache_layout.dynamic_user})
        if image:
            new_content.insert(1, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}})
        payload['messages'] = [{"role": "user", "content": new_content}]
    if budget:
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if tools:
        payload["tools"] = [{"name": tool["name"], "description": tool["description"],
                             "input_schema": tool["inputSchema"]} for tool in tools]
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    for step in range(STEPS):
        if tools and step == STEPS - 1:
            payload["tool_choice"] = {"type": "none"}
        result = post(url, headers, payload)
        blocks = result["content"]
        calls = [block for block in blocks if block.get("type") == "tool_use"]
        if not (tools and calls and result.get("stop_reason") == "tool_use"):
            if result.get("stop_reason") not in ("end_turn", "stop_sequence"):
                raise RuntimeError("模型回复未完整生成；本条未发送")
            return "".join(block["text"] for block in blocks if block.get("type") == "text")
        payload["messages"].append({"role": "assistant", "content": blocks})  # thinking blocks go back as given
        named = [{"id": call["id"], "function": {"name": call["name"], "arguments": call["input"]}} for call in calls]
        yield named
        payload["messages"].append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call["id"], "content": call["output"]} for call in named]})
    raise RuntimeError("模型查询工具调用轮数过多；本条未发送")


ADAPTERS = {"openai-completions": _completions, "openai-responses": _responses, "anthropic-messages": _anthropic}


def generate(prompt: str, directory: Path, api: dict, image_path: Path | None = None,
             allow_empty: bool = False, library: bool = False, *, api_key_override: str | None = None) -> str:
    protocol, base, model, effort = settings(api)
    # One failing provider must not stall other saved API connections.
    retry_key = f"{directory.resolve()}:{api.get('api_key_id') or 'chat'}:{base}"
    if time.monotonic() < _retry_until.get(retry_key, 0):
        raise RuntimeError("当前模型 API 暂时不可用，等待 30 秒后再试；本条未发送")
    if image_path and model in MODELS:
        model = "deepseek-flash"  # DeepSeek's image model
    image = base64.b64encode(image_path.read_bytes()).decode("ascii") if image_path else None
    usage_info = {}

    def _tracking_post(*args):
        res = _post(*args, retry_key)
        if isinstance(res, dict) and 'usage' in res and isinstance(res['usage'], dict):
            u = res['usage']
            usage_info['usage_source'] = 'provider'
            cached = int((u.get('prompt_tokens_details') or {}).get('cached_tokens', 0)
                         or (u.get('input_tokens_details') or {}).get('cached_tokens', 0)
                         or u.get('prompt_cache_hit_tokens', 0)
                         or u.get('cache_read_input_tokens', 0) or 0)
            created = int(u.get('cache_creation_input_tokens', 0) or 0)
            # Anthropic's input_tokens EXCLUDES cache reads/writes. OpenAI and
            # DeepSeek prompt_tokens already INCLUDE cached prompt tokens.
            if protocol == 'anthropic-messages':
                total_input = int(u.get('input_tokens', 0) or 0) + cached + created
            else:
                total_input = int(u.get('prompt_tokens', u.get('input_tokens', 0)) or 0)
            usage_info['input_tokens'] = usage_info.get('input_tokens', 0) + total_input
            usage_info['output_tokens'] = usage_info.get('output_tokens', 0) + int(u.get('completion_tokens', u.get('output_tokens', 0)) or 0)
            usage_info['cached_input_tokens'] = usage_info.get('cached_input_tokens', 0) + cached
            usage_info['cache_write_input_tokens'] = usage_info.get('cache_write_input_tokens', 0) + created
        return res

    full_prompt = prompt + '\n' + computer_instructions(directory)
    cache_option = api.get('prompt_cache_mode', 'auto')
    if cache_option not in ('auto', 'off', 'on'):
        cache_option = 'auto'
    host = official_host(base)
    known_provider = host in ('api.openai.com', 'api.anthropic.com', 'api.deepseek.com')
    layout = split_prompt(full_prompt) if cache_option == 'on' or (cache_option == 'auto' and known_provider) else None
    # A tenant-specific routing hint, not a shared cross-user cache identifier.
    cache_key = 'qqdot-' + hashlib.sha256(str(directory.resolve()).encode('utf-8')).hexdigest()[:32]
    tools = _tools(directory) if library else []
    tools = sorted(tools, key=lambda tool: tool.get('name', ''))  # stable schema ordering
    steps = ADAPTERS[protocol](base, api_key_override or load_key(directory, api.get('api_key_id')), model, effort,
                               full_prompt, image, tools, _tracking_post,
                               cache_layout=layout, cache_key=cache_key)
    try:
        while True:
            calls = next(steps)
            if len(calls) > 4:
                raise RuntimeError("模型查询工具调用次数过多；本条未发送")
            for call in calls:
                call["output"] = _run_tool(directory, call["function"]["name"], call["function"]["arguments"])
    except StopIteration as done:
        text = done.value
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型 API 返回格式无法识别；本条未发送") from exc
    if not isinstance(text, str) or (not text.strip() and not allow_empty):
        raise RuntimeError("模型 API 未返回正文；本条未发送")
    try:
        from usage_accounting import record as record_usage
        if not usage_info:
            usage_info = {
                'input_tokens': max(1, len(prompt) // 2),
                'output_tokens': max(1, len(text) // 2),
                'cached_input_tokens': 0,
                'cache_write_input_tokens': 0,
            }
        usage_info['usage_source'] = 'provider' if usage_info.get('usage_source') == 'provider' else 'estimate'
        record_usage(directory, model, 'api', usage_info)
    except Exception:
        pass
    return text.strip()
