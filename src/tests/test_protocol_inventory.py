from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "skills" / "protocol" / "scripts" / "inventory.py"


def git(work: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(work), *arguments], text=True, capture_output=True, check=True)


class ProtocolInventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.projects = self.root / "projects"
        self.checkouts = self.root / "upl"
        project = self.projects / "upl"
        project.mkdir(parents=True)
        (project / "project.toml").write_text(
            f'[project]\nroots = ["{self.checkouts}"]\n\n'
            '[issue_tracker]\nproduct = "jira"\nhosts = ["jira.example.test"]\n\n'
            '[dispatch]\nkey_prefix_map = { UPL = "group/upl" }\n',
            encoding="utf-8",
        )
        self.tasks = project / ".data" / "tasks"
        self.service = self.checkouts / "services" / "svc"
        self.service.mkdir(parents=True)
        git(self.service, "init", "-q", "-b", "main")
        git(self.service, "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        git(self.service, "branch", "feature/UPL-5-1")
        self.worktree = self.checkouts / "services" / "svc-UPL-5"
        git(self.service, "worktree", "add", "-q", "-b", "feature/UPL-5", str(self.worktree))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_state(self, key: str, status: str, repository_fields: str = "") -> None:
        task_dir = self.tasks / key
        (task_dir / "implementation-1").mkdir(parents=True, exist_ok=True)
        (task_dir / "state.md").write_text(
            f"# {key}\n\n## Состояние\n\n- status: {status}\n- next: проверить\n\n"
            f"### repo: svc\n\n- work_root: {self.worktree}\n- branch: feature/{key}\n{repository_fields}\n"
            "## Findings\n\n- status: ignored\n",
            encoding="utf-8",
        )

    def run_inventory(self, key: str) -> dict:
        environment = {**os.environ, "AGENTS_PROJECTS_DIR": str(self.projects)}
        for name in ("AGENTS_PROJECT", "AGENTS_PROJECT_PATH", "AGENTS_DATA_DIR"):
            environment.pop(name, None)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), key], cwd=str(self.root), env=environment,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_worktree_without_state_is_ambiguous_and_derived_key_is_not_matched(self) -> None:
        output = self.run_inventory("UPL-5")

        self.assertEqual("ambiguous", output["route"])
        [repository] = output["repositories"]
        self.assertEqual(["feature/UPL-5"], repository["branches"])
        self.assertEqual([str(self.worktree)], [tree["path"] for tree in repository["worktrees"]])
        self.assertTrue(repository["worktrees"][0]["clean"])

    def test_route_follows_state_status(self) -> None:
        self.write_state("UPL-5", "implement")
        output = self.run_inventory("upl-5")
        self.assertEqual(("continue", "implement"), (output["route"], output["state"]["status"]))
        self.assertEqual(str(self.worktree), output["state"]["repositories"]["svc"]["work_root"])
        self.assertEqual(["implementation-1"], output["iterations"])

        (self.tasks / "UPL-5" / "state.md").write_text("## Состояние\n\n- status: completed\n", encoding="utf-8")
        self.assertEqual("next-cycle", self.run_inventory("UPL-5")["route"])

    def test_continuable_state_with_missing_work_root_is_ambiguous(self) -> None:
        self.write_state("UPL-7", "accept")
        git(self.service, "worktree", "remove", str(self.worktree))

        output = self.run_inventory("UPL-7")

        self.assertEqual("ambiguous", output["route"])
        self.assertIn("not an available git tree", output["reasons"][0])

    def test_unknown_key_without_artifacts_is_new(self) -> None:
        output = self.run_inventory("UPL-9")

        self.assertEqual(("new", None, []), (output["route"], output["state"], output["repositories"]))

    def test_checked_candidate_is_compared_by_tree_not_sha(self) -> None:
        def head(*arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(self.worktree), "rev-parse", *arguments], text=True, capture_output=True, check=True
            ).stdout.strip()

        def commit(*arguments: str) -> None:
            git(self.worktree, "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-q", *arguments)

        (self.worktree / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.worktree, "add", "a.txt")
        commit("-m", "checked")
        checked_tree = head("HEAD^{tree}")
        commit("--amend", "-m", "hash-only rewrite")
        rewritten = head("HEAD")
        (self.worktree / "a.txt").write_text("b\n", encoding="utf-8")
        commit("-am", "unchecked fix")
        unchecked = head("HEAD")

        def reasons(checked_candidate: str) -> list[str]:
            self.write_state("UPL-5", "accept", f"- checked_candidate: {checked_candidate}\n- checked_tree: {checked_tree}\n")
            output = self.run_inventory("UPL-5")
            self.assertEqual("continue", output["route"])
            return output["reasons"]

        self.assertEqual([], reasons(rewritten))
        self.assertIn("differs from checked_tree", reasons(unchecked)[0])


if __name__ == "__main__":
    unittest.main()
