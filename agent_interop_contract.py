"""Portable, provider-neutral task handoff envelope over the existing agent mailbox.

Handoffs are untrusted requests, NOT verified execution records or permission grants.
No filesystem path is read here; artifacts are only workspace-relative references.
"""
from __future__ import annotations

import json
import re
from typing import Any

from agent_messaging import AgentMailbox, MessagingError, _clean_artifacts

_HANDOFF_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,100}$')


def make_handoff(task_id: str, objective: str, acceptance_criteria: list[str], *,
                 context: str = '', artifacts: list[str] | None = None,
                 limits: dict[str, int] | None = None) -> dict[str, Any]:
    """Validate a common handoff contract usable by MCP, HTTP and custom clients."""
    if not isinstance(task_id, str) or not _HANDOFF_ID.fullmatch(task_id):
        raise MessagingError('Invalid handoff task_id')
    if not isinstance(objective, str) or not (1 <= len(objective.strip()) <= 8000):
        raise MessagingError('Handoff objective must be 1-8000 characters')
    if not isinstance(acceptance_criteria, list) or not (1 <= len(acceptance_criteria) <= 20):
        raise MessagingError('Handoff requires 1-20 acceptance criteria')
    if any(not isinstance(item, str) or not (1 <= len(item.strip()) <= 600)
           for item in acceptance_criteria):
        raise MessagingError('Invalid acceptance criterion')
    if not isinstance(context, str) or len(context) > 12000:
        raise MessagingError('Handoff context exceeds 12000 characters')
    if limits is None:
        limits = {}
    if (not isinstance(limits, dict) or set(limits) - {'max_tokens', 'timeout_seconds'} or
            any(type(v) is not int or not (0 < v <= 1_000_000) for v in limits.values())):
        raise MessagingError('Invalid handoff limits')
    return {'schema': 'openqqdot.agent-handoff.v1', 'task_id': task_id,
            'objective': objective.strip(), 'acceptance_criteria': [c.strip() for c in acceptance_criteria],
            'context': context, 'artifacts': _clean_artifacts(artifacts), 'limits': limits,
            'verification': 'required_by_receiver', 'status': 'unverified_request'}


def send_handoff(mailbox: AgentMailbox, sender: str, recipients: list[str], *, task_id: str,
                 objective: str, acceptance_criteria: list[str], context: str = '',
                 artifacts: list[str] | None = None, limits: dict[str, int] | None = None,
                 thread_id: str = '', idempotency_key: str = '') -> dict[str, str]:
    payload = make_handoff(task_id, objective, acceptance_criteria, context=context,
                           artifacts=artifacts, limits=limits)
    sent = mailbox.send(sender, recipients, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        subject=f'Handoff: {task_id}', kind='handoff',
                        thread_id=thread_id, artifacts=payload['artifacts'],
                        idempotency_key=idempotency_key or f'handoff:{task_id}')
    return {'message_id': sent.message_id, 'thread_id': sent.thread_id,
            'correlation_id': sent.correlation_id, 'status': 'pending_unverified'}
