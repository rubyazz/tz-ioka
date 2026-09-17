# Ручное тестирование API (curl)

Подготовка данных (идемпотентно; `--force` — сначала очистить заказы/леджер):

```bash
docker compose exec api python -m scripts.seed_demo --force
```

Создаёт агентов: `agent/agent123` (баланс 10 000), `poor-agent/poor123` (баланс 50),
`disabled-agent/disabled123` (отключён) и заказы во всех состояниях.

> Ниже подставлены реальные id из последнего сиянья — после `--force` замените на свои
> (скрипт печатает их в конце).

---

## 1. Аутентификация

```bash
BASE=http://localhost:8000/travel

# основной агент
TOKEN=$(curl -s -X POST $BASE/auth/agent/login -H 'Content-Type: application/json' \
  -d '{"username":"agent","password":"agent123"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# «бедный» агент (для 402)
POOR_TOKEN=$(curl -s -X POST $BASE/auth/agent/login -H 'Content-Type: application/json' \
  -d '{"username":"poor-agent","password":"poor123"}' | jq -r .access_token)
POOR_AUTH="Authorization: Bearer $POOR_TOKEN"

# отключённый агент -> 401 UNAUTHORIZED
curl -s -X POST $BASE/auth/agent/login -H 'Content-Type: application/json' \
  -d '{"username":"disabled-agent","password":"disabled123"}' -w '\nhttp=%{http_code}\n'

# неверный пароль -> 401; ответ с полным профилем и балансом:
curl -s -X POST $BASE/auth/agent/login -H 'Content-Type: application/json' \
  -d '{"username":"agent","password":"agent123"}' | jq .agent
```

## 2. Справочник локаций (IATA)

```bash
curl -s "$BASE/avia/locations?query=Tashkent&limit=5" -H "$AUTH" | jq '.[].code'   # TAS
curl -s "$BASE/avia/locations?query=istanbul" -H "$AUTH" | jq '.[].code'           # IST, SAW
curl -s "$BASE/avia/locations?query=T" -H "$AUTH" -w '\nhttp=%{http_code}\n'       # 422 (мин. 2 символа)
curl -s "$BASE/avia/locations?query=TAS" -w '\nhttp=%{http_code}\n'                # 401 без токена
```

## 3. Поиск + поллинг

```bash
# 202 Accepted + search_id
SEARCH_ID=$(curl -s -X POST $BASE/avia/offers -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"origin":"TAS","destination":"IST","departure_date":"2026-12-15",
       "passengers":[{"type":"ADULT"},{"type":"CHILD"}],"service_class":"ECONOMY"}' \
  | jq -r .search_id)

# поллинг до status="done" (мок-провайдер отвечает за 1.5–3 с)
curl -s "$BASE/avia/offers/search/$SEARCH_ID" -H "$AUTH" | jq '.status, .items_found'
curl -s "$BASE/avia/offers/search/$SEARCH_ID" -H "$AUTH" | jq '.items[0]'

# ошибки валидации -> 422
curl -s -X POST $BASE/avia/offers -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"origin":"TAS","destination":"TAS","departure_date":"2026-12-15"}' -w '\nhttp=%{http_code}\n'
curl -s -X POST $BASE/avia/offers -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"origin":"ZZZ","destination":"IST","departure_date":"2026-12-15"}' -w '\nhttp=%{http_code}\n'

# чужая поисковая сессия -> 403
curl -s "$BASE/avia/offers/search/$SEARCH_ID" -H "$POOR_AUTH" -w '\nhttp=%{http_code}\n'
```

## 4. Детали оффера

```bash
OFFER_ID=$(curl -s "$BASE/avia/offers/search/$SEARCH_ID" -H "$AUTH" | jq -r '.items[0].offer_id')
curl -s "$BASE/avia/offers/$OFFER_ID" -H "$AUTH" | jq '{price, currency, validating_carrier,
  fare_families: [.fare_families[].name], baggage, offer_expires_at}'
curl -s "$BASE/avia/offers/of_00000000000099" -H "$AUTH" -w '\nhttp=%{http_code}\n'   # 404
```

## 5. Создание заказа

```bash
BODY='{"offer_id":"'$OFFER_ID'","passengers":[
  {"type":"ADULT","first_name":"IVAN","last_name":"IVANOV","date_of_birth":"1990-05-14",
   "gender":"MALE","citizenship":"UZ","doc_type":"PASSPORT","doc_number":"AB1234567"}],
  "contact":{"email":"agent@demo.ioka.uz","phone":"+998901112233"}}'

# 201 BOOKED
ORDER_ID=$(curl -s -X POST $BASE/avia/orders -H "$AUTH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: manual-$(date +%s)" -d "$BODY" | jq -r .id)

# идемпотентный повтор: тот же id + заголовок Idempotency-Replayed: true
curl -s -X POST $BASE/avia/orders -H "$AUTH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: manual-$(date +%s)" -d "$BODY" -D - -o /dev/null | grep -i replayed

# тот же ключ с другим payload -> 409 IDEMPOTENCY_KEY_REUSED
curl -s -X POST $BASE/avia/orders -H "$AUTH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: reuse-demo" -d "$BODY" > /dev/null
curl -s -X POST $BASE/avia/orders -H "$AUTH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: reuse-demo" -d "${BODY/IVAN/PETR}" -w '\nhttp=%{http_code}\n'
```

## 6. Выписка (списание баланса)

```bash
# существующий BOOKED-заказ из сиянья (замените на свой):
ORDER_ID=4cdd30cb-134c-43c0-91ab-2fb0285ad738

# 200 ISSUED + ticket_number (13 цифр, префикс 232) + balance_after
curl -s -X POST "$BASE/avia/orders/$ORDER_ID/issue" -H "$AUTH" \
  -H "Idempotency-Key: issue-$(date +%s)" | jq

# повтор БЕЗ ключа -> 200, тот же ticket_number, второго списания нет
curl -s -X POST "$BASE/avia/orders/$ORDER_ID/issue" -H "$AUTH" | jq '{ticket_number, balance_after}'

# нехватка средств (poor-agent, баланс 50 < 482.03) -> 402 INSUFFICIENT_FUNDS + details
POOR_ORDER=528f296f-e61f-4f46-b2fa-ee0642581316
curl -s -X POST "$BASE/avia/orders/$POOR_ORDER/issue" -H "$POOR_AUTH" -w '\nhttp=%{http_code}\n'

# выписка уже ISSUED-заказа -> 409 INVALID_ORDER_STATUS
ISSUED=5cebcc32-c9e5-4a8a-a6e1-9b1f241dcb60
curl -s -X POST "$BASE/avia/orders/$ISSUED/issue" -H "$AUTH" -w '\nhttp=%{http_code}\n'
```

## 7. Статус заказа + история

```bash
curl -s "$BASE/avia/orders/$ORDER_ID" -H "$AUTH" | jq '{status, total_amount, ticket_number,
  history: [.history[] | {from_status, to_status, actor}] }'

curl -s "$BASE/avia/orders/00000000-0000-0000-0000-000000000001" -H "$AUTH" \
  -w '\nhttp=%{http_code}\n'                                                          # 404
curl -s "$BASE/avia/orders/$POOR_ORDER" -H "$AUTH" -w '\nhttp=%{http_code}\n'         # 403 (чужой)
```

## 8. PDF-билет

```bash
# ISSUED-заказ без файла: 1-й вызов 202 (воркер генерирует), 2-й — 200 application/pdf
curl -s "$BASE/avia/orders/$ISSUED/ticket" -H "$AUTH" -w '\nhttp=%{http_code}\n' -o /dev/null
sleep 2
curl -s "$BASE/avia/orders/$ISSUED/ticket" -H "$AUTH" -o ticket.pdf -w 'http=%{http_code} '
file ticket.pdf    # PDF document

# BOOKED-заказ -> 409 TICKET_NOT_READY
curl -s "$BASE/avia/orders/bd6ee281-f5ce-424a-8a49-4bf7889e4f4d/ticket" -H "$AUTH" \
  -w '\nhttp=%{http_code}\n'
```

## 9. Health (без /travel и без токена)

```bash
curl -s http://localhost:8000/health/live
curl -s http://localhost:8000/health/ready
```

## Аудит в БД (опционально)

```bash
docker compose exec postgres psql -U ioka -d ioka_travel -c \
  "SELECT provider, operation, success, latency_ms FROM provider_call_logs ORDER BY created_at DESC LIMIT 5;"
docker compose exec postgres psql -U ioka -d ioka_travel -c \
  "SELECT txn_type, amount, balance_after, note FROM balance_transactions ORDER BY created_at DESC LIMIT 5;"
```
