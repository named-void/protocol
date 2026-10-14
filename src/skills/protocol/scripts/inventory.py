#!/usr/bin/env python3
"""Read-only local inventory of one task before routing.

Takes a Jira URL or issue key, finds `<DATA_ROOT>/tasks/<KEY>/state.md`, task worktrees,
local and remote-tracking branches under the project roots, and proposes a route:
`continue`, `next-cycle`, `ambiguous` or `new`.

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
REPO_FIELDS = ("work_root", "branch", "isolation", "base", "candidate", "checked_candidate", "checked_tree")
SCAN_DEPTH = 3
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
    """Fields of the `## Состояние` part: top-level `- key: value` and `### repo: <name>` blocks."""
    state: dict[str, Any] = {"repositories": {}}
    section = None
    repository: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            repository = None
            continue
        if section != "Состояние":
            continue
        if line.startswith("### repo:"):
            repository = state["repositories"].setdefault(line.split(":", 1)[1].strip(), {})
            continue
        match = re.match(r"- ([a-z_]+):\s*(.*)$", line)
        if not match:
            continue
        name, value = match.groups()
        if repository is not None and name in REPO_FIELDS:
            repository[name] = value
        elif repository is None and name in STATE_FIELDS:
            state[name] = value
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
    for name, repository in state["repositories"].items():
        work_root = repository.get("work_root")
        tree = tree_state(Path(work_root)) if work_root else {"available": False}
        if not tree["available"]:
            reasons.append(f"{name}: work_root {work_root} is not an available git tree")
        elif tree["detached"]:
            reasons.append(f"{name}: work_root {work_root} is detached")
    return ("ambiguous", reasons) if reasons else ("continue", [])


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
    return {
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
