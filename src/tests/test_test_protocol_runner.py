from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "projects" / "upl" / "scripts"


def load_runner_modules():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import test_protocol_auth
    import test_protocol_methods

    return test_protocol_auth, test_protocol_methods


def api_session(auth, rows_by_role=None, list_status=200):
    """Подмена DevSession: dev-login считается успешным, LIST отдаёт заготовленное."""

    class FakeApiSession:
        def __init__(self, role, user_id, *, base_url, timeout=30.0):
            self.role, self.user_id = role, user_id

        def request(self, method, url, *, body=None, headers=None):
            match = re.search(r"role_codes=([A-Za-z0-9_:-]+)", url)
            rows = rows_by_role.get(match.group(1), []) if match and rows_by_role else []
            payload = json.dumps({"data": rows}).encode("utf-8")
            return auth.HTTPResponse(status=list_status, body=payload, headers={})

    return FakeApiSession


class TestProtocolRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.manifest = self.root / "manifest.json"
        self.auth, self.methods = load_runner_modules()

    def scenario_result(self, scenario: dict, status: int):
        target = self.methods.StepResult(
            label="target",
            role=scenario["role"],
            method=scenario["method"],
            path=scenario["path"],
            status=status,
            expected=tuple(scenario["expected_status"]),
            body_sha256=None,
            body_size=0,
        )
        return self.methods.ScenarioResult(
            scenario_id=scenario["id"],
            role=scenario["role"],
            method=scenario["method"].upper(),
            path=scenario["path"],
            target=target,
            preparation=(),
            cleanup=(),
            error=None,
        )

    def run_runner(
        self,
        user_ids: dict[str, str],
        missing: set[str] | None = None,
        run_scenario=None,
        sessions_seen: list | None = None,
        seen_scenarios: list | None = None,
        extra: list[str] | None = None,
    ) -> tuple[int, str]:
        buffer = io.StringIO()

        def fake_resolver(roles, *, base_url, timeout):
            return dict(user_ids), set(missing or ())

        def fake_sessions(resolved, *, base_url, timeout):
            if sessions_seen is not None:
                sessions_seen.append(sorted(resolved))
            return {role: object() for role in resolved}

        def default_run(scenario, **kwargs):
            if seen_scenarios is not None:
                seen_scenarios.append(scenario["id"])
            return self.scenario_result(scenario, 200)

        with mock.patch.object(self.methods, "resolve_user_ids_via_api", fake_resolver), \
                mock.patch.object(self.methods, "create_sessions", fake_sessions), \
                mock.patch.object(self.methods, "run_scenario", run_scenario or default_run), \
                contextlib.redirect_stdout(buffer):
            code = self.methods.main(
                ["--manifest", str(self.manifest), "--phase", "required", *(extra or [])]
            )
        return code, buffer.getvalue()

    def test_resolve_reports_missing_role_without_raising(self) -> None:
        fake = api_session(self.auth, {"alpha": [{"id": "111"}]})
        with mock.patch.object(self.auth, "DevSession", fake):
            users, missing = self.auth.resolve_user_ids_via_api({"alpha", "beta"}, base_url="https://dev.test")

        self.assertEqual({"alpha": "111"}, users)
        self.assertEqual({"beta"}, missing)

    def test_resolve_takes_first_id_by_sort_order(self) -> None:
        fake = api_session(self.auth, {"gamma": [{"id": "9"}, {"id": "2"}]})
        with mock.patch.object(self.auth, "DevSession", fake):
            users, missing = self.auth.resolve_user_ids_via_api({"gamma"}, base_url="https://dev.test")

        self.assertEqual({"gamma": "2"}, users)
        self.assertEqual(set(), missing)

    def test_resolve_raises_on_failed_listing(self) -> None:
        fake = api_session(self.auth, list_status=503)
        with mock.patch.object(self.auth, "DevSession", fake):
            with self.assertRaises(self.auth.AuthError) as raised:
                self.auth.resolve_user_ids_via_api({"gamma"}, base_url="https://dev.test")

        self.assertIn("HTTP 503", str(raised.exception))

    def test_bootstrap_failure_names_the_override(self) -> None:
        auth = self.auth

        class DeadBootstrap:
            def __init__(self, role, user_id, *, base_url, timeout=30.0):
                raise auth.AuthError(f"Dev login failed for role {role}: HTTP 404")

        with mock.patch.object(auth, "DevSession", DeadBootstrap):
            with self.assertRaises(auth.AuthError) as raised:
                auth.resolve_user_ids_via_api({"alpha"}, base_url="https://dev.test")

        message = str(raised.exception)
        self.assertIn("HTTP 404", message)
        self.assertIn("UPL_DEV_BOOTSTRAP_USER_ID", message)
        self.assertIn("DEFAULT_BOOTSTRAP_USER_ID", message)

    def write_manifest(self, scenarios: list[dict]) -> None:
        self.manifest.write_text(
            json.dumps({"required": scenarios, "extended": []}),
            encoding="utf-8",
        )

    @staticmethod
    def read_only_scenario(scenario_id: str, role: str, path: str) -> dict:
        return {
            "id": scenario_id,
            "role": role,
            "method": "GET",
            "path": path,
            "expected_status": [200],
        }

    @staticmethod
    def delete_scenario(cleanup: list[dict]) -> dict:
        return {
            "id": "s-del",
            "role": "alpha",
            "method": "DELETE",
            "path": "/api/v1/b/{fix.json.id}",
            "expected_status": [204],
            "prepare": [
                {
                    "method": "POST",
                    "path": "/api/v1/b",
                    "expected_status": [201],
                    "json": {"name": "qa-${run_id}"},
                    "save_as": "fix",
                }
            ],
            "cleanup": cleanup,
        }

    def test_runner_skips_cell_without_active_user(self) -> None:
        self.write_manifest(
            [
                self.read_only_scenario("s-alpha", "alpha", "/a"),
                self.read_only_scenario("s-beta", "beta", "/b"),
            ]
        )
        seen: list[str] = []
        sessions: list[list[str]] = []

        code, output = self.run_runner(
            {"alpha": "1"},
            missing={"beta"},
            sessions_seen=sessions,
            seen_scenarios=seen,
        )

        self.assertEqual(0, code, output)
        self.assertEqual(["s-alpha"], seen)
        self.assertEqual([["alpha"]], sessions)
        self.assertIn("SKIPPED role=beta", output)
        self.assertIn("RESULT SKIP s-beta", output)
        self.assertIn("RESULT PASS s-alpha", output)

    def test_runner_skips_cell_when_cleanup_role_missing(self) -> None:
        mixed = self.read_only_scenario("s-mix", "alpha", "/c")
        mixed["cleanup"] = [
            {
                "method": "POST",
                "path": "/c/reset",
                "role": "beta",
                "expected_status": [204],
            }
        ]
        self.write_manifest([mixed])
        seen: list[str] = []

        code, output = self.run_runner({"alpha": "1"}, missing={"beta"}, seen_scenarios=seen)

        self.assertEqual(0, code, output)
        self.assertEqual([], seen)
        self.assertIn("RESULT SKIP s-mix", output)

    def test_runner_fail_cell_fails_phase(self) -> None:
        self.write_manifest(
            [
                self.read_only_scenario("s-alpha", "alpha", "/a"),
                self.read_only_scenario("s-beta", "beta", "/b"),
            ]
        )

        def failing(scenario, **kwargs):
            return self.scenario_result(scenario, 403)

        code, output = self.run_runner(
            {"alpha": "1"},
            missing={"beta"},
            run_scenario=failing,
        )

        self.assertEqual(1, code, output)
        self.assertIn("RESULT FAIL s-alpha", output)
        self.assertIn("RESULT SKIP s-beta", output)

    def test_runner_all_roles_missing_passes_with_skips(self) -> None:
        self.write_manifest(
            [
                self.read_only_scenario("s-alpha", "alpha", "/a"),
                self.read_only_scenario("s-beta", "beta", "/b"),
            ]
        )
        sessions: list[list[str]] = []

        code, output = self.run_runner({}, missing={"alpha", "beta"}, sessions_seen=sessions)

        self.assertEqual(0, code, output)
        self.assertEqual([[]], sessions)
        self.assertEqual(2, output.count("RESULT SKIP"))

    def test_runner_plan_only_needs_no_network(self) -> None:
        self.write_manifest([self.read_only_scenario("s-alpha", "alpha", "/a")])

        code, output = self.run_runner({}, extra=["--plan-only"])

        self.assertEqual(0, code, output)
        self.assertIn("PLAN phase=required scenarios=1", output)

    def test_capture_value_supports_list_index(self) -> None:
        captures = {"cap": {"json": {"data": [{"id": 42}], "0": "zero"}}}

        self.assertEqual(42, self.methods._capture_value(captures, "cap", "json.data.0.id"))
        self.assertEqual("zero", self.methods._capture_value(captures, "cap", "json.0"))
        with self.assertRaises(self.methods.ScenarioError):
            self.methods._capture_value(captures, "cap", "json.data.3.id")

    def write_and_load(self, scenarios: list[dict]):
        self.write_manifest(scenarios)
        return self.methods.load_manifest(self.manifest, "required")

    def test_delete_cleanup_without_capture_is_rejected(self) -> None:
        scenarios = [
            self.delete_scenario(
                [{"method": "DELETE", "path": "/api/v1/b/42", "expected_status": [204]}]
            )
        ]
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load(scenarios)

        self.assertIn("captured record", str(raised.exception))

    def test_delete_cleanup_with_template_placeholder_is_rejected(self) -> None:
        scenarios = [
            self.delete_scenario(
                [
                    {
                        "method": "DELETE",
                        "path": "/api/v1/b/{{fixture_id}}",
                        "expected_status": [204],
                    }
                ]
            )
        ]
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load(scenarios)

        self.assertIn("captured record", str(raised.exception))

    def test_delete_cleanup_capture_positions_are_accepted(self) -> None:
        by_path = self.delete_scenario(
            [{"method": "DELETE", "path": "/api/v1/b/{fix.json.id}", "expected_status": [204]}]
        )
        by_query = self.delete_scenario(
            [
                {
                    "method": "DELETE",
                    "path": "/api/v1/b",
                    "query": {"ref": "{fix.json.id}"},
                    "expected_status": [204],
                }
            ]
        )

        for scenario in (by_path, by_query):
            loaded, _ = self.write_and_load([scenario])
            self.assertEqual([scenario], loaded["required"])

    def test_delete_cleanup_capture_in_body_is_rejected(self) -> None:
        scenarios = [
            self.delete_scenario(
                [
                    {
                        "method": "DELETE",
                        "path": "/api/v1/b",
                        "json": {"comment": "{fix.json.id}"},
                        "expected_status": [204],
                    }
                ]
            )
        ]
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load(scenarios)

        self.assertIn("captured record", str(raised.exception))

    def test_delete_cleanup_rejects_overwritten_capture(self) -> None:
        scenario = self.delete_scenario(
            [{"method": "DELETE", "path": "/api/v1/b/{fix.json.id}", "expected_status": [204]}]
        )
        scenario["prepare"].append(
            {
                "method": "GET",
                "path": "/api/v1/b/42",
                "expected_status": [200],
                "save_as": "fix",
            }
        )
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load([scenario])

        self.assertIn("created by this scenario's prepare", str(raised.exception))

    def test_delete_target_with_static_path_is_rejected(self) -> None:
        scenario = self.delete_scenario(
            [{"method": "DELETE", "path": "/api/v1/b/{fix.json.id}", "expected_status": [204]}]
        )
        scenario["path"] = "/api/v1/b/42"
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load([scenario])

        self.assertIn("DELETE target", str(raised.exception))

    def test_delete_target_with_foreign_capture_is_rejected(self) -> None:
        scenario = self.delete_scenario(
            [{"method": "DELETE", "path": "/api/v1/b/{fix.json.id}", "expected_status": [204]}]
        )
        scenario["prepare"].append(
            {
                "method": "GET",
                "path": "/api/v1/b/42",
                "expected_status": [200],
                "save_as": "ext",
            }
        )
        scenario["path"] = "/api/v1/b/{ext.json.id}"
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load([scenario])

        self.assertIn("created by this scenario's prepare", str(raised.exception))

    def test_delete_cleanup_from_get_capture_is_rejected(self) -> None:
        scenario = self.delete_scenario(
            [{"method": "DELETE", "path": "/api/v1/b/{ext.json.id}", "expected_status": [204]}]
        )
        scenario["prepare"] = [
            {
                "method": "GET",
                "path": "/api/v1/b/42",
                "expected_status": [200],
                "save_as": "ext",
            }
        ]
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load([scenario])

        self.assertIn("created by this scenario's prepare", str(raised.exception))

    def test_restore_cleanup_without_capture_stays_allowed(self) -> None:
        scenarios = [
            self.delete_scenario(
                [
                    {
                        "method": "PUT",
                        "path": "/api/v1/b/42",
                        "expected_status": [200],
                        "json_from": {"capture": "fix"},
                    }
                ]
            )
        ]

        self.write_and_load(scenarios)

    def step(self, label: str, status: int):
        return self.methods.StepResult(
            label=label,
            role="alpha",
            method="GET",
            path="/api/v1/b",
            status=status,
            expected=(200,),
            body_sha256=None,
            body_size=0,
        )

    @staticmethod
    def prerequisite_scenario() -> dict:
        return {
            "id": "s-block",
            "role": "alpha",
            "method": "GET",
            "path": "/api/v1/b/{lookup.json.id}",
            "expected_status": [200],
            "prepare": [
                {
                    "method": "GET",
                    "path": "/api/v1/b?ref=qa",
                    "expected_status": [200],
                    "prerequisite": True,
                    "save_as": "lookup",
                }
            ],
        }

    def test_prerequisite_prepare_lookup_passes_validation(self) -> None:
        self.write_and_load([self.prerequisite_scenario()])

    def test_prerequisite_rejected_outside_prepare(self) -> None:
        on_target = self.prerequisite_scenario()
        on_target["prerequisite"] = True
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load([on_target])

        self.assertIn("only allowed on prepare", str(raised.exception))

        on_cleanup = self.prerequisite_scenario()
        on_cleanup["cleanup"] = [
            {"method": "DELETE", "path": "/api/v1/b", "expected_status": [204], "prerequisite": True}
        ]
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load([on_cleanup])

        self.assertIn("only allowed on prepare", str(raised.exception))

    def test_prerequisite_must_be_true_read_only(self) -> None:
        scenario = self.prerequisite_scenario()
        scenario["prepare"][0]["prerequisite"] = False
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load([scenario])

        self.assertIn("prerequisite must be true", str(raised.exception))

        mutating = self.prerequisite_scenario()
        mutating["prepare"][0].update({"method": "POST", "prerequisite": True})
        with self.assertRaises(self.methods.ScenarioError) as raised:
            self.write_and_load([mutating])

        self.assertIn("read-only lookup", str(raised.exception))

    def test_prerequisite_404_marks_scenario_blocked(self) -> None:
        scenario = self.prerequisite_scenario()
        with mock.patch.object(
            self.methods, "run_request", side_effect=[self.step("prepare[0]", 404)]
        ):
            result = self.methods.run_scenario(
                scenario, base_url="https://dev.test", sessions={"alpha": object()}, timeout=5
            )

        self.assertTrue(result.blocked)
        self.assertIn("prerequisite record absent", result.error)
        self.assertIsNone(result.target)

    def test_plain_prepare_404_stays_fail(self) -> None:
        scenario = self.prerequisite_scenario()
        del scenario["prepare"][0]["prerequisite"]
        with mock.patch.object(
            self.methods, "run_request", side_effect=[self.step("prepare[0]", 404)]
        ):
            result = self.methods.run_scenario(
                scenario, base_url="https://dev.test", sessions={"alpha": object()}, timeout=5
            )

        self.assertFalse(result.blocked)
        self.assertIn("preparation failed", result.error)

    def test_blocked_cell_fails_phase(self) -> None:
        self.write_manifest([self.read_only_scenario("s-alpha", "alpha", "/a")])

        def blocked(scenario, **kwargs):
            return self.methods.ScenarioResult(
                scenario_id=scenario["id"],
                role=scenario["role"],
                method=scenario["method"].upper(),
                path=scenario["path"],
                target=None,
                preparation=(),
                cleanup=(),
                error="prerequisite record absent at prepare[0]",
                blocked=True,
            )

        code, output = self.run_runner({"alpha": "1"}, run_scenario=blocked)

        self.assertEqual(1, code, output)
        self.assertIn("RESULT BLOCKED s-alpha", output)


if __name__ == "__main__":
    unittest.main()
