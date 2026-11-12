"""Deterministic, bounded Style statistics over verified target Pairs."""
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone

from dialogue_pairs import DialoguePair
from dialogue_turns import MAX_SESSION_MESSAGES
from persona_store import ScopeMismatch, Unavailable
from persona_types import DataScope, InvalidSource, NormalizedMessage, SourceOrigin, SourceRef


_HUMAN_ROLES = {'human', 'user', 'friend', 'contact', 'person', 'target', 'me'}
_TARGET_ORIGINS = {SourceOrigin.REAL_RELATIONSHIP, SourceOrigin.REAL_SINGLE_SPEAKER,
                   SourceOrigin.REAL_PERSONA_POOL, SourceOrigin.PUBLIC_DATASET}
_WORD_RUNS = re.compile(r'[\u4e00-\u9fff]+|[A-Za-z]+|[0-9]+')
_SENTENCE_END = frozenset('. ! ? .!?')


class StyleStatistics:
    @staticmethod
    def calculate(pairs):
        iterator = _pair_iterator(pairs)
        unique, seen = [], {}
        raw_pair_count = 0
        target_message_count = 0
        friend_id = None
        for pair in iterator:
            raw_pair_count += 1
            if raw_pair_count > MAX_SESSION_MESSAGES:
                raise Unavailable('Style input exceeds the 10000 Pair limit')
            if not isinstance(pair, DialoguePair):
                raise InvalidSource('Style accepts DialoguePair values only')
            if not isinstance(pair.id, str) or not pair.id.strip():
                raise InvalidSource('Pair id must be nonempty text')
            prior = seen.get(pair.id)
            if prior is not None:
                if prior != pair:
                    raise InvalidSource('duplicate Pair id has different content')
                continue
            if isinstance(pair.target_bubbles, tuple) and \
                    target_message_count + len(pair.target_bubbles) > MAX_SESSION_MESSAGES:
                raise Unavailable('Style input exceeds the 10000 Target message limit')
            _validate_pair(pair)
            if friend_id is None:
                friend_id = pair.friend_id
            elif pair.friend_id != friend_id:
                raise ScopeMismatch('Style Pairs belong to different friend owners')
            target_message_count += len(pair.target_bubbles)
            seen[pair.id] = pair
            unique.append(pair)
        return _calculate(unique)


def _pair_iterator(pairs):
    if isinstance(pairs, (str, bytes, Mapping)) or pairs is None:
        raise InvalidSource('calculate requires a bounded DialoguePair iterable')
    try:
        return iter(pairs)
    except TypeError as exc:
        raise InvalidSource('calculate requires a DialoguePair iterable') from exc


def _validate_pair(pair):
    if not isinstance(pair, DialoguePair):
        raise InvalidSource('Style accepts DialoguePair values only')
    if not isinstance(pair.id, str) or not pair.id.strip() or not isinstance(pair.friend_id, str) \
            or not pair.friend_id.strip() or not isinstance(pair.target_speaker_id, str) \
            or not pair.target_speaker_id.strip() or _unknown(pair.target_speaker_id) or \
            not isinstance(pair.session_id, str) or not pair.session_id.strip() or \
            pair.time_quality not in {'KNOWN', 'UNKNOWN'}:
        raise InvalidSource('Pair owner or Target identity is invalid')
    if not isinstance(pair.target_bubbles, tuple) or not pair.target_bubbles:
        raise InvalidSource('Pair must retain at least one Target bubble')
    if not isinstance(pair.source_refs, tuple) or not pair.source_refs or any(
            not isinstance(ref, SourceRef) for ref in pair.source_refs):
        raise InvalidSource('Pair must contain validated provenance')
    refs = set(pair.source_refs)
    target_refs = set()
    target_ids = set()
    for bubble in pair.target_bubbles:
        if not isinstance(bubble, NormalizedMessage):
            raise InvalidSource('Target bubble is not a NormalizedMessage')
        if bubble.speaker_id != pair.target_speaker_id or _unknown(bubble.speaker_id):
            raise InvalidSource('Target bubble identity differs from explicit Pair Target')
        if bubble.speaker_role.strip().lower() not in _HUMAN_ROLES:
            raise InvalidSource('nonhuman Target cannot contribute Style')
        if bubble.source.origin not in _TARGET_ORIGINS:
            raise InvalidSource('unknown, bot, synthetic or manual Target cannot contribute Style')
        if bubble.id in target_ids:
            raise InvalidSource('Target bubble id is duplicated')
        target_ids.add(bubble.id)
        target_refs.add(bubble.source)
    if not target_refs.issubset(refs):
        raise InvalidSource('Pair provenance omits Target source')
    for ref in pair.source_refs:
        if ref.origin is None:
            raise InvalidSource('Pair has an unmapped source origin')
        if ref.data_scope == DataScope.PRIVATE_FRIEND:
            if ref.friend_id != pair.friend_id:
                raise ScopeMismatch('private Pair provenance belongs to another friend')
        elif ref.friend_id is not None:
            raise ScopeMismatch('public provenance cannot claim a friend identity')


def _unknown(speaker_id):
    actor = speaker_id.strip().lower()
    return actor == 'unknown' or actor.startswith('unknown:')


def _calculate(pairs):
    pair_count = len(pairs)
    target_bubbles = [bubble for pair in pairs for bubble in pair.target_bubbles]
    text_bubbles = [bubble for bubble in target_bubbles if bubble.text]
    text_replies = []
    reply_bubble_counts = []
    sticker_classes = Counter()
    unknown_media_count = 0
    expressions = defaultdict(lambda: {'count': 0, 'sessions': set(), 'imports': set()})
    words = Counter()
    text_reply_lengths, bubble_lengths = [], []
    question_replies = period_count = exclamation_count = 0
    emoji_replies = emoji_codepoints = fragment_replies = short_replies = 0
    emoji_ranges = ((0x1F1E6, 0x1F1FF), (0x1F300, 0x1FAFF), (0x2600, 0x27BF))
    source_refs, source_seen = [], set()
    target_speakers, speaker_seen = [], set()
    session_ids, session_seen = [], set()

    for pair in pairs:
        if pair.session_id not in session_seen:
            session_seen.add(pair.session_id)
            session_ids.append(pair.session_id)
        if pair.target_speaker_id not in speaker_seen:
            speaker_seen.add(pair.target_speaker_id)
            target_speakers.append(pair.target_speaker_id)
        for ref in pair.source_refs:
            if ref not in source_seen:
                source_seen.add(ref)
                source_refs.append(ref)
        reply_bubble_counts.append(len(pair.target_bubbles))
        texts = [bubble.text for bubble in pair.target_bubbles if bubble.text]
        reply_text = ''.join(texts)
        if texts:
            text_replies.append(reply_text)
            reply_length = sum(len(text) for text in texts)
            text_reply_lengths.append(reply_length)
            if reply_length <= 10:
                short_replies += 1
            if any(mark in reply_text for mark in ('?', '？')):
                question_replies += 1
            period_count += any(mark in reply_text for mark in ('.', '。'))
            exclamation_count += any(mark in reply_text for mark in ('!', '！'))
            codes = [ord(char) for char in reply_text]
            emoji_n = sum(any(start <= code <= end for start, end in emoji_ranges)
                          for code in codes)
            emoji_codepoints += emoji_n
            if emoji_n:
                emoji_replies += 1
            trimmed = reply_text.strip()
            if len(trimmed) <= 10 and (not trimmed or trimmed[-1] not in _SENTENCE_END):
                fragment_replies += 1
        for bubble in pair.target_bubbles:
            if bubble.text:
                bubble_lengths.append(len(bubble.text))
                words.update(_WORD_RUNS.findall(bubble.text))
                expression = bubble.text.strip()
                if expression and len(expression) <= 20:
                    item = expressions[expression]
                    item['count'] += 1
                    item['sessions'].add(pair.session_id)
                    if bubble.source.import_id:
                        item['imports'].add(bubble.source.import_id)
        sticker = any(_is_sticker(bubble) for bubble in pair.target_bubbles)
        text = bool(texts)
        other_media = any(_is_other_media(bubble) for bubble in pair.target_bubbles)
        if other_media:
            sticker_classes['other_media_or_unknown'] += 1
        elif sticker and text:
            sticker_classes['text_plus_sticker'] += 1
        elif sticker:
            sticker_classes['sticker_only'] += 1
        else:
            sticker_classes['text_only'] += 1
        unknown_media_count += sum(_is_other_media(bubble) for bubble in pair.target_bubbles)

    text_reply_count = len(text_replies)
    scopes = Counter(ref.data_scope.value for ref in source_refs)
    views = Counter(ref.privacy_view.value for ref in source_refs)
    origins = Counter(ref.origin.value for ref in source_refs if ref.origin is not None)
    times = Counter(pair.time_quality for pair in pairs)
    imports = list(dict.fromkeys(ref.import_id for ref in source_refs if ref.import_id))
    has_private = bool(scopes.get(DataScope.PRIVATE_FRIEND.value))
    effective_scope = (DataScope.PRIVATE_FRIEND.value if has_private else
        DataScope.PUBLIC_PACK.value if scopes.get(DataScope.PUBLIC_PACK.value) else
        DataScope.PUBLIC_DATASET.value if scopes.get(DataScope.PUBLIC_DATASET.value) else None)
    expression_list = []
    for expression, info in expressions.items():
        if info['count'] >= 2:
            expression_list.append({'text': expression, 'count': info['count'],
                'session_count': len(info['sessions']),
                'source_import_ids': sorted(info['imports']),
                'denominator': 'nonempty_target_bubbles'})
    expression_list.sort(key=lambda item: (-item['count'], item['text']))

    return {
        'sample_info': {
            'sample_status': _sample_status(pair_count),
            'pair_count': pair_count,
            'target_bubble_count': len(target_bubbles),
            'target_text_bubble_count': len(text_bubbles),
            'target_text_codepoints': sum(len(bubble.text) for bubble in text_bubbles),
            'text_reply_count': text_reply_count,
            'session_count': len(session_ids),
            'target_speakers': target_speakers,
            'target_mapping': [{'speaker_id': speaker, 'subject': 'BOT_PERSONA'}
                               for speaker in target_speakers],
            'friend_id': pairs[0].friend_id if has_private and pairs else None,
            'data_scope': effective_scope,
            'source_scopes': dict(sorted(scopes.items())),
            'privacy_views': dict(sorted(views.items())),
            'source_origins': dict(sorted(origins.items())),
            'source_import_ids': imports,
            'time_quality': dict(sorted(times.items())),
            'scene_coverage': {'count': 0, 'status': 'not_available'},
            'denominators': {'pairs': pair_count, 'target_bubbles': len(target_bubbles),
                             'text_bubbles': len(text_bubbles), 'text_replies': text_reply_count},
        },
        'message_length': {
            'reply': _length_stats(text_reply_lengths, 'text_replies'),
            'single_bubble': _length_stats(bubble_lengths, 'nonempty_target_bubbles'),
        },
        'bubble_behavior': {
            'counts': {'single': sum(count == 1 for count in reply_bubble_counts),
                       'two': sum(count == 2 for count in reply_bubble_counts),
                       'three_plus': sum(count >= 3 for count in reply_bubble_counts)},
            'rates': {'single': _rate(sum(count == 1 for count in reply_bubble_counts), pair_count),
                      'two': _rate(sum(count == 2 for count in reply_bubble_counts), pair_count),
                      'three_plus': _rate(sum(count >= 3 for count in reply_bubble_counts), pair_count)},
            'denominator': 'pairs',
            'short_reply_rate': {'rate': _rate(short_replies, text_reply_count),
                'count': short_replies, 'denominator': 'text_replies', 'threshold_codepoints': 10},
            'estimated': False,
        },
        'question_behavior': {
            'question_rate': {'rate': _rate(question_replies, text_reply_count),
                'count': question_replies, 'denominator': 'text_replies', 'estimated': False},
            'question_mark_count': sum(sum(reply.count(mark) for mark in ('?', '？'))
                                       for reply in text_replies),
        },
        'punctuation': {
            'literal_counts': {mark: sum(reply.count(mark) for reply in text_replies)
                               for mark in ('.', '!', '?', '。', '！', '？')},
            'period_rate': {'rate': _rate(period_count, text_reply_count),
                'count': period_count, 'denominator': 'text_replies', 'estimated': False},
            'exclamation_rate': {'rate': _rate(exclamation_count, text_reply_count),
                'count': exclamation_count, 'denominator': 'text_replies', 'estimated': False},
        },
        'emoji_behavior': {
            'reply_rate': {'rate': _rate(emoji_replies, text_reply_count),
                'count': emoji_replies, 'denominator': 'text_replies', 'estimated': True},
            'matched_codepoint_count': emoji_codepoints,
            'estimated': True,
            'rule': 'Unicode ranges U+1F1E6-U+1F1FF, U+1F300-U+1FAFF, U+2600-U+27BF; approximate only',
        },
        'sticker_behavior': {
            'pair_counts': {name: sticker_classes[name] for name in
                ('text_only', 'text_plus_sticker', 'sticker_only', 'other_media_or_unknown')},
            'pair_rates': {name: _rate(sticker_classes[name], pair_count) for name in
                ('text_only', 'text_plus_sticker', 'sticker_only', 'other_media_or_unknown')},
            'denominator': 'pairs',
            'unknown_media_bubble_count': unknown_media_count,
            'preferred_styles': None,
            'status': 'not_analyzed',
            'estimated': False,
        },
        'common_expressions': {'items': expression_list[:20],
            'maximum_items': 20, 'denominator': 'nonempty_target_bubbles',
            'denominator_count': len(text_bubbles)},
        'common_words': {'items': _common_words(words), 'tokenizer': 'regex_runs',
            'estimated': True, 'rule': 'continuous CJK runs, ASCII alphabetic runs, and digit runs',
            'denominator': 'regex_tokens', 'denominator_count': sum(words.values())},
        'fragment_usage': {'short_no_terminator_rate': {
            'rate': _rate(fragment_replies, text_reply_count), 'count': fragment_replies,
            'denominator': 'text_replies', 'estimated': True, 'threshold_codepoints': 10,
            'rule': 'trimmed text length <=10 and last character is not . ! ? .!?' }},
        'interaction_patterns': {
            'subject_omission': _not_analyzed(),
            'proactive_follow_up': _not_analyzed(),
            'topic_switching': _not_analyzed(),
            'reply_delay_seconds': _not_analyzed('timestamps are not sufficient for this statistic'),
            'consecutive_target_bubbles': {
                'rate': _rate(sum(count > 1 for count in reply_bubble_counts), pair_count),
                'count': sum(count > 1 for count in reply_bubble_counts),
                'denominator': 'pairs', 'estimated': False},
        },
        'anti_patterns': [],
        'anti_patterns_status': 'not_analyzed',
        'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'source_import_ids': imports,
        'methods': {
            'character_length': 'Python Unicode codepoint count; media IDs/placeholders excluded',
            'reply_length': 'sum of nonempty Target bubble text lengths per Pair',
            'single_bubble_length': 'each nonempty Target bubble measured separately',
            'quantiles': 'sorted position (n-1)*q with linear interpolation',
            'short_reply_threshold_codepoints': 10,
            'question_rate': 'text replies containing ? or ?  divided by text replies',
            'punctuation_rates': 'text replies containing literal period/exclamation marks divided by text replies',
            'sticker_types': 'only message_type sticker/face is explicit sticker; images are never inferred as stickers',
        },
    }


def _is_sticker(bubble):
    return bubble.message_type.strip().lower() in {'sticker', 'face'}


def _is_other_media(bubble):
    kind = bubble.message_type.strip().lower()
    return (kind in {'image', 'unknown', 'audio', 'video', 'file', 'media'} or
            bool(bubble.media_id and kind not in {'sticker', 'face'}))



def _length_stats(values, denominator):
    ordered = sorted(values)
    count = len(ordered)
    return {
        'sample_count': count,
        'denominator': denominator,
        'mean': sum(ordered) / count if count else None,
        'median': _quantile(ordered, 0.5),
        'p25': _quantile(ordered, 0.25),
        'p75': _quantile(ordered, 0.75),
        'p90': _quantile(ordered, 0.9),
    }


def _quantile(ordered, q):
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _rate(count, denominator):
    return count / denominator if denominator else None


def _sample_status(count):
    if count < 50:
        return 'insufficient'
    if count < 200:
        return 'low'
    if count <= 1000:
        return 'medium'
    return 'high'


def _common_words(words):
    return [{'text': token, 'count': count} for token, count in
            sorted(((token, count) for token, count in words.items() if count >= 2),
                   key=lambda item: (-item[1], item[0]))[:20]]


def _not_analyzed(reason=None):
    return {'rate': None, 'estimated': True, 'status': 'not_analyzed',
            **({'reason': reason} if reason else {})}
