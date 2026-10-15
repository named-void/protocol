#!/usr/bin/env python3
"""Read-only local inventory of one task before routing.

Takes a Jira URL or issue key, finds `<DATA_ROOT>/tasks/<KEY>/state.md`, task worktrees,
local and remote-tracking branches under the project roots, and proposes a route:
`continue`, `next-cycle`, `ambiguous` or `new`.
For a task with `status: completed` the output also carries `brief`: per repository the
publish inputs (`base`, `candidate`, `branch`, `depends_on`) and whether the task diff
touches `migrations/`.

Exit codes: 0 inventory printed, 2 the issue does not resolve to a project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[2] / "lib"))

import resolve_issue
import skills_config

CONTINUABLE = ("define", "implement", "accept", "blocked")
STATE_FIELDS = ("status", "task", "next", "cycle", "kind", "mr", "origin", "spawned")
REPO_FIELDS = ("work_root", "branch", "isolation", "base", "candidate", "checked_candidate", "checked_tree", "depends_on")
SCAN_DEPTH = 3
SHA = re.compile(r"[0-9a-f]{7,40}")
SKIPPED_DIRS = {"node_modules", "vendor", ".git"}


def key_pattern(key: str) -> re.Pattern[str]:
    # UPL-1166 must not match UPL-11660 or the derived task UPL-1166-1.
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![0-9]|-[0-9])", re.IGNORECASE)


def git_lines(repository: Path, *arguments: str) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments], text=True, capture_output=True, check=False
    )
    return completed.stdout.splitlines() if completed.returncode == 0 else []


def parse_state(path: Path) -> dict[str, Any]:
    """Fields of the `## Состояние` part: top-level `- key: value` and `### repo: <name>` or `### <name>` blocks.

    A block is one repository only when it has `work_root` and repeats no field; other blocks are prose.
    """
    state: dict[str, Any] = {"repositories": {}}
    blocks: dict[str, dict[str, str]] = {}
    listed: set[str] = set()
    section = None
    block: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            block = None
            continue
        if section != "Состояние":
            continue
        if line.startswith("### "):
            block = line[4:].removeprefix("repo:").strip()
            blocks.setdefault(block, {})
            continue
        match = re.match(r"- ([a-z_]+):\s*(.*)$", line)
        if not match:
            continue
        name, value = match.groups()
        if block is not None and name in REPO_FIELDS:
            if name in blocks[block]:
                listed.add(block)
            blocks[block][name] = value
        elif block is None and name in STATE_FIELDS:
            state[name] = value
    state["repositories"] = {
        name: fields for name, fields in blocks.items() if "work_root" in fields and name not in listed
    }
    return state


def iterations(task_dir: Path) -> list[str]:
    found = [
        item.name
        for item in task_dir.iterdir()
        if item.is_dir() and re.fullmatch(r"(implementation|review)-[0-9]+", item.name)
    ]
    return sorted(found, key=lambda name: int(name.rsplit("-", 1)[1]))


def main_checkouts(roots: list[Path]) -> list[Path]:
    """Main checkouts (`.git` directory) under the roots; linked worktrees come from git itself."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for current, directories, _ in os.walk(root):
            path = Path(current)
            if (path / ".git").is_dir():
                found.append(path)
                directories.clear()
                continue
            if len(path.relative_to(root).parts) >= SCAN_DEPTH:
                directories.clear()
                continue
            directories[:] = sorted(d for d in directories if d not in SKIPPED_DIRS and not d.startswith("."))
    return found


def tree_state(path: Path) -> dict[str, Any]:
    lines = git_lines(path, "status", "--porcelain=v1", "--branch", "--untracked-files=normal")
    if not lines:
        return {"path": str(path), "available": False}
    header, changes = lines[0], lines[1:]
    return {
        "path": str(path),
        "available": True,
        "branch_line": header.removeprefix("## "),
        "detached": header.startswith("## HEAD (no branch)"),
        "clean": not changes,
        "changes": len(changes),
    }


def checked_snapshot_reasons(name: str, work_root: Path, repository: dict[str, str]) -> list[str]:
    """`checked_candidate` may be rewritten hash-only, so its tree, not its SHA, must equal `checked_tree`."""
    candidate = (repository.get("checked_candidate") or "").split(" ", 1)[0]
    if not SHA.fullmatch(candidate):
        return []
    checked_tree = (repository.get("checked_tree") or "").split(" ", 1)[0]
    if not SHA.fullmatch(checked_tree):
        return [f"{name}: checked_candidate {candidate} has no checked_tree"]
    tree = git_lines(work_root, "rev-parse", f"{candidate}^{{tree}}")
    if not tree:
        return [f"{name}: checked_candidate {candidate} does not resolve in {work_root}"]
    if not tree[0].startswith(checked_tree):
        return [f"{name}: tree of checked_candidate {candidate} differs from checked_tree {checked_tree}"]
    return []


def worktrees(repository: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in git_lines(repository, "worktree", "list", "--porcelain"):
        if line.startswith("worktree "):
            entries.append({"path": line[len("worktree "):], "branch": None})
        elif line.startswith("branch ") and entries:
            entries[-1]["branch"] = line[len("branch "):].removeprefix("refs/heads/")
    return entries


def scan_repository(repository: Path, pattern: re.Pattern[str]) -> dict[str, Any] | None:
    refs = git_lines(repository, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes")
    branches = [ref.removeprefix("refs/heads/") for ref in refs if ref.startswith("refs/heads/") and pattern.search(ref)]
    remotes = [ref.removeprefix("refs/remotes/") for ref in refs if ref.startswith("refs/remotes/") and pattern.search(ref)]
    linked = [
        entry
        for entry in worktrees(repository)[1:]
        if pattern.search(Path(entry["path"]).name) or (entry["branch"] and pattern.search(entry["branch"]))
    ]
    if not (branches or remotes or linked):
        return None
    return {
        "repository": str(repository),
        "main": tree_state(repository),
        "branches": branches,
        "remote_branches": remotes,
        "worktrees": [{**tree_state(Path(entry["path"])), "branch": entry["branch"]} for entry in linked],
    }


def task_migrations(repository: str | None, fields: dict[str, str]) -> bool | None:
    """True when the task diff changes `migrations/`; None without a resolvable base...candidate.

    Runs in the main checkout: after completion the task worktree is removed, its branch is not.
    """
    if not repository:
        return None
    base = (fields.get("base") or "").split(" ", 1)[0]
    candidate = (fields.get("candidate") or "").split(" ", 1)[0]
    if not (base and candidate):
        return None
    completed = subprocess.run(
        ["git", "-C", repository, "diff", "--name-only", f"{base}...{candidate}", "--", "migrations"],
        text=True, capture_output=True, check=False,
    )
    return None if completed.returncode != 0 else bool(completed.stdout.strip())


def owning_repository(
    work_root: str | None, name: str, found: list[dict[str, Any]], key: str
) -> dict[str, Any] | None:
    """Main checkout owning the task worktree; the branch alone is ambiguous across services.

    A live worktree is matched by path, a removed one by the `<repo>-<KEY>` sibling convention.
    """
    if work_root:
        work = str(Path(work_root).resolve())
        for item in found:
            main = Path(item["repository"])
            linked = [str(Path(tree["path"]).resolve()) for tree in item["worktrees"]]
            sibling = str((main.parent / f"{main.name}-{key}").resolve())
            if work in linked or work == sibling:
                return item
    return next((item for item in found if Path(item["repository"]).name == name), None)


def brief(state: dict[str, Any], found: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Publish inputs of a completed task: per repository base, candidate, branch, dependencies, migrations."""
    repositories = []
    for name, fields in state["repositories"].items():
        entry = owning_repository(fields.get("work_root"), name, found, key)
        repositories.append(
            {
                "name": name,
                "work_root": fields.get("work_root"),
                "branch": fields.get("branch"),
                "base": fields.get("base"),
                "candidate": fields.get("candidate"),
                "depends_on": fields.get("depends_on"),
                "migrations": task_migrations(entry["repository"] if entry else None, fields),
            }
        )
    return {"repositories": repositories}


def propose(state: dict[str, Any] | None, found: list[dict[str, Any]]) -> tuple[str, list[str]]:
    if state is None:
        if found:
            return "ambiguous", ["branches or worktrees exist without state.md"]
        return "new", []
    status = state.get("status", "")
    if status == "completed":
        return "next-cycle", []
    if status not in CONTINUABLE:
        return "ambiguous", [f"unknown status '{status}'"]
    reasons: list[str] = []
    notes: list[str] = []
    for name, repository in state["repositories"].items():
        work_root = repository.get("work_root")
        tree = tree_state(Path(work_root)) if work_root else {"available": False}
        if not tree["available"]:
            reasons.append(f"{name}: work_root {work_root} is not an available git tree")
        elif tree["detached"]:
            reasons.append(f"{name}: work_root {work_root} is detached")
        else:
            notes.extend(checked_snapshot_reasons(name, Path(work_root), repository))
    return ("ambiguous", reasons + notes) if reasons else ("continue", notes)


def inventory(value: str) -> dict[str, Any]:
    issue = resolve_issue.resolve(value) if value.startswith(("http://", "https://")) else resolve_issue.resolve_key(value)
    key = str(issue["key"])
    os.environ["AGENTS_PROJECT"] = str(issue["project"])
    task_dir = skills_config.data_root() / "tasks" / key
    state_path = task_dir / "state.md"
    state = parse_state(state_path) if state_path.is_file() else None
    pattern = key_pattern(key)
    roots = [Path(root) for root in issue["project_roots"]]
    found = [item for item in (scan_repository(path, pattern) for path in main_checkouts(roots)) if item]
    route, reasons = propose(state, found)
    result = {
        "key": key,
        "url": issue["url"],
        "project": issue["project"],
        "task_dir": str(task_dir),
        "state": state,
        "iterations": iterations(task_dir) if task_dir.is_dir() else [],
        "repositories": found,
        "route": route,
        "reasons": reasons,
    }
    if state is not None and state.get("status") == "completed":
        result["brief"] = brief(state, found, key)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("value", help="Jira issue URL or issue key")
    args = parser.parse_args()
    try:
        output = inventory(args.value.strip())
    except (OSError, resolve_issue.ResolveError, skills_config.ConfigError) as exc:
        print(f"inventory: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
