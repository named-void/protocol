#!/usr/bin/env python3
"""Seal and verify a protocol candidate snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

SCHEMA = "protocol.candidate-manifest/v1"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
IMAGE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
CANONICAL_REF_PATTERN = re.compile(r"^refs/(?:heads|remotes)/[^\s]+$")


class ManifestError(RuntimeError):
    """The manifest or seal input is invalid."""


class StaleSnapshot(ManifestError):
    """The sealed candidate no longer matches live state."""


def fail(message: str, *, stale: bool = False) -> NoReturn:
    error_type = StaleSnapshot if stale else ManifestError
    raise error_type(message)


def git(repository: Path, *arguments: str, stale: bool = False) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        fail(
            f"git {' '.join(arguments)} failed in {repository}: {detail}",
            stale=stale,
        )
    return result.stdout.strip()


def repository_root(value: str) -> Path:
    requested = Path(value).expanduser().resolve()
    root = Path(git(requested, "rev-parse", "--show-toplevel")).resolve()
    if root != requested:
        fail(f"repository must be its Git toplevel: {requested} != {root}")
    return root


def resolve_commit(repository: Path, value: str, *, stale: bool = False) -> str:
    commit = git(
        repository, "rev-parse", "--verify", f"{value}^{{commit}}", stale=stale
    )
    if not COMMIT_PATTERN.fullmatch(commit):
        fail(
            f"Git returned a non-canonical commit id for {value}: {commit}", stale=stale
        )
    return commit


def require_ancestor(
    repository: Path,
    ancestor: str,
    candidate: str,
    *,
    label: str,
    stale: bool = False,
) -> None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "merge-base",
            "--is-ancestor",
            ancestor,
            candidate,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    if result.returncode == 1:
        fail(
            f"{label} {ancestor} is not an ancestor of candidate {candidate}",
            stale=stale,
        )
    detail = result.stderr.strip() or result.stdout.strip()
    fail(f"cannot compare {label} with candidate: {detail}", stale=stale)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def image_id(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def validate_role(role: str) -> None:
    if not ROLE_PATTERN.fullmatch(role):
        fail(f"artifact role must match {ROLE_PATTERN.pattern}: {role}")


def artifact_entry(role: str, value: str) -> dict[str, Any]:
    validate_role(role)
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        fail(f"artifact is not a regular file: {path}")
    stat = path.stat()
    return {
        "path": str(path),
        "read_policy": "read-only",
        "role": role,
        "sha256": sha256(path),
        "size": stat.st_size,
    }


def require_clean_candidate(
    repository: Path,
    refs: dict[str, str],
    *,
    stale: bool,
) -> None:
    candidate = refs["candidate"]
    head = resolve_commit(repository, "HEAD", stale=stale)
    if head != candidate:
        fail(f"HEAD {head} does not match candidate {candidate}", stale=stale)

    status = git(
        repository, "status", "--porcelain", "--untracked-files=all", stale=stale
    )
    if status:
        fail("candidate worktree is not clean", stale=stale)

    canonical_ref = refs["canonical_ref"]
    current_canonical = resolve_commit(repository, canonical_ref, stale=stale)
    if current_canonical != refs["canonical_base"]:
        fail(
            f"canonical ref {canonical_ref} moved: "
            f"expected {refs['canonical_base']}, got {current_canonical}",
            stale=stale,
        )

    for label in ("implementation_start", "canonical_base", "check_base"):
        require_ancestor(
            repository,
            refs[label],
            candidate,
            label=label,
            stale=stale,
        )


def build_manifest(arguments: argparse.Namespace) -> dict[str, Any]:
    repository = repository_root(arguments.repository)
    canonical_ref = arguments.canonical_ref
    if not CANONICAL_REF_PATTERN.fullmatch(canonical_ref):
        fail("canonical ref must be fully qualified under refs/heads or refs/remotes")

    refs = {
        "candidate": resolve_commit(repository, arguments.candidate),
        "canonical_base": resolve_commit(repository, arguments.canonical_base),
        "canonical_ref": canonical_ref,
        "check_base": resolve_commit(repository, arguments.check_base),
        "implementation_start": resolve_commit(
            repository, arguments.implementation_start
        ),
    }
    require_clean_candidate(repository, refs, stale=False)

    artifacts = [artifact_entry(role, path) for role, path in arguments.artifact]
    paths = [artifact["path"] for artifact in artifacts]
    if len(paths) != len(set(paths)):
        fail("artifact paths must be unique after symlink resolution")
    artifacts.sort(key=lambda item: (item["role"], item["path"]))

    payload = {
        "artifacts": artifacts,
        "refs": refs,
        "repository": str(repository),
        "schema": SCHEMA,
    }
    return {**payload, "image_id": image_id(payload)}


def validate_manifest_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("manifest root must be an object")
    required = {"artifacts", "image_id", "refs", "repository", "schema"}
    if set(value) != required:
        fail(f"manifest keys must be exactly: {', '.join(sorted(required))}")
    if value["schema"] != SCHEMA:
        fail(f"unsupported manifest schema: {value['schema']}")
    if not isinstance(value["image_id"], str) or not IMAGE_ID_PATTERN.fullmatch(
        value["image_id"]
    ):
        fail("manifest image_id must be a lowercase SHA-256")
    if (
        not isinstance(value["repository"], str)
        or not Path(value["repository"]).is_absolute()
    ):
        fail("manifest repository must be an absolute path")

    refs = value["refs"]
    required_refs = {
        "candidate",
        "canonical_base",
        "canonical_ref",
        "check_base",
        "implementation_start",
    }
    if not isinstance(refs, dict) or set(refs) != required_refs:
        fail(f"manifest refs must be exactly: {', '.join(sorted(required_refs))}")
    for name in required_refs - {"canonical_ref"}:
        if not isinstance(refs[name], str) or not COMMIT_PATTERN.fullmatch(refs[name]):
            fail(f"manifest refs.{name} must be a canonical Git commit id")
    if not isinstance(
        refs["canonical_ref"], str
    ) or not CANONICAL_REF_PATTERN.fullmatch(refs["canonical_ref"]):
        fail("manifest refs.canonical_ref must be fully qualified")

    artifacts = value["artifacts"]
    if not isinstance(artifacts, list):
        fail("manifest artifacts must be an array")
    seen_paths: set[str] = set()
    for artifact in artifacts:
        required_artifact = {"path", "read_policy", "role", "sha256", "size"}
        if not isinstance(artifact, dict) or set(artifact) != required_artifact:
            fail(
                "every artifact must contain path, read_policy, role, sha256, and size"
            )
        path = artifact["path"]
        if not isinstance(path, str) or not Path(path).is_absolute():
            fail("artifact path must be absolute")
        if path in seen_paths:
            fail(f"duplicate artifact path: {path}")
        seen_paths.add(path)
        if artifact["read_policy"] != "read-only":
            fail(f"unsupported artifact read_policy: {artifact['read_policy']}")
        if not isinstance(artifact["role"], str):
            fail("artifact role must be a string")
        validate_role(artifact["role"])
        if not isinstance(artifact["sha256"], str) or not IMAGE_ID_PATTERN.fullmatch(
            artifact["sha256"]
        ):
            fail(f"artifact sha256 is invalid: {path}")
        if not isinstance(artifact["size"], int) or artifact["size"] < 0:
            fail(f"artifact size is invalid: {path}")

    payload = {key: value[key] for key in required - {"image_id"}}
    expected_id = image_id(payload)
    if value["image_id"] != expected_id:
        fail(f"manifest image_id mismatch: expected {expected_id}")
    return value


def verify_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"cannot read manifest {path}: {error}")
    manifest = validate_manifest_shape(raw)

    repository = Path(manifest["repository"]).resolve()
    if not repository.is_dir():
        fail(f"manifest repository does not exist: {repository}", stale=True)
    if str(repository) != manifest["repository"]:
        fail("manifest repository path no longer resolves identically", stale=True)

    refs = manifest["refs"]
    for name in ("candidate", "canonical_base", "check_base", "implementation_start"):
        resolved = resolve_commit(repository, refs[name], stale=True)
        if resolved != refs[name]:
            fail(f"refs.{name} no longer resolves identically", stale=True)
    require_clean_candidate(repository, refs, stale=True)

    for artifact in manifest["artifacts"]:
        artifact_path = Path(artifact["path"])
        if not artifact_path.is_file():
            fail(f"artifact is missing: {artifact_path}", stale=True)
        if artifact_path.stat().st_size != artifact["size"]:
            fail(f"artifact size changed: {artifact_path}", stale=True)
        actual_hash = sha256(artifact_path)
        if actual_hash != artifact["sha256"]:
            fail(f"artifact sha256 changed: {artifact_path}", stale=True)
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    output = path.expanduser().resolve()
    if not output.parent.is_dir():
        fail(f"manifest parent directory does not exist: {output.parent}")
    if output.exists():
        fail(f"refusing to overwrite sealed manifest: {output}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(manifest, target, ensure_ascii=False, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError:
            fail(f"refusing to overwrite sealed manifest: {output}")
        except OSError as error:
            fail(f"cannot seal manifest {output}: {error}")
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    seal = commands.add_parser("seal", help="create a sealed candidate manifest")
    seal.add_argument("--repository", required=True)
    seal.add_argument("--implementation-start", required=True)
    seal.add_argument("--canonical-ref", required=True)
    seal.add_argument("--canonical-base", required=True)
    seal.add_argument("--check-base", required=True)
    seal.add_argument("--candidate", required=True)
    seal.add_argument(
        "--artifact", action="append", default=[], nargs=2, metavar=("ROLE", "PATH")
    )
    seal.add_argument("--output", required=True)

    verify = commands.add_parser("verify", help="verify a sealed candidate manifest")
    verify.add_argument("manifest")
    return root


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.command == "seal":
            manifest = build_manifest(arguments)
            output = Path(arguments.output)
            write_manifest(output, manifest)
            action = "sealed"
        else:
            output = Path(arguments.manifest).expanduser().resolve()
            manifest = verify_manifest(output)
            action = "verified"
    except StaleSnapshot as error:
        print(f"stale: {error}", file=sys.stderr)
        return 3
    except ManifestError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(
        f"{action} image_id={manifest['image_id']} "
        f"candidate={manifest['refs']['candidate']} path={output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
