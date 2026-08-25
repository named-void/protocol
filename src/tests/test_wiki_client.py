from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch


class MCPHandler(BaseHTTPRequestHandler):
    methods: ClassVar[list[str]] = []
    tool_calls: ClassVar[list[dict]] = []
    tool_result_text = "{}"
    # Копия результата в structuredContent — часть ответа реального сервера
    # (SB-212): без неё тест не видит второго носителя тела страницы.
    tool_result_structured = None
    # REST-канал статусов резолюции (SB-217): пути запросов и порции ответа —
    # список списков, по одной порции на запрос.
    get_paths: ClassVar[list[str]] = []
    rest_pages: ClassVar[list[list[dict[str, object]]]] = [[]]
    # Цепочка предков страницы: тот же REST-канал отдаёт её объектом, а не
    # списком `results`.
    rest_ancestors: ClassVar[list[dict[str, object]]] = []

    def log_message(self, *_: object) -> None:
        pass

    def _write_json(self, payload: dict, *, session: bool = False) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", "test-session")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).methods.append(payload["method"])

        if payload["method"] == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if payload["method"] == "initialize":
            self._write_json(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "test", "version": "1"},
                    },
                },
                session=True,
            )
            return
        if payload["method"] == "tools/list":
            result = {"tools": [{"name": "confluence_search", "inputSchema": {}}]}
        else:
            type(self).tool_calls.append(payload["params"])
            result = {
                "content": [{"type": "text", "text": type(self).tool_result_text}],
                "isError": False,
            }
            if type(self).tool_result_structured is not None:
                result["structuredContent"] = {
                    "result": type(self).tool_result_structured
                }
        self._write_json(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )

    def do_GET(self) -> None:
        index = len(type(self).get_paths)
        type(self).get_paths.append(self.path)
        if "expand=ancestors" in self.path:
            self._write_json({"ancestors": type(self).rest_ancestors})
            return
        pages = type(self).rest_pages
        results = pages[index] if index < len(pages) else []
        self._write_json({"results": results, "size": len(results)})

    def do_DELETE(self) -> None:
        self.send_response(204)
        self.end_headers()


class WikiClientTest(unittest.TestCase):
    def setUp(self) -> None:
        MCPHandler.methods = []
        MCPHandler.tool_calls = []
        MCPHandler.tool_result_text = "{}"
        MCPHandler.tool_result_structured = None
        MCPHandler.get_paths = []
        MCPHandler.rest_pages = [[]]
        MCPHandler.rest_ancestors = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        # Доступ к серверу и настройки адаптера — один конфиг
        # (orchestration/adr/README.md#mcp-credentials).
        self.agents_config = Path(self.temp_dir.name) / "project.toml"
        self.secrets = Path(self.temp_dir.name) / "secrets.toml"
        self.write_config(
            "\n".join(
                [
                    "[docs_wiki]",
                    'product = "confluence"',
                    'spaces = "UPL"',
                    "",
                    "[mcp.confluence]",
                    f'url = "http://127.0.0.1:{self.server.server_port}/mcp"',
                    "",
                    "[mcp.confluence.http_headers]",
                    f'X-Atlassian-Confluence-Url = "http://127.0.0.1:{self.server.server_port}"',
                    'X-Atlassian-Confluence-Personal-Token = "pat-secret"',
                    "",
                ]
            )
        )
        self._env_patch = patch.dict(
            os.environ,
            {"AGENTS_CONFIG": str(self.agents_config), "AGENTS_SECRETS": str(self.secrets)},
        )
        self._env_patch.start()


    def write_config(self, text: str) -> None:
        """Фикстура пишет один TOML, рантайм читает два файла: карту носителя и
        секреты `[mcp.*]` (orchestration/adr/README.md#carrier-config). Секции
        `[mcp.*]` без изменений оставляют прежний файл секретов."""
        carrier: list[str] = []
        secrets: list[str] = []
        target = carrier
        for line in text.splitlines():
            if line.startswith("["):
                target = secrets if line.startswith("[mcp") else carrier
            target.append(line)
        self.agents_config.write_text("\n".join(carrier) + "\n")
        if secrets:
            self.secrets.write_text("\n".join(secrets) + "\n")

    def tearDown(self) -> None:
        self._env_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp_dir.cleanup()

    def _wiki_script(self) -> Path:
        return (
            Path(__file__).parents[1]
            / "skills"
            / "adapter"
            / "wiki"
            / "scripts"
            / "wiki.py"
        )

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self._wiki_script()), *args],
            env={**os.environ},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_page_by_id_fetches_metadata_without_listing_schema(self) -> None:
        result = self._run("page", "123456", "--body", "none")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("tools/list", MCPHandler.methods)
        self.assertEqual(
            MCPHandler.tool_calls,
            [
                {
                    "name": "confluence_get_page",
                    "arguments": {
                        "page_id": "123456",
                        "include_metadata": True,
                        "convert_to_markdown": False,
                    },
                }
            ],
        )

    def test_page_body_none_strips_body_locally(self) -> None:
        MCPHandler.tool_result_text = json.dumps(
            {
                "id": "123456",
                "version": {"number": 7},
                "content": {"value": "SECRET-BODY"},
            }
        )
        result = self._run("page", "123456", "--body", "none")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SECRET-BODY", result.stdout)
        self.assertIn("version", result.stdout)

    def test_page_body_none_strips_the_body_of_the_real_payload(self) -> None:
        # Реальный ответ get_page с include_metadata: тело — в
        # metadata.content.value, копия payload — в structuredContent.result
        # (SB-212, страница 26774980: 52066 байт вывода против 52169 у storage).
        text = json.dumps(
            {
                "metadata": {
                    "id": "123456",
                    "version": 7,
                    "content": {"value": "SECRET-BODY", "format": "storage"},
                }
            }
        )
        MCPHandler.tool_result_text = text
        MCPHandler.tool_result_structured = text

        result = self._run("page", "123456", "--body", "none")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SECRET-BODY", result.stdout)
        self.assertIn("version", result.stdout)

    def test_page_body_none_strips_the_body_of_an_object_copy(self) -> None:
        # Схема допускает `structuredContent.result` объектом, а не строкой:
        # ветвь только для строки оставляла тело в выводе.
        payload = {
            "metadata": {"id": "123456", "content": {"value": "SECRET-BODY"}}
        }
        MCPHandler.tool_result_text = json.dumps(payload)
        MCPHandler.tool_result_structured = payload

        result = self._run("page", "123456", "--body", "none")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SECRET-BODY", result.stdout)

    def test_page_body_storage_keeps_the_body(self) -> None:
        # Обратный класс к правке SB-212: снятие тела касается только --body none.
        text = json.dumps(
            {"metadata": {"id": "123456", "content": {"value": "PAGE-BODY"}}}
        )
        MCPHandler.tool_result_text = text
        MCPHandler.tool_result_structured = text

        result = self._run("page", "123456", "--body", "storage")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(2, result.stdout.count("PAGE-BODY"), result.stdout)

    def test_page_body_markdown_requests_conversion(self) -> None:
        result = self._run("page", "123456", "--body", "markdown")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            MCPHandler.tool_calls[0]["arguments"]["convert_to_markdown"], True
        )

    def test_page_rejects_removed_view_body_format(self) -> None:
        result = self._run("page", "123456", "--body", "view")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(MCPHandler.tool_calls, [])

    def test_page_id_is_extracted_from_urls(self) -> None:
        for url in (
            "https://wiki.example.com/pages/viewpage.action?pageId=777",
            "https://wiki.example.com/spaces/UPL/pages/777/Some+Title",
        ):
            with self.subTest(url=url):
                MCPHandler.tool_calls = []
                result = self._run("page", url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    MCPHandler.tool_calls[0]["arguments"]["page_id"], "777"
                )

    def test_page_id_is_decoded_from_tiny_link(self) -> None:
        # tiny-ссылка Confluence /x/<id>: id — URL-safe base64 от pageId как
        # little-endian long; MAtG → 4590384 (см. SB-64).
        for reference in (
            "https://wiki.example.com/x/MAtG",
            "/x/MAtG",
        ):
            with self.subTest(reference=reference):
                MCPHandler.tool_calls = []
                result = self._run("page", reference)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    MCPHandler.tool_calls[0]["arguments"]["page_id"], "4590384"
                )

    def test_page_rejects_reference_without_id(self) -> None:
        result = self._run("page", "https://wiki.example.com/display/UPL/Title")

        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot extract page id", result.stderr)
        self.assertEqual(MCPHandler.tool_calls, [])

    def test_disabled_adapter_makes_no_network_call(self) -> None:
        # Один shortcut подтверждает общий сетевой гейт; варианты product проверяются отдельно как pure-функция.
        self.write_config(
            "\n".join(
                [
                    "[docs_wiki]",
                    'product = "none"',
                    'spaces = "UPL"',
                    "",
                    "[mcp.confluence]",
                    f'url = "http://127.0.0.1:{self.server.server_port}/mcp"',
                    "",
                ]
            )
        )
        result = self._run("page", "123456")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("docs_wiki.product", result.stderr)
        self.assertEqual(MCPHandler.methods, [])

    def test_search_uses_configured_spaces_filter(self) -> None:
        result = self._run("search", "Epic Name")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("tools/list", MCPHandler.methods)
        self.assertEqual(
            MCPHandler.tool_calls,
            [
                {
                    "name": "confluence_search",
                    "arguments": {
                        "query": "Epic Name",
                        "limit": 10,
                        "spaces_filter": "UPL",
                    },
                }
            ],
        )

    def test_comments_shortcut_calls_tool(self) -> None:
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            MCPHandler.tool_calls,
            [{"name": "confluence_get_comments", "arguments": {"page_id": "123456"}}],
        )

    @staticmethod
    def _rest_comment(comment_id: str, status: str, when: str = "") -> dict:
        return {
            "id": comment_id,
            "extensions": {"resolution": {"status": status}},
            "version": {"when": when},
        }

    def test_comments_summary_counts_and_max_id_without_dates(self) -> None:
        # SB-23: сервер отдаёт created/updated пустыми — клиент считает
        # механический маркер свежести (open_count + max id по активным) сам.
        MCPHandler.tool_result_text = json.dumps(
            [
                {"id": "26773116", "created": "", "updated": "", "body": "a"},
                {"id": "26775930", "created": "", "updated": "", "body": "b"},
                {"id": "26773957", "created": "", "updated": "", "body": "c"},
            ]
        )
        MCPHandler.rest_pages = [
            [
                self._rest_comment("26773116", "open"),
                self._rest_comment("26775930", "open"),
                self._rest_comment("26773957", "open"),
            ]
        ]
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual(
            {
                "count": 3,
                "open_count": 3,
                "closed_count": 0,
                "max_id": 26775930,
                "latest_date": "",
                "resolution_source": "rest",
            },
            payload["summary"],
        )
        self.assertEqual(3, len(payload["comments"]))

    def test_comments_summary_keeps_server_dates_when_present(self) -> None:
        MCPHandler.tool_result_text = json.dumps(
            [
                {"id": "1", "created": "2026-07-01", "updated": ""},
                {"id": "2", "created": "2026-07-02", "updated": "2026-07-15"},
            ]
        )
        MCPHandler.rest_pages = [
            [self._rest_comment("1", "open"), self._rest_comment("2", "open")]
        ]
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual("2026-07-15", payload["summary"]["latest_date"])

    def test_comments_hide_closed_discussions_in_both_payload_copies(self) -> None:
        # SB-217: разрешённое обсуждение неотличимо от открытого и уходит в
        # вопросы автору. Вторая копия payload — тот же класс, что SB-212.
        text = json.dumps(
            [
                {"id": "10", "body": "OPEN-BODY"},
                {"id": "11", "body": "RESOLVED-BODY"},
                {"id": "12", "body": "DANGLING-BODY"},
            ]
        )
        MCPHandler.tool_result_text = text
        MCPHandler.tool_result_structured = text
        MCPHandler.rest_pages = [
            [
                self._rest_comment("10", "open", "2026-08-01T10:00:00.000Z"),
                self._rest_comment("11", "resolved", "2026-08-02T10:00:00.000Z"),
                self._rest_comment("12", "dangling", "2026-08-03T10:00:00.000Z"),
            ]
        ]
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("RESOLVED-BODY", result.stdout)
        self.assertNotIn("DANGLING-BODY", result.stdout)
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual(
            {
                "count": 3,
                "open_count": 1,
                "closed_count": 2,
                "max_id": 10,
                "latest_date": "2026-08-01T10:00:00.000Z",
                "resolution_source": "rest",
            },
            payload["summary"],
        )
        self.assertEqual(["10"], [c["id"] for c in payload["comments"]])
        self.assertEqual("open", payload["comments"][0]["resolution"])
        copy = json.loads(json.loads(result.stdout)["structuredContent"]["result"])
        self.assertEqual(["10"], [c["id"] for c in copy["comments"]])

    def test_comments_marker_without_any_dates_counts_active_only(self) -> None:
        # Класс, в котором маркер свежести виден только счётчиками: датами не
        # располагает ни MCP-сервер, ни REST-канал. Закрытый комментарий с
        # наибольшим id не смещает ни max_id, ни счётчик активных.
        MCPHandler.tool_result_text = json.dumps(
            [
                {"id": "700", "created": "", "updated": "", "body": "OPEN-BODY"},
                {"id": "900", "created": "", "updated": "", "body": "RESOLVED-BODY"},
            ]
        )
        MCPHandler.rest_pages = [
            [self._rest_comment("700", "open"), self._rest_comment("900", "resolved")]
        ]
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("RESOLVED-BODY", result.stdout)
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual(
            {
                "count": 2,
                "open_count": 1,
                "closed_count": 1,
                "max_id": 700,
                "latest_date": "",
                "resolution_source": "rest",
            },
            payload["summary"],
        )

    def test_comments_keep_entries_whose_status_rest_did_not_return(self) -> None:
        # Комментарий, которого нет в REST-ответе или у которого нет резолюции
        # (footer-комментарий), считается активным и из вывода не исчезает.
        MCPHandler.tool_result_text = json.dumps(
            [{"id": "10", "body": "a"}, {"id": "11", "body": "b"}]
        )
        MCPHandler.rest_pages = [[{"id": "11", "extensions": {}, "version": {}}]]
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual(["10", "11"], [c["id"] for c in payload["comments"]])
        self.assertEqual(["open", "open"], [c["resolution"] for c in payload["comments"]])

    def test_comments_survive_malformed_rest_base_url(self) -> None:
        # Битое значение заголовка даёт ValueError внутри urllib, а не отказ
        # сети: без нормализации в MCPError команда падала traceback'ом вместо
        # вывода со статусами unknown.
        self.write_config(
            "\n".join(
                [
                    "[docs_wiki]",
                    'product = "confluence"',
                    "",
                    "[mcp.confluence]",
                    f'url = "http://127.0.0.1:{self.server.server_port}/mcp"',
                    "",
                    "[mcp.confluence.http_headers]",
                    'X-Atlassian-Confluence-Url = "not-a-url"',
                    'X-Atlassian-Confluence-Personal-Token = "pat-secret"',
                    "",
                ]
            )
        )
        MCPHandler.tool_result_text = json.dumps([{"id": "10", "body": "a"}])
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual("unavailable", payload["summary"]["resolution_source"])
        self.assertEqual("unknown", payload["comments"][0]["resolution"])
        self.assertNotIn("pat-secret", result.stdout)

    def test_comments_include_resolved_keeps_all_with_status(self) -> None:
        MCPHandler.tool_result_text = json.dumps(
            [{"id": "10", "body": "a"}, {"id": "11", "body": "b"}]
        )
        MCPHandler.rest_pages = [
            [
                self._rest_comment("10", "open"),
                self._rest_comment("11", "resolved"),
            ]
        ]
        result = self._run("comments", "123456", "--include-resolved")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual(
            ["open", "resolved"], [c["resolution"] for c in payload["comments"]]
        )
        self.assertEqual(1, payload["summary"]["open_count"])

    def test_comments_read_statuses_with_replies_and_pagination(self) -> None:
        # `depth=all` обязателен: без него REST отдаёт только корни тредов, и
        # ответы остаются без статуса. Хвост списка забирается пагинацией.
        MCPHandler.tool_result_text = json.dumps(
            [{"id": str(number), "body": "x"} for number in range(1, 102)]
        )
        MCPHandler.rest_pages = [
            [self._rest_comment(str(number), "open") for number in range(1, 101)],
            [self._rest_comment("101", "resolved")],
        ]
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(2, len(MCPHandler.get_paths), MCPHandler.get_paths)
        for path in MCPHandler.get_paths:
            self.assertIn("/rest/api/content/123456/child/comment", path)
            self.assertIn("depth=all", path)
            self.assertIn("expand=extensions.resolution,version", path)
        self.assertIn("start=0", MCPHandler.get_paths[0])
        self.assertIn("start=100", MCPHandler.get_paths[1])
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual(100, payload["summary"]["open_count"])
        self.assertEqual(1, payload["summary"]["closed_count"])

    def test_comments_without_rest_headers_marks_status_unknown(self) -> None:
        # Отказ канала статусов не превращает команду в отказ, но и не выдаёт
        # неизвестный статус за открытый: фильтр не применяется, причина — в
        # сводке.
        self.write_config(
            "\n".join(
                [
                    "[docs_wiki]",
                    'product = "confluence"',
                    "",
                    "[mcp.confluence]",
                    f'url = "http://127.0.0.1:{self.server.server_port}/mcp"',
                    "",
                ]
            )
        )
        MCPHandler.tool_result_text = json.dumps([{"id": "10", "body": "a"}])
        result = self._run("comments", "123456")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([], MCPHandler.get_paths)
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual("unavailable", payload["summary"]["resolution_source"])
        self.assertIn(
            "X-Atlassian-Confluence-Url", payload["summary"]["resolution_error"]
        )
        self.assertEqual(1, payload["summary"]["open_count"])
        self.assertEqual("unknown", payload["comments"][0]["resolution"])

    def test_ancestors_report_the_excluded_root_of_a_page(self) -> None:
        # UPL-938: страницы поддерева черновиков читались как спецификация —
        # заголовком они от неё не отличаются, признаком служит только цепочка
        # предков.
        self.write_config(
            "\n".join(
                [
                    "[docs_wiki]",
                    'product = "confluence"',
                    'spec_exclude = ["https://wiki/spaces/IT/pages/19956263/Drafts"]',
                    "",
                    "[mcp.confluence]",
                    f'url = "http://127.0.0.1:{self.server.server_port}/mcp"',
                    "",
                    "[mcp.confluence.http_headers]",
                    f'X-Atlassian-Confluence-Url = "http://127.0.0.1:{self.server.server_port}"',
                    'X-Atlassian-Confluence-Personal-Token = "pat-secret"',
                    "",
                ]
            )
        )
        MCPHandler.rest_ancestors = [
            {"id": "360501", "title": "Root"},
            {"id": "19956263", "title": "Drafts"},
            {"id": "26778615", "title": "Service rules"},
        ]
        excluded = self._run("ancestors", "26778620")

        self.assertEqual(excluded.returncode, 0, excluded.stderr)
        payload = json.loads(excluded.stdout)
        self.assertEqual("19956263", payload["excluded_by"])
        self.assertEqual(
            ["360501", "19956263", "26778615"],
            [item["id"] for item in payload["ancestors"]],
        )

        MCPHandler.rest_ancestors = [{"id": "360501", "title": "Root"}]
        allowed = self._run("ancestors", "360505")

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIsNone(json.loads(allowed.stdout)["excluded_by"])

    def test_call_rejects_foreign_tool_prefix(self) -> None:
        result = self._run("call", "jira_search", "--arguments", "{}")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(MCPHandler.tool_calls, [])

if __name__ == "__main__":
    unittest.main()
