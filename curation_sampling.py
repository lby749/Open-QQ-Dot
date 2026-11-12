"""Bounded local weak-label stage for public pairs and private relationship events."""
from dataclasses import dataclass, replace
import hashlib
import heapq
import json
import os
import sqlite3
import tempfile
from contextlib import closing

from dialogue_pairs import DialoguePair
from persona_analysis_input import RelationshipEvent
from persona_types import (AnalysisCallPolicy, AnalysisPurpose, DataScope, InvalidSource,
                           ProviderKind, SourceOrigin)


_TARGET_ORIGINS = {SourceOrigin.REAL_RELATIONSHIP, SourceOrigin.REAL_SINGLE_SPEAKER,
                   SourceOrigin.REAL_PERSONA_POOL, SourceOrigin.PUBLIC_DATASET}
_LABEL_FIELDS = {'intent', 'emotion', 'tone', 'initiative'}


@dataclass(frozen=True)
class WeakSample:
    sample: object
    labels: dict | None
    needs_review: bool
    unknown: bool
    status: str
    reason: str
    model_fingerprint: str | None
    source_origins: tuple[str, ...]
    owner_friend_id: str | None
    subject: str


class LocalWeakLabeler:
    def __init__(self, classifier=None, *, batch_size=16):
        if type(batch_size) is not int or not 1 <= batch_size <= 64:
            raise ValueError('batch_size must be between 1 and 64')
        self.classifier = classifier
        self.batch_size = batch_size

    def iter_label(self, samples, *, limit=256):
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError('limit must be between 1 and 512')

        def stream():
            rows, fingerprint = self._sample(samples, limit)
            selected = [sample for sample, _ in rows]
            metadata = [details for _, details in rows]
            if not selected:
                return
            texts, contexts = [], []
            for sample in selected:
                text, context = _input(sample)
                texts.append(text)
                contexts.append(context)
            for start in range(0, len(selected), self.batch_size):
                end = start + self.batch_size
                valid_indexes = [i for i in range(start, min(end, len(selected))) if texts[i]]
                reviews = {}
                error = 'local_classification_failed'
                if self.classifier is not None and not fingerprint:
                    error = 'model_fingerprint_unavailable'
                if fingerprint and self.classifier is not None and valid_indexes:
                    try:
                        values = self.classifier.classify_batch_with_review(
                            [texts[i] for i in valid_indexes],
                            contexts=[contexts[i] for i in valid_indexes])
                        if len(values) != len(valid_indexes):
                            raise ValueError('classifier batch length differs')
                        for value in values:
                            _validate_review(value)
                        reviews = dict(zip(valid_indexes, values))
                    except Exception:
                        reviews = {}
                for index in range(start, min(end, len(selected))):
                    review = reviews.get(index)
                    item = selected[index]
                    origins, owner, subject = metadata[index]
                    if review is None:
                        reason = ('no_explicit_text' if not texts[index] else
                                  'classifier_unavailable' if self.classifier is None else error)
                        yield WeakSample(item, None, True, True, 'UNKNOWN', reason,
                            fingerprint, origins, owner, subject)
                    else:
                        yield WeakSample(item, dict(review['labels']), review['needs_review'],
                            False, 'WEAK_PROVISIONAL', review['reason'], fingerprint,
                            origins, owner, subject)
        return stream()

    def _sample(self, samples, limit):
        handle = tempfile.NamedTemporaryFile(prefix='curation-stage2-', suffix='.sqlite', delete=False)
        path = handle.name
        handle.close()
        try:
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE seen (identity TEXT PRIMARY KEY)')
                heap, ordinal = [], 0
                for sample in samples:
                    refs, owner, origins, subject = _validate_source(sample)
                    AnalysisCallPolicy.validate(AnalysisPurpose.DISTILLATION,
                                                ProviderKind.LOCAL, refs)
                    identity = _identity(sample, owner)
                    inserted = db.execute('INSERT OR IGNORE INTO seen VALUES (?)',
                                          (identity,)).rowcount
                    if not inserted:
                        continue
                    priority = int.from_bytes(hashlib.sha256(
                        ('CURATION-003-v1:' + identity).encode('utf-8')).digest(), 'big')
                    entry = (-priority, ordinal, sample, (origins, owner, subject))
                    if len(heap) < limit:
                        heapq.heappush(heap, entry)
                    elif priority < -heap[0][0]:
                        heapq.heapreplace(heap, entry)
                    ordinal += 1
            selected = [row[2:] for row in heap]
            selected.sort(key=lambda row: (int.from_bytes(hashlib.sha256(
                ('CURATION-003-v1:' + _identity(row[0], row[1][1])).encode('utf-8')).digest(), 'big'),
                _identity(row[0], row[1][1])))
            return [(sample, details) for sample, details in selected], _model_fingerprint(self.classifier)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def _validate_source(sample):
    if isinstance(sample, DialoguePair):
        refs, subject = sample.source_refs, 'BOT_PERSONA'
        if not sample.target_bubbles or any(
                message.speaker_id != sample.target_speaker_id or
                message.speaker_role.strip().lower() not in
                {'human', 'user', 'friend', 'contact', 'person', 'target', 'me'} or
                message.source.origin not in _TARGET_ORIGINS or message.source not in refs
                for message in sample.target_bubbles):
            raise InvalidSource('Pair has no explicit human Target provenance')
    elif isinstance(sample, RelationshipEvent):
        refs, subject = sample.source_refs, 'DYAD'
        if not sample.friend_id or not sample.inbound_message_ids:
            raise InvalidSource('relationship event lacks friend or inbound identity')
        if any(span.message_id not in sample.inbound_message_ids or span.start < 0 or
               span.end < span.start or span.end - span.start != len(span.text)
               for span in sample.explicit_spans):
            raise InvalidSource('relationship evidence span is outside inbound messages')
        if not any(ref.origin == SourceOrigin.REAL_RELATIONSHIP for ref in refs):
            raise InvalidSource('relationship event has no real relationship provenance')
    else:
        raise InvalidSource('Stage2 accepts DialoguePair or RelationshipEvent only')
    private_owners = {ref.friend_id for ref in refs if ref.data_scope == DataScope.PRIVATE_FRIEND}
    owner = next(iter(private_owners)) if len(private_owners) == 1 else None
    origins = tuple(sorted({ref.origin.value for ref in refs if ref.origin is not None}))
    if isinstance(sample, RelationshipEvent) and (owner != sample.friend_id or any(
            ref.data_scope != DataScope.PRIVATE_FRIEND for ref in refs)):
        raise InvalidSource('relationship events must retain their private friend scope')
    if isinstance(sample, DialoguePair) and owner not in (None, sample.friend_id):
        raise InvalidSource('Pair owner differs from private source owner')
    return refs, owner, origins, subject


def _identity(sample, owner):
    sample_id = sample.id if isinstance(sample, DialoguePair) else sample.event_id
    return json.dumps([owner, sample_id], ensure_ascii=False, separators=(',', ':'))


def _input(sample):
    if isinstance(sample, DialoguePair):
        text = '\n'.join(message.text for message in sample.target_bubbles if message.text.strip())
        context = [message.text for turn in sample.context_turns for message in turn.bubbles]
        context.extend(message.text for message in sample.user_bubbles if message.text.strip())
        return text, context[-3:]
    spans = tuple(span for span in sample.explicit_spans
                  if span.message_id in sample.inbound_message_ids and span.text.strip())
    return '\n'.join(span.text for span in spans), []


def _validate_review(value):
    from message_classifier import ENUMS, REVIEW_REASONS
    if (not isinstance(value, dict) or set(value) != {'labels', 'needs_review', 'reason'} or
            not isinstance(value['labels'], dict) or set(value['labels']) != _LABEL_FIELDS or
            any(value['labels'][key] not in ENUMS[key] for key in _LABEL_FIELDS) or
            type(value['needs_review']) is not bool or not isinstance(value['reason'], str) or
            value['reason'] not in REVIEW_REASONS or
            (value['needs_review'] == (value['reason'] == 'clear'))):
        raise ValueError('local classifier review output is invalid')


def _model_fingerprint(classifier):
    runtime = getattr(classifier, 'runtime', None)
    if not isinstance(runtime, dict):
        return None
    model = runtime.get('model')
    assets = runtime.get('assets')
    if not isinstance(assets, (list, tuple)):
        return None
    asset = next((item for item in assets
                  if isinstance(item, dict) and item.get('name') == model), None)
    if not model or not isinstance(asset, dict) or not asset.get('sha256'):
        return None
    import message_classifier
    value = dict(runtime_version=runtime.get('version'), model_repo=runtime.get('model_repo'),
        model_revision=runtime.get('model_revision'), base_model=runtime.get('base_model'),
        base_model_observed_revision=runtime.get('base_model_observed_revision'), model=model,
        model_sha256=asset['sha256'], system=message_classifier.SYSTEM,
        generation=message_classifier.GENERATION, schema=message_classifier.DETAIL_SCHEMA)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# CURATION-004. This accepts the public return value of PersonaJudge, rather
# than CURATION-003 WeakSample values, so local provisional labels cannot stand
# in for completed cloud-judge evidence.
@dataclass(frozen=True)
class JudgedPair:
    pair: object
    judge_result: dict
    scene: str | None = None
    reaction: str | None = None
    tag_source_ref: object | None = None


@dataclass(frozen=True)
class SelectedPair:
    pair: DialoguePair
    run_id: str
    scores: dict
    quality_score: float
    scene: str
    reaction: str
    speaker_key: str
    duplicate_cluster: str
    partitions: tuple[dict, ...]


@dataclass(frozen=True)
class DiversitySelection:
    samples: tuple[SelectedPair, ...]
    counts: dict


class DiversitySampler:
    """Bounded, local selection over completed, non-review PersonaJudge rows."""

    def select(self, candidates, *, partition_by_source, limit=64,
               candidate_limit=None, max_per_cluster=1):
        from collections import Counter
        import math
        import re
        import tempfile
        import unicodedata
        from contextlib import closing
        from pathlib import Path

        from persona_judge import PERSONA_FIELDS, PROMPT_VERSION
        from persona_types import DataScope, SourceOrigin

        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError('limit must be between 1 and 512')
        if candidate_limit is None:
            candidate_limit = min(4096, max(limit, limit * 4))
        if type(candidate_limit) is not int or not limit <= candidate_limit <= 4096:
            raise ValueError('candidate_limit must be between limit and 4096')
        if type(max_per_cluster) is not int or not 1 <= max_per_cluster <= 16:
            raise ValueError('max_per_cluster must be between 1 and 16')
        if not isinstance(partition_by_source, dict):
            raise ValueError('partition_by_source must be an explicit locator mapping')

        counts = Counter()
        heap = []
        ordinal = 0

        def source_keys(ref):
            return (ref, (ref.dataset_id, ref.import_id, ref.record_id))

        def source_partition(ref):
            value = None
            for key in source_keys(ref):
                if key in partition_by_source:
                    value = partition_by_source[key]
                    break
            if not isinstance(value, dict):
                return None, 'partition_missing'
            split, held = value.get('split'), value.get('is_holdout')
            if not isinstance(split, str) or not split.strip() or split.strip().lower() in {
                    'unknown', 'none', 'null', 'n/a', '?'} or type(held) is not bool:
                return None, 'partition_unknown'
            tokens = set(re.findall(r'[a-z0-9]+', split.lower().replace('-', '_')))
            if held or tokens.intersection({'test', 'eval', 'evaluation', 'holdout', 'heldout'}):
                return None, 'holdout_excluded'
            source_hash = value.get('source_hash')
            if source_hash is not None and (not isinstance(source_hash, str) or not source_hash.strip()):
                return None, 'partition_unknown'
            return {'dataset_id': ref.dataset_id, 'import_id': ref.import_id,
                    'record_id': ref.record_id, 'split': split.strip(),
                    'is_holdout': held, 'source_hash': source_hash}, None

        def judge_item(candidate):
            result = candidate.judge_result
            if (not isinstance(result, dict) or result.get('status') != 'COMPLETED' or
                    result.get('prompt_version') != PROMPT_VERSION or
                    not isinstance(result.get('run_id'), str) or not result['run_id'] or
                    not isinstance(result.get('model'), str) or not result['model'] or
                    not isinstance(result.get('prompt_hash'), str) or not result['prompt_hash'] or
                    result.get('error_code') is not None or
                    not isinstance(result.get('usage'), dict) or
                    result['usage'].get('status') != 'known' or
                    any(type(result['usage'].get(key)) is not int or result['usage'][key] < 0
                        for key in ('input_tokens', 'output_tokens', 'total_tokens')) or
                    result['usage']['total_tokens'] != (result['usage']['input_tokens'] +
                                                        result['usage']['output_tokens']) or
                    not isinstance(result.get('results'), list)):
                return None, 'judge_not_completed'
            matches = [item for item in result['results']
                       if isinstance(item, dict) and item.get('pair_id') == candidate.pair.id]
            if len(matches) != 1:
                return None, 'judge_result_missing_or_duplicate'
            item = matches[0]
            if item.get('needs_review') is not False:
                return None, 'judge_needs_review'
            scores = item.get('scores')
            quality = item.get('quality_score')
            if (not isinstance(scores, dict) or set(scores) != set(PERSONA_FIELDS) or
                    any(type(score) not in (int, float) or not math.isfinite(score) or
                        not 0 <= score <= 1 for score in scores.values()) or
                    type(quality) not in (int, float) or not math.isfinite(quality) or
                    not 0 <= quality <= 1):
                return None, 'judge_scores_invalid'
            return (result, item), None

        def public_pair(pair):
            if not isinstance(pair, DialoguePair):
                return 'not_dialogue_pair'
            refs = tuple(pair.source_refs)
            if not refs:
                return 'source_missing'
            if any(ref.data_scope != DataScope.PUBLIC_DATASET or ref.friend_id is not None or
                   ref.model_analysis_allowed is not True or not ref.license_id for ref in refs):
                return 'source_not_cloudsafe_public'
            allowed = {SourceOrigin.PUBLIC_DATASET, SourceOrigin.REAL_SINGLE_SPEAKER,
                       SourceOrigin.REAL_PERSONA_POOL}
            if (not pair.target_bubbles or any(
                    message.speaker_id != pair.target_speaker_id or
                    message.speaker_role.strip().lower() not in
                    {'human', 'user', 'friend', 'contact', 'person', 'target', 'me'} or
                    message.source not in refs or message.source.origin not in allowed
                    for message in pair.target_bubbles)):
                return 'target_provenance_invalid'
            if any(message.source not in refs for message in
                   (*pair.user_bubbles, *(bubble for turn in pair.context_turns
                                          for bubble in turn.bubbles))):
                return 'context_provenance_invalid'
            return None

        def normalize(text):
            value = unicodedata.normalize('NFKC', text).casefold()
            return ''.join(char for char in value if char.isalnum())

        def simhash(text):
            value = normalize(text)
            if not value:
                return None
            features = {value[i:i + 3] for i in range(max(1, len(value) - 2))}
            if len(value) < 3:
                features = {value}
            weights = [0] * 64
            for feature in features:
                hashed = int.from_bytes(hashlib.sha256(feature.encode('utf-8')).digest()[:8], 'big')
                for bit in range(64):
                    weights[bit] += 1 if hashed & (1 << bit) else -1
            return sum((1 << bit) for bit, weight in enumerate(weights) if weight >= 0)

        temp = tempfile.NamedTemporaryFile(prefix='curation-stage4-', suffix='.sqlite', delete=False)
        db_path = Path(temp.name)
        temp.close()
        try:
            with closing(sqlite3.connect(db_path)) as db:
                db.executescript('''
                  CREATE TABLE seen (pair_id TEXT PRIMARY KEY);
                  CREATE TABLE clusters (id INTEGER PRIMARY KEY, item_count INTEGER NOT NULL);
                  CREATE TABLE signatures (id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL, value TEXT NOT NULL);
                  CREATE TABLE bands (band INTEGER NOT NULL, value INTEGER NOT NULL, signature_id INTEGER NOT NULL,
                    PRIMARY KEY(band,value,signature_id));
                  CREATE INDEX band_lookup ON bands(band,value);
                ''')

                for candidate in candidates:
                    counts['input'] += 1
                    if not isinstance(candidate, JudgedPair):
                        counts['rejected_candidate_type'] += 1
                        continue
                    if not isinstance(candidate.pair, DialoguePair):
                        counts['rejected_not_dialogue_pair'] += 1
                        continue
                    issue = public_pair(candidate.pair)
                    if issue:
                        counts['rejected_' + issue] += 1
                        continue
                    result_and_item, issue = judge_item(candidate)
                    if issue:
                        counts['rejected_' + issue] += 1
                        continue
                    result, item = result_and_item

                    if ((candidate.scene is not None or candidate.reaction is not None) and
                            (candidate.tag_source_ref is None or
                             candidate.tag_source_ref not in candidate.pair.source_refs)):
                        counts['rejected_tag_provenance_invalid'] += 1
                        continue
                    scene = candidate.scene if isinstance(candidate.scene, str) and candidate.scene.strip() else 'unknown'
                    reaction = (candidate.reaction if isinstance(candidate.reaction, str) and
                                candidate.reaction.strip() else 'unknown')

                    partitions, split_issue = [], None
                    for ref in candidate.pair.source_refs:
                        partition, split_issue = source_partition(ref)
                        if split_issue:
                            break
                        partitions.append(partition)
                    if split_issue:
                        counts['rejected_' + split_issue] += 1
                        continue
                    splits = {row['split'].lower() for row in partitions}
                    if len(splits) != 1:
                        counts['rejected_mixed_split'] += 1
                        continue

                    target_text = '\n'.join(message.text for message in candidate.pair.target_bubbles
                                            if message.text.strip())
                    signature = simhash(target_text)
                    if signature is None:
                        counts['rejected_empty_target'] += 1
                        continue
                    duplicate = db.execute('INSERT OR IGNORE INTO seen VALUES (?)',
                                           (candidate.pair.id,)).rowcount == 0
                    if duplicate:
                        counts['rejected_duplicate_pair'] += 1
                        continue

                    # Four 16-bit bands guarantee a shared band for any pair at
                    # Hamming distance <= 3; full signatures decide the cluster.
                    close_clusters = set()
                    for band in range(4):
                        value = (signature >> (band * 16)) & 0xffff
                        for cluster_id, other in db.execute(
                                'SELECT signatures.cluster_id,signatures.value FROM signatures '
                                'JOIN bands ON bands.signature_id=signatures.id '
                                'WHERE bands.band=? AND bands.value=?', (band, value)):
                            if (signature ^ int(other, 16)).bit_count() <= 3:
                                close_clusters.add(cluster_id)
                    if close_clusters:
                        cluster_id = min(close_clusters)
                        for other_id in close_clusters - {cluster_id}:
                            old_count = db.execute('SELECT item_count FROM clusters WHERE id=?',
                                                   (other_id,)).fetchone()[0]
                            db.execute('UPDATE signatures SET cluster_id=? WHERE cluster_id=?',
                                       (cluster_id, other_id))
                            db.execute('UPDATE clusters SET item_count=item_count+? WHERE id=?',
                                       (old_count, cluster_id))
                            db.execute('DELETE FROM clusters WHERE id=?', (other_id,))
                    else:
                        cluster_id = db.execute('INSERT INTO clusters(item_count) VALUES(0)').lastrowid
                    cluster_count = db.execute('SELECT item_count FROM clusters WHERE id=?',
                                               (cluster_id,)).fetchone()[0]
                    signature_id = db.execute('INSERT INTO signatures(cluster_id,value) VALUES(?,?)',
                        (cluster_id, f'{signature:016x}')).lastrowid
                    for band in range(4):
                        db.execute('INSERT INTO bands VALUES(?,?,?)',
                            (band, (signature >> (band * 16)) & 0xffff, signature_id))
                    if cluster_count >= max_per_cluster:
                        counts['rejected_near_duplicate_cluster_cap'] += 1
                        continue
                    db.execute('UPDATE clusters SET item_count=item_count+1 WHERE id=?', (cluster_id,))

                    counts['eligible_after_judge_source_partition'] += 1
                    if scene == 'unknown':
                        counts['scene_unknown'] += 1
                    if reaction == 'unknown':
                        counts['reaction_unknown'] += 1
                    target_scope = ','.join(f'{dataset}:{import_id}' for dataset, import_id in
                        sorted({(ref.dataset_id, ref.import_id)
                                for ref in candidate.pair.source_refs}))
                    speaker_key = (f'{target_scope}:{candidate.pair.session_id}:'
                                   f'{candidate.pair.target_speaker_id}')
                    identity = candidate.pair.id + '|' + result['run_id']
                    priority = int.from_bytes(hashlib.sha256(
                        ('CURATION-004-v1:' + identity).encode('utf-8')).digest(), 'big')
                    picked = SelectedPair(candidate.pair, result['run_id'], dict(item['scores']),
                        float(item['quality_score']), scene, reaction, speaker_key,
                        str(cluster_id), tuple(partitions))
                    entry = (-priority, ordinal, picked, signature_id,
                             float(item['scores']['overall_persona_match']))
                    if len(heap) < candidate_limit:
                        heapq.heappush(heap, entry)
                    elif priority < -heap[0][0]:
                        heapq.heapreplace(heap, entry)
                    ordinal += 1
                canonical_heap = []
                for entry in heap:
                    cluster_id = db.execute('SELECT cluster_id FROM signatures WHERE id=?',
                                            (entry[3],)).fetchone()[0]
                    canonical_heap.append((entry[0], entry[1],
                        replace(entry[2], duplicate_cluster=str(cluster_id)), entry[3], entry[4]))
                heap = canonical_heap
                heapq.heapify(heap)
            pool = [entry[2] for entry in heap]
            counts['candidate_pool'] = len(pool)
            counts['selected_pre_diversity'] = counts['eligible_after_judge_source_partition']
            chosen = []
            scene_counts, speaker_counts, reaction_counts = Counter(), Counter(), Counter()
            cluster_counts = Counter()
            priorities = {entry[2].pair.id: -entry[0] for entry in heap}
            while pool and len(chosen) < limit:
                eligible = [sample for sample in pool
                            if cluster_counts[sample.duplicate_cluster] < max_per_cluster]
                if not eligible:
                    break
                sample = min(eligible, key=lambda row: (
                    scene_counts[row.scene], speaker_counts[row.speaker_key],
                    reaction_counts[row.reaction], cluster_counts[row.duplicate_cluster],
                    -row.quality_score, -row.scores['overall_persona_match'],
                    priorities[row.pair.id], row.pair.id))
                pool.remove(sample)
                chosen.append(sample)
                scene_counts[sample.scene] += 1
                speaker_counts[sample.speaker_key] += 1
                reaction_counts[sample.reaction] += 1
                cluster_counts[sample.duplicate_cluster] += 1

            counts['selected'] = len(chosen)
            counts['selected_scene_coverage'] = dict(sorted(scene_counts.items()))
            counts['selected_speaker_coverage'] = dict(sorted(speaker_counts.items()))
            counts['selected_reaction_coverage'] = dict(sorted(reaction_counts.items()))
            counts['selected_source_imports'] = dict(sorted(Counter(
                f'{ref.dataset_id}:{ref.import_id}' for sample in chosen
                for ref in sample.pair.source_refs).items()))
            return DiversitySelection(tuple(chosen), dict(counts))
        finally:
            try:
                os.unlink(db_path)
            except FileNotFoundError:
                pass
