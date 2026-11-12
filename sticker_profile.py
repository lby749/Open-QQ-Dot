"""Friend-local reply statistics with independent, estimated Input-Turn emotion."""
from source_state import snapshot, require_snapshot
from source_state import source_read
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

from dialogue_pairs import PairStore
from dialogue_turns import build_turns
from message_classifier import LocalMessageClassifier, _validate_detail, get_classifier
from dataset_annotation.project_contract import apply_short_message_guard, contract_fingerprint
from persona_analysis_input import AnalysisInputBuilder, _bounded_strings
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import (AnalysisCallPolicy, AnalysisPurpose, DataScope, FriendScope,
                           InvalidSource, ProviderKind, SourceOrigin)


_TYPES = ('text_only', 'text_plus_sticker', 'sticker_only', 'unknown')
_HUMAN = {'human', 'user', 'friend', 'contact', 'person', 'target', 'me'}
_ORIGINS = {SourceOrigin.REAL_RELATIONSHIP, SourceOrigin.REAL_SINGLE_SPEAKER,
            SourceOrigin.REAL_PERSONA_POOL}
_SOURCE_IDENTITY = ('dataset_id', 'import_id', 'record_id', 'message_seq', 'speaker_id',
                    'origin', 'data_scope', 'privacy_view', 'friend_id', 'event_time',
                    'recorded_at', 'license_id')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _real(turn, scope):
    return (bool(turn.bubbles) and not turn.speaker_id.lower().startswith('unknown') and
        all(b.speaker_role.lower() in _HUMAN and
        b.source.origin in _ORIGINS and b.source.data_scope == DataScope.PRIVATE_FRIEND and
        b.source.friend_id == scope.friend_id for b in turn.bubbles))


def _text(turn):
    # Image captions/OCR/placeholders, including text fields on media bubbles, are not prose.
    return '\n'.join(b.text for b in turn.bubbles if b.message_type.lower() == 'text' and
                     b.media_id is None and b.text.strip())


class StickerBehaviorProfile:
    def __init__(self, scope, pair_store, *, classifier=None):
        if not isinstance(scope, FriendScope) or not isinstance(pair_store, PairStore) or pair_store.scope != scope:
            raise ScopeMismatch('StickerBehaviorProfile requires the same verified scope and PairStore')
        if classifier is not None and (not callable(getattr(classifier, 'classify_with_review', None)) or
                not isinstance(classifier, LocalMessageClassifier) and getattr(classifier, 'provider_kind', None) != 'LOCAL'):
            raise InvalidSource('emotion classifier must expose a LOCAL review contract')
        self.scope, self.pairs, self.classifier = scope, pair_store, classifier
        self.builder = AnalysisInputBuilder(scope, pair_store)
        self._issued = None

    def _snapshot(self, request):
        self.builder._check_scope()
        sessions = [self.pairs.dialogues.load_session(sid) for sid in request['session_ids']]
        pairs = [dict(r) for sid in request['session_ids'] for r in self.pairs.db.execute(
            'SELECT * FROM dialogue_pairs WHERE friend_id=? AND session_id=? ORDER BY id',
            (self.scope.friend_id, sid))]
        assets = [dict(r) for r in self.pairs.db.execute('SELECT a.*,m.analysis_status,m.sticker_kind,'
            'm.visual_style,m.analysis_version,m.analyzer_id FROM sticker_assets a '
            'LEFT JOIN sticker_metadata m ON a.friend_id=m.friend_id AND a.asset_id=m.asset_id '
            'WHERE a.friend_id=? ORDER BY a.asset_id', (self.scope.friend_id,))]
        return sessions, assets, _hash({'sessions': [asdict(s) for s in sessions], 'pairs': pairs, 'assets': assets})

    @source_read
    def calculate(self, session_ids, target_speaker_ids=(), friend_speaker_ids=()):
        session_ids = _bounded_strings(session_ids, 'session_ids')
        target_speaker_ids = _bounded_strings(target_speaker_ids, 'target_speaker_ids')
        friend_speaker_ids = _bounded_strings(friend_speaker_ids, 'friend_speaker_ids')
        revision = self.pairs.revision
        analysis = self.builder.build(session_ids, target_speaker_ids, friend_speaker_ids)
        request = {'session_ids': list(dict.fromkeys(session_ids)),
                   'target_speaker_ids': list(analysis.sample_info.selected_target_speaker_ids),
                   'friend_speaker_ids': list(analysis.sample_info.selected_friend_speaker_ids)}
        if self.pairs.revision != revision:
            raise Conflict('source changed during sticker input verification')
        sessions, assets, source_hash = self._snapshot(request)
        pairs = {(p.session_id, p.target_turn_id): p for p in analysis.role_pairs}
        events = {e.inbound_message_ids: e for e in analysis.relationship_events}
        units, occurrences, excluded = [], [], Counter()
        selected = set(request['target_speaker_ids']) | set(request['friend_speaker_ids'])
        for session in sessions:
            turns = build_turns(session)
            for index, turn in enumerate(turns):
                pair = pairs.get((session.id, turn.id))
                event = events.get(tuple(b.id for b in turn.bubbles))
                if pair is None and event is None:
                    if turn.speaker_id in selected:
                        excluded['NON_REAL_OR_UNPAIRED'] += 1
                    continue
                if not _real(turn, self.scope):
                    excluded['NOT_PRIVATE_REAL'] += 1
                    continue
                origins = {b.source.origin.value for b in turn.bubbles}
                if len(origins) != 1:
                    excluded['MIXED_ORIGIN'] += 1
                    continue
                subject, episode = ('FRIEND', event.event_id) if event else ('BOT_PERSONA', pair.id)
                kind, media, styles, media_reason, asset_coverage = self._media(turn, assets)
                prior = turns[index - 1] if index else None
                context = []
                for previous in reversed(turns[:max(0, index - 1)]):
                    if _real(previous, self.scope) and _text(previous):
                        context.insert(0, previous)
                        if len(context) == 3:
                            break
                label = self._emotion(prior, context)
                unit = {'subject': subject, 'episode_id': episode, 'session_id': session.id,
                    'turn_id': turn.id, 'pair_id': None if event else pair.id,
                    'speaker_id': turn.speaker_id,
                    'data_scope': 'PRIVATE_FRIEND', 'source_origin': next(iter(origins)),
                    'send_type': kind, 'send_type_unknown_reason': media_reason,
                    'emotion_label': label, 'visual_styles': sorted(styles), 'asset_coverage': asset_coverage}
                units.append(unit)
                for bubble, asset in media:
                    occurrences.append({'friend_id': self.scope.friend_id, 'subject': subject,
                        'episode_id': episode, 'session_id': session.id, 'turn_id': turn.id,
                        'message_id': bubble.id, 'asset_id': asset['asset_id'], 'pair_id': unit['pair_id'],
                        'source_content_hash': _hash({'turn': asdict(turn), 'asset': asset})})
        if self.pairs.revision != revision or self._snapshot(request)[2] != source_hash:
            raise Conflict('source changed during sticker profile calculation')
        groups = _aggregate(units)
        profile = {'schema_version': 'friend-sticker-profile-v1', 'friend_id': self.scope.friend_id,
            'scope': 'PRIVATE_FRIEND', 'condition_subject': 'preceding_input_turn',
            'label_method': 'local_qwen_estimate', 'label_quality': 'uncalibrated; not ground truth',
            'status': 'estimated' if any(e['status'] == 'estimated' for g in groups for e in g['emotion_groups']) else 'unknown',
            'source_revision': revision, 'source_content_hash': source_hash, 'request': request,
            'sample_info': {'reply_unit_count': len(units), 'excluded': dict(excluded)},
            'groups': groups, 'reply_units': units, 'occurrences': occurrences,
            'low_sample_gate': {'evaluable_replies': 20, 'independent_sessions': 2}}
        self._issued = _hash(profile)
        return profile

    def _emotion(self, prior, context):
        result = {'condition_subject': 'preceding_input_turn', 'label_method': 'local_qwen_estimate',
            'status': 'unknown', 'emotion': None, 'needs_review': None, 'reason': None,
            'unknown_reason': None, 'input_turn_id': prior.id if prior else None,
            'context_turn_ids': [], 'input_hash': None, 'contract_fingerprint': contract_fingerprint(),
            'model_identity': None, 'truncated': False}
        if prior is None or not _real(prior, self.scope):
            result['unknown_reason'] = 'INPUT_NOT_REAL_OR_MISSING'
            return result
        text = _text(prior)
        real_context = [t for t in context if _real(t, self.scope) and _text(t)][-3:]
        texts = [_text(t) for t in real_context]
        result['context_turn_ids'] = [t.id for t in real_context]
        result['input_hash'] = _hash({'input': asdict(prior), 'context': [asdict(t) for t in real_context],
                                    'following': [], 'window_policy': 'input-turn-text-v1'})
        if not text:
            result['unknown_reason'] = 'EMPTY_INPUT_TEXT'
            return result
        if len(text) > 800 or any(len(value) > 160 for value in texts):
            result.update(unknown_reason='INPUT_TRUNCATED', truncated=True)
            return result
        refs = tuple(dict.fromkeys(b.source for t in (*real_context, prior) for b in t.bubbles))
        AnalysisCallPolicy.validate(AnalysisPurpose.DISTILLATION, ProviderKind.LOCAL, refs)
        try:
            classifier = self.classifier or get_classifier(self.scope.project_root)
            runtime = getattr(classifier, 'runtime', {})
            result['model_identity'] = {k: runtime.get(k) for k in ('base_model', 'model', 'model_revision')}
            result['model_identity']['model_sha256'] = next((a.get('sha256') for a in runtime.get('assets', [])
                if a.get('name') == runtime.get('model')), None)
            detail = _validate_detail(classifier.classify_with_review(text, direction=None,
                context=[{'text': value, 'direction': None, 'seq': t.ordinal}
                         for t, value in zip(real_context, texts)], following=()))
            detail = apply_short_message_guard(text, None, texts, detail)
        except Exception:
            result['unknown_reason'] = 'CLASSIFIER_UNAVAILABLE_OR_INVALID'
            return result
        result.update(needs_review=detail['needs_review'], reason=detail['reason'])
        if detail['needs_review']:
            result['unknown_reason'] = 'NEEDS_REVIEW'
        else:
            result.update(status='estimated', emotion=detail['labels']['emotion'])
        return result

    def _media(self, turn, assets):
        lookup = {a['asset_id']: a for a in assets}
        stickers, unknown, missing, media, styles = 0, 0, Counter(), [], set()
        for bubble in turn.bubbles:
            kind = bubble.message_type.lower()
            if kind == 'text' and bubble.media_id is None:
                continue
            if kind not in ('sticker', 'face', 'image'):
                unknown += 1
                missing['UNSUPPORTED_MEDIA_TYPE'] += 1
                continue
            asset = lookup.get(bubble.media_id)
            reason = 'ASSET_NOT_MAPPED'
            if asset is not None:
                try:
                    sources = json.loads(asset['source'])['source_refs']
                    identity = {key: getattr(bubble.source, key) for key in _SOURCE_IDENTITY}
                    matches = isinstance(sources, list) and any(isinstance(ref, dict) and
                        all(key in ref for key in _SOURCE_IDENTITY) and
                        _json({key: ref[key] for key in _SOURCE_IDENTITY}) == _json(identity) for ref in sources)
                except (TypeError, ValueError, KeyError):
                    matches = False
                if (asset['privacy_scope'] != DataScope.PRIVATE_FRIEND or not asset['valid'] or
                        asset['source_origin'] != bubble.source.origin.value or
                        not matches):
                    reason = 'ASSET_SOURCE_MISMATCH_OR_INVALID'
                elif asset['analysis_status'] != 'COMPLETED' or asset['sticker_kind'] != 'STICKER':
                    reason = 'ASSET_NOT_VERIFIED_STICKER'
                else:
                    reason = None
            if kind in ('sticker', 'face') or reason is None:
                stickers += 1
                if reason is None:
                    media.append((bubble, asset))
                    if asset['visual_style']:
                        styles.add(asset['visual_style'])
                else:
                    missing[reason] += 1
            else:
                unknown += 1
                missing[reason] += 1
        text = bool(_text(turn))
        send_type = ('unknown' if unknown else 'text_plus_sticker' if stickers and text else
                     'sticker_only' if stickers else 'text_only' if text else 'unknown')
        coverage = {'explicit_or_verified_sticker_bubbles': stickers, 'occurrence_count': len(media),
                    'missing_reasons': dict(missing)}
        return send_type, media, styles, 'OTHER_MEDIA_OR_UNKNOWN' if send_type == 'unknown' else None, coverage

    @source_read
    def read_examples(self):
        """Rebuild this friend's occurrence examples from current sources, without inference."""
        self.builder._check_scope()
        rows = [dict(r) for r in self.pairs.db.execute('SELECT * FROM sticker_examples '
            'WHERE friend_id=? ORDER BY subject,episode_id,message_id LIMIT 10001', (self.scope.friend_id,))]
        if len(rows) > 10000:
            raise Unavailable('occurrence read exceeds the 10000 item limit')
        if not rows:
            return []
        revision = self.pairs.revision
        request = {'session_ids': list(dict.fromkeys(r['session_id'] for r in rows))}
        sessions, assets, source_hash = self._snapshot(request)
        turns = {(s.id, t.id): t for s in sessions for t in build_turns(s)}
        pairs = {p.id: p for sid in request['session_ids'] for p in self.pairs.load_pairs(sid)}
        friend_speakers = sorted({turns[(r['session_id'], r['turn_id'])].speaker_id for r in rows
                                 if r['subject'] == 'FRIEND' and (r['session_id'], r['turn_id']) in turns})
        events = self.builder.build(request['session_ids'], friend_speaker_ids=friend_speakers).relationship_events
        media_by_turn = {}
        result = []
        for row in rows:
            turn = turns.get((row['session_id'], row['turn_id']))
            if turn is None or not _real(turn, self.scope):
                raise InvalidSource('occurrence no longer locates a real private Turn')
            if row['subject'] == 'BOT_PERSONA':
                pair = pairs.get(row['pair_id'])
                if pair is None or pair.target_turn_id != turn.id or row['episode_id'] != pair.id:
                    raise InvalidSource('occurrence Pair no longer matches its reply')
            else:
                if not any(e.event_id == row['episode_id'] and e.inbound_message_ids == tuple(b.id for b in turn.bubbles)
                           for e in events):
                    raise InvalidSource('occurrence relationship event no longer matches its Turn')
            if turn.id not in media_by_turn:
                media_by_turn[turn.id] = self._media(turn, assets)[1]
            match = [(b, a) for b, a in media_by_turn[turn.id]
                     if b.id == row['message_id'] and a['asset_id'] == row['asset_id']]
            if len(match) != 1 or row['source_content_hash'] != _hash({'turn': asdict(turn), 'asset': match[0][1]}):
                raise InvalidSource('occurrence asset/source content is stale')
            bubble, asset = match[0]
            result.append({'locator': row, 'scope': 'PRIVATE_FRIEND', 'message': asdict(bubble),
                           'visual_style': asset['visual_style']})
        if self.pairs.revision != revision or self._snapshot(request)[2] != source_hash:
            raise Conflict('occurrence source changed during read')
        return result

    def save(self, profile, expected_revision):
        source_token = snapshot(self.scope)
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidSource('expected_revision must be a nonnegative integer')
        if _hash(profile) != self._issued or profile['friend_id'] != self.scope.friend_id:
            raise InvalidSource('save requires the unchanged result issued by this calculator')
        if self.pairs.dialogues.store.readonly:
            raise Unavailable('readonly sticker projection')
        db = self.pairs.db
        db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            if self._snapshot(profile['request'])[2] != profile['source_content_hash']:
                raise Conflict('sticker profile source changed before save')
            keys = [(u['subject'], u['episode_id']) for u in profile['reply_units']]
            old = [dict(r) for subject, episode in keys for r in db.execute(
                'SELECT * FROM sticker_examples WHERE friend_id=? AND subject=? AND episode_id=? ORDER BY message_id',
                (self.scope.friend_id, subject, episode))]
            fields = ('friend_id', 'subject', 'episode_id', 'session_id', 'turn_id', 'message_id',
                      'asset_id', 'pair_id', 'source_content_hash')
            expected = sorted(_json(r) for r in profile['occurrences'])
            if sorted(_json({k: r[k] for k in fields}) for r in old) == expected:
                db.execute('COMMIT')
                return self.pairs.revision
            if self.pairs.revision != expected_revision or profile['source_revision'] != expected_revision:
                raise Conflict('sticker projection source revision is stale')
            for subject, episode in keys:
                db.execute('DELETE FROM sticker_examples WHERE friend_id=? AND subject=? AND episode_id=?',
                           (self.scope.friend_id, subject, episode))
            now = datetime.now(timezone.utc).isoformat()
            db.executemany('INSERT INTO sticker_examples VALUES(?,?,?,?,?,?,?,?,?,?)',
                          [tuple(r[k] for k in fields) + (now,) for r in profile['occurrences']])
            db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?', (self.scope.friend_id,))
            db.execute('COMMIT')
            return expected_revision + 1
        except Exception:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise


def _aggregate(units):
    groups = defaultdict(list)
    for unit in units:
        groups[(unit['subject'], unit['speaker_id'], unit['data_scope'], unit['source_origin'])].append(unit)
    output = []
    for (subject, speaker, scope, origin), rows in sorted(groups.items()):
        counts = Counter(u['send_type'] for u in rows)
        labeled = [u for u in rows if u['emotion_label']['status'] == 'estimated']
        emotional = defaultdict(list)
        for unit in labeled:
            emotional[unit['emotion_label']['emotion']].append(unit)
        conditions = []
        for emotion, values in sorted(emotional.items()):
            evaluable = [u for u in values if u['send_type'] != 'unknown']
            n = len(evaluable)
            sessions = len({u['session_id'] for u in evaluable})
            raw = Counter(u['send_type'] for u in evaluable)
            styles = Counter(style for u in evaluable for style in u['visual_styles'])
            styled = [u for u in evaluable if u['visual_styles']]
            style_enough = len(styled) >= 20 and len({u['session_id'] for u in styled}) >= 2
            enough = n >= 20 and sessions >= 2
            conditions.append({'emotion': emotion, 'status': 'estimated' if enough else 'unknown',
                'unknown_reason': None if enough else 'LOW_SAMPLE', 'denominator': n,
                'independent_sessions': sessions, 'raw_counts': {k: raw[k] for k in _TYPES[:-1]},
                'rates': {k: raw[k] / n if enough else None for k in _TYPES[:-1]},
                'unknown_send_type_count': sum(u['send_type'] == 'unknown' for u in values),
                'preferred_style_status': 'estimated' if style_enough else 'unknown',
                'style_evaluable_reply_count': len(styled),
                'style_raw_counts': dict(styles),
                'preferred_styles': [{'visual_style': s, 'reply_count': c, 'denominator': len(styled),
                    'rate': c / len(styled)} for s, c in sorted(styles.items())] if enough and style_enough else []})
        unlabeled = [u for u in rows if u['emotion_label']['status'] == 'unknown']
        if unlabeled:
            unknown_counts = Counter(u['send_type'] for u in unlabeled)
            conditions.append({'emotion': None, 'status': 'unknown', 'unknown_reason': 'EMOTION_UNAVAILABLE',
                'denominator': sum(unknown_counts[k] for k in _TYPES[:-1]),
                'independent_sessions': len({u['session_id'] for u in unlabeled if u['send_type'] != 'unknown'}),
                'raw_counts': {k: unknown_counts[k] for k in _TYPES[:-1]},
                'rates': dict.fromkeys(_TYPES[:-1]), 'unknown_send_type_count': unknown_counts['unknown'],
                'preferred_style_status': 'unknown', 'style_evaluable_reply_count': 0,
                'style_raw_counts': {}, 'preferred_styles': []})
        output.append({'subject': subject, 'speaker_id': speaker, 'data_scope': scope,
            'source_origin': origin, 'reply_unit_count': len(rows),
            'raw_counts': {k: counts[k] for k in _TYPES}, 'emotion_groups': conditions,
            'emotion_coverage': {'labeled': len(labeled), 'denominator': len(rows),
                'rate': len(labeled) / len(rows) if rows else None,
                'by_send_type': {k: {'labeled': sum(u['send_type'] == k for u in labeled),
                    'denominator': counts[k], 'rate': sum(u['send_type'] == k for u in labeled) / counts[k]
                    if counts[k] else None} for k in _TYPES}},
            'unknown_label_reasons': dict(Counter(u['emotion_label']['unknown_reason'] for u in rows
                if u['emotion_label']['status'] == 'unknown')),
            'unknown_send_type_reasons': dict(Counter(u['send_type_unknown_reason'] for u in rows
                if u['send_type'] == 'unknown')),
            'asset_coverage': {'sticker_bubbles': sum(u['asset_coverage']['explicit_or_verified_sticker_bubbles'] for u in rows),
                'occurrences': sum(u['asset_coverage']['occurrence_count'] for u in rows),
                'reply_units_with_examples': sum(u['asset_coverage']['occurrence_count'] > 0 for u in rows),
                'missing_reasons': dict(sum((Counter(u['asset_coverage']['missing_reasons']) for u in rows), Counter()))}})
    return output
