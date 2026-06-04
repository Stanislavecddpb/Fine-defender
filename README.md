# Fine Defender — «Защитник от штрафов» для селлеров Wildberries (MVP)

Автоматически находит в кабинете селлера WB оспоримые штрафы одного типа
(повышенная логистика по результатам обмеров), оценивает возвратный потенциал и
готовит **черновик** претензии. Подача — вручную оператором (concierge).
Полное описание — [`TZ_fine_defender_mvp.md`](TZ_fine_defender_mvp.md).

## Статус

Реализованы **M1–M4** (этапы 1–4 ТЗ) и боевая обвязка: Docker-стек
Postgres + API + фоновый воркер. M5 (автоматизация подачи) — намеренно вне MVP.

| Этап | Что | Статус |
|------|-----|--------|
| M1 | Выгрузка отчёта → сырой слой, идемпотентность, журнал прогонов | ✅ |
| M2 | Классификатор `oversize_logistics`, дедлайны, `recoverable_est` | ✅ |
| M3 | Дашборд: список/карточка штрафа, сводка «отбито ₽», действия оператора | ✅ |
| M4 | Генератор претензии (шаблон Jinja2 + чек-лист доказательств) | ✅ |
| Деплой | Docker Compose (Postgres + API + worker), миграции, расписание | ✅ |
| M5 | Автоматизация подачи / предиктив | ⛔ вне MVP (раздел 11 ТЗ) |

Проверено запуском:
- `pytest` — **38 passed** (включая 2 интеграционных теста на реальном Postgres);
- Docker-стек поднят: миграции применены, воркер выгрузил/классифицировал данные
  в Postgres, авто-просрочка работает, API отдаёт данные и принимает действия
  оператора (черновик, статус, возврат), healthcheck `/health/ready` зелёный;
- `PostgresRepository` верифицирован на живой БД (не только InMemory);
- идемпотентность подтверждена на рестарте воркера (rows_new=0, fines_new=0).

Реальный WB API остаётся за тумблером `WB_CLIENT_MODE`: `mock` (демо без токена,
по умолчанию в compose) / `live` (боевой). Перед `live` — сверить раздел 13 ТЗ.

## Архитектура (что уже есть)

```
src/fine_defender/
  config.py             env (секреты) + YAML (бизнес-параметры)
  crypto.py             шифрование WB-токена (envelope; Fernet → KMS в проде)
  logging_utils.py      редакция токенов в логах
  wb/adapter.py         version-tolerant парсинг ответа (поля по кандидатам, деньги-строки)
  wb/client.py          HTTP-клиент: пагинация по rrdid, ретраи/backoff + MockReportClient
  repository.py         Repository (Protocol) + InMemoryRepository (полный backend для демо/тестов)
  db/                   SQLAlchemy-модели + PostgresRepository (idempotent upsert)
  ingestion/worker.py   M1: fetch → normalize → upsert → ingestion_runs
  classifier.py         M2: is_fine → категория → дедлайн/recoverable_est → status
  dispute/generator.py  M4: рендер черновика претензии (Jinja2) + чек-лист
  templates/*.j2        шаблоны претензий по категориям
  api/                  M3: FastAPI-дашборд (main, schemas, bootstrap)
scripts/                seed_seller, run_ingestion, run_pipeline, smoke_api
migrations/001_init.sql схема БД (раздел 5 ТЗ)
config/config.yaml      пороги, паттерны причин, расписание, дедлайн, чек-лист
```

### Дашборд-API (M3)

| Метод | Путь | Назначение |
|-------|------|-----------|
| GET | `/health` | liveness (процесс жив, БД не трогает) |
| GET | `/health/ready` | readiness: проба БД, 503 если недоступна |
| GET | `/api/sellers` | список селлеров |
| GET | `/api/fines?seller_id=&status=&category=` | штрафы + `days_to_deadline`, `deadline_soon` |
| GET | `/api/fines/{id}` | карточка: штраф + сырьё + черновик + история |
| GET | `/api/summary?seller_id=` | под возврат / «отбито ₽» |
| POST | `/api/fines/{id}/draft` | сгенерировать черновик претензии (M4) |
| POST | `/api/fines/{id}/events` | действие оператора: статус, `recovered_amount`, заметка |

Ключевые свойства заложены в ядро (раздел 9 ТЗ): идемпотентный upsert по
`(seller_id, wb_txn_key)`, журнал выгрузок `ingestion_runs`, токен только
зашифрован и никогда не в логах, парсинг WB изолирован в адаптере.

## Быстрый старт (моки, без БД)

```bash
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -e ".[dev]"

# M1 на фикстуре: печатает сырьё, гоняет выгрузку, проверяет идемпотентность
python scripts/run_ingestion.py --mock

# Сквозной путь M1→M2→M4: выгрузка → классификация → сводка → черновик претензии
python scripts/run_pipeline.py

# Дашборд-API без поднятия порта (селлеры → штрафы → сводка → черновик → действие)
python scripts/smoke_api.py

# Тесты
pytest
```

### Живой сервер (демо на memory-бэкенде)

```bash
uvicorn fine_defender.api.main:app --reload
# дашборд:  http://127.0.0.1:8000/        (кликабельный UI)
# Swagger:  http://127.0.0.1:8000/docs
# APP_BACKEND=memory (по умолчанию) сам засевает демо-штрафы из фикстуры
```

**Визуальный дашборд** ([src/fine_defender/static/dashboard.html](src/fine_defender/static/dashboard.html))
отдаётся по `/` тем же сервером и ходит в `/api/*` того же origin: список штрафов с
подсветкой дедлайнов, сводка «отбито ₽», карточка штрафа, кнопки «сгенерировать
претензию» и «отметить возврат». Это самый простой способ проверить продукт
**без реального кабинета WB** — поднимите стек (`docker compose up`) и откройте
`http://localhost:8000/`.

## Боевой запуск — Docker Compose

Полный стек (Postgres + API + фоновый воркер) поднимается одной командой.

```bash
# 1. Сгенерировать ключ шифрования токенов и положить в .env
python -c "from cryptography.fernet import Fernet; print('TOKEN_ENCRYPTION_KEY='+Fernet.generate_key().decode())" >> .env
#    (POSTGRES_PASSWORD, WB_CLIENT_MODE и пр. — см. docker-compose.yml; есть дефолты)

# 2. Поднять стек
docker compose up --build -d

# 3. Открыть дашборд
#    http://localhost:8000/docs  (Swagger)
```

**Режим демо vs бой:**
- `WB_CLIENT_MODE=mock` (по умолчанию) + `DEMO_SEED=true` — стек сам заводит
  демо-селлера и наполняет БД из `samples/weekly_report_demo.json`. Удобно для приёмки.
- `WB_CLIENT_MODE=live` — боевой WB API. Селлеры заводятся вручную:

```bash
WB_TOKEN="<read-only токен категории Финансы>" \
  docker compose run --rm api python scripts/seed_seller.py --name "ООО Ромашка"
```

**Состав стека (`docker-compose.yml`):**

| Сервис | Роль |
|--------|------|
| `db` | PostgreSQL 16 |
| `migrate` | one-shot: применяет `migrations/*.sql` (идемпотентно), затем выходит |
| `api` | REST/дашборд на uvicorn (`APP_BACKEND=postgres`) |
| `worker` | периодическая выгрузка + классификация + авто-просрочка (`WORKER_INTERVAL_SECONDS`) |

Воркер каждый цикл выполняет M1+M2 и переводит оспоримые штрафы с пропущенным
дедлайном в статус `expired` (`disputable`/`drafting` → `expired`). У `api` —
Docker healthcheck на `/health/ready`.

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml): на каждый push/PR
прогоняется `pytest` (юнит + интеграция с поднятым Postgres-сервисом) и сборка
Docker-образа. Юнит-тесты не требуют БД; интеграционные включаются через
`PG_TEST_DSN`.

### Сайт-визитка (лендинг)

[`site/index.html`](site/index.html) — клиентский одностраничный лендинг
(адаптивный, без внешних зависимостей, фирменная палитра WB). Открывается двойным
кликом локально. Авто-деплой на GitHub Pages — workflow
[`.github/workflows/pages.yml`](.github/workflows/pages.yml); включить один раз:
**Settings → Pages → Source: «GitHub Actions»**. Контакт в CTA (`info@fine-defender.ru`)
— плейсхолдер, заменить на реальный.

Образ один (`Dockerfile`), роль выбирается аргументом entrypoint
(`migrate` / `seed-demo` / `worker` / `api`).

### Запуск без Docker (отдельный Postgres)

```bash
cp .env.example .env                       # DATABASE_URL, TOKEN_ENCRYPTION_KEY
python scripts/apply_migrations.py         # схема (ждёт доступности БД)
WB_TOKEN="<токен>" python scripts/seed_seller.py --name "ООО Ромашка"
python scripts/worker.py --once            # один прогон M1+M2; без --once — цикл
APP_BACKEND=postgres uvicorn fine_defender.api.main:app
```

## ⚠️ Перед боевой интеграцией — сверить (раздел 13 ТЗ)

WB регулярно депрекейтит методы. До подключения реального токена подтвердить по
[dev.wildberries.ru](https://dev.wildberries.ru) и пометки `[СВЕРИТЬ]` в
`config/config.yaml`:

- [ ] актуальные эндпоинт/версия метода (v5 объявлен к отключению) и имена полей;
- [ ] идентификатор транзакции для дедупа (`rrd_id` / `srid`), `rr_dt`, поле причины;
- [ ] формат денежных полей (string vs number);
- [ ] категории/scope токена (только read, достаточный для отчёта о реализации);
- [ ] окно оспаривания → формула `dispute_deadline`;
- [ ] лимиты запросов (rate limit).

Адаптер `wb/adapter.py` намеренно терпим к именам полей (списки кандидатов) —
смена версии метода не должна ломать остальной код.
