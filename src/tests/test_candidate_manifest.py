from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "protocol"
    / "scripts"
    / "candidate_manifest.py"
)


class CandidateManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Manifest Test")
        self.git("config", "user.email", "manifest@example.test")
        (self.repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "initial")
        self.start = self.git_output("rev-parse", "HEAD")
        self.git("branch", "feature/ABC-1")
        self.git("switch", "-c", "feature/ABC-1-codex")
        (self.repository / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        self.git("commit", "-am", "candidate")
        self.candidate = self.git_output("rev-parse", "HEAD")

        self.artifact = self.root / "handoff.md"
        self.artifact.write_text("handoff\n", encoding="utf-8")
        self.manifest = self.root / "candidate-manifest.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

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

    def run_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )

    def seal(self, output: Path | None = None) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            "seal",
            "--repository",
            str(self.repository),
            "--implementation-start",
            self.start,
            "--canonical-ref",
            "refs/heads/feature/ABC-1",
            "--canonical-base",
            self.start,
            "--check-base",
            self.start,
            "--candidate",
            self.candidate,
            "--artifact",
            "handoff",
            str(self.artifact),
            "--output",
            str(output or self.manifest),
        )

    def test_seal_and_verify_clean_candidate(self) -> None:
        sealed = self.seal()
        verified = self.run_command("verify", str(self.manifest))

        self.assertEqual(0, sealed.returncode, sealed.stderr)
        self.assertEqual(0, verified.returncode, verified.stderr)
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual("protocol.candidate-manifest/v1", payload["schema"])
        self.assertEqual(self.candidate, payload["refs"]["candidate"])
        self.assertEqual(self.start, payload["refs"]["canonical_base"])
        self.assertEqual(64, len(payload["image_id"]))
        self.assertIn(payload["image_id"], verified.stdout)

    def test_verify_reports_changed_artifact_as_stale(self) -> None:
        self.assertEqual(0, self.seal().returncode)
        self.artifact.write_text("changed\n", encoding="utf-8")

        result = self.run_command("verify", str(self.manifest))

        self.assertEqual(3, result.returncode)
        self.assertIn("stale:", result.stderr)
        self.assertIn("artifact", result.stderr)

    def test_verify_reports_moved_canonical_ref_as_stale(self) -> None:
        self.assertEqual(0, self.seal().returncode)
        self.git("switch", "feature/ABC-1")
        self.git("commit", "--allow-empty", "-m", "canonical moved")
        self.git("switch", "feature/ABC-1-codex")

        result = self.run_command("verify", str(self.manifest))

        self.assertEqual(3, result.returncode)
        self.assertIn("canonical ref", result.stderr)

    def test_seal_refuses_dirty_candidate_and_existing_output(self) -> None:
        (self.repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        dirty = self.seal()
        (self.repository / "untracked.txt").unlink()
        clean = self.seal()
        repeated = self.seal()

        self.assertEqual(2, dirty.returncode)
        self.assertIn("not clean", dirty.stderr)
        self.assertEqual(0, clean.returncode, clean.stderr)
        self.assertEqual(2, repeated.returncode)
        self.assertIn("overwrite", repeated.stderr)

    def test_verify_rejects_corrupt_image_id(self) -> None:
        self.assertEqual(0, self.seal().returncode)
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["image_id"] = "0" * 64
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")

        result = self.run_command("verify", str(self.manifest))

        self.assertEqual(2, result.returncode)
        self.assertIn("image_id mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
