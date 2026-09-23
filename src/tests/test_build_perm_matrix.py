from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "projects" / "upl" / "scripts"

FULL_RESET = """\
TRUNCATE TABLE role_permission;
TRUNCATE TABLE permission CASCADE;

DROP TABLE IF EXISTS temp_permissions;
CREATE TEMP TABLE temp_permissions (
    domain varchar,
    action varchar,
    name varchar,
    role_code varchar
);

INSERT INTO temp_permissions
SELECT split_part(r.col, ',', 1) AS col1,
       split_part(r.col, ',', 2) AS col2,
       split_part(r.col, ',', 3) AS col3,
       split_part(r.col, ',', 4) AS col4
FROM (
SELECT unnest(string_to_array('domain,action,name,role_code
vendors,read,Рекламодатели - Чтение,super_admin
vendors,read,Рекламодатели - Чтение,treasurer
vendors-card,read,Рекламодатели - Карточка,-
', '
')) as col
OFFSET 1
) r
WHERE r.col != '';

INSERT INTO permission (id, action, name, description, domain, is_active, is_regex, is_public, path_pattern)
SELECT gen_random_uuid() as id, r.action, r.name, NULL, r.domain, true, false, true, NULL
FROM (
    SELECT DISTINCT tp.domain, tp.action, tp.name, tp.role_code
    FROM temp_permissions tp
    WHERE tp.role_code = '-'
) r
ON CONFLICT (domain, action) DO NOTHING;

INSERT INTO permission (id, action, name, description, domain, is_active, is_regex, is_public, path_pattern)
SELECT r.id, r.action, r.name, NULL, r.domain, true, false, false, NULL
FROM (
    SELECT DISTINCT ON (tp.domain, tp.action) gen_random_uuid() as id, tp.domain, tp.action, tp.name, tp.role_code
    FROM temp_permissions tp
    WHERE tp.role_code != '-'
) r
ON CONFLICT (domain, action) DO NOTHING;

INSERT INTO role_permission
SELECT p.id as permission_id, r.role_code
FROM (
    SELECT DISTINCT ON (tp.domain, tp.action, tp.role_code) tp.domain, tp.action, tp.name, tp.role_code
    FROM temp_permissions tp
    WHERE tp.role_code != '-'
) r
INNER JOIN permission p ON p.domain = r.domain AND p.action = r.action
ON CONFLICT (role_code, permission_id) DO NOTHING;

DROP TABLE IF EXISTS temp_permissions;
"""

PERMISSION_VALUES = """\
INSERT INTO permission (id, action, name, description, domain, is_active, is_regex, is_public, path_pattern)
VALUES
('11111111-1111-1111-1111-111111111111', 'read', 'Краткий список', NULL, 'vendors-slim', true, false, false, NULL),
(gen_random_uuid(), 'update', 'Восстановление', NULL, 'vendors-restore', true, false, false, NULL),
('22222222-2222-2222-2222-222222222222', 'update', 'Архив рекламодателей', NULL, 'vendors-archive', true, false, false, NULL)
ON CONFLICT (domain, action) DO NOTHING;
"""

GRANT_VALUES = """\
INSERT INTO role_permission (permission_id, role_code)
VALUES
('11111111-1111-1111-1111-111111111111', 'super_admin'),
('22222222-2222-2222-2222-222222222222', 'treasurer'),
((SELECT id FROM permission WHERE domain = 'vendors-restore' AND action = 'update' LIMIT 1), 'support')
ON CONFLICT (permission_id, role_code) DO NOTHING;
"""

JOIN_GRANTS = """\
INSERT INTO role_permission (permission_id, role_code)
SELECT p.id, grants.role_code
FROM permission p
JOIN (
    VALUES
        ('vendors', 'delete', 'advertiser_head_account_manager'),
        ('vendors', 'delete', 'advertiser_account_manager')
) AS grants(domain, action, role_code)
    ON grants.domain = p.domain AND grants.action = p.action
ON CONFLICT (permission_id, role_code) DO NOTHING;

INSERT INTO role_permission (permission_id, role_code)
SELECT p.id, r.role_code
FROM permission p
JOIN (
    VALUES
        ('advertiser_admin'),
        ('treasurer')
) AS r(role_code) ON true
WHERE p.domain = 'vendors-slim' AND p.action = 'read'
ON CONFLICT (permission_id, role_code) DO NOTHING;

INSERT INTO role_permission (permission_id, role_code)
WITH grants AS (
    SELECT p.id AS permission_id, grants.role_code
    FROM permission p
    JOIN (
        VALUES
            ('vendors-slim', 'publisher_admin'),
            ('vendors-slim', 'publisher_manager')
    ) AS grants(domain, role_code)
        ON grants.domain = p.domain AND p.action = 'read'
)
SELECT permission_id, role_code
FROM grants
ON CONFLICT (permission_id, role_code) DO NOTHING;
"""

REVOKES = """\
DELETE FROM role_permission rp
USING permission p, (
    VALUES
        ('vendors', 'read', 'treasurer'),
        ('vendors', 'delete', 'advertiser_account_manager')
) AS revokes(domain, action, role_code)
WHERE rp.permission_id = p.id
  AND p.domain = revokes.domain
  AND p.action = revokes.action
  AND rp.role_code = revokes.role_code;

DELETE FROM role_permission
WHERE permission_id IN (SELECT id FROM permission WHERE domain = 'vendors-slim' AND action = 'read')
  AND role_code = 'treasurer';
"""

RENAMES = """\
UPDATE permission
SET action = 'create'
WHERE domain IN ('vendors-restore')
  AND action = 'update';

UPDATE permission SET action = 'delete'
WHERE domain = 'vendors-archive';

UPDATE permission SET is_regex = true, path_pattern = 'GET /api/v1/vendors/card'
WHERE name = 'Рекламодатели - Чтение';
"""

MIGRATIONS = {
    "000001_full.up.sql": FULL_RESET,
    "000002_values.up.sql": PERMISSION_VALUES + GRANT_VALUES,
    "000003_joins.up.sql": JOIN_GRANTS,
    "000004_revokes.up.sql": REVOKES,
    "000005_renames.up.sql": RENAMES,
}


class BuildPermMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()
        for name, body in MIGRATIONS.items():
            (self.migrations / name).write_text(body, encoding="utf-8")
        self.out = self.root / "perm-matrix.json"
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import build_perm_matrix

        self.module = build_perm_matrix

    def run_build(self, force: bool = True) -> int:
        return self.module.build(self.migrations, self.out, force)

    def test_build_replays_whitelist_idioms(self) -> None:
        self.assertEqual(0, self.run_build())
        matrix = json.loads(self.out.read_text(encoding="utf-8"))

        self.assertEqual(["super_admin"], matrix["domains"]["vendors"]["read"]["roles"])
        self.assertEqual(
            ["advertiser_head_account_manager"],
            matrix["domains"]["vendors"]["delete"]["roles"],
        )
        self.assertEqual(
            ["advertiser_admin", "publisher_admin", "publisher_manager", "super_admin"],
            matrix["domains"]["vendors-slim"]["read"]["roles"],
        )
        self.assertEqual(
            ["support"], matrix["domains"]["vendors-restore"]["create"]["roles"]
        )
        self.assertNotIn("update", matrix["domains"]["vendors-restore"])
        card = matrix["domains"]["vendors-card"]["read"]
        self.assertTrue(card["is_public"])
        self.assertEqual([], card["roles"])
        self.assertEqual(
            ["treasurer"], matrix["domains"]["vendors-archive"]["delete"]["roles"]
        )
        self.assertTrue(matrix["domains"]["vendors"]["read"]["is_regex"])
        self.assertEqual(
            "GET /api/v1/vendors/card", matrix["domains"]["vendors"]["read"]["path_pattern"]
        )

    def test_fresh_cache_is_not_rebuilt(self) -> None:
        self.assertEqual(0, self.run_build())
        stale = json.loads(self.out.read_text(encoding="utf-8"))
        (self.migrations / "000005_renames.up.sql").write_text(
            RENAMES + "\n-- touch", encoding="utf-8"
        )
        self.assertEqual(0, self.run_build(force=False))
        fresh = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(stale["generated_at"], fresh["generated_at"])

    def test_force_rebuilds_even_when_fresh(self) -> None:
        self.assertEqual(0, self.run_build())
        self.assertEqual(0, self.run_build(force=True))
        matrix = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual("000005_renames.up.sql", matrix["last_migration"])

    def test_unhandled_statement_fails_loudly(self) -> None:
        (self.migrations / "000006_future.up.sql").write_text(
            "INSERT INTO role_permission SELECT * FROM vendors_backup;\n",
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.build(self.migrations, self.out, force=True)

        self.assertEqual(2, code)
        self.assertIn("unhandled statement", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
