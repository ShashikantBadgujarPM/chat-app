"""Helpers for API tests that need a real database and authentication."""

from dataclasses import dataclass
from itertools import count

import httpx

CSRF = {"X-Requested-With": "chat-app"}
PASSWORD = "correct horse battery staple"

_sequence = count(1)


@dataclass
class RegisteredUser:
    id: str
    username: str
    email: str
    password: str


async def register(client: httpx.AsyncClient, **overrides: str) -> RegisteredUser:
    n = next(_sequence)
    body = {
        "username": f"user{n}",
        "email": f"user{n}@example.com",
        "display_name": f"User {n}",
        "password": PASSWORD,
    }
    body.update(overrides)
    response = await client.post("/api/v1/auth/register", json=body)
    assert response.status_code == 201, response.text
    return RegisteredUser(
        id=response.json()["id"],
        username=body["username"],
        email=body["email"],
        password=body["password"],
    )


async def login(
    client: httpx.AsyncClient, user: RegisteredUser, *, password: str | None = None
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": user.username, "password": password or user.password},
    )


async def login_token(client: httpx.AsyncClient, user: RegisteredUser) -> str:
    response = await login(client, user)
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def refresh_cookie(response: httpx.Response) -> str:
    value = response.cookies.get("rt")
    assert value, f"no refresh cookie in {response.headers.get_list('set-cookie')}"
    return value
