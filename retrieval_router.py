"""Pure rule-based routing for retrieval requests."""

from dataclasses import dataclass
import re

from persona_types import ConversationPlan, RetrievalRequest


@dataclass(frozen=True)
class RouterDecision:
    retrievers: list[str]
    scene: str
    query_terms: list[str]
    entities: list[str]
    time_range: tuple[str | None, str | None]
    reason: str
    mode: str


_TERM_SPLIT = re.compile(r"[\s，。！？、；：,.!?;:（）()\[\]{}]+")
_ENTITY = re.compile(r"[\"'“”‘’「」『』《<](.+?)[\"'“”‘’「」』』》>]")
_ACTIVE = ('主动聊', '主动找', '最近聊什么', '聊点什么', '找个话题', '选个话题',
           'chat proactively', 'start a topic', 'pick a topic', 'what should we talk about')
_FRIEND_PROFILE_QUESTIONS = (
    'days',
    'years',
    'chars',
    '生日是什么',
    '生日是哪天',
    '生日几号',
    '生日几日',
    '生日什么时候',
    '几岁了',
    '你多大',
    '你的年龄',
    '你叫什么',
    '叫什么名字',
    '你的名字是什么',
    '怎么称呼你',
    '你在哪上学',
    '你在哪个学校',
    '你的学校是什么',
    '你读几年级',
)
_SIMILAR = ('类似', '相似', '像', '感觉像', '差不多')
_HISTORY = ('之前', '以前', '上次', '还记得', '聊过', '说过')
_KNOWLEDGE = ('为什么', '怎么理解', '原理', '知识', '解释一下', '是什么')
_EMOTION = ('sad', 'happy', 'angry', '难过', '伤心', '开心', '焦虑', '生气', '烦', '累', '委屈', '害怕')
_KNOWLEDGE_SCENES = ('knowledge', '知识讨论')
_EMOTION_SCENES = ('emotional', '情绪')
_DAILY_SCENES = ('daily_chat',
'casual_chat',
'days',
'greeting',
'casual_update',
'concrete_question',
'knowledge_discussion',
'study_help',
'emotion_low',
'celebration',
'playful_tease',
'misunderstanding',
'conflict',
'unfinished_topic',
'silence_followup',
'image_reaction',
'sticker_exchange',
'unknown',
'日常聊天',
'闲聊')


def _terms(text: str) -> list[str]:
    seen = set()
    result = []
    for value in _TERM_SPLIT.split(text.strip()):
        if value and value not in seen:
            seen.add(value)
            result.append(value)
            if len(result) == 16:
                break
    return result


def _entities(text: str) -> list[str]:
    seen = set()
    result = []
    for match in _ENTITY.finditer(text):
        value = match.group(1).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value[:64])
            if len(result) == 8:
                break
    return result


def _active_request(request: RetrievalRequest, trusted_plan: str | None, query: str) -> bool:
    scene = request.scene.strip().casefold()
    return (trusted_plan in ('active', 'proactive') or scene in ('active', 'proactive') or
            any(marker in query for marker in _ACTIVE))


class RetrievalQueryRouter:
    """Routes validated request data only; performs no I/O or inference."""

    def route(self, request: RetrievalRequest) -> RouterDecision:
        if not isinstance(request, RetrievalRequest):
            raise TypeError('request must be RetrievalRequest')

        plan = request.plan
        trusted_plan = plan.strip().casefold() if isinstance(plan, str) else None
        mode = 'initial'
        query = request.query
        supplemental = False
        if isinstance(plan, ConversationPlan):
            if plan.action in ('wait', 'ignore'):
                return RouterDecision([], request.scene, [], [], tuple(request.filters.time_range),
                    'planner_skip', 'skip')
            if plan.introduce_new_topic and plan.core_meaning.strip():
                query = plan.core_meaning.strip()
                mode = 'supplemental'
                supplemental = True

        terms = _terms(query)
        entities = _entities(request.query)
        if supplemental:
            entities = [value for value in entities if value in query]
        lowered = query.casefold()
        normalized_scene = request.scene.strip().casefold()
        third_party_birthday = '生日' in lowered and any(
            marker in lowered for marker in ('周杰伦', '同学', '朋友', '她的', '他的'))

        if _active_request(request, trusted_plan, query):
            retrievers, reason = ['interest', 'unfinished_topic', 'temporal', 'topic'], 'active_topic'
            # unfinished_topic is a logical route name pending Orchestrator wiring (C06).
        elif any(word in lowered for word in _SIMILAR):
            retrievers, reason = ['semantic', 'keyword'], 'similarity'
        elif any(word in lowered for word in _HISTORY):
            retrievers, reason = ['keyword', 'entity', 'memory', 'temporal'], 'history_recall'
        elif third_party_birthday:
            retrievers, reason = ['topic', 'entity', 'keyword'], 'third_party_topic'
        elif (any(phrase in lowered for phrase in _FRIEND_PROFILE_QUESTIONS) and
              '生日快乐' not in lowered):
            retrievers, reason = ['structured'], 'friend_profile'
        elif normalized_scene in _KNOWLEDGE_SCENES:
            retrievers, reason = ['topic', 'entity', 'keyword', 'example'], 'knowledge_query'
        elif normalized_scene in _EMOTION_SCENES:
            retrievers, reason = ['persona', 'memory', 'example'], 'emotion'
        elif any(word in lowered for word in _KNOWLEDGE):
            retrievers, reason = ['topic', 'entity', 'keyword', 'example'], 'knowledge_query'
        elif any(word in lowered for word in _EMOTION):
            retrievers, reason = ['persona', 'memory', 'example'], 'emotion'
        elif normalized_scene in _DAILY_SCENES:
            retrievers, reason = ['example', 'persona', 'interest'], 'daily_scene'
        else:
            retrievers, reason = ['structured', 'keyword'], 'default'

        return RouterDecision(retrievers, request.scene, terms, entities,
            tuple(request.filters.time_range), reason, mode)
