from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CHECK_MCP = ROOT / "scripts" / "check-mcp.py"


class CheckMcpDisabledTest(unittest.TestCase):
    """Оба адаптера выключаются `none`, пустым значением или отсутствием ключа.

    Имя сервера — продукт адаптера, а секции `[mcp.<продукт>]` в фикстуре нет:
    у включённого адаптера это `fail:` и код 1, поэтому skip нельзя получить
    обходным путём.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.carrier = Path(self.temp_dir.name) / "project.toml"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_check(self, config_text: str) -> subprocess.CompletedProcess[str]:
        self.carrier.write_text(config_text, encoding="utf-8")
        environment = dict(os.environ)
        environment["AGENTS_CONFIG"] = str(self.carrier)
        environment["AGENTS_SECRETS"] = str(Path(self.temp_dir.name) / "secrets.toml")
        environment["AGENTS_PROJECTS_DIR"] = str(Path(self.temp_dir.name) / "projects")
        return subprocess.run(
            [sys.executable, str(CHECK_MCP)],
            cwd=str(ROOT),
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_wiki_disabled_values_skip(self) -> None:
        cases = (
            ("none", '[docs_wiki]\nproduct = "none"\n'),
            ("empty", '[docs_wiki]\nproduct = ""\n'),
            ("absent", ""),
        )
        for name, wiki_config in cases:
            with self.subTest(name=name):
                result = self.run_check('[issue_tracker]\nproduct = "none"\n' + wiki_config)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("skip: docs wiki disabled", result.stdout)

    def test_both_adapters_skip_on_case_and_padding_variants(self) -> None:
        # Клиенты адаптеров нормализуют значение (`strip().lower()`), поэтому
        # `NONE` и ` none ` выключают их; без той же нормализации здесь тот же
        # конфиг открывал сетевую проверку выключенного адаптера (SB-210).
        for product in ('"NONE"', '" none "'):
            with self.subTest(product=product):
                result = self.run_check(
                    f"[issue_tracker]\nproduct = {product}\n"
                    f"[docs_wiki]\nproduct = {product}\n"
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("skip: issue tracker disabled", result.stdout)
                self.assertIn("skip: docs wiki disabled", result.stdout)

    def test_wiki_other_product_is_checked(self) -> None:
        result = self.run_check(
            '[issue_tracker]\nproduct = "none"\n'
            '[docs_wiki]\nproduct = "confluence"\n'
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("skip: docs wiki disabled", result.stdout)
        self.assertIn("fail: docs-wiki mcp server 'confluence'", result.stdout)


if __name__ == "__main__":
    unittest.main()
