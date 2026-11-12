"""Small per-friend catalog. Persona JSON and Example text stay authoritative."""
import hashlib
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from source_state import SourceConnection, PERSONA_TABLES, state_statements, source_read


class ScopeMismatch(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


class SchemaMismatch(Unavailable):
    pass


class Conflict(RuntimeError):
    pass


def validate_friend_directory(friend_id, friend_dir, project_root):
    if not isinstance(friend_id, str) or not re.fullmatch(r'[0-9]{5,12}', friend_id):
        raise ScopeMismatch('friend_id must be 5–12 ASCII digits')
    directory = Path(friend_dir).resolve()
    friends = Path(project_root).resolve() / 'friends'
    if friends.resolve() != friends:
        raise ScopeMismatch('project friends directory redirects elsewhere')
    if directory.parent != friends or directory.name != friend_id:
        raise ScopeMismatch('friend directory does not match this project and friend')
    return directory


SCHEMA_STATEMENTS = (
    'CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, '
    'migration_hash TEXT NOT NULL, applied_at TEXT NOT NULL)',
    'CREATE TABLE scope_state (friend_id TEXT PRIMARY KEY, '
    'revision INTEGER NOT NULL CHECK(revision >= 0), pending_publication_id TEXT)',
    'CREATE TABLE artifact_versions (friend_id TEXT NOT NULL, id TEXT NOT NULL, '
    "state TEXT NOT NULL CHECK(state IN ('STAGED','COMPLETED','INVALID')), "
    'manifest_relpath TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id), '
    'UNIQUE(friend_id,manifest_relpath))',
)
REGISTRY_STATEMENTS = (
    'CREATE TABLE dataset_registry ('
    'friend_id TEXT NOT NULL, dataset_id TEXT NOT NULL, name TEXT NOT NULL, '
    'source TEXT, homepage TEXT, repository TEXT, local_path TEXT, version TEXT, '
    'license_code TEXT, license_data TEXT, license_image TEXT, '
    "data_scope TEXT NOT NULL CHECK(data_scope IN ('PRIVATE_FRIEND','PUBLIC_DATASET')), "
    'usage_scope TEXT NOT NULL, '
    'local_use_allowed INTEGER CHECK(local_use_allowed IS NULL OR local_use_allowed IN (0,1)), '
    'derived_stats_allowed INTEGER CHECK(derived_stats_allowed IS NULL OR derived_stats_allowed IN (0,1)), '
    'model_analysis_allowed INTEGER CHECK(model_analysis_allowed IS NULL OR model_analysis_allowed IN (0,1)), '
    'raw_corpus_redistribution_allowed INTEGER CHECK(raw_corpus_redistribution_allowed IS NULL OR raw_corpus_redistribution_allowed IN (0,1)), '
    'image_redistribution_allowed INTEGER CHECK(image_redistribution_allowed IS NULL OR image_redistribution_allowed IN (0,1)), '
    'contains_personal_data INTEGER CHECK(contains_personal_data IS NULL OR contains_personal_data IN (0,1)), '
    'modality TEXT, notes TEXT, active INTEGER NOT NULL CHECK(active IN (0,1)), '
    'revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,dataset_id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
)
IMPORT_STATEMENTS = (
    'CREATE TABLE dataset_imports ('
    'friend_id TEXT NOT NULL, import_id TEXT NOT NULL, dataset_id TEXT NOT NULL, '
    'source_origin TEXT, '
    "data_scope TEXT NOT NULL CHECK(data_scope IN ('PRIVATE_FRIEND','PUBLIC_DATASET')), "
    'source_hash TEXT NOT NULL, mapping_hash TEXT NOT NULL, settings_hash TEXT NOT NULL, '
    'checkpoint_json TEXT, '
    "status TEXT NOT NULL CHECK(status IN ('CREATED','RUNNING','COMPLETED','CANCELLED','FAILED')), "
    'created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,import_id), UNIQUE(friend_id,import_id,dataset_id), '
    'FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id), '
    'FOREIGN KEY(friend_id,dataset_id) REFERENCES dataset_registry(friend_id,dataset_id))',
    'CREATE TABLE imported_messages ('
    'friend_id TEXT NOT NULL, import_id TEXT NOT NULL, dataset_id TEXT NOT NULL, '
    'record_id TEXT NOT NULL, message_seq INTEGER NOT NULL CHECK(message_seq >= 0), '
    'speaker_id TEXT, speaker_role TEXT NOT NULL, direction TEXT, event_time TEXT, '
    'recorded_at TEXT NOT NULL, text TEXT NOT NULL, message_type TEXT NOT NULL, media_id TEXT, '
    'content_fingerprint TEXT NOT NULL, '
    'source_origin TEXT, source_record_json TEXT, '
    'PRIMARY KEY(friend_id,import_id,record_id,message_seq), '
    'UNIQUE(friend_id,dataset_id,record_id,message_seq), '
    'UNIQUE(friend_id,dataset_id,content_fingerprint), '
    'FOREIGN KEY(friend_id,import_id,dataset_id) '
    'REFERENCES dataset_imports(friend_id,import_id,dataset_id))',
)
DIALOGUE_STATEMENTS = (
    'CREATE TABLE dialogue_sessions ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, boundary_key TEXT NOT NULL, '
    'speaker_set_json TEXT NOT NULL, start_time TEXT, end_time TEXT, '
    'gap_seconds REAL NOT NULL CHECK(gap_seconds >= 0), '
    "time_quality TEXT NOT NULL CHECK(time_quality IN ('KNOWN','UNKNOWN')), "
    'ordered_message_ids_json TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
    'CREATE TABLE dialogue_turns ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, session_id TEXT NOT NULL, '
    'ordinal INTEGER NOT NULL CHECK(ordinal >= 0), speaker_id TEXT NOT NULL, '
    'event_time TEXT, '
    "time_quality TEXT NOT NULL CHECK(time_quality IN ('KNOWN','UNKNOWN')), "
    'previous_turn_id TEXT, bubbles_json TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,id), UNIQUE(friend_id,session_id,id), '
    'UNIQUE(friend_id,session_id,ordinal), '
    'FOREIGN KEY(friend_id,session_id) REFERENCES dialogue_sessions(friend_id,id), '
    'FOREIGN KEY(friend_id,session_id,previous_turn_id) '
    'REFERENCES dialogue_turns(friend_id,session_id,id))',
)
PAIR_STATEMENTS = (
    'CREATE TABLE dialogue_pairs ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, session_id TEXT NOT NULL, '
    'input_turn_id TEXT NOT NULL, target_turn_id TEXT NOT NULL, '
    'context_turn_ids_json TEXT NOT NULL, target_speaker_id TEXT NOT NULL, '
    'time_gap REAL CHECK(time_gap IS NULL OR time_gap >= 0), '
    "time_quality TEXT NOT NULL CHECK(time_quality IN ('KNOWN','UNKNOWN')), "
    'PRIMARY KEY(friend_id,id), UNIQUE(friend_id,target_turn_id), '
    'FOREIGN KEY(friend_id,session_id) REFERENCES dialogue_sessions(friend_id,id), '
    'FOREIGN KEY(friend_id,session_id,input_turn_id) REFERENCES dialogue_turns(friend_id,session_id,id), '
    'FOREIGN KEY(friend_id,session_id,target_turn_id) REFERENCES dialogue_turns(friend_id,session_id,id))',
)
ANALYSIS_STATEMENTS = (
    'CREATE TABLE persona_analysis_runs ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, '
    "state TEXT NOT NULL CHECK(state IN ('DRAFT','COMPLETED','FAILED','CANCELLED','STALE')), "
    'created_at TEXT NOT NULL, source_revision INTEGER NOT NULL CHECK(source_revision >= 0), '
    'input_hash TEXT NOT NULL, settings_json TEXT NOT NULL, result_json TEXT NOT NULL, '
    'model TEXT NOT NULL, prompt_version TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
    'CREATE TABLE persona_claims ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL, claim_key TEXT NOT NULL, '
    "subject TEXT NOT NULL CHECK(subject IN ('BOT_PERSONA','FRIEND','DYAD')), "
    'dimension TEXT NOT NULL, scene TEXT NOT NULL, '
    "state TEXT NOT NULL CHECK(state IN ('DRAFT','VALIDATED','INVALID')), "
    'claim_json TEXT NOT NULL, PRIMARY KEY(friend_id,id), '
    'UNIQUE(friend_id,run_id,claim_key), '
    'FOREIGN KEY(friend_id,run_id) REFERENCES persona_analysis_runs(friend_id,id))',
)
EVIDENCE_STATEMENTS = (
    'CREATE UNIQUE INDEX persona_claims_run_scope ON persona_claims(friend_id,id,run_id)',
    'CREATE TABLE persona_claim_evidence ('
    'friend_id TEXT NOT NULL, run_id TEXT NOT NULL, claim_id TEXT NOT NULL, '
    "evidence_role TEXT NOT NULL CHECK(evidence_role IN ('support','counter')), "
    'episode_id TEXT NOT NULL, source_message_id TEXT NOT NULL, '
    'span_start INTEGER, span_end INTEGER, span_text TEXT, '
    'source_origin TEXT NOT NULL, '
    "data_scope TEXT NOT NULL CHECK(data_scope IN ('PRIVATE_FRIEND','PUBLIC_DATASET','PUBLIC_PACK')), "
    'speaker_id TEXT NOT NULL, quality TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,run_id,claim_id,evidence_role,episode_id,source_message_id), '
    'FOREIGN KEY(friend_id,claim_id,run_id) REFERENCES persona_claims(friend_id,id,run_id), '
    'CHECK((span_start IS NULL AND span_end IS NULL AND span_text IS NULL) OR '
    '(span_start IS NOT NULL AND span_end IS NOT NULL AND span_text IS NOT NULL '
    'AND span_start >= 0 AND span_end >= span_start)))',
)
CURATION_STATEMENTS = (
    'CREATE TABLE dataset_curation_runs ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, '
    "status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED','USAGE_UNKNOWN')), "
    'source_revision INTEGER NOT NULL CHECK(source_revision >= 0), '
    'input_json TEXT NOT NULL, settings_json TEXT NOT NULL, model TEXT NOT NULL, '
    'prompt_version TEXT NOT NULL, prompt_hash TEXT NOT NULL, results_json TEXT NOT NULL, '
    'usage_json TEXT NOT NULL, error_code TEXT, started_at TEXT NOT NULL, completed_at TEXT, '
    'PRIMARY KEY(friend_id,id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
)
JOB_STATEMENTS = (
    'CREATE TABLE analysis_jobs ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, owner_scope TEXT NOT NULL, '
    'input_hash TEXT NOT NULL, input_json TEXT NOT NULL, source_refs_json TEXT NOT NULL, '
    'settings_hash TEXT NOT NULL, model_version TEXT NOT NULL, prompt_version TEXT NOT NULL, '
    'source_revision INTEGER NOT NULL CHECK(source_revision >= 0), '
    "state TEXT NOT NULL CHECK(state IN ('queued','running','paused','cancelled','failed','completed')), "
    'worker_pid INTEGER, lease_token TEXT, lease_until REAL, '
    "checkpoint_json TEXT NOT NULL DEFAULT '{}', attempt_json TEXT NOT NULL DEFAULT '{}', "
    'version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0), '
    'tokens_unknown INTEGER NOT NULL DEFAULT 0 CHECK(tokens_unknown IN (0,1)), '
    'error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id), '
    'UNIQUE(friend_id,kind,input_hash,settings_hash,model_version,prompt_version,source_revision))',
)
PACK_BINDING_STATEMENTS = (
    'CREATE TABLE pack_bindings ('
    'friend_id TEXT NOT NULL, pack_id TEXT NOT NULL, pack_version TEXT NOT NULL, '
    'enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), '
    'public_strength REAL NOT NULL CHECK(public_strength BETWEEN 0 AND 1), '
    'relationship_scale REAL NOT NULL CHECK(relationship_scale>0), '
    'revision INTEGER NOT NULL CHECK(revision>=1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,pack_id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
)
INTEREST_STATEMENTS = (
    'CREATE TABLE interest_entries ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, topic TEXT NOT NULL, content TEXT NOT NULL, '
    'category TEXT NOT NULL, strength REAL NOT NULL CHECK(strength BETWEEN 0 AND 1), '
    'confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), '
    "interest_type TEXT NOT NULL CHECK(interest_type IN ('stable','recent','temporary','one_off','manual')), "
    "source_type TEXT NOT NULL CHECK(source_type IN ('REAL_CHAT','MANUAL')), "
    'source_refs_json TEXT NOT NULL, first_seen TEXT, last_seen TEXT, valid_to TEXT, '
    'locked INTEGER NOT NULL CHECK(locked IN (0,1)), valid INTEGER NOT NULL CHECK(valid IN (0,1)), '
    'revision INTEGER NOT NULL CHECK(revision>=1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
)
TOPIC_STATEMENTS = (
    'CREATE TABLE topic_knowledge ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, topic TEXT NOT NULL, subtopic TEXT NOT NULL, '
    'title TEXT NOT NULL, content TEXT NOT NULL, summary TEXT NOT NULL, keywords_json TEXT NOT NULL, '
    "source_type TEXT NOT NULL CHECK(source_type IN ('REAL_CHAT','PUBLIC_RESEARCH','MANUAL','MODEL_SUMMARY','PUBLIC_DATASET','SYNTHETIC')), "
    'source_url TEXT, source_refs_json TEXT NOT NULL, source_hash TEXT NOT NULL, '
    'created_at TEXT NOT NULL, updated_at TEXT NOT NULL, valid_from TEXT, valid_to TEXT, '
    'confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), '
    'quality_score REAL NOT NULL CHECK(quality_score BETWEEN 0 AND 1), '
    "time_sensitivity TEXT NOT NULL CHECK(time_sensitivity IN ('STABLE','FAST_CHANGING','UNKNOWN')), "
    'last_verified_at TEXT, embedding_model TEXT, embedding_version TEXT, '
    'valid INTEGER NOT NULL CHECK(valid IN (0,1)), revision INTEGER NOT NULL CHECK(revision>=1), '
    'PRIMARY KEY(friend_id,id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
)
INDEX_STATEMENTS = (
    'CREATE TABLE embedding_index_metadata ('
    'friend_id TEXT NOT NULL, generation TEXT NOT NULL, '
    "state TEXT NOT NULL CHECK(state IN ('STAGED','ACTIVE','RETIRED')), "
    'model TEXT NOT NULL, dimension INTEGER NOT NULL CHECK(dimension>0), version TEXT NOT NULL, '
    'source_revision INTEGER NOT NULL CHECK(source_revision>=0), source_snapshot_hash TEXT NOT NULL, '
    'revision INTEGER NOT NULL CHECK(revision>=0), checkpoint_json TEXT NOT NULL, '
    'created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,generation), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
    "CREATE UNIQUE INDEX embedding_one_active ON embedding_index_metadata(friend_id) WHERE state='ACTIVE'",
    'CREATE TABLE semantic_documents ('
    'rowid INTEGER PRIMARY KEY, friend_id TEXT NOT NULL, generation TEXT NOT NULL, '
    'namespace TEXT NOT NULL, id TEXT NOT NULL, type TEXT NOT NULL, source_type TEXT NOT NULL, '
    'content TEXT NOT NULL, index_text TEXT NOT NULL, search_text TEXT NOT NULL, '
    'text_hash TEXT NOT NULL, source_hash TEXT NOT NULL, source_locator_json TEXT NOT NULL, '
    'source_refs_json TEXT NOT NULL, '
    "original_scope TEXT NOT NULL CHECK(original_scope IN ('PRIVATE_FRIEND','PUBLIC_PACK')), "
    'pack_id TEXT, pack_version TEXT, timestamp TEXT, valid_from TEXT, valid_to TEXT, topic TEXT, '
    'confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), '
    'valid INTEGER NOT NULL CHECK(valid IN (0,1)), vector BLOB, '
    'UNIQUE(friend_id,generation,namespace,id), '
    'FOREIGN KEY(friend_id,generation) REFERENCES embedding_index_metadata(friend_id,generation))',
    'CREATE INDEX semantic_prefilter ON semantic_documents('
    'friend_id,generation,namespace,original_scope,valid,source_type,pack_id,pack_version)',
    "CREATE VIRTUAL TABLE semantic_documents_fts USING fts5(search_text,content='semantic_documents',content_rowid='rowid',tokenize='unicode61')",
    'CREATE TRIGGER semantic_fts_insert AFTER INSERT ON semantic_documents BEGIN '
    'INSERT INTO semantic_documents_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END',
    'CREATE TRIGGER semantic_fts_delete AFTER DELETE ON semantic_documents BEGIN '
    "INSERT INTO semantic_documents_fts(semantic_documents_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text); END",
    'CREATE TRIGGER semantic_fts_update AFTER UPDATE OF search_text ON semantic_documents BEGIN '
    "INSERT INTO semantic_documents_fts(semantic_documents_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text); "
    'INSERT INTO semantic_documents_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END',
)
ENTITY_STATEMENTS = (
    'CREATE TABLE knowledge_entities ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, '
    "entity_type TEXT NOT NULL CHECK(entity_type IN ('Person','Music','Game','Movie','Model','Product','Place','Topic','Event','Concept')), "
    'aliases_json TEXT NOT NULL, source_refs_json TEXT NOT NULL, source_hash TEXT NOT NULL, '
    'confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), '
    'valid INTEGER NOT NULL CHECK(valid IN (0,1)), revision INTEGER NOT NULL CHECK(revision>=1), '
    'created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,id), FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
    'CREATE TABLE knowledge_relations ('
    'friend_id TEXT NOT NULL, id TEXT NOT NULL, source_entity TEXT NOT NULL, relation TEXT NOT NULL, '
    'target_entity TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), '
    'strength REAL NOT NULL CHECK(strength BETWEEN 0 AND 1), '
    'first_seen TEXT, last_seen TEXT, valid_from TEXT, valid_to TEXT, '
    'source_refs_json TEXT NOT NULL, source_hash TEXT NOT NULL, '
    'valid INTEGER NOT NULL CHECK(valid IN (0,1)), revision INTEGER NOT NULL CHECK(revision>=1), '
    'created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(friend_id,id), '
    'FOREIGN KEY(friend_id,source_entity) REFERENCES knowledge_entities(friend_id,id), '
    'FOREIGN KEY(friend_id,target_entity) REFERENCES knowledge_entities(friend_id,id))',
    'CREATE INDEX entity_relation_scope ON knowledge_relations(friend_id,source_entity,target_entity,valid)',
)
STICKER_STATEMENTS = (
    'CREATE TABLE sticker_assets ('
    'friend_id TEXT NOT NULL, asset_id TEXT NOT NULL '
    "CHECK(length(asset_id)=64 AND asset_id NOT GLOB '*[^0-9a-f]*'), "
    'original_relpath TEXT NOT NULL CHECK(length(original_relpath)>0), '
    "privacy_scope TEXT NOT NULL CHECK(privacy_scope IN ('PRIVATE_FRIEND','PUBLIC_DATASET','PUBLIC_PACK')), "
    'source_origin TEXT CHECK(source_origin IS NULL OR source_origin IN ('
    "'REAL_RELATIONSHIP','REAL_SINGLE_SPEAKER','REAL_PERSONA_POOL','PUBLIC_DATASET','MANUAL','SYNTHETIC','BOT_GENERATED')), "
    'source TEXT, '
    "declared_kind TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK(declared_kind IN ('STICKER','NON_STICKER','UNKNOWN')), "
    'license_image TEXT, license_evidence TEXT, '
    'local_use_allowed INTEGER CHECK(local_use_allowed IS NULL OR local_use_allowed IN (0,1)), '
    'model_analysis_allowed INTEGER CHECK(model_analysis_allowed IS NULL OR model_analysis_allowed IN (0,1)), '
    'image_redistribution_allowed INTEGER CHECK(image_redistribution_allowed IS NULL OR image_redistribution_allowed IN (0,1)), '
    'valid INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0,1)), created_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,asset_id), UNIQUE(friend_id,original_relpath), '
    'FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
    'CREATE TABLE sticker_metadata ('
    'friend_id TEXT NOT NULL, asset_id TEXT NOT NULL, '
    "analysis_status TEXT NOT NULL CHECK(analysis_status IN ('COMPLETED','UNKNOWN')), "
    "sticker_kind TEXT NOT NULL CHECK(sticker_kind IN ('STICKER','NON_STICKER','UNKNOWN')), "
    'emotion TEXT, intent TEXT, visual_style TEXT, '
    'intensity REAL CHECK(intensity IS NULL OR intensity BETWEEN 0 AND 1), '
    'text_content TEXT, description TEXT, '
    'analyzer_id TEXT NOT NULL CHECK(length(analyzer_id)>0), '
    'analysis_version TEXT NOT NULL CHECK(length(analysis_version)>0), '
    'unknown_reason TEXT CHECK(unknown_reason IS NULL OR unknown_reason IN ('
    "'LOCAL_UNAVAILABLE','PERMISSION_UNKNOWN','PERMISSION_DENIED','UNCLASSIFIED','ANALYSIS_FAILED')), "
    'analyzed_at TEXT NOT NULL, PRIMARY KEY(friend_id,asset_id), '
    'FOREIGN KEY(friend_id,asset_id) REFERENCES sticker_assets(friend_id,asset_id), '
    "CHECK((analysis_status='COMPLETED' AND unknown_reason IS NULL) OR "
    "(analysis_status='UNKNOWN' AND unknown_reason IS NOT NULL)))",
    'CREATE INDEX sticker_metadata_kind ON sticker_metadata(friend_id,sticker_kind,analysis_status)',
)
STICKER_EXAMPLE_STATEMENTS = (
    'CREATE TABLE sticker_examples ('
    'friend_id TEXT NOT NULL, '
    "subject TEXT NOT NULL CHECK(subject IN ('BOT_PERSONA','FRIEND')), "
    'episode_id TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, '
    'message_id TEXT NOT NULL, asset_id TEXT NOT NULL, pair_id TEXT, '
    'source_content_hash TEXT NOT NULL '
    "CHECK(length(source_content_hash)=64 AND source_content_hash NOT GLOB '*[^0-9a-f]*'), "
    'created_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,subject,episode_id,message_id), '
    'FOREIGN KEY(friend_id,session_id,turn_id) REFERENCES dialogue_turns(friend_id,session_id,id), '
    'FOREIGN KEY(friend_id,asset_id) REFERENCES sticker_assets(friend_id,asset_id), '
    'FOREIGN KEY(friend_id,pair_id) REFERENCES dialogue_pairs(friend_id,id), '
    "CHECK((subject='BOT_PERSONA' AND pair_id IS NOT NULL AND episode_id=pair_id) OR "
    "(subject='FRIEND' AND pair_id IS NULL)))",
    'CREATE INDEX sticker_examples_asset ON sticker_examples(friend_id,asset_id)',
)
SOURCE_STATEMENTS = (
    'CREATE TABLE source_import_tombstones ('
    'friend_id TEXT NOT NULL, import_id TEXT NOT NULL, revoked_at TEXT NOT NULL, '
    'PRIMARY KEY(friend_id,import_id), '
    'FOREIGN KEY(friend_id,import_id) REFERENCES dataset_imports(friend_id,import_id))',
    'ALTER TABLE analysis_jobs ADD COLUMN source_token TEXT',
) + state_statements(PERSONA_TABLES)
FEEDBACK_STATEMENTS = (
    'CREATE TABLE persona_feedback ('
    'friend_id TEXT NOT NULL, '
    'comparison_id TEXT NOT NULL CHECK(length(comparison_id) BETWEEN 1 AND 128 AND length(trim(comparison_id))>0), '
    'a_version TEXT NOT NULL CHECK(length(a_version) BETWEEN 1 AND 128 AND length(trim(a_version))>0), '
    'b_version TEXT NOT NULL CHECK(length(b_version) BETWEEN 1 AND 128 AND length(trim(b_version))>0), '
    "comparison_kind TEXT NOT NULL CHECK(comparison_kind IN ('context_only','offline_reply')), "
    "choice TEXT NOT NULL CHECK(choice IN ('A','B','similar','neither')), "
    "source_type TEXT NOT NULL CHECK(source_type='HUMAN_SUBJECTIVE'), "
    'created_at TEXT NOT NULL CHECK(length(created_at)>0), '
    'PRIMARY KEY(friend_id,comparison_id), '
    'FOREIGN KEY(friend_id) REFERENCES scope_state(friend_id))',
    'CREATE INDEX persona_feedback_recent ON persona_feedback(friend_id,created_at DESC,comparison_id)',
)
MIGRATIONS = ((1, SCHEMA_STATEMENTS), (2, REGISTRY_STATEMENTS),
              (3, IMPORT_STATEMENTS), (4, DIALOGUE_STATEMENTS), (5, PAIR_STATEMENTS),
              (6, ANALYSIS_STATEMENTS), (7, EVIDENCE_STATEMENTS), (8, CURATION_STATEMENTS),
              (9, JOB_STATEMENTS), (10, PACK_BINDING_STATEMENTS), (11, INTEREST_STATEMENTS),
              (12, TOPIC_STATEMENTS), (13, INDEX_STATEMENTS), (14, ENTITY_STATEMENTS),
              (15, STICKER_STATEMENTS), (16, STICKER_EXAMPLE_STATEMENTS),
              (17, SOURCE_STATEMENTS), (18, FEEDBACK_STATEMENTS))


def migration_hash(statements):
    return hashlib.sha256('\n'.join(statements).encode('utf-8')).hexdigest()


class PersonaStore:
    def __init__(self, friend_id, friend_dir, readonly=False, *, project_root=None):
        root = project_root if project_root is not None else os.environ.get(
            'QQ_BRIDGE_HOME', Path(__file__).resolve().parent)
        self.directory = validate_friend_directory(friend_id, friend_dir, root)
        self.friend_id, self.readonly = friend_id, readonly
        self.backup_path = None
        self.path = self.directory / 'persona.sqlite3'
        if self.path.resolve() != self.path:
            raise ScopeMismatch('sidecar path redirects outside its friend directory')
        history = self.directory / 'history.sqlite3'
        if self.path.exists() and history.exists() and self.path.samefile(history):
            raise ScopeMismatch('sidecar cannot reference the history database')
        if readonly and not self.path.is_file():
            raise Unavailable('persona sidecar is absent')
        # P1 uses DELETE journals. Reject WAL without opening its shared-memory files.
        if self.path.is_file():
            with self.path.open('rb') as source:
                header = source.read(20)
            if ((header.startswith(b'SQLite format 3\x00') and header[18:20] != b'\x01\x01') or
                    any(Path(str(self.path) + suffix).exists() for suffix in ('-wal', '-shm'))):
                raise Unavailable('P1 sidecar requires a stable DELETE journal database')
            with closing(self._connect(True)) as existing:
                applied = self._check(existing, allow_empty=not readonly, allow_upgrade=not readonly)
                if not readonly and applied and set(applied) != {v for v, _ in MIGRATIONS}:
                    self.backup_path = self._backup_before_upgrade(existing, applied)
        if not readonly:
            self.directory.mkdir(parents=True, exist_ok=True)
        self.db = self._connect(readonly)
        try:
            if readonly:
                self._check(self.db)
            else:
                self._migrate()
        except Exception:
            self.db.close()
            raise

    def _connect(self, readonly):
        mode = 'ro' if readonly else 'rwc'
        db = sqlite3.connect(self.path.as_uri() + '?mode=' + mode, uri=True,
                             timeout=10, isolation_level=None, factory=SourceConnection)
        if not readonly and any(version == 17 for version, _ in MIGRATIONS):
            db.bind_source(self.path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        if readonly:
            db.execute('PRAGMA query_only=ON')
        return db

    def _backup_before_upgrade(self, existing, applied):
        latest = max(version for version, _ in MIGRATIONS)
        descriptor, name = tempfile.mkstemp(
            prefix=f'persona-schema-v{max(applied)}-before-v{latest}-',
            suffix='.backup.sqlite3', dir=self.directory)
        os.close(descriptor)
        destination = Path(name)
        try:
            with closing(sqlite3.connect(destination)) as backup:
                existing.backup(backup)
            return destination
        except Exception:
            destination.unlink()
            raise

    def _check(self, db, allow_empty=False, allow_upgrade=False):
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables and allow_empty:
            return {}
        if not {'schema_migrations', 'scope_state', 'artifact_versions'} <= tables:
            raise SchemaMismatch('sidecar has incomplete or unsupported schema')
        try:
            scopes = [row[0] for row in db.execute('SELECT friend_id FROM scope_state')]
            if scopes != [self.friend_id]:
                raise ScopeMismatch('existing sidecar belongs to another scope')
            applied = dict(db.execute('SELECT version,migration_hash FROM schema_migrations'))
            expected = {version: migration_hash(sql) for version, sql in MIGRATIONS}
            versions = sorted(applied)
            if (not versions or versions != list(range(1, max(versions) + 1)) or
                    not set(applied) <= set(expected) or
                    not allow_upgrade and set(applied) != set(expected) or any(
                        applied[version] != expected[version] for version in applied)):
                raise SchemaMismatch('migration version or fixed SQL hash differs')
            if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ScopeMismatch('catalog contains cross-scope references')
        except sqlite3.DatabaseError as exc:
            raise SchemaMismatch('sidecar schema cannot be read') from exc
        return applied

    def _migrate(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            applied = self._check(self.db, allow_empty=True, allow_upgrade=True)
            for version, statements in MIGRATIONS:
                if version in applied:
                    continue
                for sql in statements:
                    self.db.execute(sql)
                if version == 1:
                    self.db.execute('INSERT INTO scope_state VALUES(?,0,NULL)', (self.friend_id,))
                self.db.execute('INSERT INTO schema_migrations VALUES(?,?,?)',
                                (version, migration_hash(statements),
                                 datetime.now().astimezone().isoformat()))
            self._check(self.db)
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    @property
    @source_read
    def revision(self):
        return self.db.execute('SELECT revision FROM scope_state WHERE friend_id=?',
                               (self.friend_id,)).fetchone()[0]

    @source_read
    def artifacts(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM artifact_versions WHERE friend_id=? ORDER BY created_at,id',
            (self.friend_id,))]

    def write_batch(self, expected_revision, records, checkpoint=None):
        """CAS-update catalog only; checkpoint may set pending_publication_id.

        This does not validate/publish JSON or provide a Job checkpoint store.
        """
        if self.readonly:
            raise Unavailable('persona sidecar is read-only')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('expected_revision must be a nonnegative integer')
        fields = ('friend_id', 'id', 'state', 'manifest_relpath', 'manifest_sha256', 'created_at')
        values = []
        for record in records:
            if set(record) != set(fields):
                raise ValueError('only artifact catalog records are supported')
            if record['friend_id'] != self.friend_id:
                raise ScopeMismatch('artifact belongs to another friend')
            if not all(isinstance(record[key], str) and record[key] for key in fields):
                raise ValueError('artifact fields must be nonempty strings')
            if record['state'] not in {'STAGED', 'COMPLETED', 'INVALID'}:
                raise ValueError('invalid artifact state')
            if not re.fullmatch('[0-9a-f]{64}', record['manifest_sha256']):
                raise ValueError('manifest hash must be SHA256')
            relative = Path(record['manifest_relpath'])
            if not relative.name or relative.drive or relative.is_absolute() or '..' in relative.parts or (
                    self.directory / relative).resolve().is_relative_to(self.directory) is False:
                raise ScopeMismatch('manifest path leaves the friend directory')
            values.append(tuple(record[key] for key in fields))
        if checkpoint is not None and (set(checkpoint) != {'pending_publication_id'} or
                checkpoint['pending_publication_id'] is not None and
                not isinstance(checkpoint['pending_publication_id'], str)):
            raise ValueError('checkpoint only accepts pending_publication_id')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._check(self.db)
            if self.revision != expected_revision:
                raise Conflict('catalog revision changed')
            self.db.executemany(
                'INSERT INTO artifact_versions VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(friend_id,id) DO UPDATE SET state=excluded.state, '
                'manifest_relpath=excluded.manifest_relpath, '
                'manifest_sha256=excluded.manifest_sha256, created_at=excluded.created_at', values)
            if checkpoint is not None:
                self.db.execute('UPDATE scope_state SET pending_publication_id=? WHERE friend_id=?',
                                (checkpoint['pending_publication_id'], self.friend_id))
            self.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                            (self.friend_id,))
            self.db.execute('COMMIT')
            return expected_revision + 1
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()
