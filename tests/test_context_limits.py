import unittest

import codex_settings
import provider_context


class ContextLimitsTests(unittest.TestCase):
    def test_default_codex_is_one_million_and_compacts_at_ninety_percent(self):
        api = {"provider": "codex", "codex_model": "gpt-6.1-sol"}
        info = provider_context.info(api)
        self.assertEqual(info.window, 1_000_000)
        self.assertEqual(info.compact_at, 900_000)
        self.assertEqual(codex_settings.compact_tokens(api), 900_000)

    def test_provider_hard_limits_are_not_exceeded(self):
        self.assertEqual(provider_context.info({"provider": "custom", "model": "x"}).window, 1_000_000)
        self.assertEqual(provider_context.info({"provider": "webchat_deepseek"}).window, 128_000)
        self.assertEqual(provider_context.info({"provider": "webchat_chatgpt"}).window, 400_000)
        self.assertEqual(provider_context.info({"provider": "claude", "claude_model": "claude-haiku"}).window, 1_000_000)

    def test_explicit_override_can_only_lower_the_window(self):
        api = {"provider": "codex", "codex_model": "gpt-6.1-sol", "context_tokens": 2_000_000}
        self.assertEqual(provider_context.info(api).window, 1_000_000)
        api["context_tokens"] = 64_000
        self.assertEqual(provider_context.info(api).compact_at, 57_600)


if __name__ == "__main__":
    unittest.main()
