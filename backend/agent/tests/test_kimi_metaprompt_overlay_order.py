"""MetaPrompt / overlay ordering contracts on top of ``PromptManager``.

GREEN: documents current ordering of evolution vs human feedback (existing behavior).
RED: ``meta_prompt_overlay`` slot expected between evolution lessons and human feedback.
"""

from __future__ import annotations

import inspect

import pytest


class TestMetapromptOverlayOrderGreen:
    """Existing PromptManager behavior — keep passing during MetaPrompt work."""

    def test_evolution_overlay_before_human_feedback_block(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        pm.set_human_feedback("Fix citation format.")
        overlay = "## Lessons\n1. Prioritize primary sources."
        sp = pm.for_stage(
            "topic_init",
            evolution_overlay=overlay,
            topic="t",
            domains="ml",
            project_name="p",
            quality_threshold="8.0",
        )
        assert "Prioritize primary sources" in sp.user
        assert "Human Researcher Feedback" in sp.user
        assert sp.user.index("Prioritize primary sources") < sp.user.index("Human Researcher Feedback")


class TestMetapromptOverlayContractRed:
    """Contract for Kimi MetaPrompt layer — fails until API is extended."""

    def test_for_stage_accepts_meta_prompt_overlay_parameter(self) -> None:
        from researchclaw.prompts import PromptManager

        sig = inspect.signature(PromptManager.for_stage)
        assert "meta_prompt_overlay" in sig.parameters, (
            "RED: add optional keyword ``meta_prompt_overlay: str = \"\"`` to PromptManager.for_stage; "
            "when non-empty, append after evolution_overlay and before human feedback block."
        )
