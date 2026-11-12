"""Pure, reply-only sending gates; complete mode/receipt state belongs to SEND-002."""
from dataclasses import dataclass
import re
import hashlib
import json
from datetime import datetime
from pathlib import Path
from persona_types import RuntimeSnapshot, SendPlan, SendStateSnapshot


def profile_revision(profile):
    canonical = json.dumps(dict(profile), sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False)
    return int.from_bytes(hashlib.sha256(canonical.encode('utf-8')).digest(), 'big')


@dataclass(frozen=True)
class ReplySnapshot:
    friend_id: str
    friend_dir: str
    bound_self_id: str
    generation: int
    source_message_id: str


@dataclass(frozen=True)
class ReplyPlan:
    runtime_snapshot: ReplySnapshot
    bubbles: tuple[str, ...]


@dataclass(frozen=True)
class ReplySendState:
    friend_id: str
    friend_dir: str
    bound_self_id: str
    verified_self_id: str | None
    current_generation: int
    current_profile: dict
    attempt_state: str
    successful_bubbles: int


@dataclass(frozen=True)
class ValidationResult:
    allowed: bool
    reason: str
    status: str


@dataclass(frozen=True)
class PrivateMediaEvidence:
    friend_id: str
    friend_dir: str
    key: str
    original_file: str
    original_sha256: str
    transport_file: str
    transport_sha256: str


class SendValidator:
    @staticmethod
    def validate(plan, runtime_snapshot, send_state_snapshot, media_registry):
        if isinstance(plan, SendPlan):
            try:
                return SendValidator._validate_mode(plan, runtime_snapshot, send_state_snapshot, media_registry)
            except (TypeError, ValueError, KeyError, AttributeError):
                return ValidationResult(False, 'send_state_unavailable', 'BLOCKED')
        return SendValidator._validate(plan, runtime_snapshot, send_state_snapshot, media_registry, True)

    @staticmethod
    def validate_local(plan, runtime_snapshot, send_state_snapshot, media_registry):
        """Preflight only; an allowed result never authorizes transport submission."""
        return SendValidator._validate(plan, runtime_snapshot, send_state_snapshot, media_registry, False)

    @staticmethod
    def _validate_mode(plan, snapshot, state, media_registry):
        def reject(reason, status='BLOCKED'):
            return ValidationResult(False, reason, status)
        if not isinstance(snapshot, RuntimeSnapshot) or not isinstance(state, SendStateSnapshot):
            return reject('send_state_unavailable')
        if plan.runtime_snapshot != snapshot or snapshot.mode not in ('proactive', 'continuation', 'care'):
            return reject('snapshot_mismatch')
        if state.friend_id != snapshot.friend_id or state.verified_self_id != snapshot.bound_self_id:
            return reject('identity_mismatch')
        if state.current_profile.get('qq_id') != snapshot.friend_id:
            return reject('profile_unavailable')
        if state.current_generation != snapshot.inbound_generation:
            return reject('generation_changed', 'CANCELLED')
        try:
            if profile_revision(state.current_profile) != snapshot.profile_revision:
                return reject('profile_changed', 'CANCELLED')
        except (ValueError, TypeError):
            return reject('profile_unavailable')
        profile = state.current_profile
        if profile.get('bridge_enabled', True) is not True:
            return reject('bridge_disabled', 'CANCELLED')
        if state.attempt_state == 'uncertain' or state.attempt_state == 'sending':
            return reject('attempt_uncertain', 'UNCERTAIN')
        if state.attempt_state != 'prepared':
            return reject('attempt_not_sendable')
        receipts = state.successful_bubble_ids
        if len(set(receipts)) != len(receipts) or len(receipts) >= len(plan.bubbles):
            return reject('attempt_complete')
        if not plan.bubbles or len(set(plan.bubbles)) != len(plan.bubbles):
            return reject('invalid_bubbles')
        now = datetime.fromisoformat(state.now)
        checked = datetime.fromisoformat(state.identity_checked_at)
        if checked > now or (now - checked).total_seconds() > 30:
            return reject('identity_check_stale')
        test = snapshot.mode == 'proactive' and snapshot.attempt_id.startswith('test_slot:')
        if snapshot.mode == 'continuation':
            if profile.get('auto_reply', True) is not True or profile.get('paused_reason'):
                return reject('reply_disabled', 'CANCELLED')
            if len(plan.bubbles) > 4:
                return reject('invalid_bubbles')
        elif snapshot.mode == 'proactive':
            settings = profile.get('proactive', {})
            from scheduled_care import configuration
            # 2026-10-13 user: care is now one of the model's own todos, sent as a proactive round (agenda.py)
            if settings.get('enabled') is not True and not configuration(profile)['enabled']:
                return reject('proactive_disabled', 'CANCELLED')
            if type(state.contact_attempts_remaining) is not int or state.contact_attempts_remaining < 1:
                return reject('contact_budget_unavailable')
            if type(settings.get('bubbles_max')) is not int or len(plan.bubbles) > settings['bubbles_max']:
                return reject('invalid_bubbles')
        elif snapshot.mode == 'care':
            if state.care_slot_state != 'ready':
                return reject('care_slot_unavailable', 'CANCELLED')
            from scheduled_care import configuration
            care = configuration(profile)
            name = snapshot.attempt_id.rsplit(':', 1)[-1]
            slot = care['slots'].get(name)
            if not care['enabled'] or not slot or not slot['enabled'] \
                    or not slot['count_min'] <= len(plan.bubbles) <= slot['count']:
                return reject('care_slot_unavailable', 'CANCELLED')
        if snapshot.mode in ('proactive', 'care') and state.window_open is not True:
            return reject('window_unavailable', 'CANCELLED')
        for field in ('cooldown_until', 'reply_pause_until', 'unanswered_pause_until'):
            # Existing continuation decisions ignore reply_waiting.
            if snapshot.mode == 'continuation' and field != 'unanswered_pause_until':
                continue
            if test and field == 'unanswered_pause_until':
                continue
            value = getattr(state, field)
            if value and datetime.fromisoformat(value) > now:
                return reject('send_paused', 'CANCELLED')
        # The caller builds the same private-byte evidence as the reply path.
        if not isinstance(media_registry, dict):
            return reject('media_state_unavailable')
        for text in plan.bubbles:
            marker = re.fullmatch(r'\[\[(?:本地表情|LOCAL_STICKER):([0-9a-f]{64})\]\]', text.strip())
            if marker:
                key = marker.group(1)
                evidence = media_registry.get(key)
                if (not isinstance(evidence, PrivateMediaEvidence) or evidence.friend_id != snapshot.friend_id or
                        evidence.friend_dir != str(snapshot.friend_dir) or evidence.key != key or
                        evidence.original_sha256 != key or evidence.transport_sha256 != key):
                    return reject('media_permission_unavailable')
                original, transport = Path(evidence.original_file), Path(evidence.transport_file)
                if (not original.is_absolute() or not transport.is_absolute() or
                        original.parent != snapshot.friend_dir / 'media' or
                        original.name not in tuple(key + ext for ext in ('.png', '.gif', '.jpg', '.webp'))):
                    return reject('media_evidence_mismatch')
            elif '[[本地表情' in text or '[[LOCAL_STICKER' in text or '[[QQ收藏表情' in text:
                return reject('media_permission_unavailable')
        return ValidationResult(True, 'allowed', 'ALLOWED')

    @staticmethod
    def _validate(plan, runtime_snapshot, send_state_snapshot, media_registry, require_verified):
        def reject(reason, status='BLOCKED'):
            return ValidationResult(False, reason, status)

        if (not isinstance(plan, ReplyPlan) or not isinstance(runtime_snapshot, ReplySnapshot)
                or not isinstance(send_state_snapshot, ReplySendState)):
            return reject('reply_state_unavailable')
        snapshot, state = runtime_snapshot, send_state_snapshot
        if plan.runtime_snapshot != snapshot:
            return reject('snapshot_mismatch')
        if (not all(isinstance(value, str) and re.fullmatch(r'[0-9]{5,12}', value)
                    for value in (snapshot.friend_id, snapshot.bound_self_id, state.friend_id,
                                  state.bound_self_id)) or
                not isinstance(snapshot.friend_dir, str) or not snapshot.friend_dir or
                not isinstance(snapshot.source_message_id, str) or not snapshot.source_message_id):
            return reject('identity_unavailable')
        if (snapshot.friend_id != state.friend_id or snapshot.friend_dir != state.friend_dir or
                snapshot.bound_self_id != state.bound_self_id):
            return reject('identity_mismatch')
        if any(type(value) is not int or value < 0 for value in
               (snapshot.generation, state.current_generation, state.successful_bubbles)):
            return reject('reply_state_unavailable')
        if state.current_generation != snapshot.generation:
            return reject('generation_changed', 'CANCELLED')
        if (not isinstance(state.current_profile, dict) or
                state.current_profile.get('qq_id') != snapshot.friend_id):
            return reject('profile_unavailable')
        # Match established defaults only for these two existing switches.
        if (state.current_profile.get('bridge_enabled', True) is not True or
                state.current_profile.get('auto_reply', True) is not True):
            return reject('reply_disabled', 'CANCELLED')
        if state.attempt_state in ('sending', 'uncertain'):
            return reject('attempt_uncertain', 'UNCERTAIN')
        if state.attempt_state not in ('processing', 'sent'):
            return reject('attempt_not_sendable')
        if state.attempt_state == 'sent' and state.successful_bubbles == 0:
            return reject('attempt_already_sent')
        if (not isinstance(plan.bubbles, tuple) or not plan.bubbles or
                any(not isinstance(text, str) or not text.strip() for text in plan.bubbles) or
                len(set(plan.bubbles)) != len(plan.bubbles)):
            return reject('invalid_bubbles')
        if len(plan.bubbles) > 4:
            return reject('invalid_bubbles')
        if state.successful_bubbles >= len(plan.bubbles):
            return reject('attempt_complete', 'CANCELLED')
        # Evidence is built by the caller from private originals and actual local
        # transport bytes. Model dictionaries and remote references are not evidence.
        if not isinstance(media_registry, dict):
            return reject('media_state_unavailable')
        for text in plan.bubbles:
            marker = re.fullmatch(r'\[\[(?:本地表情|LOCAL_STICKER):([0-9a-f]{64})\]\]', text.strip())
            if marker:
                key = marker.group(1)
                evidence = media_registry.get(key)
                if not isinstance(evidence, PrivateMediaEvidence):
                    return reject('media_permission_unavailable')
                if (evidence.friend_id != snapshot.friend_id or evidence.friend_dir != snapshot.friend_dir or
                        evidence.key != key or evidence.original_sha256 != key or evidence.transport_sha256 != key or
                        not isinstance(evidence.original_file, str) or not isinstance(evidence.transport_file, str)):
                    return reject('media_evidence_mismatch')
                original, transport = Path(evidence.original_file), Path(evidence.transport_file)
                if (not original.is_absolute() or not transport.is_absolute() or
                        original.parent != Path(snapshot.friend_dir) / 'media' or
                        original.name not in tuple(key + suffix for suffix in ('.gif', '.png', '.jpg', '.webp'))):
                    return reject('media_evidence_mismatch')
            elif '[[本地表情' in text or '[[LOCAL_STICKER' in text or '[[QQ收藏表情' in text:
                return reject('media_permission_unavailable')
        if require_verified:
            if not isinstance(state.verified_self_id, str) or not re.fullmatch(r'[0-9]{5,12}', state.verified_self_id):
                return reject('identity_unavailable')
            if state.verified_self_id != snapshot.bound_self_id:
                return reject('identity_mismatch')
        return ValidationResult(True, 'allowed', 'ALLOWED')
