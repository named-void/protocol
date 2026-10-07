-- Постоянные БД единого postgres 17: auth, user, catalog, ord, report, tracking.
-- Идемпотентно: позволяет пересоздать инстанс на существующем volume.
SELECT 'CREATE DATABASE "user"' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'user')\gexec
SELECT 'CREATE DATABASE auth' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'auth')\gexec
SELECT 'CREATE DATABASE ord' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ord')\gexec
SELECT 'CREATE DATABASE catalog' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'catalog')\gexec
SELECT 'CREATE DATABASE report' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'report')\gexec
SELECT 'CREATE DATABASE tracking' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'tracking')\gexec
