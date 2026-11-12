import tempfile
import unittest
from pathlib import Path

import skill_registry


class SkillRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        skill = root / "demo-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: Demo skill body\n---\n\nDo the demo.", encoding="utf-8")
        plugin = root / "plugins" / "demo-plugin"
        plugin.mkdir(parents=True)
        self._old_skills, self._old_plugins = skill_registry.SKILL_ROOTS, skill_registry.PLUGIN_ROOTS
        skill_registry.SKILL_ROOTS = ((root, "Test"),)
        skill_registry.PLUGIN_ROOTS = ((root / "plugins", "Test"),)
        skill_registry._CACHE.clear()

    def tearDown(self):
        skill_registry.SKILL_ROOTS, skill_registry.PLUGIN_ROOTS = self._old_skills, self._old_plugins
        skill_registry._CACHE.clear()
        self.temp.cleanup()

    def test_lists_skill_and_plugin(self):
        self.assertEqual([item.slug for item in skill_registry.list_skills()], ["demo-skill"])
        self.assertEqual([item.name for item in skill_registry.list_plugins()], ["demo-plugin"])

    def test_slash_injects_body_and_mentions_are_labeled(self):
        out = skill_registry.expand("/demo-skill 做一下")
        self.assertIn("[SKILL /demo-skill", out)
        self.assertIn("Do the demo.", out)
        self.assertIn("做一下", out)
        mention = skill_registry.expand('@plugin:demo-plugin @folder:"D:/tmp" 看看')
        self.assertIn("[REFERENCED_PLUGIN] demo-plugin", mention)
        self.assertIn("[REFERENCED_FOLDER]", mention)

    def test_builtin_slash_is_not_treated_as_skill(self):
        self.assertEqual(skill_registry.expand("/clear"), "/clear")


if __name__ == "__main__":
    unittest.main()
