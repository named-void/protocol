#!/usr/bin/env python3
"""Parse Git remote URLs in one place for shared skills and scripts."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class GitRemote:
    host: str | None
    repository_path: str


def parse_remote(value: str) -> GitRemote:
    remote = value.strip().rstrip("/")
    if not remote:
        raise ValueError("Git remote is empty")

    parsed = urlparse(remote)
    if parsed.scheme:
        host = parsed.hostname.lower() if parsed.hostname else None
        path = unquote(parsed.path)
        if host:
            path = path.lstrip("/")
    else:
        scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", remote)
        if scp:
            host, path = scp.groups()
            host = host.lower()
        else:
            host, path = None, remote

    repository_path = path.rstrip("/").removesuffix(".git")
    if not repository_path:
        raise ValueError(f"Git remote has no repository path: {value!r}")
    return GitRemote(host=host, repository_path=repository_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("remote")
    parser.add_argument("--field", choices=("host", "path", "json"), default="json")
    args = parser.parse_args()
    try:
        result = parse_remote(args.remote)
    except ValueError as exc:
        parser.error(str(exc))
    if args.field == "host":
        if result.host is None:
            parser.error("Git remote has no host")
        print(result.host)
    elif args.field == "path":
        print(result.repository_path)
    else:
        print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
