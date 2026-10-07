-- Постоянные БД postgres 15: auth, user, ord.
-- Идempotent: позволяет пересоздать инстанс на существующем volume.
SELECT 'CREATE DATABASE "user"' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'user')\gexec
SELECT 'CREATE DATABASE auth' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'auth')\gexec
SELECT 'CREATE DATABASE ord' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ord')\gexec
