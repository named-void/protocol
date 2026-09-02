#!/usr/bin/env python3

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "lib"))

from mcp_http import (
    MCPError,
    emit_output,
    execute,
    limited_int,
    parse_arguments,
    rest_get,
)
from skills_config import adapter_is_off, get_value

REPO_ROOT = Path(__file__).resolve().parents[4]

# Системное имя поля Sprint (`jira_search_fields` печатает его id вида
# customfield_NNNNN) — значение установки, не адаптера: без ключа поле просто
# не запрашивается. Приходит как raw Greenhopper toString(), а не JSON.
SPRINT_FIELD_ID = get_value("issue_tracker.fields.sprint", "")
# Режим выключенного адаптера: `product = "none"`, пустое значение либо
# отсутствие ключа — источник issue передаёт вызывающий. Дефолт тот же, что у
# гейта конфигурации (`scripts/check-mcp.py`), иначе один конфиг читается как
# выключенный там и включённый здесь. Гейт держит сам клиент, потому что его
# вызывают и вне маршрута, где прозы SKILL.md в контексте нет (SB-210).
PRODUCT = get_value("issue_tracker.product", "none")
# Имя секции секретов — продукт адаптера; формат описан в
# `skills/adapter/references/credentials.md`.
MCP_SERVER = PRODUCT
TOOL_PREFIX = get_value("issue_tracker.tool_prefix", "jira_")
# Личный список — сохранённый фильтр конкретной установки; общего дефолта нет
# (`lib/skills_config.py`), команда без ключа отказывает.
MYLIST_JQL = get_value("issue_tracker.my_issues_query", "")
KEY_PATTERN = get_value("issue_tracker.key_pattern", "^[A-Za-z][A-Za-z0-9]*-[0-9]+$")

# Remote links (панель Links задачи, в т.ч. привязанные wiki-страницы) не имеют
# MCP-инструмента чтения и не входят в поля issue — читаются прямым REST GET,
# переиспользуя заголовки MCP-шлюза с базовым URL продукта и personal-token.
REST_URL_HEADER = get_value(
    "issue_tracker.rest.url_header", "X-Atlassian-Jira-Url"
)
REST_TOKEN_HEADER = get_value(
    "issue_tracker.rest.token_header",
    "X-Atlassian-Jira-Personal-Token",
)


def _with_fields(fields: str, *required: str) -> str:
    names = [name.strip() for name in fields.split(",") if name.strip()]
    for name in required:
        if name and name not in names:
            names.append(name)
    return ",".join(names)


def _with_sprint(fields: str) -> str:
    return _with_fields(fields, SPRINT_FIELD_ID)


ISSUE_CONTEXT_FIELDS = ("components", "issuelinks", "parent")
ISSUE_FIELDS_DEFAULT = _with_fields(
    get_value(
        "issue_tracker.default_fields",
        _with_sprint(
            "status,labels,issuetype,description,updated,summary,"
            "reporter,created,assignee,priority"
        ),
    ),
    *ISSUE_CONTEXT_FIELDS,
)
DEFAULT_SEARCH_FIELDS = "summary,status,assignee,priority"
DEFAULT_COMMENT_LIMIT = 20
COMMENT_FIELD = "comment"


def with_comment_field(fields: str, limit: int) -> str:
    """Дописать поле комментариев к набору, пока лимит не нулевой.

    Комментарии — часть постановки, но набор полей приходит из конфигурации
    (`issue_tracker.default_fields`) или флага `--fields`, и оба могут его не
    перечислять: тогда трекер поле не отдаёт, а `comment_limit` на выборку не
    влияет — задача читается как «комментариев нет» (SB-129).
    """
    if limit <= 0:
        return fields
    return _with_fields(fields, COMMENT_FIELD)


MYLIST_FIELDS = _with_sprint("summary,status,priority,assignee,issuetype")
MYLIST_LIMIT = 50

# Порядок полей внутри Greenhopper toString() зависит от версии Jira,
# поэтому каждое поле ищется независимо; name читается до запятой или ']'.
SPRINT_PART_PATTERNS = {
    "id": re.compile(r"\bid=(\d+)"),
    "name": re.compile(r"\bname=([^,\]]*)"),
    "state": re.compile(r"\bstate=([A-Z]+)"),
}


def _parse_sprint_entries(raw_values: list[Any]) -> list[dict[str, Any]]:
    parsed = []
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        parts = {
            key: match.group(1)
            for key, pattern in SPRINT_PART_PATTERNS.items()
            if (match := pattern.search(raw))
        }
        if parts.keys() == SPRINT_PART_PATTERNS.keys():
            parsed.append(
                {
                    "id": int(parts["id"]),
                    "name": parts["name"],
                    "state": parts["state"],
                }
            )
    return parsed


def _normalize_sprint_field(record: dict[str, Any]) -> None:
    field = record.get(SPRINT_FIELD_ID)
    if isinstance(field, dict) and isinstance(field.get("value"), list):
        record[SPRINT_FIELD_ID] = _parse_sprint_entries(field["value"])


def normalize_sprint_fields(
    result: Any,
    expected_context_fields: tuple[str, ...] = (),
) -> Any:
    """Rewrite raw Greenhopper Sprint toString() values into structured JSON in-place."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return result
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return result
    try:
        payload = json.loads(first["text"])
    except (json.JSONDecodeError, TypeError, KeyError):
        return result
    if not isinstance(payload, dict):
        return result

    issues = payload.get("issues")
    records = issues if isinstance(issues, list) else [payload]
    for record in records:
        if isinstance(record, dict):
            _normalize_sprint_field(record)
    if expected_context_fields and not isinstance(issues, list):
        result["_adapter"] = {
            "missing_context_fields": [
                field for field in expected_context_fields if field not in payload
            ]
        }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    first["text"] = text
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured:
        structured["result"] = text
    return result


PAGE_ID_PATTERNS = (
    re.compile(r"[?&]pageId=(\d+)"),
    re.compile(r"/pages/(\d+)(?:/|$)"),
)


def _extract_page_id(url: str) -> str | None:
    for pattern in PAGE_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def normalize_remote_links(raw: Any, issue_key: str) -> dict[str, Any]:
    """Reduce the REST remotelink payload to {title, url, application_type, page_id}.

    page_id is parsed from wiki URLs (pageId query or /pages/<id> path) so a
    linked wiki page can be read by id; None when the URL carries no page id.
    """
    entries = raw if isinstance(raw, list) else []
    links = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        obj = entry.get("object") or {}
        app = entry.get("application") or {}
        url = obj.get("url")
        links.append(
            {
                "title": obj.get("title"),
                "url": url,
                "application_type": app.get("type") or app.get("name"),
                "page_id": _extract_page_id(url) if isinstance(url, str) else None,
            }
        )
    return {"issue_key": issue_key, "remote_links": links}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call issue-tracker MCP tools on demand")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("tools", help="list tools and schemas")

    for command, help_text in (
        ("call", "call an explicitly read-only issue-tracker tool"),
        ("write-call", "call an issue-tracker write tool after /external-write"),
    ):
        call_parser = subparsers.add_parser(command, help=help_text)
        call_parser.add_argument("tool")
        call_parser.add_argument("--arguments")
        call_parser.add_argument("--arguments-file")

    issue_parser = subparsers.add_parser(
        "issue",
        help="read an issue without requesting the tool schema",
    )
    issue_parser.add_argument("issue_key")
    issue_parser.add_argument("--fields", default=ISSUE_FIELDS_DEFAULT)
    issue_parser.add_argument(
        "--comments",
        type=limited_int(0, 100),
        default=DEFAULT_COMMENT_LIMIT,
        help="max comments to include (0 disables)",
    )
    issue_parser.add_argument("--expand")
    issue_parser.add_argument(
        "--output",
        type=Path,
        help="write output to a file instead of stdout",
    )

    search_parser = subparsers.add_parser(
        "search",
        help="search issues without requesting the tool schema",
    )
    search_parser.add_argument("jql")
    search_parser.add_argument("--fields", default=DEFAULT_SEARCH_FIELDS)
    search_parser.add_argument("--limit", type=limited_int(1, 50), default=10)
    search_parser.add_argument("--start-at", type=limited_int(0, 1_000_000), default=0)
    search_parser.add_argument(
        "--output",
        type=Path,
        help="write output to a file instead of stdout",
    )

    mylist_parser = subparsers.add_parser(
        "mylist",
        help="personal task list: saved filter, fixed display-table fields",
    )
    mylist_parser.add_argument("--limit", type=limited_int(1, 50), default=MYLIST_LIMIT)
    mylist_parser.add_argument("--start-at", type=limited_int(0, 1_000_000), default=0)
    mylist_parser.add_argument(
        "--output",
        type=Path,
        help="write output to a file instead of stdout",
    )

    fields_parser = subparsers.add_parser(
        "fields",
        help="search issue-tracker field ids by keyword",
    )
    fields_parser.add_argument("keyword")

    remote_links_parser = subparsers.add_parser(
        "remote-links",
        help="read remote links (Links panel, incl. linked wiki pages) via REST",
    )
    remote_links_parser.add_argument("issue_key")
    remote_links_parser.add_argument(
        "--output",
        type=Path,
        help="write output to a file instead of stdout",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if adapter_is_off(PRODUCT):
        print(
            "error: issue_tracker.product is 'none' or empty: the adapter is off"
            " and makes no network call; the issue source comes from the caller"
            " (file or task text)",
            file=sys.stderr,
        )
        return 2
    try:
        if args.command == "tools":
            result = execute(MCP_SERVER, TOOL_PREFIX, "tools")
        elif args.command in ("call", "write-call"):
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=args.tool,
                arguments=parse_arguments(args.arguments, args.arguments_file),
                require_read_only=args.command == "call",
            )
        elif args.command == "issue":
            if not re.match(KEY_PATTERN, args.issue_key):
                raise MCPError(f"invalid issue key '{args.issue_key}'")
            arguments = {
                "issue_key": args.issue_key,
                "fields": with_comment_field(args.fields, args.comments),
                "comment_limit": args.comments,
                "update_history": False,
            }
            if args.expand:
                arguments["expand"] = args.expand
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=f"{TOOL_PREFIX}get_issue",
                arguments=arguments,
            )
        elif args.command == "fields":
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=f"{TOOL_PREFIX}search_fields",
                arguments={"keyword": args.keyword},
            )
        elif args.command == "remote-links":
            if not re.match(KEY_PATTERN, args.issue_key):
                raise MCPError(f"invalid issue key '{args.issue_key}'")
            raw = rest_get(
                MCP_SERVER,
                f"/rest/api/2/issue/{args.issue_key}/remotelink",
                url_header=REST_URL_HEADER,
                token_header=REST_TOKEN_HEADER,
            )
            result = normalize_remote_links(raw, args.issue_key)
        elif args.command == "mylist":
            if not MYLIST_JQL:
                print(
                    "error: issue_tracker.my_issues_query is not configured;"
                    " the personal list is a value of the installation"
                    " (project card or station config)",
                    file=sys.stderr,
                )
                return 2
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=f"{TOOL_PREFIX}search",
                arguments={
                    "jql": MYLIST_JQL,
                    "fields": MYLIST_FIELDS,
                    "limit": args.limit,
                    "start_at": args.start_at,
                },
            )
        else:
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=f"{TOOL_PREFIX}search",
                arguments={
                    "jql": args.jql,
                    "fields": args.fields,
                    "limit": args.limit,
                    "start_at": args.start_at,
                },
            )
        if args.command == "issue":
            requested = {
                field.strip() for field in args.fields.split(",") if field.strip()
            }
            expected_context_fields = tuple(
                field for field in ISSUE_CONTEXT_FIELDS if field in requested
            )
            result = normalize_sprint_fields(result, expected_context_fields)
        elif args.command not in ("tools", "fields", "remote-links"):
            result = normalize_sprint_fields(result)
        emit_output(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            getattr(args, "output", None),
        )
        return 0
    except (MCPError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
