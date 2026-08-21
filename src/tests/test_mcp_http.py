from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))

from mcp_http import MCPError, StreamableHTTPClient, rest_get


def sse_event(payload: dict, *, newline: str = "\n") -> bytes:
    """Одно SSE-событие с телом ответа в единственном поле `data:`."""
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: message{newline}data: {body}{newline}{newline}".encode()


class DecodeResponseTest(unittest.TestCase):
    def message(self, text: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": text}]},
        }

    def test_unicode_line_separators_inside_json_string_survive(self) -> None:
        # U+2028/U+2029/U+0085 валидны внутри строки JSON и приходят от сервера
        # незаэкранированными; резать поток по ним нельзя — хвост события теряет
        # префикс `data:`, отбрасывается, и разбор падает на усечённом JSON.
        for name, char in (
            ("U+2028 line separator", "\u2028"),
            ("U+2029 paragraph separator", "\u2029"),
            ("U+0085 next line", "\u0085"),
        ):
            with self.subTest(name):
                payload = self.message(f"комментарий{char}продолжение")
                decoded = StreamableHTTPClient._decode_response(
                    sse_event(payload), "text/event-stream", 1
                )
                self.assertEqual(payload, decoded)

    def test_multiline_data_fields_are_joined_with_lf(self) -> None:
        body = 'data: {"jsonrpc": "2.0", "id": 1,\ndata: "result": {"ok": true}}\n\n'
        decoded = StreamableHTTPClient._decode_response(
            body.encode("utf-8"), "text/event-stream", 1
        )
        self.assertEqual({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, decoded)

    def test_crlf_delimited_stream_is_parsed(self) -> None:
        payload = self.message("обычный комментарий")
        decoded = StreamableHTTPClient._decode_response(
            sse_event(payload, newline="\r\n"), "text/event-stream", 1
        )
        self.assertEqual(payload, decoded)

    def test_response_for_another_request_id_is_rejected(self) -> None:
        payload = self.message("ответ на чужой запрос")
        with self.assertRaises(MCPError):
            StreamableHTTPClient._decode_response(
                sse_event(payload), "text/event-stream", 2
            )


class RestGetTest(unittest.TestCase):
    """Базовый URL REST приходит из заголовка конфига, поэтому его битое
    значение обязано давать MCPError: вызывающие обрабатывают отказ канала как
    MCPError/OSError, а ValueError выходит из них traceback'ом."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        carrier = Path(self.temp_dir.name) / "project.toml"
        carrier.write_text('[docs_wiki]\nproduct = "confluence"\n')
        secrets = Path(self.temp_dir.name) / "secrets.toml"
        secrets.write_text(
            """[mcp.confluence]
url = "http://127.0.0.1:9/mcp"

[mcp.confluence.http_headers]
X-Url = "not-a-url"
X-Token = "pat-secret"
"""
        )
        patcher = patch.dict(
            os.environ,
            {"AGENTS_CONFIG": str(carrier), "AGENTS_SECRETS": str(secrets)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_malformed_base_url_is_reported_as_mcp_error(self) -> None:
        with self.assertRaises(MCPError) as failure:
            rest_get(
                "confluence",
                "/rest/api/content/123/child/comment",
                url_header="X-Url",
                token_header="X-Token",
            )

        self.assertIn("REST request failed", str(failure.exception))
        self.assertNotIn("pat-secret", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
