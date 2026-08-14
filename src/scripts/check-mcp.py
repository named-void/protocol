#!/usr/bin/env python3
"""Проверка доступности MCP-серверов трекера и wiki для `make doctor`.

Печатает по строке на сервер: `ok:` — сервер ответил на initialize,
`fail:` — секция отсутствует, заголовки битые или сервер недоступен,
`skip:` — адаптер выключен конфигом: `product = "none"`, пустое значение или
отсутствие ключа. Значение нормализуется тем же `skills_config.adapter_is_off`,
что и в клиентах адаптеров; имя секции секретов — продукт адаптера
(orchestration/adr/README.md#mcp-credentials).
Имя сервера и исход — единственное, что выводится: URL, заголовки и токены не
печатаются даже в тексте ошибки (правило адаптеров).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from mcp_http import MCPError, StreamableHTTPClient, load_server_config  # noqa: E402
from skills_config import adapter_is_off, get_value, identify_project  # noqa: E402


def check(label: str, server: str, project: str | None) -> bool:
    try:
        config = load_server_config(server, project)
    except MCPError:
        print(f"fail: {label} mcp server '{server}': section [mcp.{server}] missing or invalid")
        return False
    try:
        with StreamableHTTPClient(config) as client:
            client.initialize()
    except Exception as error:  # noqa: BLE001 — любой отказ транспорта равнозначен
        print(f"fail: {label} mcp server '{server}' unreachable ({type(error).__name__})")
        return False
    print(f"ok: {label} mcp server '{server}' reachable")
    return True


def main() -> int:
    ok = True
    project = identify_project()[0]
    if adapter_is_off(get_value("issue_tracker.product", "none")):
        print("skip: issue tracker disabled (issue_tracker.product = none)")
    else:
        ok &= check("issue-tracker", get_value("issue_tracker.product", ""), project)

    if adapter_is_off(get_value("docs_wiki.product", "")):
        print("skip: docs wiki disabled (docs_wiki.product = none or empty)")
    else:
        ok &= check("docs-wiki", get_value("docs_wiki.product", ""), project)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
