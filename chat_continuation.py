"""Model decisions about continuing an unanswered ordinary conversation."""
import json
from datetime import datetime, timedelta


def decide(store, profile, directory, now, generate):
    plan = json.loads(store.get('chat_continuation', '{}'))
    if plan.get('state') not in ('pending', 'waiting', 'ended'):
        return None
    if plan.get('generation') != store.get('inbound_generation', '0'):
        plan['state'] = 'cancelled'
        store.set('chat_continuation', json.dumps(plan, ensure_ascii=False))
        return None
    # 2026-10-13 user: how many follow-ups we may send while the friend stays silent (counted per reply of ours, set to
    # 0 on a new plan); used up -> the plan ends without another model call
    sent, limit = int(plan.get('sent_count', 0)), int(profile.get('continuation_max', 1))
    if sent >= limit:
        if plan.get('state') != 'ended':
            plan.update(state='ended', reason=f'Continuation reached {sent} rounds, reached max limit {limit}')
            store.set('chat_continuation', json.dumps(plan, ensure_ascii=False))
        return None
    forced_due = (not plan.get('forced_check_done', True) and plan.get('forced_check_at')
                  and now >= datetime.fromisoformat(plan['forced_check_at']))
    if not forced_due and (plan.get('state') == 'ended' or now < datetime.fromisoformat(plan['next_check_at'])):
        return None
    if (not profile.get('bridge_enabled', True) or not profile.get('auto_reply', True)
            or store.get('paused_reason')):
        return None
    from main import conversation_context, _runtime_context_for_mode, record_prompt, EMOTION_PROMPT
    from prompt_context import recent_rounds
    # one tag line per constraint under  []  section titles. 2026-10-10 user (): the check-time range,
    # the bubble cap and "no messages on wait/end" are applied below instead of being spelled out; the search tool is
    # off for this task (app_server), and the proactive settings are no longer injected (unused here)
    prompt = '\n'.join((
        ' [CONTINUATION] ',
        'GOAL: WE_REPLIED FRIEND_SILENT -> CHOOSE wait|end|supplement|change_topic',
        'EVIDENCE: PERSONA RELATIONSHIP_STAGE TIMED_CHAT ROUTINES; ALLOW_UNSURE; WAIT_TIME_NOT_EMOTION_OR_STATUS',
        'INITIATIVE: DEFAULT_NOT_IDLE; SOMETHING_TO_ADD -> supplement|change_topic; TOPIC_DONE|SHORT_GAP|NO_REPLY_NOT_END',
        'CONTINUITY: BUILD_ON_WHAT_WE_SAID -> NEW_POINT|ONE_QUESTION|NEW_TOPIC',
        'RESPECT_STOP: EXPLICIT_NO_REPLY|DONT_DISTURB|END_CHAT; GOODNIGHT_OR_BUSY_BY_MEANING_NOT_KEYWORD',
        ' [OUTPUT] ',
        'ACTIONS: wait=SEND_NOTHING; end=STOP_THIS_FOLLOWUP(NO_GOODBYE); supplement|change_topic=SHORT_ZH_BUBBLES',
        'NEXT_CHECK: next_check_at=ISO_WITH_TZ WHEN_TO_LOOK_AGAIN; end -> null; FRESH_JUDGMENT_EACH_TIME',
        'OUTPUT_FORMAT: JSON {"action":"...","assessment":"不确定|尚未来得及回|暂时无法回|可能不想聊",'
        '"reason":"SHORT","messages":[],"next_check_at":"ISO|null"}',
        ' [DATA] ',
        'DATA_NOW: ' + now.isoformat(),
        'DATA_TIMED_MESSAGES: ' + json.dumps(recent_rounds(store), ensure_ascii=False),
        # memory: the core blocks and the recalled entries above, as in a reply (no longer the whole memory text)
        # 6stage (AstrBot): how many of our unsolicited rounds in a row went unanswered
        'DATA_UNANSWERED_ROUNDS: ' + store.get('active_unanswered_rounds', '0'),
        f'DATA_FOLLOWUPS_LEFT: {limit - sent}',
        f'DATA_FOLLOWUP_NUMBER: {sent + 1}',
        f'DATA_FOLLOWUP_LIMIT: {limit}',
        'DATA_PREVIOUS_DECISION: ' + json.dumps({key: plan.get(key) for key in ('action', 'decided_at', 'next_check_at')},
                                               ensure_ascii=False)))
    from main import emoji_line, ignored_mood_line, tail_line
    # 2026-10-13 user (): a follow-up is sent too, so it gets the same emoji rules as a reply
    prompt += '\n' + '\n'.join(filter(None, (emoji_line(profile), tail_line(store), ignored_mood_line(store, profile))))
    if profile.get('emotion_policy', '').strip():
        prompt += EMOTION_PROMPT
    if forced_due:
        prompt += '\nFORCED_CHECK: 3_MIN_AFTER_REPLY; SENDING_OPTIONAL; RECHECK_EVEN_IF_ENDED; END_ALLOWED'
    legacy_context = conversation_context(store, profile, include_recent=False)
    context = _runtime_context_for_mode(store, profile, directory, legacy_context)
    prompt = record_prompt(store, '普通聊天续话判断', context, prompt)
    plan['state'] = 'processing'
    if forced_due:
        plan['forced_check_done'] = True
    store.set('chat_continuation', json.dumps(plan, ensure_ascii=False))
    try:
        response, _ = generate(prompt, directory, ephemeral=True, text_only=True, profile=profile)
        result = json.loads(response)
        action = result.get('action')
        messages = result.get('messages')
        if (action not in ('wait', 'end', 'supplement', 'change_topic')
                or not isinstance(result.get('assessment'), str) or not isinstance(result.get('reason'), str)
                or not isinstance(messages, list) or any(not isinstance(m, str) for m in messages)):
            raise ValueError('普通聊天续话判断格式无效')
        # 2026-10-10 user (): wait/end send nothing, at most 4 bubbles (no stickers offered here), and
        # the next check is clamped to 30 s .. 24 h (missing or unreadable -> 30 min) instead of failing the decision
        from main import tidy_bubbles
        messages = [] if action in ('wait', 'end') else tidy_bubbles(store, messages, {})
        if action in ('supplement', 'change_topic') and not messages:
            action = 'wait'
        result.update(action=action, messages=messages)
        if action == 'end':
            result['next_check_at'] = None
        else:
            try:
                at = datetime.fromisoformat(result['next_check_at'])
                at = at if at.tzinfo else at.replace(tzinfo=now.tzinfo)
            except (TypeError, ValueError):
                at = now + timedelta(minutes=30)
            at = min(max(at, now + timedelta(seconds=30)), now + timedelta(days=1))
            result['next_check_at'] = at.isoformat()
            if forced_due and action == 'wait' and plan.get('next_check_at') and now < datetime.fromisoformat(plan['next_check_at']):
                result['next_check_at'] = plan['next_check_at']
        result.update(generation=plan['generation'], decided_at=now.isoformat(), sent_count=sent,
                      forced_check_at=plan.get('forced_check_at'),
                      forced_check_done=plan.get('forced_check_done', True),
                      state='ended' if action == 'end' else 'waiting')
        # Transport commits this decision after checking for new incoming messages.
        return result
    except Exception:
        plan['state'] = 'failed'
        store.set('chat_continuation', json.dumps(plan, ensure_ascii=False))
        raise
