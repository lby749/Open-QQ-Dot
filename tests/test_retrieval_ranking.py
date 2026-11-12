import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from persona_types import (Candidate, ContextBudgets, DataScope, FriendScope, PackVersion,
    PrivacyView, RetrievalBatch, RetrievalFilters, RetrievalRequest, ScopeMismatch, SourceOrigin, SourceRef)
from retrieval_ranking import rank_candidates


NOW = '2026-10-06T00:00:00+00:00'


class RetrievalRankingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        friend = self.root / 'friends' / '12345'
        friend.mkdir(parents=True)
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
            'qq_id': '12345', 'directory': str(friend)}}}), 'utf-8')
        self.packs = frozenset({PackVersion(pack_id='pack-a', version='v1'),
                               PackVersion(pack_id='pack-b', version='v1')})
        self.scope = FriendScope.load('12345', project_root=self.root,
                                      allowed_pack_versions=self.packs)

    def request(self, scene='any', allowed=None):
        return RetrievalRequest(friend_id='12345', query='query', recent_context='', scene=scene,
            plan=None, filters=RetrievalFilters(), budgets=ContextBudgets(),
            allowed_pack_versions=self.packs if allowed is None else allowed, now=NOW)

    def candidate(self, identifier, text, *, path='keyword', score=0, timestamp=None, owner='12345'):
        source = SourceRef(dataset_id=None, import_id=None, record_id=None, message_seq=1,
            speaker_id=owner, origin=SourceOrigin.REAL_RELATIONSHIP,
            data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
            friend_id=owner, event_time=timestamp, recorded_at=NOW)
        return Candidate(id=identifier, namespace='memory', type='message', content=text,
            score=score, source=source, reason='retrieved', retriever=path, friend_id=owner,
            timestamp=timestamp, confidence=1.0, original_scope=DataScope.PRIVATE_FRIEND,
            pack_version=None)

    def pack_candidate(self, pack_id):
        source = SourceRef(dataset_id='public', import_id='import', record_id='same',
            message_seq=None, speaker_id='author', origin=SourceOrigin.PUBLIC_DATASET,
            data_scope=DataScope.PUBLIC_PACK, privacy_view=PrivacyView.CLOUD_SAFE,
            friend_id=None, event_time=None, recorded_at=NOW, pack_id=pack_id, pack_version='v1')
        return Candidate(id='same', namespace='example', type='example', content='public example',
            score=0, source=source, reason='retrieved', retriever='example', friend_id='12345',
            timestamp=None, confidence=1, original_scope=DataScope.PUBLIC_PACK, pack_version='v1')

    def test_rank_only_dedupe_pack_isolation_time_and_mmr(self):
        a = self.candidate('a', '周杰伦喜欢晴天演唱会', score=1e9)
        b = self.candidate('b', '周杰伦喜欢晴天演唱会的现场片段', score=-1e9)
        c = self.candidate('c', '天文望远镜观测火星轨道', score=100)
        fused = rank_candidates(self.scope, self.request(), [
            RetrievalBatch(candidates=(a, a, b, c), elapsed_ms=0),
            RetrievalBatch(candidates=tuple(replace(item, retriever='dense')
                for item in (a, a, b, c)), elapsed_ms=0)], 3)
        by_id = {item.id: item for item in fused.candidates}
        self.assertAlmostEqual(by_id['a'].raw_score, 2 / 61)
        self.assertAlmostEqual(by_id['b'].raw_score, 2 / 62)
        self.assertEqual([item.id for item in fused.candidates[:2]], ['a', 'c'])
        self.assertIn('RRF=', by_id['a'].reason)
        self.assertIn('path:keyword', by_id['a'].signals)

        unknown = self.candidate('unknown', '时间不明', timestamp=None)
        old = self.candidate('old', '旧时间', timestamp='2020-01-01T00:00:00+00:00')
        recent = self.candidate('recent', '近日聊天', timestamp='2026-10-05T00:00:00+00:00')
        timed = rank_candidates(self.scope, self.request(), [
            RetrievalBatch(candidates=(unknown, old, recent), elapsed_ms=0)], 3)
        signals = {item.id: item.signals for item in timed.candidates}
        self.assertNotIn('time:recent', signals['unknown'])
        self.assertIn('time:recent', signals['recent'])

        public = rank_candidates(self.scope, self.request(), [
            RetrievalBatch(candidates=(self.pack_candidate('pack-a'), self.pack_candidate('pack-b')),
                           elapsed_ms=0)], 5)
        self.assertEqual(len(public.candidates), 2)
        with self.assertRaises(ScopeMismatch):
            rank_candidates(self.scope, self.request(), [
                RetrievalBatch(candidates=(self.candidate('foreign', 'text', owner='23456'),), elapsed_ms=0)], 1)
        with self.assertRaises(ScopeMismatch):
            rank_candidates(self.scope, self.request(allowed=frozenset()), [
                RetrievalBatch(candidates=(self.pack_candidate('pack-a'),), elapsed_ms=0)], 1)


if __name__ == '__main__':
    unittest.main()
