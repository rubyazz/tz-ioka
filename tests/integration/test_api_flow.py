"""Full API flow against the real app: auth → search → poll → book → issue → PDF.

RabbitMQ is deliberately unreachable in the test environment, so issuing
falls back to synchronous PDF generation and the ticket endpoint returns the
finished file immediately (200) instead of 202.
"""

import asyncio
import time
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Agent, Order

PASSENGER = {
    "type": "ADULT",
    "first_name": "IVAN",
    "last_name": "IVANOV",
    "date_of_birth": "1990-05-14",
    "gender": "MALE",
    "citizenship": "UZ",
    "doc_type": "PASSPORT",
    "doc_number": "AB1234567",
}


async def start_and_wait_search(client: AsyncClient, origin="TAS", dest="IST") -> dict:
    response = await client.post(
        "/travel/avia/offers",
        json={
            "origin": origin,
            "destination": dest,
            "departure_date": "2026-10-20",
            "passengers": [{"type": "ADULT"}],
        },
    )
    assert response.status_code == 202, response.text
    search_id = response.json()["search_id"]

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        poll = await client.get(f"/travel/avia/offers/search/{search_id}")
        assert poll.status_code == 200, poll.text
        body = poll.json()
        if body["status"] == "done":
            return body
        assert body["status"] == "pending"
        await asyncio.sleep(0.1)
    pytest.fail("search did not finish in 10s")


async def book(client: AsyncClient, offer_id: str, key: str | None = None) -> dict:
    headers = {"Idempotency-Key": key} if key else {}
    response = await client.post(
        "/travel/avia/orders",
        json={
            "offer_id": offer_id,
            "passengers": [PASSENGER],
            "contact": {"email": "agent@test.uz", "phone": "+998901112233"},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- auth -----------------------------------------------------------------


async def test_login_ok(client: AsyncClient, agent: Agent) -> None:
    response = await client.post(
        "/travel/auth/agent/login",
        json={"username": agent.username, "password": "pass1234"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["agent"]["balance"] == "1000.00"
    assert body["expires_in"] > 0


async def test_login_wrong_password(client: AsyncClient, agent: Agent) -> None:
    response = await client.post(
        "/travel/auth/agent/login",
        json={"username": agent.username, "password": "nope"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_endpoints_require_token() -> None:
    from httpx import ASGITransport
    from httpx import AsyncClient as AC

    from app.main import create_app

    async with AC(transport=ASGITransport(app=create_app()), base_url="http://test") as anon:
        response = await anon.get("/travel/avia/locations?query=TAS")
        assert response.status_code == 401
        response = await anon.post(
            "/travel/avia/orders", json={"offer_id": "of_x", "passengers": [], "contact": {}}
        )
        assert response.status_code in (401, 422)  # auth is checked first


# --- locations & search ---------------------------------------------------


async def test_locations_lookup(client: AsyncClient) -> None:
    response = await client.get("/travel/avia/locations", params={"query": "Tashkent"})
    assert response.status_code == 200
    codes = [item["code"] for item in response.json()]
    assert "TAS" in codes

    short = await client.get("/travel/avia/locations", params={"query": "T"})
    assert short.status_code == 422


async def test_search_validation(client: AsyncClient) -> None:
    same = await client.post(
        "/travel/avia/offers",
        json={"origin": "TAS", "destination": "TAS", "departure_date": "2026-10-20"},
    )
    assert same.status_code == 422

    unknown = await client.post(
        "/travel/avia/offers",
        json={"origin": "ZZZ", "destination": "IST", "departure_date": "2026-10-20"},
    )
    assert unknown.status_code == 422


async def test_search_poll_and_offer_detail(client: AsyncClient) -> None:
    result = await start_and_wait_search(client)
    assert result["items_found"] == len(result["items"])
    assert result["items"]

    offer = result["items"][0]
    assert isinstance(offer["price"], str)
    assert offer["segments"][0]["origin"] == "TAS"
    assert offer["segments"][-1]["destination"] == "IST"

    detail = await client.get(f"/travel/avia/offers/{offer['offer_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert len(body["fare_families"]) == 3
    assert isinstance(body["fare_families"][0]["price"], str | None)

    bogus = await client.get("/travel/avia/offers/of_00000000000099")
    assert bogus.status_code == 404


# --- booking & issuing ----------------------------------------------------


async def test_full_flow_book_issue_ticket(client: AsyncClient) -> None:
    result = await start_and_wait_search(client, origin="TAS", dest="IST")
    offer_id = result["items"][0]["offer_id"]

    order = await book(client, offer_id, key="flow-1")
    assert order["status"] == "BOOKED"
    assert isinstance(order["total_amount"], str)
    order_id = order["id"]

    status = await client.get(f"/travel/avia/orders/{order_id}")
    assert status.status_code == 200
    history = status.json()["history"]
    assert [(h["from_status"], h["to_status"]) for h in history] == [(None, "BOOKED")]

    ticket_early = await client.get(f"/travel/avia/orders/{order_id}/ticket")
    assert ticket_early.status_code == 409
    assert ticket_early.json()["error"]["code"] == "TICKET_NOT_READY"

    issue = await client.post(f"/travel/avia/orders/{order_id}/issue")
    assert issue.status_code == 200, issue.text
    issued = issue.json()
    assert issued["status"] == "ISSUED"
    assert issued["ticket_number"].startswith("232")
    assert issued["debited_amount"] == order["total_amount"]

    # idempotent re-issue without a key: same ticket, no second debit
    reissue = await client.post(f"/travel/avia/orders/{order_id}/issue")
    assert reissue.status_code == 200
    assert reissue.json()["ticket_number"] == issued["ticket_number"]

    # PDF: broker unreachable in tests -> generated synchronously on issue
    ticket = await client.get(f"/travel/avia/orders/{order_id}/ticket")
    assert ticket.status_code == 200
    assert ticket.headers["content-type"].startswith("application/pdf")
    assert ticket.content[:4] == b"%PDF"


async def test_booking_idempotency_replay_and_reuse(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    result = await start_and_wait_search(client)
    offer_id = result["items"][0]["offer_id"]

    first = await book(client, offer_id, key="idem-1")
    replay_response = await client.post(
        "/travel/avia/orders",
        json={
            "offer_id": offer_id,
            "passengers": [PASSENGER],
            "contact": {"email": "agent@test.uz", "phone": "+998901112233"},
        },
        headers={"Idempotency-Key": "idem-1"},
    )
    assert replay_response.status_code == 201
    assert replay_response.headers.get("Idempotency-Replayed") == "true"
    assert replay_response.json()["id"] == first["id"]

    from sqlalchemy import func

    async with factory() as session:
        count = (await session.execute(select(func.count()).select_from(Order))).scalar()
        assert count == 1  # replay did not create a second order

    # same key with a DIFFERENT payload -> rejected
    other_payload = dict(PASSENGER)
    other_payload["doc_number"] = "ZZ9999999"
    reused = await client.post(
        "/travel/avia/orders",
        json={
            "offer_id": offer_id,
            "passengers": [other_payload],
            "contact": {"email": "agent@test.uz"},
        },
        headers={"Idempotency-Key": "idem-1"},
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_issue_insufficient_funds_then_retry_after_topup(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    result = await start_and_wait_search(client)
    order = await book(client, result["items"][0]["offer_id"])

    async with factory() as session:
        row = await session.get(Agent, agent.id)
        row.balance = Decimal("1.00")
        await session.commit()

    rejected = await client.post(
        f"/travel/avia/orders/{order['id']}/issue", headers={"Idempotency-Key": "low-1"}
    )
    assert rejected.status_code == 402
    error = rejected.json()["error"]
    assert error["code"] == "INSUFFICIENT_FUNDS"
    assert error["details"]["balance"] == "1.00"

    status = await client.get(f"/travel/avia/orders/{order['id']}")
    assert status.json()["status"] == "BOOKED"  # unaffected

    async with factory() as session:
        row = await session.get(Agent, agent.id)
        row.balance = Decimal("5000.00")
        await session.commit()

    retried = await client.post(
        f"/travel/avia/orders/{order['id']}/issue", headers={"Idempotency-Key": "low-1"}
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "ISSUED"


async def test_ownership_is_enforced(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    from app.core.security import create_access_token, hash_password

    result = await start_and_wait_search(client)
    order = await book(client, result["items"][0]["offer_id"])
    search_id = result["search_id"]

    async with factory() as session:
        intruder = Agent(
            username=f"intruder-{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:8]}@test.uz",
            password_hash=hash_password("pass1234"),
            balance=Decimal("100.00"),
        )
        session.add(intruder)
        await session.commit()
        token, _ = create_access_token(intruder.id)

    headers = {"Authorization": f"Bearer {token}"}
    foreign_order = await client.get(f"/travel/avia/orders/{order['id']}", headers=headers)
    assert foreign_order.status_code == 403
    assert foreign_order.json()["error"]["code"] == "FORBIDDEN"

    foreign_search = await client.get(f"/travel/avia/offers/search/{search_id}", headers=headers)
    assert foreign_search.status_code == 403


async def test_unknown_order_is_404(client: AsyncClient) -> None:
    response = await client.get(f"/travel/avia/orders/{uuid.uuid4()}")
    assert response.status_code == 404
