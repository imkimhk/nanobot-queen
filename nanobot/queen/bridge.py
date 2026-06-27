"""Queen channel↔gateway bridge — route channel messages through /queen/chat.

This is the glue between any nanobot chat channel (CLI bus channel ``cli``,
Telegram, …) and the Queen gateway's User→Sub path. It mirrors
``AgentLoop.run()``: consume an ``InboundMessage`` from the bus, call
``/queen/chat`` (which routes to the right Sub — no Core orchestrator LLM for a
single-Sub task), and publish an ``OutboundMessage`` whose ``metadata`` carries
the responding ``sub_id`` so each channel can render its own responder label.

Grafting onto the interactive ``nanobot agent`` command is a single-line swap of
``agent_loop.run()`` for ``bridge.run()`` (reported before any edit). Driven by
the standalone Queen CLI it needs no upstream change at all.

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.queen.labels import PREV_RESPONDER_META_KEY, RESPONDER_META_KEY


class GatewayClient:
    """Minimal async client for the gateway's ``POST /queen/chat``."""

    def __init__(self, base_url: str, user_key: str, *, post=None, timeout: float = 300.0):
        self.url = base_url.rstrip("/") + "/queen/chat"
        self.user_key = user_key
        self._post = post or self._default_post
        self.timeout = timeout

    async def chat(self, message: str, session_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        if session_id:
            body["session_id"] = session_id
        status, data = await self._post(self.url, self.user_key, body)
        if status != 200:
            return {"content": f"[gateway error HTTP {status}]", "responder": ["core"],
                    "routing": "error", "multi": False}
        return data

    async def _default_post(self, url: str, key: str, body: dict):
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
            try:
                data = r.json()
            except Exception:
                data = {}
            return r.status_code, data


class QueenBridge:
    """Bus consumer: channel message -> /queen/chat -> labelled outbound."""

    def __init__(self, bus, gateway: GatewayClient):
        self.bus = bus
        self.gateway = gateway
        self._running = False
        self._last_responder: dict[str, list[str]] = {}  # chat_id -> responder

    async def handle_one(self, msg: InboundMessage) -> OutboundMessage:
        """Route one inbound message and build the labelled outbound."""
        data = await self.gateway.chat(msg.content, session_id=msg.session_key)
        responder = data.get("responder") or ["core"]
        prev = self._last_responder.get(msg.chat_id)
        self._last_responder[msg.chat_id] = responder

        meta: dict[str, Any] = {
            RESPONDER_META_KEY: responder,
            "routing": data.get("routing"),
            "multi": data.get("multi"),
            "sub_usage": data.get("sub_usage"),
            "routing_usage": data.get("routing_usage"),
            "latency_ms": data.get("latency_ms"),
        }
        if prev is not None and prev != responder:
            meta[PREV_RESPONDER_META_KEY] = prev
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=data.get("content") or "",
            metadata=meta,
        )

    async def run(self) -> None:
        """Consume inbound messages and publish labelled outbound (like AgentLoop.run)."""
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                out = await self.handle_one(msg)
                await self.bus.publish_outbound(out)
            except Exception as e:  # never let one turn kill the bridge
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=f"[bridge error: {e}]",
                    metadata={RESPONDER_META_KEY: ["core"]},
                ))

    def stop(self) -> None:
        self._running = False
