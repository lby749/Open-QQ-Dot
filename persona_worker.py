"""Hidden Persona job worker: no GUI, bridge, QQ link or QQ imports."""
import argparse
from dataclasses import asdict
import json
import threading
from contextlib import closing
import sqlite3

from persona_jobs import JobControl, _json
from persona_store import Conflict
from persona_types import FriendScope, PackVersion


class JobExecution:
    def __init__(self, control, job_id, token):
        self.control, self.job_id, self.token = control, job_id, token

    def cancelled(self):
        quota_path = self.control.scope.project_root / 'docs' / 'quota_state.json'
        if quota_path.is_file() and json.loads(quota_path.read_text('utf-8-sig')).get('status') in (
                'PAUSED_QUOTA', 'PAUSED_WEEKLY'):
            self.control.pause(self.job_id)
        return self.control.read_status(self.job_id)['state'] in ('paused', 'cancelled')

    is_set = cancelled

    def checkpoint(self, batch_id, batch_hash, result):
        self.control.checkpoint(self.job_id, self.token, batch_id, batch_hash, result)

    def start_attempt(self, batch_id, batch_hash):
        self.control.start_attempt(self.job_id, self.token, batch_id, batch_hash)


def _pack_judge_call(prompt, execution, *, quota_checker):
    from persona_judge import PERSONA_FIELDS
    from persona_types import DataScope, SourceRef, AnalysisCallPolicy, AnalysisPurpose, ProviderKind
    job = execution.control.read_status(execution.job_id)
    refs = tuple(SourceRef(**r) for r in job['source_refs'])
    if job['kind'] != 'pack' or any(r.data_scope != DataScope.PUBLIC_DATASET or r.friend_id is not None for r in refs):
        raise Conflict('public pack Judge input required')
    AnalysisCallPolicy.validate(AnalysisPurpose.JUDGE, ProviderKind.CLOUD, refs)
    if job['attempt'].get('status') != 'STARTED':
        raise Conflict('cloud attempt must be persisted before adapter call')
    if not quota_checker() or execution.cancelled():
        execution.control.not_sent(execution.job_id, execution.token)
        execution.control.pause(execution.job_id)
        raise Conflict('quota/cancellation stopped cloud dispatch; definitely not sent')
    directory = execution.control.scope.project_root / 'data' / 'persona_packs' / 'job_usage' / execution.job_id
    directory.mkdir(parents=True, exist_ok=True)
    usage_path = directory / 'history.sqlite3'
    if directory.resolve() != directory or usage_path.resolve() != usage_path:
        raise Conflict('job usage path redirects')
    with closing(sqlite3.connect(usage_path)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {'usage_calls'}:
            raise Conflict('job usage database must not contain friend/history data')
        before = db.execute('SELECT COALESCE(MAX(seq),0) FROM usage_calls').fetchone()[0] if tables else 0
    score = {'type': 'number', 'minimum': 0, 'maximum': 1}
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['items'], 'properties': {'items': {
        'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'type': 'object', 'additionalProperties': False,
        'required': ['pair_id', 'scores', 'quality_score', 'needs_review', 'abstain_reason'], 'properties': {
            'pair_id': {'type': 'string'}, 'scores': {'type': 'object', 'additionalProperties': False,
            'required': list(PERSONA_FIELDS), 'properties': {k: score for k in PERSONA_FIELDS}},
            'quality_score': score, 'needs_review': {'type': 'boolean'},
            'abstain_reason': {'type': ['string', 'null']}}}}}}
    schema_path = directory / 'judge-schema.json'
    schema_path.write_text(json.dumps(schema), 'utf-8')
    import main
    content, _ = main.codex_text(prompt, directory, ephemeral=True, text_only=True, output_schema=schema_path,
        profile={'model_api': {'provider': 'codex', 'codex_model': 'gpt-6-luna', 'codex_effort': 'medium', 'codex_fast': False}})
    with closing(sqlite3.connect(usage_path)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='usage_calls'").fetchone()
        rows = db.execute('SELECT model,usage FROM usage_calls WHERE seq>?', (before,)).fetchall() if exists else []
    model, usage = None, None
    if len(rows) == 1:
        model, raw = rows[0][0], json.loads(rows[0][1])
        if (isinstance(raw, dict) and all(type(raw.get(k)) is int and raw[k] >= 0 for k in ('input_tokens', 'output_tokens'))):
            total = raw['input_tokens'] + raw['output_tokens']
            if raw.get('total_tokens', total) == total:
                usage = {'input_tokens': raw['input_tokens'], 'output_tokens': raw['output_tokens'], 'total_tokens': total}
    return {'content': content, 'model': model, 'usage': usage}


def _pack(job, execution):
    from persona_pack import run_pack_job
    from dataset_annotation.quota_guard import QuotaGuard
    with QuotaGuard(execution.control.scope.project_root) as guard:
        return run_pack_job(job, execution, judge_call=lambda prompt:
                            _pack_judge_call(prompt, execution, quota_checker=guard.allowed))


def _distill(job, execution):
    from dialogue_pairs import PairStore
    from persona_distiller import PersonaDistiller
    data = job['input']
    batch_id = 'distill:' + job['input_hash']
    if batch_id in job['checkpoint']:
        return
    pairs = PairStore(execution.control.scope)
    try:
        result = PersonaDistiller(execution.control.scope, pairs).analyze(
            data['session_ids'], data.get('target_speaker_ids', ()), data.get('friend_speaker_ids', ()),
            data.get('use_local_model', False), execution, pair_ids=data.get('pair_ids'))
        actual_refs = sorted(_json(asdict(ref)) for ref in result.scene_analysis.source_refs)
        if actual_refs != sorted(_json(ref) for ref in job['source_refs']):
            raise Conflict('job provenance differs from actual analysis sources')
        summary = {'state': 'DRAFT', 'run_id': result.run_id, 'source_revision': result.source_revision,
                   'input_hash': result.input_hash, 'settings': result.settings,
                   'role_claim_drafts': [asdict(x) for x in result.role_claim_drafts],
                   'relationship_claim_drafts': [asdict(x) for x in result.relationship_claim_drafts],
                   'style_profile': result.style_profile, 'sample_status': result.sample_status,
                   'model': result.model, 'prompt_version': result.prompt_version}
        execution.checkpoint(batch_id, job['input_hash'], summary)
    finally:
        pairs.close()


def _pack_media(job, execution):
    from persona_pack import run_pack_media_job
    result = run_pack_media_job(job, execution)
    execution.checkpoint('pack-media:' + job['input_hash'], job['input_hash'], result)
    return result


def run_job(control, job_id, handler=None):
    """A trusted future PACK adapter may share this lease/checkpoint execution boundary."""
    token = control.claim(job_id)
    if token is None:
        return control.read_status(job_id)
    stop, heartbeat_error = threading.Event(), []
    def heartbeat():
        heartbeat_control = None
        try:
            heartbeat_control = JobControl(control.scope)
            while not stop.wait(30):
                try:
                    heartbeat_control.renew(job_id, token)
                except Exception as exc:
                    heartbeat_error.append(type(exc).__name__)
                    break
        except Exception as exc:
            heartbeat_error.append(type(exc).__name__)
        finally:
            if heartbeat_control:
                heartbeat_control.close()
    thread = threading.Thread(target=heartbeat, name='persona-job-lease', daemon=True)
    thread.start()
    execution = JobExecution(control, job_id, token)
    try:
        job = control.read_status(job_id)
        default_handler = {'distill': _distill, 'pack': _pack, 'pack_media': _pack_media}[job['kind']]
        (handler or default_handler)(job, execution)
        control.finish(job_id, token, error_code=heartbeat_error[0] if heartbeat_error else None)
    except Exception as exc:
        try:
            control.finish(job_id, token, error_code=type(exc).__name__)
        except Conflict:
            pass  # Another lease owns it; never write through the lost lease.
    finally:
        stop.set()
        thread.join(5)
    return control.read_status(job_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--friend-id', required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--allowed-pack-version', nargs=2, action='append', default=[])
    args = parser.parse_args(argv)
    scope = FriendScope.load(args.friend_id, project_root=args.project_root,
        allowed_pack_versions=[PackVersion(pack_id=pack, version=version) for pack, version in args.allowed_pack_version])
    control = JobControl(scope)
    try:
        return 0 if run_job(control, args.job_id)['state'] in ('completed', 'paused', 'cancelled') else 2
    finally:
        control.close()


if __name__ == '__main__':
    raise SystemExit(main())
