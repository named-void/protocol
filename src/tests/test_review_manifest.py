from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "skills" / "checker" / "scripts" / "review_manifest.py"


def git(work: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(work), *arguments], text=True, capture_output=True, check=True
    ).stdout.strip()


class ReviewManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.work = self.root / "service"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "config", "user.email", "test@example.test")
        git(self.work, "config", "user.name", "test")
        self.base = self.commit("a.txt", "one")
        self.candidate = self.commit("a.txt", "two")
        self.evidence = self.root / "verify.log"
        self.evidence.write_text("ok\n", encoding="utf-8")
        self.task = self.root / "task"
        self.review = self.task / "review-1" / "technical"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def commit(self, name: str, content: str) -> str:
        (self.work / name).write_text(content, encoding="utf-8")
        git(self.work, "add", name)
        git(self.work, "commit", "-q", "-m", content)
        return git(self.work, "rev-parse", "HEAD")

    def draft(self, **overrides: object) -> Path:
        repository = {
            "name": "service",
            "work_root": str(self.work),
            "base": self.base[:10],
            "candidate": self.candidate,
            "evidence": [{"path": str(self.evidence)}],
        }
        manifest = {
            "mode": "technical",
            "repositories": [repository],
            "progress": str(self.review / "progress"),
            "result": str(self.review / "result"),
            **overrides,
        }
        path = self.root / "draft.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def run_script(self, *arguments: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments], text=True, capture_output=True, check=False
        )
        return completed.returncode, json.loads(completed.stdout)

    def seal(self, draft: Path) -> tuple[int, dict]:
        return self.run_script("seal", str(draft), str(self.review / "manifest"))

    def test_sealed_manifest_verifies_and_digest_follows_documented_formula(self) -> None:
        code, output = self.seal(self.draft())
        self.assertEqual(0, code, output)

        manifest = json.loads((self.review / "manifest").read_text(encoding="utf-8"))
        repository = manifest["repositories"][0]
        self.assertEqual(self.base, repository["base"])
        self.assertEqual(git(self.work, "rev-parse", "HEAD^{tree}"), repository["candidate_tree"])
        payload = {k: v for k, v in manifest.items() if k not in ("input_digest", "progress", "result")}
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), output["input_digest"])

        code, verified = self.run_script("verify", str(self.review / "manifest"), "--digest", output["input_digest"])
        self.assertEqual((0, "ok"), (code, verified["status"]), verified)

    def test_verify_reports_stale_after_inputs_change(self) -> None:
        self.assertEqual(0, self.seal(self.draft())[0])
        manifest = str(self.review / "manifest")

        self.evidence.write_text("changed\n", encoding="utf-8")
        code, output = self.run_script("verify", manifest)
        self.assertEqual(3, code)
        self.assertTrue(any("verify.log sha256" in reason for reason in output["reasons"]), output)

        self.evidence.write_text("ok\n", encoding="utf-8")
        self.commit("a.txt", "three")
        code, output = self.run_script("verify", manifest)
        self.assertEqual(3, code)
        self.assertTrue(any("HEAD" in reason for reason in output["reasons"]), output)

    def test_rule_change_keeps_pass_valid_while_task_sources_stay_pinned(self) -> None:
        rule = self.root / "rules.md"
        rule.write_text("v1\n", encoding="utf-8")
        spec = self.task / "inputs" / "spec.json"
        spec.parent.mkdir(parents=True)
        spec.write_text("v1\n", encoding="utf-8")
        code, output = self.seal(self.draft(rule_files=[{"path": str(rule)}], sources=[{"path": str(spec)}]))
        self.assertEqual(0, code, output)
        manifest = str(self.review / "manifest")

        rule.write_text("v2\n", encoding="utf-8")
        code, verified = self.run_script("verify", manifest, "--digest", output["input_digest"])
        self.assertEqual((0, "ok"), (code, verified["status"]), verified)

        spec.write_text("v2\n", encoding="utf-8")
        code, verified = self.run_script("verify", manifest)
        self.assertEqual(3, code)
        self.assertTrue(any("spec.json sha256" in reason for reason in verified["reasons"]), verified)

        state = self.task / "state.md"
        state.write_text("status: accept\n", encoding="utf-8")
        (self.review / "manifest").unlink()
        for draft in (self.draft(rule_files=[{"path": str(spec)}]), self.draft(sources=[{"path": str(state)}])):
            code, output = self.seal(draft)
            self.assertEqual(2, code, output)

    def test_seal_refuses_dirty_tree_and_existing_manifest(self) -> None:
        (self.work / "a.txt").write_text("dirty", encoding="utf-8")
        code, output = self.seal(self.draft())
        self.assertEqual(3, code)
        self.assertIn("service: worktree is dirty", output["reasons"])
        self.assertFalse((self.review / "manifest").exists())

        git(self.work, "checkout", "-q", "--", "a.txt")
        self.assertEqual(0, self.seal(self.draft())[0])
        code, output = self.seal(self.draft())
        self.assertEqual(2, code)
        self.assertIn("immutable", output["reasons"][0])

    def test_requirements_fix_after_rewrite_uses_checked_tree_instead_of_ancestry(self) -> None:
        checked = self.candidate
        checked_tree = git(self.work, "rev-parse", f"{checked}^{{tree}}")
        git(self.work, "reset", "-q", "--hard", self.base)
        self.commit("a.txt", "two rewritten")
        self.commit("b.txt", "fix")

        def fix_draft(tree: str) -> Path:
            draft = self.draft(mode="requirements-fix", targets=[{"id": "F1"}])
            manifest = json.loads(draft.read_text(encoding="utf-8"))
            manifest["repositories"][0].update(base=checked, checked_tree=tree, candidate="HEAD")
            draft.write_text(json.dumps(manifest), encoding="utf-8")
            return draft

        code, output = self.seal(fix_draft("0" * 40))
        self.assertEqual(3, code)
        self.assertTrue(any("checked_tree" in reason for reason in output["reasons"]), output)

        code, output = self.seal(fix_draft(checked_tree))
        self.assertEqual(0, code, output)


if __name__ == "__main__":
    unittest.main()
