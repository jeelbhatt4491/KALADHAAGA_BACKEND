from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.errors import AppError


class _Login(BaseModel):
    phone: str
    password: str


def _add_routes(app: FastAPI) -> None:
    @app.get("/_test/app-error")
    async def app_error() -> None:
        raise AppError("invalid_otp", status_code=400, attempt=2, max_attempts=5)

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("unexpected")

    @app.post("/_test/login")
    async def login(body: _Login) -> dict[str, str]:
        return {"ok": "yes"}


async def test_app_error_shape_matches_app_contract(app: FastAPI) -> None:
    _add_routes(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.get("/_test/app-error")

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_otp"
    assert body["attempt"] == 2 and body["max_attempts"] == 5
    assert body["request_id"] == response.headers["x-request-id"]


async def test_unhandled_exception_returns_500_with_request_id(app: FastAPI) -> None:
    _add_routes(app)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as http:
        response = await http.get("/_test/crash")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["request_id"]


async def test_validation_error_never_echoes_input(app: FastAPI) -> None:
    _add_routes(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.post("/_test/login", json={"phone": "+919876543210"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "+919876543210" not in response.text


async def test_unknown_route_is_not_found(client: AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
