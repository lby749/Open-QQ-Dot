"""Build a bounded, privacy-checked prompt extension for one request."""
from dataclasses import dataclass
import json

from persona_store import ScopeMismatch
from persona_types import Candidate, DataScope, PackVersion, PrivacyViolation
from prompt_context import PromptSections, header


_GROUPS = (
    ('structured', 'RETRIEVED_FACTS', 'retrieval/structured'),
    ('memory', 'RETRIEVED_MEMORY', 'retrieval/memory'),
    ('persona', 'RETRIEVED_PERSONA', 'retrieval/persona'),
    ('interests', 'RETRIEVED_INTERESTS', 'retrieval/interest'),
    ('topics', 'RETRIEVED_TOPICS', 'retrieval/knowledge'),
    ('examples', 'RETRIEVED_EXAMPLES', 'retrieval/example'),
    ('style', 'RETRIEVED_STYLE', 'retrieval/style'),
    ('stickers', 'RETRIEVED_STICKERS', 'retrieval/sticker'),
)
_ORDER = tuple(item[0] for item in _GROUPS)
_NAMESPACES = {
    'structured': frozenset({'structured', 'entity'}),
    'memory': frozenset({'memory'}),
    'persona': frozenset({'persona'}),
    'interests': frozenset({'interest'}),
    'topics': frozenset({'knowledge'}),
    'examples': frozenset({'example'}),
    'style': frozenset({'style'}),
    'stickers': frozenset({'sticker'}),
}
_PUBLIC_GROUPS = frozenset({'persona', 'examples', 'style', 'stickers'})


@dataclass(frozen=True)
class BuiltContext:
    prompt_blocks: str
    token_usage: dict
    estimate: bool
    degraded: bool
    trace: dict


class RuntimeContextBuilder:
    def build(self, request, relevant, legacy_context, settings):
        if not isinstance(legacy_context, str):
            raise TypeError('legacy_context must be str')
        settings = settings or {}
        enabled = settings.get('enabled', False)
        if type(enabled) is not bool:
            raise ValueError('enabled must be bool')
        if not enabled:
            return self._result(legacy_context, False, {}, legacy_context, {})

        section_flags = settings.get('sections', {}) or {}
        if not isinstance(section_flags, dict):
            raise ValueError('sections must be a dict')
        active = tuple(group for group in _ORDER if section_flags.get(group, True))
        if not active:
            return self._result(legacy_context, False, {}, legacy_context, {})

        if request is None:
            raise ValueError('request is required when runtime context is enabled')
        trace = {'candidate_counts': {}, 'reason_codes': [],
                 'estimate_kind': 'utf8_bytes_proxy', 'generation_allowed': True}
        selected = {group: [] for group in active}
        seen_candidates = set()
        degraded = bool(getattr(relevant, 'degraded', False)) if relevant is not None else False
        if relevant is None:
            degraded = True
            trace['reason_codes'].append('retrieval_unavailable')
        elif degraded:
            trace['reason_codes'].append('retrieval_degraded')
        seen = 0
        if relevant is not None:
            for group in active:
                try:
                    values = getattr(relevant, group)
                    iterator = iter(values)
                except Exception:
                    trace['reason_codes'].append('malformed_group')
                    degraded = True
                    trace['candidate_counts'][group] = {'accepted': 0, 'excluded': 1}
                    continue
                excluded = 0
                for item in iterator:
                    if seen >= 50:
                        trace['reason_codes'].append('candidate_limit_exceeded')
                        degraded = True
                        break
                    seen += 1
                    try:
                        if not isinstance(item, Candidate):
                            raise ValueError('invalid_candidate')
                        self._validate_scope(item, request)
                        if item.namespace not in _NAMESPACES[group]:
                            raise ScopeMismatch('candidate namespace does not match its result group')
                        if item.original_scope == DataScope.PUBLIC_PACK and group not in _PUBLIC_GROUPS:
                            raise ScopeMismatch('public candidate entered a private knowledge group')
                        if not item.content.strip():
                            raise ValueError('empty_content')
                        # If the entire substantive candidate is already in the
                        # protected context, do not pay for it a second time.
                        # Short fragments may occur coincidentally and are kept.
                        if len(item.content.strip()) >= 80 and item.content.strip() in legacy_context:
                            excluded += 1
                            trace['reason_codes'].append('context_candidate_already_present')
                            continue
                        duplicate_key = (item.original_scope, item.namespace, item.id, item.pack_version)
                        if duplicate_key in seen_candidates:
                            excluded += 1
                            degraded = True
                            trace['reason_codes'].append('duplicate_candidate_removed')
                            continue
                        seen_candidates.add(duplicate_key)
                        selected[group].append(item)
                    except (ScopeMismatch, PrivacyViolation):
                        raise
                    except Exception:
                        excluded += 1
                        degraded = True
                        trace['reason_codes'].append('invalid_candidate_excluded')
                trace['candidate_counts'][group] = {
                    'accepted': len(selected[group]), 'excluded': excluded}

        if not any(selected.values()):
            max_context = settings.get('max_context_tokens')
            if max_context is not None:
                if type(max_context) is not int or max_context < 0:
                    raise ValueError('max_context_tokens must be a nonnegative integer')
                if len(legacy_context.encode('utf-8')) > max_context:
                    trace['reason_codes'].append('protected_context_over_budget')
                    trace['generation_allowed'] = False
                    degraded = True
            trace['reason_codes'] = sorted(set(trace['reason_codes']))
            return self._result(legacy_context, degraded, trace, legacy_context, selected)

        budgets = request.budgets
        # These limits use the same explicit UTF-8-byte proxy as global estimation.
        self._fit_bucket(selected, ('examples',), budgets.examples, trace, 'examples_budget')
        self._fit_persona_bucket(selected, budgets.persona, trace)
        self._fit_bucket(selected, ('interests', 'topics'), budgets.knowledge,
                         trace, 'knowledge_budget')
        if trace['reason_codes']:
            degraded = True

        max_context = settings.get('max_context_tokens')
        if max_context is not None:
            if type(max_context) is not int or max_context < 0:
                raise ValueError('max_context_tokens must be a nonnegative integer')
            while self._total_size(legacy_context, selected) > max_context:
                removed = self._remove_lowest(selected, 'examples')
                code = 'global_examples_removed'
                if not removed:
                    removed = self._remove_lowest(selected, 'interests', 'topics')
                    code = 'global_knowledge_removed'
                if not removed:
                    removed = self._remove_lowest(selected, 'persona')
                    code = 'global_persona_removed'
                if not removed and selected.get('style'):
                    selected['style'].clear()
                    removed, code = True, 'global_style_removed'
                if not removed and selected.get('stickers'):
                    selected['stickers'].clear()
                    removed, code = True, 'global_stickers_removed'
                if not removed:
                    trace['reason_codes'].append('protected_context_over_budget')
                    trace['generation_allowed'] = False
                    degraded = True
                    break
                trace['reason_codes'].append(code)
                degraded = True

        additions = self._render_additions(selected)
        prompt = legacy_context if not additions else legacy_context + '\n\n' + additions
        usage = self._usage(legacy_context, selected)
        trace['estimated_bytes'] = usage['total']
        trace['candidate_counts']['rendered'] = {
            group: len(values) for group, values in selected.items() if values}
        trace['reason_codes'] = sorted(set(trace['reason_codes']))
        return self._result(prompt, degraded, trace, legacy_context, selected,
                            token_usage=usage)

    @staticmethod
    def _validate_scope(candidate, request):
        if candidate.friend_id != request.friend_id:
            raise ScopeMismatch('candidate belongs to another friend')
        source = candidate.source
        if candidate.original_scope == DataScope.PRIVATE_FRIEND:
            if source.friend_id != request.friend_id:
                raise ScopeMismatch('private candidate source belongs to another friend')
        elif candidate.original_scope == DataScope.PUBLIC_PACK:
            version = PackVersion(pack_id=source.pack_id, version=candidate.pack_version)
            if source.pack_version != candidate.pack_version or version not in request.allowed_pack_versions:
                raise ScopeMismatch('public candidate pack is not allowed')
        else:
            raise ScopeMismatch('runtime candidate scope is not allowed')

    @classmethod
    def _render_group(cls, group, values):
        if not values:
            return ''
        _, title, source = next(row for row in _GROUPS if row[0] == group)
        sections = PromptSections()
        sections.add(title, source, '\n\n'.join(item.content for item in values), data=True)
        block_name, block_source, text, data = sections.blocks[0]
        content = json.dumps(text, ensure_ascii=False) if data else text
        return header(block_name, block_source, True) + '\n' + content

    @classmethod
    def _render_additions(cls, selected):
        return '\n\n'.join(block for group in _ORDER
                            if group in selected
                            if (block := cls._render_group(group, selected[group])))

    @classmethod
    def _total_size(cls, legacy, selected):
        additions = cls._render_additions(selected)
        prompt = legacy if not additions else legacy + '\n\n' + additions
        return len(prompt.encode('utf-8'))

    @classmethod
    def _group_size(cls, group, values):
        return len(cls._render_group(group, values).encode('utf-8'))

    @classmethod
    def _fit_bucket(cls, selected, groups, limit, trace, code):
        while cls._bucket_size(selected, groups) > limit:
            if not cls._remove_lowest(selected, *groups):
                break
            trace['reason_codes'].append(code)

    @classmethod
    def _fit_persona_bucket(cls, selected, limit, trace):
        groups = ('examples', 'persona', 'style', 'stickers')
        if cls._bucket_size(selected, groups) <= limit:
            return
        while selected.get('examples') and cls._bucket_size(selected, groups) > limit:
            cls._remove_lowest(selected, 'examples')
            trace['reason_codes'].append('persona_examples_budget')
        while selected.get('persona') and cls._bucket_size(selected, groups) > limit:
            cls._remove_lowest(selected, 'persona')
            trace['reason_codes'].append('persona_budget')
        if selected.get('style') and cls._bucket_size(selected, groups) > limit:
            selected['style'].clear()
            trace['reason_codes'].append('persona_style_removed')
        if selected.get('stickers') and cls._bucket_size(selected, groups) > limit:
            selected['stickers'].clear()
            trace['reason_codes'].append('persona_stickers_removed')

    @classmethod
    def _bucket_size(cls, selected, groups):
        blocks = [cls._render_group(group, selected[group]) for group in groups
                  if group in selected and selected[group]]
        return len('\n\n'.join(blocks).encode('utf-8'))

    @staticmethod
    def _remove_lowest(selected, *groups):
        candidates = [(group, index) for group in groups if group in selected
                      for index in range(len(selected[group]))]
        if not candidates:
            return False
        # Lower score is less relevant; ties remove later rows/groups first.
        group, index = min(candidates, key=lambda item: (
            selected[item[0]][item[1]].score, -item[1], -groups.index(item[0])))
        selected[group].pop(index)
        return True

    @classmethod
    def _usage(cls, legacy, selected):
        sections = {group: cls._group_size(group, values)
                    for group, values in selected.items() if values}
        total = cls._total_size(legacy, selected)
        return {
            'method': 'utf8_bytes',
            'total': total,
            'legacy': len(legacy.encode('utf-8')),
            'sections': sections,
            'persona_used': cls._bucket_size(selected,
                ('persona', 'examples', 'style', 'stickers')),
            'examples_used': cls._bucket_size(selected, ('examples',)),
            'knowledge_used': cls._bucket_size(selected, ('interests', 'topics')),
        }

    @staticmethod
    def _result(prompt, degraded, trace, legacy, selected, token_usage=None):
        if token_usage is None:
            token_usage = RuntimeContextBuilder._usage(legacy, selected)
        trace = dict(trace)
        trace.setdefault('estimated_bytes', token_usage['total'])
        trace.setdefault('estimate_kind', 'utf8_bytes_proxy')
        trace.setdefault('generation_allowed', True)
        return BuiltContext(prompt_blocks=prompt, token_usage=token_usage,
                            estimate=True, degraded=bool(degraded), trace=trace)
