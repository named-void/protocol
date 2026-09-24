#!/usr/bin/env python3
"""Resolve UPL service role users and create ephemeral Dev cookie sessions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROLE_PATTERN = re.compile(r"^[A-Za-z0-9_:-]+$")
# Роль подставляется литералом после ROLE_PATTERN.fullmatch (паттерн исключает
# кавычки): psql-переменные (:'role') не подставляются, когда psql исполняется
# docker-exec shim'ом внутри контейнера db (указание 09-22).
ROLE_USER_QUERY = """
SELECT id::text
FROM users
WHERE role_code = '{role}'
  AND deleted_at IS NULL
ORDER BY id;
""".strip()
XSRF_COOKIE = "XSRF-TOKEN"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthError(RuntimeError):
    """Raised when role resolution or Dev authentication cannot complete."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


@dataclass
class DevSession:
    role: str
    user_id: str
    base_url: str
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self._cookies = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookies))
        self._login()

    def _login(self) -> None:
        payload = json.dumps(
            {"user_id": self.user_id, "auth_type": "cookie"}
        ).encode("utf-8")
        response = self._request(
            method="POST",
            url=_join_url(self.base_url, "/auth/dev/login"),
            body=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            add_xsrf=False,
        )
        if response.status not in {200, 204}:
            raise AuthError(
                f"Dev login failed for role {self.role}: HTTP {response.status}"
            )
        if not self._cookie(XSRF_COOKIE):
            raise AuthError(f"Dev login returned no {XSRF_COOKIE} cookie for role {self.role}")

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> HTTPResponse:
        return self._request(
            method=method,
            url=url,
            body=body,
            headers=headers or {},
            add_xsrf=method.upper() not in SAFE_METHODS,
        )

    def _request(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        add_xsrf: bool,
    ) -> HTTPResponse:
        request_headers = dict(headers)
        if add_xsrf and XSRF_COOKIE not in request_headers:
            xsrf = self._cookie(XSRF_COOKIE)
            if xsrf:
                request_headers[XSRF_COOKIE] = xsrf
        request = Request(
            url,
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return HTTPResponse(
                    status=response.status,
                    body=response.read(),
                    headers={key: value for key, value in response.headers.items()},
                )
        except HTTPError as error:
            return HTTPResponse(
                status=error.code,
                body=error.read(),
                headers={key: value for key, value in error.headers.items()},
            )
        except (OSError, URLError) as error:
            raise AuthError(f"HTTP request failed for role {self.role}: {error.reason}") from error

    def _cookie(self, name: str) -> str | None:
        for cookie in self._cookies:
            if cookie.name == name:
                return cookie.value
        return None


def resolve_user_ids(
    roles: set[str],
    *,
    db_url_env: str = "UPL_DEV_DATABASE_URL",
    timeout: float = 30.0,
) -> tuple[dict[str, str], set[str]]:
    """Resolve one active user per role; return resolved users and roles with no active user."""

    if not roles:
        raise AuthError("No roles supplied")
    for role in roles:
        if not ROLE_PATTERN.fullmatch(role):
            raise AuthError(f"Invalid role code: {role}")

    psql = shutil.which("psql")
    if psql is None:
        raise AuthError("psql is required to resolve role users from the Dev DB")
    db_url = os.environ.get(db_url_env)
    if not db_url and not _has_pg_environment():
        db_url = _env_file_value(DEFAULT_DEV_ENV_FILE, db_url_env)
        if db_url:
            os.environ[db_url_env] = db_url
    if not db_url and not _has_pg_environment():
        raise AuthError(
            f"Set {db_url_env} or PostgreSQL PG* connection variables before testing"
        )

    environment = _postgres_environment(db_url)
    result: dict[str, str] = {}
    missing: set[str] = set()
    for role in sorted(roles):
        try:
            completed = subprocess.run(
                [
                    psql,
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    "--quiet",
                    "--command",
                    ROLE_USER_QUERY.format(role=role),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise AuthError(f"Timed out resolving role {role} from the Dev DB") from error
        if completed.returncode != 0:
            raise AuthError(f"Could not query the Dev DB for role {role}")
        user_ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not user_ids:
            missing.add(role)
            continue
        # Любой активный пользователь роли годится для сессии; берём первого по
        # id — на общей Dev-БД активных пользователей роли обычно много.
        result[role] = user_ids[0]
    return result, missing


def _has_pg_environment() -> bool:
    return bool(os.environ.get("PGHOST") or os.environ.get("PGSERVICE")) and bool(
        os.environ.get("PGDATABASE")
    )


# Дефолтный файл окружения проекта UPL (рядом с каталогом scripts). Хранит
# UPL_DEV_DATABASE_URL для test-protocol; значение никогда не выводится.
DEFAULT_DEV_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _env_file_value(path: Path, key: str) -> str | None:
    """Прочитать KEY=VALUE из файла окружения, не раскрывая значение."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def _postgres_environment(db_url: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    if not db_url:
        return environment
    parsed = urlsplit(db_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise AuthError("UPL_DEV_DATABASE_URL must be a PostgreSQL URL")
    environment["PGHOST"] = parsed.hostname
    environment["PGPORT"] = str(parsed.port or 5432)
    if parsed.username:
        environment["PGUSER"] = parsed.username
    if parsed.password:
        environment["PGPASSWORD"] = parsed.password
    if parsed.path.lstrip("/"):
        environment["PGDATABASE"] = parsed.path.lstrip("/")
    query = dict(part.split("=", 1) for part in parsed.query.split("&") if "=" in part)
    if "sslmode" in query:
        environment["PGSSLMODE"] = query["sslmode"]
    environment.pop("UPL_DEV_DATABASE_URL", None)
    return environment


def _join_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def create_sessions(
    user_ids: dict[str, str],
    *,
    base_url: str,
    timeout: float = 30.0,
) -> dict[str, DevSession]:
    """Authenticate each resolved role in an isolated in-memory cookie jar."""

    return {
        role: DevSession(role, user_id, base_url, timeout)
        for role, user_id in sorted(user_ids.items())
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roles", nargs="+", help="role codes to resolve and authenticate")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("UPL_DEV_BASE_URL", "https://develop.getblogger.ru"),
    )
    parser.add_argument("--db-url-env", default="UPL_DEV_DATABASE_URL")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        roles = set(args.roles)
        user_ids, missing_roles = resolve_user_ids(
            roles, db_url_env=args.db_url_env, timeout=args.timeout
        )
        create_sessions(user_ids, base_url=args.base_url, timeout=args.timeout)
    except AuthError as error:
        print(f"test_protocol_auth: {error}", file=sys.stderr)
        return 2
    for role in sorted(missing_roles):
        print(f"skipped: {role} (no active Dev DB user)")
    for role in sorted(user_ids):
        print(f"authenticated: {role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
