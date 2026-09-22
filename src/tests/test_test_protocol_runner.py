from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "projects" / "upl" / "scripts"

FAKE_PSQL = """#!/bin/sh
role=""
prev=""
for argument in "$@"; do
  if [ "$prev" = "--set" ]; then role="$argument"; fi
  prev="$argument"
done
role=${role#role=}
if [ "$role" = "__dbfail__" ]; then
  exit 1
fi
safe=$(printf '%s' "$role" | tr -c 'A-Za-z0-9_' '_')
eval "lines=\\$FAKE_ROLE_${safe}"
[ -n "$lines" ] && printf '%s\\n' "$lines"
exit 0
"""


def load_runner_modules():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import test_protocol_auth
    import test_protocol_methods

    return test_protocol_auth, test_protocol_methods


class TestProtocolRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.psql = self.root / "psql"
        self.psql.write_text(FAKE_PSQL, encoding="utf-8")
        self.psql.chmod(0o755)
        self.manifest = self.root / "manifest.json"
        self.auth, self.methods = load_runner_modules()

    def environment(self, **roles: str) -> dict[str, str]:
        values = {
            "PATH": f"{self.root}:{os.environ['PATH']}",
            "UPL_DEV_DATABASE_URL": "postgresql://tester@localhost:5432/upl",
        }
        values.update({f"FAKE_ROLE_{name}": value for name, value in roles.items()})
        return values

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
        environment: dict[str, str],
        run_scenario=None,
        sessions_seen: list | None = None,
        seen_scenarios: list | None = None,
        extra: list[str] | None = None,
    ) -> tuple[int, str]:
        buffer = io.StringIO()

        def fake_sessions(user_ids, *, base_url, timeout):
            if sessions_seen is not None:
                sessions_seen.append(sorted(user_ids))
            return {role: object() for role in user_ids}

        def default_run(scenario, **kwargs):
            if seen_scenarios is not None:
                seen_scenarios.append(scenario["id"])
            return self.scenario_result(scenario, 200)

        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(self.methods, "create_sessions", fake_sessions), \
                mock.patch.object(self.methods, "run_scenario", run_scenario or default_run), \
                contextlib.redirect_stdout(buffer):
            code = self.methods.main(
                ["--manifest", str(self.manifest), "--phase", "required", *(extra or [])]
            )
        return code, buffer.getvalue()

    def test_resolve_reports_missing_role_without_raising(self) -> None:
        with mock.patch.dict(os.environ, self.environment(alpha="111")):
            users, missing = self.auth.resolve_user_ids({"alpha", "beta"})

        self.assertEqual({"alpha": "111"}, users)
        self.assertEqual({"beta"}, missing)

    def test_resolve_raises_on_ambiguous_role(self) -> None:
        with mock.patch.dict(os.environ, self.environment(gamma="1\n2")):
            with self.assertRaises(self.auth.AuthError) as raised:
                self.auth.resolve_user_ids({"gamma"})

        self.assertIn("found 2", str(raised.exception))

    def test_resolve_raises_on_database_failure(self) -> None:
        with mock.patch.dict(os.environ, self.environment()):
            with self.assertRaises(self.auth.AuthError) as raised:
                self.auth.resolve_user_ids({"__dbfail__"})

        self.assertIn("Could not query", str(raised.exception))

    def test_resolve_requires_roles(self) -> None:
        with self.assertRaises(self.auth.AuthError):
            self.auth.resolve_user_ids(set())

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
            self.environment(alpha="111"),
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

        code, output = self.run_runner(
            self.environment(alpha="111"),
            seen_scenarios=seen,
        )

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
            self.environment(alpha="111"),
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

        code, output = self.run_runner(self.environment(), sessions_seen=sessions)

        self.assertEqual(0, code, output)
        self.assertEqual([[]], sessions)
        self.assertEqual(2, output.count("RESULT SKIP"))

    def test_runner_plan_only_needs_no_database(self) -> None:
        self.write_manifest([self.read_only_scenario("s-alpha", "alpha", "/a")])
        environment = self.environment()
        environment.pop("UPL_DEV_DATABASE_URL")

        code, output = self.run_runner(environment, extra=["--plan-only"])

        self.assertEqual(0, code, output)
        self.assertIn("PLAN phase=required scenarios=1", output)


if __name__ == "__main__":
    unittest.main()
