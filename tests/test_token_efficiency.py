"""Prompt-size reduction tests: character/UTF-8 proxy, not provider billing claims."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_messaging import render_for_prompt
from multi_agent_orchestrator import _without_duplicate_mission, build_claude_english_handover
from prompt_context import _recent_window, recent_rounds
from store import FriendStore


class TokenEfficiencyTests(unittest.TestCase):
    def test_custom_api_recent_history_has_independent_budget_and_full_latest_round(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            profile = {'model_api': {'provider': 'custom', 'compact_tokens': 1000000}}
            config = directory / 'friend.json'
            config.write_text(json.dumps(profile), 'utf-8')
            store = FriendStore(directory)
            try:
                for i in range(120):
                    store.add_message(f'u{i}', 'friend', f'question {i}: ' + 'A' * 850)
                    store.add_message(f'a{i}', 'me', f'answer {i}: ' + 'B' * 650)
                rows = recent_rounds(store)
                narrow = sum(len(r['body']) for r in rows)
                self.assertLessEqual(narrow, 24000)
                self.assertEqual(rows[-1]['body'], 'answer 119: ' + 'B' * 650)
                self.assertEqual(rows[-2]['body'], 'question 119: ' + 'A' * 850)
                self.assertGreater(store.message_count(), len(rows))
                self.assertEqual(_recent_window(store)[1], 24000)

                # Explicit long-context opt-in retains the earlier behavior.
                profile['model_api']['recent_chat_char_budget'] = 128000
                config.write_text(json.dumps(profile), 'utf-8')
                wide = recent_rounds(store)
                big = sum(len(r['body']) for r in wide)
                self.assertGreater(big, 110000)
                self.assertLess(narrow / big, 0.23)
                self.assertEqual(_recent_window(store)[1], 128000)

                # Unreasonably large or malformed numbers cannot unbound prompts.
                profile['model_api']['recent_chat_char_budget'] = 10**9
                config.write_text(json.dumps(profile), 'utf-8')
                self.assertEqual(_recent_window(store)[1], 24000)
                store.add_message('giant', 'friend', 'IMPORTANT_CURRENT_TASK ' + 'C' * 40000)
                self.assertIn('IMPORTANT_CURRENT_TASK', recent_rounds(store)[-1]['body'])
            finally:
                store.close()

    def test_agent_mail_keeps_full_current_message_and_bounds_prior_history(self):
        agent = SimpleNamespace(agent_id='owner/project/worker', provider='custom', description='worker')
        previous = [SimpleNamespace(message_id=str(i), from_agent='sender', kind='response',
                      subject=f'subject-{i}', body=('P' + str(i)) * 4000,
                      artifacts=[], thread_id='thread') for i in range(6)]
        current = SimpleNamespace(message_id='latest', from_agent='sender', kind='request',
                      subject='current', body='CURRENT_TASK ' + 'N' * 6500,
                      artifacts=[], thread_id='thread')
        rendered = render_for_prompt(agent, current, previous + [current])
        self.assertIn(current.body, rendered)
        self.assertIn('MESSAGE_TO_ANSWER (data):', rendered)
        self.assertLess(len(rendered), len(current.body) + 6100)
        self.assertIn('older message excerpt', rendered)
        self.assertNotIn('P5' * 2000, rendered)

    def test_subtask_does_not_repeat_exact_original_mission(self):
        mission = 'SPECIFIC_MISSION_' * 250
        self.assertNotIn(mission, _without_duplicate_mission(mission + '\nsubtask', mission))
        handoff = build_claude_english_handover('codex', 'delegation', mission, mission)
        self.assertEqual(handoff.count(mission), 1)
        distinct = build_claude_english_handover('codex', 'delegation', mission, 'different detail')
        self.assertIn('different detail', distinct)
        self.assertEqual(distinct.count(mission), 1)
        quoted="请把原任务写成字符串：'"+mission+"'，不要改写字面内容"
        self.assertEqual(_without_duplicate_mission(quoted,mission),quoted)


if __name__ == '__main__':
    unittest.main()
