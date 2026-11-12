from dataclasses import asdict
import unittest

from persona_types import (ContextBudgets, ConversationPlan, PackVersion, RetrievalFilters,
    RetrievalRequest)
from retrieval_router import RetrievalQueryRouter


NOW = '2026-10-06T00:00:00+00:00'


def request(query, *, scene='unknown_scene', plan=None, time_range=('2026-01-01', '2026-12-31')):
    return RetrievalRequest(friend_id='12345', query=query, recent_context='private context',
        scene=scene, plan=plan, filters=RetrievalFilters(time_range=time_range),
        budgets=ContextBudgets(), allowed_pack_versions=frozenset({PackVersion(pack_id='p', version='v1')}),
        now=NOW)


def plan(action='reply', *, introduce=False, core=''):
    return ConversationPlan(action=action, intent='active', tone='casual', core_meaning=core,
        ask_question=False, continue_topic=False, introduce_new_topic=introduce,
        desired_bubbles=1, sticker_intent=None)


class RetrievalQueryRouterTests(unittest.TestCase):
    def setUp(self):
        self.router = RetrievalQueryRouter()

    def test_task_acceptance_routes(self):
        history = self.router.route(request('周杰伦之前聊过什么'))
        self.assertEqual(history.retrievers, ['keyword', 'entity', 'memory', 'temporal'])
        similar = self.router.route(request('这首歌感觉像周杰伦以前唱的吗'))
        self.assertEqual(similar.retrievers, ['semantic', 'keyword'])
        profile = self.router.route(request('你生日是什么时候'))
        self.assertEqual(profile.retrievers, ['structured'])
        self.assertEqual(self.router.route(request('生日是什么')).retrievers, ['structured'])
        active = self.router.route(request('proactive', plan='active'))
        self.assertEqual(active.retrievers, ['interest', 'unfinished_topic', 'temporal', 'topic'])

    def test_explicit_knowledge_and_emotional_scenes(self):
        knowledge = ['topic', 'entity', 'keyword', 'example']
        emotion = ['persona', 'memory', 'example']
        self.assertEqual(self.router.route(request('继续', scene='knowledge')).retrievers, knowledge)
        self.assertEqual(self.router.route(request('yeah', scene='emotional')).retrievers, emotion)
        self.assertEqual(self.router.route(request('继续', scene='知识讨论')).retrievers, knowledge)
        self.assertEqual(self.router.route(request('yeah', scene='情绪')).retrievers, emotion)

        self.assertEqual(self.router.route(request('继续', scene='knowledge', plan='active')).reason,
                         'active_topic')
        self.assertEqual(self.router.route(request('像之前那样', scene='knowledge')).reason,
                         'similarity')
        self.assertEqual(self.router.route(request('之前说过', scene='emotional')).reason,
                         'history_recall')
        self.assertEqual(self.router.route(request('周杰伦的生日是哪天', scene='emotional')).reason,
                         'third_party_topic')
        self.assertEqual(self.router.route(request('你生日是什么时候', scene='knowledge')).reason,
                         'friend_profile')

    def test_none_unknown_and_birthday_negatives(self):
        first_turn = self.router.route(request('Hello', plan=None))
        self.assertEqual(first_turn.mode, 'initial')
        self.assertEqual(first_turn.retrievers, ['structured', 'keyword'])
        self.assertEqual(self.router.route(request('生日快乐', scene='daily_chat')).retrievers,
                         ['example', 'persona', 'interest'])
        self.assertEqual(self.router.route(request('同学的生日是什么时候')).retrievers,
                         ['topic', 'entity', 'keyword'])
        self.assertEqual(self.router.route(request('周杰伦的生日是哪天')).retrievers,
                         ['topic', 'entity', 'keyword'])
        self.assertEqual(self.router.route(request('周杰伦的生日之前聊过吗')).retrievers,
                         ['keyword', 'entity', 'memory', 'temporal'])
        self.assertEqual(self.router.route(request('周杰伦之前聊过什么')).retrievers,
                         ['keyword', 'entity', 'memory', 'temporal'])
        self.assertEqual(self.router.route(request('我喜欢生日歌', scene='daily_chat')).retrievers,
                         ['example', 'persona', 'interest'])

    def test_plan_object_skip_supplement_and_intent_is_not_authority(self):
        for action in ('wait', 'ignore'):
            decision = self.router.route(request('继续', plan=plan(action)))
            self.assertEqual((decision.retrievers, decision.mode), ([], 'skip'))
        supplemental = self.router.route(request('<months>Hello', plan=plan(introduce=True, core='months')))
        self.assertEqual(supplemental.mode, 'supplemental')
        self.assertEqual(supplemental.query_terms, ['months'])
        self.assertEqual(supplemental.entities, ['months'])
        unmatched_entity = self.router.route(request('<>Hello',
            plan=plan(introduce=True, core='months')))
        self.assertEqual(unmatched_entity.entities, [])
        empty_supplement = self.router.route(request('生日是什么', plan=plan(introduce=True, core='')))
        self.assertEqual((empty_supplement.mode, empty_supplement.retrievers),
                         ('initial', ['structured']))
        self.assertEqual(self.router.route(request('Hello', plan=plan())).retrievers,
                         ['structured', 'keyword'])

    def test_scene_time_entities_and_request_are_preserved(self):
        original = request('<astronomy>  "months" ', scene='Odd Scene', plan=None,
                           time_range=('2025-01-01', '2025-02-01'))
        before = asdict(original)
        decision = self.router.route(original)
        self.assertEqual(decision.scene, 'Odd Scene')
        self.assertEqual(decision.time_range, ('2025-01-01', '2025-02-01'))
        self.assertEqual(decision.entities, ['astronomy', 'months'])
        self.assertEqual(asdict(original), before)
        no_guess = self.router.route(request('你叫什么名字'))
        self.assertEqual(no_guess.entities, [])

    def test_reason_does_not_include_query_text(self):
        decision = self.router.route(request('私人秘密之前说过'))
        self.assertNotIn('私人秘密', decision.reason)
        self.assertEqual(decision.reason, 'history_recall')


if __name__ == '__main__':
    unittest.main()
