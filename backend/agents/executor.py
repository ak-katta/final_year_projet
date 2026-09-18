"""Agent #3 — Executor.

Translates one Step → Playwright action, runs it.
Called once per step.
"""

from datetime import datetime

from state import (
    QAState, Step, ActionType, TestStatus, PageSnapshot,
)
from agents.base import Agent
from capture import capture


SYSTEM = """You convert a single test step into ONE Playwright action.
Use ONLY selectors from the provided element list.
Reply with ONLY valid JSON. No prose, no markdown fences."""


PROMPT = """Convert this step into a Playwright action.

Step: {description}
Preferred action: {action_hint}

Available selectors:
{elements}

Reply with ONLY:
{{
  "action": "click|fill|select|goto|press|hover|wait|assert_text|assert_url|assert_visible|scroll",
  "selector": "css selector from list (empty for goto/wait/assert_url/scroll)",
  "value": "text to fill OR expected text/url (empty for click/hover)",
  "timeout_ms": 5000
}}"""


class ExecutorAgent(Agent):
    name = "executor"
    purpose = "step_convert"

    def __init__(self, llm, healer=None):
        super().__init__(llm)
        self.healer = healer

    # ── Main entry ────────────────────────────────────────────

    def run(self, state: QAState, step: Step | None = None) -> QAState:
        if step is None:
            step = state.get_current_step()
        if not step:
            return state

        page = state._page
        if page is None:
            step.status = TestStatus.ERROR
            step.error = "no page handle"
            return state

        # Screenshot before (optional)
        if state.screenshot_config.get("on_every_step"):
            s = capture(
                state, page, kind="before_action",
                label=f"Before: {step.description}",
                step_index=step.index,
            )
            step.screenshot_before = s.id

        # Translate + execute
        step.started_at = datetime.now()
        action = self._translate(step, state)

        try:
            self._perform(page, action, state)
            step.status = TestStatus.PASSED
        except Exception as e:
            step.status = TestStatus.FAILED
            step.error = self._short(str(e))

            # Self-heal once
            if self._try_heal(page, step, action, state):
                step.status = TestStatus.PASSED
                step.error = None
            else:
                if state.screenshot_config.get("on_failure"):
                    shot = capture(
                        state, page, kind="on_failure",
                        label=f"Failure: {step.description}",
                        step_index=step.index,
                    )
                    step.screenshot_after = shot.id

        step.finished_at = datetime.now()
        step.duration_ms = (
            step.finished_at - step.started_at
        ).total_seconds() * 1000

        # Screenshot after (optional)
        if (state.screenshot_config.get("on_every_step")
                and step.status == TestStatus.PASSED):
            s = capture(
                state, page, kind="after_action",
                label=f"After: {step.description}",
                step_index=step.index,
            )
            step.screenshot_after = s.id

        return state

    # ── Translation ───────────────────────────────────────────

    def _translate(self, step: Step, state: QAState) -> dict:
        snap = state.initial_snapshot
        elements = self._fmt_elements(snap) if snap else "(none)"

        prompt = PROMPT.format(
            description=step.description or step.action.value,
            action_hint=step.action.value,
            elements=elements,
        )
        raw = self.ask(prompt, state, system=SYSTEM)
        action = self.extract_json(raw)
        if not isinstance(action, dict):
            action = {}

        # Merge: step fields fill gaps the LLM left
        action.setdefault("timeout_ms", step.timeout_ms)
        if not action.get("selector") and step.selector:
            action["selector"] = step.selector
        if not action.get("value") and step.value:
            action["value"] = step.value
        if not action.get("action"):
            action["action"] = step.action.value
        return action

    # ── Perform ───────────────────────────────────────────────

    def _perform(self, page, action: dict, state: QAState) -> None:
        a = str(action.get("action", "click")).lower()
        sel = action.get("selector", "") or ""
        val = action.get("value", "") or ""
        try:
            to = int(action.get("timeout_ms", 5000) or 5000)
        except (TypeError, ValueError):
            to = 5000

        if a == "click":
            page.click(sel, timeout=to)
        elif a == "fill":
            page.fill(sel, str(val), timeout=to)
        elif a == "select":
            page.select_option(sel, str(val), timeout=to)
        elif a == "goto":
            page.goto(str(val), timeout=to)
        elif a == "press":
            page.press(sel, str(val) or "Enter", timeout=to)
        elif a == "hover":
            page.hover(sel, timeout=to)
        elif a == "wait":
            ms = int(val) if str(val).isdigit() else 1000
            page.wait_for_timeout(ms)
        elif a == "assert_text":
            body = page.inner_text("body")
            assert str(val) in body, f"expected {val!r} not found on page"
        elif a == "assert_url":
            assert str(val) in page.url, (
                f"URL does not contain {val!r} (got {page.url})"
            )
        elif a == "assert_visible":
            assert page.is_visible(sel, timeout=to), f"{sel!r} not visible"
        elif a == "scroll":
            page.mouse.wheel(0, 800)
        elif a == "screenshot":
            capture(state, page, kind="viewport", label="manual")
        else:
            raise ValueError(f"unknown action: {a!r}")

    # ── Self-heal ─────────────────────────────────────────────

    def _try_heal(self, page, step: Step, action: dict, state: QAState) -> bool:
        if not self.healer or not action.get("selector"):
            return False
        try:
            fixed = self.healer.repair_selector(
                state, action, step.error or "", page
            )
            if not fixed:
                return False
            self._perform(page, fixed, state)
            state.retries += 1
            self.log(
                state,
                f"healed step {step.index} → selector {fixed.get('selector')!r}",
            )
            return True
        except Exception as e:
            self.log(state, f"heal failed: {e}")
            return False

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _short(s: str, n: int = 300) -> str:
        return s if len(s) <= n else s[:n] + "…"

    def _fmt_elements(self, snap: PageSnapshot) -> str:
        if not snap.interactive_elements:
            return "(none)"
        lines = []
        for el in snap.interactive_elements[:60]:
            sel = el.selector or f"{el.tag}[name={el.name}]"
            lines.append(f"- {sel}   # {el.text[:40]!r}")
        return "\n".join(lines)