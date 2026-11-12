"""Safely clean UI display echoes without altering ordinary Chinese/English chat.

Pure Python, independent of Qt. Treat model text as untrusted: do not match empty
section names or use an accidentally translated regex character class.
"""
from __future__ import annotations
import re

_SECTION_LABELS = (
    '智能体专属工作台会话', '机前参照', '设定模板', '说话方式', '角色设定',
    '当前使用者', '外脑事实', '长期事实', '事实记忆', '话题资料库',
    '近期会话历史', '核心要求', '绝对输出禁令', '深度思考推理要求',
    '极速响应模式', '输出规则', '输出铁律',
    'Agent Workspace Session', 'Speaking Style', 'Topic Knowledge Library',
    'Reasoning Rules', 'Core Requirements', 'Output Rules', 'Persona',
)
_SECTION_RE = re.compile(
    r'【(?:' + '|'.join(re.escape(x) for x in _SECTION_LABELS) + r')(?:[：:]?[^】]{0,1200})】'
)
_NAMES = ('Dot 智能体', '智能体助手', '智能体', '助手', 'Dot Agent',
          'Agent Assistant', 'Agent', 'Assistant', 'AI', 'TA')

def extract_clean_reply(raw_text: str, agent_name: str = '') -> tuple[str, str]:
    """Return (human-facing reply, think text); never strip ordinary bracketed speech."""
    if not raw_text:
        return '', ''
    text = raw_text.strip()
    thinking = ''
    thinking_match = re.search(r'<think>(.*?)</think>', text, flags=re.S | re.I)
    if thinking_match:
        thinking = thinking_match.group(1).strip()
        text = (text[:thinking_match.start()] + text[thinking_match.end():]).strip()
    else:
        unfinished = re.search(r'<think>', text, re.I)
        if unfinished:
            text = text[:unfinished.start()].strip()
    text = _SECTION_RE.sub('', text).strip()
    text = re.sub(r'SPEAK_AS_TA:[\s\S]*?(?:STYLE_EXAMPLE|OPENER_EXAMPLE|\[TAG:[^\]]+\])', '', text).strip()
    text = re.sub(r'\[TAG:[^\]]+\][\s\S]*?(?=\n\n|\Z)', '', text).strip()
    agents = [re.escape(agent_name)] if agent_name else []
    agents.extend(re.escape(n) for n in _NAMES)
    names_re = '(?:' + '|'.join(sorted(set(agents), key=len, reverse=True)) + ')'
    split_re = re.compile(r'(?:^|\n)(?:用户|User)[：:][\s\S]*?' + names_re + r'[：:]\s*', re.I)
    chunks = split_re.split(text)
    if len(chunks) > 1 and chunks[-1].strip():
        text = chunks[-1].strip()
    text = re.sub(r'^' + names_re + r'[：:]\s*', '', text, flags=re.I).strip()
    text = re.sub(r'^(?:好的[，,！!]?|收到[，,！!]?)(?:根据|遵照|按照|针对).*?(?:回复如下|回答如下|回复您|回答您)[：:\n]\s*', '', text).strip()
    text = re.sub(r'^(?:以下是(?:我的|针对您.*?的)?(?:回复|回答)[：:\n]\s*)', '', text).strip()
    return text, thinking
