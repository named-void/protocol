#!/usr/bin/env python3
"""Resolve UPL service role users and create ephemeral Dev cookie sessions."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROLE_PATTERN = re.compile(r"^[A-Za-z0-9_:-]+$")
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


def resolve_user_ids_via_api(
    roles: set[str],
    *,
    base_url: str,
    timeout: float = 30.0,
) -> tuple[dict[str, str], set[str]]:
    """IDs активных пользователей ролей через LIST /api/v1/users: один dev-login
    bootstrap-пользователя, БД не используется. Роль без активных пользователей
    попадает в missing; из страницы берётся первый по id (детерминированный выбор)."""
    bootstrap_id = _bootstrap_user_id()
    try:
        session = DevSession("super_admin", bootstrap_id, base_url=base_url, timeout=timeout)
    except AuthError as error:
        raise AuthError(
            f"{error}; bootstrap user {bootstrap_id} is gone from Develop (autotest cleanups remove such users) — "
            "update the bootstrap: set UPL_DEV_BOOTSTRAP_USER_ID or replace DEFAULT_BOOTSTRAP_USER_ID in test_protocol_auth.py"
        ) from error
    result: dict[str, str] = {}
    missing: set[str] = set()
    for role in sorted(roles):
        if not ROLE_PATTERN.fullmatch(role):
            raise AuthError(f"Invalid role code: {role}")
        ids: list[str] = []
        page = 1
        while True:
            try:
                response = session.request(
                    method="GET",
                    url=_join_url(
                        base_url,
                        f"/api/v1/users?role_codes={role}&is_deleted=false&page={page}&page_size=100",
                    ),
                    headers={"Accept": "application/json"},
                )
            except (HTTPError, URLError) as error:
                raise AuthError(
                    f"LIST /api/v1/users failed for role {role}: {error}"
                ) from error
            if response.status != 200:
                raise AuthError(f"LIST /api/v1/users failed for role {role}: HTTP {response.status}")
            data = (response.json or {}).get("data") or []
            ids.extend(str(item["id"]) for item in data if item.get("id"))
            if len(data) < 100:
                break
            page += 1
        if not ids:
            missing.add(role)
            continue
        result[role] = sorted(ids)[0]
    return result, missing


# Bootstrap для API-резолва ролей: id активного супер-админа на Develop.
# Не секрет: без dev-login на стенде id ничего не даёт. Override — переменная
# окружения UPL_DEV_BOOTSTRAP_USER_ID (пользователь может быть удалён autotest'ом).
DEFAULT_BOOTSTRAP_USER_ID = "00e838f1-04a4-40fe-95ec-92a05d0b4922"


def _bootstrap_user_id() -> str:
    """ID супер-админа для первого dev-login: окружение или дефолт выше."""
    return os.environ.get("UPL_DEV_BOOTSTRAP_USER_ID") or DEFAULT_BOOTSTRAP_USER_ID


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
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        roles = set(args.roles)
        user_ids, missing_roles = resolve_user_ids_via_api(
            roles, base_url=args.base_url, timeout=args.timeout
        )
        create_sessions(user_ids, base_url=args.base_url, timeout=args.timeout)
    except AuthError as error:
        print(f"test_protocol_auth: {error}", file=sys.stderr)
        return 2
    for role in sorted(missing_roles):
        print(f"skipped: {role} (no active user)")
    for role in sorted(user_ids):
        print(f"authenticated: {role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
