"""Literal friend-configured QQ notice, sent at most once until a normal reply recovers."""
import hashlib
import json

from agent_errors import AgentError, AgentErrorType, classify_error
from send_validator import ReplyPlan

KEY = 'quota_reply_notice'


def exhausted(error):
    normalized = error if isinstance(error, AgentError) else classify_error('configured', exc=error)
    return normalized.error_type == AgentErrorType.QUOTA_EXHAUSTED


def send_notice(bridge, record, profile, store, message_id, snapshot):
    text = (profile.get('model_api') or {}).get('quota_reply', '')
    if not isinstance(text, str) or not text.strip():
        return False
    text = text.strip()
    if len(text) > 1200:
        raise ValueError('额度提示最多 1200 字，未发送')
    notice = {'state': 'sending', 'input_id': str(message_id),
              'text_hash': hashlib.sha256(text.encode('utf-8')).hexdigest()}
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        row = store.db.execute('SELECT value FROM metadata WHERE key=?', (KEY,)).fetchone()
        previous = json.loads(row['value']) if row and row['value'] else {}
        if previous.get('state') in ('sending', 'sent', 'uncertain'):
            return False
        store.db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (KEY, json.dumps(notice)))
    try:
        receipt = bridge.send_text(record, store, text, reply_id=message_id,
            reply_plan=ReplyPlan(snapshot, (text,)), voice_decision=False)
        if not receipt:
            raise RuntimeError('额度提示没有发送回执，不自动重发')
        notice.update(state='sent', receipt=str(receipt))
        store.set(KEY, json.dumps(notice))
        store.mark_handled(message_id)
        return True
    except Exception:
        attempt = store.reply_attempt(message_id) or {}
        notice['state'] = 'uncertain' if attempt.get('state') in ('sending', 'sent', 'uncertain') else 'blocked'
        store.set(KEY, json.dumps(notice))
        raise


def recovered(store):
    with store.db:
        store.db.execute('DELETE FROM metadata WHERE key=?', (KEY,))
