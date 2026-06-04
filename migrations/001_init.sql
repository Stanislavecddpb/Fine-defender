-- Миграция 001: начальная схема (соответствует разделу 5 ТЗ).
-- Двухслойно: неизменяемый сырой слой + нормализованные штрафы.
-- Применение: psql "$DATABASE_URL" -f migrations/001_init.sql

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- для gen_random_uuid()

-- Подключённые селлеры (тенанты)
CREATE TABLE IF NOT EXISTS sellers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT,
    wb_token_enc    BYTEA NOT NULL,                  -- зашифрованный токен (envelope encryption)
    token_scopes    TEXT[],                          -- зафиксировать, что только read
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active       BOOLEAN NOT NULL DEFAULT true
);

-- Сырой слой: исходные транзакции отчёта как есть. Append-only, не редактируется.
CREATE TABLE IF NOT EXISTS raw_transactions (
    id              BIGSERIAL PRIMARY KEY,
    seller_id       UUID NOT NULL REFERENCES sellers(id),
    wb_txn_key      TEXT NOT NULL,                   -- rrd_id/srid; ключ дедупликации
    report_period   DATERANGE,
    raw_json        JSONB NOT NULL,                  -- полная исходная строка
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (seller_id, wb_txn_key)                   -- идемпотентность upsert
);

CREATE INDEX IF NOT EXISTS idx_raw_txn_seller ON raw_transactions (seller_id);

-- Нормализованные штрафы
CREATE TABLE IF NOT EXISTS fines (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id        UUID NOT NULL REFERENCES sellers(id),
    raw_txn_id       BIGINT NOT NULL REFERENCES raw_transactions(id),
    category         TEXT NOT NULL,                  -- на MVP: 'oversize_logistics' | 'other'
    reason_raw       TEXT,                           -- исходная расшифровка причины
    amount           NUMERIC(12,2) NOT NULL,         -- сумма штрафа
    charged_at       DATE NOT NULL,                  -- rr_dt
    dispute_deadline DATE,                           -- вычисляется от charged_at
    recoverable_est  NUMERIC(12,2),                  -- оценка возвратного потенциала
    status           TEXT NOT NULL DEFAULT 'new',    -- см. перечень статусов
    discrepancy_flag BOOLEAN NOT NULL DEFAULT false, -- API != ЛК
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (raw_txn_id)                              -- один штраф на исходную транзакцию
);

CREATE INDEX IF NOT EXISTS idx_fines_seller_status ON fines (seller_id, status);
CREATE INDEX IF NOT EXISTS idx_fines_deadline ON fines (dispute_deadline);

-- Сгенерированные черновики претензий
CREATE TABLE IF NOT EXISTS dispute_drafts (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fine_id            UUID NOT NULL REFERENCES fines(id),
    body_md            TEXT NOT NULL,                -- текст претензии
    evidence_checklist JSONB NOT NULL,              -- список требуемых доказательств
    generated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Журнал статусов спора (обновляется оператором вручную)
CREATE TABLE IF NOT EXISTS dispute_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fine_id          UUID NOT NULL REFERENCES fines(id),
    status           TEXT NOT NULL,
    recovered_amount NUMERIC(12,2),                  -- фактически возвращено (при won)
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Журнал выгрузок (наблюдаемость)
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id     UUID NOT NULL REFERENCES sellers(id),
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL,                     -- running | success | failed | partial
    rows_ingested INT,
    error_detail  TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_seller ON ingestion_runs (seller_id, started_at DESC);
