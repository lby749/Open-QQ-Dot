"""Immutable persona files; active.json is authoritative, SQLite is a catalog."""
from source_state import source_read, source_lock, require_sources
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import uuid

from persona_distiller import _hash, _json
from persona_evidence import EvidenceValidator, ValidatedAnalysis
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import InvalidSource


FILES = ('persona_profile.json', 'style_profile.json', 'persona_skill.md',
         'persona_report.md', 'examples.db')
EXAMPLE_COLUMNS = ('id', 'user', 'context', 'target_bubbles', 'scene', 'emotion',
                   'topic', 'reaction_type', 'source_origin', 'friend_id',
                   'relationship_tags', 'source_refs', 'version')


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path, value):
    path.write_text(_json(value), encoding='utf-8')


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_derived(profile, style):
    if profile['version'] != style['version']:
        raise InvalidSource('derived files require the same JSON version')
    heading = '# Persona ' + profile['version'] + '\n\n'
    skill = heading + 'Claims: ' + _json([{k: c.get(k) for k in
        ('claim', 'subject', 'dimension', 'scene', 'strength', 'confidence')}
        for c in profile['claims']]) + '\n'
    skill += '\nObserved style: ' + _json({k: style['observed'].get(k) for k in
        ('sample_info', 'message_length', 'bubble_behavior', 'punctuation',
         'question_behavior', 'sticker_behavior')}) + '\n'
    skill += '\n\nManual style: ' + _json(style.get('manual_overrides', {})) + '\n'
    report = heading + '```json\n' + _json({'profile': profile, 'style': style}) + '\n```\n'
    return skill, report


def claim_documents(validated, version):
    """Exact claim JSON expected from a builder, including verified evidence."""
    claims = []
    for draft in validated.result.role_claim_drafts + validated.result.relationship_claim_drafts:
        row = asdict(draft)
        row.update(evidence=[asdict(e) for e in validated.evidence
                             if e.claim_id == draft.claim_id and e.evidence_role == 'support'],
                   counter_evidence=[asdict(e) for e in validated.evidence
                                     if e.claim_id == draft.claim_id and e.evidence_role == 'counter'],
                   source_origin=list(draft.source_origins), version=version)
        claims.append(json.loads(_json(row)))
    return claims


class PersonaVersionStore:
    def __init__(self, scope, pair_store):
        self.validator = EvidenceValidator(scope, pair_store)
        self.scope = self.validator.scope
        self.pairs = pair_store
        self.catalog = pair_store.dialogues.store
        self.root = self.scope.friend_dir / 'persona_versions'

    @contextmanager
    def _lock(self):
        with source_lock(self.scope.friend_dir, publication=True):
            self.validator.analyzer.builder._check_scope()
            if self.catalog.readonly:
                raise Unavailable('readonly catalog cannot publish')
            self.root.mkdir(exist_ok=True)
            if self.root.resolve() != self.root:
                raise ScopeMismatch('version directory escaped scope')
            yield

    def _version(self, version, expected_hash=None):
        if not isinstance(version, str) or len(version) != 32 or any(c not in '0123456789abcdef' for c in version):
            raise Conflict('invalid version identifier')
        directory = self.root / version
        if directory.resolve() != directory:
            raise ScopeMismatch('version path escaped scope')
        try:
            manifest = _read(directory / 'manifest.json')
            if expected_hash and _digest(directory / 'manifest.json') != expected_hash:
                raise Conflict('manifest hash changed')
            if manifest['friend_id'] != self.scope.friend_id or manifest['version'] != version:
                raise ScopeMismatch('manifest scope differs')
            if set(manifest['files']) != set(FILES):
                raise Conflict('incomplete manifest')
            for name in FILES:
                if (directory / name).resolve().parent != directory or _digest(directory / name) != manifest['files'][name]:
                    raise Conflict('immutable version file changed: ' + name)
            profile, style = _read(directory / FILES[0]), _read(directory / FILES[1])
            if profile['version'] != version or style['version'] != version or profile['manual_revision'] != manifest['manual_revision']:
                raise Conflict('JSON version metadata differs')
            if render_derived(profile, style) != tuple((directory / n).read_text(encoding='utf-8') for n in FILES[2:4]):
                raise Conflict('derived files differ from JSON')
            return dict(manifest=manifest, profile=profile, style=style, directory=str(directory))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Conflict('invalid version files') from exc

    @source_read
    def read_active(self):
        self.validator.analyzer.builder._check_scope()
        pointer = self.root / 'active.json'
        if not pointer.exists():
            return None
        try:
            value = _read(pointer)
            if value['friend_id'] != self.scope.friend_id:
                raise ScopeMismatch('active scope differs')
            return self._version(value['version'], value['manifest_sha256'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Conflict('invalid active pointer') from exc

    def _cas(self, revision, active_version, manual_revision):
        current = self.read_active()
        if (type(revision) is not int or self.pairs.revision != revision or
                (current['manifest']['version'] if current else None) != active_version or
                (current['manifest']['manual_revision'] if current else 0) != manual_revision):
            raise Conflict('catalog, active or manual revision changed')
        return current

    def _verified(self, validated):
        if not isinstance(validated, ValidatedAnalysis):
            raise InvalidSource('publication requires ValidatedAnalysis')
        fresh = self.validator.validate(validated.result)
        if (fresh.source_content_hash != validated.source_content_hash or
                fresh.evidence != validated.evidence or fresh.validated_source_refs != validated.validated_source_refs):
            raise InvalidSource('validated source changed')
        self._completed(fresh.result.run_id, fresh.source_content_hash)
        return fresh

    def _completed(self, run_id, source_hash):
        row = self.pairs.db.execute('SELECT state,input_hash FROM persona_analysis_runs '
            'WHERE friend_id=? AND id=?', (self.scope.friend_id, run_id)).fetchone()
        if row is None or tuple(row) != ('COMPLETED', source_hash):
            raise InvalidSource('publication requires a COMPLETED source run')

    def _recheck(self, manifest):
        require_sources(self.scope, manifest['source_refs'])
        settings = manifest['settings']
        source = self.validator.analyzer.builder.build(settings['session_ids'],
            settings['target_speaker_ids'], settings['friend_speaker_ids'],
            pair_ids=settings.get('pair_ids'))
        if _hash(asdict(source)) != manifest['source_content_hash']:
            raise InvalidSource('version source changed')
        self._completed(manifest['run_id'], manifest['source_content_hash'])

    def _examples(self, path, validated):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            columns = {r[1] for r in db.execute('PRAGMA table_info(examples)')}
            if not set(EXAMPLE_COLUMNS) <= columns or db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise InvalidSource('examples schema or integrity differs')
            pairs = {p.id: p for p in validated.result.scene_analysis.analysis_input.role_pairs}
            scenes = {s.episode_id: s.scene for s in validated.result.scene_analysis.scenes}
            for row in db.execute('SELECT * FROM examples'):
                pair = pairs.get(row['id'])
                if pair is None:
                    raise InvalidSource('example is not a verified Target pair')
                for field, actual in [('user', pair.user_bubbles), ('context', pair.context_turns),
                                      ('target_bubbles', pair.target_bubbles), ('source_refs', pair.source_refs)]:
                    if json.loads(row[field]) != json.loads(_json([asdict(v) for v in actual])):
                        raise InvalidSource('example content differs from source')
                origins = {b.source.origin.value for b in pair.target_bubbles}
                owners = {b.source.friend_id for b in pair.target_bubbles}
                if origins != {row['source_origin']} or owners != {row['friend_id']}:
                    raise ScopeMismatch('example origin or owner differs')
                if row['scene'] != scenes.get(pair.id):
                    raise InvalidSource('example scene differs from verified scene')

    def publish_version(self, validated, build_dir, *, expected_revision,
                        expected_active_version, expected_manual_revision):
        with self._lock():
            current = self._cas(expected_revision, expected_active_version, expected_manual_revision)
            validated = self._verified(validated)
            directory = Path(build_dir).resolve()
            if any(not (directory / name).is_file() for name in FILES):
                raise InvalidSource('publication requires all five built files')
            profile, style = _read(directory / FILES[0]), _read(directory / FILES[1])
            if (profile.get('schema_version') != 'persona-profile-v1' or
                    style.get('schema_version') != 'style-profile-v1' or
                    profile['friend_id'] != self.scope.friend_id or style['friend_id'] != self.scope.friend_id or
                    profile['run_id'] != validated.result.run_id or
                    profile['source_content_hash'] != validated.source_content_hash or
                    profile['claims'] != claim_documents(validated, profile['version']) or
                    _json(profile['sample_status']) != _json(validated.result.sample_status)):
                raise InvalidSource('profile differs from verified claims')
            observed = dict(style['observed'])
            actual = dict(validated.result.style_profile)
            observed.pop('generated_at', None)
            actual.pop('generated_at', None)
            if _json(observed) != _json(actual) or render_derived(profile, style) != tuple(
                    (directory / n).read_text(encoding='utf-8') for n in FILES[2:4]):
                raise InvalidSource('style or derived files differ')
            self._examples(directory / 'examples.db', validated)
            profile['automatic_claims'] = profile['claims']
            profile['manual_overlay'] = current['profile']['manual_overlay'] if current else {}
            profile['manual_claims'] = current['profile'].get('manual_claims', {}) if current else {}
            profile['tombstones'] = current['profile']['tombstones'] if current else []
            profile['manual_revision'] = expected_manual_revision
            style['manual_overrides'] = current['style']['manual_overrides'] if current else {}
            metadata = dict(run_id=validated.result.run_id, source_content_hash=validated.source_content_hash,
                            settings=validated.result.settings,
                            source_refs=[asdict(ref) for ref in validated.validated_source_refs],
                            data_scopes=sorted({ref.data_scope.value for ref in validated.validated_source_refs}))
            return self._publish(directory, profile, style, metadata, expected_revision)

    def _publish(self, source, profile, style, metadata, revision):
        pointer = self.root / 'active.json'
        old_pointer_hash = _digest(pointer) if pointer.exists() else None
        version = uuid.uuid4().hex
        destination = self.root / version
        destination.mkdir()
        for name in FILES:
            shutil.copyfile(source / name, destination / name)
        profile['version'] = style['version'] = version
        profile['claims'] = []
        bases = {c['claim_key']: c for c in profile['automatic_claims']}
        for key, claim in profile.get('manual_claims', {}).items():
            bases.setdefault(key, claim)
        for original in bases.values():
            claim = dict(original, version=version)
            if claim['claim_key'] in profile['tombstones']:
                continue
            if claim['claim_key'] in profile['manual_overlay']:
                claim.update(profile['manual_overlay'][claim['claim_key']])
                claim.update(quality='MANUAL_LOCK', strength=None, confidence=0,
                             evidence=[], counter_evidence=[], source_origin=['MANUAL'])
            profile['claims'].append(claim)
        _write(destination / FILES[0], profile)
        _write(destination / FILES[1], style)
        for name, content in zip(FILES[2:4], render_derived(profile, style)):
            (destination / name).write_text(content, encoding='utf-8')
        with closing(sqlite3.connect(destination / 'examples.db')) as db:
            db.execute('UPDATE examples SET version=?', (version,))
            db.commit()
        manifest = dict(metadata, schema_version='persona-version-v1', friend_id=self.scope.friend_id,
            version=version, manual_revision=profile['manual_revision'], created_at=datetime.now(timezone.utc).isoformat(),
            files={name: _digest(destination / name) for name in FILES},
            derived_indexes={'status': 'not_built_P5', 'invalidated': True})
        _write(destination / 'manifest.json', manifest)
        row = self._catalog_row(manifest, 'STAGED')
        stage_revision = self.catalog.write_batch(revision, [row],
                                                {'pending_publication_id': version})
        self._switch_pointer(manifest, stage_revision, old_pointer_hash)
        self._reconcile_locked()
        return self.read_active()

    def _catalog_row(self, manifest, state):
        return dict(friend_id=self.scope.friend_id, id=manifest['version'], state=state,
            manifest_relpath='persona_versions/' + manifest['version'] + '/manifest.json',
            manifest_sha256=_digest(self.root / manifest['version'] / 'manifest.json'),
            created_at=manifest['created_at'])

    def _switch_pointer(self, manifest, revision, old_pointer_hash):
        db = self.pairs.db
        db.execute('BEGIN IMMEDIATE')
        try:
            if self.pairs.revision != revision:
                raise Conflict('catalog changed before pointer switch')
            pointer = self.root / 'active.json'
            if (_digest(pointer) if pointer.exists() else None) != old_pointer_hash:
                raise Conflict('active pointer changed before switch')
            self.read_active()
            temporary = self.root / 'active.tmp'
            _write(temporary, dict(schema_version='persona-active-v1', friend_id=self.scope.friend_id,
                version=manifest['version'], manifest_sha256=_digest(self.root / manifest['version'] / 'manifest.json')))
            os.replace(temporary, self.root / 'active.json')
        finally:
            db.rollback()

    def _reconcile_locked(self):
        current = self.read_active()
        version = current['manifest']['version'] if current else None
        rows = list(self.pairs.db.execute('SELECT * FROM artifact_versions WHERE friend_id=?', (self.scope.friend_id,)))
        changes = []
        for row in rows:
            if row['state'] == 'STAGED' and row['id'] != version:
                changes.append(dict(row, state='INVALID'))
        if current:
            expected = self._catalog_row(current['manifest'], 'COMPLETED')
            if not any(dict(row) == expected for row in rows):
                changes.append(expected)
        pending = self.pairs.db.execute('SELECT pending_publication_id FROM scope_state WHERE friend_id=?',
                                       (self.scope.friend_id,)).fetchone()[0]
        if changes or pending:
            self.catalog.write_batch(self.pairs.revision, changes, {'pending_publication_id': None})
        return current

    def reconcile(self):
        with self._lock():
            return self._reconcile_locked()

    def edit_manual(self, overlay, tombstones=(), style_overrides=None, *, expected_revision,
                    expected_active_version, expected_manual_revision):
        with self._lock():
            current = self._cas(expected_revision, expected_active_version, expected_manual_revision)
            if current is None:
                raise Conflict('no active version')
            self._recheck(current['manifest'])
            profile, style = current['profile'], current['style']
            bases = {c['claim_key']: c for c in profile['automatic_claims']}
            bases.update(profile.get('manual_claims', {}))
            keys = set(bases)
            if not isinstance(overlay, dict) or not set(overlay) <= keys or not set(tombstones) <= keys:
                raise InvalidSource('manual keys must identify existing claims')
            for value in overlay.values():
                if not isinstance(value, dict) or not set(value) <= {'claim'} or any(not isinstance(v, str) or not v.strip() for v in value.values()):
                    raise InvalidSource('manual overlay only changes claim text')
            profile['manual_overlay'].update(overlay)
            profile.setdefault('manual_claims', {}).update({key: bases[key] for key in overlay})
            profile['tombstones'] = sorted(set(profile['tombstones']) | set(tombstones))
            profile['manual_revision'] += 1
            for key, value in (style_overrides or {}).items():
                node = style['observed']
                for part in key.split('.'):
                    if not isinstance(node, dict) or part not in node:
                        raise InvalidSource('manual style path is not observed metadata')
                    node = node[part]
                _json(value)
                style['manual_overrides'][key] = {'value': value, 'source': 'MANUAL_LOCK'}
            return self._publish(Path(current['directory']), profile, style,
                                 self._metadata(current), expected_revision)

    @staticmethod
    def _metadata(current):
        return {k: current['manifest'][k] for k in
                ('run_id', 'source_content_hash', 'settings', 'source_refs', 'data_scopes')}

    def rollback(self, version, *, expected_revision, expected_active_version, expected_manual_revision):
        with self._lock():
            current = self._cas(expected_revision, expected_active_version, expected_manual_revision)
            if current is None:
                raise Conflict('no active version')
            row = self.pairs.db.execute('SELECT manifest_sha256 FROM artifact_versions WHERE friend_id=? AND id=? AND state=?',
                (self.scope.friend_id, version, 'COMPLETED')).fetchone()
            if row is None:
                raise Conflict('rollback version is not completed')
            old = self._version(version, row[0])
            self._recheck(old['manifest'])
            for key in ('manual_overlay', 'manual_claims', 'tombstones', 'manual_revision'):
                old['profile'][key] = current['profile'][key]
            old['style']['manual_overrides'] = current['style']['manual_overrides']
            return self._publish(Path(old['directory']), old['profile'], old['style'],
                                 self._metadata(old), expected_revision)
