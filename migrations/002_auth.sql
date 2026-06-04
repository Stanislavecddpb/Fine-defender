-- Миграция 002: авторизация / личный кабинет (см. docs/TZ_addendum_cabinet.md).
-- Идемпотентно. Применение: psql "$DATABASE_URL" -f migrations/002_auth.sql

ALTER TABLE sellers ADD COLUMN IF NOT EXISTS email         TEXT;  -- логин (уникальный)
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS password_hash TEXT;  -- pbkdf2_sha256$...

-- Уникальность email (NULL допускается у селлеров без логина — Postgres
-- разрешает несколько NULL в unique-индексе).
CREATE UNIQUE INDEX IF NOT EXISTS sellers_email_key ON sellers (email);
