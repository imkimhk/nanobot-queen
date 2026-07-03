"""Queen per-session router state — sticky Sub + Core-direct chat history.

This is the *routing* side-store used by ``QueenChat`` and the Core-direct
answerer so that a single ``session_id`` (chat thread) keeps its resolved Sub
across turns and Core-direct replies can see prior turns.

Why this exists
---------------
The Queen previously re-classified every user message from scratch. That was
correct for the *first* turn of a thread, but wrong for follow-ups: a message
like "그 코드명 뭐였지?" would jump from the ``research`` Sub (holding the ZEBRA
memory) to the ``coder`` Sub (matching "코드"), and the follow-up went to a Sub
that never saw the earlier turns. Two changes fix this:

  * **sticky_sub**: once a session's turn resolves to a single Sub, subsequent
    turns of the same session reuse that Sub without re-routing. Explicit
    breaks — ``OUT_OF_SCOPE`` from the sticky Sub, or a user-side ``@sub_id``
    mention — are the only conditions that reroute.
  * **core_history**: Core-direct replies (used when no Sub fits) used to be
    stateless. History here lets Core see previous turns of the same session
    without touching the classifier or integrator calls.

The store is in-process, single-node, unbounded lifetime. The Queen gateway is
a single asyncio process so no lock is needed (mutations happen synchronously
within one coroutine step). Core history is bounded per-session to avoid
unbounded growth. This module is transport-agnostic and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Cap the retained Core-direct history per session so long threads do not
# grow the prompt without bound. 20 pairs = 40 messages ~ a reasonable
# conversation buffer for follow-up context.
DEFAULT_MAX_CORE_HISTORY_MESSAGES = 40

# Sentinel sub id used to represent "Core answered directly" in the sticky
# store so a Core-direct thread stays on the Core-direct path on follow-ups.
STICKY_CORE = "core"


@dataclass
class _SessionEntry:
    sticky: list[str] | None = None            # resolved sub_ids for the session
    core_history: list[dict[str, Any]] = field(default_factory=list)  # {role, content}


class SessionRouterStore:
    """In-process ``session_id → routing state`` store.

    Purely additive: callers that never look up a session_id keep the previous
    stateless behavior. Only ``QueenChat`` and the per-request Core-direct
    answerer wrapper consult this store.
    """

    def __init__(self, *, max_core_history_messages: int = DEFAULT_MAX_CORE_HISTORY_MESSAGES):
        self._entries: dict[str, _SessionEntry] = {}
        # Guard against absurd/negative caps; keep at least one exchange.
        self._max_hist = max(2, int(max_core_history_messages))

    # -- sticky Sub ---------------------------------------------------------

    def get_sticky(self, session_id: str) -> list[str] | None:
        """Return the sub_ids the given session is currently pinned to, or None."""
        entry = self._entries.get(session_id)
        if entry is None or entry.sticky is None:
            return None
        return list(entry.sticky)

    def set_sticky(self, session_id: str, sub_ids: list[str]) -> None:
        """Pin the session to the given sub_ids for follow-up turns."""
        entry = self._entries.setdefault(session_id, _SessionEntry())
        entry.sticky = list(sub_ids)

    def clear_sticky(self, session_id: str) -> None:
        """Drop the sticky Sub for the session (next turn re-routes)."""
        entry = self._entries.get(session_id)
        if entry is not None:
            entry.sticky = None

    # -- Core-direct history -----------------------------------------------

    def get_core_history(self, session_id: str) -> list[dict[str, Any]]:
        """Return a *copy* of the Core-direct message history for the session."""
        entry = self._entries.get(session_id)
        if entry is None:
            return []
        return [dict(m) for m in entry.core_history]

    def append_core_history(self, session_id: str, role: str, content: str) -> None:
        """Append one message to the session's Core-direct history and cap size."""
        entry = self._entries.setdefault(session_id, _SessionEntry())
        entry.core_history.append({"role": role, "content": content})
        if len(entry.core_history) > self._max_hist:
            # Drop oldest to keep the newest window.
            del entry.core_history[: len(entry.core_history) - self._max_hist]

    def clear_core_history(self, session_id: str) -> None:
        """Reset Core-direct history for the session (kept for tests/admin)."""
        entry = self._entries.get(session_id)
        if entry is not None:
            entry.core_history.clear()
