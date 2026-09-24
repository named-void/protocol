#!/usr/bin/env python3
"""Run UPL method authorization scenarios from a per-method manifest."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from test_protocol_auth import (
    AuthError,
    DevSession,
    create_sessions,
    resolve_user_ids,
    resolve_user_ids_via_api,
)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
REFERENCE_PATTERN = re.compile(r"\{([^{}]+)\}")
TEMPLATE_PATTERN = re.compile(r"\{\{[^{}]*\}\}")
RUN_ID_PATTERN = re.compile(r"\$\{run_id\}")


class ScenarioError(RuntimeError):
    """Raised for an invalid or incomplete method scenario."""


@dataclass(frozen=True)
class StepResult:
    label: str
    role: str
    method: str
    path: str
    status: int
    expected: tuple[int, ...]
    body_sha256: str | None
    body_size: int

    @property
    def passed(self) -> bool:
        return self.status in self.expected


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    role: str
    method: str
    path: str
    target: StepResult | None
    preparation: tuple[StepResult, ...]
    cleanup: tuple[StepResult, ...]
    error: str | None
    skipped: bool = False
    blocked: bool = False

    @property
    def passed(self) -> bool:
        if self.error or self.target is None or not self.target.passed:
            return False
        return all(step.passed for step in self.preparation + self.cleanup)


def load_manifest(path: Path, phase: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioError(f"Cannot read manifest {path}: {error}") from error
    if not isinstance(manifest, dict):
        raise ScenarioError("Manifest root must be an object")
    scenarios = manifest.get(phase)
    if not isinstance(scenarios, list) or not scenarios:
        raise ScenarioError(f"Manifest has no non-empty {phase} scenario list")
    for scenario in scenarios:
        _validate_scenario(scenario)
    return manifest, scenarios


def _validate_scenario(scenario: Any) -> None:
    if not isinstance(scenario, dict):
        raise ScenarioError("Every scenario must be an object")
    for field in ("id", "role", "method", "path", "expected_status"):
        if field not in scenario:
            raise ScenarioError(f"Scenario misses {field}")
    if not isinstance(scenario["id"], str) or not scenario["id"]:
        raise ScenarioError("Scenario id must be a non-empty string")
    if not isinstance(scenario["role"], str) or not scenario["role"]:
        raise ScenarioError(f"Scenario {scenario['id']} has no role")
    _validate_request(scenario, f"scenario {scenario['id']}")
    preparation = scenario.get("prepare", [])
    cleanup = scenario.get("cleanup", [])
    if not isinstance(preparation, list) or not isinstance(cleanup, list):
        raise ScenarioError(f"Scenario {scenario['id']} prepare/cleanup must be arrays")
    for index, request in enumerate(preparation):
        _validate_request(request, f"scenario {scenario['id']} prepare[{index}]")
        _validate_prerequisite(request, f"scenario {scenario['id']} prepare[{index}]")
    if "prerequisite" in scenario:
        raise ScenarioError(
            f"scenario {scenario['id']} prerequisite is only allowed on prepare steps"
        )
    for index, request in enumerate(cleanup):
        _validate_request(request, f"scenario {scenario['id']} cleanup[{index}]")
        if "prerequisite" in request:
            raise ScenarioError(
                f"scenario {scenario['id']} cleanup[{index}] prerequisite is only allowed "
                "on prepare steps"
            )
        if str(request.get("method", "")).upper() != "DELETE":
            continue
        names = _capture_names(request)
        if not names:
            raise ScenarioError(
                f"DELETE cleanup[{index}] of scenario {scenario['id']} must reference a "
                "captured record of this run in path or query; broad deletions "
                "are not allowed"
            )
        unknown = sorted(names - _created_capture_names(preparation))
        if unknown:
            raise ScenarioError(
                f"DELETE cleanup[{index}] of scenario {scenario['id']} must delete a "
                "record created by this scenario's prepare; capture is unknown or "
                f"not created by POST prepare: {', '.join(unknown)}"
            )
    mutation = _request_is_mutating(scenario) or any(
        _request_is_mutating(request) for request in preparation + cleanup
    )
    if mutation and not cleanup:
        raise ScenarioError(
            f"Mutating scenario {scenario['id']} must declare cleanup requests"
        )
    target_method = scenario["method"].upper()
    if target_method in {"PUT", "PATCH"} and not any(
        request.get("method", "").upper() == "GET" for request in preparation
    ):
        raise ScenarioError(
            f"{target_method} scenario {scenario['id']} must read the existing object first"
        )
    if target_method in {"PUT", "PATCH"}:
        if not isinstance(scenario.get("json_patch"), dict) or not scenario["json_patch"]:
            raise ScenarioError(
                f"{target_method} scenario {scenario['id']} must declare a non-empty json_patch"
            )
        if not any("json_from" in request for request in cleanup):
            raise ScenarioError(
                f"{target_method} scenario {scenario['id']} must restore the captured body"
            )
    if target_method == "DELETE":
        if not any(
            request.get("method", "").upper() == "POST" for request in preparation
        ):
            raise ScenarioError(
                f"DELETE scenario {scenario['id']} must create a fixture with POST first"
            )
        names = _capture_names(scenario)
        if not names:
            raise ScenarioError(
                f"DELETE target of scenario {scenario['id']} must reference a captured "
                "record of this run in path or query; broad deletions are not allowed"
            )
        unknown = sorted(names - _created_capture_names(preparation))
        if unknown:
            raise ScenarioError(
                f"DELETE target of scenario {scenario['id']} must delete a record "
                "created by this scenario's prepare; capture is unknown or not "
                f"created by POST prepare: {', '.join(unknown)}"
            )


def _validate_request(request: Any, label: str) -> None:
    if not isinstance(request, dict):
        raise ScenarioError(f"{label} must be an object")
    method = request.get("method")
    path = request.get("path")
    expected = request.get("expected_status")
    if not isinstance(method, str) or method.upper() not in {
        "GET",
        "HEAD",
        "OPTIONS",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }:
        raise ScenarioError(f"{label} has an unsupported HTTP method")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ScenarioError(f"{label} path must start with /")
    if not isinstance(expected, list) or not expected or not all(
        isinstance(status, int) and 100 <= status <= 599 for status in expected
    ):
        raise ScenarioError(f"{label} expected_status must be a non-empty status list")


def _request_is_mutating(request: dict[str, Any]) -> bool:
    return str(request.get("method", "")).upper() not in SAFE_METHODS


def _validate_prerequisite(request: dict[str, Any], label: str) -> None:
    if "prerequisite" not in request:
        return
    if request["prerequisite"] is not True:
        raise ScenarioError(f"{label} prerequisite must be true")
    if _request_is_mutating(request):
        raise ScenarioError(f"{label} prerequisite must be a read-only lookup")


def _capture_names(request: dict[str, Any]) -> set[str]:
    """Capture names referenced in path or query (``{name...}``/``$capture``)."""
    names: set[str] = set()

    def scan(value: Any) -> None:
        if isinstance(value, str):
            value = RUN_ID_PATTERN.sub("", value)
            value = TEMPLATE_PATTERN.sub("", value)
            for match in REFERENCE_PATTERN.finditer(value):
                names.add(match.group(1).split(".")[0])
        elif isinstance(value, dict):
            if isinstance(value.get("$capture"), str):
                names.add(value["$capture"])
            for item in value.values():
                scan(item)
        elif isinstance(value, list):
            for item in value:
                scan(item)

    scan(request.get("path", ""))
    scan(request.get("query", {}))
    return names


def _created_capture_names(preparation: list[dict[str, Any]]) -> set[str]:
    """Names whose last prepare writer is a creating (POST) step."""
    provenance: dict[str, bool] = {}
    for request in preparation:
        name = request.get("save_as")
        if isinstance(name, str):
            provenance[name] = request.get("method", "").upper() == "POST"
    return {name for name, created in provenance.items() if created}


def _scenario_roles(scenarios: list[dict[str, Any]]) -> set[str]:
    roles: set[str] = set()
    for scenario in scenarios:
        roles.update(_request_roles(scenario, scenario["role"]))
    return roles


def _request_roles(request: dict[str, Any], default_role: str) -> set[str]:
    roles = {request.get("role", default_role)}
    for nested in request.get("prepare", []) + request.get("cleanup", []):
        roles.update(_request_roles(nested, default_role))
    return {role for role in roles if isinstance(role, str) and role}


def _scenario_mutates(scenarios: list[dict[str, Any]]) -> bool:
    return any(
        _request_is_mutating(scenario)
        or any(_request_is_mutating(request) for request in scenario.get("prepare", []))
        or any(_request_is_mutating(request) for request in scenario.get("cleanup", []))
        for scenario in scenarios
    )


def print_plan(phase: str, scenarios: list[dict[str, Any]]) -> None:
    print(f"PLAN phase={phase} scenarios={len(scenarios)}")
    for scenario in scenarios:
        mutation = "mutation" if _scenario_mutates([scenario]) else "read-only"
        print(
            f"- {scenario['id']}: {scenario['method'].upper()} {scenario['path']} "
            f"role={scenario['role']} expected={','.join(map(str, scenario['expected_status']))} {mutation}"
        )
        for label, requests in (("prepare", scenario.get("prepare", [])), ("cleanup", scenario.get("cleanup", []))):
            for index, request in enumerate(requests):
                print(
                    f"  {label}[{index}]: {request['method'].upper()} {request['path']} "
                    f"role={request.get('role', scenario['role'])} "
                    f"expected={','.join(map(str, request['expected_status']))}"
                )


def run_scenario(
    scenario: dict[str, Any],
    *,
    base_url: str,
    sessions: dict[str, DevSession],
    timeout: float,
) -> ScenarioResult:
    captures: dict[str, dict[str, Any]] = {}
    preparation: list[StepResult] = []
    cleanup: list[StepResult] = []
    target: StepResult | None = None
    error: str | None = None
    blocked = False
    run_id = uuid.uuid4().hex[:12]

    try:
        for index, request in enumerate(scenario.get("prepare", [])):
            step = run_request(
                request,
                label=f"prepare[{index}]",
                default_role=scenario["role"],
                base_url=base_url,
                sessions=sessions,
                captures=captures,
                run_id=run_id,
                timeout=timeout,
            )
            preparation.append(step)
            if not step.passed:
                if request.get("prerequisite") is True and step.status == 404:
                    blocked = True
                    error = f"prerequisite record absent at {step.label}"
                else:
                    error = f"preparation failed at {step.label}"
                break
        if error is None:
            target = run_request(
                scenario,
                label="target",
                default_role=scenario["role"],
                base_url=base_url,
                sessions=sessions,
                captures=captures,
                run_id=run_id,
                timeout=timeout,
            )
    except (AuthError, ScenarioError) as caught:
        error = str(caught)
    finally:
        for index, request in enumerate(scenario.get("cleanup", [])):
            try:
                cleanup.append(
                    run_request(
                        request,
                        label=f"cleanup[{index}]",
                        default_role=scenario["role"],
                        base_url=base_url,
                        sessions=sessions,
                        captures=captures,
                        run_id=run_id,
                        timeout=timeout,
                    )
                )
            except (AuthError, ScenarioError) as caught:
                error = error or str(caught)

    return ScenarioResult(
        scenario_id=scenario["id"],
        role=scenario["role"],
        method=scenario["method"].upper(),
        path=scenario["path"],
        target=target,
        preparation=tuple(preparation),
        cleanup=tuple(cleanup),
        error=error,
        blocked=blocked,
    )


def run_request(
    request: dict[str, Any],
    *,
    label: str,
    default_role: str,
    base_url: str,
    sessions: dict[str, DevSession],
    captures: dict[str, dict[str, Any]],
    run_id: str,
    timeout: float,
) -> StepResult:
    role = request.get("role", default_role)
    if role not in sessions:
        raise ScenarioError(f"No authenticated session for role {role}")
    method = request["method"].upper()
    path = _resolve_string(request["path"], captures, run_id)
    url = _build_url(base_url, path, request.get("query", {}), captures, run_id)
    headers = _resolve_value(request.get("headers", {}), captures, run_id)
    if not isinstance(headers, dict):
        raise ScenarioError(f"{label} headers must resolve to an object")
    body = _request_body(request, captures, run_id)
    encoded_body = None
    if body is not None:
        encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **headers}
    response = sessions[role].request(method, url, body=encoded_body, headers=headers)
    step = StepResult(
        label=label,
        role=role,
        method=method,
        path=path,
        status=response.status,
        expected=tuple(request["expected_status"]),
        body_sha256=_body_sha(response.body),
        body_size=len(response.body),
    )
    save_as = request.get("save_as")
    if save_as:
        captures[save_as] = {
            "status": response.status,
            "json": response.json,
            "body_sha256": step.body_sha256,
            "body_size": step.body_size,
        }
    return step


def _request_body(
    request: dict[str, Any], captures: dict[str, dict[str, Any]], run_id: str
) -> Any:
    if "json_from" in request:
        source = request["json_from"]
        if not isinstance(source, dict) or not isinstance(source.get("capture"), str):
            raise ScenarioError("json_from must contain capture")
        body = copy.deepcopy(_capture_value(captures, source["capture"], source.get("path", "json")))
        if "json_patch" in request:
            patch = _resolve_value(request["json_patch"], captures, run_id)
            body = _deep_merge(body, patch)
        return body
    if "json" in request:
        return _resolve_value(request["json"], captures, run_id)
    return None


def _build_url(
    base_url: str,
    path: str,
    query: Any,
    captures: dict[str, dict[str, Any]],
    run_id: str,
) -> str:
    if not isinstance(query, dict):
        raise ScenarioError("query must be an object")
    resolved_query = _resolve_value(query, captures, run_id)
    encoded = urlencode(resolved_query, doseq=True)
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}" + (f"?{encoded}" if encoded else "")


def _resolve_value(value: Any, captures: dict[str, dict[str, Any]], run_id: str) -> Any:
    if isinstance(value, list):
        return [_resolve_value(item, captures, run_id) for item in value]
    if isinstance(value, dict):
        if "$capture" in value:
            capture = value.get("$capture")
            if not isinstance(capture, str):
                raise ScenarioError("$capture must be a string")
            return copy.deepcopy(_capture_value(captures, capture, value.get("$path", "json")))
        return {key: _resolve_value(item, captures, run_id) for key, item in value.items()}
    if isinstance(value, str):
        if value == "${run_id}":
            return run_id
        return _resolve_string(value, captures, run_id)
    return value


def _resolve_string(value: str, captures: dict[str, dict[str, Any]], run_id: str) -> str:
    value = RUN_ID_PATTERN.sub(run_id, value)

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        parts = token.split(".")
        if not parts or parts[0] not in captures:
            raise ScenarioError(f"Unknown capture reference {{{token}}}")
        resolved = _capture_value(captures, parts[0], ".".join(parts[1:]) or "json")
        if resolved is None:
            raise ScenarioError(f"Capture reference {{{token}}} is null")
        if isinstance(resolved, (dict, list)):
            raise ScenarioError(f"Capture reference {{{token}}} is not scalar")
        return str(resolved)

    return REFERENCE_PATTERN.sub(replace, value)


def _capture_value(captures: dict[str, dict[str, Any]], name: str, path: str) -> Any:
    if name not in captures:
        raise ScenarioError(f"Unknown capture {name}")
    value: Any = captures[name]
    if path:
        for part in path.split("."):
            if isinstance(value, list) and part.isdecimal() and int(part) < len(value):
                value = value[int(part)]
            elif isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise ScenarioError(f"Capture {name} has no path {path}")
    return value


def _deep_merge(base: Any, patch: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(patch, dict):
        raise ScenarioError("json_from/json_patch requires object bodies")
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _body_sha(body: bytes) -> str | None:
    return hashlib.sha256(body).hexdigest() if body else None


def print_result(result: ScenarioResult) -> None:
    outcome = "SKIP" if result.skipped else (
        "BLOCKED" if result.blocked else ("PASS" if result.passed else "FAIL")
    )
    print(
        f"RESULT {outcome} {result.scenario_id}: {result.method} {result.path} role={result.role}"
    )
    if result.error:
        print(f"  error: {result.error}")
    for step in result.preparation + ((result.target,) if result.target else tuple()) + result.cleanup:
        status = "PASS" if step.passed else "FAIL"
        digest = step.body_sha256 or "empty"
        print(
            f"  {status} {step.label} {step.method} {step.path} role={step.role} "
            f"status={step.status} expected={','.join(map(str, step.expected))} "
            f"body_sha256={digest} body_bytes={step.body_size}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--phase", choices=("required", "extended"), default="required")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("UPL_DEV_BASE_URL", "https://develop.getblogger.ru"),
    )
    parser.add_argument("--db-url-env", default="UPL_DEV_DATABASE_URL")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        manifest, scenarios = load_manifest(args.manifest, args.phase)
        base_url = args.base_url.rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ScenarioError("base_url must be an absolute HTTP(S) URL")
        print_plan(args.phase, scenarios)
        if args.plan_only:
            return 0
        roles = _scenario_roles(scenarios)
        bootstrap_user_id = os.environ.get("UPL_DEV_BOOTSTRAP_USER_ID")
        if bootstrap_user_id:
            user_ids, missing_roles = resolve_user_ids_via_api(
                roles,
                base_url=base_url,
                bootstrap_user_id=bootstrap_user_id,
                timeout=args.timeout,
            )
        else:
            user_ids, missing_roles = resolve_user_ids(
                roles, db_url_env=args.db_url_env, timeout=args.timeout
            )
        for role in sorted(missing_roles):
            print(f"SKIPPED role={role} (no active Dev DB user)")
        sessions = create_sessions(
            user_ids,
            base_url=base_url,
            timeout=args.timeout,
        )
    except (AuthError, ScenarioError, OSError) as error:
        print(f"test_protocol_methods: {error}", file=sys.stderr)
        return 2

    results = []
    for scenario in scenarios:
        if _scenario_roles([scenario]) & missing_roles:
            result = ScenarioResult(
                scenario_id=scenario["id"],
                role=scenario["role"],
                method=scenario["method"].upper(),
                path=scenario["path"],
                target=None,
                preparation=(),
                cleanup=(),
                error=None,
                skipped=True,
            )
        else:
            result = run_scenario(
                scenario,
                base_url=base_url,
                sessions=sessions,
                timeout=args.timeout,
            )
        results.append(result)
        print_result(result)
    return 0 if all(result.passed or result.skipped for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
