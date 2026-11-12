"""Build an immutable public persona pack from explicitly curated public Pairs."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid

from dataset_annotation.quota_guard import QuotaGuard
from dataset_registry import DatasetRegistry
from data_curation import RuleFilter
from curation_sampling import DiversitySampler, JudgedPair, LocalWeakLabeler
from dialogue_pairs import PairStore
from persona_distiller import PersonaDistiller, _json
from persona_evidence import EvidenceValidator
from persona_export import claim_documents, render_derived
from persona_judge import JudgeRunStore, PersonaJudge
from persona_types import (DataScope, InvalidSource, PrivacyView, PrivacyViolation,
                           SourceOrigin)
from style_examples import build_examples


_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS persona_packs('
    'pack_id TEXT PRIMARY KEY, active_version TEXT, revision INTEGER NOT NULL '
    'CHECK(revision>=0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS persona_pack_versions('
    'pack_id TEXT NOT NULL, version TEXT NOT NULL, job_id TEXT NOT NULL, '
    "state TEXT NOT NULL CHECK(state IN ('STAGED','ACTIVE','SUPERSEDED','INVALID')), "
    'manifest_sha256 TEXT NOT NULL, input_hash TEXT NOT NULL, '
    'source_revision INTEGER NOT NULL CHECK(source_revision>=0), created_at TEXT NOT NULL, '
    'PRIMARY KEY(pack_id,version), FOREIGN KEY(pack_id) REFERENCES persona_packs(pack_id))')


def _digest(value):
    payload = value if isinstance(value, bytes) else _json(value).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _safe_id(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', value):
        raise InvalidSource(name + ' must be a safe nonempty identifier')
    return value


class PersonaPackBuilder:
    def __init__(self, scope, pair_store, *, classifier=None, quota_factory=QuotaGuard):
        from persona_types import FriendScope
        if not isinstance(scope, FriendScope) or not isinstance(pair_store, PairStore) or pair_store.scope != scope:
            raise InvalidSource('PackBuilder requires a verified owner scope and its PairStore')
        self.scope, self.pairs = scope, pair_store
        self.classifier, self.quota_factory = classifier, quota_factory

    def run_pack_job(self, job, execution, *, judge_call):
        if job.get('kind') != 'pack' or not callable(judge_call):
            raise InvalidSource('pack job and injected Judge adapter are required')
        data = job['input']
        pack_id = _safe_id(data.get('pack_id'), 'pack_id')
        spec = _target_spec(data.get('target_spec'))
        session_ids = _bounded_ids(data.get('session_ids'), 'session_ids')
        pair_ids = _bounded_ids(data.get('pair_ids'), 'pair_ids')
        targets = _bounded_ids(data.get('target_speaker_ids'), 'target_speaker_ids')
        if len(pair_ids) > 64 or len(session_ids) > 64 or len(targets) > 64:
            raise InvalidSource('Pack accepts at most 64 source locators')
        selection_limit = _bounded_int(data.get('selection_limit'), 1, 64, 'selection_limit')
        batch_size = _bounded_int(data.get('judge_batch_size'), 1, 8, 'judge_batch_size')
        if data.get('use_local_model') is not False:
            raise InvalidSource('PACK-001 scene analysis is deterministic; no model is started here')
        source_revision = job['source_revision']
        if self.pairs.revision != source_revision:
            raise InvalidSource('job source revision is stale')

        loaded = []
        for session_id in session_ids:
            loaded.extend(self.pairs.load_pairs(session_id))
        by_id = {pair.id: pair for pair in loaded}
        if len(by_id) != len(loaded) or set(pair_ids) - set(by_id):
            raise InvalidSource('candidate Pair IDs are absent or duplicated in PairStore')
        candidates = [by_id[pair_id] for pair_id in pair_ids]
        if any(pair.target_speaker_id not in targets or pair.session_id not in session_ids for pair in candidates):
            raise InvalidSource('candidate Pair differs from frozen Target/session locators')
        refs = tuple(dict.fromkeys(ref for pair in candidates for ref in pair.source_refs))
        _validate_refs(refs, job['source_refs'])

        registry = DatasetRegistry(self.scope, readonly=True)
        try:
            licenses = _check_datasets(registry, refs)
            rules = RuleFilter()
            partitions = _partition_map(data.get('source_partitions'), refs)
            for ref in refs:
                imported = registry.db.execute('SELECT source_hash FROM dataset_imports WHERE '
                    'friend_id=? AND dataset_id=? AND import_id=?',
                    (self.scope.friend_id, ref.dataset_id, ref.import_id)).fetchone()
                if imported is None or partitions[(ref.dataset_id, ref.import_id, ref.record_id)].get(
                        'source_hash') != imported['source_hash']:
                    raise InvalidSource('partition source hash differs from actual import')
            eligible = [pair for pair in candidates if all(_train_partition(
                partitions[(ref.dataset_id, ref.import_id, ref.record_id)]) for ref in pair.source_refs)
                and len({partitions[(ref.dataset_id, ref.import_id, ref.record_id)]['split'].strip().lower()
                         for ref in pair.source_refs}) == 1]
            rule_pairs = tuple(rules.iter_filter(eligible))
            if not rule_pairs:
                raise InvalidSource('RuleFilter produced no eligible public Target Pair')
            weak = tuple(LocalWeakLabeler(self.classifier).iter_label(rule_pairs, limit=64))
            if len(weak) != len(rule_pairs) or any(item.subject != 'BOT_PERSONA' for item in weak):
                raise InvalidSource('local weak-label stage returned incomplete or wrong-subject rows')
            weak_by_id = {item.sample.id: item for item in weak}
            if len(weak_by_id) != len(weak):
                raise InvalidSource('local weak-label stage duplicated a Pair')

            run_store = JudgeRunStore(self.scope, execution.control.store)
            judge = PersonaJudge(self.scope, run_store, judge_call,
                model=job['model_version'], batch_size=batch_size)
            judged, run_records = [], {}
            for offset in range(0, len(rule_pairs), batch_size):
                if execution.cancelled():
                    raise InvalidSource('Pack job paused or cancelled at a batch boundary')
                batch = rule_pairs[offset:offset + batch_size]
                batch_hash = _digest({'input_hash': job['input_hash'],
                    'pairs': [pair.id for pair in batch], 'target_spec': spec})
                batch_id = 'pack-judge:' + batch_hash[:32]
                run_id = 'pack-' + _digest({'job_id': job['id'], 'batch_hash': batch_hash})[:48]
                prior = _load_judge_run(execution.control.db, self.scope.friend_id, run_id, batch)
                if prior is None:
                    with self.quota_factory(self.scope.project_root) as quota:
                        if not quota.allowed():
                            raise InvalidSource('quota guard stopped before public Judge request')
                        execution.start_attempt(batch_id, batch_hash)
                        result = judge.judge(batch, run_id=run_id, source_revision=source_revision,
                                             target_spec=spec)
                    checkpoint = dict(stage='judge', run_id=run_id, result=result)
                    if result['usage'].get('status') == 'known':
                        execution.checkpoint(batch_id, batch_hash, checkpoint)
                else:
                    result = prior
                if result['status'] != 'COMPLETED' or result['usage'].get('status') != 'known':
                    raise InvalidSource('Judge result is failed or has unknown usage; no retry')
                result = _load_judge_run(execution.control.db, self.scope.friend_id, run_id, batch)
                run_records[run_id] = result
                for pair in batch:
                    # Weak labels remain provisional and never replace Judge evidence.
                    judged.append(JudgedPair(pair, result))

            selection = DiversitySampler().select(judged, partition_by_source=partitions,
                limit=selection_limit, candidate_limit=min(4096, max(selection_limit, len(judged))))
            if not selection.samples:
                raise InvalidSource('DiversitySampler selected no public train samples')
            chosen = selection.samples
            chosen_ids = tuple(item.pair.id for item in chosen)
            if len(chosen_ids) != len(set(chosen_ids)) or len(chosen_ids) > 64:
                raise InvalidSource('selected Pair set is invalid or exceeds analyzer limit')
            chosen_sessions = tuple(sorted({item.pair.session_id for item in chosen}))
            chosen_targets = tuple(sorted({item.pair.target_speaker_id for item in chosen}))
            result = PersonaDistiller(self.scope, self.pairs).analyze(chosen_sessions, chosen_targets,
                (), False, execution, pair_ids=chosen_ids)
            if self.pairs.revision != source_revision:
                raise InvalidSource('source revision changed during Pack distillation')
            validated = EvidenceValidator(self.scope, self.pairs).validate(result)
            chosen_refs = validated.validated_source_refs
            _validate_refs(chosen_refs, job['source_refs'], require_subset=True)
            return self._publish(job, data, pack_id, spec, chosen, selection.counts,
                rules.stats, weak, run_records, validated, licenses, source_revision,
                execution, data.get('expected_active_version'), len(candidates) - len(eligible))
        finally:
            registry.close()

    def _publish(self, job, data, pack_id, spec, chosen, diversity_counts, rule_counts,
                 weak, run_records, validated, licenses, source_revision,
                 execution, expected_active, partition_excluded):
        project_root = self.scope.project_root
        root = project_root / 'data' / 'persona_packs' / pack_id
        root.mkdir(parents=True, exist_ok=True)
        if root.resolve().parent != (project_root / 'data' / 'persona_packs').resolve():
            raise InvalidSource('Pack path escaped project catalog root')
        version = _digest({'job_id': job['id'], 'input_hash': job['input_hash']})[:32]
        versions = root / 'versions'
        versions.mkdir(exist_ok=True)
        destination = versions / version
        if destination.exists():
            return _activate(root, pack_id, version, job, source_revision,
                manifest_path=destination / 'manifest.json', execution=execution,
                expected_active=expected_active)
        temp_dir = Path(tempfile.mkdtemp(prefix='.pack-', dir=root))
        try:
            profile = {'schema_version': 'persona-profile-v1', 'scope': 'PUBLIC_PACK',
                'pack_id': pack_id, 'friend_id': None, 'version': version,
                'run_id': validated.result.run_id, 'source_content_hash': validated.source_content_hash,
                'claims': claim_documents(validated, version),
                'sample_status': validated.result.sample_status, 'quality': 'validated_drafts',
                'target_spec': spec,
                'sticker_profile': {'status': 'unavailable', 'reason': 'P6 media analysis is not implemented'}}
            style = {'schema_version': 'style-profile-v1', 'scope': 'PUBLIC_PACK',
                'pack_id': pack_id, 'friend_id': None, 'version': version,
                'observed': validated.result.style_profile, 'manual_overrides': {},
                'sticker_profile': profile['sticker_profile']}
            (temp_dir / 'persona_profile.json').write_text(_json(profile), encoding='utf-8')
            (temp_dir / 'style_profile.json').write_text(_json(style), encoding='utf-8')
            skill, report = render_derived(profile, style)
            skill += '\nSticker profile: unavailable (P6 media stage pending).\n'
            report += '\nSticker profile: unavailable (P6 media stage pending).\n'
            (temp_dir / 'persona_skill.md').write_text(skill, encoding='utf-8')
            (temp_dir / 'persona_report.md').write_text(report, encoding='utf-8')
            build_examples(validated, temp_dir / 'examples.db', version, PrivacyView.LOCAL_FULL)
            _validate_files(temp_dir, validated, pack_id, version)
            files = {path.name: _digest(path.read_bytes()) for path in temp_dir.iterdir() if path.is_file()}
            if set(files) != {'persona_profile.json', 'style_profile.json', 'persona_skill.md',
                              'persona_report.md', 'examples.db'}:
                raise InvalidSource('Pack requires exactly five complete files')
            speakers = sorted({item.speaker_key for item in chosen})
            costs = _costs(run_records)
            manifest = {'schema_version': 'persona-pack-manifest-v1', 'pack_id': pack_id,
                'version': version, 'scope': 'PUBLIC_PACK', 'friend_id': None,
                'job_id': job['id'], 'input_hash': job['input_hash'],
                'source_revision': source_revision, 'source_content_hash': validated.source_content_hash,
                'target_spec': spec, 'target_cohort': {'multi_speaker': len(speakers) > 1,
                    'speaker_key_count': len(speakers), 'identity_policy':
                    'dataset/import/session/speaker scoped; no cross-session identity merge'},
                'selected_pair_ids': [item.pair.id for item in chosen],
                'selection': [{'pair_id': item.pair.id, 'judge_run_id': item.run_id,
                    'scores': item.scores, 'quality_score': item.quality_score, 'scene': item.scene,
                    'reaction': item.reaction, 'speaker_key': item.speaker_key,
                    'duplicate_cluster': item.duplicate_cluster, 'partitions': item.partitions}
                    for item in chosen],
                'sources': licenses, 'source_refs': [asdict(ref) for ref in validated.validated_source_refs],
                'raw_export_allowed': all(item['raw_export_allowed'] for item in licenses),
                'raw_export_reasons': [item['raw_export_reason'] for item in licenses
                    if not item['raw_export_allowed']],
                'stage_counts': {'partition': {'excluded_before_judge': partition_excluded},
                    'rule_filter': rule_counts,
                    'weak_label': {'input': len(weak), 'unknown': sum(row.unknown for row in weak),
                        'needs_review': sum(row.needs_review for row in weak),
                        'provisional': sum(row.status == 'WEAK_PROVISIONAL' for row in weak)},
                    'judge': {'input': sum(len(run['results']) for run in run_records.values()),
                        'calls': len(run_records)}, 'diversity': diversity_counts,
                    'distillation': validated.result.sample_status,
                    'claims': len(profile['claims']), 'examples': len(chosen)},
                'judge_runs': list(run_records.values()), 'costs': costs,
                'sticker_profile': profile['sticker_profile'], 'files': files,
                'created_at': datetime.now(timezone.utc).isoformat()}
            manifest_path = temp_dir / 'manifest.json'
            manifest_path.write_text(_json(manifest), encoding='utf-8')
            if self.pairs.revision != source_revision:
                raise InvalidSource('source revision changed before Pack publication')
            os.replace(temp_dir, destination)
            return _activate(root, pack_id, version, job, source_revision,
                manifest_path=destination / 'manifest.json', execution=execution,
                expected_active=expected_active)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)


def _target_spec(value):
    if not isinstance(value, dict) or set(value) != {'traits', 'behavior_requirements', 'anti_patterns'}:
        raise InvalidSource('target_spec must contain traits, behavior_requirements, and anti_patterns')
    if any(not isinstance(rows, list) or any(not isinstance(x, str) or not x.strip() for x in rows)
           for rows in value.values()):
        raise InvalidSource('target_spec fields must be lists of nonempty strings')
    return value


def _bounded_ids(values, name):
    if not isinstance(values, list) or not values or len(values) != len(set(values)) or any(
            not isinstance(value, str) or not value for value in values):
        raise InvalidSource(name + ' must be a nonempty unique list of strings')
    return tuple(values)


def _bounded_int(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidSource(name + ' is outside its supported bound')
    return value


def _validate_refs(refs, job_refs, require_subset=False):
    from persona_types import SourceRef
    frozen = tuple(SourceRef(**item) for item in job_refs)
    if not refs or any(ref.data_scope != DataScope.PUBLIC_DATASET or ref.friend_id is not None or
            ref.model_analysis_allowed is not True or not ref.license_id or ref.origin is None
            for ref in refs):
        raise PrivacyViolation('Pack accepts licensed public dataset refs only')
    actual, expected = set(refs), set(frozen)
    if (not require_subset and actual != expected) or (require_subset and not actual <= expected):
        raise InvalidSource('job source refs differ from actual Pair provenance')
    if any(ref.origin in (SourceOrigin.BOT_GENERATED, SourceOrigin.SYNTHETIC) for ref in refs):
        raise InvalidSource('synthetic or generated targets cannot enter a real persona Pack')


def _check_datasets(registry, refs):
    records = []
    for dataset_id in sorted({ref.dataset_id for ref in refs}):
        row = registry.get(dataset_id)
        if (row is None or not row.active or row.data_scope != 'PUBLIC_DATASET' or
                row.local_use_allowed is not True or row.derived_stats_allowed is not True or
                row.model_analysis_allowed is not True or not row.license_data):
            raise PrivacyViolation('dataset lacks explicit active local, derived, model, or license permission')
        raw_allowed = row.raw_corpus_redistribution_allowed is True
        records.append({'dataset_id': row.dataset_id, 'version': row.version,
            'license_code': row.license_code, 'license_data': row.license_data,
            'local_use_allowed': row.local_use_allowed,
            'derived_stats_allowed': row.derived_stats_allowed,
            'model_analysis_allowed': row.model_analysis_allowed,
            'raw_corpus_redistribution_allowed': row.raw_corpus_redistribution_allowed,
            'raw_export_allowed': raw_allowed,
            'raw_export_reason': None if raw_allowed else 'dataset permission does not allow raw redistribution'})
    return records


def _partition_map(rows, refs):
    if not isinstance(rows, list):
        raise InvalidSource('source_partitions must be an explicit list')
    mapping = {}
    for row in rows:
        if not isinstance(row, dict) or not all(key in row for key in
                ('dataset_id', 'import_id', 'record_id', 'split', 'is_holdout')):
            raise InvalidSource('partition row is incomplete')
        if type(row['is_holdout']) is not bool:
            raise InvalidSource('is_holdout must be explicit bool')
        key = (row['dataset_id'], row['import_id'], row['record_id'])
        if key in mapping:
            raise InvalidSource('duplicate source partition locator')
        mapping[key] = row
    needed = {(ref.dataset_id, ref.import_id, ref.record_id) for ref in refs}
    if set(mapping) != needed:
        raise InvalidSource('source partition locators must exactly cover candidate refs')
    return mapping


def _train_partition(row):
    split = row.get('split')
    if not isinstance(split, str) or split.strip().lower() in ('', 'unknown', 'none', 'null', 'n/a', '?'):
        return False
    tokens = set(re.findall(r'[a-z0-9]+', split.lower()))
    return not row['is_holdout'] and not tokens.intersection(
        {'test', 'eval', 'evaluation', 'holdout', 'heldout'})


def _load_judge_run(db, friend_id, run_id, batch):
    row = db.execute('SELECT * FROM dataset_curation_runs WHERE friend_id=? AND id=?',
                     (friend_id, run_id)).fetchone()
    if row is None:
        return None
    usage = json.loads(row['usage_json'])
    results = json.loads(row['results_json'])
    if row['status'] != 'COMPLETED' or usage.get('status') != 'known':
        raise InvalidSource('existing Judge run is incomplete/uncertain; never retry')
    if {item.get('pair_id') for item in results} != {pair.id for pair in batch}:
        raise InvalidSource('existing Judge run does not cover this exact batch')
    return {'run_id': run_id, 'status': row['status'], 'results': results,
        'model': row['model'], 'prompt_version': row['prompt_version'],
        'prompt_hash': row['prompt_hash'], 'usage': usage, 'error_code': row['error_code'],
        'started_at': row['started_at'], 'completed_at': row['completed_at']}


def _costs(runs):
    rows = sorted(runs.values(), key=lambda row: row['run_id'])
    totals = {name: sum(row['usage'][name] for row in rows)
              for name in ('input_tokens', 'output_tokens', 'total_tokens')}
    return {'unique_judge_runs': len(rows), 'usage': totals, 'currency_cost': None,
            'currency_cost_status': 'not_computed; no authoritative price attached'}


def _validate_files(directory, validated, pack_id, version):
    profile = json.loads((directory / 'persona_profile.json').read_text('utf-8'))
    style = json.loads((directory / 'style_profile.json').read_text('utf-8'))
    if any(row.get('pack_id') != pack_id or row.get('friend_id') is not None or
           row.get('version') != version for row in (profile, style)):
        raise InvalidSource('public profile cannot carry a friend identity')
    with closing(sqlite3.connect((directory / 'examples.db').as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise InvalidSource('examples database failed integrity check')
        ids = {row[0] for row in db.execute('SELECT id FROM examples')}
        expected = {pair.id for pair in validated.result.scene_analysis.analysis_input.role_pairs}
        if ids != expected:
            raise InvalidSource('examples DB must contain exactly the selected verified Pairs')
        if db.execute('SELECT count(*) FROM examples WHERE friend_id IS NOT NULL').fetchone()[0]:
            raise InvalidSource('public examples cannot carry friend_id')


def _catalog(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('BEGIN IMMEDIATE')
    try:
        for sql in _SCHEMA:
            db.execute(sql)
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')
        db.close()
        raise
    return db


def _empty_sticker_profile(pack_id, version, pair_count):
    return {'schema_version': 'sticker-profile-v1', 'scope': 'PUBLIC_PACK',
            'pack_id': pack_id, 'version': version, 'friend_id': None,
            'status': 'unknown', 'reason_codes': ['PUBLIC_ASSETS_UNAVAILABLE',
                'EMOTION_NOT_ANALYZED', 'VISION_NOT_CONFIGURED'],
            'sample_info': {'text_pair_count': pair_count, 'media_evaluable_count': 0,
                            'emotion_coverage': None},
            'emotion_groups': [], 'preferred_styles': [], 'asset_count': 0,
            'occurrence_count': 0, 'send_type_rates': None,
            'available_for_retrieval': False, 'image_rights_status': 'NOT_APPLICABLE'}


def _read_sticker_profile(directory, pack_id, version, pair_count):
    media = json.loads((directory / 'sticker_profile.json').read_text('utf-8'))
    style = json.loads((directory / 'style_profile.json').read_text('utf-8'))
    if (_json(media) != _json(_empty_sticker_profile(pack_id, version, pair_count)) or
            _json(style.get('sticker_profile')) != _json(media)):
        raise InvalidSource('Pack sticker profile must match its empty unknown Style copy')
    return media


def _manifest(directory, pack_id, version):
    path = directory / 'manifest.json'
    if directory.resolve() != directory or path.is_symlink():
        raise InvalidSource('Pack manifest path redirects outside its version directory')
    manifest = json.loads(path.read_text('utf-8'))
    names = {'persona_profile.json', 'style_profile.json', 'persona_skill.md',
             'persona_report.md', 'examples.db'}
    schema = manifest.get('schema_version')
    if schema == 'persona-pack-manifest-v2':
        names.add('sticker_profile.json')
    elif 'schema_version' in manifest and schema != 'persona-pack-manifest-v1':
        raise InvalidSource('unsupported Pack manifest schema')
    if (manifest.get('pack_id') != pack_id or manifest.get('version') != version or
            manifest.get('scope') != 'PUBLIC_PACK' or manifest.get('friend_id') is not None or
            not isinstance(manifest.get('files'), dict) or set(manifest['files']) != names or
            {p.name for p in directory.iterdir()} != names | {'manifest.json'}):
        raise InvalidSource('existing Pack manifest/files are incomplete or mismatched')
    if any(not (directory / name).is_file() or (directory / name).is_symlink() or
            (directory / name).resolve().parent != directory or
            _digest((directory / name).read_bytes()) != digest
            for name, digest in manifest['files'].items()):
        raise InvalidSource('immutable Pack artifact hash changed')
    if schema == 'persona-pack-manifest-v2':
        with closing(sqlite3.connect((directory / 'examples.db').as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            pair_count = db.execute('SELECT count(*) FROM examples').fetchone()[0]
        _read_sticker_profile(directory, pack_id, version, pair_count)
    return manifest, _digest(path.read_bytes())


def _sync_catalog(catalog, root, pack_id, active):
    # Immutable manifests and active.json are authoritative; this DB is a projection.
    now = datetime.now(timezone.utc).isoformat()
    catalog.execute('INSERT INTO persona_packs VALUES(?,?,?,?,?) ON CONFLICT(pack_id) DO UPDATE SET '
        'active_version=excluded.active_version,revision=excluded.revision,updated_at=excluded.updated_at',
        (pack_id, active['version'], active['revision'], now, now))
    catalog.execute("UPDATE persona_pack_versions SET state='SUPERSEDED' WHERE pack_id=? AND state='ACTIVE'",
                    (pack_id,))
    for directory in sorted((root / 'versions').iterdir()):
        if not directory.is_dir() or directory.name.startswith('.'):
            continue
        manifest, digest = _manifest(directory, pack_id, directory.name)
        catalog.execute('INSERT INTO persona_pack_versions VALUES(?,?,?,?,?,?,?,?) '
            "ON CONFLICT(pack_id,version) DO UPDATE SET state=CASE WHEN excluded.state='ACTIVE' "
            "THEN 'ACTIVE' ELSE persona_pack_versions.state END,manifest_sha256=excluded.manifest_sha256",
            (pack_id, directory.name, manifest['job_id'],
             'ACTIVE' if directory.name == active['version'] else 'STAGED', digest,
             manifest['input_hash'], manifest['source_revision'], manifest['created_at']))


def _activate(root, pack_id, version, job, source_revision, *, manifest_path,
              execution, expected_active):
    manifest, manifest_hash = _manifest(manifest_path.parent, pack_id, version)
    if (manifest['job_id'] != job['id'] or manifest['input_hash'] != job['input_hash'] or
            manifest['source_revision'] != source_revision):
        raise InvalidSource('existing Pack version differs from frozen job')
    if execution.cancelled():
        raise InvalidSource('Pack paused or cancelled before activation')
    catalog = _catalog(root.parent / 'catalog.sqlite3')
    temp = None
    switched = False
    projection_pending = False
    try:
        with execution.control._transaction():
            row = execution.control._lease(job['id'], execution.token)
            if row['state'] != 'running' or execution.control.store.revision != source_revision:
                raise InvalidSource('Pack lease/state/source revision changed before activation')
            if job['kind'] == 'pack_media':
                base = _media_base(execution.control.scope, job['input'])
                if _json(base['manifest']['source_refs']) != _json(job['source_refs']):
                    raise InvalidSource('Pack base provenance changed from frozen job')
            catalog.execute('BEGIN IMMEDIATE')
            active_path = root / 'active.json'
            current = json.loads(active_path.read_text('utf-8')) if active_path.exists() else None
            replay = current and current.get('version') == version and current.get('manifest_sha256') == manifest_hash
            if not replay and (current.get('version') if current else None) != expected_active:
                raise InvalidSource('Pack active version changed from the job snapshot')
            if (job['kind'] == 'pack_media' and not replay and
                    current.get('manifest_sha256') != job['input']['base_manifest_sha256']):
                raise InvalidSource('Pack active manifest changed from the job snapshot')
            if replay:
                active = current
            else:
                active = {'pack_id': pack_id, 'version': version, 'manifest_sha256': manifest_hash,
                          'revision': (current['revision'] if current else 0) + 1}
            # Validate/rebuild catalog before the final pointer switch.
            _sync_catalog(catalog, root, pack_id, active)
            execution.control._lease(job['id'], execution.token)
            if not replay:
                temp = active_path.with_name(active_path.name + '.' + uuid.uuid4().hex + '.tmp')
                temp.write_text(_json(active), encoding='utf-8')
                os.replace(temp, active_path)
            switched = True
            try:
                catalog.execute('COMMIT')
            except sqlite3.Error:
                projection_pending = True  # Pointer already published; never overwrite it from DB.
                if catalog.in_transaction:
                    catalog.execute('ROLLBACK')
    except Exception:
        if catalog.in_transaction:
            catalog.execute('ROLLBACK')
        raise
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        catalog.close()
    return {'pack_id': pack_id, 'version': version, 'path': str(manifest_path.parent),
            'manifest': str(manifest_path), 'active': switched,
            'catalog_reconciliation_pending': projection_pending,
            'stage_counts': manifest['stage_counts'], 'costs': manifest['costs']}


def run_pack_job(job, execution, *, judge_call):
    from dialogue_pairs import PairStore
    scope = execution.control.scope
    pairs = PairStore(scope)
    try:
        return PersonaPackBuilder(scope, pairs).run_pack_job(job, execution, judge_call=judge_call)
    finally:
        pairs.close()


def _media_base(scope, data):
    """Read verified public artifacts and recheck their current local source grants."""
    from dataset_normalizer import DatasetNormalizer
    from persona_pack_bindings import PackBindingStore
    reader = PackBindingStore(scope, readonly=True)
    try:
        base = reader._read_version(data['pack_id'], data['base_version'])
    finally:
        reader.close()
    manifest = base['manifest']
    if (base['manifest_sha256'] != data['base_manifest_sha256'] or
            manifest.get('schema_version') not in (None, 'persona-pack-manifest-v1')):
        raise InvalidSource('pack_media requires the unchanged five-file public base')
    registry = DatasetRegistry(scope, readonly=True)
    try:
        for source in manifest['sources']:
            current = registry.get(source['dataset_id'])
            if (current is None or not current.active or current.data_scope != DataScope.PUBLIC_DATASET or
                    current.local_use_allowed is not True or current.derived_stats_allowed is not True or
                    not current.license_data or any(source.get(key) != getattr(current, key) for key in
                    ('version', 'license_code', 'license_data', 'local_use_allowed', 'derived_stats_allowed',
                     'model_analysis_allowed', 'raw_corpus_redistribution_allowed'))):
                raise PrivacyViolation('base source permission/license changed or is unavailable')
            if source.get('raw_export_allowed') is True and current.raw_corpus_redistribution_allowed is not True:
                raise PrivacyViolation('base raw export permission is not currently verified')
        if manifest.get('raw_export_allowed') is True and any(
                source.get('raw_export_allowed') is not True for source in manifest['sources']):
            raise PrivacyViolation('base raw export permission differs from its source grants')
        partitions = {(p['dataset_id'], p['import_id'], p['record_id']): p['source_hash']
                      for row in manifest['selection'] for p in row['partitions']}
        for ref in manifest['source_refs']:
            if ref['data_scope'] != DataScope.PUBLIC_DATASET or ref['friend_id'] is not None:
                raise PrivacyViolation('pack_media requires direct public dataset provenance')
            imported = registry.db.execute('SELECT source_hash,data_scope FROM dataset_imports '
                'WHERE friend_id=? AND dataset_id=? AND import_id=?',
                (scope.friend_id, ref['dataset_id'], ref['import_id'])).fetchone()
            if (imported is None or imported['data_scope'] != DataScope.PUBLIC_DATASET or
                    imported['source_hash'] != partitions.get((ref['dataset_id'], ref['import_id'], ref['record_id']))):
                raise InvalidSource('base public import/hash no longer exists')
        normalizer = DatasetNormalizer(scope, registry)
        for example in base['examples']:
            pair = registry.db.execute('SELECT * FROM dialogue_pairs WHERE friend_id=? AND id=?',
                                      (scope.friend_id, example['id'])).fetchone()
            if pair is None or json.loads(pair['context_turn_ids_json']) != [t['id'] for t in example['context']]:
                raise InvalidSource('base Pair/provenance no longer exists')
            for turn_id, expected_bubbles in ((pair['input_turn_id'], example['user']),
                    (pair['target_turn_id'], example['target_bubbles']),
                    *((t['id'], t['bubbles']) for t in example['context'])):
                turn = registry.db.execute('SELECT bubbles_json FROM dialogue_turns WHERE friend_id=? AND id=?',
                                           (scope.friend_id, turn_id)).fetchone()
                strip_roles = lambda rows: [{k: v for k, v in b.items()
                    if k not in ('target_role', 'target_selected')} for b in rows]
                if turn is None or _json(strip_roles(json.loads(turn[0]))) != _json(strip_roles(expected_bubbles)):
                    raise InvalidSource('base Pair text differs from persisted Turn')
            bubbles = example['user'] + example['target_bubbles'] + [bubble
                for turn in example['context'] for bubble in turn['bubbles']]
            for bubble in bubbles:
                if not bubble['id'].startswith('import:'):
                    raise InvalidSource('base bubble lacks a persisted import locator')
                locator = json.loads(bubble['id'][7:])
                actual = list(normalizer.normalize([dict(zip(
                    ('dataset_id', 'import_id', 'record_id', 'message_seq'), locator), friend_id=scope.friend_id)]))
                expected = {k: v for k, v in bubble.items() if k not in ('target_role', 'target_selected')}
                if len(actual) != 1 or _json({k: v for k, v in asdict(actual[0]).items()
                        if k not in ('target_role', 'target_selected')}) != _json(expected):
                    raise InvalidSource('base original text/source differs from persisted import')
    finally:
        registry.close()
    with closing(sqlite3.connect((base['path'] / 'examples.db').as_uri() + '?mode=ro', uri=True)) as db:
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if objects != [('table', 'examples')]:
            raise InvalidSource('base examples database contains unexpected objects')
    return base


def run_pack_media_job(job, execution):
    job = execution.control.read_status(execution.job_id)
    if (job['kind'] != 'pack_media' or job['state'] != 'running' or
            job['source_revision'] != execution.control.store.revision or execution.cancelled()):
        raise InvalidSource('pack_media requires a running current deterministic job')
    data, scope = job['input'], execution.control.scope
    base = _media_base(scope, data)
    if _json(base['manifest']['source_refs']) != _json(job['source_refs']):
        raise InvalidSource('Pack base provenance differs from frozen job')
    pack_id = data['pack_id']
    version = _digest([job['id'], job['input_hash'], 'EMPTY_UNKNOWN'])[:32]
    root = base['path'].parent.parent
    destination = root / 'versions' / version
    if destination.resolve() != destination or version == data['base_version']:
        raise InvalidSource('new Pack version path must be independent')
    if destination.exists():
        return _activate(root, pack_id, version, job, job['source_revision'],
            manifest_path=destination / 'manifest.json', execution=execution,
            expected_active=data['expected_active_version'])
    temp = Path(tempfile.mkdtemp(prefix='.pack-media-', dir=root / 'versions')).resolve()
    installed = False
    try:
        media = _empty_sticker_profile(pack_id, version, len(base['examples']))
        for name in ('persona_profile.json', 'style_profile.json'):
            document = json.loads((base['path'] / name).read_text('utf-8'))
            document['version'] = version
            for claim in document.get('claims', []):
                claim['version'] = version
            document['sticker_profile'] = media
            (temp / name).write_text(_json(document), 'utf-8')
        profile = json.loads((temp / 'persona_profile.json').read_text('utf-8'))
        style = json.loads((temp / 'style_profile.json').read_text('utf-8'))
        for name, text in zip(('persona_skill.md', 'persona_report.md'), render_derived(profile, style)):
            (temp / name).write_text(text + '\n媒体状态：unknown；无可用媒体素材，回退纯文字。\n', 'utf-8')
        with closing(sqlite3.connect((base['path'] / 'examples.db').as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(temp / 'examples.db')) as target:
                source.backup(target)
                target.execute('UPDATE examples SET version=?', (version,))
                target.commit()
        (temp / 'sticker_profile.json').write_text(_json(media), 'utf-8')
        manifest = dict(base['manifest'], schema_version='persona-pack-manifest-v2', version=version,
            job_id=job['id'], input_hash=job['input_hash'], source_revision=job['source_revision'],
            parent_version=data['base_version'], parent_manifest_sha256=data['base_manifest_sha256'],
            media_mode='EMPTY_UNKNOWN', media_status='unknown', sticker_profile=media,
            created_at=datetime.now(timezone.utc).isoformat(),
            files={p.name: _digest(p.read_bytes()) for p in temp.iterdir()})
        (temp / 'manifest.json').write_text(_json(manifest), 'utf-8')
        _manifest(temp, pack_id, version)
        os.replace(temp, destination)
        installed = True
        return _activate(root, pack_id, version, job, job['source_revision'],
            manifest_path=destination / 'manifest.json', execution=execution,
            expected_active=data['expected_active_version'])
    except Exception:
        active_path = root / 'active.json'
        active = json.loads(active_path.read_text('utf-8')) if active_path.exists() else {}
        if installed and active.get('version') != version and destination.resolve() == destination:
            shutil.rmtree(destination)
        raise
    finally:
        if temp.exists() and temp.resolve().parent == (root / 'versions').resolve():
            shutil.rmtree(temp)
