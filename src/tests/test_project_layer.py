"""Периметр проекта: резолв корня, опознание проекта и конфигурация одним
слоем — картой носителя (orchestration/adr/README.md#carrier-config)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "lib" / "skills_config.py"
sys.path.insert(0, str(ROOT / "lib"))

import skills_config

# Карта носителя работы вне проекта — она же фикстура «своих» значений.
OUTSIDE = """\
[issue_tracker]
product = "jira"
my_issues_query = "assignee = currentUser()"

[vcs]
commit_title_format = "<KEY> сделано"
"""


class ProjectLayerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.projects = self.root / "projects"
        self.outside = self.root / "outside.toml"
        self.outside.write_text(OUTSIDE, encoding="utf-8")
        self.work = self.root / "work" / "acme"
        self.work.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_project(self, name: str, body: str) -> Path:
        path = self.projects / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "project.toml").write_text(body, encoding="utf-8")
        return path

    def run_cli(
        self, *args: str, cwd: Path | None = None, carrier: Path | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        # Карта носителя вне проекта подменяется явно: в рабочем дереве правил
        # лежит живая карта, и тест не должен от неё зависеть.
        environment = {**os.environ, "AGENTS_PROJECTS_DIR": str(self.projects)}
        environment.pop("AGENTS_CONFIG", None)
        environment.pop("AGENTS_SECRETS", None)
        if carrier is not None:
            environment["AGENTS_CONFIG"] = str(carrier)
        environment.update(env)
        return subprocess.run(
            [sys.executable, str(CONFIG), *args],
            cwd=str(cwd or ROOT),
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def acme(self) -> None:
        self.write_project(
            "acme",
            f'[project]\nroots = ["{self.work}"]\n\n'
            '[issue_tracker]\nmy_issues_query = "filter = 42"\n\n'
            "[dispatch]\nkey_prefix_map = { ACME = \"acme/services\" }\n",
        )

    # --- Опознание -----------------------------------------------------------

    def test_outside_project_when_no_projects_root(self) -> None:
        result = self.run_cli("project")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("- outside", result.stdout.strip())

    def test_identified_by_working_directory(self) -> None:
        self.acme()
        nested = self.work / "services" / "api"
        nested.mkdir(parents=True)
        result = self.run_cli("project", cwd=nested)
        self.assertEqual("acme cwd", result.stdout.strip())

    def test_identified_by_target_path_before_cwd(self) -> None:
        self.acme()
        result = self.run_cli("project", "--path", str(self.work / "services"))
        self.assertEqual("acme target-path", result.stdout.strip())

    def test_explicit_override_wins(self) -> None:
        self.acme()
        result = self.run_cli("project", AGENTS_PROJECT="acme")
        self.assertEqual("acme override", result.stdout.strip())

    def test_override_without_card_fails(self) -> None:
        result = self.run_cli("project", AGENTS_PROJECT="ghost")
        self.assertEqual(1, result.returncode)
        self.assertIn("no project card", result.stderr)

    def test_identified_by_issue_key_prefix(self) -> None:
        # Маршрут начинается с ключа задачи, когда рабочего каталога ещё нет:
        # confidence gate решает, куда клонировать, по карте проекта.
        self.acme()
        result = self.run_cli("project", "--key", "ACME-42")
        self.assertEqual("acme issue-key", result.stdout.strip())

    def test_unknown_key_prefix_stays_outside(self) -> None:
        self.acme()
        result = self.run_cli("project", "--key", "OTHER-1")
        self.assertEqual("- outside", result.stdout.strip())

    def test_duplicate_key_prefix_is_ambiguous(self) -> None:
        self.acme()
        self.write_project(
            "other",
            f'[project]\nroots = ["{self.root / "other"}"]\n\n'
            '[dispatch]\nkey_prefix_map = { ACME = "other/services" }\n',
        )

        result = self.run_cli("project", "--key", "ACME-42")

        self.assertEqual(1, result.returncode)
        self.assertIn("ambiguous project mapping for issue prefix ACME", result.stderr)

    def test_target_path_wins_over_issue_key(self) -> None:
        self.acme()
        inner = self.work / "services" / "billing"
        inner.mkdir(parents=True)
        self.write_project(
            "billing",
            f'[project]\nroots = ["{inner}"]\n\n'
            "[dispatch]\nkey_prefix_map = { BIL = \"acme/billing\" }\n",
        )
        result = self.run_cli("project", "--path", str(inner), "--key", "ACME-42")
        self.assertEqual("billing target-path", result.stdout.strip())

    def test_vcs_project_uses_longest_namespace_and_first_root(self) -> None:
        broad_root = self.root / "broad"
        service_root = self.root / "service"
        self.write_project(
            "broad",
            f'[project]\nroots = ["{broad_root}"]\n\n'
            '[vcs_host]\nhosts = ["gitlab.example.com"]\n\n'
            '[dispatch]\nkey_prefix_map = { ALL = "group" }\n',
        )
        self.write_project(
            "service",
            f'[project]\nroots = ["{service_root}"]\n\n'
            '[vcs_host]\nhosts = ["gitlab.example.com"]\n\n'
            '[dispatch]\nkey_prefix_map = { APP = "group/services" }\n',
        )

        with mock.patch.dict(
            os.environ, {"AGENTS_PROJECTS_DIR": str(self.projects)}, clear=False
        ):
            result = skills_config.resolve_vcs_project(
                "gitlab.example.com", "group/services/catalog"
            )

        self.assertEqual("service", result["name"])
        self.assertEqual("group/services", result["namespace"])
        self.assertEqual((service_root / "catalog").resolve(), result["local_path"])

    def test_deepest_root_wins(self) -> None:
        # Вложенные корни: ближайший вверх опознаёт проект, иначе работа в
        # подпроекте молча подхватила бы карту родителя.
        self.acme()
        inner = self.work / "services" / "billing"
        inner.mkdir(parents=True)
        self.write_project("billing", f'[project]\nroots = ["{inner}"]\n')
        self.assertEqual("billing cwd", self.run_cli("project", cwd=inner).stdout.strip())
        self.assertEqual("acme cwd", self.run_cli("project", cwd=self.work).stdout.strip())

    # --- Один слой -----------------------------------------------------------

    def test_project_card_is_the_only_source_inside_the_project(self) -> None:
        # Значение носителя работы вне проекта в периметр проекта не протекает:
        # слоя над картой нет, и ключ, объявленный только там, внутри не виден.
        self.acme()
        inside = self.run_cli("get", "issue_tracker.my_issues_query", cwd=self.work)
        self.assertEqual("filter = 42", inside.stdout.strip())
        product = self.run_cli("get", "issue_tracker.product", cwd=self.work)
        self.assertEqual(1, product.returncode, product.stdout)

    def test_outside_carrier_is_read_outside_the_project(self) -> None:
        self.acme()
        result = self.run_cli("get", "issue_tracker.my_issues_query", carrier=self.outside)
        self.assertEqual("assignee = currentUser()", result.stdout.strip())

    def test_common_carrier_is_read_outside_a_known_project(self) -> None:
        self.write_project("_common", OUTSIDE)

        result = self.run_cli("get", "issue_tracker.my_issues_query", cwd=self.root)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("assignee = currentUser()", result.stdout.strip())

    def test_project_only_key_is_invisible_outside(self) -> None:
        self.acme()
        inside = self.run_cli("get", "dispatch.key_prefix_map", "--json", cwd=self.work)
        self.assertIn("acme/services", inside.stdout)
        outside = self.run_cli("get", "dispatch.key_prefix_map", "--json", carrier=self.outside)
        self.assertEqual(1, outside.returncode)

    def test_project_declares_heartbeat_threshold(self) -> None:
        self.write_project(
            "acme",
            f'[project]\nroots = ["{self.work}"]\n\n'
            '[thresholds]\nheartbeat_seconds = 30\n',
        )
        self.assertEqual(
            "30", self.run_cli("get", "thresholds.heartbeat_seconds", cwd=self.work).stdout.strip()
        )

    def test_adapter_off_values_are_normalized(self) -> None:
        for value in ("", "none", "NONE", " none "):
            with self.subTest(value=value):
                self.assertTrue(skills_config.adapter_is_off(value))
        self.assertFalse(skills_config.adapter_is_off("jira"))

    def test_roots_prints_the_project_roots(self) -> None:
        # Первый корень читают маршрут vcs-host и запуск периметра: разбор карты
        # у каждого потребителя был бы второй копией правила.
        self.acme()
        result = self.run_cli("roots", "--project", "acme")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([str(self.work)], result.stdout.split())

    def test_relative_root_is_resolved_from_project_card(self) -> None:
        project_dir = self.write_project("local", '[project]\nroots = ["."]\n')

        result = self.run_cli("roots", "--project", "local")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([str(project_dir.resolve())], result.stdout.split())

    def test_roots_of_an_unknown_project_fails(self) -> None:
        result = self.run_cli("roots", "--project", "ghost")

        self.assertEqual(1, result.returncode)
        self.assertIn("no project card", result.stderr)

    def test_roots_without_project_uses_the_identified_one(self) -> None:
        self.acme()
        result = self.run_cli("roots", cwd=self.work)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([str(self.work)], result.stdout.split())

    def test_data_root_uses_project_runtime_store(self) -> None:
        self.acme()
        result = self.run_cli("data-root", cwd=self.work)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(self.projects / "acme" / ".data"), result.stdout.strip())

    def test_projects_root_uses_configured_carrier(self) -> None:
        result = self.run_cli("projects-root")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(self.projects), result.stdout.strip())

    def test_common_task_ids_are_global_and_monotonic(self) -> None:
        existing = self.projects / "acme" / ".data" / "tasks" / "common-7"
        existing.mkdir(parents=True)

        first = self.run_cli("allocate-common-task")
        second = self.run_cli("allocate-common-task")

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("common-8", first.stdout.strip())
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual("common-9", second.stdout.strip())
        self.assertEqual(
            "9",
            (self.projects / "_common" / ".data" / "common-task-index")
            .read_text(encoding="utf-8")
            .strip(),
        )

    def test_data_root_uses_common_runtime_store_outside_a_known_project(self) -> None:
        result = self.run_cli("data-root", cwd=self.root)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(self.projects / "_common" / ".data"), result.stdout.strip())

    def test_src_checkout_uses_parent_as_runtime_carrier(self) -> None:
        with mock.patch.object(
            skills_config, "rules_repo_root", return_value=Path("/carrier/src")
        ):
            self.assertEqual(Path("/carrier"), skills_config.runtime_carrier_root())

    def test_src_checkout_uses_carrier_projects_root(self) -> None:
        with (
            mock.patch.object(
                skills_config, "rules_repo_root", return_value=Path("/carrier/src")
            ),
            mock.patch.dict(os.environ, {"AGENTS_PROJECTS_DIR": ""}),
        ):
            self.assertEqual(Path("/carrier/projects"), skills_config.projects_root())

    # --- Секреты вне карты ---------------------------------------------------

    def test_mcp_section_in_a_carrier_map_is_an_error(self) -> None:
        # Право объявить url и http_headers превратило бы клонируемый
        # репозиторий носителя в канал утечки personal-token.
        self.write_project(
            "acme",
            f'[project]\nroots = ["{self.work}"]\n\n'
            '[mcp.jira]\nurl = "https://evil.example/mcp"\n',
        )
        result = self.run_cli("get", "issue_tracker.product", cwd=self.work)
        self.assertEqual(1, result.returncode)
        self.assertIn("mcp", result.stderr)

    def test_secrets_file_is_read_for_mcp_sections(self) -> None:
        self.acme()
        secrets = self.root / "secrets.toml"
        secrets.write_text('[mcp.jira]\nurl = "https://gateway.example/mcp"\n', encoding="utf-8")
        result = self.run_cli(
            "get", "mcp.jira.url", cwd=self.work, AGENTS_SECRETS=str(secrets)
        )
        self.assertEqual("https://gateway.example/mcp", result.stdout.strip())

    def test_validate_rejects_station_leftovers_in_the_secrets_file(self) -> None:
        # Неперенесённый станционный слой обязан быть видимым: его значения
        # рантайм уже не читает.
        self.acme()
        secrets = self.root / "secrets.toml"
        secrets.write_text('[station]\nlegacy = true\n', encoding="utf-8")
        result = self.run_cli("validate", carrier=self.outside, AGENTS_SECRETS=str(secrets))
        self.assertEqual(1, result.returncode)
        self.assertIn("only [mcp.<server>] sections belong here", result.stdout)

    def test_validate_rejects_a_scalar_under_the_mcp_root(self) -> None:
        # Скаляр под корнем `mcp` сервером не является: без проверки он
        # проходил гейт и читался как значение конфигурации.
        self.acme()
        secrets = self.root / "secrets.toml"
        secrets.write_text('[mcp]\nonly = "readable-root"\n', encoding="utf-8")

        result = self.run_cli("validate", carrier=self.outside, AGENTS_SECRETS=str(secrets))
        value = self.run_cli("get", "mcp.only", carrier=self.outside, AGENTS_SECRETS=str(secrets))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("mcp.only", result.stdout)
        self.assertEqual(1, value.returncode, value.stdout)

    def test_validate_reports_a_broken_card_of_any_project(self) -> None:
        self.acme()
        self.write_project("other", '[project]\nroots = []\n\n[mcp.jira]\nurl = "x"\n')
        result = self.run_cli("validate", carrier=self.outside)
        self.assertEqual(1, result.returncode)
        self.assertIn("fail: project 'other'", result.stdout)

    def test_unreadable_card_does_not_break_work_elsewhere(self) -> None:
        # Опознание читает карты всех проектов: битая карта соседа роняла бы
        # любой вызов конфига, включая работу вне проектов.
        self.acme()
        self.write_project("broken", "roots = [\n")
        result = self.run_cli("get", "issue_tracker.my_issues_query", cwd=self.work)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("filter = 42", result.stdout.strip())
        self.assertIn("warn: project 'broken'", result.stderr)

    def test_validate_passes_with_a_clean_card(self) -> None:
        self.write_project(
            "acme",
            f'[project]\nroots = ["{self.work}"]\n\n' + OUTSIDE,
        )
        result = self.run_cli("validate", cwd=self.work)
        self.assertEqual(0, result.returncode, result.stdout)

    # --- Резолв профилей -----------------------------------------------------

    def write_profile_rules(self, rules: str) -> Path:
        project = self.write_project(
            "acme", f'[project]\nroots = ["{self.work}"]\n\n{rules}'
        )
        (project / "acme-services.md").write_text("Проза проекта\n", encoding="utf-8")
        return project

    def test_profiles_resolves_project_document_and_repository_profile(self) -> None:
        """Один резолв на всех потребителей: оркестратор и манифест участника
        получают одни и те же абсолютные пути (SB-179)."""
        project = self.write_profile_rules(
            "[[profile_rules]]\n"
            f'match = "path:{self.work}"\n'
            'profiles = ["./acme-services.md", "profiles/go.md"]\n'
        )

        result = self.run_cli("profiles", "--path", str(self.work))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [
                str((project / "acme-services.md").resolve()),
                str(ROOT / "profiles" / "go.md"),
            ],
            result.stdout.split(),
        )

    def test_profiles_skips_a_rule_whose_fragment_is_elsewhere(self) -> None:
        self.write_profile_rules(
            "[[profile_rules]]\n"
            'match = "path:/nowhere/else/"\n'
            'profiles = ["profiles/go.md"]\n'
        )

        result = self.run_cli("profiles", "--path", str(self.work))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_profiles_names_a_rule_with_an_unknown_match(self) -> None:
        """Незнакомый вид `match` — не тихое применение и не тихий пропуск."""
        self.write_profile_rules(
            "[[profile_rules]]\n"
            'match = "repo:acme"\n'
            'profiles = ["profiles/go.md"]\n'
        )

        result = self.run_cli("profiles", "--path", str(self.work))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())
        self.assertIn("is not 'path:<fragment>'", result.stderr)

    def test_profiles_is_empty_outside_any_project(self) -> None:
        self.acme()

        result = self.run_cli("profiles", "--path", str(self.root))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())


class ServerConfigDiagnosticsTest(unittest.TestCase):
    """Отсутствующая секция `[mcp.*]` называет периметр: секреты принадлежат
    носителю, и вне его периметра секции нет (SB-107)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.projects = self.root / "projects"
        (self.projects / "acme").mkdir(parents=True)
        (self.projects / "acme" / "project.toml").write_text(
            f'[project]\nroots = ["{self.root / "work"}"]\n', encoding="utf-8"
        )
        self.config = self.root / "config.toml"
        self.config.write_text('[mcp.other]\nurl = "https://gateway.example/mcp"\n', encoding="utf-8")
        sys.path.insert(0, str(ROOT / "lib"))
        os.environ["AGENTS_PROJECTS_DIR"] = str(self.projects)
        os.environ["AGENTS_SECRETS"] = str(self.config)

    def tearDown(self) -> None:
        for name in ("AGENTS_PROJECTS_DIR", "AGENTS_SECRETS", "AGENTS_PROJECT"):
            os.environ.pop(name, None)
        self.temp_dir.cleanup()

    def message(self, server: str) -> str:
        from mcp_http import MCPError, load_server_config

        with self.assertRaises(MCPError) as caught:
            load_server_config(server)
        return str(caught.exception)

    def test_missing_section_outside_a_project_points_at_the_perimeter(self) -> None:
        message = self.message("jira")

        self.assertIn("perimeter: _common", message)
        self.assertIn("AGENTS_PROJECT_PATH", message)

    def test_missing_section_names_the_identified_project(self) -> None:
        os.environ["AGENTS_PROJECT"] = "acme"

        message = self.message("jira")

        self.assertIn("perimeter: project acme (by override)", message)
        self.assertNotIn("AGENTS_PROJECT_PATH", message)


if __name__ == "__main__":
    unittest.main()
