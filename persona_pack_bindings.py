"""Per-friend CAS bindings to immutable public persona packs."""
from contextlib import closing
from source_state import source_read, snapshot, require_snapshot
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import sqlite3

from persona_pack import _manifest, _read_sticker_profile, _safe_id
from persona_store import Conflict, PersonaStore, SchemaMismatch, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, PackVersion,
                           PrivacyViolation, SourceOrigin, SourceRef)


_PUBLIC_ORIGINS = {SourceOrigin.PUBLIC_DATASET, SourceOrigin.REAL_SINGLE_SPEAKER,
                   SourceOrigin.REAL_PERSONA_POOL}


def _public_document(value):
    if isinstance(value, list):
        for item in value:
            _public_document(item)
    elif isinstance(value, dict):
        if value.get('friend_id') is not None or ('data_scope' in value and
                value['data_scope'] not in (DataScope.PUBLIC_DATASET, DataScope.PUBLIC_PACK)):
            raise PrivacyViolation('public pack contains private identity or source scope')
        if 'source_origin' in value:
            origins = value['source_origin'] if isinstance(value['source_origin'], list) else [value['source_origin']]
            if not origins or any(origin not in _PUBLIC_ORIGINS for origin in origins):
                raise PrivacyViolation('public pack contains non-human or private source origin')
        if 'privacy_view' in value and 'record_id' in value:
            ref = SourceRef(**value)
            if ref.origin not in _PUBLIC_ORIGINS:
                raise PrivacyViolation('public pack source is not public human evidence')
        for item in value.values():
            _public_document(item)


class PackBindingStore:
    def __init__(self, scope, readonly=False):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('PackBindingStore requires a verified FriendScope')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
                                     friend_dir=scope.friend_dir)
        try:
            self.store = PersonaStore(scope.friend_id, self.scope.friend_dir,
                                      readonly=readonly, project_root=self.scope.project_root)
        except SchemaMismatch as exc:
            if readonly:
                raise Unavailable('pack binding schema is unavailable in this read-only sidecar') from exc
            raise
        self.db = self.store.db

    def close(self):
        self.store.close()

    def _check_scope(self):
        FriendScope.load(self.scope.friend_id, project_root=self.scope.project_root,
                         friend_dir=self.scope.friend_dir)

    @source_read
    def bindings(self):
        self._check_scope()
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM pack_bindings WHERE friend_id=? ORDER BY pack_id', (self.scope.friend_id,))]

    @source_read
    def bound_scope(self):
        return FriendScope.load(self.scope.friend_id, project_root=self.scope.project_root,
            friend_dir=self.scope.friend_dir, allowed_pack_versions=[PackVersion(pack_id=row['pack_id'],
                version=row['pack_version']) for row in self.bindings() if row['enabled']])

    def _binding(self, pack_id, version):
        self._check_scope()
        _safe_id(pack_id, 'pack_id')
        _safe_id(version, 'version')
        row = self.db.execute('SELECT * FROM pack_bindings WHERE friend_id=? AND pack_id=?',
                              (self.scope.friend_id, pack_id)).fetchone()
        if row is None or not row['enabled'] or row['pack_version'] != version:
            raise ScopeMismatch('exact public pack version is not enabled for this friend')
        return dict(row)

    def _read_version(self, pack_id, version):
        _safe_id(pack_id, 'pack_id')
        _safe_id(version, 'version')
        directory = self.scope.project_root / 'data' / 'persona_packs' / pack_id / 'versions' / version
        if directory.resolve() != directory:
            raise ScopeMismatch('public pack path redirects outside its version directory')
        try:
            manifest, digest = _manifest(directory, pack_id, version)
            if manifest.get('scope') != 'PUBLIC_PACK' or manifest.get('friend_id') is not None:
                raise PrivacyViolation('pack manifest is not explicitly public')
            refs = tuple(SourceRef(**ref) for ref in manifest['source_refs'])
            if not refs or any(ref.data_scope not in (DataScope.PUBLIC_DATASET, DataScope.PUBLIC_PACK)
                               or ref.friend_id is not None or ref.origin not in _PUBLIC_ORIGINS for ref in refs):
                raise PrivacyViolation('pack must have exclusively public human source references')
            licenses = manifest['sources']
            if (not licenses or {ref.dataset_id for ref in refs} - {row['dataset_id'] for row in licenses}
                    or any(row.get('local_use_allowed') is not True or
                           row.get('derived_stats_allowed') is not True or not row.get('license_data')
                           for row in licenses)):
                raise PrivacyViolation('pack lacks explicit local/derived source permissions')
            profile = json.loads((directory / 'persona_profile.json').read_text('utf-8'))
            style = json.loads((directory / 'style_profile.json').read_text('utf-8'))
            for document in (profile, style):
                if (document.get('scope') != 'PUBLIC_PACK' or document.get('pack_id') != pack_id or
                        document.get('version') != version or document.get('friend_id') is not None):
                    raise PrivacyViolation('pack profile identity/scope differs from its manifest')
                _public_document(document)
            _public_document(manifest)
            examples = []
            with closing(sqlite3.connect((directory / 'examples.db').as_uri() + '?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise InvalidSource('public examples database failed integrity check')
                for row in db.execute('SELECT * FROM examples ORDER BY id'):
                    item = dict(row)
                    if item['friend_id'] is not None or item['version'] != version:
                        raise PrivacyViolation('public Example carries a friend identity or wrong version')
                    for key in ('user', 'context', 'target_bubbles', 'source_refs'):
                        item[key] = json.loads(item[key])
                    sources = {SourceRef(**ref) for ref in item['source_refs']}
                    if not sources or not sources <= set(refs) or not item['target_bubbles']:
                        raise InvalidSource('Example sources differ from pack provenance or lack a Target')
                    bubbles = item['user'] + item['target_bubbles'] + [bubble
                        for turn in item['context'] for bubble in turn['bubbles']]
                    if any(SourceRef(**bubble['source']) not in sources for bubble in bubbles):
                        raise InvalidSource('Example bubble source differs from its provenance')
                    _public_document(item)
                    examples.append(item)
            if {item['id'] for item in examples} != set(manifest['selected_pair_ids']):
                raise InvalidSource('public examples differ from selected Pair IDs')
            media = None
            if manifest.get('schema_version') == 'persona-pack-manifest-v2':
                media = _read_sticker_profile(directory, pack_id, version, len(examples))
                _public_document(media)
            return {'path': directory, 'manifest_sha256': digest, 'manifest': manifest,
                    'persona_profile': profile, 'style_profile': style, 'examples': examples,
                    'sticker_profile': media}
        except (OSError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise Unavailable('public pack is missing, incomplete or unreadable') from exc

    def read_pack(self, pack_id, version):
        token = snapshot(self.scope)
        binding = self._binding(pack_id, version)
        pack = self._read_version(pack_id, version)
        if self._binding(pack_id, version)['revision'] != binding['revision']:
            raise Conflict('pack binding changed during read')
        require_snapshot(self.scope, token)
        return pack  # Detached data; shared files and examples DB are never opened for writing.

    def bind(self, pack_id, version, expected_revision=0, public_strength=1.0, relationship_scale=20.0):
        self._check_scope()
        if self.store.readonly:
            raise Unavailable('read-only bindings cannot change')
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidSource('binding expected_revision must be a nonnegative integer')
        if (type(public_strength) not in (int, float) or not math.isfinite(public_strength) or
                not 0 <= public_strength <= 1 or type(relationship_scale) not in (int, float) or
                not math.isfinite(relationship_scale) or relationship_scale <= 0):
            raise InvalidSource('binding weights require finite strength 0..1 and positive scale')
        self._read_version(pack_id, version)
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT revision FROM pack_bindings WHERE friend_id=? AND pack_id=?',
                                  (self.scope.friend_id, pack_id)).fetchone()
            if (row['revision'] if row else 0) != expected_revision:
                raise Conflict('pack binding revision changed')
            self.db.execute('INSERT INTO pack_bindings VALUES(?,?,?,1,?,?,?,?,?) '
                'ON CONFLICT(friend_id,pack_id) DO UPDATE SET pack_version=excluded.pack_version,enabled=1,'
                'public_strength=excluded.public_strength,relationship_scale=excluded.relationship_scale,'
                'revision=excluded.revision,updated_at=excluded.updated_at',
                (self.scope.friend_id, pack_id, version, public_strength, relationship_scale,
                 expected_revision + 1, now, now))
            self.db.execute('COMMIT')
            return self._binding(pack_id, version)
        except Exception:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def unbind(self, pack_id, expected_revision):
        self._check_scope()
        _safe_id(pack_id, 'pack_id')
        if self.store.readonly:
            raise Unavailable('read-only bindings cannot change')
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidSource('unbind requires a positive binding revision')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE pack_bindings SET enabled=0,revision=revision+1,updated_at=? '
                'WHERE friend_id=? AND pack_id=? AND revision=?',
                (datetime.now(timezone.utc).isoformat(), self.scope.friend_id, pack_id, expected_revision))
            if self.db.execute('SELECT changes()').fetchone()[0] != 1:
                raise Conflict('pack binding revision changed')
            self.db.execute('COMMIT')
            return expected_revision + 1
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    @source_read
    def weights(self, pack_id, version, verified_relationship_samples):
        binding = self._binding(pack_id, version)
        if type(verified_relationship_samples) is not int or verified_relationship_samples < 0:
            raise InvalidSource('relationship sample count must be a verified nonnegative integer')
        weight = binding['public_strength'] / (1 + verified_relationship_samples / binding['relationship_scale'])
        return {'public_weight': weight, 'relationship_weight': 1 - weight}

    def export_pack(self, pack_id, version, destination):
        pack = self.read_pack(pack_id, version)
        if (pack['manifest'].get('raw_export_allowed') is not True or
                any(source.get('raw_export_allowed') is not True for source in pack['manifest']['sources'])):
            reasons = pack['manifest'].get('raw_export_reasons') or ['raw redistribution permission unavailable']
            raise PrivacyViolation('full public pack export denied: ' + '; '.join(reasons))
        target = Path(destination).absolute()
        if target.exists() or target.resolve() != target:
            raise InvalidSource('export requires a new destination without redirected paths')
        shutil.copytree(pack['path'], target)
        return target
