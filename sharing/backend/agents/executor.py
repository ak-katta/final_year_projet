"""Agent #3 — Executor.

Translates one Step → Playwright action, runs it.
Called once per step.
"""

from datetime import datetime
from urllib.parse import urljoin

from state import (
    QAState, Step, ActionType, TestStatus, PageSnapshot,
)
from agents.base import Agent
from capture import capture


# Actions that don't need a selector to be runnable as-is
SELECTORLESS = {"goto", "wait", "assert_url", "assert_text", "scroll", "screenshot"}

# Actions that can kick off a navigation, after which we let the page
# settle before the next step (usually an assert) reads page.url
NAVIGATING = {"click", "press", "goto", "select"}


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
        except Exception as first_err:
            # a lot of "failures" are really just the page not having
            # settled yet — give it one plain retry before assuming the
            # selector itself is wrong and reaching for the healer
            try:
                page.wait_for_timeout(500)
                self._perform(page, action, state)
                step.status = TestStatus.PASSED
            except Exception as e:
                step.status = TestStatus.FAILED
                step.error = self._short(str(e))

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

    @staticmethod
    def _is_runnable(step: Step) -> bool:
        """
        True when the planner already produced everything the step needs.

        The planner picks selectors while looking at the full element list
        for the right page; the executor's own prompt only ever saw the
        landing page's elements, so re-deriving a selector here tended to
        replace a good one with a guess from the wrong page.
        """
        a = step.action.value
        if a in SELECTORLESS:
            # these need a value instead (except the ones that need nothing)
            if a in {"goto", "assert_url", "assert_text"}:
                return bool(step.value)
            return True
        return bool(step.selector)

    def _translate(self, step: Step, state: QAState) -> dict:
        # Fast path: the plan is already executable, so don't spend an LLM
        # call per step (that was ~40 calls on a 10-test run) re-deriving it.
        if self._is_runnable(step):
            return {
                "action": step.action.value,
                "selector": step.selector or "",
                "value": step.value or "",
                "timeout_ms": step.timeout_ms,
            }

        # Underspecified step — ask the model, showing it the page we're
        # actually on rather than the original landing page
        elements = self._current_elements(state)

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

    def _current_elements(self, state: QAState) -> str:
        """Element list for whichever page the browser is on right now."""
        page = state._page
        if page is not None:
            current = page.url
            for snap in state.page_snapshots:
                if snap.url == current:
                    return self._fmt_elements(snap)
        snap = state.initial_snapshot
        return self._fmt_elements(snap) if snap else "(none)"

    # ── Perform ───────────────────────────────────────────────

    @staticmethod
    def _target(page, sel: str):
        """
        Resolve a selector to one element, preferring a visible match.

        A bare CSS selector matching several elements makes Playwright throw
        a strict-mode violation, so we always narrow to one. Plain `.first`
        isn't enough though: sites routinely ship hidden duplicates (mobile
        vs desktop nav, collapsed menus) that sit earlier in the DOM, and
        clicking one just burns the whole timeout. Prefer a visible match
        and only fall back to the first when nothing is visible yet.
        """
        loc = page.locator(sel)
        try:
            visible = loc.filter(visible=True)
            if visible.count() > 0:
                return visible.first
        except Exception:
            pass
        return loc.first

    @staticmethod
    def _settle(page, timeout: int = 5000) -> None:
        """Let a navigation triggered by the last action finish, if there was one."""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass  # no navigation, or it timed out — either way keep going

    def _resolve_url(self, page, val: str, state: QAState) -> str:
        """
        Turn whatever the planner produced into something page.goto accepts.

        Plans very often contain relative paths ("/wiki/Blog", "cart"), which
        Chromium rejects outright with "Cannot navigate to invalid URL" —
        that alone was failing whole tests at step 0.
        """
        val = (val or "").strip()
        if not val:
            return state.url
        if val.startswith(("http://", "https://", "about:", "file://")):
            return val
        base = page.url if page.url and page.url != "about:blank" else state.url
        return urljoin(base, val)

    def _perform(self, page, action: dict, state: QAState) -> None:
        a = str(action.get("action", "click")).lower()
        sel = action.get("selector", "") or ""
        val = action.get("value", "") or ""
        try:
            to = int(action.get("timeout_ms", 5000) or 5000)
        except (TypeError, ValueError):
            to = 5000

        if a not in SELECTORLESS and not sel:
            raise ValueError(f"{a!r} step has no selector")

        if a == "click":
            self._target(page, sel).click(timeout=to)
        elif a == "fill":
            loc = self._target(page, sel)
            try:
                # focuses/opens autocomplete-style search widgets first, but
                # it's only a nicety — fill() focuses on its own, so a click
                # that can't land must not fail the whole step
                loc.click(timeout=min(to, 2000))
            except Exception:
                loc = self._target(page, sel)  # re-resolve in case it detached
            loc.fill(str(val), timeout=to)
        elif a == "select":
            self._target(page, sel).select_option(str(val), timeout=to)
        elif a == "goto":
            page.goto(self._resolve_url(page, str(val), state),
                      wait_until="domcontentloaded", timeout=max(to, 15_000))
        elif a == "press":
            key = str(val) or "Enter"
            try:
                self._target(page, sel).press(key, timeout=to)
            except Exception:
                # Rich search/combobox widgets swap their input out for a new
                # node as soon as you type, so the selector the plan captured
                # is already detached by the time we press. Focus is still in
                # the replacement, so send the key to the page instead.
                if page.locator(sel).count() == 0:
                    page.keyboard.press(key)
                else:
                    raise
        elif a == "hover":
            self._target(page, sel).hover(timeout=to)
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
            # wait for it rather than sampling once — the element is often
            # still being rendered by the click in the step before this one
            try:
                self._target(page, sel).wait_for(state="visible", timeout=to)
            except Exception as e:
                raise AssertionError(f"{sel!r} not visible: {self._short(str(e), 120)}")
        elif a == "scroll":
            page.mouse.wheel(0, 800)
        elif a == "screenshot":
            capture(state, page, kind="viewport", label="manual")
        else:
            raise ValueError(f"unknown action: {a!r}")

        if a in NAVIGATING:
            # an assert_url/assert_text in the next step would otherwise race
            # the navigation this action just started and read the old page
            self._settle(page)

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
        for el in snap.interactive_elements[:150]:
            if not el.selector:
                continue  # no unique selector was found — don't offer a guess
            # Hidden or disabled controls can't be driven — a step written
            # against one is guaranteed to burn its full timeout and fail,
            # which is what most "Timeout 5000ms exceeded" results really were.
            if not el.is_visible or not el.is_enabled:
                continue
            lines.append(f"- {el.selector}   # {el.text[:40]!r}")
            if len(lines) >= 60:
                break
        return "\n".join(lines) if lines else "(none)"