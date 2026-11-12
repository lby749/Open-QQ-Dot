"""Small, provenance-checked cloud judging for explicitly public DialoguePairs."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from dialogue_pairs import DialoguePair
from persona_store import ScopeMismatch
from persona_types import (AnalysisCallPolicy, AnalysisPurpose, DataScope, FriendScope,
                           InvalidSource, PrivacyViolation, ProviderKind, SourceOrigin)


PERSONA_FIELDS = ('lively', 'warm', 'cute', 'playful', 'natural_chat',
                  'relationship_fit', 'over_formal', 'ai_like', 'overall_persona_match')
PROMPT_VERSION = 'CURATION-002-v1'
MAX_BATCH = 8


class JudgeRunStore:
    """Atomic run reservation and completion in the verified owner's sidecar."""

    def __init__(self, scope, persona_store):
        if (not isinstance(scope, FriendScope) or persona_store.friend_id != scope.friend_id or
                persona_store.directory != scope.friend_dir):
            raise ScopeMismatch('JudgeRunStore requires the verified owner sidecar')
        self.scope, self.store = scope, persona_store

    def begin_run(self, record):
        db = self.store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            current = db.execute('SELECT revision FROM scope_state WHERE friend_id=?',
                                 (self.scope.friend_id,)).fetchone()[0]
            if current != record['source_revision']:
                db.execute('ROLLBACK')
                return False
            db.execute('INSERT INTO dataset_curation_runs '
                '(friend_id,id,status,source_revision,input_json,settings_json,model,'
                'prompt_version,prompt_hash,results_json,usage_json,error_code,started_at,completed_at) '
                'VALUES(?,? ,\'RUNNING\',?,?,?,?,?,?,\'[]\',?,?,?,NULL)',
                (self.scope.friend_id, record['id'], current, record['input_json'],
                 record['settings_json'], record['model'], record['prompt_version'],
                 record['prompt_hash'], record['usage_json'], None, record['started_at']))
            db.execute('COMMIT')
            return True
        except sqlite3.IntegrityError:
            db.execute('ROLLBACK')
            return False
        except Exception:
            db.execute('ROLLBACK')
            raise

    def finish_run(self, run_id, record):
        db = self.store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            current = db.execute('SELECT revision FROM scope_state WHERE friend_id=?',
                                 (self.scope.friend_id,)).fetchone()[0]
            status, error = record['status'], record['error_code']
            if current != record['source_revision']:
                status, error = 'FAILED', 'source_revision_changed'
            db.execute('UPDATE dataset_curation_runs SET status=?,model=?,results_json=?,'
                'usage_json=?,error_code=?,completed_at=? WHERE friend_id=? AND id=? AND status=\'RUNNING\'',
                (status, record['model'], record['results_json'], record['usage_json'], error,
                 record['completed_at'], self.scope.friend_id, run_id))
            if db.execute('SELECT changes()').fetchone()[0] != 1:
                raise InvalidSource('claimed curation run is no longer RUNNING')
            db.execute('COMMIT')
            return status, error
        except Exception:
            db.execute('ROLLBACK')
            raise


class PersonaJudge:
    def __init__(self, scope, run_store, call, *, model, batch_size=4):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('PersonaJudge requires a verified owner scope')
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH:
            raise ValueError('batch_size must be between 1 and 8')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        if getattr(run_store, 'scope', None) != self.scope:
            raise ScopeMismatch('run store and Judge must share the verified owner scope')
        if not callable(call) or not isinstance(model, str) or not model.strip():
            raise InvalidSource('injected cloud call and model name are required')
        self.run_store, self.call, self.model = run_store, call, model
        self.batch_size = batch_size

    def judge(self, pairs, *, run_id, source_revision, target_spec):
        if not isinstance(run_id, str) or not run_id.strip():
            raise InvalidSource('run_id is required')
        if type(source_revision) is not int or source_revision < 0:
            raise InvalidSource('source_revision must be a nonnegative integer')
        if not isinstance(target_spec, dict) or not target_spec:
            raise InvalidSource('target_spec must be a nonempty object')
        values = tuple(pairs)
        if not values or len(values) > self.batch_size or any(not isinstance(p, DialoguePair) for p in values):
            raise InvalidSource('judge accepts one bounded batch of DialoguePairs')
        if len({p.id for p in values}) != len(values):
            raise InvalidSource('duplicate pair id in batch')
        refs = tuple(dict.fromkeys(ref for pair in values for ref in pair.source_refs))
        if any(ref.data_scope == DataScope.PRIVATE_FRIEND for ref in refs):
            raise PrivacyViolation('private sources are never eligible for cloud judging')
        if any(ref.data_scope != DataScope.PUBLIC_DATASET for ref in refs):
            raise PrivacyViolation('this friend-sidecar Judge accepts PUBLIC_DATASET only')
        if any(pair.friend_id != self.scope.friend_id for pair in values):
            raise ScopeMismatch('pair owner differs from verified sidecar owner')
        pair_messages = [message for pair in values for message in (
            *pair.user_bubbles, *pair.target_bubbles,
            *(message for turn in pair.context_turns for message in turn.bubbles))]
        if any(message.source not in refs for message in pair_messages):
            raise InvalidSource('Pair contains a bubble absent from its provenance refs')
        if any(not pair.target_bubbles or any(
                message.speaker_id != pair.target_speaker_id or
                message.speaker_role.strip().lower() != 'human' or
                message.source not in pair.source_refs or
                message.source.origin not in (SourceOrigin.PUBLIC_DATASET,
                    SourceOrigin.REAL_SINGLE_SPEAKER, SourceOrigin.REAL_PERSONA_POOL)
                for message in pair.target_bubbles) for pair in values):
            raise InvalidSource('Pair lacks explicitly mapped public human Target evidence')
        AnalysisCallPolicy.validate(AnalysisPurpose.JUDGE, ProviderKind.CLOUD, refs)

        prompt = _prompt(values, target_spec)
        timestamp = datetime.now(timezone.utc).isoformat()
        source_rows = [_source_row(ref) for ref in refs]
        input_json = json.dumps({'pairs': [_pair_record(p) for p in values], 'sources': source_rows},
                                ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        settings_json = json.dumps({'target_spec': target_spec, 'batch_size': self.batch_size},
                                   ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        record = {'id': run_id, 'source_revision': source_revision, 'input_json': input_json,
            'settings_json': settings_json, 'model': self.model, 'prompt_version': PROMPT_VERSION,
            'prompt_hash': prompt_hash, 'usage_json': '{"status":"unknown"}',
            'started_at': timestamp}
        if not self.run_store.begin_run(record):
            raise InvalidSource('run already claimed or source revision changed; no call made')
        usage, usage_known, model = {'status': 'unknown'}, False, self.model
        try:
            response = self.call(prompt)
            if not isinstance(response, dict) or not isinstance(response.get('content'), str):
                raise ValueError('injected call must return content, model, and usage')
            usage, usage_known = _usage(response.get('usage'))
            model = response.get('model')
            if not isinstance(model, str) or not model.strip():
                raise ValueError('model identity missing')
            if model != self.model:
                raise ValueError('response model differs from requested model')
            results = _parse(response['content'], {p.id for p in values})
            status = 'COMPLETED' if usage_known else 'USAGE_UNKNOWN'
            error = None if usage_known else 'usage_unknown'
        except Exception as exc:
            results = []
            if not usage_known:
                usage, status = {'status': 'unknown'}, 'USAGE_UNKNOWN'
            else:
                status = 'FAILED'
            error = type(exc).__name__
        completed = datetime.now(timezone.utc).isoformat()
        status, finish_error = self.run_store.finish_run(run_id, {
            'status': status, 'source_revision': source_revision,
            'model': model if 'model' in locals() and isinstance(model, str) else self.model,
            'results_json': json.dumps(results, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')),
            'usage_json': json.dumps(usage, sort_keys=True, separators=(',', ':')),
            'error_code': error, 'completed_at': completed})
        if finish_error:
            error = finish_error
        return {'run_id': run_id, 'status': status, 'results': results,
                'model': model if 'model' in locals() and isinstance(model, str) else self.model,
                'prompt_version': PROMPT_VERSION, 'prompt_hash': prompt_hash, 'usage': usage,
                'error_code': error}


def _source_row(ref):
    return {'dataset_id': ref.dataset_id, 'import_id': ref.import_id,
        'record_id': ref.record_id, 'origin': ref.origin.value if ref.origin else None,
        'data_scope': ref.data_scope.value, 'license_id': ref.license_id,
        'model_analysis_allowed': ref.model_analysis_allowed}


def _pair_input(pair):
    return {'pair_id': pair.id, 'scene_evidence': [t for turn in pair.context_turns
        for t in (m.text for m in turn.bubbles)],
        'user': [m.text for m in pair.user_bubbles],
        'target': [m.text for m in pair.target_bubbles]}


def _pair_record(pair):
    return {'pair_id': pair.id, 'session_id': pair.session_id,
        'input_turn_ids': list(pair.input_turn_ids), 'context_turn_ids': list(pair.context_turn_ids),
        'target_turn_id': pair.target_turn_id,
        'input_bubble_ids': [message.id for message in pair.user_bubbles],
        'target_bubble_ids': [message.id for message in pair.target_bubbles]}


def _prompt(pairs, target_spec):
    data = [_pair_input(pair) for pair in pairs]
    return ('Score public human-target dialogue examples for BOT_PERSONA only. Do not infer '
        'gender, identity, biography, or facts. Use numeric scores from 0 to 1. Return JSON '
        'exactly as {"items":[{"pair_id":...,"scores":{all score fields},'
        '"quality_score":0..1,"needs_review":boolean,"abstain_reason":string|null}]} . '
        'Persona score fields: ' + ','.join(PERSONA_FIELDS) +
        '; separate top-level quality_score measures text-example usability. '
        'quality_score measures text-example usability. Target specification: ' +
        json.dumps(target_spec, ensure_ascii=False, sort_keys=True) +
        ' Data: ' + json.dumps(data, ensure_ascii=False, separators=(',', ':')))


def _parse(content, expected_ids):
    value = json.loads(content)
    if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value['items'], list):
        raise ValueError('judge output must contain items')
    results, seen = [], set()
    for item in value['items']:
        if not isinstance(item, dict) or set(item) != {'pair_id', 'scores', 'quality_score',
                                                       'needs_review', 'abstain_reason'}:
            raise ValueError('judge item schema is invalid')
        pair_id, scores = item['pair_id'], item['scores']
        if pair_id not in expected_ids or pair_id in seen or not isinstance(scores, dict) or set(scores) != set(PERSONA_FIELDS):
            raise ValueError('judge ids or score fields are invalid')
        for score in (*scores.values(), item['quality_score']):
            if type(score) not in (float, int) or not 0 <= score <= 1:
                raise ValueError('scores must be numeric values in [0,1]')
        if type(item['needs_review']) is not bool or (item['abstain_reason'] is not None and
                not isinstance(item['abstain_reason'], str)):
            raise ValueError('judge review fields are invalid')
        seen.add(pair_id)
        results.append(item)
    if seen != expected_ids:
        raise ValueError('judge output omitted one or more pairs')
    return results


def _usage(value):
    if not isinstance(value, dict) or any(type(value.get(key)) is not int or value[key] < 0
            for key in ('input_tokens', 'output_tokens', 'total_tokens')):
        return {'status': 'unknown'}, False
    if value['total_tokens'] != value['input_tokens'] + value['output_tokens']:
        return {'status': 'unknown'}, False
    return {'status': 'known', **{key: value[key] for key in
        ('input_tokens', 'output_tokens', 'total_tokens')}}, True
