from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "projects" / "upl" / "final-checks.sh"


class UplFinalChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "service"
        self.repository.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Final Checks Test")
        self.git("config", "user.email", "final-checks@example.test")
        (self.repository / "go.mod").write_text(
            "module example.test/service\n", encoding="utf-8"
        )
        (self.repository / "formatted.go").write_text(
            "package service\n", encoding="utf-8"
        )
        (self.repository / ".gitignore").write_text("api/docs/\n", encoding="utf-8")
        self.git("add", ".gitignore", "go.mod", "formatted.go")
        self.git("commit", "-m", "initial")
        generated = self.repository / "api" / "docs" / "generated.go"
        generated.parent.mkdir(parents=True)
        generated.write_text("package docs\n", encoding="utf-8")

        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.fake_gopath = self.root / "gopath"
        (self.fake_gopath / "bin").mkdir(parents=True)
        self.write_executable(
            self.fake_bin / "go",
            """#!/bin/sh
if [ "$1" = "env" ] && [ "$2" = "GOPATH" ]; then
  printf '%s\n' "$FAKE_GOPATH"
fi
exit 0
""",
        )
        self.write_executable(
            self.fake_bin / "make",
            """#!/bin/sh
if [ "$1" = "-n" ]; then
  exit 0
fi
if [ "${FAKE_MAKE_MUTATES:-0}" = "1" ]; then
  printf 'package service // formatted\n' > formatted.go
fi
if [ "${FAKE_SEED_MUTATES:-0}" = "1" ]; then
  printf 'package docs // regenerated\n' > api/docs/generated.go
fi
exit 0
""",
        )
        self.write_executable(
            self.fake_gopath / "bin" / "shadow", "#!/bin/sh\nexit 0\n"
        )
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.logs = self.root / "logs"
        self.logs.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def git_output(self, *arguments: str) -> str:
        return self.git(*arguments).stdout.strip()

    def run_checks(
        self,
        mode: str,
        *,
        mutates: bool,
        candidate: str | None = None,
        seed_mutates: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "AGENT_SKILLS_DIR": str(self.skills),
            "FAKE_GOPATH": str(self.fake_gopath),
            "FAKE_MAKE_MUTATES": "1" if mutates else "0",
            "FAKE_SEED_MUTATES": "1" if seed_mutates else "0",
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "TMPDIR": str(self.logs),
        }
        if candidate is not None:
            environment["CANDIDATE_REF"] = candidate
        return subprocess.run(
            [str(SCRIPT), mode],
            cwd=self.repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_prepare_reports_post_check_mutation(self) -> None:
        result = self.run_checks("prepare", mutates=True)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("mode: prepare", result.stdout)
        self.assertIn("git status --short: есть изменения", result.stdout)
        self.assertIn(
            "formatted", (self.repository / "formatted.go").read_text(encoding="utf-8")
        )

    def test_verify_uses_disposable_worktree_and_rejects_mutation(self) -> None:
        candidate = self.git_output("rev-parse", "HEAD")

        result = self.run_checks("verify", mutates=True, candidate=candidate)

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"candidate: {candidate}", result.stdout)
        self.assertIn("candidate purity: не пройдено", result.stdout)
        self.assertEqual("", self.git_output("status", "--porcelain"))
        self.assertNotIn(
            "formatted", (self.repository / "formatted.go").read_text(encoding="utf-8")
        )

    def test_verify_passes_clean_candidate_without_mutating_target(self) -> None:
        candidate = self.git_output("rev-parse", "HEAD")

        result = self.run_checks("verify", mutates=False, candidate=candidate)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("mode: verify", result.stdout)
        self.assertIn("git status --short: чисто", result.stdout)
        self.assertEqual("", self.git_output("status", "--porcelain"))

    def test_verify_rejects_changes_to_seeded_ignored_artifact(self) -> None:
        candidate = self.git_output("rev-parse", "HEAD")

        result = self.run_checks(
            "verify",
            mutates=False,
            candidate=candidate,
            seed_mutates=True,
        )

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("seeded artifact purity: не пройдено", result.stdout)
        generated = self.repository / "api" / "docs" / "generated.go"
        self.assertEqual("package docs\n", generated.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
