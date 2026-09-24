"""Base agent. Every agent is (state, tools) → state."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from state import QAState
from llm_router import LLMRouter


class Agent(ABC):
    """All agents inherit from this. Subclasses implement `run`."""

    name: str = "base"
    purpose: str = "generic"  # used by LLMRouter to pick model tier

    def __init__(self, llm: LLMRouter):
        self.llm = llm

    @abstractmethod
    def run(self, state: QAState, **kwargs) -> QAState:
        """Read from state, write to state, return it."""
        ...

    # ── LLM convenience ───────────────────────────────────────

    def ask(
        self,
        prompt: str,
        state: QAState,
        system: str = "",
        purpose: Optional[str] = None,
    ) -> str:
        return self.llm.ask(
            prompt,
            state,
            system=system,
            purpose=purpose or self.purpose,
        )

    # ── Logging ───────────────────────────────────────────────

    def log(self, state: QAState, msg: str) -> None:
        state.add_warning(f"[{self.name}] {msg}")

    # ── JSON extraction (used by many agents) ─────────────────

    @staticmethod
    def extract_json(raw: str) -> Optional[Any]:
        """
        Try to pull a JSON object or array out of an LLM response.
        Handles code fences, prose preambles, and trailing junk.
        """
        if not raw:
            return None

        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?\s*", "", raw)
        cleaned = cleaned.replace("```", "").strip()

        # Try direct parse
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Greedy: first {...} or [...]
        for pattern in (r"\{.*\}", r"\[.*\]"):
            m = re.search(pattern, cleaned, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    continue
        return None

    # ── Pydantic validation with one self-heal retry ──────────

    def validate_with_retry(
        self,
        state: QAState,
        raw: str,
        model_cls: type[BaseModel],
        healer: Optional["Agent"] = None,
        max_retries: int = 1,
    ) -> Optional[BaseModel]:
        """
        Parse raw → model_cls. On failure, ask Healer to fix, retry.
        """
        data = self.extract_json(raw)
        if data is None:
            if healer and max_retries > 0:
                fixed = healer.repair_json(  # type: ignore
                    state, raw, schema_hint=model_cls.__name__
                )
                return self.validate_with_retry(
                    state, fixed, model_cls, healer, max_retries - 1
                )
            return None

        try:
            return model_cls.model_validate(data)
        except ValidationError as e:
            self.log(state, f"{model_cls.__name__} validation failed: {e.errors()[:2]}")
            if healer and max_retries > 0:
                fixed = healer.repair_json(  # type: ignore
                    state, raw,
                    schema_hint=f"{model_cls.__name__} — errors: {e.json()[:400]}",
                )
                return self.validate_with_retry(
                    state, fixed, model_cls, healer, max_retries - 1
                )
            return None