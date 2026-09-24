from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "projects" / "upl" / "scripts"


def load_helper():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import resolve_targets

    return resolve_targets


def fake_api(auth, users_by_role, cards):
    """Подмена DevSession: LIST отдаёт users_by_role, card — привязки из cards."""

    class FakeApiSession:
        def __init__(self, role, user_id, *, base_url, timeout=30.0):
            self.role, self.user_id = role, user_id

        def request(self, method, url, *, body=None, headers=None):
            if "role_codes=" in url:
                role = re.search(r"role_codes=([A-Za-z0-9_:-]+)", url).group(1)
                payload = {"data": users_by_role.get(role, [])}
            else:
                include = re.search(r"include=(vendors|publishers)", url).group(1)
                user_id = re.search(r"/api/v1/users/([^?]+)", url).group(1)
                payload = {include: cards.get(user_id, {}).get(include, [])}
            return auth.HTTPResponse(status=200, body=json.dumps(payload).encode("utf-8"), headers={})

    return FakeApiSession


def user(user_id: str, *, is_contact: bool = True) -> dict:
    return {"id": user_id, "is_contact": is_contact}


class ResolveTargetsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = load_helper()
        import test_protocol_auth

        self.auth = test_protocol_auth

    def run_helper(self, argv: list[str], users_by_role: dict, cards: dict) -> tuple[int, str]:
        buffer = io.StringIO()

        def fake_resolver(roles, *, base_url, timeout):
            return {role: f"id-{role}" for role in roles}, set()

        with mock.patch.object(self.helper, "resolve_user_ids_via_api", fake_resolver), \
                mock.patch.object(self.helper, "DevSession", fake_api(self.auth, users_by_role, cards)), \
                contextlib.redirect_stdout(buffer):
            code = self.helper.main(argv)
        return code, buffer.getvalue()

    def test_picks_in_contour_candidate(self) -> None:
        users = {"advertiser_admin": [user("u-out"), user("u-in")]}
        cards = {
            "id-advertiser_account_manager": {"vendors": [{"id": "p1"}]},
            "u-out": {"vendors": [{"id": "p2"}]},
            "u-in": {"vendors": [{"id": "p1"}]},
        }
        argv = ["--target-role", "advertiser_admin", "--partner-kind", "vendors",
                "--in-contour", "advertiser_account_manager"]

        code, output = self.run_helper(argv, users, cards)

        self.assertEqual(0, code, output)
        self.assertEqual({"user_id": "u-in", "partner_id": "p1"}, json.loads(output))

    def test_single_binding_is_required_by_default(self) -> None:
        users = {"publisher_admin": [user("u-multi"), user("u-single")]}
        cards = {
            "u-multi": {"publishers": [{"id": "p1"}, {"id": "p2"}]},
            "u-single": {"publishers": [{"id": "p1"}]},
        }
        base = ["--target-role", "publisher_admin", "--partner-kind", "publishers"]

        code, output = self.run_helper(base, users, cards)

        self.assertEqual(0, code, output)
        self.assertEqual({"user_id": "u-single", "partner_id": "p1"}, json.loads(output))

    def test_any_binding_allows_multi_link_candidate(self) -> None:
        users = {"publisher_admin": [user("u-multi")]}
        cards = {"u-multi": {"publishers": [{"id": "p9"}, {"id": "p2"}]}}
        argv = ["--target-role", "publisher_admin", "--partner-kind", "publishers", "--any-binding"]

        code, output = self.run_helper(argv, users, cards)

        self.assertEqual(0, code, output)
        self.assertEqual({"user_id": "u-multi", "partner_id": "p9"}, json.loads(output))

    def test_is_contact_flag_skips_non_contact(self) -> None:
        users = {"advertiser_admin": [user("u-plain", is_contact=False)]}
        cards = {"u-plain": {"vendors": [{"id": "p1"}]}}
        base = ["--target-role", "advertiser_admin", "--partner-kind", "vendors"]

        code, output = self.run_helper(base, users, cards)
        self.assertEqual(0, code, output)
        self.assertEqual({"user_id": "u-plain", "partner_id": "p1"}, json.loads(output))

        code, _ = self.run_helper([*base, "--is-contact"], users, cards)
        self.assertEqual(1, code)

    def test_exclude_skips_candidate(self) -> None:
        users = {"advertiser_admin": [user("u-1"), user("u-2")]}
        cards = {"u-1": {"vendors": [{"id": "p1"}]}, "u-2": {"vendors": [{"id": "p2"}]}}
        argv = ["--target-role", "advertiser_admin", "--partner-kind", "vendors", "--exclude", "u-1"]

        code, output = self.run_helper(argv, users, cards)

        self.assertEqual(0, code, output)
        self.assertEqual({"user_id": "u-2", "partner_id": "p2"}, json.loads(output))

    def test_no_candidate_fails_with_clear_status(self) -> None:
        users = {"advertiser_admin": []}
        argv = ["--target-role", "advertiser_admin", "--partner-kind", "vendors"]

        code, output = self.run_helper(argv, users, {})

        self.assertEqual(1, code)
        self.assertEqual("", output)

    def test_empty_contour_reports_no_candidate(self) -> None:
        users = {"advertiser_admin": [user("u-1")]}
        cards = {"id-advertiser_account_manager": {"vendors": []}, "u-1": {"vendors": [{"id": "p1"}]}}
        argv = ["--target-role", "advertiser_admin", "--partner-kind", "vendors",
                "--in-contour", "advertiser_account_manager"]

        code, output = self.run_helper(argv, users, cards)

        self.assertEqual(1, code)
        self.assertEqual("", output)


if __name__ == "__main__":
    unittest.main()
