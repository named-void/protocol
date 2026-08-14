from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "skills" / "protocol" / "scripts" / "resolve_issue.py"


class ProtocolIssueEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.projects = self.root / "projects"
        self.checkout_root = self.root / "upl"
        self.checkout_root.mkdir()
        project = self.projects / "upl"
        project.mkdir(parents=True)
        (project / "project.toml").write_text(
            f'[project]\nroots = ["{self.checkout_root}"]\n\n'
            '[issue_tracker]\nproduct = "jira"\n'
            'hosts = ["jira.example.test"]\n'
            'key_pattern = "^[A-Za-z][A-Za-z0-9]*-[0-9]+$"\n\n'
            '[dispatch]\nkey_prefix_map = { UPL = "group/upl" }\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_resolver(self, url: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "AGENTS_PROJECTS_DIR": str(self.projects)}
        environment.pop("AGENTS_PROJECT", None)
        environment.pop("AGENTS_PROJECT_PATH", None)
        return subprocess.run(
            [sys.executable, str(SCRIPT), url],
            cwd=str(ROOT),
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_resolves_configured_jira_url(self) -> None:
        result = self.run_resolver("https://jira.example.test/browse/upl-892")

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("UPL-892", payload["key"])
        self.assertEqual("upl", payload["project"])
        self.assertEqual([str(self.checkout_root.resolve())], payload["project_roots"])

    def test_rejects_unconfigured_host(self) -> None:
        result = self.run_resolver("https://other.example.test/browse/UPL-892")

        self.assertEqual(2, result.returncode)
        self.assertIn("no project mapping for Jira host", result.stderr)

    def test_rejects_noncanonical_issue_path(self) -> None:
        result = self.run_resolver("https://jira.example.test/issues/UPL-892")

        self.assertEqual(2, result.returncode)
        self.assertIn("expected /browse/<KEY>", result.stderr)

        trailing_slash = self.run_resolver("https://jira.example.test/browse/UPL-892/")
        self.assertEqual(2, trailing_slash.returncode)

    def test_rejects_unknown_key_prefix(self) -> None:
        result = self.run_resolver("https://jira.example.test/browse/OTHER-1")

        self.assertEqual(2, result.returncode)
        self.assertIn("no project mapping for issue key", result.stderr)


if __name__ == "__main__":
    unittest.main()
