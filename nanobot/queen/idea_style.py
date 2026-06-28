"""Queen idea-Sub style injection (STEP 2) — shape a blank idea Sub by words.

A user gives natural language ("이런 식으로 아이디어를 도출해") or a document; the
Queen turns it into the idea Sub's **working style** and injects it via the
STEP 7 role adjuster (draft → human approval → apply, home-kept so memory is
preserved, with history/rollback).

What natural language CAN change: *how* ideas are generated/structured/evaluated
(style, perspective, structure). What it can NOT change (hard locks, enforced
structurally — not by the prompt):

  * **tools** — an idea Sub's config disables file/exec/web (factory tool gating
    from its idea.* capabilities); injected text can't grant tools.
  * **capability domain** — stays ``idea.*``; the adjuster keeps the capability
    list, so routing/boundary stay in the idea domain.
  * **gateway routing** — provider always points at the gateway (config).

The injected text is additionally screened: the STEP 7 forbidden-pattern filter
(gateway bypass / Sub impersonation / credential exfiltration) PLUS an
idea-invariant filter that rejects attempts to broaden scope, remove the
boundary, or enable artifact/tool work. (Lesson from STEP 1: the model can
rationalise scope creep, so the boundary is re-verified after every injection.)

Additive Core-fork module; no upstream files are modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nanobot.queen.adjuster import AdjustmentDraft, AdjustmentError, RoleAdjuster, screen_prompt
from nanobot.queen.factory import SpawnSpec


class IdeaStyleError(ValueError):
    pass


# Attempts to change an idea Sub's INVARIANTS via natural language. Screened on
# the user's raw instruction / converted style — NOT on the rendered template
# (whose boundary legitimately names "코드 작성 금지").
_INVARIANT_VIOLATION_RULES: list[tuple[str, str]] = [
    (r"(?i)(모든|아무|어떤|무엇이든|뭐든|전부|any|every|all|anything)\s*\S{0,6}\s*"
     r"(요청|질문|것|일|request|task|thing)?.{0,18}(답|응답|수행|처리|해라|해줘|do|answer|handle)",
     "scope broadening (answer everything)"),
    # limit-removal in EITHER word order (동사→명사 and 명사→동사)
    (r"(?i)(무시|해제|제거|풀어|벗어|넘어|ignore|disable|remove|bypass|우회|override|relax)"
     r".{0,18}(제한|경계|범위|제약|규칙|보안|잠금|limit|restrict|boundary|scope|rule|guard|lock)",
     "removing limits / boundary"),
    (r"(?i)(제한|경계|범위|제약|규칙|보안|잠금|limit|restrict|boundary|scope|rule|guard|lock)"
     r".{0,12}(무시|해제|제거|풀|넘|벗어|ignore|disable|remove|bypass|relax)",
     "removing limits / boundary"),
    (r"(?i)(코드|함수|프로그램|스크립트|파일|코딩|문서)\s*\S{0,4}\s*"
     r"(작성|실행|생성|만들|짜|write|exec|run|create|build|produce)",
     "enabling artifact creation"),
    (r"(?i)(tool|툴|exec|shell|web|파일\s*쓰|file\s*write|외부\s*호출)\s*\S{0,4}\s*"
     r"(사용|허용|쓰|enable|켜|use|grant)",
     "enabling tools / external work"),
    (r"(?i)(out[\s_-]?of[\s_-]?scope).{0,18}(말|하지|금지|무시|안|never|don'?t|stop|skip)",
     "disabling the OUT_OF_SCOPE rule"),
]


def screen_idea_invariants(text: str) -> None:
    """Raise IdeaStyleError if the text tries to change an idea Sub's invariants."""
    for pattern, why in _INVARIANT_VIOLATION_RULES:
        if re.search(pattern, text):
            raise IdeaStyleError(f"불변 항목 변경 시도 차단: {why}")


# (raw_instruction) -> style_summary   (optional Core LLM "compile" step)
StyleConverter = "Callable[[str], tuple[str, dict]]"  # documented; injected


@dataclass
class StylePlan:
    sub_id: str
    style_summary: str          # shown to the user for approval
    adjustment_plan: object     # underlying RoleAdjuster AdjustmentPlan
    prompt_version: str


class IdeaStyleManager:
    """Inject a working style into an idea Sub, reusing the STEP 7 adjuster."""

    def __init__(self, adjuster: RoleAdjuster, *, converter=None):
        self.adjuster = adjuster
        self.factory = adjuster.factory
        self.registry = adjuster.registry
        self.converter = converter  # optional async/sync (raw)->(summary, usage)

    def draft(self, sub_id: str, raw: str, *, source: str = "nl",
              prompt_version: str | None = None) -> StylePlan:
        """Build + screen a style draft for approval. Nothing is applied."""
        rec = self.registry.get(sub_id)
        if rec is None:
            raise IdeaStyleError(f"unknown sub_id: {sub_id}")
        if rec.role != "idea":
            raise IdeaStyleError(f"style injection is only for idea Subs, not {rec.role!r}")
        if not raw or not raw.strip():
            raise IdeaStyleError("빈 지침은 주입할 수 없다")

        # 1) screen the user's raw instruction / document (same filter for both)
        screen_prompt(raw)                # STEP 7 forbidden patterns
        screen_idea_invariants(raw)       # idea hard-lock invariants

        # 2) (optional) Core converts raw -> a clean working-style summary
        style = raw.strip()
        if self.converter is not None:
            style = self.converter(raw)
            if isinstance(style, tuple):
                style = style[0]
            style = (style or "").strip()
            screen_prompt(style)
            screen_idea_invariants(style)

        # 3) render the full idea prompt: invariant scaffold + injected style.
        #    Capability stays idea.* (domain lock); tools stay off (factory gating).
        version = prompt_version or _bump(rec.prompt_version)
        spec = SpawnSpec(role="idea", capability=list(rec.capability), prompt_version=version)
        agents_md = self.factory._render_agents_md(spec, sub_id, working_style=style)

        # 4) hand to the STEP 7 adjuster (validates allowlist + forbidden-pattern
        #    filter on the final prompt + config screen). Home-kept (no isolate).
        plan = self.adjuster.draft(AdjustmentDraft(
            sub_id=sub_id, capability=list(rec.capability), role_label="idea",
            prompt_version=version, role_prompt_text=agents_md, isolate=False,
        ))
        return StylePlan(sub_id=sub_id, style_summary=style, adjustment_plan=plan,
                         prompt_version=version)

    def apply(self, plan: StylePlan, *, approved: bool = False) -> dict:
        if not approved:
            raise AdjustmentError("apply requires approved=True (human approval)")
        return self.adjuster.apply(plan.adjustment_plan, approved=True)

    def rollback(self, sub_id: str) -> dict:
        return self.adjuster.rollback(sub_id)


def _bump(version: str) -> str:
    """v1 -> v2, v2 -> v3, else append -s1."""
    m = re.fullmatch(r"v(\d+)", version or "")
    return f"v{int(m.group(1)) + 1}" if m else f"{version or 'v1'}-s1"
