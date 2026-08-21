#!/usr/bin/env python3
"""Resolve a GitLab MR URL to a configured local checkout."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

SOURCE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE_ROOT / "lib"))

import skills_config
from git_remote import parse_remote


class ResolveError(ValueError):
    pass


def parse_url(value: str) -> tuple[str, str, int]:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResolveError("expected an absolute HTTP(S) GitLab MR URL")

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ResolveError("project path must not contain dot segments")
    marker = ["-", "merge_requests"]
    positions = [i for i in range(len(parts) - 1) if parts[i : i + 2] == marker]
    if len(positions) != 1:
        raise ResolveError("expected /<project>/-/merge_requests/<iid>")

    pos = positions[0]
    if pos == 0 or len(parts) != pos + 3:
        raise ResolveError("expected /<project>/-/merge_requests/<iid>")
    try:
        iid = int(parts[pos + 2])
    except ValueError as exc:
        raise ResolveError("merge request IID must be an integer") from exc
    if iid < 1:
        raise ResolveError("merge request IID must be positive")
    return parsed.hostname.lower(), "/".join(parts[:pos]), iid


def origin_matches(local_path: Path, host: str, project_path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(local_path), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

    try:
        remote = parse_remote(result.stdout)
    except ValueError:
        return False
    return remote.host == host and remote.repository_path == project_path


def resolve(url: str) -> dict[str, object]:
    host, project_path, iid = parse_url(url)
    try:
        project = skills_config.resolve_vcs_project(host, project_path)
        local_path = Path(project["local_path"]).resolve()
        rules_paths = list(
            dict.fromkeys(
                Path(path).resolve()
                for path in [
                    *skills_config.mreviewer_rules(str(project["name"])),
                    *skills_config.profiles_for_path(local_path),
                ]
            )
        )
    except skills_config.ConfigError as exc:
        raise ResolveError(str(exc)) from exc
    checkout_matches = local_path.is_dir() and origin_matches(
        local_path, host, project_path
    )

    output: dict[str, object] = {
        "url": url,
        "host": host,
        "project_path": project_path,
        "iid": iid,
        "profile": project["name"],
        "local_path": str(local_path),
        "local_exists": local_path.is_dir(),
        "checkout_matches": checkout_matches,
        "rules_paths": [str(path) for path in rules_paths],
        "rules_exist": all(path.is_file() for path in rules_paths),
    }
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args()
    try:
        output = resolve(args.url)
    except (OSError, ResolveError) as exc:
        print(f"resolve-mr: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if not output["checkout_matches"] or not output["rules_exist"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
