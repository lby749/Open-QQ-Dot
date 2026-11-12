"""Optional local scene judgment and additive retrieval hints."""
from dataclasses import dataclass
import json

from persona_store import ScopeMismatch
from persona_types import (AnalysisCallPolicy, AnalysisPurpose, ConversationPlan,
    InvalidSource, PrivacyViolation, ProviderKind, RetrievalRequest, SourceRef)
from retrieval_router import RetrievalQueryRouter, RouterDecision


_SCENES = frozenset({'daily_chat', 'knowledge', 'history', 'emotional', 'active', 'unknown'})
_STATUSES = frozenset({'ok', 'unknown'})
_REASON_CODES = frozenset({'clear', 'ambiguous', 'insufficient_context',
    'mixed_signals', 'unknown'})
_ROUTES = frozenset({'structured', 'memory', 'keyword', 'semantic', 'entity',
    'temporal', 'interest', 'topic', 'persona', 'example'})
_OUTPUT_FIELDS = frozenset({'scene', 'reason_code', 'status', 'route_hints'})
_MAX_OUTPUT_CHARS = 4096
_MAX_ROUTE_HINTS = 5
_MAX_INPUT_CHARS = 8192
_MAX_RETRIEVERS = 6


@dataclass(frozen=True)
class LocalDecisionResult:
    baseline: RouterDecision
    retrievers: tuple[str, ...]
    scene: str
    reason_code: str
    status: str
    route_hints: tuple[str, ...]
    degraded_code: str | None


class LocalDecisionRouter:
    """Wrap the deterministic router with one injected LOCAL decision call."""

    def __init__(self, provider=None):
        if provider is not None and getattr(provider, 'provider_kind', None) not in (
                ProviderKind.LOCAL, ProviderKind.LOCAL.value):
            raise PrivacyViolation('provider must explicitly declare LOCAL')
        self._provider = provider
        self._router = RetrievalQueryRouter()

    def decide(self, request, source_refs) -> LocalDecisionResult:
        if not isinstance(request, RetrievalRequest):
            raise InvalidSource('request must be RetrievalRequest')
        baseline = self._router.route(request)
        if isinstance(request.plan, ConversationPlan) and request.plan.action in ('wait', 'ignore'):
            return LocalDecisionResult(baseline=baseline,
                retrievers=tuple(dict.fromkeys(baseline.retrievers)), scene='unknown',
                reason_code='planner_skip', status='unknown', route_hints=(), degraded_code=None)

        _validated_local_sources(request, source_refs)
        if len(request.query) + len(request.recent_context) > _MAX_INPUT_CHARS:
            return _fallback(baseline, 'input_too_large')
        if self._provider is None:
            return _fallback(baseline, 'provider_unavailable')

        payload = {'scene': request.scene, 'query': request.query,
                   'recent_context': request.recent_context}
        try:
            raw = self._provider(payload)
        except (ScopeMismatch, PrivacyViolation, InvalidSource):
            raise
        except TimeoutError:
            return _fallback(baseline, 'provider_timeout')
        except Exception:
            return _fallback(baseline, 'provider_error')

        try:
            scene, reason_code, status, hints = _parse_output(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _fallback(baseline, 'provider_output_invalid')
        base = tuple(dict.fromkeys(baseline.retrievers))
        accepted_hints, additions = [], []
        for hint in hints:
            if hint in accepted_hints:
                continue
            if hint not in base and len(base) + len(additions) >= _MAX_RETRIEVERS:
                continue
            accepted_hints.append(hint)
            if hint not in base:
                additions.append(hint)
        merged = base + tuple(additions)
        return LocalDecisionResult(baseline=baseline, retrievers=merged, scene=scene,
            reason_code=reason_code, status=status, route_hints=tuple(accepted_hints),
            degraded_code=None)


def _validated_local_sources(request, source_refs):
    if not isinstance(source_refs, (list, tuple)) or not source_refs:
        raise InvalidSource('source_refs must be a nonempty sequence')
    if any(not isinstance(ref, SourceRef) for ref in source_refs):
        raise InvalidSource('source_refs must contain SourceRef values')
    if any(ref.data_scope.value == 'PRIVATE_FRIEND' and ref.friend_id != request.friend_id
           for ref in source_refs):
        raise ScopeMismatch('source belongs to another friend')
    AnalysisCallPolicy.validate(AnalysisPurpose.SCENE_ANALYSIS, ProviderKind.LOCAL, source_refs)


def _fallback(baseline, code):
    return LocalDecisionResult(baseline=baseline,
        retrievers=tuple(dict.fromkeys(baseline.retrievers)), scene='unknown',
        reason_code='unknown', status='unknown', route_hints=(), degraded_code=code)


def _parse_output(raw):
    if isinstance(raw, str):
        if len(raw) > _MAX_OUTPUT_CHARS:
            raise ValueError('output too large')
        raw = json.loads(raw)
    if not isinstance(raw, dict) or set(raw) != _OUTPUT_FIELDS:
        raise ValueError('output fields do not match the fixed schema')
    scene, reason_code, status, hints = (raw[name] for name in
        ('scene', 'reason_code', 'status', 'route_hints'))
    if not isinstance(scene, str) or scene not in _SCENES or (
            not isinstance(status, str) or status not in _STATUSES):
        raise ValueError('output contains an unknown fixed value')
    if not isinstance(reason_code, str) or reason_code not in _REASON_CODES:
        raise ValueError('output contains an unknown reason code')
    if (status == 'unknown') != (scene == 'unknown'):
        raise ValueError('unknown status and scene must agree')
    if (status == 'unknown') != (reason_code == 'unknown'):
        raise ValueError('unknown status and reason code must agree')
    if not isinstance(hints, list) or len(hints) > _MAX_ROUTE_HINTS:
        raise ValueError('route_hints must be a bounded list')
    if any(not isinstance(route, str) or route not in _ROUTES for route in hints):
        raise ValueError('route_hints contains an unsupported retriever')
    if status == 'unknown' and hints:
        raise ValueError('unknown output cannot add routes')
    return scene, reason_code, status, tuple(dict.fromkeys(hints))
