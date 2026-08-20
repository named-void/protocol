from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "skills" / "git-workflow" / "scripts" / "sync-branch.sh"
ACCEPT = ROOT / "skills" / "git-workflow" / "scripts" / "accept-worktree.sh"


class VcsScriptTestCase(unittest.TestCase):
    """Общие git-хелперы тестов vcs-скриптов: временный репозиторий,
    обёртки git, seed-remote и clone. Скрипт-специфичные пути worktree и
    запуск конкретного скрипта живут в наследниках."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        )

    def git_output(self, cwd: Path, *args: str) -> str:
        return self.git(cwd, *args).stdout.strip()

    def upstream(self, repository: Path, branch: str) -> str:
        return self.git_output(
            repository,
            "for-each-ref",
            "--format=%(upstream:short)",
            f"refs/heads/{branch}",
        )

    def configure_user(self, repository: Path) -> None:
        self.git(repository, "config", "user.name", "Tasks Test")
        self.git(repository, "config", "user.email", "tasks@example.test")

    def remote_repository(
        self,
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


class SyncBranchTest(VcsScriptTestCase):
    def worktree_path(self, repository: Path, key: str, label: str = "claude") -> Path:
        # git resolves symlinks (e.g. macOS /var -> /private/var) when
        # printing --path-format=absolute paths, so match that here.
        resolved = repository.resolve()
        return resolved.parent / f"{resolved.name}-{key}-{label}"

    def run_script(
        self,
        repository: Path,
        key: str,
        branch_type: str,
        label: str | None = "claude",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        script: Path = SCRIPT,
    ) -> subprocess.CompletedProcess[str]:
        args = [str(script), key, branch_type]
        if label is not None:
            args.append(label)
        return subprocess.run(
            args,
            cwd=cwd or repository,
            check=False,
            env=env,
            text=True,
            capture_output=True,
        )

    def test_existing_worktree_recreates_deleted_local_canonical_from_origin(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        created = self.run_script(work, "ABC-9", "feature")
        self.assertEqual(0, created.returncode, created.stderr)
        # Каноническая опубликована на origin, локальная копия удалена, пока
        # worktree агента продолжает существовать.
        self.git(work, "push", "origin", "feature/ABC-9")
        self.git(work, "branch", "-D", "feature/ABC-9")

        resumed = self.run_script(work, "ABC-9", "feature")

        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        self.assertIn("current: feature/ABC-9", resumed.stdout)
        self.assertNotIn("fatal", resumed.stderr)
        # Локальная каноническая восстановлена из origin.
        self.git(work, "rev-parse", "--verify", "refs/heads/feature/ABC-9")

    def accept(
        self,
        repository: Path,
        key: str,
        branch_type: str,
        title: str,
        label: str = "claude",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        expected_base: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        effective_env = dict(env or os.environ)
        if expected_base is not None:
            effective_env["EXPECTED_CANONICAL_BASE"] = expected_base
        elif "EXPECTED_CANONICAL_BASE" not in effective_env:
            normalized_key = key if key.startswith("common-") else key.upper()
            canonical_ref = f"refs/heads/{branch_type}/{normalized_key}"
            resolved = subprocess.run(
                ["git", "rev-parse", "--verify", canonical_ref],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
            effective_env["EXPECTED_CANONICAL_BASE"] = (
                resolved.stdout.strip() if resolved.returncode == 0 else "0" * 40
            )
        return subprocess.run(
            [str(ACCEPT), key, branch_type, title, label],
            cwd=cwd or repository,
            check=False,
            env=effective_env,
            text=True,
            capture_output=True,
        )

    def test_common_task_uses_protocol_branch_from_explicit_base(self) -> None:
        remote, _ = self.remote_repository(branch="main")
        work = self.clone(remote)
        env = {
            **os.environ,
            "AGENTS_LOCAL_ONLY": "1",
            "AGENTS_BASE_BRANCH": "main",
        }

        created = self.run_script(work, "common-7", "protocol", env=env)

        self.assertEqual(0, created.returncode, created.stdout + created.stderr)
        self.assertIn("created: protocol/common-7 (from main)", created.stdout)
        task_worktree = self.worktree_path(work, "common-7")
        (task_worktree / "result.txt").write_text("done\n", encoding="utf-8")

        accepted = self.accept(
            work,
            "common-7",
            "protocol",
            "common-7 выполнена локальная задача",
            env=env,
        )

        self.assertEqual(0, accepted.returncode, accepted.stdout + accepted.stderr)
        self.assertFalse(task_worktree.exists())
        self.git(work, "show", "protocol/common-7:result.txt")

    def test_issue_revision_uses_previous_canonical_branch_as_base(self) -> None:
        remote, seed = self.remote_repository()
        self.git(seed, "checkout", "-b", "feature/UPL-819")
        self.git(seed, "commit", "--allow-empty", "-m", "original task")
        self.git(seed, "push", "-u", "origin", "feature/UPL-819")
        work = self.clone(remote)
        env = {**os.environ, "AGENTS_BASE_BRANCH": "feature/UPL-819"}

        created = self.run_script(work, "UPL-819-2", "feature", env=env)

        self.assertEqual(0, created.returncode, created.stdout + created.stderr)
        self.assertIn(
            "created: feature/UPL-819-2 (from feature/UPL-819)",
            created.stdout,
        )
        task_worktree = self.worktree_path(work, "UPL-819-2")
        (task_worktree / "revision.txt").write_text("fixed\n", encoding="utf-8")

        accepted = self.accept(
            work,
            "UPL-819-2",
            "feature",
            "UPL-819-2 исправил существенный недостаток",
            env=env,
        )

        self.assertEqual(0, accepted.returncode, accepted.stdout + accepted.stderr)
        self.assertFalse(task_worktree.exists())
        self.git(work, "show", "feature/UPL-819-2:revision.txt")

    def test_issue_revision_number_starts_at_two(self) -> None:
        work = self.root / "work"
        self.git(self.root, "init", "-b", "develop", str(work))
        self.configure_user(work)
        self.git(work, "commit", "--allow-empty", "-m", "initial")

        result = self.run_script(work, "UPL-819-1", "feature")

        self.assertEqual(64, result.returncode)
        self.assertIn("invalid task key", result.stderr)

    def test_diverged_branch_does_not_switch_head(self) -> None:
        remote, seed = self.remote_repository()
        self.git(seed, "checkout", "-b", "feature/UPL-1")
        self.git(seed, "commit", "--allow-empty", "-m", "feature base")
        self.git(seed, "push", "-u", "origin", "feature/UPL-1")

        work = self.clone(remote)
        self.git(work, "checkout", "-b", "feature/UPL-1", "--track", "origin/feature/UPL-1")
        self.git(work, "commit", "--allow-empty", "-m", "local change")
        self.git(work, "checkout", "develop")

        self.git(seed, "commit", "--allow-empty", "-m", "remote change")
        self.git(seed, "push")

        result = self.run_script(work, "UPL-1", "feature")

        self.assertEqual(12, result.returncode, result.stderr)
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))
        self.assertIn("diverged", result.stderr)
        self.assertFalse(self.worktree_path(work, "UPL-1").exists())

    def test_diverged_develop_does_not_switch_head(self) -> None:
        remote, seed = self.remote_repository()
        work = self.clone(remote)
        self.git(work, "checkout", "develop")
        self.git(work, "commit", "--allow-empty", "-m", "local develop change")
        self.git(work, "checkout", "-b", "scratch")

        self.git(seed, "commit", "--allow-empty", "-m", "remote develop change")
        self.git(seed, "push")

        result = self.run_script(work, "UPL-7", "feature")

        self.assertEqual(13, result.returncode, result.stderr)
        self.assertEqual("scratch", self.git_output(work, "branch", "--show-current"))
        self.assertIn("develop", result.stderr)
        self.assertFalse(self.worktree_path(work, "UPL-7").exists())

    def test_new_branch_creates_isolated_worktree_even_with_untracked_files(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        (work / "untracked.txt").write_text("pending\n")

        result = self.run_script(work, "UPL-8", "feature")

        worktree = self.worktree_path(work, "UPL-8")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-8 (from develop) via feature/UPL-8-claude at {worktree}\n",
            result.stdout,
        )
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))
        self.assertEqual("feature/UPL-8-claude", self.git_output(worktree, "branch", "--show-current"))
        self.assertTrue((work / "untracked.txt").exists())
        self.assertEqual("", self.upstream(work, "feature/UPL-8"))
        self.assertEqual("", self.upstream(worktree, "feature/UPL-8-claude"))

    def test_only_canonical_branch_names_are_candidates(self) -> None:
        remote, seed = self.remote_repository()
        self.git(seed, "checkout", "-b", "feature/UPL-1-wip")
        self.git(seed, "commit", "--allow-empty", "-m", "wip")
        self.git(seed, "push", "-u", "origin", "feature/UPL-1-wip")
        work = self.clone(remote)

        result = self.run_script(work, "UPL-1", "feature")

        worktree = self.worktree_path(work, "UPL-1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-1 (from develop) via feature/UPL-1-claude at {worktree}\n",
            result.stdout,
        )
        self.assertEqual("feature/UPL-1-claude", self.git_output(worktree, "branch", "--show-current"))

    def test_existing_remote_branch_is_checked_out(self) -> None:
        remote, seed = self.remote_repository()
        self.git(seed, "checkout", "-b", "bugfix/UPL-2")
        self.git(seed, "commit", "--allow-empty", "-m", "bugfix")
        self.git(seed, "push", "-u", "origin", "bugfix/UPL-2")
        work = self.clone(remote)

        result = self.run_script(work, "UPL-2", "bugfix")

        worktree = self.worktree_path(work, "UPL-2")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"switched: bugfix/UPL-2 (existing) via bugfix/UPL-2-claude at {worktree}\n",
            result.stdout,
        )
        self.assertEqual("bugfix/UPL-2-claude", self.git_output(worktree, "branch", "--show-current"))
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))
        self.assertEqual("", self.upstream(work, "bugfix/UPL-2"))
        self.assertEqual("", self.upstream(worktree, "bugfix/UPL-2-claude"))

    def test_lowercase_key_uses_existing_canonical_branch(self) -> None:
        remote, seed = self.remote_repository()
        self.git(seed, "checkout", "-b", "feature/UPL-9")
        self.git(seed, "commit", "--allow-empty", "-m", "feature")
        self.git(seed, "push", "-u", "origin", "feature/UPL-9")
        work = self.clone(remote)

        result = self.run_script(work, "upl-9", "feature")

        worktree = self.worktree_path(work, "UPL-9")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"switched: feature/UPL-9 (existing) via feature/UPL-9-claude at {worktree}\n",
            result.stdout,
        )
        self.assertEqual("feature/UPL-9-claude", self.git_output(worktree, "branch", "--show-current"))

    def test_new_branch_uses_fetched_develop_when_local_branch_lags(self) -> None:
        remote, seed = self.remote_repository()
        work = self.clone(remote)
        self.git(seed, "commit", "--allow-empty", "-m", "new develop commit")
        self.git(seed, "push")

        result = self.run_script(work, "UPL-3", "feature")

        worktree = self.worktree_path(work, "UPL-3")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("feature/UPL-3-claude", self.git_output(worktree, "branch", "--show-current"))
        self.assertEqual(
            self.git_output(work, "rev-parse", "origin/develop"),
            self.git_output(worktree, "rev-parse", "HEAD"),
        )

    def test_missing_origin_has_explicit_error(self) -> None:
        work = self.root / "work"
        self.git(self.root, "init", "-b", "develop", str(work))
        self.configure_user(work)
        self.git(work, "commit", "--allow-empty", "-m", "initial")

        result = self.run_script(work, "UPL-4", "feature")

        self.assertEqual(15, result.returncode)
        self.assertIn("origin", result.stderr)

    def test_local_only_skips_fetch_of_unreachable_origin(self) -> None:
        # Режим local-only не трогает origin, даже когда он настроен.
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.git(work, "remote", "set-url", "origin", str(self.root / "missing.git"))

        denied = self.run_script(work, "UPL-79", "feature")
        self.assertEqual(16, denied.returncode, denied.stderr)

        env = {**os.environ, "AGENTS_LOCAL_ONLY": "1"}
        result = self.run_script(work, "UPL-79", "feature", env=env)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("created: feature/UPL-79", result.stdout)
        # Fetch пропущен — база не сверена с upstream, предупреждаем.
        self.assertIn("may lag behind upstream", result.stderr)

    def test_no_origin_is_allowed_with_env_flag(self) -> None:
        # Режим local-only: локальный репозиторий без origin.
        work = self.root / "work"
        self.git(self.root, "init", "-b", "develop", str(work))
        self.configure_user(work)
        self.git(work, "commit", "--allow-empty", "-m", "initial")
        env = {**os.environ, "AGENTS_LOCAL_ONLY": "1"}

        result = self.run_script(work, "UPL-77", "feature", env=env)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("created: feature/UPL-77", result.stdout)
        self.assertTrue(self.worktree_path(work, "UPL-77").is_dir())

    def test_no_origin_accept_completes_route(self) -> None:
        work = self.root / "work"
        self.git(self.root, "init", "-b", "develop", str(work))
        self.configure_user(work)
        self.git(work, "commit", "--allow-empty", "-m", "initial")
        env = {**os.environ, "AGENTS_LOCAL_ONLY": "1"}
        self.run_script(work, "UPL-78", "feature", env=env)
        worktree = self.worktree_path(work, "UPL-78")
        (worktree / "hello.txt").write_text("hi\n", encoding="utf-8")
        self.git(worktree, "add", "hello.txt")

        result = self.accept(work, "UPL-78", "feature", "UPL-78 добавил hello", env=env)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            "UPL-78 добавил hello",
            self.git_output(work, "log", "--format=%s", "feature/UPL-78"),
        )

    def test_missing_develop_has_explicit_error(self) -> None:
        remote, _ = self.remote_repository(branch="main")
        work = self.clone(remote)

        result = self.run_script(work, "UPL-5", "feature")

        self.assertEqual(17, result.returncode)
        self.assertEqual("main", self.git_output(work, "branch", "--show-current"))
        self.assertIn("develop", result.stderr)
        self.assertFalse(self.worktree_path(work, "UPL-5").exists())

    def test_master_fallback_is_limited_to_infrastructure(self) -> None:
        remote, _ = self.remote_repository(branch="master")
        work = self.clone(remote)

        result = self.run_script(work, "UPL-30", "feature")

        self.assertEqual(17, result.returncode)
        self.assertIn("develop", result.stderr)
        self.assertFalse(self.worktree_path(work, "UPL-30").exists())

    def test_infrastructure_repository_uses_master_when_develop_is_absent(self) -> None:
        remote, _ = self.remote_repository(
            branch="master",
            remote_path="getblogger/upl/infrastructure/httperrors.git",
        )
        work = self.clone(remote)

        result = self.run_script(work, "UPL-31", "feature")

        worktree = self.worktree_path(work, "UPL-31")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-31 (from master) via feature/UPL-31-claude at {worktree}\n",
            result.stdout,
        )
        self.assertEqual("master", self.git_output(work, "branch", "--show-current"))
        self.assertEqual(
            self.git_output(work, "rev-parse", "origin/master"),
            self.git_output(worktree, "rev-parse", "HEAD"),
        )

    def test_multiple_canonical_branches_are_ambiguous(self) -> None:
        remote, seed = self.remote_repository()
        for branch in ("feature/UPL-6", "bugfix/UPL-6"):
            self.git(seed, "checkout", "-b", branch, "develop")
            self.git(seed, "commit", "--allow-empty", "-m", branch)
            self.git(seed, "push", "-u", "origin", branch)
            self.git(seed, "checkout", "develop")
        work = self.clone(remote)

        result = self.run_script(work, "UPL-6", "feature")

        self.assertEqual(11, result.returncode)
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))
        self.assertIn("feature/UPL-6", result.stderr)
        self.assertIn("bugfix/UPL-6", result.stderr)

    def env_without_executor(self) -> dict[str, str]:
        # Автодетект-кейсы не должны зависеть от AGENTS_EXECUTOR тест-раннера.
        return {k: v for k, v in os.environ.items() if k != "AGENTS_EXECUTOR"}

    def test_label_cannot_be_autodetected_from_repo_checkout(self) -> None:
        # Invoke through a symlink in a marker-free temp dir so the check stays
        # hermetic even when the repo itself sits under a .codex/.claude/... path
        # (e.g. a nested git worktree at .claude/worktrees/<name>): the
        # deliberately-unresolved SCRIPT_INVOKED_PATH keeps the clean symlink dir
        # while lib-git.sh is still located via the single-file symlink readlink.
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        clean_link = self.root / "sync-branch.sh"
        clean_link.symlink_to(SCRIPT)

        result = self.run_script(
            work, "UPL-20", "feature", label=None,
            env=self.env_without_executor(), script=clean_link,
        )

        self.assertEqual(19, result.returncode, result.stderr)
        self.assertIn("cannot auto-detect label", result.stderr)
        self.assertIn("AGENTS_EXECUTOR", result.stderr)
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))

    def test_invalid_label_is_rejected(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)

        result = self.run_script(work, "UPL-21", "feature", label="bad_label")

        self.assertEqual(20, result.returncode, result.stderr)
        self.assertIn("bad_label", result.stderr)

    def test_label_from_session_environment(self) -> None:
        # Вызов по абсолютному пути репозитория, label — из окружения.
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        env = self.env_without_executor()
        env["AGENTS_EXECUTOR"] = "codex"

        result = self.run_script(work, "UPL-40", "feature", label=None, env=env)

        worktree = self.worktree_path(work, "UPL-40", "codex")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-40 (from develop) via feature/UPL-40-codex at {worktree}\n",
            result.stdout,
        )

    def test_explicit_label_wins_over_environment(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        env = self.env_without_executor()
        env["AGENTS_EXECUTOR"] = "codex"

        result = self.run_script(work, "UPL-41", "feature", label="claude", env=env)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("via feature/UPL-41-claude", result.stdout)

    def test_invalid_environment_executor_rejected(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        env = self.env_without_executor()
        env["AGENTS_EXECUTOR"] = "Bad_Executor"

        result = self.run_script(work, "UPL-42", "feature", label=None, env=env)

        self.assertEqual(20, result.returncode, result.stderr)
        self.assertIn("AGENTS_EXECUTOR", result.stderr)

    def test_label_is_autodetected_from_installed_skill_path(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        skills_dir = self.root / "fake-home" / ".claude" / "skills" / "git-workflow" / "scripts"
        skills_dir.mkdir(parents=True)
        symlinked_script = skills_dir / "sync-branch.sh"
        symlinked_script.symlink_to(SCRIPT)

        result = subprocess.run(
            [str(symlinked_script), "UPL-22", "feature"],
            cwd=work,
            check=False,
            env=self.env_without_executor(),
            text=True,
            capture_output=True,
        )

        worktree = self.worktree_path(work, "UPL-22", "claude")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-22 (from develop) via feature/UPL-22-claude at {worktree}\n",
            result.stdout,
        )

    def test_resume_reports_current_and_fast_forwards_worktree(self) -> None:
        remote, seed = self.remote_repository()
        work = self.clone(remote)

        first = self.run_script(work, "UPL-23", "feature")
        self.assertEqual(0, first.returncode, first.stderr)
        worktree = self.worktree_path(work, "UPL-23")

        self.git(seed, "checkout", "-b", "feature/UPL-23")
        self.git(seed, "commit", "--allow-empty", "-m", "remote progress")
        self.git(seed, "push", "-u", "origin", "feature/UPL-23")

        second = self.run_script(work, "UPL-23", "feature")

        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(
            f"current: feature/UPL-23 via feature/UPL-23-claude at {worktree}\n",
            second.stdout,
        )
        self.assertEqual(
            self.git_output(work, "rev-parse", "origin/feature/UPL-23"),
            self.git_output(worktree, "rev-parse", "HEAD"),
        )

    def test_resume_works_when_invoked_from_within_worktree(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)

        first = self.run_script(work, "UPL-28", "feature")
        self.assertEqual(0, first.returncode, first.stderr)
        worktree = self.worktree_path(work, "UPL-28")

        second = self.run_script(work, "UPL-28", "feature", cwd=worktree)

        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(
            f"current: feature/UPL-28 via feature/UPL-28-claude at {worktree}\n",
            second.stdout,
        )

    def test_different_labels_get_independent_worktrees_for_same_task(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)

        first = self.run_script(work, "UPL-24", "feature", label="claude")
        self.assertEqual(0, first.returncode, first.stderr)
        claude_worktree = self.worktree_path(work, "UPL-24", "claude")

        second = self.run_script(work, "UPL-24", "feature", label="codex")

        codex_worktree = self.worktree_path(work, "UPL-24", "codex")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertTrue(claude_worktree.exists())
        self.assertTrue(codex_worktree.exists())
        self.assertEqual("feature/UPL-24-claude", self.git_output(claude_worktree, "branch", "--show-current"))
        self.assertEqual("feature/UPL-24-codex", self.git_output(codex_worktree, "branch", "--show-current"))

    def test_dirty_worktree_blocks_resume(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)

        first = self.run_script(work, "UPL-25", "feature")
        self.assertEqual(0, first.returncode, first.stderr)
        worktree = self.worktree_path(work, "UPL-25")
        (worktree / "pending.txt").write_text("wip\n")
        self.git(worktree, "add", "pending.txt")

        second = self.run_script(work, "UPL-25", "feature")

        self.assertEqual(10, second.returncode, second.stderr)
        self.assertIn("uncommitted changes", second.stderr)

    def test_stale_directory_for_new_branch_is_reported(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        worktree = self.worktree_path(work, "UPL-26")
        worktree.mkdir(parents=True)

        result = self.run_script(work, "UPL-26", "feature")

        self.assertEqual(22, result.returncode, result.stderr)
        self.assertIn(str(worktree), result.stderr)

    def test_stale_directory_for_existing_branch_is_reported(self) -> None:
        remote, seed = self.remote_repository()
        self.git(seed, "checkout", "-b", "bugfix/UPL-27")
        self.git(seed, "commit", "--allow-empty", "-m", "bugfix")
        self.git(seed, "push", "-u", "origin", "bugfix/UPL-27")
        work = self.clone(remote)
        worktree = self.worktree_path(work, "UPL-27")
        worktree.mkdir(parents=True)

        result = self.run_script(work, "UPL-27", "bugfix")

        self.assertEqual(22, result.returncode, result.stderr)
        self.assertIn(str(worktree), result.stderr)

    def test_accept_commits_switches_merges_and_removes_agent_worktree(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        sync = self.run_script(work, "UPL-30", "feature")
        self.assertEqual(0, sync.returncode, sync.stderr)
        worktree = self.worktree_path(work, "UPL-30")
        (worktree / "accepted.txt").write_text("accepted\n")

        result = self.accept(work, "UPL-30", "feature", "UPL-30 Добавлена оркестрация")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("feature/UPL-30-claude -> feature/UPL-30", result.stdout)
        self.assertFalse(worktree.exists())
        self.assertEqual("accepted", self.git_output(work, "show", "feature/UPL-30:accepted.txt"))
        self.assertEqual(
            "UPL-30 Добавлена оркестрация",
            self.git_output(work, "log", "-1", "--format=%s", "feature/UPL-30"),
        )
        self.assertNotIn("feature/UPL-30-claude", self.git_output(work, "branch", "--list"))

    def test_accept_can_remove_worktree_when_invoked_from_inside_it(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        sync = self.run_script(work, "UPL-34", "feature")
        self.assertEqual(0, sync.returncode, sync.stderr)
        worktree = self.worktree_path(work, "UPL-34")
        (worktree / "accepted.txt").write_text("accepted\n")

        result = self.accept(
            work,
            "UPL-34",
            "feature",
            "UPL-34 Добавлена оркестрация",
            cwd=worktree,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(worktree.exists())
        self.assertNotIn("feature/UPL-34-claude", self.git_output(work, "branch", "--list"))

    def test_accept_merges_parallel_agent_branch_into_advanced_canonical(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-31", "feature", "claude").returncode)
        self.assertEqual(0, self.run_script(work, "UPL-31", "feature", "codex").returncode)
        claude_worktree = self.worktree_path(work, "UPL-31", "claude")
        codex_worktree = self.worktree_path(work, "UPL-31", "codex")
        (claude_worktree / "claude.txt").write_text("claude\n")
        (codex_worktree / "codex.txt").write_text("codex\n")

        first = self.accept(work, "UPL-31", "feature", "UPL-31 Добавлена часть Claude", "claude")
        second = self.accept(work, "UPL-31", "feature", "UPL-31 Добавлена часть Codex", "codex")

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual("claude", self.git_output(work, "show", "feature/UPL-31:claude.txt"))
        self.assertEqual("codex", self.git_output(work, "show", "feature/UPL-31:codex.txt"))

    def test_accept_rejects_stale_expected_canonical_base_before_commit(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-39", "feature", "claude").returncode)
        self.assertEqual(0, self.run_script(work, "UPL-39", "feature", "codex").returncode)
        canonical_base = self.git_output(work, "rev-parse", "feature/UPL-39")
        claude_worktree = self.worktree_path(work, "UPL-39", "claude")
        codex_worktree = self.worktree_path(work, "UPL-39", "codex")
        (claude_worktree / "claude.txt").write_text("claude\n")
        (codex_worktree / "codex.txt").write_text("codex\n")

        first = self.accept(
            work,
            "UPL-39",
            "feature",
            "UPL-39 Добавлена часть Claude",
            "claude",
            expected_base=canonical_base,
        )
        stale = self.accept(
            work,
            "UPL-39",
            "feature",
            "UPL-39 Добавлена часть Codex",
            "codex",
            expected_base=canonical_base,
        )

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(27, stale.returncode, stale.stderr)
        self.assertIn("stale:", stale.stderr)
        self.assertTrue(codex_worktree.exists())
        self.assertTrue((codex_worktree / "codex.txt").is_file())
        self.assertNotIn("codex.txt", self.git_output(work, "ls-tree", "-r", "--name-only", "feature/UPL-39"))

    def test_accept_detects_remote_canonical_move_before_local_fast_forward(self) -> None:
        remote, seed = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-43", "feature").returncode)
        worktree = self.worktree_path(work, "UPL-43")
        canonical_base = self.git_output(work, "rev-parse", "feature/UPL-43")
        self.git(work, "push", "origin", "feature/UPL-43")
        self.git(seed, "fetch", "origin")
        self.git(seed, "switch", "-c", "feature/UPL-43", "origin/feature/UPL-43")
        self.git(seed, "commit", "--allow-empty", "-m", "remote canonical move")
        self.git(seed, "push", "origin", "feature/UPL-43")
        (worktree / "candidate.txt").write_text("candidate\n")

        stale = self.accept(
            work,
            "UPL-43",
            "feature",
            "UPL-43 Добавлен кандидат",
            expected_base=canonical_base,
        )

        self.assertEqual(27, stale.returncode, stale.stderr)
        self.assertIn("stale:", stale.stderr)
        self.assertTrue(worktree.exists())
        self.assertTrue((worktree / "candidate.txt").is_file())
        self.assertEqual(canonical_base, self.git_output(work, "rev-parse", "feature/UPL-43"))

    def test_accept_rejects_commit_title_without_issue_key(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)

        result = self.accept(work, "UPL-32", "feature", "Добавлена оркестрация")

        self.assertEqual(64, result.returncode)
        self.assertIn("commit title", result.stderr)

    def test_accept_resumes_after_merge_conflict_is_resolved(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-33", "feature", "claude").returncode)
        self.assertEqual(0, self.run_script(work, "UPL-33", "feature", "codex").returncode)
        claude_worktree = self.worktree_path(work, "UPL-33", "claude")
        codex_worktree = self.worktree_path(work, "UPL-33", "codex")
        (claude_worktree / "shared.txt").write_text("claude\n")
        (codex_worktree / "shared.txt").write_text("codex\n")
        self.assertEqual(
            0,
            self.accept(work, "UPL-33", "feature", "UPL-33 Добавлена часть Claude", "claude").returncode,
        )

        conflicted = self.accept(work, "UPL-33", "feature", "UPL-33 Добавлена часть Codex", "codex")
        self.assertEqual(25, conflicted.returncode, conflicted.stderr)
        self.assertTrue(codex_worktree.exists())
        (codex_worktree / "shared.txt").write_text("claude and codex\n")
        self.git(codex_worktree, "add", "shared.txt")

        resumed = self.accept(work, "UPL-33", "feature", "UPL-33 Добавлена часть Codex", "codex")

        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertFalse(codex_worktree.exists())
        self.assertEqual("claude and codex", self.git_output(work, "show", "feature/UPL-33:shared.txt"))

    def test_accept_releases_canonical_holder_parked_on_base(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-35", "feature").returncode)
        worktree = self.worktree_path(work, "UPL-35")
        (worktree / "accepted.txt").write_text("accepted\n")
        # Основной checkout занял канон, стоя ровно на базе, дерево чисто:
        # переключение на базу его содержимого не меняет.
        self.git(work, "switch", "feature/UPL-35")

        result = self.accept(work, "UPL-35", "feature", "UPL-35 Добавлена оркестрация")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("develop", self.git_output(work, "branch", "--show-current"))
        self.assertFalse(worktree.exists())
        self.assertEqual("accepted", self.git_output(work, "show", "feature/UPL-35:accepted.txt"))

    def test_accept_rejects_dirty_canonical_holder(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-36", "feature").returncode)
        (self.worktree_path(work, "UPL-36") / "accepted.txt").write_text("accepted\n")
        self.git(work, "switch", "feature/UPL-36")
        (work / "local.txt").write_text("local\n")

        result = self.accept(work, "UPL-36", "feature", "UPL-36 Добавлена оркестрация")

        self.assertEqual(21, result.returncode, result.stdout + result.stderr)
        self.assertEqual("feature/UPL-36", self.git_output(work, "branch", "--show-current"))

    def test_accept_rejects_canonical_holder_ahead_of_base(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-37", "feature").returncode)
        (self.worktree_path(work, "UPL-37") / "accepted.txt").write_text("accepted\n")
        self.git(work, "switch", "feature/UPL-37")
        self.git(work, "commit", "--allow-empty", "-m", "UPL-37 Своя правка в каноне")

        result = self.accept(work, "UPL-37", "feature", "UPL-37 Добавлена оркестрация")

        self.assertEqual(21, result.returncode, result.stdout + result.stderr)
        self.assertEqual("feature/UPL-37", self.git_output(work, "branch", "--show-current"))

    def test_accept_does_not_release_parallel_agent_worktree(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        self.assertEqual(0, self.run_script(work, "UPL-38", "feature", "claude").returncode)
        self.assertEqual(0, self.run_script(work, "UPL-38", "feature", "codex").returncode)
        claude_worktree = self.worktree_path(work, "UPL-38", "claude")
        codex_worktree = self.worktree_path(work, "UPL-38", "codex")
        (claude_worktree / "accepted.txt").write_text("accepted\n")
        # Worktree соседнего агента встал на канон (например, после аборта
        # своего merge): увод его на базу лишил бы его приёмку resume-пути.
        # База при этом свободна — иначе отказ дал бы сам git, а не проверка.
        self.git(work, "switch", "--detach")
        self.git(codex_worktree, "switch", "feature/UPL-38")

        result = self.accept(work, "UPL-38", "feature", "UPL-38 Добавлена часть Claude", "claude")

        self.assertEqual(21, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "feature/UPL-38",
            self.git_output(codex_worktree, "branch", "--show-current"),
        )
    def test_agents_base_branch_main_changes_base(self) -> None:
        remote, _ = self.remote_repository(branch="main")
        work = self.clone(remote)
        env = {**os.environ, "AGENTS_BASE_BRANCH": "main"}

        result = self.run_script(work, "UPL-50", "feature", env=env)

        worktree = self.worktree_path(work, "UPL-50")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-50 (from main) via feature/UPL-50-claude at {worktree}\n",
            result.stdout,
        )

    def test_branch_types_accepts_nonstandard_type(self) -> None:
        remote, _ = self.remote_repository()
        work = self.clone(remote)
        env = {**os.environ, "AGENTS_BRANCH_TYPES": "feature bugfix hotfix spike"}

        result = self.run_script(work, "UPL-51", "spike", env=env)

        worktree = self.worktree_path(work, "UPL-51")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: spike/UPL-51 (from develop) via spike/UPL-51-claude at {worktree}\n",
            result.stdout,
        )

    def test_fallback_glob_triggered_by_env(self) -> None:
        remote, _ = self.remote_repository(
            branch="master",
            remote_path="group/noninfra/project.git",
        )
        work = self.clone(remote)
        env = {**os.environ, "AGENTS_BASE_BRANCH_FALLBACK_GLOB": "*noninfra/*"}

        result = self.run_script(work, "UPL-52", "feature", env=env)

        worktree = self.worktree_path(work, "UPL-52")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            f"created: feature/UPL-52 (from master) via feature/UPL-52-claude at {worktree}\n",
            result.stdout,
        )
