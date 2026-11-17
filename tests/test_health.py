from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["env"] == "test"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"
    assert len(response.headers["x-request-id"]) == 32


async def test_health_reports_degraded_when_redis_is_down(
    app: FastAPI, client: AsyncClient
) -> None:
    async def broken_ping() -> None:
        raise ConnectionError("redis unreachable")

    app.state.redis.ping = broken_ping

    response = await client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"]["status"] == "error"
    assert body["checks"]["database"]["status"] == "ok"


async def test_incoming_request_id_is_kept(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "app-req-12345678"})

    assert response.headers["x-request-id"] == "app-req-12345678"


async def test_api_docs_are_served(client: AsyncClient) -> None:
    assert (await client.get("/docs")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert "/health" in schema["paths"]
