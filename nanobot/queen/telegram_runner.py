"""Queen Telegram runner — connect Telegram to the Queen via the shared bridge.

Reuses the STEP 10-2 channel glue (QueenBridge): the standard nanobot Telegram
channel publishes inbound messages to a bus, QueenBridge routes them through
``/queen/chat`` (passthrough/routing/labels), and this runner sends the reply
back via the Telegram channel with a **markdown responder label** baked in
(``**[Coder]**`` + ``↪ A → B``). No upstream file is modified — Telegram is just
another channel on top of the existing glue.

Token: read from ``~/.nbq-core/telegram.json`` ({"token": ..., "allow_from": [...]})
or the env vars QUEEN_TELEGRAM_TOKEN / QUEEN_TELEGRAM_ALLOW_FROM. The token is
never logged.

Run::

    QUEEN_GATEWAY_URL=http://127.0.0.1:8900 QUEEN_USER_KEY=user-key \
        python -m nanobot.queen.telegram_runner
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.queen.bridge import GatewayClient, QueenBridge
from nanobot.queen.labels import PREV_RESPONDER_META_KEY, RESPONDER_META_KEY, render_telegram_md

_TOKEN_FILE = Path.home() / ".nbq-core" / "telegram.json"


def _load_telegram_settings() -> tuple[str, list[str]]:
    """Return (token, allow_from). Env overrides the file. Token never logged."""
    token = os.environ.get("QUEEN_TELEGRAM_TOKEN", "").strip()
    allow_env = os.environ.get("QUEEN_TELEGRAM_ALLOW_FROM", "").strip()
    allow = [a.strip() for a in allow_env.split(",") if a.strip()] if allow_env else []
    if (not token or not allow) and _TOKEN_FILE.exists():
        try:
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"telegram settings unreadable: {_TOKEN_FILE}: {e}")
        token = token or str(data.get("token", "")).strip()
        if not allow:
            allow = [str(a).strip() for a in data.get("allow_from", []) if str(a).strip()]
    if not token:
        raise SystemExit(
            f"No Telegram bot token. Put it in {_TOKEN_FILE} "
            '({"token": "...", "allow_from": ["<your id>"]}) or set QUEEN_TELEGRAM_TOKEN.'
        )
    return token, allow


async def _pump_outbound(bus: MessageBus, channel, stop: asyncio.Event) -> None:
    """Consume Queen outbound, bake the markdown responder label, send to Telegram."""
    while not stop.is_set():
        try:
            msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        responder = msg.metadata.get(RESPONDER_META_KEY) or ["core"]
        prev = msg.metadata.get(PREV_RESPONDER_META_KEY)
        labelled = render_telegram_md(responder, msg.content or "", prev=prev)
        await channel.send(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=labelled,
            reply_to=msg.reply_to, metadata=msg.metadata,
        ))


async def amain() -> None:
    from nanobot.channels.telegram import TelegramChannel, TelegramConfig

    token, allow_from = _load_telegram_settings()
    base_url = os.environ.get("QUEEN_GATEWAY_URL", "http://127.0.0.1:8900")
    user_key = os.environ.get("QUEEN_USER_KEY", "user-key")

    bus = MessageBus()
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token=token, allow_from=allow_from, mode="polling"),
        bus,
    )
    bridge = QueenBridge(bus, GatewayClient(base_url, user_key))
    stop = asyncio.Event()

    await channel.start()
    print(f"🐝 Queen Telegram runner — bot polling. Gateway={base_url}, "
          f"allow_from={allow_from or '(open)'}. Ctrl+C to stop.")
    bridge_task = asyncio.create_task(bridge.run())
    pump_task = asyncio.create_task(_pump_outbound(bus, channel, stop))

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        await stop.wait()
    finally:
        bridge.stop()
        bridge_task.cancel()
        pump_task.cancel()
        await channel.stop()
        print("\nstopped.")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
