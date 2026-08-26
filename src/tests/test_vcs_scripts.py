from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "skills" / "git-workflow" / "scripts" / "sync-branch.sh"
REMOVE_SCRIPT = ROOT / "skills" / "git-workflow" / "scripts" / "remove-task-worktree.sh"


class GitScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def git(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        )

    def git_output(self, cwd: Path, *arguments: str) -> str:
        return self.git(cwd, *arguments).stdout.strip()

    def configure_user(self, repository: Path) -> None:
        self.git(repository, "config", "user.name", "Protocol Test")
        self.git(repository, "config", "user.email", "protocol@example.test")

    def remote_repository(
        self,
        *,
        branch: str = "develop",
        remote_path: str = "origin.git",
    ) -> tuple[Path, Path]:
        remote = self.root / remote_path
        seed = self.root / "seed"
        remote.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "init", "--bare", f"--initial-branch={branch}", str(remote))
        self.git(self.root, "init", "-b", branch, str(seed))
        self.configure_user(seed)
        self.git(seed, "commit", "--allow-empty", "-m", "initial")
        self.git(seed, "remote", "add", "origin", str(remote))
        self.git(seed, "push", "-u", "origin", branch)
        return remote, seed

    def clone(self, remote: Path, name: str = "work") -> Path:
        repository = self.root / name
        self.git(self.root, "clone", str(remote), str(repository))
        self.configure_user(repository)
        return repository

    def worktree_path(self, repository: Path, key: str) -> Path:
        resolved = repository.resolve()
        return resolved.parent / f"{resolved.name}-{key}"

    def run_script(
        self,
        repository: Path,
        key: str,
        branch_type: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), key, branch_type],
            cwd=cwd or repository,
            env=env or os.environ.copy(),
            check=False,
            text=True,
            capture_output=True,
        )


class SyncBranchTest(GitScriptTest):
    def task_key_from(self, cwd: Path) -> subprocess.CompletedProcess[str]:
        library = SCRIPT.parent / "lib-git.sh"
        return subprocess.run(
            ["bash", "-c", f'source "{library}"; task_key_from_worktree'],
            cwd=cwd,
            env=os.environ.copy(),
            check=False,
            text=True,
            capture_output=True,
        )

    def test_task_worktree_directory_names_the_task(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-35", "feature").returncode)

        inside = self.task_key_from(self.worktree_path(work, "UPL-35"))
        outside = self.task_key_from(work)

        self.assertEqual(0, inside.returncode, inside.stderr)
        self.assertEqual("UPL-35", inside.stdout.strip())
        self.assertNotEqual(0, outside.returncode)
        self.assertEqual("", outside.stdout.strip())

    def test_creates_task_branch_in_isolated_worktree(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        (work / "local-note.txt").write_text("keep\n", encoding="utf-8")

        result = self.run_script(work, "UPL-20", "feature")

        task_worktree = self.worktree_path(work, "UPL-20")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-20 (from develop) at {task_worktree}\n",
            result.stdout,
        )
        self.assertEqual("feature/UPL-20", self.git_output(task_worktree, "branch", "--show-current"))
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))
        self.assertTrue((work / "local-note.txt").is_file())

    def test_other_task_branch_in_checkout_does_not_become_the_base(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.git(work, "switch", "-c", "feature/UPL-29")
        (work / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        self.git(work, "add", "foreign.txt")
        self.git(work, "commit", "-m", "UPL-29 work")

        result = self.run_script(work, "UPL-30", "feature")

        task_worktree = self.worktree_path(work, "UPL-30")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-30 (from develop) at {task_worktree}\n",
            result.stdout,
        )
        self.assertFalse((task_worktree / "foreign.txt").exists())
        self.assertEqual("feature/UPL-29", self.git_output(work, "branch", "--show-current"))

    def test_second_task_in_same_repository_gets_its_own_worktree(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-31", "feature").returncode)

        result = self.run_script(work, "UPL-32", "feature")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-32 (from develop) at {self.worktree_path(work, 'UPL-32')}\n",
            result.stdout,
        )
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))

    def test_task_branch_checked_out_in_main_checkout_stays_in_place(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.git(work, "switch", "-c", "feature/UPL-33")

        result = self.run_script(work, "UPL-33", "feature")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(f"current: feature/UPL-33 at {work.resolve()}\n", result.stdout)
        self.assertFalse(self.worktree_path(work, "UPL-33").exists())

    def test_always_splits_task_branch_out_of_main_checkout(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.git(work, "switch", "-c", "feature/UPL-34")

        result = self.run_script(
            work,
            "UPL-34",
            "feature",
            env={**os.environ, "AGENTS_TASK_WORKTREE": "always"},
        )

        task_worktree = self.worktree_path(work, "UPL-34")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(f"switched: feature/UPL-34 at {task_worktree}\n", result.stdout)
        self.assertEqual("feature/UPL-34", self.git_output(task_worktree, "branch", "--show-current"))
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))

    def test_checks_out_existing_remote_task_branch(self) -> None:
        remote, seed = self.remote_repository()
        self.git(seed, "switch", "-c", "bugfix/UPL-21")
        (seed / "remote.txt").write_text("remote\n", encoding="utf-8")
        self.git(seed, "add", "remote.txt")
        self.git(seed, "commit", "-m", "remote task")
        self.git(seed, "push", "-u", "origin", "bugfix/UPL-21")
        work = self.clone(remote)

        result = self.run_script(work, "UPL-21", "bugfix")

        task_worktree = self.worktree_path(work, "UPL-21")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(f"switched: bugfix/UPL-21 at {task_worktree}\n", result.stdout)
        self.assertEqual("remote", (task_worktree / "remote.txt").read_text().strip())

    def test_resume_fast_forwards_clean_task_worktree(self) -> None:
        remote, seed = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-22", "feature").returncode)
        task_worktree = self.worktree_path(work, "UPL-22")
        self.git(task_worktree, "push", "-u", "origin", "feature/UPL-22")
        self.git(seed, "fetch", "origin")
        self.git(seed, "switch", "-c", "feature/UPL-22", "origin/feature/UPL-22")
        self.git(seed, "commit", "--allow-empty", "-m", "remote progress")
        self.git(seed, "push", "origin", "feature/UPL-22")

        result = self.run_script(work, "UPL-22", "feature")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(f"current: feature/UPL-22 at {task_worktree}\n", result.stdout)
        self.assertEqual(
            self.git_output(work, "rev-parse", "origin/feature/UPL-22"),
            self.git_output(task_worktree, "rev-parse", "HEAD"),
        )

    def test_dirty_task_worktree_blocks_resume(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-23", "feature").returncode)
        (self.worktree_path(work, "UPL-23") / "pending.txt").write_text("pending\n")

        result = self.run_script(work, "UPL-23", "feature")

        self.assertEqual(10, result.returncode)
        self.assertIn("uncommitted changes", result.stderr)

    def test_diverged_task_branch_blocks_resume(self) -> None:
        remote, seed = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-24", "feature").returncode)
        task_worktree = self.worktree_path(work, "UPL-24")
        self.git(task_worktree, "push", "-u", "origin", "feature/UPL-24")
        self.git(task_worktree, "commit", "--allow-empty", "-m", "local progress")
        self.git(seed, "fetch", "origin")
        self.git(seed, "switch", "-c", "feature/UPL-24", "origin/feature/UPL-24")
        self.git(seed, "commit", "--allow-empty", "-m", "remote progress")
        self.git(seed, "push", "origin", "feature/UPL-24")

        result = self.run_script(work, "UPL-24", "feature")

        self.assertEqual(12, result.returncode)
        self.assertIn("diverged", result.stderr)

    def test_missing_origin_requires_explicit_local_only_mode(self) -> None:
        work = self.root / "work"
        self.git(self.root, "init", "-b", "develop", str(work))
        self.configure_user(work)
        self.git(work, "commit", "--allow-empty", "-m", "initial")

        refused = self.run_script(work, "UPL-25", "feature")
        allowed = self.run_script(
            work,
            "UPL-25",
            "feature",
            env={**os.environ, "AGENTS_LOCAL_ONLY": "1"},
        )

        self.assertEqual(15, refused.returncode)
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertIn("created: feature/UPL-25", allowed.stdout)

    def test_multiple_task_branch_types_are_ambiguous(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.git(work, "branch", "feature/UPL-26")
        self.git(work, "branch", "bugfix/UPL-26")

        result = self.run_script(work, "UPL-26", "feature")

        self.assertEqual(11, result.returncode)
        self.assertIn("multiple task branches", result.stderr)

    def test_key_without_a_tracker_source_allows_the_protocol_type(self) -> None:
        remote, _ = self.remote_repository(branch="main")
        work = self.clone(remote)

        result = self.run_script(
            work,
            "ord-service-7",
            "protocol",
            env={**os.environ, "AGENTS_BASE_BRANCH": "main"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("created: protocol/ord-service-7 (from main)", result.stdout)

    def test_key_derived_from_a_tracker_key_keeps_its_case_and_type(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)

        result = self.run_script(work, "upl-940-6", "feature")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            f"created: feature/UPL-940-6 (from develop) at {self.worktree_path(work, 'UPL-940-6')}",
            result.stdout,
        )

    def test_infrastructure_repository_falls_back_to_master(self) -> None:
        remote, _ = self.remote_repository(
            branch="master",
            remote_path="group/infrastructure/project.git",
        )
        work = self.clone(remote)

        result = self.run_script(work, "UPL-27", "feature")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("created: feature/UPL-27 (from master)", result.stdout)


class RemoveTaskWorktreeTest(GitScriptTest):
    """Дерево уносит evidence вне Git: удаление без подтверждённой копии
    оставляет зафиксированные SHA-256 без предмета сверки."""

    def local_repository(self) -> Path:
        repository = self.root / "work"
        self.git(self.root, "init", "-b", "develop", str(repository))
        self.configure_user(repository)
        (repository / ".gitignore").write_text("evidence/\n", encoding="utf-8")
        self.git(repository, "add", ".gitignore")
        self.git(repository, "commit", "-m", "initial")
        return repository

    def task_worktree(self, repository: Path, key: str) -> Path:
        result = self.run_script(
            repository,
            key,
            "feature",
            env={**os.environ, "AGENTS_LOCAL_ONLY": "1"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return self.worktree_path(repository, key)

    def evidence(self, worktree: Path, name: str = "swagger.json") -> tuple[str, str]:
        relative = f"evidence/{name}"
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return relative, digest

    def archive(self) -> Path:
        directory = self.root / "implementation-1"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def remove(self, repository: Path, key: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(REMOVE_SCRIPT), "remove", key, *arguments],
            cwd=repository,
            env=os.environ.copy(),
            check=False,
            text=True,
            capture_output=True,
        )

    def test_archives_evidence_before_removing_the_worktree(self) -> None:
        repository = self.local_repository()
        worktree = self.task_worktree(repository, "UPL-40")
        relative, digest = self.evidence(worktree)
        archive = self.archive()

        result = self.remove(repository, "UPL-40", str(archive), f"{digest}:{relative}")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(worktree.exists())
        self.assertEqual("generated\n", (archive / relative).read_text(encoding="utf-8"))
        self.assertIn("feature/UPL-40", self.git_output(repository, "branch", "--list"))

    def test_unverified_evidence_keeps_the_worktree(self) -> None:
        repository = self.local_repository()
        worktree = self.task_worktree(repository, "UPL-41")
        relative, _ = self.evidence(worktree)
        archive = self.archive()

        result = self.remove(repository, "UPL-41", str(archive), f"{'0' * 64}:{relative}")

        self.assertEqual(31, result.returncode)
        self.assertTrue(worktree.exists())
        self.assertFalse((archive / relative).exists())

    def test_missing_evidence_keeps_the_worktree(self) -> None:
        repository = self.local_repository()
        worktree = self.task_worktree(repository, "UPL-42")
        archive = self.archive()

        result = self.remove(repository, "UPL-42", str(archive), f"{'0' * 64}:evidence/absent.json")

        self.assertEqual(30, result.returncode)
        self.assertTrue(worktree.exists())

    def test_uncommitted_changes_keep_the_worktree(self) -> None:
        repository = self.local_repository()
        worktree = self.task_worktree(repository, "UPL-43")
        (worktree / "pending.txt").write_text("pending\n", encoding="utf-8")

        result = self.remove(repository, "UPL-43", str(self.archive()))

        self.assertEqual(10, result.returncode)
        self.assertTrue(worktree.exists())

    def test_stash_entry_keeps_the_worktree(self) -> None:
        repository = self.local_repository()
        worktree = self.task_worktree(repository, "UPL-44")
        (worktree / "pending.txt").write_text("pending\n", encoding="utf-8")
        self.git(worktree, "add", "pending.txt")
        self.git(worktree, "stash")

        result = self.remove(repository, "UPL-44", str(self.archive()))

        self.assertEqual(11, result.returncode)
        self.assertTrue(worktree.exists())

    def test_detached_head_keeps_the_worktree(self) -> None:
        repository = self.local_repository()
        worktree = self.task_worktree(repository, "UPL-47")
        self.git(worktree, "checkout", "--detach")
        self.git(worktree, "commit", "--allow-empty", "-m", "work outside a branch")

        result = self.remove(repository, "UPL-47", str(self.archive()))

        self.assertEqual(26, result.returncode)
        self.assertTrue(worktree.exists())

    def test_list_reports_state_and_merge_state(self) -> None:
        repository = self.local_repository()
        merged = self.task_worktree(repository, "UPL-45")
        unmerged = self.task_worktree(repository, "UPL-46")
        (unmerged / "tracked.txt").write_text("work\n", encoding="utf-8")
        self.git(unmerged, "add", "tracked.txt")
        self.git(unmerged, "commit", "-m", "UPL-46 work")

        result = subprocess.run(
            [str(REMOVE_SCRIPT), "list"],
            cwd=repository,
            env=os.environ.copy(),
            check=False,
            text=True,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"UPL-45 clean merged {merged}", result.stdout)
        self.assertIn(f"UPL-46 clean unmerged {unmerged}", result.stdout)

if __name__ == "__main__":
    unittest.main()
