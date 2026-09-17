# ioka travel

B2B-сервис бронирования авиабилетов (тестовое задание ioka.uz): аутентификация агента → IATA-справочник → асинхронный поиск тарифов с поллингом → детали тарифа → бронирование → атомарная выписка со списанием баланса → PDF-билет.

- **API:** FastAPI (async, Pydantic v2) · **DB:** PostgreSQL 16 + SQLAlchemy 2.0/async + Alembic · **Кэш:** Redis · **Очереди:** RabbitMQ (aio-pika, DLQ) · **PDF:** reportlab · **Auth:** JWT + argon2 · **Логи:** structlog · **Инфра:** Docker Compose, GitHub Actions (ruff + pre-commit + pytest)
- Все деньги — `Decimal`, в API сериализуются **строками** (`"264.30"`).
- Подробности: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/API.md](docs/API.md)

## Быстрый старт

Требуется только Docker.

```bash
make up        # = docker compose up --build -d
```

- Swagger: **http://localhost:8000/travel/docs** · OpenAPI: `/travel/openapi.json`
- Демо-агент: **`agent` / `agent123`**, баланс 10 000 USD
- RabbitMQ UI: **http://localhost:15672** (`ioka/ioka`) · Логи: `make logs` · Стоп: `make down`

Конфигурация — через `env_file: .env` в [docker-compose.yml](docker-compose.yml). Файл `.env` **не хранится в git**: `make` создаёт его из шаблона [.env.example](.env.example) при первом запуске; личные переопределения — `.env.local`.

## Полный цикл (curl)

```bash
BASE=http://localhost:8000/travel
TOKEN=$(curl -s -X POST $BASE/auth/agent/login -H 'Content-Type: application/json' \
  -d '{"username":"agent","password":"agent123"}' | jq -r .access_token)

# 1) IATA-справочник
curl -s "$BASE/avia/locations?query=Tashkent" -H "Authorization: Bearer $TOKEN"

# 2) Асинхронный поиск → search_id (202)
SEARCH_ID=$(curl -s -X POST $BASE/avia/offers -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"origin":"TAS","destination":"IST","departure_date":"2026-10-20","passengers":[{"type":"ADULT"}]}' \
  | jq -r .search_id)

# 3) Поллинг до status="done" (мок-провайдер: 1.5–3 с), оффер детально
curl -s "$BASE/avia/offers/search/$SEARCH_ID" -H "Authorization: Bearer $TOKEN" | jq '.status, .items_found'
OFFER_ID=$(curl -s "$BASE/avia/offers/search/$SEARCH_ID" -H "Authorization: Bearer $TOKEN" | jq -r '.items[0].offer_id')
curl -s "$BASE/avia/offers/$OFFER_ID" -H "Authorization: Bearer $TOKEN" | jq '.fare_families[].name'

# 4) Бронирование (201, BOOKED) — Idempotency-Key защищает от задвоения
ORDER_ID=$(curl -s -X POST $BASE/avia/orders -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-001" \
  -d '{"offer_id":"'$OFFER_ID'","passengers":[{"type":"ADULT","first_name":"IVAN","last_name":"IVANOV","date_of_birth":"1990-05-14","gender":"MALE","citizenship":"UZ","doc_type":"PASSPORT","doc_number":"AB1234567"}],"contact":{"email":"agent@demo.uz"}}' \
  | jq -r .id)

# 5) Выписка: атомарное списание + номер билета (200; повтор — без второго списания)
curl -s -X POST "$BASE/avia/orders/$ORDER_ID/issue" -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: issue-001"

# 6) Статус/история и PDF-билет (202 → 200, application/pdf)
curl -s "$BASE/avia/orders/$ORDER_ID" -H "Authorization: Bearer $TOKEN" | jq '.status, .history'
curl -s "$BASE/avia/orders/$ORDER_ID/ticket" -H "Authorization: Bearer $TOKEN" -o ticket.pdf
```

## Эндпоинты

| Метод и путь | Назначение | Ошибки |
|---|---|---|
| `POST /travel/auth/agent/login` | username/password → JWT | 401, 422 |
| `GET /travel/avia/locations` | IATA-справочник | 401, 422 |
| `POST /travel/avia/offers` | старт поиска (202, `search_id`) | 401, 422 |
| `GET /travel/avia/offers/search/{id}` | поллинг статуса/результатов | 401, 403, 404 |
| `GET /travel/avia/offers/{offer_id}` | детали тарифа (fare families, багаж) | 401, 404, 502, 504 |
| `POST /travel/avia/orders` | бронирование (`Idempotency-Key`) | 401, 404, 409, 410, 422 |
| `POST /travel/avia/orders/{id}/issue` | выписка + списание баланса | 401, 402, 403, 404, 409 |
| `GET /travel/avia/orders/{id}` | заказ + история статусов | 401, 403, 404 |
| `GET /travel/avia/orders/{id}/ticket` | PDF (200) или 202 «генерируется» | 401, 403, 404, 409 |
| `GET /health/live`, `GET /health/ready` | пробы (без `/travel`) | 503 |

Ошибки — единый конверт `{"error": {"code", "message", "details?"}}`; каждый ответ несёт `X-Request-ID`. Полный справочник с примерами — [docs/API.md](docs/API.md).

## Надёжность (кратко)

- **Атомарная выписка:** списание + смена статуса в одной транзакции под `SELECT ... FOR UPDATE` (порядок блокировок агент→заказ); каждое списание — запись в ledger `balance_transactions` c `UNIQUE(order_id, txn_type)`.
- **Идемпотентность:** заголовок `Idempotency-Key` (создание заказа и выписка; replay сохранённого ответа) + естественная идемпотентность повтора issue.
- **Поиск:** фоновый вызов провайдера с дедлайном, ленивый fail мёртвых сессий, Redis-кэш c TTL и фолбэком на PostgreSQL.
- **Аудит:** `provider_call_logs`, `order_status_history`, JSON-логи с `request_id`/`agent_id`.
- **PDF:** генерация в фоновом воркере через RabbitMQ (ретраи ×3, DLQ, ручной ack); при недоступности брокера — синхронный фолбэк.

## Тесты и CI

```bash
make test   # pytest в docker против живых PG/Redis (отдельная тестовая БД)
make lint   # ruff check + format
```

52 теста: unit (прайсинг, JWT, стейт-машина, конкурентная выписка, семантика идемпотентных ключей) и integration (полный цикл API). CI (GitHub Actions): ruff + pre-commit + pytest с сервисами PG/Redis/RabbitMQ — [.github/workflows/ci.yml](.github/workflows/ci.yml). Хуки: `pip install pre-commit && make hooks` (ручной прогон — `make precommit`).

## Структура

```
app/
├── domain/        # сущности, еnums, ports (гексагональное ядро)
├── providers/     # мок-провайдер авиаконтента (детерминированный)
├── repositories/  # SQLAlchemy-адаптеры (orders, agents, idempotency, audit)
├── services/      # бизнес-логика: auth, search, booking, issuing, ticket
├── api/           # FastAPI-роутеры и зависимости (JWT)
├── workers/       # PDF-воркер (RabbitMQ consumer) и рендер
├── messaging/     # шина RabbitMQ
└── core/          # config, security, errors, logging
alembic/  tests/  scripts/  docs/
```

## Известные ограничения

Мок-провайдер (реальный GDS — реализовать `AviaContentProvider`); после потери Redis DONE-сессия опрашивается с пустыми items; stale `LOCKED` идемпотентные ключи (краш между claim и complete) отдают 409 до очистки; нет пагинации заказов и rate limiting.
