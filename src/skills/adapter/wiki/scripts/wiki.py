#!/usr/bin/env python3

import argparse
import base64
import json
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "lib"))

from mcp_http import (  # noqa: E402
    MCPError,
    emit_output,
    execute,
    limited_int,
    parse_arguments,
    rest_get,
)
from skills_config import adapter_is_off, get_value  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[4]

# Режим выключенного адаптера: `product = "none"`, пустое значение либо
# отсутствие секции `docs_wiki` (lib/skills_config.py, REQUIRED_KEYS). Гейт
# держит сам клиент, потому что его вызывают и вне маршрута, где прозы
# SKILL.md в контексте нет (SB-210).
PRODUCT = get_value("docs_wiki.product", "")
# Имя секции секретов — продукт адаптера; формат описан в
# `skills/adapter/references/credentials.md`.
MCP_SERVER = PRODUCT
TOOL_PREFIX = get_value("docs_wiki.tool_prefix", "confluence_")
DEFAULT_SPACES = get_value("docs_wiki.spaces", "")

# Статус резолюции комментария MCP-инструмент не отдаёт, поэтому он читается
# прямым REST GET, переиспользующим заголовки MCP-шлюза, — тем же каналом, что
# remote links адаптера трекера (SB-217).
REST_URL_HEADER = get_value(
    "docs_wiki.rest.url_header", "X-Atlassian-Confluence-Url"
)
REST_TOKEN_HEADER = get_value(
    "docs_wiki.rest.token_header",
    "X-Atlassian-Confluence-Personal-Token",
)

# Статусы резолюции, при которых обсуждение закрыто: снятый вопрос и
# потерянный якорь inline-комментария. Читатель страницы их не видит, поэтому
# открытым вопросом автору такой комментарий не является.
CLOSED_RESOLUTIONS = frozenset({"resolved", "dangling"})
# Порция REST-списка комментариев; хвост забирается пагинацией.
COMMENT_PAGE_LIMIT = 100

# pageId в query-параметре или последний числовой сегмент pretty-URL
# (/pages/<id>/Title, /pages/viewpage.action?pageId=<id>).
PAGE_ID_QUERY_RE = re.compile(r"[?&]pageId=(\d+)")
PAGE_ID_PATH_RE = re.compile(r"/pages/(\d+)(?:/|$)")
# tiny-ссылка Confluence: /x/<id>, где id — URL-safe base64 от pageId,
# уложенного в little-endian 8-байтовый long с обрезанными хвостовыми
# нулевыми байтами (SB-64).
PAGE_ID_TINY_RE = re.compile(r"/x/([A-Za-z0-9_-]+)")


def _page_id_from_tiny(identifier: str) -> str:
    # Достроить обрезанные нулевые байты (символ 'A') до полного long и
    # добавить padding до кратности 4.
    padded = identifier + "A" * (11 - len(identifier)) + "="
    try:
        (page_id,) = struct.unpack("<q", base64.urlsafe_b64decode(padded))
    except (ValueError, struct.error):
        raise MCPError(f"cannot decode tiny link '/x/{identifier}'")
    return str(page_id)


def page_id_from_reference(reference: str) -> str:
    if reference.isdigit():
        return reference
    for pattern in (PAGE_ID_QUERY_RE, PAGE_ID_PATH_RE):
        match = pattern.search(reference)
        if match:
            return match.group(1)
    tiny = PAGE_ID_TINY_RE.search(reference)
    if tiny:
        return _page_id_from_tiny(tiny.group(1))
    raise MCPError(f"cannot extract page id from '{reference}'")


BODY_KEYS = ("content", "body")


def _without_body(payload: dict) -> dict:
    # При include_metadata=True тело приходит в `metadata.content.value`, а не
    # на верхнем уровне payload (SB-212), поэтому ключи снимаются на обоих
    # уровнях: у разных версий инструмента уровень разный.
    for key in BODY_KEYS:
        payload.pop(key, None)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in BODY_KEYS:
            metadata.pop(key, None)
    return payload


def _apply_to_payloads(result: dict, transform) -> dict:
    # Тот же JSON приходит двумя носителями: текстом в `content` и копией в
    # `structuredContent.result` — без обработки копии снятое остаётся в выводе
    # целиком (SB-212). Best-effort: неразборный payload остаётся как есть;
    # transform возвращает None, если payload ему не подходит.
    for item in result.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            payload = json.loads(item["text"])
        except (KeyError, TypeError, ValueError):
            continue
        changed = transform(payload)
        if changed is not None:
            item["text"] = json.dumps(changed, ensure_ascii=False)
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return result
    copy = structured.get("result")
    # Сервер отдаёт копию строкой, но схема допускает и объект.
    if isinstance(copy, str):
        try:
            payload = json.loads(copy)
        except ValueError:
            return result
        changed = transform(payload)
        if changed is not None:
            structured["result"] = json.dumps(changed, ensure_ascii=False)
    elif copy is not None:
        changed = transform(copy)
        if changed is not None:
            structured["result"] = changed
    return result


def strip_page_body(result: dict) -> dict:
    # У инструмента нет metadata-only режима, поэтому режим --body none убирает
    # тело локально.
    def transform(payload):
        return _without_body(payload) if isinstance(payload, dict) else None

    return _apply_to_payloads(result, transform)


def fetch_resolutions(page_id: str) -> dict[str, dict[str, str]]:
    """Статус резолюции и дату версии каждого комментария страницы по REST.

    Возвращает отображение id комментария в `{"resolution": …, "date": …}`;
    отсутствующий у комментария статус даёт пустую строку. `depth=all`
    обязателен: без него список несёт только корни тредов, а ответы остаются
    без статуса (проверено — 21 запись против 25 у MCP-инструмента).
    """
    resolutions: dict[str, dict[str, str]] = {}
    start = 0
    while True:
        raw = rest_get(
            MCP_SERVER,
            f"/rest/api/content/{page_id}/child/comment"
            "?expand=extensions.resolution,version&depth=all"
            f"&limit={COMMENT_PAGE_LIMIT}&start={start}",
            url_header=REST_URL_HEADER,
            token_header=REST_TOKEN_HEADER,
        )
        results = raw.get("results") if isinstance(raw, dict) else None
        if not isinstance(results, list):
            raise MCPError("REST comment list carries no 'results' array")
        for entry in results:
            if not isinstance(entry, dict):
                continue
            extensions = entry.get("extensions")
            resolution = (
                extensions.get("resolution") if isinstance(extensions, dict) else None
            )
            version = entry.get("version")
            resolutions[str(entry.get("id", ""))] = {
                "resolution": (
                    str(resolution.get("status") or "")
                    if isinstance(resolution, dict)
                    else ""
                ),
                "date": (
                    str(version.get("when") or "") if isinstance(version, dict) else ""
                ),
            }
        # Список отдаётся порциями: без пагинации статусы хвоста остались бы
        # неизвестными, а его закрытые комментарии — в выводе.
        if len(results) < COMMENT_PAGE_LIMIT:
            return resolutions
        start += len(results)


def summarize_comments(
    result: dict,
    resolutions: dict[str, dict[str, str]],
    *,
    source: str,
    error: str = "",
    include_resolved: bool = False,
) -> dict:
    # Реестр Sources фиксирует свежесть комментариев, но MCP-сервер отдаёт
    # created/updated пустыми на всех инструментах (SB-23) — клиент считает
    # механический маркер свежести сам: количество и максимальный числовой
    # id (id контента Confluence монотонно растут). Маркер считается по
    # активным комментариям: закрытое обсуждение открытым вопросом не является
    # и по умолчанию из вывода убирается (SB-217).
    def transform(payload):
        if not isinstance(payload, list):
            return None
        entries = []
        for comment in payload:
            if not isinstance(comment, dict):
                continue
            found = resolutions.get(str(comment.get("id", "")), {})
            annotated = dict(comment)
            # Статус, которого REST не дал: комментарий без резолюции активен,
            # недоступный канал статусов — unknown, и тогда не фильтруется
            # ничего.
            annotated["resolution"] = (
                found.get("resolution") or ("open" if source == "rest" else "unknown")
            )
            annotated["date"] = found.get("date", "")
            entries.append(annotated)
        active = [c for c in entries if c["resolution"] not in CLOSED_RESOLUTIONS]
        ids = [int(c["id"]) for c in active if str(c.get("id", "")).isdigit()]
        latest_date = max(
            (
                str(c.get("updated") or c.get("created") or c.get("date") or "")
                for c in active
            ),
            default="",
        )
        summary = {
            "count": len(entries),
            "open_count": len(active),
            "closed_count": len(entries) - len(active),
            "max_id": max(ids) if ids else None,
            "latest_date": latest_date,
            "resolution_source": source,
        }
        if error:
            summary["resolution_error"] = error
        return {
            "summary": summary,
            "comments": entries if include_resolved else active,
        }

    return _apply_to_payloads(result, transform)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call docs-wiki MCP tools on demand")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("tools", help="list tools and schemas")

    call_parser = subparsers.add_parser("call", help="call any docs-wiki tool")
    call_parser.add_argument("tool")
    call_parser.add_argument("--arguments")
    call_parser.add_argument("--arguments-file")

    page_parser = subparsers.add_parser(
        "page",
        help="read a page with version metadata without requesting the tool schema",
    )
    page_parser.add_argument("reference", help="page id or page URL")
    page_parser.add_argument(
        "--body",
        choices=["storage", "markdown", "none"],
        default="storage",
        help="body format; 'markdown' converts (loses ri:page links); "
        "'none' strips the body locally before emitting (cheap version check)",
    )
    page_parser.add_argument(
        "--output",
        type=Path,
        help="write output to a file instead of stdout",
    )

    search_parser = subparsers.add_parser(
        "search",
        help="full-text search without requesting the tool schema",
    )
    search_parser.add_argument("query")
    search_parser.add_argument(
        "--spaces",
        default=DEFAULT_SPACES,
        help="comma-separated space keys filter (default from docs_wiki.spaces)",
    )
    search_parser.add_argument("--limit", type=limited_int(1, 50), default=10)
    search_parser.add_argument(
        "--output",
        type=Path,
        help="write output to a file instead of stdout",
    )

    comments_parser = subparsers.add_parser(
        "comments",
        help="page comments without requesting the tool schema",
    )
    comments_parser.add_argument("reference", help="page id or page URL")
    comments_parser.add_argument(
        "--include-resolved",
        action="store_true",
        help="keep closed discussions (resolved, dangling) in the output; "
        "by default only active comments are emitted, with counts in summary",
    )
    comments_parser.add_argument(
        "--output",
        type=Path,
        help="write output to a file instead of stdout",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if adapter_is_off(PRODUCT):
        print(
            "error: docs_wiki.product is 'none', empty or unset: the adapter is"
            " off and makes no network call; requirement sources come from the"
            " caller",
            file=sys.stderr,
        )
        return 2
    try:
        if args.command == "tools":
            result = execute(MCP_SERVER, TOOL_PREFIX, "tools")
        elif args.command == "call":
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=args.tool,
                arguments=parse_arguments(args.arguments, args.arguments_file),
            )
        elif args.command == "page":
            # get_page не принимает content_format: формат тела управляется
            # только convert_to_markdown, метаданные — include_metadata (SB-2).
            arguments = {
                "page_id": page_id_from_reference(args.reference),
                "include_metadata": True,
                "convert_to_markdown": args.body == "markdown",
            }
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=f"{TOOL_PREFIX}get_page",
                arguments=arguments,
            )
            if args.body == "none":
                result = strip_page_body(result)
        elif args.command == "comments":
            page_id = page_id_from_reference(args.reference)
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=f"{TOOL_PREFIX}get_comments",
                arguments={"page_id": page_id},
            )
            # Статусы идут вторым каналом: отказ REST оставляет команду
            # рабочей, но статусы неизвестны — тогда вывод не фильтруется, а
            # причина стоит в сводке.
            try:
                resolutions = fetch_resolutions(page_id)
                source, error = "rest", ""
            except (MCPError, OSError) as failure:
                resolutions, source, error = {}, "unavailable", str(failure)
            result = summarize_comments(
                result,
                resolutions,
                source=source,
                error=error,
                include_resolved=args.include_resolved,
            )
        else:
            arguments = {"query": args.query, "limit": args.limit}
            if args.spaces:
                arguments["spaces_filter"] = args.spaces
            result = execute(
                MCP_SERVER,
                TOOL_PREFIX,
                "call",
                tool=f"{TOOL_PREFIX}search",
                arguments=arguments,
            )
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
