"""Guard against translation passes that blank string literals.

An earlier English port replaced Chinese literals with ``''``. Because ``'' in text`` is
always True and ``text.startswith('')`` always matches, that silently turned refusal
detection, intent routing, proactive gating and verifier checks into constant results.
These checks fail on the patterns that caused it.
"""
import ast
import unittest
from pathlib import Path

import lexicon

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {'.venv', '__pycache__', 'build', 'dist', 'tests', '_backup_before_dsh_focus'}


def _sources():
    for path in sorted(ROOT.rglob('*.py')):
        parts = path.relative_to(ROOT).parts
        if SKIP_PARTS.intersection(parts) or any(part.startswith('.pytest') for part in parts) or 'f' == parts[0]:
            continue
        yield path, ast.parse(path.read_text(encoding='utf-8'), str(path))


def _is_blank(node) -> bool:
    return isinstance(node, ast.Constant) and node.value == ''


class LiteralIntegrityTests(unittest.TestCase):
    def test_no_blank_needle_in_membership_tests(self):
        hits = []
        for path, tree in _sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) \
                        and _is_blank(node.left):
                    hits.append(f'{path.name}:{node.lineno}')
        self.assertEqual(hits, [], 'blank string used as the needle of `in` (always True)')

    def test_no_blank_entries_in_matching_vocabularies(self):
        hits = []
        for path, tree in _sources():
            for node in ast.walk(tree):
                # `value in ('a', '')` is a legitimate equality test; the bug is a collection whose
                # entries are used as substrings or prefixes, where '' matches everything.
                haystacks = []
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ('startswith', 'endswith') and node.args:
                    haystacks.append(node.args[0])
                elif isinstance(node, (ast.For, ast.comprehension)):
                    haystacks.append(node.iter)
                for hay in haystacks:
                    if _is_blank(hay):
                        hits.append(f'{path.name}:{hay.lineno}')
                    elif isinstance(hay, (ast.Tuple, ast.List, ast.Set)) and any(_is_blank(e) for e in hay.elts) \
                            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in hay.elts):
                        hits.append(f'{path.name}:{hay.lineno}')
        self.assertEqual(hits, [], 'blank entries in a vocabulary that is searched (match everything)')

    def test_named_keyword_constants_have_no_blank_entries(self):
        hits = []
        for path, tree in _sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
                    continue
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any(word in name.upper() for name in names for word in ('KEYWORD', 'REFUSAL', 'PREFIX', 'MARKER', 'EVASION')) \
                        and any(_is_blank(e) for e in node.value.elts):
                    hits.append(f'{path.name}:{node.lineno}:{names[0]}')
        self.assertEqual(hits, [])

    def test_lexicon_vocabularies_are_bilingual_and_non_blank(self):
        for name in ('TASK_STATUS_EXACT', 'TASK_CANCEL_PREFIXES', 'TASK_PAUSE_PREFIXES', 'CORRECTION_PREFIXES',
                     'EXPLICIT_TASK_PREFIXES', 'HEAVY_TASK_PREFIXES'):
            values = getattr(lexicon, name)
            self.assertTrue(all(v.strip() for v in values), name)
            self.assertTrue(any(any('一' <= ch <= '鿿' for ch in v) for v in values), f'{name} lost Chinese')
            self.assertTrue(any(v.isascii() for v in values), f'{name} lost English')

    def test_lexicon_routes_chinese_and_english(self):
        self.assertEqual(lexicon.task_category('帮我渲染一个视频'), 'video')
        self.assertEqual(lexicon.task_category('render a video'), 'video')
        self.assertEqual(lexicon.task_category('修复登录模块的代码'), 'code')
        self.assertEqual(lexicon.task_category('写首诗'), 'general')
        self.assertTrue(lexicon.is_task_status_query('任务状态'))
        self.assertFalse(lexicon.is_task_status_query('今天天气不错'))
        self.assertTrue(lexicon.is_heavy_task_request('请帮我开发一个完整的记账小程序'))
        self.assertFalse(lexicon.is_heavy_task_request('你好呀'))

    def test_refusal_detection_is_not_constant(self):
        import multi_agent_orchestrator as mao
        self.assertTrue(all(k.strip() for k in mao.REFUSAL_KEYWORDS))
        self.assertEqual(mao.is_capability_refusal('抱歉，我无法联网查询最新价格。'), (True, 'web_search'))
        self.assertEqual(mao.is_capability_refusal("Sorry, I don't have internet access."), (True, 'web_search'))
        self.assertEqual(mao.is_capability_refusal('结论：该函数的时间复杂度是 O(n log n)。'), (False, ''))

    def test_prompt_section_labels_are_non_empty(self):
        import prompt_context
        blank = [key for key, (slot, label) in prompt_context.BLOCKS.items() if not str(slot).strip() or not str(label).strip()]
        self.assertEqual(blank, [])


if __name__ == '__main__':
    unittest.main()
