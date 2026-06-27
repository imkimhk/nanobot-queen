"""Queen interactive CLI — one input box, responder labels, handoff transitions.

A thin, additive client (no upstream change) that gives the "same input box"
experience: you type, the message goes through the bus → QueenBridge →
``/queen/chat`` → the right Sub, and the reply is printed with a ``[Responder]``
label and an ``↪ A → B`` note whenever the responder changes.

Usage::

    QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key \
        python -m nanobot.queen.cli                # interactive (reads stdin)
        python -m nanobot.queen.cli "msg1" "msg2"  # scripted (one turn each)
"""

from __future__ import annotations

import asyncio
import os
import sys

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.queen.bridge import GatewayClient, QueenBridge
from nanobot.queen.labels import PREV_RESPONDER_META_KEY, RESPONDER_META_KEY, render_cli

_CHAT_ID = "queen"


def _format_meta(meta: dict) -> str:
    su = (meta.get("sub_usage") or {}).get("total_tokens", 0)
    ru = (meta.get("routing_usage") or {}).get("total_tokens", 0)
    return (f"  · routing={meta.get('routing')} sub_tokens={su} "
            f"routing_tokens={ru} latency_ms={meta.get('latency_ms')}")


async def _one_turn(bus: MessageBus, text: str) -> None:
    await bus.publish_inbound(InboundMessage(
        channel="cli", sender_id="user", chat_id=_CHAT_ID, content=text,
    ))
    out = await asyncio.wait_for(bus.consume_outbound(), timeout=320.0)
    responder = out.metadata.get(RESPONDER_META_KEY) or ["core"]
    prev = out.metadata.get(PREV_RESPONDER_META_KEY)
    print(render_cli(responder, out.content, prev=prev))
    print(_format_meta(out.metadata))


async def amain(messages: list[str]) -> None:
    base_url = os.environ.get("QUEEN_GATEWAY_URL", "http://127.0.0.1:8900")
    user_key = os.environ.get("QUEEN_USER_KEY", "user-key")
    bus = MessageBus()
    bridge = QueenBridge(bus, GatewayClient(base_url, user_key))
    bridge_task = asyncio.create_task(bridge.run())
    try:
        if messages:
            for text in messages:
                print(f"\n› {text}")
                await _one_turn(bus, text)
        else:
            print("Queen CLI — type a message ('exit' to quit)")
            loop = asyncio.get_event_loop()
            while True:
                try:
                    text = (await loop.run_in_executor(None, sys.stdin.readline))
                except (EOFError, KeyboardInterrupt):
                    break
                if not text:
                    break
                text = text.strip()
                if not text or text.lower() in {"exit", "quit"}:
                    break
                await _one_turn(bus, text)
    finally:
        bridge.stop()
        bridge_task.cancel()


def main() -> None:
    asyncio.run(amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
