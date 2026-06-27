"""Unit tests for Queen responder labels (CLI text + Telegram HTML)."""

from __future__ import annotations

from nanobot.queen.labels import (
    cli_prefix,
    render_cli,
    render_telegram,
    responder_label,
    telegram_prefix,
    transition_note,
)


def test_responder_label_single_and_multi():
    assert responder_label(["research"]) == "Research"
    assert responder_label(["coder"]) == "Coder"
    assert responder_label(["core"]) == "Core"
    assert responder_label(["research", "coder"]) == "Research+Coder"
    assert responder_label(None) == "Core"


def test_cli_prefix():
    assert cli_prefix(["research"]) == "[Research]"
    assert cli_prefix(["research", "coder"]) == "[Research+Coder]"


def test_telegram_prefix_is_html_bold():
    assert telegram_prefix(["coder"]) == "<b>[Coder]</b>"
    assert telegram_prefix(["research", "coder"]) == "<b>[Research+Coder]</b>"


def test_transition_note_only_on_change():
    assert transition_note(None, ["research"]) is None       # first turn
    assert transition_note(["research"], ["research"]) is None  # same responder
    assert transition_note(["research"], ["coder"]) == "↪ Research → Coder"


def test_transition_note_telegram_style_is_html():
    note = transition_note(["research"], ["coder"], style="telegram")
    assert note == "↪ <i>Research → Coder</i>"


def test_render_cli_with_and_without_transition():
    assert render_cli(["research"], "hello") == "[Research] hello"
    out = render_cli(["coder"], "fixed it", prev=["research"])
    assert out == "↪ Research → Coder\n[Coder] fixed it"


def test_render_telegram_html():
    out = render_telegram(["coder"], "done", prev=["research"])
    assert out == "↪ <i>Research → Coder</i>\n<b>[Coder]</b> done"
