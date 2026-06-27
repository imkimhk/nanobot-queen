"""Queen responder labels — show which Sub answered, per channel format.

The gateway returns the responding ``sub_id``(s) as token-0 metadata
(``X-Responder-Sub-Id``). Each channel renders that into its own label:

  * CLI      -> plain text prefix  ``[Research]``
  * Telegram -> HTML prefix        ``<b>[Research]</b>``

On a Sub switch (handoff) a transition note is shown: ``↪ Research → Coder``.

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import html

# Metadata key carrying the responder identity through OutboundMessage.metadata.
RESPONDER_META_KEY = "responder_sub_id"
# Metadata key carrying the previous responder (for transition rendering).
PREV_RESPONDER_META_KEY = "responder_prev"

# Friendly display names for known sub_ids; others are title-cased.
_DISPLAY = {"core": "Core"}


def display_name(sub_id: str) -> str:
    return _DISPLAY.get(sub_id, sub_id[:1].upper() + sub_id[1:])


def responder_label(responder: list[str] | str | None) -> str:
    """Human label for one or more responders, e.g. ``Research`` or ``Research+Coder``."""
    if responder is None:
        return "Core"
    if isinstance(responder, str):
        responder = [responder]
    if not responder:
        return "Core"
    return "+".join(display_name(s) for s in responder)


def cli_prefix(responder: list[str] | str | None) -> str:
    return f"[{responder_label(responder)}]"


def telegram_prefix(responder: list[str] | str | None) -> str:
    return f"<b>[{html.escape(responder_label(responder))}]</b>"


def transition_note(
    prev: list[str] | str | None,
    cur: list[str] | str | None,
    *,
    style: str = "cli",
) -> str | None:
    """Return a handoff note if the responder changed, else None."""
    pl, cl = responder_label(prev), responder_label(cur)
    if prev is None or pl == cl:
        return None
    if style == "telegram":
        return f"↪ <i>{html.escape(pl)} → {html.escape(cl)}</i>"
    return f"↪ {pl} → {cl}"


def render_cli(responder: list[str] | str | None, content: str,
               *, prev: list[str] | str | None = None) -> str:
    """Full CLI render: optional transition note + label prefix + content."""
    lines = []
    note = transition_note(prev, responder, style="cli")
    if note:
        lines.append(note)
    lines.append(f"{cli_prefix(responder)} {content}")
    return "\n".join(lines)


def render_telegram(responder: list[str] | str | None, content: str,
                    *, prev: list[str] | str | None = None) -> str:
    """Full Telegram render: optional transition note + HTML label + content."""
    parts = []
    note = transition_note(prev, responder, style="telegram")
    if note:
        parts.append(note)
    parts.append(f"{telegram_prefix(responder)} {content}")
    return "\n".join(parts)
