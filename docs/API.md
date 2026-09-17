# API Reference

Справочник HTTP API сервиса. Те же схемы в машиночитаемом виде — OpenAPI: **http://localhost:8000/travel/docs** (swagger) и `/travel/openapi.json`.

## Общие положения

- **Базовый URL:** `http://localhost:8000`, все бизнес-роуты смонтированы под префиксом **`/travel`** (задаётся `API_PREFIX`). Пробы здоровья — вне префикса: `/health/live`, `/health/ready`.
- **Авторизация:** все эндпоинты, кроме логина и health-проб, требуют заголовок `Authorization: Bearer <access_token>` (JWT HS256, `scope=agent`, время жизни `JWT_EXPIRE_MINUTES`, по умолчанию 60 минут).
- **Деньги — строки.** Любая денежная величина (`price`, `total_amount`, `fare_amount`, `debited_amount`, `balance_after`, `balance`) сериализуется строкой (`"264.30"`) — Decimal никогда не превращается во float.
- **Формат ошибок.** Все бизнес-ошибки — единый конверт:

  ```json
  {"error": {"code": "NOT_FOUND", "message": "Order not found"}}
  ```

  Опциональное поле `details` содержит структуру (например, при 402). Нюанс: если запрос не проходит **схемную** валидацию pydantic (неверный тип, отсутствующее поле, нарушение `pattern`/длины), FastAPI возвращает свой стандартный формат `{"detail": [...]}` со статусом 422; конверт `error` с кодом `VALIDATION_ERROR` используется для **бизнес-**валидации (например, «unknown airport code»). Необработанные исключения → `500` `INTERNAL_ERROR`.
- **Трассировка.** Каждый ответ несёт заголовок `X-Request-ID` (входящий из запроса либо сгенерированный) — он же присутствует во всех структурных логах вместе с `agent_id`.

---

## POST /travel/auth/agent/login

Обмен username/password на bearer-токен.

- **Авторизация:** не требуется.

**Запрос** (`LoginIn`):

| Поле | Тип | Ограничения |
|---|---|---|
| `username` | string | 1–64 символа |
| `password` | string | 1–128 символов |

```json
{"username": "agent", "password": "agent123"}
```

**200** (`LoginOut`):

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600,
  "agent": {
    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "username": "agent",
    "company_name": "Demo Travel Agency LLC",
    "balance": "10000.00",
    "currency": "USD"
  }
}
```

`expires_in` — оставшееся время жизни токена в секундах.

**Ошибки:** `401` — неизвестный агент, неверный пароль или отключённая учётная запись (ответ одинаковый во всех трёх случаях — перебор пользователей невозможен); `422` — нарушение схемы тела.

---

## GET /travel/avia/locations

Поиск по справочнику IATA-локаций: код, аэропорт, город или страна. Точное совпадение по IATA-коду ранжируется первым. Результат кэшируется (ключ `locations:{query}:{limit}`, TTL `LOCATIONS_CACHE_TTL_SECONDS`, по умолчанию 1 час).

- **Авторизация:** требуется.

**Параметры запроса:**

| Параметр | Тип | Ограничения | По умолчанию |
|---|---|---|---|
| `query` | string | обязателен, 2–64 символа | — |
| `limit` | int | 1–50 | 10 |

**200** — массив `LocationOut`:

```json
[
  {
    "code": "TAS",
    "name": "Tashkent International Airport",
    "city": "Tashkent",
    "country": "Uzbekistan",
    "country_code": "UZ",
    "type": "AIRPORT"
  }
]
```

**Ошибки:** `401`; `422` (схемная: `query` короче 2 символов).

---

## POST /travel/avia/offers

Старт асинхронного поиска тарифов. Валидирует маршрут и даты, создаёт сессию поиска (PostgreSQL + Redis) и сразу возвращает `202` с идентификатором для поллинга; сам поиск выполняется в фоне.

- **Авторизация:** требуется.

**Запрос** (`SearchCreateIn`):

| Поле | Тип | Ограничения | По умолчанию |
|---|---|---|---|
| `origin` | string | ровно 3 символа (IATA), приводится к верхнему регистру | — |
| `destination` | string | ровно 3 символа (IATA), приводится к верхнему регистру | — |
| `departure_date` | date | не в прошлом | — |
| `return_date` | date \| null | не раньше `departure_date` | null |
| `passengers` | array | 1–9 элементов `{type}` | `[{"type": "ADULT"}]` |
| `passengers[].type` | enum | `ADULT` \| `CHILD` \| `INFANT` | `ADULT` |
| `service_class` | enum | `ECONOMY` \| `BUSINESS` | `ECONOMY` |

```json
{
  "origin": "TAS",
  "destination": "IST",
  "departure_date": "2026-10-20",
  "passengers": [{"type": "ADULT"}],
  "service_class": "ECONOMY"
}
```

**202 Accepted** (`SearchStartedOut`) — не готово, опрашивайте статус:

```json
{
  "search_id": "6c2f9a3e-1b47-4c8e-9f0a-2d5b7e8c1a90",
  "status": "pending",
  "expires_at": "2026-09-18T12:30:00Z"
}
```

`expires_at` = сейчас + `SEARCH_SESSION_TTL_SECONDS` (900 с).

**Ошибки:** `401`; `422` (`VALIDATION_ERROR`, бизнес-валидация): `origin == destination`, `departure_date` в прошлом, `return_date` раньше `departure_date`, неизвестный IATA-код (`"Unknown origin airport code: XXX"`).

---

## GET /travel/avia/offers/search/{search_id}

Поллинг статуса поиска.

- **Авторизация:** требуется. Сессия принадлежит создавшему её агенту: чужой `search_id` → `403`.

**Семантика поллинга:**

- `status` = `pending` → поиск ещё выполняется, `items` **всегда пуст**.
- `status` = `done` → готово, `items` содержит все найденные офферы, `items_found` = их количество.
- `status` = `failed` → поиск не удался; причина — в `error` (`"provider_timeout"`, `"deadline_exceeded"` либо `"provider_error: ..."`).
- Сессия, чей фоновый воркер погиб, не завершившись, лениво переводится в `failed` при очередном поллинге, если её возраст превысил `SEARCH_DEADLINE_SECONDS` (30 с) + 5 с льготы.
- Если текущее время больше `expires_at`, вместо тела возвращается `404 SEARCH_EXPIRED`.

**200** (`SearchStatusOut`):

```json
{
  "search_id": "6c2f9a3e-1b47-4c8e-9f0a-2d5b7e8c1a90",
  "status": "done",
  "items_found": 2,
  "items": [
    {
      "offer_id": "of_9f2c1a4b3d5e00",
      "origin": "TAS",
      "destination": "IST",
      "validating_carrier": "TK",
      "price": "264.30",
      "currency": "USD",
      "service_class": "ECONOMY",
      "refundable": false,
      "seats_left": 4,
      "segments": [
        {
          "origin": "TAS",
          "destination": "IST",
          "flight_number": "TK368",
          "carrier_code": "TK",
          "carrier_name": "Turkish Airlines",
          "departure": "2026-10-20T05:35:00Z",
          "arrival": "2026-10-20T08:20:00Z",
          "duration_minutes": 225,
          "aircraft": "A321"
        }
      ],
      "baggage": {
        "cabin": {"kg": 8, "pieces": 1},
        "checked": {"kg": 20, "pieces": 1}
      }
    }
  ],
  "expires_at": "2026-09-18T12:30:00Z",
  "error": null
}
```

`offer_id` имеет вид `of_<12 hex-символов><двузначный индекс>` (офферы отсортированы по возрастанию цены). У сессии в статусе `pending` поле `items` — пустой массив, `error` — `null`.

**Ошибки:** `401`; `403` (сессия другого агента); `404` (сессия не найдена или истекла — `SEARCH_EXPIRED`); `422` (невалидный UUID).

---

## GET /travel/avia/offers/{offer_id}

Детали оффера: семейства тарифов, правила применения, срок жизни. Кэшируется в Redis (ключ `offer:{offer_id}`, TTL `SEARCH_SESSION_TTL_SECONDS`).

- **Авторизация:** требуется.

**Параметры:** `offer_id` в пути; идентификаторы, не начинающиеся с `of_`, сразу дают `404`.

**200** (`OfferDetailOut`) — все поля компактного оффера (см. выше) плюс:

```json
{
  "offer_id": "of_9f2c1a4b3d5e00",
  "origin": "TAS",
  "destination": "IST",
  "validating_carrier": "TK",
  "price": "264.30",
  "currency": "USD",
  "service_class": "ECONOMY",
  "refundable": false,
  "seats_left": 4,
  "segments": ["..."],
  "baggage": {"cabin": {"kg": 8, "pieces": 1}, "checked": {"kg": 20, "pieces": 1}},
  "fare_families": [
    {
      "name": "ECONOMY LITE",
      "service_class": "ECONOMY",
      "price": "264.30",
      "refundable": false,
      "exchangeable": false,
      "baggage": {"cabin": {"kg": 8, "pieces": 1}, "checked": {"kg": 0, "pieces": 0}},
      "seats_left": 4,
      "fare_rules": [
        "Name changes are not permitted after ticketing.",
        "No-show: the full ticket value is forfeited."
      ]
    },
    {
      "name": "ECONOMY STANDARD",
      "price": "321.30",
      "refundable": true,
      "exchangeable": true,
      "baggage": {"cabin": {"kg": 8, "pieces": 1}, "checked": {"kg": 20, "pieces": 1}},
      "seats_left": 3
    },
    {
      "name": "ECONOMY FLEX",
      "price": "397.30",
      "refundable": true,
      "exchangeable": true,
      "baggage": {"cabin": {"kg": 8, "pieces": 1}, "checked": {"kg": 23, "pieces": 2}},
      "seats_left": 2
    }
  ],
  "fare_rules": ["...правила STANDARD-семейства..."],
  "offer_expires_at": "2026-09-18T12:30:00Z"
}
```

В примере выше показаны типичные значения мок-провайдера: три семейства `LITE` / `STANDARD` / `FLEX` (STANDARD и FLEX дороже базы, возвратные и обменные). `price` семейства — полная цена (не надбавка). `offer_expires_at` — срок, до которого оффер можно забронировать; после него создание заказа вернёт `410`.

**Ошибки:** `401`; `404` (оффер неизвестен/не распознан); `502 PROVIDER_ERROR`, `504 PROVIDER_TIMEOUT` (сбой вызова провайдера, детали оффера не закэшированы).

---

## POST /travel/avia/orders

Бронирование оффера: оффер запрашивается у провайдера заново, ценается по пассажирам и сохраняется как заказ `BOOKED` с неизменяемым снапшотом (`offer_snapshot`, `contact`).

- **Авторизация:** требуется.
- **Идемпотентность:** заголовок `Idempotency-Key` (необязателен, до 128 символов).

**Заголовки:**

| Заголовок | Обязателен | Смысл |
|---|---|---|
| `Idempotency-Key` | нет | ключ повтора: ретрай с тем же ключом и тем же телом вернёт исходный ответ, не создав второй заказ |

**Запрос** (`BookingCreateIn`):

| Поле | Тип | Ограничения |
|---|---|---|
| `offer_id` | string | 1–64 символа, из ответов поиска/деталей |
| `passengers` | array | 1–9 элементов `PassengerIn` |
| `passengers[].type` | enum | `ADULT` \| `CHILD` \| `INFANT`, по умолчанию `ADULT` |
| `passengers[].first_name` | string | 1–64, только латиница, паттерн `^[A-Za-z]+(?:[ -][A-Za-z]+)*$` (пробелы по краям срезаются) |
| `passengers[].last_name` | string | 1–64, тот же паттерн |
| `passengers[].date_of_birth` | date | не в будущем |
| `passengers[].gender` | enum \| null | `MALE` \| `FEMALE` |
| `passengers[].citizenship` | string \| null | 2–3 латинские буквы |
| `passengers[].doc_type` | enum | `PASSPORT` \| `ID_CARD`, по умолчанию `PASSPORT` |
| `passengers[].doc_number` | string | 4–32 символа |
| `contact.email` | string (email) | валидный e-mail |
| `contact.phone` | string \| null | — |

```json
{
  "offer_id": "of_9f2c1a4b3d5e00",
  "passengers": [
    {
      "type": "ADULT",
      "first_name": "IVAN",
      "last_name": "IVANOV",
      "date_of_birth": "1990-05-14",
      "gender": "MALE",
      "citizenship": "UZ",
      "doc_type": "PASSPORT",
      "doc_number": "AB123456"
    }
  ],
  "contact": {"email": "agent@demo.uz", "phone": "+998901234567"}
}
```

**Ценообразование:** цена оффера × коэффициент пассажира — `ADULT` 1.00, `CHILD` 0.75, `INFANT` 0.10; округление `ROUND_HALF_UP` до цента; `total_amount` — сумма по всем пассажирам.

**201 Created** (`OrderOut`):

```json
{
  "id": "0e8d7c6b-5a49-4837-9210-fecdba987654",
  "status": "BOOKED",
  "offer_id": "of_9f2c1a4b3d5e00",
  "total_amount": "264.30",
  "currency": "USD",
  "passengers": [
    {
      "id": "11c2b3a4-95f6-4a70-8b1c-223344556677",
      "type": "ADULT",
      "first_name": "IVAN",
      "last_name": "IVANOV",
      "date_of_birth": "1990-05-14",
      "gender": "MALE",
      "citizenship": "UZ",
      "doc_type": "PASSPORT",
      "doc_number": "AB123456",
      "fare_amount": "264.30"
    }
  ],
  "route": {
    "origin": "TAS",
    "destination": "IST",
    "validating_carrier": "TK",
    "departure": "2026-10-20T05:35:00Z",
    "arrival": "2026-10-20T08:20:00Z"
  },
  "ticket_number": null,
  "created_at": "2026-09-18T12:18:44.123456Z",
  "issued_at": null
}
```

`route` собирается из первого/последнего сегментов снапшота. Повторный запрос с тем же `Idempotency-Key` и тем же телом вернёт **тот же** `201` с сохранённым телом и заголовком `Idempotency-Replayed: true`.

**Ошибки:**

| Код | HTTP | Причина |
|---|---|---|
| `UNAUTHORIZED` | 401 | нет/просрочен токен |
| `NOT_FOUND` | 404 | оффер неизвестен провайдеру |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | параллельный запрос с тем же ключом ещё выполняется |
| `IDEMPOTENCY_KEY_REUSED` | 409 | ключ уже использован с другим телом |
| `OFFER_NOT_AVAILABLE` | 410 | срок жизни оффера истёк (`offer_expires_at`) |
| `VALIDATION_ERROR` | 422 | бизнес-валидация |
| `PROVIDER_ERROR` / `PROVIDER_TIMEOUT` | 502 / 504 | провайдер не отдал детали оффера |

---

## POST /travel/avia/orders/{order_id}/issue

Выписка заказа: атомарное списание баланса агента, присвоение номера электронного билета, запись в ledger и постановка задачи на PDF.

- **Авторизация:** требуется (только владелец заказа).
- **Идемпотентность:** заголовок `Idempotency-Key` (необязателен) **плюс естественная**: повторный вызов по уже-ISSUED заказу возвращает результат без повторного списания — даже без заголовка.

**Заголовки:** `Idempotency-Key` — ключ хранится в PostgreSQL атомарным claim (`INSERT ... ON CONFLICT DO NOTHING`); повтор с тем же ключом после успеха вернёт сохранённое тело с `Idempotency-Replayed: true`; параллельный вызов с тем же ключом — `409 IDEMPOTENCY_IN_PROGRESS`; ошибка исходной попытки помечает ключ `FAILED` и разрешает повтор.

**200 OK** (`IssueOut`):

```json
{
  "id": "0e8d7c6b-5a49-4837-9210-fecdba987654",
  "status": "ISSUED",
  "ticket_number": "2321234567890",
  "debited_amount": "264.30",
  "balance_after": "9735.70",
  "currency": "USD",
  "issued_at": "2026-09-18T12:20:01.987654Z"
}
```

`ticket_number` — 13 цифр (префикс формы `232` + 10 цифр, производных от UUID заказа), глобально уникален. Списание и смена статуса выполняются в одной транзакции под `SELECT ... FOR UPDATE`; каждая выписка сопровождается записью `DEBIT` в `balance_transactions` с `UNIQUE(order_id, txn_type)` — двойное списание исключено на уровне БД. PDF генерируется асинхронно уже после ответа.

**Ошибки:**

| Код | HTTP | Причина |
|---|---|---|
| `UNAUTHORIZED` | 401 | нет/просрочен токен |
| `INSUFFICIENT_FUNDS` | 402 | баланса не хватает; `details` содержит суммы: `{"error": {"code": "INSUFFICIENT_FUNDS", "message": "Agent balance is insufficient to issue this order", "details": {"order_amount": "264.30", "balance": "100.00"}}}`. Ключ помечается `FAILED` — после пополнения баланса повтор тем же ключом сработает |
| `FORBIDDEN` | 403 | заказ другого агента |
| `NOT_FOUND` | 404 | заказ не найден |
| `INVALID_ORDER_STATUS` | 409 | заказ не в статусе `BOOKED` (повтор по ISSUED-заказу ошибкой не является — см. выше) |
| `CURRENCY_MISMATCH` | 409 | валюта заказа не совпадает с валютой счёта агента |
| `IDEMPOTENCY_IN_PROGRESS` / `IDEMPOTENCY_KEY_REUSED` | 409 | конфликты ключа повтора |
| `VALIDATION_ERROR` | 422 | невалидный UUID заказа |

---

## GET /travel/avia/orders/{order_id}

Заказ с полной хронологией статусов.

- **Авторизация:** требуется (только владелец).

**200** (`OrderStatusOut`) — все поля `OrderOut` (см. создание заказа) плюс `history` (упорядочена по времени):

```json
{
  "id": "0e8d7c6b-5a49-4837-9210-fecdba987654",
  "status": "ISSUED",
  "offer_id": "of_9f2c1a4b3d5e00",
  "total_amount": "264.30",
  "currency": "USD",
  "passengers": ["...как при создании..."],
  "route": {"origin": "TAS", "destination": "IST", "validating_carrier": "TK",
            "departure": "2026-10-20T05:35:00Z", "arrival": "2026-10-20T08:20:00Z"},
  "ticket_number": "2321234567890",
  "created_at": "2026-09-18T12:18:44.123456Z",
  "issued_at": "2026-09-18T12:20:01.987654Z",
  "history": [
    {"from_status": null, "to_status": "BOOKED", "reason": null, "actor": "agent",
     "created_at": "2026-09-18T12:18:44.123456Z"},
    {"from_status": "BOOKED", "to_status": "ISSUED", "reason": null, "actor": "agent",
     "created_at": "2026-09-18T12:20:01.987654Z"}
  ]
}
```

**Ошибки:** `401`; `403` (заказ другого агента); `404` (не найден); `422` (невалидный UUID).

---

## GET /travel/avia/orders/{order_id}/ticket

PDF-билет выпущенного заказа.

- **Авторизация:** требуется (только владелец).

**Поведение:**

1. Заказ не в статусе `ISSUED` → `409 TICKET_NOT_READY`:

   ```json
   {"error": {"code": "TICKET_NOT_READY", "message": "Ticket is issued only after order is ISSUED"}}
   ```

2. PDF уже сгенерирован → **200**, `application/pdf`, файл `ticket_{ticket_number}.pdf`:

   ```text
   Content-Type: application/pdf
   Content-Disposition: attachment; filename="ticket_2321234567890.pdf"
   ```

3. PDF ещё не готов → **202 Accepted** с заголовком `Retry-After: 2` — повторите запрос через пару секунд:

   ```json
   {"status": "generating", "detail": "PDF is being generated, retry shortly"}
   ```

Генерация выполняется воркером из очереди RabbitMQ (при недоступном брокере — синхронно в API-процессе, `TICKET_FALLBACK_SYNC=true`), файл пишется атомарно (`.tmp` → `os.replace`).

**Ошибки:** `401`; `403`; `404`; `409` (`TICKET_NOT_READY`); `422` (невалидный UUID).

---

## Пробы здоровья (без авторизации и префикса /travel)

### GET /health/live

```json
{"status": "alive"}
```

### GET /health/ready

Проверяет доступность PostgreSQL и Redis.

**200:**

```json
{"status": "ready", "checks": {"postgres": true, "redis": true}}
```

**503** (если хотя бы одна зависимость недоступна):

```json
{"status": "degraded", "checks": {"postgres": true, "redis": false}}
```

---

## Сводка кодов ошибок

| HTTP | `code` | Где возникает |
|---|---|---|
| 401 | `UNAUTHORIZED` | нет Bearer-токена, токен просрочен/невалиден, неверные креды логина, аккаунт отключён |
| 402 | `INSUFFICIENT_FUNDS` | выписка при недостаточном балансе |
| 403 | `FORBIDDEN` | чужой заказ/сессия поиска |
| 404 | `NOT_FOUND` | неизвестный заказ, оффер или сессия поиска |
| 404 | `SEARCH_EXPIRED` | TTL сессии поиска истёк |
| 409 | `CONFLICT` | базовый класс конфликтов состояния |
| 409 | `IDEMPOTENCY_IN_PROGRESS` | параллельный запрос с тем же `Idempotency-Key` |
| 409 | `IDEMPOTENCY_KEY_REUSED` | ключ повторно использован с другим payload |
| 409 | `INVALID_ORDER_STATUS` | выписка не-BOOKED заказа |
| 409 | `CURRENCY_MISMATCH` | валюта заказа ≠ валюта счёта |
| 409 | `TICKET_NOT_READY` | PDF запрошен до выписки |
| 410 | `OFFER_NOT_AVAILABLE` | бронирование истёкшего оффера |
| 422 | `VALIDATION_ERROR` | бизнес-валидация (схемная — в формате FastAPI `{"detail": [...]}`) |
| 500 | `INTERNAL_ERROR` | необработанное исключение |
| 502 | `PROVIDER_ERROR` | сбой avia-провайдера |
| 504 | `PROVIDER_TIMEOUT` | таймаут avia-провайдера |
