#!/usr/bin/env python3
"""Build the effective UPL permission matrix from auth-service migrations.

Replays the up-chain of auth migrations (INSERT/DELETE/UPDATE/TRUNCATE over
permission and role_permission) into a cache file shaped as
``domains -> action -> {roles, is_public, is_regex, ...}``.

The replay is whitelist-driven: every statement touching permission storage
must match a known SQL idiom, otherwise the build fails loudly. The cache is
rebuilt only when a newer migration appears; replay starts from the last
migration that fully resets permission storage (TRUNCATE role_permission).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERSION_RE = re.compile(r"^(\d{6})_.+\.up\.sql$")
ON_CONFLICT_ROLES = "ON CONFLICT (permission_id, role_code) DO NOTHING"
PUBLIC_ROLE = "-"

# Any statement matching this guard must be handled by a known idiom.
PERMISSION_GUARD = re.compile(
    r"role_permission|temp_permissions|INTO permission|UPDATE permission"
    r"|DELETE FROM permission|TRUNCATE TABLE permission|FROM permission"
)

TRUNCATE_ROLES_RE = re.compile(r"TRUNCATE TABLE role_permission(?: CASCADE)?$")
TRUNCATE_PERMS_RE = re.compile(r"TRUNCATE TABLE permission CASCADE$")
DROP_TEMP_RE = re.compile(r"DROP TABLE IF EXISTS temp_permissions$")
CREATE_TEMP_RE = re.compile(r"CREATE TEMP TABLE temp_permissions \(")

TEMP_INSERT_RE = re.compile(
    r"^INSERT INTO temp_permissions\s+SELECT split_part\(r\.col, ',', \d\) AS col1,\s*"
    r"split_part\(r\.col, ',', \d\) AS col2,\s*split_part\(r\.col, ',', \d\) AS col3,\s*"
    r"split_part\(r\.col, ',', \d\) AS col4\s+"
    r"FROM \(\s*SELECT unnest\(string_to_array\('(?P<csv>.*)',\s*'\n'\)\) as col\s+"
    r"OFFSET 1\s*\) r\s+WHERE r\.col != ''$",
    re.S,
)

PERM_COLUMNS = "id, action, name, description, domain, is_active, is_regex, is_public, path_pattern"
PERM_VALUES_RE = re.compile(
    rf"^INSERT INTO permission \({PERM_COLUMNS}\)\s+VALUES\s+(?P<rows>.*?)"
    r"(?:\s*ON CONFLICT \(domain, action\) DO NOTHING)?$",
    re.S,
)
PERM_FROM_TEMP_RE = re.compile(
    rf"^INSERT INTO permission \({PERM_COLUMNS}\)\s+"
    r"SELECT (?:gen_random_uuid\(\) as id|r\.id), r\.action, r\.name, NULL, r\.domain, "
    r"true, false, (?P<is_public>true|false), NULL\s+"
    r"FROM \(.*FROM temp_permissions tp(?P<filter>.*?)\) r\s+"
    r"ON CONFLICT \(domain, action\) DO NOTHING$",
    re.S,
)
PERM_DELETE_BY_KEYS_RE = re.compile(
    r"^DELETE FROM permission\s+WHERE \(domain, action\) IN \((?P<keys>.*)\)$", re.S
)
PERM_DELETE_BY_UUIDS_RE = re.compile(
    r"^DELETE FROM permission p\s+WHERE p\.id IN\s*\((?P<uuids>.*)\)\s+"
    r"AND NOT EXISTS\s*\(\s*SELECT 1 FROM role_permission rp\s+"
    r"WHERE rp\.permission_id = p\.id\s*\)$",
    re.S,
)

GRANT_VALUES_RE = re.compile(
    r"^INSERT INTO role_permission \(permission_id, role_code\)\s+VALUES\s+(?P<rows>.*?)"
    r"(?:\s*ON CONFLICT \((?:permission_id, role_code|role_code, permission_id)\) DO NOTHING)?$",
    re.S,
)
GRANT_SELECT_HEAD_RE = re.compile(
    r"^INSERT INTO role_permission(?:\s*\(permission_id, role_code\))?\s+"
    r"(?P<with>WITH \w+ AS \(.*?\)\s+)?"
    r"SELECT p\.id(?:\s+AS permission_id)?,\s*\w+\.role_code\s+"
    r"FROM permission p\s+JOIN\s*\(\s*VALUES\s*",
    re.S,
)
GRANT_CTE_HEAD_RE = re.compile(
    r"^INSERT INTO role_permission \(permission_id, role_code\)\s+"
    r"WITH \w+ AS \(\s*SELECT p\.id(?:\s+AS permission_id)?,\s*\w+\.role_code\s+"
    r"FROM permission p\s+JOIN\s*\(\s*VALUES\s*",
    re.S,
)
GRANT_TEMP_RE = re.compile(
    r"^INSERT INTO role_permission\b.*FROM temp_permissions tp.*"
    r"INNER JOIN permission p ON p\.domain = r\.domain AND p\.action = r\.action.*",
    re.S,
)
REVOKE_USING_RE = re.compile(
    r"^DELETE FROM role_permission(?:\s+\w+)?\s+USING permission p,\s*\(\s*VALUES\s*",
    re.S,
)
REVOKE_IN_SELECT_RE = re.compile(
    r"^DELETE FROM role_permission\s+WHERE permission_id IN\s*"
    r"\(SELECT id FROM permission WHERE domain = '(?P<domain>[^']+)' "
    r"AND action = '(?P<action>[^']+)'(?: LIMIT 1)?\)"
    r"(?P<rest>.*)$",
    re.S,
)
REVOKE_IN_UUIDS_RE = re.compile(
    r"^DELETE FROM role_permission\s+WHERE permission_id IN\s*\((?P<uuids>.*)\)$", re.S
)
UPDATE_ACTION_RE = re.compile(
    r"^UPDATE permission\s+SET action = '(?P<new>[^']+)'\s+WHERE (?P<where>domain[^=].*)$",
    re.S,
)
UPDATE_META_RE = re.compile(
    r"^UPDATE permission SET (?P<sets>.*?)\s+WHERE name = '(?P<name>[^']+)'$", re.S
)

class MigrationError(RuntimeError):
    """Raised when a migration statement matches no known idiom."""


@dataclass
class Permission:
    domain: str
    action: str
    name: str = ""
    is_active: bool = True
    is_regex: bool = False
    is_public: bool = False
    path_pattern: str | None = None


@dataclass
class Replay:
    permissions: dict[tuple[str, str], Permission] = field(default_factory=dict)
    grants: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    uuid_index: dict[str, tuple[str, str]] = field(default_factory=dict)
    temp_rows: list[tuple[str, str, str, str]] = field(default_factory=list)

    def grant(self, key: tuple[str, str], role: str) -> None:
        self.grants.setdefault(key, set()).add(role)

    def revoke(self, key: tuple[str, str], role: str) -> None:
        self.grants.get(key, set()).discard(role)

    def register(self, permission: Permission, uuid: str | None = None) -> None:
        key = (permission.domain, permission.action)
        self.permissions[key] = permission
        if uuid:
            self.uuid_index[uuid] = key

    def rename_action(self, domains: list[str], new: str, old: str | None) -> None:
        for domain in domains:
            if old:
                keys = [(domain, old)]
            else:
                keys = [key for key in self.permissions if key[0] == domain]
            for key in keys:
                permission = self.permissions.pop(key)
                permission.action = new
                self.register(permission)
                roles = self.grants.pop(key, set())
                if roles:
                    self.grants[(domain, new)] = roles

    def resolve_uuid(self, uuid: str) -> tuple[str, str]:
        if uuid not in self.uuid_index:
            raise MigrationError(f"Unknown permission UUID {uuid}")
        return self.uuid_index[uuid]


def strip_comments(text: str) -> str:
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "'" and text[index + 1: index + 2] == "'":
                out.append("''")
                index += 2
                continue
            if char == "'":
                in_string = False
            out.append(char)
        elif char == "'":
            in_string = True
            out.append(char)
        elif char == "-" and text[index + 1: index + 2] == "-":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def split_statements(text: str) -> list[str]:
    """Split on semicolons outside single-quoted strings."""
    statements: list[str] = []
    buffer: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "'" and text[index + 1: index + 2] == "'":
                buffer.append("''")
                index += 2
                continue
            if char == "'":
                in_string = False
            buffer.append(char)
        elif char == "'":
            in_string = True
            buffer.append(char)
        elif char == ";":
            statements.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return [statement for statement in statements if statement]


def split_rows(blob: str) -> list[str]:
    """Split VALUES rows stored inside parentheses, comma-separated."""
    rows: list[str] = []
    buffer: list[str] = []
    depth = 0
    in_string = False
    index = 0
    while index < len(blob):
        char = blob[index]
        if in_string:
            if char == "'" and blob[index + 1: index + 2] == "'":
                buffer.append("''")
                index += 2
                continue
            if char == "'":
                in_string = False
            buffer.append(char)
        elif char == "'":
            in_string = True
            buffer.append(char)
        elif char == "(":
            depth += 1
            if depth == 1:
                buffer = []
                index += 1
                continue
            buffer.append(char)
        elif char == ")":
            depth -= 1
            if depth == 0:
                rows.append("".join(buffer).strip())
                buffer = []
                index += 1
                continue
            buffer.append(char)
        else:
            buffer.append(char)
        index += 1
    return [row for row in rows if row]


def split_values(blob: str) -> list[str]:
    """Split on commas outside quotes and parentheses."""
    values: list[str] = []
    buffer: list[str] = []
    depth = 0
    in_string = False
    index = 0
    while index < len(blob):
        char = blob[index]
        if in_string:
            if char == "'" and blob[index + 1: index + 2] == "'":
                buffer.append("''")
                index += 2
                continue
            if char == "'":
                in_string = False
            buffer.append(char)
        elif char == "'":
            in_string = True
            buffer.append(char)
        elif char == "(":
            depth += 1
            buffer.append(char)
        elif char == ")":
            depth -= 1
            buffer.append(char)
        elif char == "," and depth == 0:
            values.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
        index += 1
    values.append("".join(buffer).strip())
    return [value for value in values if value != ""]


def literal(value: str) -> str | None:
    if value.upper() == "NULL":
        return None
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    raise MigrationError(f"Expected a string literal, got {value!r}")


def boolean(value: str) -> bool:
    if value.lower() not in {"true", "false"}:
        raise MigrationError(f"Expected a boolean literal, got {value!r}")
    return value.lower() == "true"


def parse_scalar(value: str) -> str | None:
    if re.fullmatch(r"(?:gen_random_uuid|uuid_generate_v4)\(\)", value, re.I):
        return None
    return literal(value)


class MatrixBuilder:
    def __init__(self) -> None:
        self.state = Replay()

    def apply(self, version: str, statement: str) -> None:
        if not PERMISSION_GUARD.search(statement):
            return
        for handler in (
            self._truncate,
            self._create_temp,
            self._temp_insert,
            self._drop_temp,
            self._perm_values,
            self._perm_from_temp,
            self._grant_values,
            self._grant_cte_select,
            self._grant_join_select,
            self._grant_temp,
            self._revoke_using,
            self._revoke_in_select,
            self._revoke_uuids,
            self._delete_permission,
            self._delete_permission_keys,
            self._update_action,
            self._update_meta,
        ):
            if handler(statement):
                return
        raise MigrationError(
            f"migration {version}: unhandled statement:\n{statement[:400]}"
        )

    # ------------------------------------------------------------------ setup

    def _truncate(self, statement: str) -> bool:
        if TRUNCATE_ROLES_RE.fullmatch(statement):
            self.state.grants.clear()
            return True
        if TRUNCATE_PERMS_RE.fullmatch(statement):
            self.state.permissions.clear()
            self.state.uuid_index.clear()
            return True
        return False

    def _create_temp(self, statement: str) -> bool:
        if CREATE_TEMP_RE.match(statement):
            return True
        return False

    def _temp_insert(self, statement: str) -> bool:
        match = TEMP_INSERT_RE.match(statement)
        if not match:
            return False
        rows: list[tuple[str, str, str, str]] = []
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("domain,"):
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 4:
                raise MigrationError(f"Malformed temp row: {line!r}")
            rows.append((parts[0], parts[1], parts[2], parts[3]))
        self.state.temp_rows = rows
        return True

    def _drop_temp(self, statement: str) -> bool:
        if DROP_TEMP_RE.fullmatch(statement):
            self.state.temp_rows = []
            return True
        return False

    # ---------------------------------------------------------- permission DML

    def _perm_values(self, statement: str) -> bool:
        match = PERM_VALUES_RE.match(statement)
        if not match:
            return False
        for row in split_rows(match.group("rows")):
            values = split_values(row)
            if len(values) != 9:
                raise MigrationError(f"Permission row must have 9 values: {row[:120]}")
            uuid = parse_scalar(values[0])
            action = literal(values[1])
            name = literal(values[2])
            domain = literal(values[4])
            is_regex = boolean(values[6])
            is_public = boolean(values[7])
            path_pattern = literal(values[8])
            self.state.register(
                Permission(domain, action, name, is_regex=is_regex,
                           is_public=is_public, path_pattern=path_pattern),
                uuid=uuid,
            )
        return True

    def _perm_from_temp(self, statement: str) -> bool:
        match = PERM_FROM_TEMP_RE.match(statement)
        if not match:
            return False
        is_public = match.group("is_public") == "true"
        if re.search(r"tp\.role_code = '-'", match.group("filter")):
            rows = {row for row in self.state.temp_rows if row[3] == PUBLIC_ROLE}
        elif re.search(r"tp\.role_code != '-'", match.group("filter")):
            rows = {row for row in self.state.temp_rows if row[3] != PUBLIC_ROLE}
        else:
            raise MigrationError(f"Unhandled temp filter: {match.group('filter')[:120]}")
        for domain, action, name, _role in rows:
            self.state.register(Permission(domain, action, name, is_public=is_public))
        return True

    def _delete_permission_keys(self, statement: str) -> bool:
        match = PERM_DELETE_BY_KEYS_RE.match(statement)
        if not match:
            return False
        for row in split_rows(match.group("keys")):
            values = split_values(row)
            if len(values) != 2:
                raise MigrationError(f"Permission key row must have 2 values: {row[:120]}")
            key = (literal(values[0]), literal(values[1]))
            self.state.permissions.pop(key, None)
            self.state.grants.pop(key, None)
        return True

    def _delete_permission(self, statement: str) -> bool:
        match = PERM_DELETE_BY_UUIDS_RE.match(statement)
        if not match:
            return False
        for value in split_values(match.group("uuids")):
            key = self.state.resolve_uuid(literal(value))
            self.state.permissions.pop(key, None)
        return True

    # ------------------------------------------------------- role_permission

    def _grant_values(self, statement: str) -> bool:
        match = GRANT_VALUES_RE.match(statement)
        if not match:
            return False
        for row in split_rows(match.group("rows")):
            values = split_values(row)
            if len(values) != 2:
                raise MigrationError(f"Grant row must have 2 values: {row[:120]}")
            key = self._resolve_permission_ref(values[0])
            self.state.grant(key, literal(values[1]))
        return True

    def _resolve_permission_ref(self, ref: str) -> tuple[str, str]:
        inner = re.fullmatch(
            r"\(SELECT id FROM permission WHERE domain = '([^']+)' "
            r"AND action = '([^']+)'(?: LIMIT 1)?\)",
            ref,
        )
        if inner:
            return inner.group(1), inner.group(2)
        if ref.startswith("'"):
            return self.state.resolve_uuid(literal(ref))
        raise MigrationError(f"Unhandled permission reference: {ref[:120]}")

    def _grant_cte_select(self, statement: str) -> bool:
        head = GRANT_CTE_HEAD_RE.match(statement)
        if not head:
            return False
        rows_blob, columns, tail = self._split_join_tail(statement[head.end():])
        roles_by_row = [split_values(row) for row in split_rows(rows_blob)]
        tail = re.sub(
            r"\)\s*SELECT permission_id, role_code\s+FROM \w+\s*", "", tail or ""
        )
        tail_data = self._parse_join_tail(tail) if tail else None
        self._apply_join(columns, roles_by_row, None, tail_data)
        return True

    def _grant_temp(self, statement: str) -> bool:
        if not GRANT_TEMP_RE.match(statement):
            return False
        for domain, action, _name, role in self.state.temp_rows:
            if role != PUBLIC_ROLE:
                self.state.grant((domain, action), role)
        return True

    def _grant_join_select(self, statement: str) -> bool:
        head = GRANT_SELECT_HEAD_RE.match(statement)
        if not head:
            return False
        rows_blob, columns, tail = self._split_join_tail(statement[head.end():])
        roles_by_row = [split_values(row) for row in split_rows(rows_blob)]
        tail_data = self._parse_join_tail(tail) if tail else None
        self._apply_join(columns, roles_by_row, head.group("with"), tail_data)
        return True

    @staticmethod
    def _split_join_tail(blob: str) -> tuple[str, list[str], str | None]:
        """Split `<rows>) AS alias(cols) ON ...` into rows, columns and the tail."""
        depth = 0
        in_string = False
        close = -1
        index = 0
        while index < len(blob):
            char = blob[index]
            if in_string:
                if char == "'" and blob[index + 1: index + 2] == "'":
                    index += 2
                    continue
                if char == "'":
                    in_string = False
            elif char == "'":
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    close = index
                    break
                depth -= 1
            index += 1
        if close < 0:
            raise MigrationError(f"Malformed join values: {blob[:160]}")
        rows_blob = blob[:close]
        rest = blob[close + 1:].strip()
        alias_match = re.match(r"AS \w+\((?P<cols>[a-z_, ]+)\)\s*", rest)
        if not alias_match:
            raise MigrationError(f"Malformed join alias: {rest[:160]}")
        columns = [column.strip() for column in alias_match.group("cols").split(",")]
        tail = rest[alias_match.end():].strip()
        return rows_blob, columns, tail or None

    @staticmethod
    def _parse_join_tail(tail: str) -> dict[str, str | None]:
        result: dict[str, str | None] = {"on": None, "where": None, "with_tail": None}
        remainder = tail
        on_match = re.match(r"ON\s+(.*?)(?:\n|$)(?P<rest>.*)$", remainder, re.S)
        if on_match:
            result["on"] = on_match.group(1).strip()
            remainder = on_match.group("rest")
        where_match = re.search(
            r"WHERE\s+(.*?)(?=\nON CONFLICT|\nSELECT |$)", remainder, re.S
        )
        if where_match:
            result["where"] = where_match.group(1).strip()
            remainder = remainder[: where_match.start()] + remainder[where_match.end():]
        if re.search(r"SELECT permission_id, role_code\s+FROM \w+", remainder):
            result["with_tail"] = "select"
            remainder = re.sub(
                r"SELECT permission_id, role_code\s+FROM \w+", "", remainder
            )
        leftover = remainder.replace(ON_CONFLICT_ROLES, "").strip()
        if leftover:
            raise MigrationError(f"Unparsed join tail: {leftover[:160]}")
        return result

    def _apply_join(
        self,
        columns: list[str],
        roles_by_row: list[list[str]],
        with_clause: str | None,
        tail: dict[str, str | None] | None,
    ) -> None:
        if not tail or not tail.get("on"):
            raise MigrationError("Grant join without ON clause")
        on_clause: str = tail["on"]
        where = tail.get("where")
        if with_clause and not tail.get("with_tail"):
            raise MigrationError("WITH join missing select tail")
        if columns == ["domain", "action", "role_code"]:
            if not re.fullmatch(r"\w+\.domain = p\.domain AND \w+\.action = p\.action", on_clause):
                raise MigrationError(f"Unhandled 3-col join: {on_clause[:120]}")
            for row in roles_by_row:
                domain, action, role = (literal(value) for value in row)
                self.state.grant((domain, action), role)
        elif columns == ["domain", "role_code"]:
            on_action = re.fullmatch(
                r"\w+\.domain = p\.domain AND p\.action = '([^']+)'", on_clause
            )
            if on_action:
                action = on_action.group(1)
                if where:
                    raise MigrationError(f"Unhandled 2-col where: {where[:120]}")
            else:
                if not re.fullmatch(r"\w+\.domain = p\.domain", on_clause):
                    raise MigrationError(f"Unhandled 2-col join: {on_clause[:120]}")
                action = self._single_where(where, r"p\.action = '([^']+)'", "2-col")[0]
            for row in roles_by_row:
                domain, role = (literal(value) for value in row)
                self.state.grant((domain, action), role)
        elif columns == ["role_code"]:
            if on_clause != "true":
                raise MigrationError(f"Unhandled 1-col join: {on_clause[:120]}")
            scope = self._single_where(
                where, r"p\.domain = '([^']+)' AND p\.action = '([^']+)'", "1-col"
            )
            for row in roles_by_row:
                role = literal(row[0])
                self.state.grant((scope[0], scope[1]), role)
        else:
            raise MigrationError(f"Unhandled join columns: {columns}")

    @staticmethod
    def _single_where(where: str | None, pattern: str, label: str):
        if not where:
            raise MigrationError(f"{label} join requires WHERE clause")
        match = re.fullmatch(pattern, where.strip())
        if not match:
            raise MigrationError(f"Unhandled {label} where: {where[:120]}")
        return match.groups()

    def _revoke_using(self, statement: str) -> bool:
        head = REVOKE_USING_RE.match(statement)
        if not head:
            return False
        rows_blob, columns, tail = self._split_join_tail(statement[head.end():])
        if not tail:
            raise MigrationError("Unhandled revoke USING shape")
        if columns == ["domain", "action", "role_code"]:
            if not re.fullmatch(
                r"WHERE rp\.permission_id = p\.id\s+AND p\.domain = \w+\.domain\s+"
                r"AND p\.action = \w+\.action\s+AND rp\.role_code = \w+\.role_code",
                tail,
            ):
                raise MigrationError(f"Unhandled revoke USING tail: {tail[:160]}")
            for row in split_rows(rows_blob):
                domain, action, role = (literal(value) for value in split_values(row))
                self.state.revoke((domain, action), role)
            return True
        if columns == ["domain", "role_code"]:
            tail_action = re.fullmatch(
                r"WHERE rp\.permission_id = p\.id\s+AND p\.domain = \w+\.domain\s+"
                r"AND p\.action = '([^']+)'\s+AND rp\.role_code = \w+\.role_code",
                tail,
            )
            if not tail_action:
                raise MigrationError(f"Unhandled revoke USING tail: {tail[:160]}")
            for row in split_rows(rows_blob):
                domain, role = (literal(value) for value in split_values(row))
                self.state.revoke((domain, tail_action.group(1)), role)
            return True
        raise MigrationError("Unhandled revoke USING shape")

    def _revoke_in_select(self, statement: str) -> bool:
        match = REVOKE_IN_SELECT_RE.match(statement)
        if not match:
            return False
        key = (match.group("domain"), match.group("action"))
        rest = match.group("rest").replace(ON_CONFLICT_ROLES, "").strip()
        if not rest:
            roles: list[str] | None = None
        else:
            single = re.fullmatch(r"AND role_code = '([^']+)'", rest)
            multiple = re.fullmatch(r"AND role_code IN \(([^)]*)\)", rest, re.S)
            if single:
                roles = [single.group(1)]
            elif multiple:
                roles = [literal(value) for value in split_values(multiple.group(1))]
            else:
                raise MigrationError(f"Unhandled revoke filter: {rest[:120]}")
        for role in roles or sorted(self.state.grants.get(key, set())):
            self.state.revoke(key, role)
        return True

    def _revoke_uuids(self, statement: str) -> bool:
        match = REVOKE_IN_UUIDS_RE.match(statement)
        if not match:
            return False
        for value in split_values(match.group("uuids")):
            key = self.state.resolve_uuid(literal(value))
            self.state.grants.pop(key, None)
        return True

    # ----------------------------------------------------------------- update

    def _update_action(self, statement: str) -> bool:
        match = UPDATE_ACTION_RE.match(statement)
        if not match:
            return False
        where = match.group("where").strip()
        in_list = re.fullmatch(
            r"domain IN \((?P<domains>[^)]*)\)(?:\s+AND action = '(?P<old>[^']+)')?", where
        )
        single = re.fullmatch(r"domain = '(?P<domain>[^']+)'", where)
        if in_list:
            domains = [literal(value) for value in split_values(in_list.group("domains"))]
            self.state.rename_action(domains, match.group("new"), in_list.group("old"))
            return True
        if single:
            self.state.rename_action([single.group("domain")], match.group("new"), None)
            return True
        raise MigrationError(f"Unhandled UPDATE action where: {where[:120]}")

    def _update_meta(self, statement: str) -> bool:
        match = UPDATE_META_RE.match(statement)
        if not match:
            return False
        assignments: dict[str, str | bool | None] = {}
        for pair in split_values(match.group("sets")):
            column, value = pair.split("=", 1)
            column = column.strip()
            value = value.strip()
            if column in {"is_regex", "is_public", "is_active"}:
                assignments[column] = boolean(value)
            elif column in {"path_pattern", "name"}:
                assignments[column] = literal(value)
            else:
                raise MigrationError(f"Unhandled permission column update: {column}")
        updated = 0
        for permission in self.state.permissions.values():
            if permission.name != match.group("name"):
                continue
            for column, value in assignments.items():
                setattr(permission, column, value)
            updated += 1
        if not updated:
            raise MigrationError(
                f"UPDATE permission matched no known permission named {match.group('name')!r}"
            )
        return True

    # ----------------------------------------------------------------- output

    def to_dict(self, migrations_dir: str, last_migration: str) -> dict:
        domains: dict[str, dict[str, dict]] = {}
        keys = set(self.state.permissions) | set(self.state.grants)
        for domain, action in sorted(keys):
            permission = self.state.permissions.get((domain, action))
            domains.setdefault(domain, {})[action] = {
                "roles": sorted(self.state.grants.get((domain, action), set())),
                "is_public": permission.is_public if permission else False,
                "is_regex": permission.is_regex if permission else False,
                "is_active": permission.is_active if permission else True,
                "path_pattern": permission.path_pattern if permission else None,
                "name": permission.name if permission else "",
            }
        all_roles = sorted(
            {role for roles in self.state.grants.values() for role in roles}
        )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "migrations_dir": migrations_dir,
            "last_migration": last_migration,
            "roles": all_roles,
            "domains": domains,
        }


def migration_files(migrations_dir: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for path in sorted(migrations_dir.iterdir()):
        match = VERSION_RE.match(path.name)
        if match:
            files.append((match.group(1), path))
    if not files:
        raise MigrationError(f"No up-migrations found in {migrations_dir}")
    return files


def replay_plan(files: list[tuple[str, Path]]) -> list[tuple[str, list[str]]]:
    """Statements from the last migration that resets permission storage onward."""
    reset_index = 0
    for index, (_version, path) in enumerate(files):
        text = strip_comments(path.read_text(encoding="utf-8"))
        if "TRUNCATE TABLE role_permission" in text:
            reset_index = index
    plan: list[tuple[str, list[str]]] = []
    for _version, path in files[reset_index:]:
        statements = split_statements(strip_comments(path.read_text(encoding="utf-8")))
        plan.append((path.name, statements))
    return plan


def build(migrations_dir: Path, out_path: Path, force: bool) -> int:
    files = migration_files(migrations_dir)
    last_migration = files[-1][1].name
    if not force and out_path.exists():
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("last_migration") == last_migration:
            print(f"up-to-date {last_migration}")
            return 0
    builder = MatrixBuilder()
    for migration_name, statements in replay_plan(files):
        for statement in statements:
            try:
                builder.apply(migration_name, statement)
            except MigrationError as error:
                print(f"build_perm_matrix: {error}", file=sys.stderr)
                return 2
    document = builder.to_dict(str(migrations_dir), last_migration)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=out_path.parent, delete=False
    ) as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(out_path)
    domains = len(document["domains"])
    cells = sum(len(actions) for actions in document["domains"].values())
    print(
        f"built {last_migration} domains={domains} cells={cells} "
        f"roles={len(document['roles'])}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrations", required=True, type=Path,
                        help="auth-service migrations directory")
    parser.add_argument("--out", required=True, type=Path,
                        help="cache file path (runtime perimeter, not versioned)")
    parser.add_argument("--force", action="store_true", help="rebuild even when fresh")
    args = parser.parse_args(argv)
    if not args.migrations.is_dir():
        print(f"build_perm_matrix: no such directory {args.migrations}", file=sys.stderr)
        return 2
    return build(args.migrations, args.out, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
