from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
DESCRIPTION_WITH_TRIGGER = re.compile(
    r"^description:\s*.*\bТриггер(?:ы)?\s*:", re.MULTILINE
)


class SkillDescriptionsTest(unittest.TestCase):
    def test_every_skill_description_declares_a_trigger(self) -> None:
        skill_files = sorted((ROOT / "skills").rglob("SKILL.md"))
        self.assertTrue(skill_files)

        for skill_file in skill_files:
            frontmatter = skill_file.read_text(encoding="utf-8").split("---", 2)
            self.assertEqual(3, len(frontmatter), skill_file)
            self.assertRegex(
                frontmatter[1], DESCRIPTION_WITH_TRIGGER, skill_file
            )


if __name__ == "__main__":
    unittest.main()
