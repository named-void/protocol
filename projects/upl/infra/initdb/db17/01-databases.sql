-- Постоянные БД postgres 17: catalog, report, tracking.
SELECT 'CREATE DATABASE catalog' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'catalog')\gexec
SELECT 'CREATE DATABASE report' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'report')\gexec
SELECT 'CREATE DATABASE tracking' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'tracking')\gexec
