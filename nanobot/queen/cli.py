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
from nanobot.queen.factory import (
    ALLOWED_ROLES,
    ROLE_DEFAULT_CAPABILITIES,
    SpawnError,
    SpawnSpec,
    SubFactory,
)
from nanobot.queen.labels import PREV_RESPONDER_META_KEY, RESPONDER_META_KEY, render_cli
from nanobot.queen.lifecycle import OnDemandManager
from nanobot.queen.registry import STATUS_STOPPED, SubRegistry

_CHAT_ID = "queen"
# Pending idea-style plan awaiting /apply or /cancel (STEP 2 approval gate).
_PENDING: dict[str, object] = {}


def _idea_manager():
    from nanobot.queen.adjuster import RoleAdjuster
    from nanobot.queen.idea_style import IdeaStyleManager
    return IdeaStyleManager(RoleAdjuster(SubFactory(SubRegistry())))


def _known_subs() -> list[str]:
    return [r.id for r in SubRegistry().list()]


def _handle_natural_admin(text: str) -> bool:
    """Natural-language config/approval/rollback. Returns True if handled here.

    The safety mechanism (IdeaStyleManager: filter → approval → apply, rollback)
    is unchanged — only the entry is natural language. Nothing applies without
    an explicit affirmative answer.
    """
    from pathlib import Path

    from nanobot.queen.adjuster import AdjustmentError, ForbiddenPatternError
    from nanobot.queen.admin_nl import detect_intent, is_affirmative, is_negative
    from nanobot.queen.idea_style import IdeaStyleError

    # 1) pending approval? interpret this turn as the answer.
    plan = _PENDING.get("plan")
    if plan is not None:
        if is_affirmative(text):
            _PENDING.pop("plan", None)
            res = _idea_manager().apply(plan, approved=True)
            print(f"  ✅ 적용됨: '{plan.sub_id}' status={res['status']} (기억 보존). "
                  f"되돌리려면 '{plan.sub_id}를 이전으로 되돌려줘'.")
            return True
        if is_negative(text):
            _PENDING.pop("plan", None)
            print("  취소됨 (미적용).")
            return True
        # neither yes nor no -> drop the pending change, then process this text fresh
        _PENDING.pop("plan", None)
        print("  (승인 대기 취소 — 새 입력으로 처리합니다)")

    # 2) detect an admin intent in this message
    intent = detect_intent(text, _known_subs())

    if intent.kind == "rollback":
        try:
            res = _idea_manager().rollback(intent.sub_id)
            print(f"  ↩️ '{intent.sub_id}' 이전 설정으로 되돌렸습니다 ({res.get('rolled_back_to')}).")
        except AdjustmentError as e:
            print(f"  ❌ {e}")
        return True

    if intent.kind == "config":
        instruction = intent.instruction
        if intent.doc_path:
            try:
                instruction = Path(intent.doc_path).read_text(encoding="utf-8")
                print(f"  📄 문서 사용: {intent.doc_path} ({len(instruction)}자, 동일 필터 적용)")
            except OSError as e:
                print(f"  ❌ 문서 읽기 실패: {e}")
                return True
        try:
            plan = _idea_manager().draft(intent.sub_id, instruction)
        except (IdeaStyleError, ForbiddenPatternError) as e:
            print(f"  ❌ 거부(필터/불변 잠금): {e}")
            return True
        _PENDING["plan"] = plan
        print(f"  📝 '{intent.sub_id}' Sub를 이렇게 설정하려 합니다 (작동 스타일만, 경계·툴·범위는 불변):")
        print(f"     {plan.style_summary[:300]}")
        print("  적용할까요? ('응'/'네'/'적용'=적용, '아니'/'취소'=취소)")
        return True

    return False


def _handle_command(text: str) -> bool:
    """Handle a Queen admin slash-command locally. Returns True if it was one.

    Commands manage Subs directly via the factory/registry (same machine), so
    they never go through /queen/chat. Creation stays allowlist-guarded.
    """
    if not text.startswith("/"):
        return False
    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/help", "/?"):
        print("  명령: /spawn <role> [cap1,cap2]  ·  /subs  ·  /stop <role>  ·  /help")
        print("  설정은 자연어로: 'idea가 디자인씽킹 방식으로 작동하게 해줘' → 승인('응'/'아니')")
        print("                  되돌리기: 'idea를 이전으로 되돌려줘'")
        print(f"  생성 가능 role: {', '.join(sorted(ALLOWED_ROLES))}")
        return True

    if cmd == "/subs":
        subs = SubRegistry().list()
        if not subs:
            print("  (등록된 Sub 없음)")
        for r in subs:
            print(f"  {r.id:10s} caps={r.capability} port={r.port} {r.status}")
        return True

    if cmd == "/spawn":
        if len(parts) < 2:
            print("  사용법: /spawn <role> [cap1,cap2]   (caps 생략 시 role 기본값)")
            return True
        role = parts[1]
        caps = (parts[2].split(",") if len(parts) > 2
                else ROLE_DEFAULT_CAPABILITIES.get(role, []))
        try:
            mgr = OnDemandManager(SubFactory(SubRegistry()))
            res = mgr.ensure(SpawnSpec(role=role, capability=caps, mode="always"))
            print(f"  ✅ {res.sub_id}: {res.action} port={res.port} "
                  f"healthy={res.healthy} caps={caps}")
        except SpawnError as e:
            print(f"  ❌ 생성 거부(allowlist): {e}")
        return True

    if cmd == "/stop":
        if len(parts) < 2:
            print("  사용법: /stop <role>")
            return True
        role = parts[1]
        reg = SubRegistry()
        rec = reg.get(role)
        if rec is None:
            print(f"  (그런 Sub 없음: {role})")
            return True
        if rec.pid:
            import os
            import signal
            try:
                os.kill(rec.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        reg.set_status(role, STATUS_STOPPED)
        print(f"  🛑 {role} 종료(워크스페이스·기억 보존)")
        return True

    print(f"  알 수 없는 명령: {cmd}  (/help)")
    return True


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
                if _handle_command(text):
                    continue
                if _handle_natural_admin(text):
                    continue
                await _one_turn(bus, text)
        else:
            print("Queen CLI — 메시지 입력('exit' 종료, '/help' 명령 도움말)")
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
                if _handle_command(text):
                    continue
                if _handle_natural_admin(text):
                    continue
                await _one_turn(bus, text)
    finally:
        bridge.stop()
        bridge_task.cancel()


def main() -> None:
    asyncio.run(amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
