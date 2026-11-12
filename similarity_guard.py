"""Bounded text similarity check for local reply examples and history."""
import json
import re
from dataclasses import dataclass

from persona_types import Candidate


_SHORT_PHRASES = frozenset({'haha', 'lol', 'what?', '?', 'ok'})
_LIMIT_TARGETS = 50
_LIMIT_EXAMPLES = 20
_LIMIT_HISTORY = 50
_LIMIT_BUBBLES_PER_SOURCE = 50
_THRESHOLD = 0.9


@dataclass(frozen=True)
class SimilarityResult:
    flagged: bool
    reason: str
    similarities: tuple[float, ...]


class SimilarityGuard:
    def check(self, target_bubbles, retrieved_examples, successful_history):
        targets = _strings(target_bubbles, _LIMIT_TARGETS, 'target_bubbles')
        examples = _reference_examples(retrieved_examples)
        history = _strings(successful_history, _LIMIT_HISTORY, 'successful_history')
        references = examples + list(history)
        if not references:
            return SimilarityResult(False, 'no_reference', tuple(0.0 for _ in targets))

        scores = []
        for target in targets:
            normalized = _normalize(target)
            if normalized in _SHORT_PHRASES:
                scores.append(0.0)
                continue
            target_grams = _bigrams(normalized)
            best = 0.0
            for reference in references:
                candidate = _normalize(reference)
                if candidate in _SHORT_PHRASES:
                    continue
                grams = _bigrams(candidate)
                union = target_grams | grams
                if union:
                    best = max(best, len(target_grams & grams) / len(union))
            scores.append(best)

        flagged = any(score >= _THRESHOLD for score in scores)
        if flagged:
            reason = 'high_similarity'
        elif all(_normalize(item) in _SHORT_PHRASES for item in targets):
            reason = 'short_phrase_exempt'
        else:
            reason = 'below_threshold'
        return SimilarityResult(flagged, reason, tuple(scores))


def may_rewrite_once(result, rewrite_attempted=False):
    """Return a single caller-controlled rewrite suggestion; never invokes a model."""
    return isinstance(result, SimilarityResult) and result.flagged and rewrite_attempted is False


def _reference_examples(values):
    if not isinstance(values, (list, tuple)) or len(values) > _LIMIT_EXAMPLES:
        raise ValueError('retrieved_examples must be a bounded sequence')
    found = []
    for candidate in values:
        if not isinstance(candidate, Candidate) or candidate.namespace != 'example':
            continue
        try:
            payload = json.loads(candidate.content)
            bubbles = payload.get('target_bubbles') if isinstance(payload, dict) else None
            found.extend(_bubble_texts(bubbles))
        except (TypeError, ValueError):
            continue
    return found


def _bubble_texts(values):
    if not isinstance(values, (list, tuple)) or len(values) > _LIMIT_BUBBLES_PER_SOURCE:
        return []
    result = []
    for bubble in values:
        if isinstance(bubble, str):
            text = bubble
        elif isinstance(bubble, dict):
            text = bubble.get('text')
        else:
            continue
        if isinstance(text, str) and text.strip():
            result.append(text)
    return result


def _strings(values, limit, name):
    if not isinstance(values, (list, tuple)) or len(values) > limit:
        raise ValueError(name + ' must be a bounded sequence of strings')
    if any(not isinstance(value, str) for value in values):
        raise ValueError(name + ' must contain only strings')
    return tuple(value for value in values if value.strip())


def _normalize(value):
    return re.sub(r'\s+', '', value.casefold())


def _bigrams(value):
    return {value[index:index + 2] for index in range(len(value) - 1)}
