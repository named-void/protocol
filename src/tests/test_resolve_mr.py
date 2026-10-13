import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "skills" / "protocol" / "scripts" / "resolve_mr.py"
SPEC = importlib.util.spec_from_file_location("resolve_mr", SCRIPT)
assert SPEC and SPEC.loader
resolve_mr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolve_mr)


class ResolveMrTest(unittest.TestCase):
    def test_parses_gitlab_mr_url(self) -> None:
        self.assertEqual(
            ("gitlab.example.com", "group/services/catalog", 42),
            resolve_mr.parse_url(
                "https://gitlab.example.com/group/services/catalog/-/merge_requests/42"
            ),
        )

    def test_rejects_non_mr_url(self) -> None:
        with self.assertRaises(resolve_mr.ResolveError):
            resolve_mr.parse_url("https://gitlab.example.com/group/project")

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(resolve_mr.ResolveError):
            resolve_mr.parse_url(
                "https://gitlab.example.com/group/%2E%2E/project/-/merge_requests/1"
            )

    def test_resolves_project_and_local_checkout_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "services" / "catalog"
            checkout.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "remote",
                    "add",
                    "origin",
                    "git@gitlab.example.com:getblogger/upl/services/catalog.git",
                ],
                check=True,
            )
            with mock.patch.object(
                resolve_mr.skills_config,
                "resolve_vcs_project",
                return_value={
                    "name": "upl",
                    "namespace": "getblogger/upl",
                    "local_path": checkout,
                },
            ):
                result = resolve_mr.resolve(
                    "https://gitlab.example.com/getblogger/upl/services/catalog/-/merge_requests/7"
                )

            self.assertEqual("upl", result["profile"])
            self.assertEqual(str(checkout.resolve()), result["local_path"])
            self.assertTrue(result["local_exists"])
            self.assertTrue(result["checkout_matches"])

    def test_rejects_checkout_with_different_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "remote",
                    "add",
                    "origin",
                    "git@gitlab.example.com:other/project.git",
                ],
                check=True,
            )
            self.assertFalse(
                resolve_mr.origin_matches(
                    checkout, "gitlab.example.com", "group/project"
                )
            )


if __name__ == "__main__":
    unittest.main()
