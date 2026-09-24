#!/usr/bin/env python3
"""Резолв цели manifest-сценария test-protocol через API Develop (без БД).

Выбирает активного пользователя целевой роли по привязкам к партнёрам:
--partner-kind задаёт тип привязки (vendors|publishers), --in-contour
ограничивает кандидатов партнёрами из контура сессионной роли, по умолчанию
требуется единственная активная привязка (--any-binding отменяет). Найденная
цель печатается одной строкой JSON {"user_id", "partner_id"}; диагностика —
в stderr. Цель резолвится непосредственно перед прогоном: состав активных
админов на Develop дрейфует.

Запуск: python3 resolve_targets.py --target-role advertiser_admin --partner-kind vendors
        python3 resolve_targets.py --target-role advertiser_admin --partner-kind vendors --in-contour advertiser_account_manager
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

from test_protocol_auth import (
    AuthError,
    DevSession,
    _join_url,
    resolve_user_ids_via_api,
)

PAGE_SIZE = 100
PARTNER_KINDS = ("vendors", "publishers")


@dataclass(frozen=True)
class Target:
    user_id: str
    partner_id: str


def list_users(session: DevSession, base_url: str, role: str) -> list[dict]:
    """Активные пользователи роли, все страницы LIST /api/v1/users."""
    users: list[dict] = []
    page = 1
    while True:
        response = session.request(
            method="GET",
            url=_join_url(
                base_url,
                f"/api/v1/users?role_codes={role}&is_deleted=false&page={page}&page_size={PAGE_SIZE}",
            ),
            headers={"Accept": "application/json"},
        )
        if response.status != 200:
            raise AuthError(f"LIST /api/v1/users failed for role {role}: HTTP {response.status}")
        data = (response.json or {}).get("data") or []
        users.extend(data)
        if len(data) < PAGE_SIZE:
            return users
        page += 1


def user_partners(session: DevSession, base_url: str, user_id: str, partner_kind: str) -> list[str]:
    """ID партнёров пользователя заданного типа из GET /api/v1/users/{id}?include=..."""
    response = session.request(
        method="GET",
        url=_join_url(base_url, f"/api/v1/users/{user_id}?include={partner_kind}"),
        headers={"Accept": "application/json"},
    )
    if response.status != 200:
        raise AuthError(f"GET /api/v1/users/{user_id} failed: HTTP {response.status}")
    links = (response.json or {}).get(partner_kind) or []
    return [link["id"] for link in links if link.get("id")]


def pick_target(
    session: DevSession,
    base_url: str,
    *,
    target_role: str,
    partner_kind: str,
    contour: set[str] | None,
    require_single_binding: bool,
    is_contact: bool,
    exclude: set[str],
) -> Target | None:
    """Первый кандидат, проходящий по фильтрам; None — кандидата нет."""
    for user in list_users(session, base_url, target_role):
        user_id = user.get("id")
        if not user_id or user_id in exclude:
            continue
        if is_contact and not user.get("is_contact"):
            continue
        partners = user_partners(session, base_url, user_id, partner_kind)
        if not partners:
            continue
        if require_single_binding and len(partners) != 1:
            continue
        if contour is not None and not any(partner in contour for partner in partners):
            continue
        return Target(user_id=user_id, partner_id=partners[0])
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-role", required=True, help="роль искомой цели")
    parser.add_argument(
        "--partner-kind", required=True, choices=PARTNER_KINDS, help="тип партнёрской привязки"
    )
    parser.add_argument(
        "--in-contour",
        help="разрешить только партнёров из контура сессионного пользователя этой роли",
    )
    parser.add_argument(
        "--any-binding",
        action="store_true",
        help="не требовать единственную активную привязку",
    )
    parser.add_argument(
        "--is-contact", action="store_true", help="только пользователи с is_contact=true"
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="USER_ID", help="исключить id (повторяемо)"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("UPL_DEV_BASE_URL", "https://develop.getblogger.ru"),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    roles = {"super_admin", args.target_role}
    if args.in_contour:
        roles.add(args.in_contour)
    try:
        user_ids, missing = resolve_user_ids_via_api(
            roles, base_url=args.base_url, timeout=args.timeout
        )
        for role in sorted(missing):
            print(f"no active user for role {role}", file=sys.stderr)
        if "super_admin" in missing:
            return 2
        if args.in_contour and args.in_contour in missing:
            return 1
        admin = DevSession("super_admin", user_ids["super_admin"], base_url=args.base_url, timeout=args.timeout)
        contour: set[str] | None = None
        if args.in_contour:
            contour = set(user_partners(admin, args.base_url, user_ids[args.in_contour], args.partner_kind))
            if not contour:
                print(f"contour of {args.in_contour} has no {args.partner_kind}", file=sys.stderr)
                return 1
            print(f"contour of {args.in_contour}: {len(contour)} partner(s)", file=sys.stderr)
        target = pick_target(
            admin,
            args.base_url,
            target_role=args.target_role,
            partner_kind=args.partner_kind,
            contour=contour,
            require_single_binding=not args.any_binding,
            is_contact=args.is_contact,
            exclude=set(args.exclude),
        )
    except AuthError as error:
        print(f"resolve_targets: {error}", file=sys.stderr)
        return 2
    if target is None:
        print("no candidate matches the filters", file=sys.stderr)
        return 1
    print(json.dumps({"user_id": target.user_id, "partner_id": target.partner_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
