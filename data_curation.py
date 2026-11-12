"""Streaming, provenance-aware rules for public persona dialogue pairs."""
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata

from dialogue_pairs import DialoguePair
from persona_types import DataScope, SourceOrigin


_PUBLIC_TARGET_ORIGINS = {SourceOrigin.PUBLIC_DATASET, SourceOrigin.REAL_PERSONA_POOL,
                          SourceOrigin.REAL_SINGLE_SPEAKER}
_MIN_HAN_RATIO = 0.10


class RuleFilter:
    def __init__(self):
        self.stats = self._empty_stats()

    @staticmethod
    def _empty_stats():
        return dict(input=0, provenance_rejected=0, empty_rejected=0,
                    language_checked=0, language_rejected=0, exact_duplicate=0,
                    near_duplicate=0, text_unprocessed=0, accepted=0)

    def iter_filter(self, pairs):
        self.stats = self._empty_stats()
        def stream():
            handle = tempfile.NamedTemporaryFile(prefix='curation-', suffix='.sqlite', delete=False)
            path = handle.name
            handle.close()
            try:
                with closing(sqlite3.connect(path)) as db:
                    db.execute('CREATE TABLE signatures (exact TEXT UNIQUE, normalized TEXT UNIQUE)')
                    for pair in pairs:
                        self.stats['input'] += 1
                        if not self._public_target(pair):
                            self.stats['provenance_rejected'] += 1
                            continue
                        texts = [bubble.text for bubble in pair.target_bubbles if bubble.text]
                        if not texts and not any(bubble.media_id for bubble in pair.target_bubbles):
                            self.stats['empty_rejected'] += 1
                            continue
                        target_text = ''.join(texts)
                        han = sum('\u3400' <= char <= '\u9fff' for char in target_text)
                        latin = sum(char.isalpha() and not ('\u3400' <= char <= '\u9fff')
                                    for char in target_text)
                        if not target_text or (han == 0 and latin == 0):
                            self.stats['text_unprocessed'] += 1
                        else:
                            self.stats['language_checked'] += 1
                            if latin and (han == 0 or han / (han + latin) < _MIN_HAN_RATIO):
                                self.stats['language_rejected'] += 1
                                continue
                        exact = _signature(pair, normalized=False)
                        normalized = _signature(pair, normalized=True)
                        cursor = db.execute('INSERT INTO signatures VALUES (?, ?) '
                            'ON CONFLICT DO NOTHING', (exact, normalized))
                        if cursor.rowcount == 0:
                            found = db.execute('SELECT 1 FROM signatures WHERE exact=?', (exact,)).fetchone()
                            self.stats['exact_duplicate' if found else 'near_duplicate'] += 1
                            continue
                        self.stats['accepted'] += 1
                        yield pair
            finally:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        return stream()

    @staticmethod
    def _public_target(pair):
        if not isinstance(pair, DialoguePair) or not pair.target_bubbles or not pair.source_refs:
            return False
        if any(ref.data_scope not in (DataScope.PUBLIC_DATASET, DataScope.PUBLIC_PACK) or
               ref.friend_id is not None for ref in pair.source_refs):
            return False
        return all(message.speaker_id == pair.target_speaker_id and
                   message.speaker_role.strip().lower() in {'human', 'user', 'friend', 'contact',
                       'person', 'target', 'me'} and message.source.data_scope in
                   (DataScope.PUBLIC_DATASET, DataScope.PUBLIC_PACK) and
                   message.source.friend_id is None and message.source.origin in _PUBLIC_TARGET_ORIGINS
                   for message in pair.target_bubbles)


def _signature(pair, *, normalized):
    def bubble(message):
        text = message.text
        if normalized:
            text = unicodedata.normalize('NFKC', text).casefold()
            text = ''.join(char for char in text if not char.isspace() and
                           not unicodedata.category(char).startswith('P'))
        return [message.message_type, text, message.media_id]

    value = dict(user=[[bubble(message) for message in pair.user_bubbles]],
        context=[[bubble(message) for message in turn.bubbles] for turn in pair.context_turns],
        target=[[bubble(message) for message in pair.target_bubbles]])
    payload = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
