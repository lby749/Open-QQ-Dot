"""Dynamic multi-model capability registry, task matcher, concurrency limiter,
and call accounting subsystem.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from agent_errors import AgentError, AgentErrorType

logger = logging.getLogger(__name__)


class ModelCapability(str, Enum):
    DEEP_THINKING = "deep_thinking"             # , ,
    CODE_EXECUTION = "code_execution"           # ,
    LIGHTWEIGHT = "lightweight_classification" # , ,
    VERIFICATION = "verification"               # , ,
    WEB_SEARCH = "web_search"                   # Note
    TERMINAL_OPERATION = "terminal_operation"   # ok


class TaskCategory(str, Enum):
    PLANNING = "planning"
    CODE = "code"
    RESEARCH = "research"
    CLASSIFICATION = "classification"
    VERIFICATION = "verification"
    GENERAL = "general"


@dataclass
class ModelProfileSpec:
    provider: str
    display_name: str
    capabilities: set[ModelCapability]
    relative_cost: int  # 1 (cheapest) to 10 (most expensive)
    max_concurrency: int = 2
    routing_tags: set[str] = field(default_factory=set)


@dataclass
class ModelUsageStats:
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    total_duration_sec: float = 0.0
    error_counts: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls

    @property
    def avg_duration(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_duration_sec / self.total_calls


# Pre-defined capabilities for standard providers
DEFAULT_MODEL_SPECS: dict[str, ModelProfileSpec] = {
    "codex_app_server": ModelProfileSpec(
        provider="codex_app_server",
        display_name="Codex App Server (JSON-RPC)",
        capabilities={
            ModelCapability.CODE_EXECUTION,
            ModelCapability.DEEP_THINKING,
            ModelCapability.TERMINAL_OPERATION,
            ModelCapability.VERIFICATION,
        },
        relative_cost=6,
        max_concurrency=2,
    ),
    "claude": ModelProfileSpec(
        provider="claude",
        display_name="Claude Code",
        capabilities={
            ModelCapability.DEEP_THINKING,
            ModelCapability.CODE_EXECUTION,
            ModelCapability.VERIFICATION,
            ModelCapability.TERMINAL_OPERATION,
        },
        relative_cost=8,
        max_concurrency=2,
    ),
    "codex": ModelProfileSpec(
        provider="codex",
        display_name="Codex CLI",
        capabilities={
            ModelCapability.CODE_EXECUTION,
            ModelCapability.DEEP_THINKING,
            ModelCapability.TERMINAL_OPERATION,
        },
        relative_cost=6,
        max_concurrency=2,
    ),
    "antigravity": ModelProfileSpec(
        provider="antigravity",
        display_name="Antigravity Tools",
        capabilities={
            ModelCapability.CODE_EXECUTION,
            ModelCapability.WEB_SEARCH,
            ModelCapability.DEEP_THINKING,
            ModelCapability.TERMINAL_OPERATION,
        },
        relative_cost=5,
        max_concurrency=3,
    ),
    "custom": ModelProfileSpec(
        provider="custom",
        display_name="Custom API",
        capabilities={
            ModelCapability.LIGHTWEIGHT,
            ModelCapability.VERIFICATION,
            ModelCapability.DEEP_THINKING,
        },
        relative_cost=2,
        max_concurrency=4,
    ),
    "dsh": ModelProfileSpec(
        provider="dsh",
        display_name="DeepSeek Harness",
        capabilities={
            ModelCapability.DEEP_THINKING,
            ModelCapability.LIGHTWEIGHT,
            ModelCapability.VERIFICATION,
        },
        relative_cost=2,
        max_concurrency=4,
    ),
}


from agent_tool_adapters import AGENT_BACKENDS, BUILTINS
for _backend in AGENT_BACKENDS:
    DEFAULT_MODEL_SPECS[_backend] = ModelProfileSpec(
        provider=_backend, display_name=BUILTINS[_backend].name,
        capabilities={ModelCapability.CODE_EXECUTION, ModelCapability.DEEP_THINKING,
                      ModelCapability.TERMINAL_OPERATION}, relative_cost=6,
        routing_tags={'terminal', 'file_write', 'code_implementation'})
for _backend, _name in (('webchat_deepseek', '网页端 DeepSeek'),
                        ('webchat_chatgpt', '网页端 ChatGPT'),
                        ('webchat_gemini', '网页端 Gemini')):
    DEFAULT_MODEL_SPECS[_backend] = ModelProfileSpec(
        provider=_backend, display_name=_name,
        capabilities={ModelCapability.DEEP_THINKING, ModelCapability.WEB_SEARCH},
        relative_cost=1, max_concurrency=1,
        routing_tags={'webchat', 'analysis', 'web_search', 'file_write', 'terminal'})

# Orchestrator routing names live in the same provider registry as capability/concurrency.
for _provider,_tags in {
    'codex': {'terminal','file_write','code_implementation','math_logic'},
    'codex_app_server': {'terminal','file_write','code_implementation','steerable','native_agent'},
    'claude': {'terminal','file_write','code_implementation','architecture','security_audit','complex_reasoning'},
    'antigravity': {'web_search','internet_info','deep_reasoning','terminal','file_write','code_implementation'},
    'dsh': {'math_logic','algorithm','fast_code'},
    'custom': {'domain_api','fast_code'},
}.items():
    DEFAULT_MODEL_SPECS[_provider].routing_tags=set(_tags)


class ModelDispatcher:
    """Manages model selection, concurrency gating, failover fallback, and statistics."""

    _instance: ModelDispatcher | None = None
    _lock = threading.RLock()

    def __init__(self):
        self._specs = dict(DEFAULT_MODEL_SPECS)
        self._semaphores: dict[str, threading.Semaphore] = {
            p: threading.Semaphore(spec.max_concurrency) for p, spec in self._specs.items()
        }
        self._stats: dict[str, ModelUsageStats] = {p: ModelUsageStats() for p in self._specs}
        self._cooldown_until: dict[str, float] = {}  # Rate-limited or quota exhausted cooldowns

    @classmethod
    def get_instance(cls) -> ModelDispatcher:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def mark_cooldown(self, provider: str, duration_sec: float = 60.0):
        """Temporarily mark a provider as cooling down due to rate limits or errors."""
        with self._lock:
            self._cooldown_until[provider] = time.time() + duration_sec
            logger.warning(f"Provider {provider} marked in cooldown for {duration_sec}s")

    def is_available(self, provider: str) -> bool:
        """Check if provider is not currently in cooldown."""
        with self._lock:
            cd = self._cooldown_until.get(provider, 0.0)
            return time.time() >= cd

    def routing_capabilities(self,provider):
        norm='custom' if provider in ('deepseek','api') else 'dsh' if provider=='deepseek_harness' else provider
        spec=self._specs.get(norm)
        return set(spec.routing_tags) if spec else set()

    def select_best_model(
        self,
        task_category: TaskCategory | str,
        available_providers: list[str],
        required_capability: ModelCapability | None = None,
        prefer_cost_effective: bool = True,
        manual_provider: str | None = None,
        strict_required: bool = True,
    ) -> str:
        """Select the most optimal model based on capability, cost, and availability.

        Respects user-specified manual_provider and enforces required capabilities to avoid downgrade failure.
        """
        if isinstance(task_category, str):
            try:
                task_category = TaskCategory(task_category.lower())
            except ValueError:
                task_category = TaskCategory.GENERAL

        # Map task category to primary required capability if not explicitly specified
        if required_capability is None:
            if task_category == TaskCategory.PLANNING:
                required_capability = ModelCapability.DEEP_THINKING
            elif task_category == TaskCategory.CODE:
                required_capability = ModelCapability.CODE_EXECUTION
            elif task_category == TaskCategory.CLASSIFICATION:
                required_capability = ModelCapability.LIGHTWEIGHT
            elif task_category == TaskCategory.VERIFICATION:
                required_capability = ModelCapability.VERIFICATION
            else:
                required_capability = ModelCapability.DEEP_THINKING

        # 1. Respect manually pinned model if available and capable
        if manual_provider:
            norm_manual = 'custom' if manual_provider in ('deepseek', 'api') else 'dsh' if manual_provider == 'deepseek_harness' else manual_provider
            if manual_provider in available_providers and self.is_available(norm_manual):
                spec = self._specs.get(norm_manual)
                if spec and (not strict_required or required_capability in spec.capabilities):
                    return norm_manual

        # 2. Filter candidate providers by required capability
        candidates = []
        for p in available_providers:
            norm_p = 'custom' if p in ('deepseek', 'api') else 'dsh' if p == 'deepseek_harness' else p
            spec = self._specs.get(norm_p)
            if not spec:
                continue
            if not self.is_available(norm_p):
                continue
            if required_capability in spec.capabilities:
                candidates.append((norm_p, spec))

        if not candidates:
            # If critical capability (e.g. code execution) is strictly required, reject incapable fallback
            if strict_required and required_capability in (ModelCapability.CODE_EXECUTION, ModelCapability.DEEP_THINKING):
                raise AgentError(AgentErrorType.RATE_LIMIT, message=f'No available model satisfies required capability: {required_capability.value}')
            active = [p for p in available_providers if self.is_available(p)]
            if not active:
                raise AgentError(AgentErrorType.RATE_LIMIT, message='没有可用模型，不使用冷却或未授权模型')
            return active[0]

        # Sorting strategy:
        # If classification or lightweight task -> sort by lowest cost first!
        # If code or complex planning and prefer_cost_effective is False -> sort by power (highest cost or specific priority)
        if task_category in (TaskCategory.CLASSIFICATION, TaskCategory.VERIFICATION) or prefer_cost_effective:
            candidates.sort(key=lambda item: item[1].relative_cost)
        else:
            candidates.sort(key=lambda item: -item[1].relative_cost)

        return candidates[0][0]

    def execute_with_budget_and_gate(
        self,
        provider: str,
        runner_fn: Callable[[], tuple[str, Any]],
        timeout_sec: float = 300.0,
        should_stop: Callable[[], None] | None = None,
    ) -> tuple[str, Any]:
        """Execute a call within the model's concurrency gate and update statistics.

        ``should_stop`` is called while waiting for a slot; it raises (e.g. InterruptedError from a task's
        Execution.check) to abandon the wait when the caller was cancelled or paused."""
        norm_p = 'custom' if provider in ('deepseek', 'api') else 'dsh' if provider == 'deepseek_harness' else provider
        sem = self._semaphores.setdefault(norm_p, threading.Semaphore(2))
        stats = self._stats.setdefault(norm_p, ModelUsageStats())

        deadline = time.monotonic() + timeout_sec
        acquired = False
        while not acquired:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            acquired = sem.acquire(timeout=min(1.0, remaining))
            if not acquired and should_stop is not None:
                should_stop()
        if not acquired:
            raise AgentError(
                AgentErrorType.TIMEOUT, provider=norm_p,
                message=f"Model provider {norm_p} concurrency limit reached, wait timed out ({timeout_sec}s)",
            )

        start_time = time.time()
        stats.total_calls += 1
        try:
            result = runner_fn()
            duration = time.time() - start_time
            stats.successful_calls += 1
            stats.total_duration_sec += duration
            return result
        except AgentError as err:
            duration = time.time() - start_time
            stats.failed_calls += 1
            stats.total_duration_sec += duration
            stats.error_counts[err.error_type.value] = stats.error_counts.get(err.error_type.value, 0) + 1
            if err.is_quota_or_rate_limit():
                self.mark_cooldown(norm_p, duration_sec=120.0)
            raise
        except Exception as exc:
            duration = time.time() - start_time
            stats.failed_calls += 1
            stats.total_duration_sec += duration
            err_name = type(exc).__name__
            stats.error_counts[err_name] = stats.error_counts.get(err_name, 0) + 1
            raise
        finally:
            sem.release()

    def get_metrics_report(self) -> dict[str, Any]:
        """Return structured execution metrics for all models."""
        with self._lock:
            report = {}
            for p, st in self._stats.items():
                spec = self._specs.get(p)
                report[p] = {
                    "display_name": spec.display_name if spec else p,
                    "total_calls": st.total_calls,
                    "successful_calls": st.successful_calls,
                    "failed_calls": st.failed_calls,
                    "success_rate": round(st.success_rate, 4),
                    "avg_duration_sec": round(st.avg_duration, 2),
                    "error_breakdown": dict(st.error_counts),
                    "is_in_cooldown": not self.is_available(p),
                }
            return report
