import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import friend_persona_spec

SCOPE = SimpleNamespace(friend_id='12345', friend_dir=Path('fixture'))
PROFILE = {'qq_id': '12345', 'style': '说话温柔，不要说教。',
           'basic_info': {'nickname': '小明', 'notes': '喜欢猫，电话13800000000'}}


def model(reply):
    return patch('main.codex_text', return_value=(json.dumps(reply, ensure_ascii=False), None))


class FriendPersonaSpecTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('main.load_profile', return_value=PROFILE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_target_spec_uses_notes_and_style_without_identity(self):
        reply = {'traits': ['gentle', 'gentle'], 'behavior_requirements': ['先共情'], 'anti_patterns': ['说教']}
        with model(reply) as codex:
            spec = friend_persona_spec.extract_target_spec(SCOPE)
        self.assertEqual(spec, {'traits': ['gentle'], 'behavior_requirements': ['先共情'],
                                'anti_patterns': ['说教']})
        prompt = codex.call_args.args[0]
        self.assertIn('不要说教', prompt)
        self.assertIn('喜欢猫', prompt)
        self.assertNotIn('小明', prompt)
        self.assertNotIn('13800000000', prompt)
        self.assertTrue(codex.call_args.kwargs['ephemeral'])

    def test_target_spec_rejects_missing_fields_and_empty_settings(self):
        with model({'traits': []}), self.assertRaises(ValueError):
            friend_persona_spec.extract_target_spec(SCOPE)
        with patch('main.load_profile', return_value={'qq_id': '12345', 'style': ''}), \
                self.assertRaises(ValueError):
            friend_persona_spec.extract_target_spec(SCOPE)

    def test_calibration_returns_only_real_changes_on_existing_keys(self):
        claims = [{'claim_key': 'a', 'claim': '外向'}, {'claim_key': 'b', 'claim': 'gentle'}]
        observed = {'length': {'mean': 12.5}, 'emoji_rate': 0.1}
        reply = {'claims': {'a': '内向', 'b': 'gentle'}, 'style': {'length.mean': 8, 'emoji_rate': 0.1}}
        with model(reply):
            overlay, overrides = friend_persona_spec.calibrate_persona(SCOPE, claims, observed)
        self.assertEqual(overlay, {'a': {'claim': '内向'}})
        self.assertEqual(overrides, {'length.mean': 8})

    def test_calibration_rejects_unknown_claims_and_paths(self):
        claims = [{'claim_key': 'a', 'claim': '外向'}]
        for reply in ({'claims': {'x': '新'}, 'style': {}},
                      {'claims': {}, 'style': {'missing': 1}},
                      {'claims': {}, 'style': {'rate': 'high'}}):
            with model(reply), self.assertRaises(ValueError):
                friend_persona_spec.calibrate_persona(SCOPE, claims, {'rate': 0.5})


if __name__ == '__main__':
    unittest.main()
