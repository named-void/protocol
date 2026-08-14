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
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))

from mcp_http import MCPError, StreamableHTTPClient, load_server_config


# Разные версии Jira отдают поля Greenhopper toString() в разном порядке;
# оба варианта должны нормализоваться одинаково.
SPRINT_RAW_STATE_BEFORE_NAME = (
    "com.atlassian.greenhopper.service.sprint.Sprint@6f8[id=41,rapidViewId=7,"
    "state=ACTIVE,name=UPL Sprint 12,startDate=2026-06-01T10:00:00.000+03:00]"
)
SPRINT_RAW_NAME_BEFORE_STATE = (
    "com.atlassian.greenhopper.service.sprint.Sprint@6f9[id=40,"
    "name=UPL Sprint 11,goal=,state=CLOSED]"
)


class MCPHandler(BaseHTTPRequestHandler):
    deleted = False
    methods = []
    received_header = None
    tool_calls = []
    get_paths = []
    get_authorization = None
    issue_extra_fields = {}

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
        type(self).received_header = self.headers.get("X-Test-Token")

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
            result = {"tools": [{"name": "jira_search", "inputSchema": {}}]}
        elif payload["params"]["name"] == "jira_fail":
            result = {
                "content": [{"type": "text", "text": "expected failure"}],
                "isError": True,
            }
        elif payload["params"]["name"] == "jira_get_issue":
            type(self).tool_calls.append(payload["params"])
            issue = {
                "key": payload["params"]["arguments"]["issue_key"],
                "customfield_10109": {
                    "value": [SPRINT_RAW_NAME_BEFORE_STATE]
                },
            }
            issue.update(type(self).issue_extra_fields)
            result = {
                "content": [{"type": "text", "text": json.dumps(issue)}],
                "isError": False,
            }
        elif payload["params"]["name"] == "jira_search":
            type(self).tool_calls.append(payload["params"])
            issues = {
                "total": 1,
                "issues": [
                    {
                        "key": "UPL-1",
                        "customfield_10109": {
                            "value": [SPRINT_RAW_STATE_BEFORE_NAME]
                        },
                    }
                ],
            }
            result = {
                "content": [{"type": "text", "text": json.dumps(issues)}],
                "isError": False,
            }
        else:
            type(self).tool_calls.append(payload["params"])
            result = {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False,
            }
        self._write_json(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )

    def do_GET(self) -> None:
        type(self).get_paths.append(self.path)
        type(self).get_authorization = self.headers.get("Authorization")
        payload = [
            {
                "application": {"type": "com.atlassian.confluence", "name": "wiki"},
                "relationship": "mentioned in",
                "object": {
                    "url": "https://wiki.example/pages/viewpage.action?pageId=26774519",
                    "title": "Wiki Page",
                },
            },
            {
                "application": {"type": "com.atlassian.confluence", "name": "wiki"},
                "object": {
                    "url": "https://wiki.example/spaces/IT/pages/19956161/Spravochniki",
                    "title": "Spravochniki",
                },
            },
        ]
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        type(self).deleted = True
        self.send_response(204)
        self.end_headers()


class TrackerClientTest(unittest.TestCase):
    def setUp(self) -> None:
        MCPHandler.deleted = False
        MCPHandler.methods = []
        MCPHandler.received_header = None
        MCPHandler.tool_calls = []
        MCPHandler.get_paths = []
        MCPHandler.get_authorization = None
        MCPHandler.issue_extra_fields = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        # Доступ к серверу и настройки адаптера — один конфиг
        # (orchestration/adr/README.md#mcp-credentials). Значения установки
        # (сохранённый фильтр, id поля Sprint) задаются здесь: дефолтов у них
        # нет — это специфика проекта, а не адаптера.
        self.agents_config = Path(self.temp_dir.name) / "project.toml"
        self.secrets = Path(self.temp_dir.name) / "secrets.toml"
        self.write_config(
            "\n".join(
                [
                    "[issue_tracker]",
                    'product = "jira"',
                    'my_issues_query = "filter = 10504"',
                    "",
                    "[issue_tracker.fields]",
                    'sprint = "customfield_10109"',
                    "",
                    "[mcp.jira]",
                    f'url = "http://127.0.0.1:{self.server.server_port}/mcp"',
                    "",
                    "[mcp.jira.http_headers]",
                    'X-Test-Token = "secret"',
                    f'X-Atlassian-Jira-Url = "http://127.0.0.1:{self.server.server_port}"',
                    'X-Atlassian-Jira-Personal-Token = "pat-secret"',
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

    def _tracker_script(self) -> Path:
        return (
            Path(__file__).parents[1]
            / "skills"
            / "adapter"
            / "issues"
            / "scripts"
            / "tracker.py"
        )

    def _environment(self) -> dict[str, str]:
        return {**os.environ}

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self._tracker_script()), *args],
            env=self._environment(),
            text=True,
            capture_output=True,
            check=False,
        )

    def _set_config_issue_fields(self, fields: str) -> None:
        """Задать набор полей задачи в конфиге: путь проектной карты, а не дефолта."""
        self.write_config(
            self.agents_config.read_text().replace(
                "[issue_tracker]\n",
                f'[issue_tracker]\ndefault_fields = "{fields}"\n',
                1,
            )
        )

    def test_disabled_adapter_makes_no_network_call(self) -> None:
        # Режим `issue_tracker.product = "none"` требует не делать сетевых
        # вызовов; клиента зовут и вне маршрута, поэтому гейт живёт в нём
        # (SB-210). Отсутствие ключа выключает адаптер так же — как в гейте
        # конфигурации `scripts/check-mcp.py`.
        # Подкоманды перечислены все: `remote-links` идёт не через MCP, а прямым
        # REST GET, и признак его вызова — `get_paths`, а не `methods`.
        commands = (
            ("issue", "UPL-490"),
            ("search", "project = UPL"),
            ("mylist",),
            ("fields", "sprint"),
            ("tools",),
            ("call", "jira_search", "--arguments", "{}"),
            ("remote-links", "UPL-677"),
        )
        base = self.agents_config.read_text()
        for product in ('product = "none"\n', 'product = "NONE"\n', 'product = " none "\n', 'product = ""\n', ""):
            self.write_config(
                base.replace('product = "jira"\n', product, 1)
            )
            for command in commands:
                with self.subTest(product=product, command=command[0]):
                    MCPHandler.methods = []
                    MCPHandler.get_paths = []

                    result = self._run(*command)

                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn("issue_tracker.product", result.stderr)
                    self.assertEqual(MCPHandler.methods, [])
                    self.assertEqual(MCPHandler.get_paths, [])

    def test_disabled_config_can_be_called_on_demand_and_session_is_closed(self) -> None:
        config = load_server_config("jira")
        with StreamableHTTPClient(config) as client:
            self.assertEqual(
                [tool["name"] for tool in client.list_tools()], ["jira_search"]
            )
            self.assertFalse(client.call_tool("jira_search", {})["isError"])

        self.assertEqual(MCPHandler.received_header, "secret")
        self.assertTrue(MCPHandler.deleted)

    def test_tool_error_is_not_returned_as_success(self) -> None:
        config = load_server_config("jira")
        with StreamableHTTPClient(config) as client:
            with self.assertRaisesRegex(MCPError, "expected failure"):
                client.call_tool("jira_fail", {})

    def test_issue_shortcut_calls_tool_without_listing_schema(self) -> None:
        result = self._run("issue", "UPL-490")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("tools/list", MCPHandler.methods)
        self.assertEqual(
            MCPHandler.tool_calls,
            [
                {
                    "name": "jira_get_issue",
                    "arguments": {
                        "issue_key": "UPL-490",
                        "fields": (
                            "status,labels,issuetype,description,updated,summary,"
                            "reporter,created,assignee,priority,customfield_10109,"
                            "components,issuelinks,parent,comment"
                        ),
                        "comment_limit": 20,
                        "update_history": False,
                    },
                }
            ],
        )

    def test_issue_shortcut_can_disable_comments(self) -> None:
        result = self._run("issue", "UPL-490", "--comments", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(MCPHandler.tool_calls[0]["arguments"]["comment_limit"], 0)

    def test_issue_shortcut_requests_comment_field_when_config_omits_it(self) -> None:
        self._set_config_issue_fields("status,summary,customfield_10109")

        result = self._run("issue", "UPL-490")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            MCPHandler.tool_calls[0]["arguments"]["fields"],
            "status,summary,customfield_10109,components,issuelinks,parent,comment",
        )

    def test_issue_shortcut_requests_comment_field_beside_explicit_fields(self) -> None:
        result = self._run("issue", "UPL-490", "--fields", "status")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(MCPHandler.tool_calls[0]["arguments"]["fields"], "status,comment")
        response = json.loads(result.stdout)
        self.assertNotIn("_adapter", response)

    def test_issue_shortcut_adds_context_fields_when_comments_disabled(self) -> None:
        self._set_config_issue_fields("status,summary")

        result = self._run("issue", "UPL-490", "--comments", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            MCPHandler.tool_calls[0]["arguments"]["fields"],
            "status,summary,components,issuelinks,parent",
        )

    def test_issue_shortcut_does_not_duplicate_configured_context_fields(self) -> None:
        self._set_config_issue_fields("status,parent,issuelinks,components")

        result = self._run("issue", "UPL-490")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            MCPHandler.tool_calls[0]["arguments"]["fields"],
            "status,parent,issuelinks,components,comment",
        )

    def test_issue_shortcut_does_not_duplicate_comment_field(self) -> None:
        self._set_config_issue_fields("status,comment")

        result = self._run("issue", "UPL-490")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            MCPHandler.tool_calls[0]["arguments"]["fields"],
            "status,comment,components,issuelinks,parent",
        )

    def test_issue_shortcut_reports_missing_context_fields(self) -> None:
        MCPHandler.issue_extra_fields = {
            "_adapter_missing_context_fields": ["server-value"]
        }
        result = self._run("issue", "UPL-490")

        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        issue = json.loads(response["content"][0]["text"])
        self.assertEqual(
            response["_adapter"]["missing_context_fields"],
            ["components", "issuelinks", "parent"],
        )
        self.assertEqual(
            issue["_adapter_missing_context_fields"],
            ["server-value"],
        )

    def test_issue_shortcut_writes_output_to_file_with_confirmation(self) -> None:
        output_path = Path(self.temp_dir.name) / "issue.json"
        result = self._run("issue", "UPL-490", "--output", str(output_path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(f"saved: {output_path} ("))
        self.assertTrue(output_path.exists())

    def test_issue_shortcut_normalizes_sprint_field(self) -> None:
        result = self._run("issue", "UPL-490")

        self.assertEqual(result.returncode, 0, result.stderr)
        issue = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual(
            issue["customfield_10109"],
            [{"id": 40, "name": "UPL Sprint 11", "state": "CLOSED"}],
        )

    def test_mylist_shortcut_uses_saved_filter_and_normalizes_sprint(self) -> None:
        result = self._run("mylist")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("tools/list", MCPHandler.methods)
        self.assertEqual(
            MCPHandler.tool_calls,
            [
                {
                    "name": "jira_search",
                    "arguments": {
                        "jql": "filter = 10504",
                        "fields": (
                            "summary,status,priority,assignee,issuetype,"
                            "customfield_10109"
                        ),
                        "limit": 50,
                        "start_at": 0,
                    },
                }
            ],
        )
        payload = json.loads(json.loads(result.stdout)["content"][0]["text"])
        self.assertEqual(
            payload["issues"][0]["customfield_10109"],
            [{"id": 41, "name": "UPL Sprint 12", "state": "ACTIVE"}],
        )

    def test_remote_links_reads_via_rest_and_normalizes(self) -> None:
        result = self._run("remote-links", "UPL-677")

        self.assertEqual(result.returncode, 0, result.stderr)
        # remote links use a direct REST GET, not the MCP handshake
        self.assertEqual(MCPHandler.methods, [])
        self.assertEqual(
            MCPHandler.get_paths, ["/rest/api/2/issue/UPL-677/remotelink"]
        )
        self.assertEqual(MCPHandler.get_authorization, "Bearer pat-secret")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["issue_key"], "UPL-677")
        self.assertEqual(
            payload["remote_links"],
            [
                {
                    "title": "Wiki Page",
                    "url": "https://wiki.example/pages/viewpage.action?pageId=26774519",
                    "application_type": "com.atlassian.confluence",
                    "page_id": "26774519",
                },
                {
                    "title": "Spravochniki",
                    "url": "https://wiki.example/spaces/IT/pages/19956161/Spravochniki",
                    "application_type": "com.atlassian.confluence",
                    "page_id": "19956161",
                },
            ],
        )

    def test_remote_links_rejects_invalid_key(self) -> None:
        result = self._run("remote-links", "not-a-key")

        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid issue key", result.stderr)
        self.assertEqual(MCPHandler.get_paths, [])

    def test_fields_shortcut_searches_field_ids(self) -> None:
        result = self._run("fields", "sprint")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("tools/list", MCPHandler.methods)
        self.assertEqual(
            MCPHandler.tool_calls,
            [{"name": "jira_search_fields", "arguments": {"keyword": "sprint"}}],
        )


if __name__ == "__main__":
    unittest.main()
