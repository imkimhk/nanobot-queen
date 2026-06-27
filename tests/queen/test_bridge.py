"""Unit tests for the Queen channel↔gateway bridge."""

from __future__ import annotations

from nanobot.bus.events import InboundMessage
from nanobot.queen.bridge import GatewayClient, QueenBridge
from nanobot.queen.labels import PREV_RESPONDER_META_KEY, RESPONDER_META_KEY


class FakeBus:
    def __init__(self):
        self.outbound = []

    async def publish_outbound(self, msg):
        self.outbound.append(msg)


def _client(responses):
    """GatewayClient whose POST returns scripted /queen/chat payloads."""
    seq = list(responses)

    async def fake_post(url, key, body):
        assert url.endswith("/queen/chat")
        assert key == "user-key"
        return 200, seq.pop(0)

    return GatewayClient("http://x", "user-key", post=fake_post)


def _inbound(content, chat_id="c1"):
    return InboundMessage(channel="cli", sender_id="u", chat_id=chat_id, content=content)


async def test_handle_sets_responder_metadata():
    bus = FakeBus()
    client = _client([{"content": "def add", "responder": ["coder"], "routing": "rule",
                       "multi": False, "sub_usage": {"total_tokens": 100},
                       "routing_usage": {"total_tokens": 0}, "latency_ms": 50}])
    bridge = QueenBridge(bus, client)
    out = await bridge.handle_one(_inbound("함수 짜줘"))
    assert out.metadata[RESPONDER_META_KEY] == ["coder"]
    assert out.content == "def add"
    assert PREV_RESPONDER_META_KEY not in out.metadata  # first turn, no transition


async def test_handoff_records_previous_responder():
    bus = FakeBus()
    client = _client([
        {"content": "a", "responder": ["research"], "routing": "rule"},
        {"content": "b", "responder": ["coder"], "routing": "llm"},
    ])
    bridge = QueenBridge(bus, client)
    await bridge.handle_one(_inbound("조사"))
    out2 = await bridge.handle_one(_inbound("함수"))
    # transition recorded so the channel can render '↪ Research → Coder'
    assert out2.metadata[RESPONDER_META_KEY] == ["coder"]
    assert out2.metadata[PREV_RESPONDER_META_KEY] == ["research"]


async def test_same_responder_no_transition():
    bus = FakeBus()
    client = _client([
        {"content": "a", "responder": ["research"]},
        {"content": "b", "responder": ["research"]},
    ])
    bridge = QueenBridge(bus, client)
    await bridge.handle_one(_inbound("조사1"))
    out2 = await bridge.handle_one(_inbound("조사2"))
    assert PREV_RESPONDER_META_KEY not in out2.metadata


async def test_gateway_http_error_falls_back_to_core():
    async def fake_post(url, key, body):
        return 502, {}
    client = GatewayClient("http://x", "user-key", post=fake_post)
    data = await client.chat("hi", None)
    assert data["responder"] == ["core"]
    assert "error" in data["content"]
