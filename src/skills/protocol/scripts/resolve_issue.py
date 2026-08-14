#!/usr/bin/env python3
"""Resolve a Jira issue URL to a configured project carrier."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse


SOURCE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE_ROOT / "lib"))

import skills_config  # noqa: E402


class ResolveError(ValueError):
    pass


def parse_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResolveError("expected an absolute HTTP(S) Jira issue URL")

    path_match = re.fullmatch(r"/browse/([^/]+)", unquote(parsed.path))
    if path_match is None:
        raise ResolveError("expected /browse/<KEY>")
    key = path_match.group(1)
    if key in {".", ".."}:
        raise ResolveError("issue key must not be a dot segment")
    return parsed.hostname.lower(), key


def resolve(url: str) -> dict[str, object]:
    host, raw_key = parse_url(url)
    try:
        project_name, channel = skills_config.identify_project(key=raw_key)
        if project_name is None or channel != "issue-key":
            raise ResolveError(f"no project mapping for issue key {raw_key}")
        carrier = skills_config.load_carrier_config(project_name)
        issue_tracker = carrier.get("issue_tracker", {})
        if not isinstance(issue_tracker, dict):
            raise ResolveError(f"project '{project_name}' has no issue tracker map")

        hosts = issue_tracker.get("hosts", [])
        if isinstance(hosts, str) or not isinstance(hosts, list) or not hosts:
            raise ResolveError(f"project '{project_name}' has no Jira URL hosts")
        allowed_hosts = {str(item).strip().lower() for item in hosts}
        if host not in allowed_hosts:
            raise ResolveError(f"no project mapping for Jira host {host}")

        pattern = str(
            issue_tracker.get("key_pattern", r"^[A-Za-z][A-Za-z0-9]*-[0-9]+$")
        )
        if re.fullmatch(pattern, raw_key) is None:
            raise ResolveError(f"invalid issue key {raw_key}")
        key = raw_key.upper()
        roots = skills_config.project_roots(project_name)
    except (re.error, skills_config.ConfigError) as exc:
        raise ResolveError(str(exc)) from exc

    return {
        "url": url,
        "host": host,
        "key": key,
        "project": project_name,
        "project_roots": [str(path.resolve()) for path in roots],
        "roots_exist": bool(roots) and all(path.is_dir() for path in roots),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args()
    try:
        output = resolve(args.url)
    except (OSError, ResolveError) as exc:
        print(f"resolve-issue: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if output["roots_exist"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
