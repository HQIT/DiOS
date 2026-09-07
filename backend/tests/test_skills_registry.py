import tempfile
import unittest
from pathlib import Path

from app.api.os.skills import _locate_skill_dir, _parse_skill_dir, _resolve_git_source


class SkillRegistryTest(unittest.TestCase):
    def test_github_tree_url_resolves_repository_and_subdirectory(self):
        result = _resolve_git_source(
            "https://github.com/anthropics/skills/tree/main/skills/mcp-builder"
        )
        self.assertEqual(
            ("https://github.com/anthropics/skills.git", "main", "skills/mcp-builder"),
            result,
        )

    def test_multiple_skills_require_explicit_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for name in ("first", "second"):
                directory = root / name
                directory.mkdir()
                (directory / "SKILL.md").write_text("---\nname: test\ndescription: test\n---\n")
            with self.assertRaisesRegex(ValueError, "多个 Skill"):
                _locate_skill_dir(root, None)

    def test_frontmatter_name_and_description_are_parsed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            (directory / "SKILL.md").write_text(
                "---\nname: code-review\ndescription: Review code when a pull request changes.\n---\n\n# Instructions\n",
                encoding="utf-8",
            )
            name, description, content = _parse_skill_dir(directory, "fallback")
            self.assertEqual("code-review", name)
            self.assertIn("when a pull request", description)
            self.assertIn("# Instructions", content)

    def test_invalid_skill_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            (directory / "SKILL.md").write_text(
                "---\nname: Bad_Name\ndescription: Invalid name.\n---\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Skill name"):
                _parse_skill_dir(directory, "fallback")


if __name__ == "__main__":
    unittest.main()
