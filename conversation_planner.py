"""Pure plan adapter and compatibility parser for model conversation output."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json

from persona_store import ScopeMismatch, Unavailable
from persona_types import (Candidate, ConversationPlan, DataScope, InvalidSource,
    PackVersion, RetrievalRequest)


_GROUP_NAMESPACES = {
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
_MODES = frozenset({'reply', 'continuation', 'active', 'care'})
_CONTINUATION_PLANS = {
    'wait': 'wait',
    'end': 'ignore',
    'supplement': 'reply',
    'change_topic': 'reply',
}
_PLAN_FIELDS = frozenset({
    'action', 'intent', 'tone', 'core_meaning', 'ask_question', 'continue_topic',
    'introduce_new_topic', 'desired_bubbles', 'sticker_intent',
})
_REQUIRED_PLAN_FIELDS = _PLAN_FIELDS - {'sticker_intent'}


@dataclass(frozen=True)
class ParsedConversation:
    messages: tuple[str, ...]
    plan: ConversationPlan | None
    legacy_payload: dict
    plan_status: str
    mode: str

    @property
    def continuation_action(self):
        return self.legacy_payload.get('action') if self.mode == 'continuation' else None


class ConversationPlanner:
    def plan(self, request, relevant):
        if not isinstance(request, RetrievalRequest):
            raise InvalidSource('request must be RetrievalRequest')
        if relevant is not None:
            for group in _GROUP_NAMESPACES:
                try:
                    candidates = getattr(relevant, group)
                    iterator = iter(candidates)
                except Exception:
                    raise InvalidSource('relevant context is malformed') from None
                for candidate in iterator:
                    self._validate_candidate(candidate, group, request)
        if isinstance(request.plan, ConversationPlan):
            return request.plan
        raise Unavailable('explicit conversation plan is unavailable')

    @staticmethod
    def _validate_candidate(candidate, group, request):
        if not isinstance(candidate, Candidate):
            raise InvalidSource('relevant context contains an invalid candidate')
        if candidate.friend_id != request.friend_id:
            raise ScopeMismatch('candidate belongs to another friend')
        if candidate.namespace not in _GROUP_NAMESPACES[group]:
            raise ScopeMismatch('candidate namespace does not match its result group')
        source = candidate.source
        if candidate.original_scope == DataScope.PRIVATE_FRIEND:
            if source.friend_id != request.friend_id:
                raise ScopeMismatch('private candidate source belongs to another friend')
        elif candidate.original_scope == DataScope.PUBLIC_PACK:
            if group not in _PUBLIC_GROUPS:
                raise ScopeMismatch('public candidate entered a private result group')
            version = PackVersion(pack_id=source.pack_id, version=candidate.pack_version)
            if source.pack_version != candidate.pack_version or version not in request.allowed_pack_versions:
                raise ScopeMismatch('public candidate pack is not allowed')
        else:
            raise ScopeMismatch('runtime candidate scope is not allowed')


class LegacyConversationAdapter:
    @staticmethod
    def parse(model_output, *, mode, now=None, expected_count=None):
        if not isinstance(mode, str) or mode not in _MODES:
            raise InvalidSource('mode must be reply, continuation, active or care')
        if not isinstance(model_output, str):
            raise InvalidSource('model output must be text')

        text = model_output.strip()
        if not text:
            raise InvalidSource('model output is empty')
        if mode == 'reply':
            text = LegacyConversationAdapter._unwrap_json_fence(text)
            try:
                payload = json.loads(text, parse_constant=LegacyConversationAdapter._reject_constant)
            except (json.JSONDecodeError, ValueError):
                if text.startswith(('{', '[', '```')):
                    raise InvalidSource('reply is not valid JSON') from None
                messages = LegacyConversationAdapter._messages([text])
                return ParsedConversation(messages, None, {}, 'plan_unavailable', mode)
        else:
            try:
                payload = json.loads(text, parse_constant=LegacyConversationAdapter._reject_constant)
            except (json.JSONDecodeError, ValueError):
                raise InvalidSource('model output must be strict JSON') from None

        if not isinstance(payload, dict):
            raise InvalidSource('model output must be a JSON object')
        payload = dict(payload)
        plan = LegacyConversationAdapter._parse_plan(payload.get('plan')) if 'plan' in payload else None
        messages_value = LegacyConversationAdapter._get_messages(payload, mode)
        if mode == 'continuation':
            messages = LegacyConversationAdapter._continuation_messages(payload, messages_value, plan, now)
        elif mode in ('active', 'care'):
            messages = LegacyConversationAdapter._counted_messages(
                messages_value, expected_count, allow_empty=(mode == 'care'))
        else:
            messages = LegacyConversationAdapter._messages(
                messages_value, allow_empty=plan is not None and plan.action in ('wait', 'ignore'))

        LegacyConversationAdapter._check_plan_messages(plan, messages)
        return ParsedConversation(messages, plan, payload,
            'available' if plan is not None else 'plan_unavailable', mode)

    @staticmethod
    def _reject_constant(_value):
        raise ValueError('non-standard JSON number')

    @staticmethod
    def _unwrap_json_fence(text):
        if text.startswith('```json') and text.endswith('```'):
            return text[7:-3].strip()
        return text

    @staticmethod
    def _get_messages(payload, mode):
        messages = payload.get('messages')
        if mode == 'reply' and messages is None and isinstance(payload.get('reply'), str):
            messages = [payload['reply']]
        return messages

    @staticmethod
    def _messages(value, *, allow_empty=False, maximum=4, normalize_whitespace=False):
        if not isinstance(value, list):
            raise InvalidSource('messages must be an array')
        if not value and not allow_empty:
            raise InvalidSource('messages must not be empty')
        if maximum is not None and len(value) > maximum:
            raise InvalidSource('too many messages for this mode')
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise InvalidSource('messages must contain nonempty strings')
        messages = tuple(' '.join(item.split()) if normalize_whitespace else item.strip()
                         for item in value)
        if len(set(messages)) != len(messages):
            raise InvalidSource('messages contain duplicate bubbles')
        return messages

    @staticmethod
    def _counted_messages(value, expected_count, *, allow_empty):
        if expected_count is None:
            raise Unavailable('trusted expected_count is required')
        if type(expected_count) is not int or expected_count < 0:
            raise InvalidSource('expected_count must be a nonnegative integer')
        if allow_empty and isinstance(value, list) and not value:
            return ()
        messages = LegacyConversationAdapter._messages(
            value, allow_empty=allow_empty, maximum=None, normalize_whitespace=True)
        if len(messages) != expected_count:
            raise InvalidSource('message count does not match trusted expected_count')
        return messages

    @staticmethod
    def _parse_plan(value):
        if not isinstance(value, dict):
            raise InvalidSource('plan must be a JSON object')
        if set(value) - _PLAN_FIELDS or not _REQUIRED_PLAN_FIELDS.issubset(value):
            raise InvalidSource('plan fields are invalid')
        fields = dict(value)
        fields.setdefault('sticker_intent', None)
        try:
            return ConversationPlan(**fields)
        except (TypeError, InvalidSource):
            raise InvalidSource('plan fields are invalid') from None

    @staticmethod
    def _check_plan_messages(plan, messages):
        if plan is not None and plan.action in ('wait', 'ignore') and messages:
            raise InvalidSource('wait/ignore plans require empty messages')

    @staticmethod
    def _continuation_messages(payload, value, plan, now):
        action = payload.get('action')
        if not isinstance(action, str) or action not in _CONTINUATION_PLANS:
            raise InvalidSource('continuation action is invalid')
        if plan is not None and plan.action != _CONTINUATION_PLANS[action]:
            raise InvalidSource('continuation action conflicts with nested plan')
        if action in ('wait', 'end'):
            messages = LegacyConversationAdapter._messages(value, allow_empty=True)
            if messages:
                raise InvalidSource('wait/end continuation must have empty messages')
            if action == 'end':
                if payload.get('next_check_at') is not None:
                    raise InvalidSource('end continuation must have empty next_check_at')
            else:
                LegacyConversationAdapter._validate_next_check(payload, now)
            return messages

        messages = LegacyConversationAdapter._messages(value)
        LegacyConversationAdapter._validate_next_check(payload, now)
        return messages

    @staticmethod
    def _validate_next_check(payload, now):
        value = payload.get('next_check_at')
        if not isinstance(value, str) or not value.strip():
            raise InvalidSource('next_check_at is required')
        if now is None:
            raise Unavailable('trusted now is required for continuation scheduling')
        try:
            target = datetime.fromisoformat(value.replace('Z', '+00:00'))
            current = datetime.fromisoformat(now.replace('Z', '+00:00')) if isinstance(now, str) else now
        except (TypeError, ValueError):
            raise InvalidSource('continuation times must be ISO8601') from None
        if not isinstance(current, datetime) or target.tzinfo is None or target.utcoffset() is None:
            raise InvalidSource('continuation times require timezone offsets')
        if current.tzinfo is None or current.utcoffset() is None:
            raise InvalidSource('trusted now requires a timezone offset')
        if target < current + timedelta(seconds=30) or target > current + timedelta(hours=24):
            raise InvalidSource('next_check_at is outside the allowed window')
