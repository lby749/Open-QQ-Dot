"""Chinese distribution: bilingual UI, sanitizer and critical source parity checks."""
from __future__ import annotations
import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import lexicon
from reply_sanitizer import extract_clean_reply
from ui_locale import tr

ROOT = Path(__file__).resolve().parents[1]

class ChineseEditionTests(unittest.TestCase):
    def test_chinese_default_and_english_fallback(self):
        with patch.dict(os.environ, {'OPENQQ_UI_LANG':'zh-CN'}):
            self.assertEqual(tr('Agent Workbench'), '智能体工作台')
            self.assertEqual(tr('Status & Remote'), '状态与远程连接')
            self.assertEqual(tr('unknown-protocol-value'), 'unknown-protocol-value')
        with patch.dict(os.environ, {'OPENQQ_UI_LANG':'en'}):
            self.assertEqual(tr('Agent Workbench'), 'Agent Workbench')

    def test_bilingual_qq_commands(self):
        self.assertTrue(lexicon.is_task_status_query('任务状态'))
        self.assertTrue(lexicon.is_task_status_query('task status'))
        self.assertEqual(lexicon.task_category('帮我修复一段代码'), 'code')
        self.assertEqual(lexicon.task_category('fix code'), 'code')
        self.assertFalse(lexicon.is_task_status_query('今天过得怎么样？'))

    def test_regular_brackets_are_not_removed(self):
        text = '我想说【这个真的很重要】，不要删掉。'
        self.assertEqual(extract_clean_reply(text), (text, ''))
        self.assertEqual(extract_clean_reply('Hello [plain text].'), ('Hello [plain text].', ''))

    def test_zh_and_en_prompt_section_removal(self):
        self.assertEqual(extract_clean_reply('【设定模板：严格遵照】\n正常回复。')[0], '正常回复。')
        self.assertEqual(extract_clean_reply('【Agent Workspace Session: internal】\nFinal answer.')[0], 'Final answer.')
        self.assertEqual(extract_clean_reply('我正在学习【AI】和【编程】。')[0], '我正在学习【AI】和【编程】。')

    def test_role_prefix_and_thinking(self):
        self.assertEqual(extract_clean_reply('<think>private notes</think>助手：你好呀')[0], '你好呀')
        self.assertEqual(extract_clean_reply('<think>private notes</think>助手：你好呀')[1], 'private notes')
        self.assertEqual(extract_clean_reply('Assistant: Hello!')[0], 'Hello!')

    def test_gui_uses_localizer_without_modifying_persistent_keys(self):
        window = (ROOT/'house_qt/window.py').read_text(encoding='utf-8')
        navigation_calls = [node for node in ast.walk(ast.parse(window)) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute) and node.func.attr == 'addSubInterface']
        self.assertTrue(navigation_calls)
        for call in navigation_calls:
            self.assertEqual(ast.unparse(call.args[2]), 'tr(title)')
        self.assertIn('workbenchPage', window)
        self.assertIn('friend.json', window)
        self.assertTrue((ROOT/'agent_mailbox_mcp.py').is_file())
        self.assertTrue((ROOT/'queue_governor.py').is_file())
        self.assertTrue((ROOT/'tests/test_agent_messaging.py').is_file())

    def test_no_broken_literals_in_localized_sources(self):
        for file in (ROOT/'ui_locale.py',ROOT/'reply_sanitizer.py', * (ROOT/'house_qt').glob('*.py')):
            ast.parse(file.read_text(encoding='utf-8'), filename=str(file))

if __name__ == '__main__':
    unittest.main()
