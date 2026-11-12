"""Turn validated model output into a send plan without sending it."""
from persona_store import Unavailable
from persona_types import ConversationPlan, InvalidSource, RuntimeSnapshot, SendPlan
from conversation_planner import ParsedConversation


_CONTINUATION_PLAN = {
    'wait': 'wait',
    'end': 'ignore',
    'supplement': 'reply',
    'change_topic': 'reply',
}


class BubblePlanner:
    def plan(self, model_output, conversation_plan, settings, runtime_snapshot):
        if not isinstance(runtime_snapshot, RuntimeSnapshot):
            raise Unavailable('trusted runtime snapshot is required')
        if not isinstance(settings, dict):
            raise InvalidSource('settings must be a dict')
        if conversation_plan is not None and not isinstance(conversation_plan, ConversationPlan):
            raise InvalidSource('conversation_plan must be a typed ConversationPlan or None')

        messages, payload, parsed_plan, parsed_mode = self._output(model_output)
        if parsed_plan is not None:
            if conversation_plan is not None and conversation_plan != parsed_plan:
                raise InvalidSource('conversation plans disagree')
            conversation_plan = conversation_plan or parsed_plan
        self._validate_mode(parsed_mode, runtime_snapshot.mode)

        legacy_action = payload.get('action')
        if runtime_snapshot.mode == 'continuation' and legacy_action is not None:
            if not isinstance(legacy_action, str) or legacy_action not in _CONTINUATION_PLAN:
                raise InvalidSource('continuation action is invalid')
            if conversation_plan is not None and conversation_plan.action != _CONTINUATION_PLAN[legacy_action]:
                raise InvalidSource('continuation action conflicts with conversation plan')
            if legacy_action in ('wait', 'end') and messages:
                raise InvalidSource('wait/end continuation must have empty messages')
            if legacy_action in ('supplement', 'change_topic') and not messages:
                raise InvalidSource('supplement/change_topic requires messages')

        if conversation_plan is not None and conversation_plan.action in ('wait', 'ignore') and messages:
            raise InvalidSource('wait/ignore plan cannot produce sendable messages')
        messages = self._normalize_messages(messages, runtime_snapshot.mode)
        self._validate_limits(messages, conversation_plan, legacy_action, settings, runtime_snapshot.mode)
        self._validate_image_stickers(messages)

        sticker_intent = (conversation_plan.sticker_intent if conversation_plan is not None
                          else None)
        if sticker_intent is None:
            sticker_intent = payload.get('sticker_intent')
        if sticker_intent is not None and not isinstance(sticker_intent, str):
            raise InvalidSource('sticker_intent must be a string or None')
        delay_hint = payload.get('delay_hint')
        confidence = payload.get('confidence', 0.0)
        return SendPlan(runtime_snapshot=runtime_snapshot, bubbles=messages,
            sticker_intent=sticker_intent, delay_hint=delay_hint, confidence=confidence)

    @staticmethod
    def _output(model_output):
        if isinstance(model_output, ParsedConversation):
            return model_output.messages, model_output.legacy_payload, model_output.plan, model_output.mode
        if isinstance(model_output, dict):
            if 'plan' in model_output:
                raise InvalidSource('raw model plan must be parsed by ConversationPlanner')
            payload = dict(model_output)
            messages = payload.get('messages')
            if messages is None and isinstance(payload.get('reply'), str):
                messages = [payload['reply']]
            return messages, payload, None, None
        if isinstance(model_output, (list, tuple)):
            return model_output, {}, None, None
        if isinstance(model_output, str):
            return [model_output], {}, None, None
        raise InvalidSource('model_output must be parsed conversation data')

    @staticmethod
    def _validate_mode(parsed_mode, runtime_mode):
        if parsed_mode is None:
            return
        expected = {'active': {'proactive'}, 'reply': {'reply'}}.get(
            parsed_mode, {parsed_mode})
        if runtime_mode not in expected:
            raise InvalidSource('parsed mode differs from trusted runtime mode')

    @staticmethod
    def _normalize_messages(messages, mode):
        if not isinstance(messages, (list, tuple)):
            raise InvalidSource('messages must be a sequence')
        normalized = []
        for message in messages:
            if not isinstance(message, str) or not message.strip():
                raise InvalidSource('messages must contain nonempty strings')
            value = ' '.join(message.split()) if mode in ('proactive', 'care') else message.strip()
            normalized.append(value)
        if len(set(normalized)) != len(normalized):
            raise InvalidSource('messages contain duplicate bubbles')
        return tuple(normalized)

    @staticmethod
    def _validate_limits(messages, plan, legacy_action, settings, mode):
        if mode in ('proactive', 'care'):
            expected = settings.get('expected_count')
            if expected is None:
                raise Unavailable('trusted expected_count is required')
            if type(expected) is not int or expected < 0:
                raise InvalidSource('expected_count must be a nonnegative integer')
            if mode == 'care' and not messages:
                return
            if len(messages) != expected:
                raise InvalidSource('message count does not match trusted expected_count')
            return

        no_send = (plan is not None and plan.action in ('wait', 'ignore')) or (
            mode == 'continuation' and legacy_action in ('wait', 'end'))
        if not messages:
            if no_send or mode == 'care':
                return
            raise InvalidSource('messages must not be empty')
        if len(messages) > 4:
            raise InvalidSource('ordinary response exceeds four bubbles')

    @staticmethod
    def _validate_image_stickers(messages):
        marker_prefix = '[[LOCAL_STICKER:'
        found = 0
        for message in messages:
            if marker_prefix not in message:
                continue
            if not _is_local_sticker(message):
                raise InvalidSource('image sticker marker must be a standalone valid bubble')
            found += 1
        if found > 1:
            raise InvalidSource('at most one image sticker is allowed per plan')


def _is_local_sticker(value):
    if not value.startswith('[[LOCAL_STICKER:') or not value.endswith(']]'):
        return False
    key = value[len('[[LOCAL_STICKER:'):-2]
    return len(key) == 64 and all(char in '0123456789abcdef' for char in key)
