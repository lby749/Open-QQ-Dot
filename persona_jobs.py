"""Per-owner persistent jobs; input provenance remains separate from ownership."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from house_settings import python_executable

from persona_store import PersonaStore, Conflict, ScopeMismatch, Unavailable
from persona_types import FriendScope, SourceRef, DataScope, AnalysisCallPolicy, AnalysisPurpose, ProviderKind
from source_state import snapshot, require_sources


def _json(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(text.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('job payload exceeds 2 MiB')
    return text


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class JobControl:
    def __init__(self, scope, *, readonly=False, clock=time.time):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('JobControl requires a verified FriendScope')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            allowed_pack_versions=scope.allowed_pack_versions)
        self.store = PersonaStore(self.scope.friend_id, self.scope.friend_dir,
                                  readonly=readonly, project_root=self.scope.project_root)
        self.db, self.clock = self.store.db, clock

    def close(self):
        self.store.close()

    @contextmanager
    def _transaction(self):
        if self.store.readonly:
            raise Unavailable('readonly job control')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def _row(self, job_id):
        row = self.db.execute('SELECT * FROM analysis_jobs WHERE friend_id=? AND id=?',
                              (self.scope.friend_id, job_id)).fetchone()
        if not row or row['owner_scope'] != 'friend:' + self.scope.friend_id:
            raise ScopeMismatch('job missing or owned by another scope')
        return dict(row)

    def read_status(self, job_id):
        row = self._row(job_id)
        for name in ('input', 'source_refs', 'checkpoint', 'attempt'):
            row[name] = json.loads(row.pop(name + '_json'))
        return row

    def list_recent(self, limit=100):
        """Return bounded status rows for this owner's local job workbench."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('job list limit must be between 1 and 500')
        rows = self.db.execute('SELECT id FROM analysis_jobs WHERE friend_id=? AND owner_scope=? '
            'ORDER BY updated_at DESC,created_at DESC,id LIMIT ?',
            (self.scope.friend_id, 'friend:' + self.scope.friend_id, limit)).fetchall()
        return tuple(self.read_status(row['id']) for row in rows)

    def _update(self, job_id, **values):
        values.update(updated_at=str(self.clock()))
        self.db.execute('UPDATE analysis_jobs SET ' + ','.join(key + '=?' for key in values) +
                        ',version=version+1 WHERE friend_id=? AND id=?',
                        (*values.values(), self.scope.friend_id, job_id))

    def _source_valid(self, row):
        if row['source_revision'] != self.store.revision or not row['source_token']:
            return False
        if row['kind'] == 'pack_media':
            return row['source_token'] == 'PUBLIC_PACK'
        try:
            require_sources(self.scope, json.loads(row['source_refs_json']))
            return row['source_token'] == snapshot(self.scope, catalog=False)
        except Conflict:
            return False

    def enqueue(self, kind, input_data, source_refs, *, model_version=None, prompt_version='job-v1'):
        if kind == 'pack_media':
            return self._enqueue_pack_media(input_data, source_refs, model_version, prompt_version)
        if kind not in ('distill', 'pack'):
            raise ValueError('unregistered job kind')
        fields = ({'pack_id', 'target_spec', 'session_ids', 'pair_ids', 'target_speaker_ids',
                   'source_partitions', 'selection_limit', 'judge_batch_size', 'use_local_model'} if kind == 'pack'
                  else {'session_ids', 'pair_ids', 'target_speaker_ids', 'friend_speaker_ids', 'use_local_model'})
        if not isinstance(input_data, dict) or set(input_data) - fields:
            raise ValueError('unsupported job input fields')
        if kind == 'pack':
            if set(input_data) != fields or not input_data.get('pair_ids') or not input_data.get('target_speaker_ids'):
                raise ValueError('pack requires the complete frozen input and selected pair/target IDs')
            pack_id, spec = input_data['pack_id'], input_data['target_spec']
            if not isinstance(pack_id, str) or not pack_id or pack_id in ('.', '..') or any(x in pack_id for x in '/\\:'):
                raise ValueError('invalid pack_id')
            if not isinstance(spec, dict) or set(spec) != {'traits', 'behavior_requirements', 'anti_patterns'}:
                raise ValueError('target_spec requires traits, behavior_requirements and anti_patterns')
            if any(not isinstance(v, (list, tuple)) or any(not isinstance(x, str) or not x for x in v) for v in spec.values()):
                raise ValueError('target_spec values must be string lists')
            if (type(input_data['selection_limit']) is not int or not 1 <= input_data['selection_limit'] <= 64
                    or type(input_data['judge_batch_size']) is not int or not 1 <= input_data['judge_batch_size'] <= 8
                    or input_data['use_local_model'] is not False):
                raise ValueError('pack limits/local model policy invalid')
            partitions = input_data['source_partitions']
            keys = {'dataset_id', 'import_id', 'record_id', 'split', 'is_holdout', 'source_hash'}
            if (not isinstance(partitions, list) or not partitions or any(not isinstance(p, dict) or set(p) != keys or
                    type(p['is_holdout']) is not bool or any(not isinstance(p[x], str) or not p[x] for x in keys - {'is_holdout'})
                    for p in partitions)):
                raise ValueError('explicit complete source partitions required')
        if not input_data.get('session_ids') or not isinstance(input_data['session_ids'], (list, tuple)):
            raise ValueError('nonempty session_ids required')
        for key in ('session_ids', 'pair_ids', 'target_speaker_ids', 'friend_speaker_ids'):
            if key == 'pair_ids' and input_data.get(key) is None:
                continue
            values = input_data.get(key, ())
            if (not isinstance(values, (list, tuple)) or len(values) > 64
                    or any(not isinstance(x, str) or not x for x in values)):
                raise ValueError('job locators must be at most 64 nonempty strings')
        from local_persona_analyzer import model_name, provider_kind, valid_model_choice
        choice = input_data.get('use_local_model', False)
        if not valid_model_choice(choice):
            raise ValueError('use_local_model must be False, True (local 9B) or "cloud"')
        model_version = model_version or ('gpt-6-luna' if kind == 'pack' else
            model_name(choice, self.scope.friend_dir) if choice else 'deterministic-local')
        if kind == 'pack' and model_version != 'gpt-6-luna':
            raise ValueError('pack Judge model is fixed to gpt-6-luna')
        if any(not isinstance(x, str) or not x for x in (model_version, prompt_version)):
            raise ValueError('model/prompt versions required')
        refs = tuple(source_refs)
        AnalysisCallPolicy.validate(AnalysisPurpose.DISTILLATION, ProviderKind(provider_kind(choice)), refs)
        if kind == 'pack':
            if any(ref.data_scope != DataScope.PUBLIC_DATASET or ref.friend_id is not None for ref in refs):
                raise ScopeMismatch('pack jobs require complete PUBLIC_DATASET provenance')
            AnalysisCallPolicy.validate(AnalysisPurpose.JUDGE, ProviderKind.CLOUD, refs)
            locators = {(p['dataset_id'], p['import_id'], p['record_id']) for p in partitions}
            if any((r.dataset_id, r.import_id, r.record_id) not in locators for r in refs):
                raise ValueError('source partition missing for provenance')
        for ref in refs:
            if ref.data_scope == DataScope.PRIVATE_FRIEND and ref.friend_id != self.scope.friend_id:
                raise ScopeMismatch('private input belongs to another friend')
            if ref.data_scope == DataScope.PUBLIC_PACK:
                self.scope.require_pack(ref.pack_id, ref.pack_version)
        refs_json = [asdict(ref) for ref in refs]
        if kind == 'pack':
            # Freeze activation CAS at enqueue, before the worker can spend tokens.
            input_data = dict(input_data)
            active_path = self.scope.project_root / 'data' / 'persona_packs' / pack_id / 'active.json'
            if active_path.resolve() != active_path:
                raise ScopeMismatch('Pack active path redirects')
            active = json.loads(active_path.read_text('utf-8')) if active_path.exists() else None
            if active is not None and (not isinstance(active, dict) or active.get('pack_id') != pack_id
                    or not isinstance(active.get('version'), str) or not active['version']):
                raise ValueError('invalid Pack active pointer')
            input_data['expected_active_version'] = active['version'] if active else None
        digest = _hash({'input': input_data, 'sources': refs_json})
        settings = _hash(input_data)
        with self._transaction():
            revision = self.store.revision
            source_token = snapshot(self.scope, catalog=False)
            require_sources(self.scope, refs_json)
            if kind == 'pack':
                reused = self._completed_pack(input_data, refs_json, model_version,
                                              prompt_version, revision, source_token)
                if reused is not None:
                    return reused
            settings = _hash([input_data, source_token])
            identity = [self.scope.friend_id, kind, digest, settings, model_version, prompt_version, revision]
            job_id, now = _hash(identity), str(self.clock())
            self.db.execute('''INSERT OR IGNORE INTO analysis_jobs
                (friend_id,id,kind,owner_scope,input_hash,input_json,source_refs_json,settings_hash,
                 model_version,prompt_version,source_revision,state,created_at,updated_at,source_token)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?)''',
                (self.scope.friend_id, job_id, kind, 'friend:' + self.scope.friend_id, digest,
                 _json(input_data), _json(refs_json), settings, model_version, prompt_version, revision, now, now, source_token))
        return job_id

    def _completed_pack(self, data, refs, model, prompt, revision, token):
        from persona_pack import _digest, _manifest
        from persona_judge import _usage

        request = {key: value for key, value in data.items() if key != 'expected_active_version'}
        rows = self.db.execute('SELECT * FROM analysis_jobs WHERE friend_id=? AND owner_scope=? '
            "AND kind='pack' AND model_version=? AND prompt_version=? AND source_refs_json=? "
            "AND json_remove(input_json,'$.expected_active_version')=? ORDER BY created_at DESC,id",
            (self.scope.friend_id, 'friend:' + self.scope.friend_id, model, prompt,
             _json(refs), _json(request)))
        completed = None
        for row in rows:
            row = dict(row)
            attempt = json.loads(row['attempt_json'])
            if row['tokens_unknown'] or attempt.get('status') in ('STARTED', 'UNCERTAIN'):
                raise Conflict('UNCERTAIN matching Pack attempt; no new paid job')
            if (row['state'] != 'completed' or row['source_revision'] != revision or
                    row['source_token'] != token):
                continue
            version = _digest({'job_id': row['id'], 'input_hash': row['input_hash']})[:32]
            directory = self.scope.project_root / 'data' / 'persona_packs' / data['pack_id'] / 'versions' / version
            manifest, _ = _manifest(directory, data['pack_id'], version)
            if (manifest.get('schema_version') not in (None, 'persona-pack-manifest-v1') or
                    manifest.get('job_id') != row['id'] or manifest.get('input_hash') != row['input_hash'] or
                    manifest.get('source_revision') != revision or not manifest.get('judge_runs') or
                    any(not isinstance(run, dict) or not isinstance(run.get('usage'), dict) or
                        run['usage'].get('status') != 'known' or not _usage(run['usage'])[1]
                        for run in manifest['judge_runs'])):
                raise Conflict('completed Pack artifact or confirmed usage differs from its job')
            if not self._source_valid(row):
                raise Conflict('completed Pack sources changed during reuse')
            completed = row['id']
        return completed

    def _enqueue_pack_media(self, input_data, source_refs, model_version, prompt_version):
        from persona_pack import _media_base, _safe_id
        fields = {'pack_id', 'base_version', 'base_manifest_sha256', 'media_mode', 'media_schema_version'}
        if not isinstance(input_data, dict) or set(input_data) != fields:
            raise ValueError('pack_media requires the exact frozen input')
        _safe_id(input_data['pack_id'], 'pack_id')
        _safe_id(input_data['base_version'], 'base_version')
        digest = input_data['base_manifest_sha256']
        if (not isinstance(digest, str) or len(digest) != 64 or
                any(c not in '0123456789abcdef' for c in digest) or
                input_data['media_mode'] != 'EMPTY_UNKNOWN' or
                input_data['media_schema_version'] != 'sticker-profile-v1'):
            raise ValueError('unsupported pack_media hash/mode/schema')
        if model_version not in (None, 'deterministic-local') or not isinstance(prompt_version, str) or not prompt_version:
            raise ValueError('pack_media is deterministic-local with an explicit prompt version')
        data = dict(input_data, expected_active_version=input_data['base_version'])
        with self._transaction():
            base = _media_base(self.scope, data)
            refs = base['manifest']['source_refs']
            supplied = [asdict(ref) for ref in source_refs]
            if supplied and sorted(_json(r) for r in supplied) != sorted(_json(r) for r in refs):
                raise ScopeMismatch('caller sources differ from verified public base')
            active = json.loads((base['path'].parent.parent / 'active.json').read_text('utf-8'))
            if (active.get('pack_id') != data['pack_id'] or active.get('version') != data['base_version'] or
                    active.get('manifest_sha256') != digest):
                raise Conflict('pack_media base is not the current active version')
            revision = self.store.revision
            input_hash, settings = _hash({'input': data, 'sources': refs}), _hash(data)
            job_id = _hash([self.scope.friend_id, 'pack_media', input_hash, settings,
                            'deterministic-local', prompt_version, revision])
            now = str(self.clock())
            self.db.execute('''INSERT OR IGNORE INTO analysis_jobs
                (friend_id,id,kind,owner_scope,input_hash,input_json,source_refs_json,settings_hash,
                 model_version,prompt_version,source_revision,state,created_at,updated_at,source_token)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?)''',
                (self.scope.friend_id, job_id, 'pack_media', 'friend:' + self.scope.friend_id,
                 input_hash, _json(data), _json(refs), settings, 'deterministic-local', prompt_version,
                 revision, now, now, 'PUBLIC_PACK'))
        return job_id

    def pause(self, job_id):
        with self._transaction():
            row = self._row(job_id)
            if row['state'] in ('queued', 'running'):
                self._update(job_id, state='paused')

    def cancel(self, job_id):
        with self._transaction():
            if self._row(job_id)['state'] != 'completed':
                self._update(job_id, state='cancelled')

    def _unknown(self, job_id, row):
        attempt = json.loads(row['attempt_json'])
        if attempt.get('status') == 'STARTED':
            attempt['status'] = 'UNCERTAIN'
            self._update(job_id, attempt_json=_json(attempt), tokens_unknown=1, error_code='UNCERTAIN',
                         state=row['state'] if row['state'] in ('paused', 'cancelled') else 'failed',
                         worker_pid=None, lease_token=None, lease_until=None)
            return True
        return attempt.get('status') == 'UNCERTAIN'

    def retry(self, job_id):
        blocked = False
        with self._transaction():
            row = self._row(job_id)
            if row['lease_token'] and row['lease_until'] > self.clock():
                raise Conflict('worker lease is still active')
            blocked = self._unknown(job_id, row)
            if not blocked:
                if row['state'] not in ('paused', 'failed'):
                    raise Conflict('only paused or failed jobs may resume')
                if not self._source_valid(row):
                    raise Conflict('job source revision is stale')
                self._update(job_id, state='queued', worker_pid=None, lease_token=None, lease_until=None, error_code=None)
        if blocked:
            raise Conflict('UNCERTAIN external attempt requires manual reconciliation; no automatic paid retry')

    def claim(self, job_id, *, worker_pid=None, lease_seconds=300):
        if not 1 <= lease_seconds <= 3600:
            raise ValueError('lease_seconds outside 1..3600')
        token = None
        with self._transaction():
            row = self._row(job_id)
            active = row['lease_token'] and row['lease_until'] > self.clock()
            if active:
                return None
            if self._unknown(job_id, row) or row['state'] not in ('queued', 'running'):
                return None
            if not self._source_valid(row):
                self._update(job_id, state='failed', error_code='STALE_SOURCE')
                return None
            token = uuid.uuid4().hex
            self._update(job_id, state='running', worker_pid=worker_pid or os.getpid(),
                         lease_token=token, lease_until=self.clock() + lease_seconds)
        return token

    def _lease(self, job_id, token):
        row = self._row(job_id)
        if row['lease_token'] != token or not token or row['lease_until'] <= self.clock():
            raise Conflict('worker lease lost')
        return row

    def renew(self, job_id, token, lease_seconds=300):
        with self._transaction():
            self._lease(job_id, token)
            self._update(job_id, lease_until=self.clock() + lease_seconds)

    def start_attempt(self, job_id, token, batch_id, batch_hash):
        """Trusted cloud adapter must call before issuing its external request."""
        with self._transaction():
            row = self._lease(job_id, token)
            if row['state'] != 'running' or not self._source_valid(row):
                raise Conflict('job paused/cancelled/stale before cloud attempt')
            refs = tuple(SourceRef(**ref) for ref in json.loads(row['source_refs_json']))
            AnalysisCallPolicy.validate(AnalysisPurpose.DISTILLATION, ProviderKind.CLOUD, refs)
            checkpoint = json.loads(row['checkpoint_json'])
            if batch_id in checkpoint:
                raise Conflict('batch already checkpointed; do not call again')
            if json.loads(row['attempt_json']).get('status') in ('STARTED', 'UNCERTAIN'):
                raise Conflict('external attempt already started or unknown')
            self._update(job_id, attempt_json=_json({'batch_id': batch_id, 'hash': batch_hash, 'status': 'STARTED'}),
                         tokens_unknown=1)

    def not_sent(self, job_id, token):
        """Only an adapter with positive evidence of no dispatch may clear STARTED."""
        with self._transaction():
            row = self._lease(job_id, token)
            attempt = json.loads(row['attempt_json'])
            if attempt.get('status') != 'STARTED':
                raise Conflict('no started attempt')
            attempt['status'] = 'NOT_SENT'
            self._update(job_id, attempt_json=_json(attempt), tokens_unknown=0)

    def checkpoint(self, job_id, token, batch_id, batch_hash, result):
        with self._transaction():
            row = self._lease(job_id, token)
            if not self._source_valid(row):
                raise Conflict('job source revision is stale')
            checkpoint = json.loads(row['checkpoint_json'])
            value = json.loads(_json({'hash': batch_hash, 'result': result}))
            if batch_id in checkpoint:
                if checkpoint[batch_id] != value:
                    raise Conflict('checkpoint replay differs')
                return
            attempt = json.loads(row['attempt_json'])
            if attempt.get('status') == 'UNCERTAIN':
                raise Conflict('unknown external attempt cannot checkpoint a new result')
            if attempt.get('status') == 'STARTED':
                if (attempt['batch_id'], attempt['hash']) != (batch_id, batch_hash):
                    raise Conflict('checkpoint does not match external attempt')
                attempt['status'] = 'SUCCEEDED'
            checkpoint[batch_id] = value
            # Pause/cancel requested during this batch remain authoritative.
            self._update(job_id, checkpoint_json=_json(checkpoint), attempt_json=_json(attempt), tokens_unknown=0)

    def finish(self, job_id, token, *, error_code=None):
        with self._transaction():
            row = self._lease(job_id, token)
            if self._unknown(job_id, row):
                return
            state = row['state']
            if state == 'running':
                error_code = ('STALE_SOURCE' if not self._source_valid(row) else error_code)
                state = 'failed' if error_code else 'completed'
            self._update(job_id, state=state, error_code=error_code,
                         worker_pid=None, lease_token=None, lease_until=None)

    def spawn_worker(self, job_id):
        row = self._row(job_id)
        if row['state'] != 'queued':
            raise Conflict('worker launch requires queued job')
        args = [python_executable(self.scope.project_root), str(Path(__file__).with_name('persona_worker.py')), '--project-root',
                str(self.scope.project_root), '--friend-id', self.scope.friend_id, '--job-id', job_id]
        for pack in sorted(self.scope.allowed_pack_versions, key=lambda x: (x.pack_id, x.version)):
            args += ['--allowed-pack-version', pack.pack_id, pack.version]
        return subprocess.Popen(args, cwd=self.scope.project_root,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0).pid
