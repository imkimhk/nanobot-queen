"""Unit tests for the Queen Model Gateway: key validation, concurrency, 429 backoff."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from nanobot.providers.base import LLMResponse
from nanobot.queen.gateway import create_app

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

pytestmark = pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp test utils unavailable")

KEYS = {"poc-key": "sub1"}
BODY = {
    "model": "openai-codex/gpt-5.5",
    "messages": [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ],
}


class FakeProvider:
    """Records calls and returns scripted LLMResponses."""

    def __init__(self, responses=None, *, default=None):
        self.calls = 0
        self.responses = list(responses or [])
        self.default = default or LLMResponse(
            content="OK", finish_reason="stop",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )

    async def chat(self, messages=None, tools=None, model=None, **kw):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return self.default


class BlockingProvider:
    """Blocks inside chat() until released, signalling entry — for concurrency tests."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def chat(self, messages=None, tools=None, model=None, **kw):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return LLMResponse(content="OK", finish_reason="stop",
                           usage={"total_tokens": 1})


@pytest_asyncio.fixture
async def client_factory():
    clients: list[TestClient] = []

    async def _make(app):
        c = TestClient(TestServer(app))
        await c.start_server()
        clients.append(c)
        return c

    try:
        yield _make
    finally:
        for c in clients:
            await c.close()


def _auth(key="poc-key"):
    return {"Authorization": f"Bearer {key}"}


# --- key validation: invalid key blocked BEFORE upstream -------------------


async def test_invalid_key_blocked_before_upstream(client_factory):
    provider = FakeProvider()
    app = create_app(provider=provider, keys=KEYS)
    client = await client_factory(app)

    resp = await client.post("/v1/chat/completions", json=BODY, headers=_auth("WRONG"))
    assert resp.status == 401
    body = await resp.json()
    assert body["error"]["type"] == "authentication_error"
    assert provider.calls == 0  # never reached Codex


async def test_missing_key_rejected(client_factory):
    provider = FakeProvider()
    app = create_app(provider=provider, keys=KEYS)
    client = await client_factory(app)

    resp = await client.post("/v1/chat/completions", json=BODY)  # no Authorization
    assert resp.status == 401
    assert provider.calls == 0


async def test_valid_key_multi_message_relays(client_factory):
    provider = FakeProvider()
    app = create_app(provider=provider, keys=KEYS)
    client = await client_factory(app)

    resp = await client.post("/v1/chat/completions", json=BODY, headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == "OK"
    assert body["usage"]["total_tokens"] == 5
    assert provider.calls == 1


async def test_sub_id_header_mismatch_forbidden(client_factory):
    provider = FakeProvider()
    app = create_app(provider=provider, keys=KEYS)
    client = await client_factory(app)

    headers = {**_auth(), "X-Sub-Id": "someone-else"}
    resp = await client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert resp.status == 403
    assert provider.calls == 0


# --- concurrency cap -------------------------------------------------------


async def test_concurrency_cap_returns_429(client_factory):
    provider = BlockingProvider()
    app = create_app(provider=provider, keys=KEYS, max_concurrency=1)
    client = await client_factory(app)

    # First request enters and blocks inside provider.chat (inflight == 1).
    first = asyncio.create_task(
        client.post("/v1/chat/completions", json=BODY, headers=_auth())
    )
    await asyncio.wait_for(provider.entered.wait(), timeout=2.0)

    # Second request hits the cap immediately -> 429.
    second = await client.post("/v1/chat/completions", json=BODY, headers=_auth())
    assert second.status == 429
    sbody = await second.json()
    assert sbody["error"]["type"] == "rate_limit_error"

    # Release the first; it completes 200.
    provider.release.set()
    first_resp = await asyncio.wait_for(first, timeout=2.0)
    assert first_resp.status == 200
    assert provider.calls == 1  # the 429'd request never called the provider


# --- 429 backoff -----------------------------------------------------------


async def test_429_backoff_retries_then_succeeds(client_factory):
    r429 = LLMResponse(content=None, finish_reason="error", error_status_code=429,
                       error_retry_after_s=0.01)
    ok = LLMResponse(content="RECOVERED", finish_reason="stop",
                     usage={"total_tokens": 9})
    provider = FakeProvider(responses=[r429, r429, ok])

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    app = create_app(provider=provider, keys=KEYS, max_429_retries=3,
                     backoff_base_s=0.01, sleep=fake_sleep)
    client = await client_factory(app)

    resp = await client.post("/v1/chat/completions", json=BODY, headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == "RECOVERED"
    assert provider.calls == 3        # 2 x 429 + 1 success
    assert len(slept) == 2            # backed off twice


async def test_429_backoff_gives_up_after_max_retries(client_factory):
    r429 = LLMResponse(content=None, finish_reason="error", error_status_code=429,
                       error_retry_after_s=0.01)
    provider = FakeProvider(responses=[r429, r429, r429, r429])

    async def fake_sleep(s):
        pass

    app = create_app(provider=provider, keys=KEYS, max_429_retries=2,
                     backoff_base_s=0.01, sleep=fake_sleep)
    client = await client_factory(app)

    resp = await client.post("/v1/chat/completions", json=BODY, headers=_auth())
    # final response is the 429 error surfaced as an OpenAI completion with finish_reason error
    body = await resp.json()
    assert provider.calls == 3        # initial + 2 retries, then give up
    assert resp.status == 200         # surfaced as completion (finish_reason=error)
    assert body["choices"][0]["finish_reason"] == "error"
