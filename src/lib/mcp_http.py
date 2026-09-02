#!/usr/bin/env python3
"""Minimal on-demand Streamable HTTP client for configured Atlassian MCP servers."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skills_config import (
    get_section,
    identify_project,
    secrets_path,
)

PROTOCOL_VERSION = "2025-03-26"


class MCPError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerConfig:
    url: str
    headers: dict[str, str]


def load_server_config(server_name: str, project: str | None = None) -> ServerConfig:
    """URL и заголовки сервера — из файла секретов периметра, секция
    ``[mcp.<server_name>]`` (orchestration/adr/README.md#mcp-credentials).
    Конфиг чужого CLI не читается: креды принадлежат этому слою, иначе работа в
    claude/kilo молча зависит от заполненного конфига Codex. ``project`` —
    явный контекст проекта; по умолчанию опознаётся сам."""
    resolved, channel = (
        (project, "argument") if project is not None else identify_project()
    )
    config_path = secrets_path()
    try:
        server = get_section(f"mcp.{server_name}")
        url = server["url"]
        headers = server.get("http_headers", {})
    except (KeyError, TypeError) as error:
        # Секреты принадлежат периметру: у каждого носителя свой файл, и
        # отсутствие секции читается как битые креды, хотя причина — работа не
        # в том периметре (SB-107).
        layer = (
            f"; perimeter: project {resolved} (by {channel})"
            if resolved
            else "; perimeter: _common — work outside any identified project"
            " reads the common carrier: run from the project directory or set"
            " AGENTS_PROJECT_PATH/AGENTS_PROJECT"
        )
        raise MCPError(
            f"cannot load MCP server {server_name!r} from [mcp.{server_name}]"
            f" of {config_path}: {error}{layer}"
        ) from error

    if not isinstance(url, str) or not url:
        raise MCPError(f"MCP server {server_name!r} has no valid URL")
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in headers.items()
    ):
        raise MCPError(f"MCP server {server_name!r} has invalid HTTP headers")

    return ServerConfig(url=url, headers=headers)


class StreamableHTTPClient:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.session_id: str | None = None
        self.request_id = 0

    def __enter__(self) -> Self:
        self.initialize()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.config.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    @staticmethod
    def _decode_response(body: bytes, content_type: str, request_id: int | None) -> Any:
        if not body:
            return None

        text = body.decode("utf-8")
        if "text/event-stream" in content_type or text.startswith("event:"):
            messages = []
            data_lines: list[str] = []
            # SSE разделяет строки только по CR, LF и CRLF, а `str.splitlines()`
            # режет ещё и по U+2028/U+2029/U+0085 — они валидны внутри строки
            # JSON, и отделившийся хвост события терял префикс `data:`, уходил в
            # игнорируемые строки, а разбор падал на усечённом JSON (SB-133).
            stream = text.replace("\r\n", "\n").replace("\r", "\n")
            for line in stream.split("\n"):
                if line.startswith("data:"):
                    data_lines.append(line.removeprefix("data:").lstrip())
                elif not line and data_lines:
                    messages.append(json.loads("\n".join(data_lines)))
                    data_lines = []
            if data_lines:
                messages.append(json.loads("\n".join(data_lines)))
            if request_id is None:
                return None
            for message in messages:
                if message.get("id") == request_id:
                    return message
            raise MCPError(f"MCP response does not contain request id {request_id}")

        return json.loads(text)

    def _send(self, payload: dict[str, Any]) -> Any:
        request_id = payload.get("id")
        request = urllib.request.Request(
            self.config.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if not self.session_id:
                    self.session_id = response.headers.get("Mcp-Session-Id")
                message = self._decode_response(
                    response.read(),
                    response.headers.get_content_type(),
                    request_id,
                )
        except (OSError, json.JSONDecodeError) as error:
            raise MCPError(f"MCP request failed: {error}") from error

        if isinstance(message, dict) and "error" in message:
            error = message["error"]
            raise MCPError(
                f"MCP error {error.get('code', 'unknown')}: "
                f"{error.get('message', 'unknown error')}"
            )
        return message

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.request_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        message = self._send(payload)
        if not isinstance(message, dict) or "result" not in message:
            raise MCPError(f"MCP method {method!r} returned no result")
        return message["result"]

    def initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "codex-atlassian-skill", "version": "1"},
            },
        )
        if not self.session_id:
            raise MCPError("MCP server did not return a session id")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError(f"MCP tool {name!r} returned an invalid result")
        if result.get("isError"):
            messages = [
                item.get("text", "")
                for item in result.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            detail = "; ".join(filter(None, messages)) or "unknown tool error"
            raise MCPError(f"MCP tool {name!r} failed: {detail}")
        return result

    def close(self) -> None:
        if not self.session_id:
            return
        request = urllib.request.Request(
            self.config.url,
            headers=self._headers(),
            method="DELETE",
        )
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except OSError:
            pass
        finally:
            self.session_id = None


def parse_arguments(raw: str | None, path: str | None) -> dict[str, Any]:
    if raw is not None and path is not None:
        raise MCPError("use either --arguments or --arguments-file")
    if path is not None:
        raw = Path(path).read_text()
    if raw is None:
        return {}
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MCPError(f"invalid JSON arguments: {error}") from error
    if not isinstance(arguments, dict):
        raise MCPError("tool arguments must be a JSON object")
    return arguments


def execute(
    server_name: str,
    tool_prefix: str,
    command: str,
    *,
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
    require_read_only: bool = False,
) -> Any:
    if command not in ("tools", "call"):
        raise MCPError(f"unsupported command {command!r}")
    if command == "call":
        if tool is None:
            raise MCPError("tool name is required")
        if not tool.startswith(tool_prefix):
            raise MCPError(
                f"tool {tool!r} is outside allowed prefix {tool_prefix!r}"
            )

    config = load_server_config(server_name)
    with StreamableHTTPClient(config) as client:
        if command == "tools":
            return [
                item for item in client.list_tools()
                if item.get("name", "").startswith(tool_prefix)
            ]
        if require_read_only:
            matching = [
                item for item in client.list_tools()
                if item.get("name") == tool
            ]
            if len(matching) != 1:
                raise MCPError(
                    f"tool {tool!r} is not uniquely declared by the MCP server"
                )
            annotations = matching[0].get("annotations")
            if (
                not isinstance(annotations, dict)
                or annotations.get("readOnlyHint") is not True
            ):
                raise MCPError(
                    f"external write denied: tool {tool!r} is not explicitly read-only"
                )
        return client.call_tool(tool, arguments or {})


def rest_get(
    server_name: str,
    path: str,
    *,
    url_header: str,
    token_header: str,
    timeout: int = 60,
) -> Any:
    """Read-only REST GET against the product host behind the MCP gateway.

    Reuses the gateway's configured base-URL and personal-token headers so a
    direct REST call needs no separate credentials. Needed for product endpoints
    the MCP server exposes no tool for (e.g. Jira issue remote links).
    """
    config = load_server_config(server_name)
    base = config.headers.get(url_header)
    token = config.headers.get(token_header)
    if not base:
        raise MCPError(
            f"MCP server {server_name!r} has no {url_header!r} header for REST access"
        )
    if not token:
        raise MCPError(
            f"MCP server {server_name!r} has no {token_header!r} header for REST access"
        )
    url = base.rstrip("/") + "/" + path.lstrip("/")
    # Базовый URL приходит из заголовка конфига, поэтому битое значение даёт
    # ValueError вместо отказа сети: без нормализации в MCPError вызывающий
    # получает traceback там, где обрабатывает отказ канала.
    try:
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (OSError, ValueError) as error:
        raise MCPError(f"REST request failed: {error}") from error
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise MCPError(f"REST response is not JSON: {error}") from error


def limited_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum}"
            )
        return number

    return parse


def emit_output(text: str, output: Path | None) -> None:
    if output is not None:
        output.write_text(text, encoding="utf-8")
        print(f"saved: {output} ({len(text)} chars)")
    else:
        sys.stdout.write(text)
