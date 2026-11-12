"""One-shot, friend-local sticker metadata analysis for existing media files."""
from source_state import snapshot, require_snapshot
from source_state import source_read
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path, PureWindowsPath

from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import (AnalysisCallPolicy, AnalysisPurpose, DataScope,
                           FriendScope, ProviderKind, SourceOrigin, SourceRef)


_KINDS = {'STICKER', 'NON_STICKER', 'UNKNOWN'}
_PERMISSION_FIELDS = ('local_use_allowed', 'model_analysis_allowed',
                     'image_redistribution_allowed')
_RESULT_FIELDS = {'sticker_kind', 'emotion', 'intent', 'visual_style', 'intensity',
                  'text_content', 'description', 'analyzer_id', 'analysis_version'}
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_FIELDS = ('friend_id', 'asset_id', 'original_relpath', 'privacy_scope', 'source_origin',
           'source', 'declared_kind', 'license_image', 'license_evidence',
           'local_use_allowed', 'model_analysis_allowed', 'image_redistribution_allowed',
           'valid', 'created_at')


class StickerAsset:
    """Explicit identity, source, and license facts for one local original image."""

    def __init__(self, *, friend_id, asset_id, original_relpath, declared_kind,
                 source_origin=None, source=None, source_refs=(), license_image=None,
                 license_evidence=None, local_use_allowed=None,
                 model_analysis_allowed=None, image_redistribution_allowed=None):
        self.friend_id = friend_id
        self.asset_id = asset_id
        self.original_relpath = original_relpath
        self.declared_kind = declared_kind
        self.source_origin = getattr(source_origin, 'value', source_origin)
        self.source = source
        self.source_refs = tuple(source_refs)
        self.license_image = license_image
        self.license_evidence = license_evidence
        self.local_use_allowed = local_use_allowed
        self.model_analysis_allowed = model_analysis_allowed
        self.image_redistribution_allowed = image_redistribution_allowed


class StickerMetadataStore:
    def __init__(self, scope: FriendScope, readonly=False, analyzer=None):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('verified FriendScope required')
        if analyzer is not None:
            try:
                provider = ProviderKind(analyzer.provider_kind)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError('analyzer must declare provider_kind=LOCAL') from exc
            if provider != ProviderKind.LOCAL or not callable(getattr(analyzer, 'analyze', None)):
                raise ValueError('analyzer must expose analyze(bytes) as a LOCAL provider')
        self.scope = scope
        self.analyzer = analyzer
        self.store = PersonaStore(scope.friend_id, scope.friend_dir, readonly=readonly,
                                  project_root=scope.project_root)

    @source_read
    def get(self, asset_id):
        if not isinstance(asset_id, str) or not re.fullmatch(r'[0-9a-f]{64}', asset_id):
            raise ValueError('asset_id must be a lowercase SHA-256')
        row = self.store.db.execute(
            'SELECT m.* FROM sticker_metadata m JOIN sticker_assets a '
            'ON a.friend_id=m.friend_id AND a.asset_id=m.asset_id '
            'WHERE m.friend_id=? AND m.asset_id=? AND a.valid=1',
            (self.scope.friend_id, asset_id)).fetchone()
        return dict(row) if row is not None else None

    def analyze_once(self, asset: StickerAsset, source_refs=()):
        source_token = snapshot(self.scope)
        if not isinstance(asset, StickerAsset):
            raise ValueError('StickerAsset required')
        refs = tuple(source_refs or asset.source_refs)
        image_bytes = self._validate_asset(asset, refs)
        stored_source = self._source_record(asset, refs)
        identity = self._identity(asset, stored_source)

        existing = self._existing(asset.asset_id)
        if existing is not None:
            self._require_same_asset(existing, identity)
            require_snapshot(self.scope, source_token)
            return dict(existing['metadata'])
        if self.store.readonly:
            raise Unavailable('metadata sidecar is read-only')

        result = self._initial_result(asset)
        if asset.declared_kind == 'STICKER':
            reason = self._permission_reason(asset, refs)
            if reason is None and not self._has_analysis_source(asset, refs):
                reason = 'UNCLASSIFIED'
            if reason is not None:
                result = self._unknown('UNKNOWN', reason, 'none', 'none')
            elif self.analyzer is None:
                result = self._unknown('UNKNOWN', 'LOCAL_UNAVAILABLE', 'none', 'none')
            else:
                AnalysisCallPolicy.validate(AnalysisPurpose.STICKER_ANALYSIS,
                                            ProviderKind.LOCAL, refs)
                try:
                    result = self._validated_result(self.analyzer.analyze(image_bytes))
                except Exception:
                    result = self._unknown('UNKNOWN', 'ANALYSIS_FAILED', 'local', 'unknown')

        analyzed_at = datetime.now().astimezone().isoformat()
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            existing = self._existing(asset.asset_id)
            if existing is not None:
                self._require_same_asset(existing, identity)
                self.store.db.execute('COMMIT')
                return dict(existing['metadata'])
            now = analyzed_at
            values = (self.scope.friend_id, asset.asset_id, asset.original_relpath,
                      DataScope.PRIVATE_FRIEND.value, asset.source_origin, stored_source,
                      asset.declared_kind, asset.license_image, asset.license_evidence,
                      self._permission_db_value(asset.local_use_allowed),
                      self._permission_db_value(asset.model_analysis_allowed),
                      self._permission_db_value(asset.image_redistribution_allowed), 1, now)
            self.store.db.execute(
                'INSERT INTO sticker_assets (' + ','.join(_FIELDS) + ') VALUES (' +
                ','.join('?' for _ in _FIELDS) + ')', values)
            self.store.db.execute(
                'INSERT INTO sticker_metadata '
                '(friend_id,asset_id,analysis_status,sticker_kind,emotion,intent,visual_style,'
                'intensity,text_content,description,analyzer_id,analysis_version,unknown_reason,'
                'analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.scope.friend_id, asset.asset_id, result['analysis_status'],
                 result['sticker_kind'], result['emotion'], result['intent'],
                 result['visual_style'], result['intensity'], result['text_content'],
                 result['description'], result['analyzer_id'], result['analysis_version'],
                 result['unknown_reason'], now))
            self.store.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                                  (self.scope.friend_id,))
            self.store.db.execute('COMMIT')
        except Exception:
            self.store.db.execute('ROLLBACK')
            raise
        return self.get(asset.asset_id)

    def _validate_asset(self, asset, refs):
        if asset.friend_id != self.scope.friend_id:
            raise ScopeMismatch('asset belongs to another friend')
        if not isinstance(asset.asset_id, str) or not re.fullmatch(r'[0-9a-f]{64}', asset.asset_id):
            raise ValueError('asset_id must be a lowercase SHA-256')
        if asset.declared_kind not in _KINDS:
            raise ValueError('invalid declared_kind')
        if asset.source is not None and (not isinstance(asset.source, str) or
                                         not asset.source.strip()):
            raise ValueError('source locator must be text or unknown')
        if asset.source_origin is not None and asset.source_origin not in {
                item.value for item in SourceOrigin}:
            raise ValueError('invalid source_origin')
        for field in _PERMISSION_FIELDS:
            value = getattr(asset, field)
            if value is not None and (type(value) not in (int, bool) or value not in (0, 1)):
                raise ValueError(field + ' must be true, false, or unknown')
        for name in ('license_image', 'license_evidence'):
            if getattr(asset, name) is not None and not isinstance(getattr(asset, name), str):
                raise ValueError(name + ' must be text or unknown')
        if any(not isinstance(ref, SourceRef) for ref in refs):
            raise ValueError('source refs must be validated SourceRef values')
        for ref in refs:
            if (ref.data_scope != DataScope.PRIVATE_FRIEND or
                    ref.friend_id != self.scope.friend_id or
                    asset.source_origin is None or
                    getattr(ref.origin, 'value', ref.origin) != asset.source_origin):
                raise ScopeMismatch('source refs must match this friend and asset origin')
        if not isinstance(asset.original_relpath, str) or not asset.original_relpath.strip():
            raise ValueError('original_relpath must be nonempty text')
        rel = Path(asset.original_relpath)
        win_rel = PureWindowsPath(asset.original_relpath)
        if (rel.is_absolute() or win_rel.is_absolute() or
                win_rel.drive or '..' in rel.parts or '..' in win_rel.parts):
            raise ScopeMismatch('asset path must be relative to friend media')
        friend_root = self.scope.friend_dir.resolve()
        media_root = (friend_root / 'media').resolve()
        if media_root == friend_root or not media_root.is_relative_to(friend_root):
            raise ScopeMismatch('friend media directory redirects outside friend scope')
        candidate = self.scope.friend_dir / 'media' / rel
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ScopeMismatch('asset file is unavailable') from exc
        if not resolved.is_relative_to(media_root) or resolved == media_root:
            raise ScopeMismatch('asset path leaves friend media')
        cursor = self.scope.friend_dir / 'media'
        if cursor.is_symlink():
            raise ScopeMismatch('friend media path redirects')
        for part in rel.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ScopeMismatch('asset path contains a symbolic link')
        if not resolved.is_file():
            raise ScopeMismatch('asset is not a regular file')
        if resolved.stat().st_size > _MAX_IMAGE_BYTES:
            raise ScopeMismatch('asset exceeds 32 MiB')
        with resolved.open('rb') as source:
            image_bytes = source.read(_MAX_IMAGE_BYTES + 1)
        if len(image_bytes) > _MAX_IMAGE_BYTES:
            raise ScopeMismatch('asset exceeds 32 MiB')
        if hashlib.sha256(image_bytes).hexdigest() != asset.asset_id:
            raise ScopeMismatch('asset content hash does not match asset_id')
        return image_bytes

    @staticmethod
    def _has_analysis_source(asset, refs):
        return bool(refs) and asset.source_origin is not None

    def _permission_reason(self, asset, refs):
        fields = (asset.local_use_allowed, asset.model_analysis_allowed,
                  *(ref.model_analysis_allowed for ref in refs))
        if any(value is False or value == 0 for value in fields):
            return 'PERMISSION_DENIED'
        if any(value is None for value in fields):
            return 'PERMISSION_UNKNOWN'
        return None

    def _initial_result(self, asset):
        if asset.declared_kind == 'NON_STICKER':
            return self._unknown('NON_STICKER', 'UNCLASSIFIED', 'none', 'none')
        if asset.declared_kind == 'UNKNOWN':
            return self._unknown('UNKNOWN', 'UNCLASSIFIED', 'none', 'none')
        return self._unknown('UNKNOWN', 'LOCAL_UNAVAILABLE', 'none', 'none')

    @staticmethod
    def _unknown(kind, reason, analyzer_id, version):
        return {'analysis_status': 'UNKNOWN', 'sticker_kind': kind, 'emotion': None,
                'intent': None, 'visual_style': None, 'intensity': None,
                'text_content': None, 'description': None, 'analyzer_id': analyzer_id,
                'analysis_version': version, 'unknown_reason': reason}

    @staticmethod
    def _validated_result(value):
        if not isinstance(value, dict) or set(value) != _RESULT_FIELDS:
            raise ValueError('invalid analyzer result shape')
        kind = value['sticker_kind']
        if kind not in _KINDS:
            raise ValueError('invalid sticker classification')
        for name in ('analyzer_id', 'analysis_version'):
            if not isinstance(value[name], str) or not value[name].strip():
                raise ValueError('invalid analyzer identity')
        normalized = {}
        for name in ('emotion', 'intent', 'visual_style', 'text_content', 'description'):
            item = value[name]
            if item is not None and not isinstance(item, str):
                raise ValueError('invalid metadata text')
            normalized[name] = item
        intensity = value['intensity']
        if intensity is not None and (type(intensity) not in (int, float) or
                                      not math.isfinite(intensity) or not 0 <= intensity <= 1):
            raise ValueError('invalid intensity')
        if kind == 'UNKNOWN':
            return StickerMetadataStore._unknown(
                'UNKNOWN', 'UNCLASSIFIED', value['analyzer_id'], value['analysis_version'])
        return {'analysis_status': 'COMPLETED', 'sticker_kind': kind,
                **normalized, 'intensity': intensity, 'analyzer_id': value['analyzer_id'],
                'analysis_version': value['analysis_version'], 'unknown_reason': None}

    @staticmethod
    def _permission_db_value(value):
        return None if value is None else int(bool(value))

    @staticmethod
    def _source_record(asset, refs):
        refs_data = [{name: (getattr(ref, name).value if hasattr(getattr(ref, name), 'value')
                             else getattr(ref, name)) for name in (
            'dataset_id', 'import_id', 'record_id', 'message_seq', 'speaker_id', 'origin',
            'data_scope', 'privacy_view', 'friend_id', 'event_time', 'recorded_at',
            'license_id', 'model_analysis_allowed')}
            for ref in refs]
        if asset.source is None and not refs_data:
            return None
        return json.dumps({'locator': asset.source, 'source_refs': refs_data},
                          ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def _identity(self, asset, stored_source):
        return {'friend_id': self.scope.friend_id, 'asset_id': asset.asset_id,
                'original_relpath': asset.original_relpath,
                'privacy_scope': DataScope.PRIVATE_FRIEND.value,
                'source_origin': asset.source_origin, 'source': stored_source,
                'declared_kind': asset.declared_kind, 'license_image': asset.license_image,
                'license_evidence': asset.license_evidence,
                'local_use_allowed': self._permission_db_value(asset.local_use_allowed),
                'model_analysis_allowed': self._permission_db_value(asset.model_analysis_allowed),
                'image_redistribution_allowed': self._permission_db_value(
                    asset.image_redistribution_allowed), 'valid': 1}

    def _existing(self, asset_id):
        asset = self.store.db.execute(
            'SELECT * FROM sticker_assets WHERE friend_id=? AND asset_id=?',
            (self.scope.friend_id, asset_id)).fetchone()
        metadata = self.store.db.execute(
            'SELECT * FROM sticker_metadata WHERE friend_id=? AND asset_id=?',
            (self.scope.friend_id, asset_id)).fetchone()
        if asset is None and metadata is None:
            return None
        if asset is None or metadata is None:
            raise Conflict('asset metadata pair is incomplete')
        return {'asset': dict(asset), 'metadata': dict(metadata)}

    @staticmethod
    def _require_same_asset(existing, identity):
        if any(existing['asset'].get(key) != value for key, value in identity.items()
               if key != 'created_at'):
            raise Conflict('asset identity or permissions changed')

    def close(self):
        self.store.close()
