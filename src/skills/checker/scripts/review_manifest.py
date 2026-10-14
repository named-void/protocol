#!/usr/bin/env python3
"""Seal and verify the immutable input manifest of one checker mode.

seal <draft> <manifest>: resolve snapshots and evidence digests, check them, write the manifest atomically.
  Rule files are kept by path only: rules evolve while a pass runs, and any of their versions is valid.
verify <manifest> [--digest D] [--integrity]: recheck a sealed manifest against the current state;
  --integrity skips Git snapshots, which Git finalization legitimately rewrites.

Exit codes: 0 ok, 2 blocked (missing or malformed manifest), 3 stale (inputs differ from the snapshot).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

MODES = ("requirements", "technical", "requirements-fix")
DIGEST_EXCLUDED = ("input_digest", "progress", "result")


class Blocked(ValueError):
    pass


def input_digest(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key not in DIGEST_EXCLUDED}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_sha256(path: Path) -> str:
    """SHA-256 of a file, or of a directory as sorted `<relpath>\\0<file sha256>\\n` lines."""
    if path.is_file():
        return file_sha256(path)
    digest = hashlib.sha256()
    for item in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(f"{item.relative_to(path).as_posix()}\0{file_sha256(item)}\n".encode())
    return digest.hexdigest()


def git(work_root: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", work_root, *arguments], text=True, capture_output=True, check=False
    )


def rev(work_root: str, expression: str) -> str | None:
    completed = git(work_root, "rev-parse", "--verify", "--quiet", expression)
    return completed.stdout.strip() if completed.returncode == 0 else None


def _absolute(value: Any, field: str) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise Blocked(f"{field}: absolute path required")
    return value


def _files(manifest: dict[str, Any], key: str, owner: str, *, sealed: bool, hashed: bool) -> list[dict[str, Any]]:
    entries = manifest.get(key, [])
    if not isinstance(entries, list):
        raise Blocked(f"{owner}{key}: list required")
    for index, entry in enumerate(entries):
        field = f"{owner}{key}[{index}]"
        if not isinstance(entry, dict):
            raise Blocked(f"{field}: object required")
        _absolute(entry.get("path"), f"{field}.path")
        if sealed and hashed and not isinstance(entry.get("sha256"), str):
            raise Blocked(f"{field}.sha256 missing")
    return entries


def validate(manifest: Any, *, sealed: bool) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise Blocked("manifest: JSON object required")
    mode = manifest.get("mode")
    if mode not in MODES:
        raise Blocked(f"mode: one of {', '.join(MODES)} required")
    _absolute(manifest.get("progress"), "progress")
    _absolute(manifest.get("result"), "result")
    if mode == "requirements-fix":
        targets = manifest.get("targets")
        if not isinstance(targets, list) or not targets:
            raise Blocked("targets: non-empty list required for requirements-fix")
    repositories = manifest.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise Blocked("repositories: non-empty list required")
    names: set[str] = set()
    for index, repository in enumerate(repositories):
        field = f"repositories[{index}]"
        if not isinstance(repository, dict):
            raise Blocked(f"{field}: object required")
        name = repository.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise Blocked(f"{field}.name: unique non-empty string required")
        names.add(name)
        _absolute(repository.get("work_root"), f"{field}.work_root")
        required = ("base", "candidate") + (("base_tree", "candidate_tree") if sealed else ())
        for key in required:
            if not isinstance(repository.get(key), str) or not repository[key]:
                raise Blocked(f"{field}.{key}: string required")
        if "checked_tree" in repository and mode != "requirements-fix":
            raise Blocked(f"{field}.checked_tree: allowed only in requirements-fix")
        _files(repository, "evidence", f"{field}.", sealed=sealed, hashed=True)
    _files(manifest, "rule_files", "", sealed=sealed, hashed=False)
    _files(manifest, "excluded_rules", "", sealed=sealed, hashed=False)
    _files(manifest, "sources", "", sealed=sealed, hashed=True)
    if sealed and not isinstance(manifest.get("input_digest"), str):
        raise Blocked("input_digest missing")
    return manifest


def check_snapshot(repository: dict[str, Any], *, pin: bool) -> list[str]:
    """Check one repository snapshot; with `pin`, resolve SHAs and trees into the entry."""
    name, work_root = repository["name"], repository["work_root"]
    if not Path(work_root, ".git").exists():
        return [f"{name}: {work_root} is not a git work tree"]
    reasons: list[str] = []
    resolved: dict[str, str] = {}
    for key in ("base", "candidate"):
        sha = rev(work_root, f"{repository[key]}^{{commit}}")
        if sha is None:
            reasons.append(f"{name}: {key} {repository[key]} does not resolve to a commit")
            continue
        tree = rev(work_root, f"{sha}^{{tree}}")
        if pin:
            repository[key], repository[f"{key}_tree"] = sha, tree
        else:
            if sha != repository[key]:
                reasons.append(f"{name}: {key} resolves to {sha}, manifest has {repository[key]}")
            if tree != repository[f"{key}_tree"]:
                reasons.append(f"{name}: {key}_tree is {tree}, manifest has {repository[f'{key}_tree']}")
        resolved[key] = sha
    if reasons:
        return reasons
    head = rev(work_root, "HEAD")
    if head != resolved["candidate"]:
        reasons.append(f"{name}: HEAD {head} != candidate {resolved['candidate']}")
    if git(work_root, "status", "--porcelain", "--untracked-files=normal").stdout.strip():
        reasons.append(f"{name}: worktree is dirty")
    checked_tree = repository.get("checked_tree")
    if checked_tree is not None:
        base_tree = rev(work_root, f"{resolved['base']}^{{tree}}")
        if base_tree != checked_tree:
            reasons.append(f"{name}: base tree {base_tree} != checked_tree {checked_tree}")
    elif git(work_root, "merge-base", "--is-ancestor", resolved["base"], resolved["candidate"]).returncode:
        reasons.append(f"{name}: base {resolved['base']} is not an ancestor of candidate")
    return reasons


def check_files(entries: list[dict[str, Any]], owner: str, *, pin: bool) -> list[str]:
    reasons: list[str] = []
    for entry in entries:
        path = Path(entry["path"])
        if not path.exists():
            reasons.append(f"{owner}: {path} is missing")
            continue
        actual = path_sha256(path)
        expected = entry.get("sha256")
        if expected is not None and expected != actual:
            reasons.append(f"{owner}: {path} sha256 {actual} != {expected}")
        elif pin:
            entry["sha256"] = actual
    return reasons


def check_inputs(manifest: dict[str, Any], *, pin: bool, snapshots: bool = True) -> list[str]:
    reasons: list[str] = []
    for repository in manifest["repositories"]:
        if snapshots:
            reasons += check_snapshot(repository, pin=pin)
        reasons += check_files(repository.get("evidence", []), f"{repository['name']} evidence", pin=pin)
    reasons += check_files(manifest.get("sources", []), "sources", pin=pin)
    return reasons


def load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Blocked(f"{path}: {exc}") from exc


def write_exclusive(target: Path, manifest: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise Blocked(f"{target} already exists: a sealed manifest is immutable") from exc
    finally:
        temporary.unlink(missing_ok=True)


def pin_rule_files(manifest: dict[str, Any]) -> None:
    """Keep rule paths without content hashes; task files are hashed `sources`, not rules."""
    task_dir = Path(manifest["result"]).resolve().parents[2]
    pinned: list[dict[str, str]] = []
    for entry in manifest.get("rule_files", []):
        path = Path(entry["path"]).resolve()
        if not path.exists():
            raise Blocked(f"rule_files: {entry['path']} is missing")
        if path.is_relative_to(task_dir):
            raise Blocked(f"rule_files: {entry['path']} is a task file; pass inputs/ snapshots in sources")
        pinned.append({"path": entry["path"]})
    manifest["rule_files"] = pinned
    for entry in manifest.get("sources", []):
        if Path(entry["path"]).resolve() == task_dir / "state.md":
            raise Blocked("sources: state.md changes during the pass; pass task data in the mode view")


def seal(draft_path: Path, target: Path) -> tuple[int, dict[str, Any]]:
    manifest = validate(load(draft_path), sealed=False)
    manifest.pop("input_digest", None)
    pin_rule_files(manifest)
    reasons = check_inputs(manifest, pin=True)
    if reasons:
        return 3, {"status": "stale", "reasons": reasons}
    manifest["input_digest"] = input_digest(manifest)
    write_exclusive(target, manifest)
    return 0, {"status": "sealed", "manifest": str(target), "input_digest": manifest["input_digest"]}


def verify(manifest_path: Path, expected_digest: str | None, *, snapshots: bool) -> tuple[int, dict[str, Any]]:
    manifest = validate(load(manifest_path), sealed=True)
    actual = input_digest(manifest)
    reasons: list[str] = []
    if actual != manifest["input_digest"]:
        reasons.append(f"input_digest {actual} != stored {manifest['input_digest']}")
    if expected_digest is not None and expected_digest != manifest["input_digest"]:
        reasons.append(f"stored input_digest {manifest['input_digest']} != expected {expected_digest}")
    reasons += check_inputs(manifest, pin=False, snapshots=snapshots)
    status = "stale" if reasons else "ok"
    return (3 if reasons else 0), {"status": status, "reasons": reasons, "input_digest": actual}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    seal_parser = commands.add_parser("seal", help="seal a draft into an immutable manifest")
    seal_parser.add_argument("draft", type=Path)
    seal_parser.add_argument("manifest", type=Path)
    verify_parser = commands.add_parser("verify", help="verify a sealed manifest")
    verify_parser.add_argument("manifest", type=Path)
    verify_parser.add_argument("--digest", help="input_digest passed by the caller")
    verify_parser.add_argument("--integrity", action="store_true", help="skip Git snapshot checks")
    args = parser.parse_args()
    try:
        if args.command == "seal":
            code, output = seal(args.draft, args.manifest.absolute())
        else:
            code, output = verify(args.manifest, args.digest, snapshots=not args.integrity)
    except Blocked as exc:
        code, output = 2, {"status": "blocked", "reasons": [str(exc)]}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
