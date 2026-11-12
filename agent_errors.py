"""Structured Agent Error Classification, Unified Error Adapter & Exponential Backoff Engine.

Provides reliable classification across Codex, Claude, Antigravity, DeepSeek Harness, and Custom API:
- RATE_LIMIT: Temporary rate limit (429, retryable with backoff)
- QUOTA_EXHAUSTED: Zero balance / quota exhausted / out of credits (triggers immediate handover)
- AUTH_ERROR: Unauthorized, invalid API key, login expired (401, 403)
- NETWORK_ERROR: Connection reset, DNS failure, 502/503 (retryable)
- TIMEOUT: Request timeout, subprocess timeout (retryable)
- POLICY_BLOCKED: Safety filter, content policy rejection
- MODEL_UNAVAILABLE: Model deprecated, not found, or backend service down
- TOOL_ERROR: Tool invocation failure, execution error
"""
from __future__ import annotations

import enum
import json
import logging
import re
import socket
import subprocess
import time
import urllib.error
from typing import Any, Callable

logger = logging.getLogger(__name__)


class AgentErrorType(str, enum.Enum):
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_ERROR = "AUTH_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TOOL_ERROR = "TOOL_ERROR"

class AgentError(RuntimeError):
    """Structured exception wrapping backend errors with normalized categorization."""
    def __init__(
        self,
        error_type: AgentErrorType | str = AgentErrorType.TOOL_ERROR,
        provider: str = "generic",
        message: str = "",
        status_code: int | None = None,
        retryable: bool = False,
        raw_data: Any = None,
    ):
        if isinstance(error_type, str) and not isinstance(error_type, AgentErrorType):
            # Check if user passed message as first arg
            if error_type in [e.value for e in AgentErrorType]:
                error_type = AgentErrorType(error_type)
            else:
                message = error_type
                error_type = AgentErrorType.TOOL_ERROR

        super().__init__(f"[{provider.upper()}:{error_type.value}] {message}")
        self.error_type = error_type
        self.provider = provider
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.raw_data = raw_data

    def is_quota_or_rate_limit(self) -> bool:
        return self.error_type in (AgentErrorType.RATE_LIMIT, AgentErrorType.QUOTA_EXHAUSTED)


def classify_error(
    provider: str,
    exc: Exception | None = None,
    status_code: int | None = None,
    error_payload: dict | None = None,
    exit_code: int | None = None,
    stderr: str | None = None,
) -> AgentError:
    """Classify model invocation failure into structured AgentError.

    DOES NOT classify arbitrary assistant response text; only inspects verified
    HTTP status codes, process exit codes, error payloads, and stderr/exceptions.
    """
    msg_parts = []
    p = provider.lower()
    if isinstance(exc, PermissionError):
        return AgentError(AgentErrorType.PERMISSION_DENIED, provider=p,
                          message='没有所需权限，受限操作没有继续执行', retryable=False)

    # 1. Inspect HTTPError or status code
    if exc and isinstance(exc, urllib.error.HTTPError):
        status_code = exc.code
        try:
            body = exc.read().decode('utf-8', errors='ignore')
            msg_parts.append(body)
            try:
                error_payload = json.loads(body)
            except Exception:
                pass
        except Exception:
            pass

    if exc and isinstance(exc, (subprocess.TimeoutExpired, TimeoutError, socket.timeout)):
        return AgentError(
            AgentErrorType.TIMEOUT,
            provider=p,
            message="调用超时 (TimeoutExpired)",
            status_code=408,
            retryable=True,
            raw_data=exc,
        )

    if exc and isinstance(exc, (urllib.error.URLError, ConnectionError, ConnectionResetError)):
        return AgentError(
            AgentErrorType.NETWORK_ERROR,
            provider=p,
            message=f"Network connectivity error: {exc}",
            retryable=True,
            raw_data=exc,
        )

    # Combine text from payload / stderr / exception for error analysis
    err_text = ""
    if error_payload:
        err_text += " " + json.dumps(error_payload, ensure_ascii=False).lower()
    if stderr:
        err_text += " " + stderr.lower()
    if exc:
        err_text += " " + str(exc).lower()

    # Model entitlement is narrower than an account quota. Other Claude models remain usable.
    if p == 'claude' and 'requires usage credits' in err_text and 'switch to another model' in err_text:
        return AgentError(AgentErrorType.MODEL_UNAVAILABLE, provider=p,
                          message='所选 Claude 模型需要额外用量额度，请切换其他可用模型。',
                          retryable=False, raw_data=error_payload)

    # Check HTTP Status Code first
    if status_code == 429:
        # Distinguish between rate limit and exhausted quota
        if any(k in err_text for k in ('quota', 'insufficient_quota', 'credit', 'balance', 'billing', '欠费', '额度耗尽', '余额不足')):
            return AgentError(AgentErrorType.QUOTA_EXHAUSTED, provider=p, message="API 额度已耗尽 (429 Quota Exhausted)", status_code=429, retryable=False, raw_data=error_payload)
        return AgentError(AgentErrorType.RATE_LIMIT, provider=p, message="请求频率超限 (429 Rate Limit)", status_code=429, retryable=True, raw_data=error_payload)

    if status_code in (401, 403):
        return AgentError(AgentErrorType.AUTH_ERROR, provider=p, message=f"Authentication failed or access denied ({status_code} Unauthorized/Forbidden)", status_code=status_code, retryable=False, raw_data=error_payload)

    if status_code == 404:
        return AgentError(AgentErrorType.MODEL_UNAVAILABLE, provider=p, message="请求的模型或接口端点不存在 (404 Not Found)", status_code=404, retryable=False, raw_data=error_payload)

    if status_code in (502, 503, 504):
        return AgentError(AgentErrorType.NETWORK_ERROR, provider=p, message=f"Upstream service temporarily unavailable ({status_code})", status_code=status_code, retryable=True, raw_data=error_payload)

    # Check CLI Exit Code
    if exit_code == 124:
        return AgentError(AgentErrorType.TIMEOUT, provider=p, message="CLI 进程运行超时 (Exit Code 124)", retryable=True)

    # Check structured error payload fields (OpenAI / Anthropic / Gemini format)
    if error_payload and isinstance(error_payload, dict):
        err_obj = error_payload.get('error') or error_payload
        if isinstance(err_obj, dict):
            err_code = str(err_obj.get('code') or '').lower()
            err_type = str(err_obj.get('type') or '').lower()
            err_msg = str(err_obj.get('message') or '')
            if err_code in ('insufficient_quota', 'quota_exceeded', 'bill_overdue') or 'quota' in err_type:
                return AgentError(AgentErrorType.QUOTA_EXHAUSTED, provider=p, message=err_msg or "额度已耗尽", retryable=False, raw_data=error_payload)
            if err_code == 'rate_limit_exceeded' or 'rate_limit' in err_type:
                return AgentError(AgentErrorType.RATE_LIMIT, provider=p, message=err_msg or "频率限制", retryable=True, raw_data=error_payload)
            if err_type in ('authentication_error', 'permission_error') or err_code in ('invalid_api_key', 'unauthorized'):
                return AgentError(AgentErrorType.AUTH_ERROR, provider=p, message=err_msg or "鉴权无效", retryable=False, raw_data=error_payload)
            if 'safety' in err_type or 'blocked' in err_code or 'content_policy' in err_type:
                return AgentError(AgentErrorType.POLICY_BLOCKED, provider=p, message=err_msg or "触发安全策略或内容审查", retryable=False, raw_data=error_payload)
            if 'model_not_found' in err_code or 'model' in err_type:
                return AgentError(AgentErrorType.MODEL_UNAVAILABLE, provider=p, message=err_msg or "模型不可用", retryable=False, raw_data=error_payload)

    # Check stderr / exception message for unequivocal error signatures
    if any(k in err_text for k in ('claude auth login', 'not logged in', 'invalid api key', 'unauthorized', 'api key not found')):
        return AgentError(AgentErrorType.AUTH_ERROR, provider=p, message="账号未登录或 API Key 无效", retryable=False)

    if any(k in err_text for k in ('insufficient_quota', 'exceeded your current quota', 'credit balance is too low',
                                  'individual quota reached', 'free usage limit reached', '额度不足', '账号欠费')):
        return AgentError(AgentErrorType.QUOTA_EXHAUSTED, provider=p, message="额度已耗尽", retryable=False)

    if any(k in err_text for k in ('rate limit', 'ratelimit', 'too many requests', '429', 'resource_exhausted')):
        return AgentError(AgentErrorType.RATE_LIMIT, provider=p, message="频控速率限制", retryable=True)

    if any(k in err_text for k in ('timed out', 'timeout expired', 'request timeout')):
        return AgentError(AgentErrorType.TIMEOUT, provider=p, message="请求超时", retryable=True)

    if any(k in err_text for k in ('safety policy', 'content filter', 'harmful content', 'policy violation')):
        return AgentError(AgentErrorType.POLICY_BLOCKED, provider=p, message="触发安全策略拦截", retryable=False)

    if any(k in err_text for k in ('model not found', 'unknown model', 'does not exist')):
        return AgentError(AgentErrorType.MODEL_UNAVAILABLE, provider=p, message="指定的模型不存在或不可用", retryable=False)

    if any(k in err_text for k in ('tool error', 'tool execution failed', 'mcp error')):
        return AgentError(AgentErrorType.TOOL_ERROR, provider=p, message="okfailed", retryable=False)

    # Default fallback
    fallback_msg = str(exc) if exc else (stderr.strip()[:200] if stderr else "未知调用异常")
    return AgentError(AgentErrorType.TOOL_ERROR, provider=p, message=fallback_msg, retryable=False)


def execute_with_retry(
    provider: str,
    func: Callable[[], Any],
    max_retries: int = 2,
    base_delay: float = 1.0,
) -> Any:
    """Execute model call with structured error handling and exponential backoff."""
    last_error: AgentError | None = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except InterruptedError:
            raise  # cancellation or an uncertain outcome must never become a retryable model error
        except AgentError as err:
            last_error = err
            if not err.retryable or attempt >= max_retries:
                raise
            delay = min(base_delay * (2 ** attempt), 8.0)
            logger.warning(f"[{provider}] Retryable error {err.error_type.value}: {err.message}, retrying in {delay:.1f}s ({attempt+1}/{max_retries})")
            time.sleep(delay)
        except Exception as exc:
            err = classify_error(provider, exc=exc)
            last_error = err
            if not err.retryable or attempt >= max_retries:
                raise err
            delay = min(base_delay * (2 ** attempt), 8.0)
            logger.warning(f"[{provider}] Retryable error {err.error_type.value}: {err.message}, retrying in {delay:.1f}s ({attempt+1}/{max_retries})")
            time.sleep(delay)
    if last_error:
        raise last_error
