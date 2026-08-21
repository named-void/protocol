import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "skills" / "mreviewer" / "scripts" / "resolve_mr.py"
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

    def test_resolves_local_checkout_and_rules_offline(self) -> None:
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
            rules = root / "upl.md"
            rules.write_text("# rules\n", encoding="utf-8")
            review_rules = root / "upl-review.md"
            review_rules.write_text("# review rules\n", encoding="utf-8")
            with (
                mock.patch.object(
                    resolve_mr.skills_config,
                    "resolve_vcs_project",
                    return_value={
                        "name": "upl",
                        "namespace": "getblogger/upl",
                        "local_path": checkout,
                    },
                ),
                mock.patch.object(
                    resolve_mr.skills_config,
                    "profiles_for_path",
                    return_value=[rules],
                ),
                mock.patch.object(
                    resolve_mr.skills_config,
                    "mreviewer_rules",
                    return_value=[review_rules],
                ),
            ):
                result = resolve_mr.resolve(
                    "https://gitlab.example.com/getblogger/upl/services/catalog/-/merge_requests/7"
                )

            self.assertEqual(str(checkout.resolve()), result["local_path"])
            self.assertTrue(result["local_exists"])
            self.assertTrue(result["checkout_matches"])
            self.assertEqual(
                [str(rules.resolve()), str(review_rules.resolve())],
                result["rules_paths"],
            )
            self.assertTrue(result["rules_exist"])

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

    def test_allows_project_without_extra_rules(self) -> None:
        with (
            mock.patch.object(
                resolve_mr.skills_config,
                "resolve_vcs_project",
                return_value={
                    "name": "plain",
                    "namespace": "group",
                    "local_path": Path("/missing/checkout"),
                },
            ),
            mock.patch.object(
                resolve_mr.skills_config, "profiles_for_path", return_value=[]
            ),
            mock.patch.object(
                resolve_mr.skills_config, "mreviewer_rules", return_value=[]
            ),
        ):
            result = resolve_mr.resolve(
                "https://gitlab.example.com/group/project/-/merge_requests/7"
            )

        self.assertEqual([], result["rules_paths"])
        self.assertTrue(result["rules_exist"])


if __name__ == "__main__":
    unittest.main()
