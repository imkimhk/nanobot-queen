"""Queen Model Gateway — Core-side OpenAI-compatible relay for Sub instances.

Why this exists
---------------
Upstream ``nanobot serve`` (`nanobot/api/server.py`) is a *single-message agent*
endpoint: it rejects any request whose ``messages`` is not exactly one ``user``
message (``Only a single user message is supported``). A Sub nanobot, when it
uses Core as its ``provider=custom`` LLM backend, sends a full OpenAI
conversation (system prompt + user + history + tool results). That multi-message
request is rejected by ``serve``.

This module is an **additive, standalone** Core-fork component (it does not touch
any upstream file). It exposes a real OpenAI-compatible ``/v1/chat/completions``
relay that:

  1. accepts multi-message requests (system + user + history) and forwards them
     verbatim to the Codex provider that only Core holds OAuth for;
  2. validates a pre-shared key on every request and **blocks invalid keys
     before any upstream (Codex) call** — no token is ever spent on a bad key;
  3. identifies the calling Sub (``sub_id``) from the key map (or ``X-Sub-Id``);
  4. logs usage per request (sub_id, model, token counts, latency, status).

Only Core runs this. The real Codex OAuth token lives solely in Core's
machine-global OAuth session (read by the Codex provider); Subs hold only their
pre-shared key. Binds to 127.0.0.1 by default.

Run it with::

    python -m nanobot.queen.gateway

Configuration (environment variables):

  QUEEN_GATEWAY_HOST       bind host (default 127.0.0.1)
  QUEEN_GATEWAY_PORT       bind port (default 8900)
  QUEEN_GATEWAY_MODEL      model string forwarded to Codex (default
                           openai-codex/gpt-5.5; the provider strips the prefix)
  QUEEN_GATEWAY_KEYS       comma-separated ``sub_id:psk`` pairs, e.g.
                           ``sub1:poc-key,sub2:other-key``
  QUEEN_GATEWAY_PSK        single fallback pre-shared key (sub_id="sub")
  QUEEN_GATEWAY_USAGE_LOG  usage JSONL path (default ~/.nbq-core/usage.jsonl)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from nanobot.providers.openai_codex_provider import OpenAICodexProvider

DEFAULT_MODEL = "openai-codex/gpt-5.5"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8900
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_MAX_429_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 0.5


# ---------------------------------------------------------------------------
# Pre-shared key store (sub identification)
# ---------------------------------------------------------------------------


def load_keys() -> dict[str, str]:
    """Return a mapping of ``psk -> sub_id`` from the environment.

    ``QUEEN_GATEWAY_KEYS`` is a comma-separated list of ``sub_id:psk`` pairs.
    ``QUEEN_GATEWAY_PSK`` provides a single fallback key (sub_id ``"sub"``).
    """
    keys: dict[str, str] = {}
    raw = os.environ.get("QUEEN_GATEWAY_KEYS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        sub_id, psk = pair.split(":", 1)
        sub_id, psk = sub_id.strip(), psk.strip()
        if sub_id and psk:
            keys[psk] = sub_id
    single = os.environ.get("QUEEN_GATEWAY_PSK", "").strip()
    if single:
        keys.setdefault(single, "sub")
    return keys


def _load_keys_file(path: Path) -> dict[str, str]:
    """Load a ``{psk: sub_id}`` JSON keystore. Returns {} on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _current_keys(app: web.Application) -> dict[str, str]:
    """Return the live psk->sub_id map: env/static keys merged with the keystore
    file (reloaded when the file's mtime changes), so a *running* gateway picks
    up Subs spawned after startup without a restart. The keystore never appears
    in logs; only resolved sub_ids are logged.
    """
    env_keys: dict[str, str] = app["env_keys"]
    keys_file = app.get("keys_file")
    if not keys_file:
        return env_keys
    path = Path(keys_file)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    cache = app["_file_keys_cache"]
    if cache["mtime"] != mtime:
        cache["keys"] = _load_keys_file(path) if mtime is not None else {}
        cache["mtime"] = mtime
    return {**env_keys, **cache["keys"]}


def _extract_bearer(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        return token or None
    return None


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------


def _usage_log_path() -> Path:
    raw = os.environ.get("QUEEN_GATEWAY_USAGE_LOG")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".nbq-core" / "usage.jsonl"


def _log_usage(record: dict[str, Any]) -> None:
    """Append one usage record as JSONL. Never raises into the request path."""
    record = {"ts": time.time(), **record}
    try:
        path = _usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # pragma: no cover - logging must not break the relay
        logger.exception("Failed to write usage log")
    logger.info(
        "usage sub_id={} model={} status={} prompt={} completion={} total={} latency_ms={}",
        record.get("sub_id"), record.get("model"), record.get("status"),
        record.get("prompt_tokens"), record.get("completion_tokens"),
        record.get("total_tokens"), record.get("latency_ms"),
    )


# ---------------------------------------------------------------------------
# OpenAI response shaping
# ---------------------------------------------------------------------------


def _error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _completion_json(model: str, content: str, usage: dict[str, int], finish_reason: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def _sse_chunk(chunk_id: str, model: str, delta: str | None, finish_reason: str | None) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


_SSE_DONE = b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------


def _authenticate(request: web.Request) -> tuple[str | None, web.Response | None]:
    """Return ``(sub_id, None)`` on success, or ``(None, error_response)``.

    Invalid/missing keys are rejected here, BEFORE any upstream Codex call.
    """
    keys: dict[str, str] = _current_keys(request.app)
    presented = _extract_bearer(request)
    if not presented:
        _log_usage({"sub_id": None, "status": "missing_key", "model": None})
        return None, _error_json(401, "Missing or malformed Authorization header", "authentication_error")
    sub_id = keys.get(presented)
    if not sub_id:
        # Do not log the presented key; record the blocked attempt only.
        _log_usage({"sub_id": None, "status": "invalid_key", "model": None})
        return None, _error_json(401, "Invalid pre-shared key", "authentication_error")
    # Optional explicit sub identity header, must agree with the key's sub_id.
    header_sub = request.headers.get("X-Sub-Id")
    if header_sub and header_sub != sub_id:
        _log_usage({"sub_id": sub_id, "status": "sub_id_mismatch", "model": None})
        return None, _error_json(403, "X-Sub-Id does not match pre-shared key", "authentication_error")
    return sub_id, None


def _parse_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("'messages' must be a non-empty array")
    for m in messages:
        if not isinstance(m, dict) or "role" not in m:
            raise ValueError("each message must be an object with a 'role'")
    return messages


async def _relay_with_retry(request: web.Request, messages, tools, model, **stream_kw):
    """Call the Codex provider, retrying on upstream 429 with backoff.

    Returns the provider's LLMResponse. Backoff honours the provider-supplied
    retry-after when present, else exponential ``backoff_base * 2**attempt``.
    """
    provider: OpenAICodexProvider = request.app["provider"]
    max_retries: int = request.app["max_429_retries"]
    base: float = request.app["backoff_base_s"]
    sleep = request.app["sleep"]
    attempt = 0
    while True:
        result = await provider.chat(messages=messages, tools=tools, model=model, **stream_kw)
        is_429 = result.finish_reason == "error" and result.error_status_code == 429
        if is_429 and attempt < max_retries:
            wait = result.error_retry_after_s or result.retry_after or (base * (2 ** attempt))
            logger.warning("Codex 429; backoff {}s (attempt {}/{})", wait, attempt + 1, max_retries)
            await sleep(wait)
            attempt += 1
            continue
        return result


async def handle_chat_completions(request: web.Request) -> web.Response:
    sub_id, err = _authenticate(request)
    if err is not None:
        return err  # blocked before any Codex call

    # Concurrency cap: reject (429) when too many requests are already in flight.
    # Single-threaded asyncio => this check-and-increment is atomic (no await).
    inflight: dict[str, int] = request.app["inflight"]
    max_conc: int = request.app["max_concurrency"]
    if inflight["n"] >= max_conc:
        _log_usage({"sub_id": sub_id, "status": "concurrency_limited", "model": request.app["model"]})
        return _error_json(429, "Gateway concurrency limit reached; retry later", "rate_limit_error")
    inflight["n"] += 1
    try:
        return await _handle_chat_completions_inner(request, sub_id)
    finally:
        inflight["n"] -= 1


async def _handle_chat_completions_inner(request: web.Request, sub_id: str) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")

    try:
        messages = _parse_messages(body)
    except ValueError as e:
        return _error_json(400, str(e))

    provider: OpenAICodexProvider = request.app["provider"]
    model: str = request.app["model"]
    requested_model = body.get("model")
    tools = body.get("tools")
    stream = bool(body.get("stream", False))
    started = time.monotonic()

    # -- streaming path --
    if stream:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        await resp.prepare(request)

        async def _on_delta(token: str) -> None:
            await resp.write(_sse_chunk(chunk_id, model, token, None))

        try:
            result = await provider.chat_stream(
                messages=messages, tools=tools, model=model,
                on_content_delta=_on_delta,
            )
        except Exception as e:
            logger.exception("Codex relay (stream) failed")
            await resp.write(_sse_chunk(chunk_id, model, f"[gateway error: {e}]", "stop"))
            await resp.write(_SSE_DONE)
            await resp.write_eof()
            _log_usage({"sub_id": sub_id, "model": model, "requested_model": requested_model,
                        "status": "upstream_error", "latency_ms": int((time.monotonic() - started) * 1000)})
            return resp

        finish = "stop" if result.finish_reason != "error" else "error"
        await resp.write(_sse_chunk(chunk_id, model, None, finish))
        await resp.write(_SSE_DONE)
        await resp.write_eof()
        usage = result.usage or {}
        _log_usage({
            "sub_id": sub_id, "model": model, "requested_model": requested_model,
            "status": "ok" if result.finish_reason != "error" else "error",
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "stream": True,
        })
        return resp

    # -- non-streaming path (with 429 backoff) --
    try:
        result = await _relay_with_retry(request, messages, tools, model)
    except Exception as e:
        logger.exception("Codex relay failed")
        _log_usage({"sub_id": sub_id, "model": model, "requested_model": requested_model,
                    "status": "upstream_error", "latency_ms": int((time.monotonic() - started) * 1000)})
        return _error_json(502, f"Upstream relay error: {e}", "upstream_error")

    usage = result.usage or {}
    status = "ok" if result.finish_reason != "error" else "error"
    _log_usage({
        "sub_id": sub_id, "model": model, "requested_model": requested_model, "status": status,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "stream": False,
    })
    return web.json_response(
        _completion_json(model, result.content or "", usage, result.finish_reason)
    )


async def handle_models(request: web.Request) -> web.Response:
    sub_id, err = _authenticate(request)
    if err is not None:
        return err
    model: str = request.app["model"]
    return web.json_response({
        "object": "list",
        "data": [{"id": model, "object": "model", "owned_by": "queen-gateway"}],
    })


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "queen-gateway"})


# ---------------------------------------------------------------------------
# User -> Sub chat (STEP 10): routing wired to the orchestrator + passthrough
# ---------------------------------------------------------------------------


def load_user_keys() -> dict[str, str]:
    """psk -> user_id map from ``QUEEN_GATEWAY_USER_KEYS`` (``user_id:psk`` pairs)."""
    keys: dict[str, str] = {}
    for pair in os.environ.get("QUEEN_GATEWAY_USER_KEYS", "").split(","):
        pair = pair.strip()
        if pair and ":" in pair:
            uid, psk = pair.split(":", 1)
            if uid.strip() and psk.strip():
                keys[psk.strip()] = uid.strip()
    return keys


def _authenticate_user(request: web.Request) -> tuple[str | None, web.Response | None]:
    presented = _extract_bearer(request)
    user_keys: dict[str, str] = request.app["user_keys"]
    if not presented or presented not in user_keys:
        return None, _error_json(401, "Invalid or missing user key", "authentication_error")
    return user_keys[presented], None


def make_codex_classifier(provider, model: str):
    """Core LLM router: pick which Sub(s) handle an ambiguous request."""
    async def _classify(text: str, subs: list):
        catalog = "\n".join(f"- {s.id}: {', '.join(s.capability)}" for s in subs)
        prompt = (
            "너는 라우터다. 아래 가용 Sub 중 사용자 요청을 처리할 sub_id만 쉼표로 답하라. "
            "맞는 Sub가 없으면 정확히 'none'.\n\n"
            f"가용 Sub:\n{catalog}\n\n사용자 요청: {text}\n\nsub_id(쉼표) 또는 none:"
        )
        r = await provider.chat(messages=[{"role": "user", "content": prompt}], model=model)
        ans = (r.content or "").strip().lower()
        usage = r.usage or {}
        if "none" in ans:
            return [], usage
        ids = [s.id for s in subs if s.id.lower() in ans]
        return ids, usage
    return _classify


def make_codex_integrator(provider, model: str):
    async def _integrate(text: str, results: list):
        joined = "\n\n".join(f"[{r.sub_id}] {r.content}" for r in results)
        prompt = (
            f"사용자 요청: {text}\n\n여러 전문가의 답변:\n{joined}\n\n"
            "이를 하나의 일관된 응답으로 통합하라."
        )
        r = await provider.chat(messages=[{"role": "user", "content": prompt}], model=model)
        return (r.content or ""), (r.usage or {})
    return _integrate


def make_codex_answerer(provider, model: str):
    async def _answer(text: str):
        r = await provider.chat(messages=[{"role": "user", "content": text}], model=model)
        return (r.content or ""), (r.usage or {})
    return _answer


async def handle_queen_chat(request: web.Request) -> web.Response:
    user_id, err = _authenticate_user(request)
    if err is not None:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")
    text = body.get("message")
    if not text and isinstance(body.get("messages"), list) and body["messages"]:
        text = body["messages"][-1].get("content", "")
    if not text:
        return _error_json(400, "'message' is required")
    session_id = body.get("session_id")

    from nanobot.queen.chat import QueenChat, SubForwarder
    from nanobot.queen.registry import SubRegistry

    registry = SubRegistry(request.app["registry_path"])
    forwarder = SubForwarder(registry, model=request.app["model"])

    async def sub_call(sub_id: str, task_id: str, msg: str):
        return await forwarder.forward(sub_id, msg, session_id=session_id, task_id=task_id)

    chat = QueenChat(
        registry, sub_call,
        classify=request.app["classify"],
        integrate=request.app["integrate"],
        core_answer=request.app["core_answer"],
    )
    res = await chat.handle(text)

    _log_usage({
        "sub_id": "queen", "user": user_id, "status": "ok",
        "routing": res.routing, "responder": res.responder, "multi": res.multi,
        "prompt_tokens": res.sub_usage.get("prompt_tokens", 0)
        + res.routing_usage.get("prompt_tokens", 0),
        "completion_tokens": res.sub_usage.get("completion_tokens", 0)
        + res.routing_usage.get("completion_tokens", 0),
        "total_tokens": res.sub_usage.get("total_tokens", 0)
        + res.routing_usage.get("total_tokens", 0),
        "routing_tokens": res.routing_usage.get("total_tokens", 0),
        "latency_ms": res.latency_ms,
    })
    return web.json_response(
        {
            "content": res.content,
            "responder": res.responder,
            "routing": res.routing,
            "multi": res.multi,
            "task_id": res.task_id,
            "sub_usage": res.sub_usage,
            "routing_usage": res.routing_usage,
            "latency_ms": res.latency_ms,
        },
        headers={"X-Responder-Sub-Id": ",".join(res.responder)},
    )


# ---------------------------------------------------------------------------
# App / entrypoint
# ---------------------------------------------------------------------------


def create_app(
    model: str | None = None,
    *,
    provider: Any = None,
    keys: dict[str, str] | None = None,
    keys_file: str | None = None,
    registry_path: str | None = None,
    user_keys: dict[str, str] | None = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_429_retries: int = DEFAULT_MAX_429_RETRIES,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    sleep: Any = None,
) -> web.Application:
    """Build the gateway app.

    ``provider``/``keys``/``sleep`` are injectable for testing. In production
    they default to the real Codex provider, env-loaded keys, and asyncio.sleep.
    ``keys_file`` is an optional JSON keystore (``{psk: sub_id}``) that is
    hot-reloaded so dynamically spawned Subs are recognised without a restart.
    """
    import asyncio as _asyncio

    model = model or os.environ.get("QUEEN_GATEWAY_MODEL", DEFAULT_MODEL)
    app = web.Application()
    app["env_keys"] = keys if keys is not None else load_keys()
    app["keys_file"] = keys_file
    app["_file_keys_cache"] = {"mtime": object(), "keys": {}}
    app["model"] = model
    app["provider"] = provider if provider is not None else OpenAICodexProvider(default_model=model)
    app["inflight"] = {"n": 0}
    app["max_concurrency"] = max_concurrency
    app["max_429_retries"] = max_429_retries
    app["backoff_base_s"] = backoff_base_s
    app["sleep"] = sleep if sleep is not None else _asyncio.sleep
    # User->Sub routing wiring (STEP 10). registry_path defaults to ~/.nbq-core/subs.json.
    app["registry_path"] = registry_path
    app["user_keys"] = user_keys if user_keys is not None else load_user_keys()
    prov = app["provider"]
    app["classify"] = make_codex_classifier(prov, model)
    app["integrate"] = make_codex_integrator(prov, model)
    app["core_answer"] = make_codex_answerer(prov, model)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/queen/chat", handle_queen_chat)
    return app


def main() -> None:
    host = os.environ.get("QUEEN_GATEWAY_HOST", DEFAULT_HOST)
    port = int(os.environ.get("QUEEN_GATEWAY_PORT", DEFAULT_PORT))
    keys_file = os.environ.get("QUEEN_GATEWAY_KEYS_FILE") or None
    app = create_app(
        keys_file=keys_file,
        max_concurrency=int(os.environ.get("QUEEN_GATEWAY_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)),
        max_429_retries=int(os.environ.get("QUEEN_GATEWAY_MAX_429_RETRIES", DEFAULT_MAX_429_RETRIES)),
    )
    current = _current_keys(app)
    logger.enable("nanobot")
    logger.info("🐝 Queen Model Gateway starting on http://{}:{}", host, port)
    logger.info("   model={}  authorized_subs={}  keys_file={}",
                app["model"], sorted(set(current.values())), keys_file)
    if not current:
        logger.warning("No pre-shared keys configured — set QUEEN_GATEWAY_KEYS or QUEEN_GATEWAY_KEYS_FILE.")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning("Gateway bound to {} — expose only behind a trusted boundary.", host)
    web.run_app(app, host=host, port=port, print=lambda msg: logger.info(msg))


if __name__ == "__main__":
    main()
