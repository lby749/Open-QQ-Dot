"""Rebuildable per-friend FTS/dense projections; source files remain authoritative."""
from source_state import snapshot, require_snapshot, require_sources
from source_state import source_read
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import time
import uuid

import numpy as np

from persona_pack_bindings import PackBindingStore
from persona_snapshot import ReadOnlyLegacyReader
from persona_store import Conflict, PersonaStore, SchemaMismatch, ScopeMismatch, Unavailable
from persona_types import (AnalysisCallPolicy, AnalysisPurpose, Candidate, DataScope,
    EmbeddingMetadata, FriendScope, InvalidSource, PrivacyView, ProviderKind,
    RetrievalBatch, SourceOrigin, SourceRef)
from persona_versions import FILES, render_derived


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _tokens(text, query=False):
    result = []
    for word in re.findall(r'[\u3400-\u9fff]+|[a-zA-Z0-9_]+', text.casefold()):
        if '\u3400' <= word[0] <= '\u9fff':
            result.extend(word if not query or len(word) == 1 else ())
            result.extend(word[i:i+2] for i in range(len(word)-1))
        else:
            result.append(word)
    return list(dict.fromkeys(result))


class SemanticIndex:
    def __init__(self, scope, readonly=False, embedding_provider=None):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('verified FriendScope required')
        self.scope = scope
        try:
            self.store = PersonaStore(scope.friend_id, scope.friend_dir, readonly=readonly,
                                      project_root=scope.project_root)
        except SchemaMismatch as exc:
            if readonly:
                raise Unavailable('readonly index schema unavailable') from exc
            raise
        self.db = self.store.db
        self.provider = embedding_provider

    def close(self):
        self.store.close()

    def _check(self, write=False):
        FriendScope.load(self.scope.friend_id, project_root=self.scope.project_root,
            friend_dir=self.scope.friend_dir, allowed_pack_versions=self.scope.allowed_pack_versions)
        if write and self.store.readonly:
            raise Unavailable('readonly index')

    @source_read
    def metadata(self):
        self._check()
        row = self.db.execute("SELECT * FROM embedding_index_metadata WHERE friend_id=? AND state='ACTIVE'",
                              (self.scope.friend_id,)).fetchone()
        return dict(row) if row else None

    def _head(self, expected):
        if type(expected) is not int or expected < 0:
            raise InvalidSource('head revision must be nonnegative integer')
        head = self.metadata()
        if (head['revision'] if head else 0) != expected:
            raise Conflict('index head changed')
        return head

    def _generation(self, generation):
        row = self.db.execute('SELECT * FROM embedding_index_metadata WHERE friend_id=? AND generation=?',
                              (self.scope.friend_id, generation)).fetchone()
        if row is None or row['state'] != 'STAGED':
            raise Conflict('staged generation required')
        return dict(row)

    @staticmethod
    def _model(row):
        return EmbeddingMetadata(model=row['model'], dimension=row['dimension'], version=row['version'])

    def begin_generation(self, expected_revision, source_snapshot_hash, metadata):
        self._check(True)
        if not isinstance(metadata, EmbeddingMetadata) or not isinstance(source_snapshot_hash, str) or not source_snapshot_hash:
            raise InvalidSource('metadata and selected-source snapshot required')
        generation, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._head(expected_revision)
            self.db.execute('INSERT INTO embedding_index_metadata VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.scope.friend_id, generation, 'STAGED', metadata.model, metadata.dimension, metadata.version,
                 self.store.revision, source_snapshot_hash, expected_revision, '{}', now, now))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return generation

    def _legacy_message(self, seq=None, qq_id=None, inbound=True):
        with ReadOnlyLegacyReader(self.scope) as reader:
            row = reader._db.execute('SELECT seq,qq_id,direction,body,created_at FROM messages WHERE ' +
                ('seq=?' if seq is not None else 'qq_id=?'), (seq if seq is not None else qq_id,)).fetchone()
        if row is None or inbound and row['direction'] != 'friend':
            raise InvalidSource('inbound source missing')
        return dict(row)

    def _message_ref(self, row):
        return SourceRef(dataset_id=None, import_id=None, record_id=None, message_seq=row['seq'],
            speaker_id=self.scope.friend_id, origin=SourceOrigin.REAL_RELATIONSHIP,
            data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
            friend_id=self.scope.friend_id, event_time=row['created_at'],
            recorded_at=datetime.now(timezone.utc).isoformat())

    def _refs_current(self, refs):
        hashes = []
        for ref in refs:
            if ref.data_scope == DataScope.PRIVATE_FRIEND:
                if ref.friend_id != self.scope.friend_id:
                    raise ScopeMismatch('source friend differs')
                if ref.dataset_id and not ref.dataset_id.startswith(('manual:', 'legacy:')):
                    row = self.db.execute('SELECT * FROM imported_messages WHERE friend_id=? AND import_id=? '
                        'AND dataset_id=? AND record_id=? AND message_seq=?', (self.scope.friend_id, ref.import_id,
                        ref.dataset_id, ref.record_id, ref.message_seq)).fetchone()
                    if row is None:
                        raise InvalidSource('imported source missing')
                    hashes.append(dict(row))
                    registry = self.db.execute('SELECT active FROM dataset_registry WHERE friend_id=? AND dataset_id=?',
                                               (self.scope.friend_id, ref.dataset_id)).fetchone()
                    imported = self.db.execute('SELECT status FROM dataset_imports WHERE friend_id=? AND import_id=?',
                                              (self.scope.friend_id, ref.import_id)).fetchone()
                    if not registry or not registry['active'] or not imported or imported['status'] != 'COMPLETED':
                        raise InvalidSource('imported source retired')
                elif ref.message_seq is not None:
                    row = self._legacy_message(seq=ref.message_seq, inbound=False)
                    if ref.event_time is not None and row['created_at'] != ref.event_time:
                        raise InvalidSource('source timestamp differs')
                    hashes.append(row)
            else:
                if ref.friend_id is not None:
                    raise ScopeMismatch('public source has private owner')
                if ref.data_scope == DataScope.PUBLIC_PACK:
                    self.scope.require_pack(ref.pack_id, ref.pack_version)
                    row = self.db.execute('SELECT enabled,pack_version FROM pack_bindings WHERE friend_id=? AND pack_id=?',
                                          (self.scope.friend_id, ref.pack_id)).fetchone()
                    if not row or not row['enabled'] or row['pack_version'] != ref.pack_version:
                        raise ScopeMismatch('embedding source pack is no longer bound')
        return hashes

    def _private_version(self, version):
        root = self.scope.friend_dir / 'persona_versions'
        if not re.fullmatch('[0-9a-f]{32}', version):
            raise InvalidSource('private version identifier')
        pointer = json.loads((root / 'active.json').read_text('utf-8'))
        directory = root / version
        if directory.resolve() != directory or pointer['friend_id'] != self.scope.friend_id or pointer['version'] != version:
            raise Conflict('private version is no longer active')
        manifest_path = directory / 'manifest.json'
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != pointer['manifest_sha256']:
            raise Conflict('private manifest changed')
        manifest = json.loads(manifest_path.read_text('utf-8'))
        if manifest['friend_id'] != self.scope.friend_id or manifest['version'] != version or set(manifest['files']) != set(FILES):
            raise InvalidSource('private manifest identity/files differ')
        for name in FILES:
            path = directory / name
            if path.resolve().parent != directory or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['files'][name]:
                raise Conflict('immutable private file changed')
        profile = json.loads((directory / FILES[0]).read_text('utf-8'))
        style = json.loads((directory / FILES[1]).read_text('utf-8'))
        if profile['version'] != version or style['version'] != version or render_derived(profile, style) != tuple(
                (directory / name).read_text('utf-8') for name in FILES[2:4]):
            raise InvalidSource('private derived documents differ')
        return directory, profile, style, manifest

    def _resolve(self, locator):
        """Fixed locator kinds, never caller-supplied paths or content."""
        kind = locator.get('kind')
        pack_id = locator.get('pack_id')
        tags, valid_from, valid_to, confidence, timestamp = {}, None, None, 1.0, None
        if kind == 'history_message':
            raw = self._legacy_message(seq=locator['seq'])
            refs = (self._message_ref(raw),)
            namespace, source_type, content, text = 'memory', 'REAL_CHAT', raw['body'], raw['body']
            timestamp = raw['created_at']
        elif kind == 'memory_entry':
            with ReadOnlyLegacyReader(self.scope) as reader:
                row = reader._db.execute('SELECT * FROM memory_entries WHERE id=? AND active=1', (locator['id'],)).fetchone()
            if row is None:
                raise InvalidSource('memory missing')
            raw = dict(row)
            messages = [self._legacy_message(qq_id=value) for value in json.loads(raw['sources'])]
            if not messages:
                if raw['kind'] != 'manual' or not raw['locked']:
                    raise InvalidSource('memory lacks verified message provenance')
                refs = (SourceRef(dataset_id='manual:memory_entries', import_id=raw['id'], record_id=raw['id'],
                    message_seq=None, speaker_id='operator', origin=SourceOrigin.MANUAL,
                    data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                    friend_id=self.scope.friend_id, event_time=None, recorded_at=raw['updated_at']),)
            else:
                refs = tuple(self._message_ref(row) for row in messages)
            namespace, source_type, content = 'memory', 'REAL_CHAT' if messages else 'MANUAL', raw['text']
            text = content + ' ' + ' '.join(json.loads(raw['keywords']))
        elif kind == 'structured_manual':
            if locator.get('field') != 'basic_info':
                raise InvalidSource('only existing manual basic_info is a structured source')
            profile = json.loads((self.scope.friend_dir / 'friend.json').read_text('utf-8'))
            raw = profile.get('basic_info')
            if not isinstance(raw, dict) or not raw:
                raise InvalidSource('manual basic_info absent')
            namespace, source_type, content, text = 'structured', 'MANUAL', _json(raw), _json(raw)
            refs = (SourceRef(dataset_id='manual:friend_profile', import_id='basic_info', record_id='basic_info',
                message_seq=None, speaker_id='operator', origin=SourceOrigin.MANUAL,
                data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                friend_id=self.scope.friend_id, event_time=None, recorded_at='1970-01-01T00:00:00+00:00'),)
        elif kind in ('interest', 'topic'):
            table = 'interest_entries' if kind == 'interest' else 'topic_knowledge'
            row = self.db.execute(f'SELECT * FROM {table} WHERE friend_id=? AND id=? AND valid=1',
                                  (self.scope.friend_id, locator['id'])).fetchone()
            if row is None:
                raise InvalidSource('structured source missing')
            raw = dict(row)
            refs = tuple(SourceRef(**value) for value in json.loads(raw['source_refs_json']))
            if not refs or any(ref.data_scope != DataScope.PRIVATE_FRIEND for ref in refs):
                raise ScopeMismatch('only current-friend structured sources are indexed')
            if kind == 'topic':
                from topic_knowledge import TopicKnowledgeStore, _source_hash
                verifier = TopicKnowledgeStore(self.scope, readonly=True)
                try:
                    entry = dict(raw, keywords=json.loads(raw['keywords_json']))
                    mirror = {item['id']: item for item in verifier._mirror_rows()} if raw['source_type'] == 'PUBLIC_RESEARCH' else None
                    verifier._verify_sources(entry, refs, mirror)
                    if _source_hash(entry, refs) != raw['source_hash']:
                        raise InvalidSource('topic source hash differs')
                finally:
                    verifier.close()
                namespace, content = 'knowledge', raw['content']
                text = ' '.join([raw['title'], raw['content'], raw['summary']] + entry['keywords'])
                valid_from = raw['valid_from']
            else:
                from interest_entries import InterestStore
                verifier = InterestStore(self.scope, readonly=True)
                try:
                    verifier._verify_sources(refs, source_type=raw['source_type'], entry_id=raw['id'])
                finally:
                    verifier.close()
                namespace, content, text = 'interest', raw['content'], raw['topic'] + ' ' + raw['content']
                timestamp = raw['last_seen']
            source_type, valid_to, confidence = raw['source_type'], raw['valid_to'], raw['confidence']
            tags['topic'] = raw['topic']
        elif kind in ('example', 'persona_claim'):
            version = locator['version']
            if pack_id:
                store = PackBindingStore(self.scope, readonly=True)
                try:
                    pack = store.read_pack(pack_id, version)
                finally:
                    store.close()
                directory, profile, manifest = pack['path'], pack['persona_profile'], pack['manifest']
            else:
                directory, profile, _, manifest = self._private_version(version)
            if kind == 'example':
                with closing(sqlite3.connect((directory / 'examples.db').as_uri() + '?mode=ro', uri=True)) as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute('SELECT * FROM examples WHERE id=?', (locator['id'],)).fetchone()
                if row is None:
                    raise InvalidSource('Example missing')
                raw = dict(row)
                if raw['version'] != version or raw['friend_id'] != (None if pack_id else self.scope.friend_id):
                    raise ScopeMismatch('Example owner/version differs')
                refs = tuple(SourceRef(**value) for value in json.loads(raw['source_refs']))
                user, context = json.loads(raw['user']), json.loads(raw['context'])
                # Targets remain in display content, never in trigger embedding/search text.
                text = ' '.join([bubble['text'] for turn in context for bubble in turn['bubbles']] +
                    [bubble['text'] for bubble in user] + [raw[key] or '' for key in ('scene', 'emotion', 'topic')])
                tags = {key: raw[key] for key in ('scene', 'emotion', 'topic', 'relationship_tags')}
                relationships = tags['relationship_tags']
                if relationships:
                    try:
                        relationships = json.loads(relationships)
                    except (TypeError, ValueError):
                        pass
                    tags['relationship_tags'] = relationships if isinstance(relationships, list) else [relationships]
                namespace, content = 'example', _json(dict(raw, user=user, context=context,
                    target_bubbles=json.loads(raw['target_bubbles']), source_refs=json.loads(raw['source_refs'])))
            else:
                raw = next((claim for claim in profile['claims'] if claim['claim_id'] == locator['id']), None)
                if raw is None:
                    raise InvalidSource('claim missing')
                refs = tuple(SourceRef(**value) for value in manifest['source_refs'])
                namespace, content, text = 'persona', _json(raw), raw['claim']
            source_type = 'PUBLIC_PACK' if pack_id else 'PERSONA'
            if pack_id:
                refs = tuple(SourceRef(**dict(asdict(ref), data_scope=DataScope.PUBLIC_PACK,
                    pack_id=pack_id, pack_version=version)) for ref in refs)
        else:
            raise InvalidSource('unsupported source locator kind')
        if not refs or not text.strip():
            raise InvalidSource('source lacks usable text/provenance')
        require_sources(self.scope, refs, memory=namespace == 'memory')
        current_sources = self._refs_current(refs)
        scope = DataScope.PUBLIC_PACK if pack_id else DataScope.PRIVATE_FRIEND
        if any(ref.data_scope != scope or ref.friend_id != (None if pack_id else self.scope.friend_id) for ref in refs):
            raise ScopeMismatch('resolved source provenance differs')
        return dict(namespace=namespace, id=str(locator.get('id', locator.get('seq', locator.get('field')))), type=kind,
            source_type=source_type, content=content, index_text=text, search_text=' '.join(_tokens(text)),
            text_hash=_hash(text), source_hash=_hash([raw, current_sources]),
            source_locator_json=_json(dict(locator, tags=tags)), source_refs_json=_json([asdict(ref) for ref in refs]),
            original_scope=scope.value, pack_id=pack_id, pack_version=locator.get('version') if pack_id else None,
            timestamp=timestamp, valid_from=valid_from, valid_to=valid_to, topic=tags.get('topic'),
            confidence=confidence, valid=1, vector=None)

    def write_batch(self, generation, records, checkpoint, expected_source_revision):
        source_token = snapshot(self.scope)
        self._check(True)
        if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= 256 or len(_json(checkpoint)) > 16384:
            raise InvalidSource('bounded records/checkpoint required')
        staged = self._generation(generation)
        if self.store.revision != expected_source_revision or staged['source_revision'] != expected_source_revision:
            raise Conflict('source revision changed while building')
        documents = [self._resolve(record) for record in records]
        metadata, head = self._model(staged), self.metadata()
        missing = []
        for document in documents:
            old = None
            if head and self._model(head) == metadata:
                old = self.db.execute('SELECT vector FROM semantic_documents WHERE friend_id=? AND generation=? '
                    'AND namespace=? AND id=? AND text_hash=? AND source_hash=? AND valid=1',
                    (self.scope.friend_id, head['generation'], document['namespace'], document['id'],
                     document['text_hash'], document['source_hash'])).fetchone()
            if old and old['vector'] is not None:
                document['vector'] = old['vector']
            else:
                missing.append(document)
        if missing and self.provider:
            try:
                refs = tuple(SourceRef(**value) for row in missing for value in json.loads(row['source_refs_json']))
                AnalysisCallPolicy.validate(AnalysisPurpose.EMBEDDING, ProviderKind.LOCAL, refs)
                batch = self.provider.embed_texts([row['index_text'] for row in missing],
                    source_refs=refs,
                    expected_metadata=metadata)
                metadata.validate_compatible(batch.metadata)
                if len(batch.vectors) != len(missing):
                    raise InvalidSource('embedding batch length differs')
                for row, vector in zip(missing, batch.vectors):
                    row['vector'] = np.asarray(vector, dtype='<f4').tobytes()
            except Unavailable:
                pass  # Honest FTS-only projection; no substitute vectors.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            self._generation(generation)
            if self.store.revision != expected_source_revision:
                raise Conflict('source revision changed')
            for locator, row in zip(records, documents):
                if self._resolve(locator)['source_hash'] != row['source_hash']:
                    raise Conflict('source changed during encoding')
                if row['vector'] is not None and len(row['vector']) != 4 * metadata.dimension:
                    raise InvalidSource('vector BLOB dimension differs')
                columns = tuple(row)
                self.db.execute('INSERT INTO semantic_documents(friend_id,generation,' + ','.join(columns) +
                    ') VALUES(' + ','.join('?' for _ in range(len(columns)+2)) + ') '
                    'ON CONFLICT(friend_id,generation,namespace,id) DO UPDATE SET ' +
                    ','.join(column + '=excluded.' + column for column in columns),
                    (self.scope.friend_id, generation) + tuple(row.values()))
            self.db.execute('UPDATE embedding_index_metadata SET checkpoint_json=?,updated_at=? WHERE friend_id=? AND generation=?',
                (_json(checkpoint), datetime.now(timezone.utc).isoformat(), self.scope.friend_id, generation))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return len(documents)

    def activate(self, generation, expected_revision, expected_source_revision, source_snapshot_hash):
        source_token = snapshot(self.scope)
        self._check(True)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            staged = self._generation(generation)
            self._head(expected_revision)
            if staged['revision'] != expected_revision or self.store.revision != expected_source_revision or staged['source_revision'] != expected_source_revision or staged['source_snapshot_hash'] != source_snapshot_hash:
                raise Conflict('build snapshot changed')
            for row in self.db.execute('SELECT source_locator_json,source_hash FROM semantic_documents WHERE friend_id=? AND generation=?',
                                       (self.scope.friend_id, generation)):
                if self._resolve(json.loads(row['source_locator_json']))['source_hash'] != row['source_hash']:
                    raise Conflict('staged source changed')
            self.db.execute("UPDATE embedding_index_metadata SET state='RETIRED' WHERE friend_id=? AND state='ACTIVE'", (self.scope.friend_id,))
            self.db.execute("UPDATE embedding_index_metadata SET state='ACTIVE',revision=?,updated_at=? WHERE friend_id=? AND generation=?",
                (expected_revision+1, datetime.now(timezone.utc).isoformat(), self.scope.friend_id, generation))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return expected_revision+1

    def tombstone(self, namespace, id, expected_revision):
        self._check(True)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            head = self._head(expected_revision)
            if not head:
                raise Conflict('no active index')
            self.db.execute('UPDATE semantic_documents SET valid=0 WHERE friend_id=? AND generation=? AND namespace=? AND id=?',
                (self.scope.friend_id, head['generation'], namespace, id))
            self.db.execute('UPDATE embedding_index_metadata SET revision=revision+1 WHERE friend_id=? AND generation=?',
                (self.scope.friend_id, head['generation']))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return expected_revision+1

    def _where(self, request, head):
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('retrieval owner differs')
        clauses = ['d.friend_id=?', 'd.generation=?', 'd.valid=1', 'd.confidence>=?',
            '(d.valid_from IS NULL OR CASE WHEN length(d.valid_from)=10 THEN d.valid_from<=substr(?,1,10) ELSE julianday(d.valid_from)<=julianday(?) END)',
            '(d.valid_to IS NULL OR CASE WHEN length(d.valid_to)=10 THEN d.valid_to>=substr(?,1,10) ELSE julianday(d.valid_to)>julianday(?) END)']
        values = [self.scope.friend_id, head['generation'], request.filters.confidence] + [request.now]*4
        for column, choices in (('namespace', request.filters.namespace), ('source_type', request.filters.source_type)):
            if choices:
                clauses.append('d.' + column + ' IN(' + ','.join('?' for _ in choices) + ')')
                values.extend(choices)
        if request.filters.topic:
            clauses.append('d.topic=?')
            values.append(request.filters.topic)
        for tag in ('emotion', 'relationship_tags'):
            choices = getattr(request.filters, tag)
            if choices:
                if tag == 'relationship_tags':
                    clauses.append("(d.namespace!='example' OR EXISTS(SELECT 1 FROM json_each(d.source_locator_json,'$.tags.relationship_tags') t WHERE t.value IN(" + ','.join('?' for _ in choices) + ')))')
                else:
                    clauses.append("(d.namespace!='example' OR json_extract(d.source_locator_json,'$.tags." + tag +
                        "') IN(" + ','.join('?' for _ in choices) + '))')
                values.extend(choices)
        if request.filters.privacy_scope == DataScope.PUBLIC_PACK:
            clauses.append("d.original_scope='PUBLIC_PACK'")
        if request.scene not in ('any', 'unknown'):
            clauses.append("(d.namespace!='example' OR json_extract(d.source_locator_json,'$.tags.scene')=?)")
            values.append(request.scene)
        for endpoint, operator in zip(request.filters.time_range, ('>=', '<=')):
            if endpoint:
                clauses.append(('substr(d.timestamp,1,10)' if len(endpoint) == 10 else
                    'julianday(d.timestamp)') + operator + ('?' if len(endpoint) == 10 else 'julianday(?)'))
                values.append(endpoint)
        clauses.append("(d.original_scope='PRIVATE_FRIEND' OR (d.original_scope='PUBLIC_PACK' AND EXISTS("
            'SELECT 1 FROM pack_bindings b WHERE b.friend_id=d.friend_id AND b.pack_id=d.pack_id '
            'AND b.pack_version=d.pack_version AND b.enabled=1)))')
        packs = request.allowed_pack_versions & self.scope.allowed_pack_versions
        clauses.append("(d.original_scope='PRIVATE_FRIEND' OR (" + (' OR '.join('(d.pack_id=? AND d.pack_version=?)' for _ in packs) or '0') + '))')
        for pack in sorted(packs, key=lambda item: (item.pack_id, item.version)):
            values.extend((pack.pack_id, pack.version))
        return ' AND '.join(clauses), values

    def _candidate(self, row, score, retriever, request):
        source = self._resolve(json.loads(row['source_locator_json']))
        if source['source_hash'] != row['source_hash']:
            raise InvalidSource('indexed source changed')
        candidate = Candidate(id=row['id'], namespace=row['namespace'], type=row['type'], content=row['content'],
            score=float(score), source=SourceRef(**json.loads(row['source_refs_json'])[0]), reason='verified source',
            retriever=retriever, friend_id=self.scope.friend_id, timestamp=row['timestamp'], confidence=row['confidence'],
            original_scope=DataScope(row['original_scope']), pack_version=row['pack_version'],
            raw_score=float(score), valid_time=(row['valid_from'], row['valid_to']))
        candidate.validate_scope(self.scope)
        return candidate

    def _retrieve(self, request, limit, dense, source_refs):
        started = time.perf_counter()
        self._check()
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidSource('retrieval limit must be 1..50')
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('request friend differs')
        head, errors, results = self.metadata(), [], []
        if not head:
            return RetrievalBatch(candidates=(), elapsed_ms=0, degraded=True, errors=('index unavailable',))
        where, values = self._where(request, head)
        try:
            if dense:
                if self.provider is None:
                    raise Unavailable('dense provider unavailable')
                metadata = self._model(head)
                metadata.validate_compatible(self.provider.metadata)
                count = self.db.execute('SELECT count(*) FROM semantic_documents d WHERE ' + where + ' AND d.vector IS NOT NULL', values).fetchone()[0]
                if count > 10000:
                    raise Unavailable('dense candidate limit exceeded')
                AnalysisCallPolicy.validate(AnalysisPurpose.EMBEDDING, ProviderKind.LOCAL, source_refs)
                self._refs_current(source_refs)
                batch = self.provider.embed_query(request.query, source_refs=source_refs, expected_metadata=metadata)
                metadata.validate_compatible(batch.metadata)
                query = np.asarray(batch.vectors[0], dtype=np.float32)
                norm = np.linalg.norm(query)
                if not norm:
                    raise Unavailable('zero query vector')
                ranked = []
                cursor = self.db.execute('SELECT d.* FROM semantic_documents d WHERE ' + where + ' AND d.vector IS NOT NULL', values)
                while True:
                    rows = cursor.fetchmany(256)
                    if not rows:
                        break
                    for row in rows:
                        try:
                            self._candidate(row, 0, 'dense', request)
                        except ScopeMismatch:
                            raise
                        except (Conflict, InvalidSource, Unavailable, OSError, ValueError, KeyError):
                            errors.append('source invalid')
                            continue
                        vector = np.frombuffer(row['vector'], dtype='<f4')
                        if len(vector) != metadata.dimension or not np.isfinite(vector).all():
                            raise Unavailable('invalid stored vector')
                        denominator = norm * np.linalg.norm(vector)
                        if denominator:
                            ranked.append((float(np.dot(query, vector)/denominator), row))
                    ranked.sort(key=lambda pair: pair[0], reverse=True)
                    ranked = ranked[:50]
                rows = [(row, score) for score, row in ranked]
            else:
                tokens = _tokens(request.query, True)
                if not tokens:
                    return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter()-started)*1000)
                match = ' AND '.join('"' + token + '"' for token in tokens)
                rows = [(row, -row['bm']) for row in self.db.execute('SELECT d.*,bm25(semantic_documents_fts) AS bm '
                    'FROM semantic_documents_fts JOIN semantic_documents d ON d.rowid=semantic_documents_fts.rowid '
                    'WHERE semantic_documents_fts MATCH ? AND ' + where + ' ORDER BY bm LIMIT 10000', [match] + values)]
            for row, score in rows:
                try:
                    results.append(self._candidate(row, score, 'dense' if dense else 'fts', request))
                except ScopeMismatch:
                    raise
                except (Conflict, InvalidSource, Unavailable, OSError, ValueError, KeyError) as exc:
                    errors.append(type(exc).__name__ + ': source invalid')
                if len(results) == limit:
                    break
        except ScopeMismatch:
            raise
        except (Unavailable, ValueError, InvalidSource) as exc:
            errors.append(type(exc).__name__ + ': ' + str(exc))
        return RetrievalBatch(candidates=tuple(results), elapsed_ms=(time.perf_counter()-started)*1000,
                              degraded=bool(errors), errors=tuple(dict.fromkeys(errors)))

    @source_read
    def keyword(self, request, limit):
        return self._retrieve(request, limit, False, ())

    @source_read
    def dense(self, request, limit, *, source_refs):
        return self._retrieve(request, limit, True, source_refs)
