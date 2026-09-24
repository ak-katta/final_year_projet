"""Agent #2 — Test Planner.

Input:  site_type, plan_notes, initial_snapshot
Output: state.test_plan (list[TestCase])
"""

from pydantic import ValidationError

from state import (
    QAState, TestCase, Step, ActionType, TestCategory, Severity, PageSnapshot,
)
from agents.base import Agent


SYSTEM = """You are a senior QA engineer.
Generate concrete, executable test plans.
Reply with ONLY valid JSON.
Every step MUST use a real selector from the provided element list.
Do not invent selectors. Do not wrap output in markdown fences."""


PROMPT = """Plan tests for this website.

URL: {url}
Site type: {site_type}

Domain guidance:
{notes}

Pages discovered, each with its own elements and forms. A test can only
use selectors from ONE page block below, and its first step MUST be a
"goto" to that page's URL — selectors from other pages won't exist until
you navigate there:
{pages}

Generate 8-15 test cases covering the domain guidance, spread across the
discovered pages where relevant.

Reply with ONLY this JSON array:
[
  {{
    "name": "Add product to cart",
    "category": "functional",
    "description": "Verify cart updates when adding a product",
    "expected": "Cart badge shows 1",
    "severity_if_fail": "high",
    "steps": [
      {{"action": "goto", "value": "https://example.com/cart", "description": "Open the cart page"}},
      {{"action": "click", "selector": "#add-to-cart", "description": "Click add to cart"}},
      {{"action": "assert_text", "value": "1", "description": "Cart shows 1 item"}}
    ]
  }}
]

Every "goto" step's value MUST be a full absolute URL copied from one of
the page headers above (starting with http:// or https://), never a bare
path like "/cart".

Valid categories: functional, ui, a11y, performance, security, compatibility, visual, content, seo
Valid actions:    click, fill, select, goto, press, hover, wait, assert_text, assert_url, assert_visible, scroll, screenshot
Valid severities: critical, high, medium, low, info

For a search box: filling it is not enough by itself — the search still
needs to be submitted. Follow the "fill" step with either a "press" step
(selector = the same search box, value = "Enter") or a "click" on a
visible search/submit button, and only then assert on the results.
"""


class PlannerAgent(Agent):
    name = "planner"
    purpose = "plan"

    def __init__(self, llm, healer=None):
        super().__init__(llm)
        self.healer = healer

    def run(self, state: QAState) -> QAState:
        if not state.initial_snapshot and not state.page_snapshots:
            state.add_error("planner", "no initial_snapshot")
            return state

        prompt = PROMPT.format(
            url=state.url,
            site_type=state.site_type.value,
            notes=state.plan_notes or "(none)",
            pages=self._fmt_pages(state),
        )

        raw = self.ask(prompt, state, system=SYSTEM)
        cases = self._parse_cases(raw, state)

        # Self-heal if zero valid cases
        if not cases and self.healer:
            self.log(state, "no valid cases; asking healer")
            fixed = self.healer.repair_json(
                state, raw,
                schema_hint="JSON array of test cases with name/category/steps",
            )
            cases = self._parse_cases(fixed, state)

        state.test_plan = cases
        state.plan_generated_by = state.current_model or "unknown"
        state.plan_revision_count += 1
        self.log(state, f"planned {len(cases)} tests")
        return state

    # ── Parsing ───────────────────────────────────────────────

    def _parse_cases(self, raw: str, state: QAState) -> list[TestCase]:
        data = self.extract_json(raw)
        if not isinstance(data, list):
            # Model may have wrapped it
            if isinstance(data, dict):
                for k in ("tests", "test_cases", "cases", "plan"):
                    if isinstance(data.get(k), list):
                        data = data[k]
                        break
            if not isinstance(data, list):
                self.log(state, "planner output not a list")
                return []

        cases: list[TestCase] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            try:
                steps = self._build_steps(item.get("steps", []), state, i)
                if not steps:
                    continue
                case = TestCase(
                    name=str(item.get("name") or f"Test {i+1}"),
                    category=self._safe_category(item.get("category")),
                    description=str(item.get("description", "")),
                    expected=str(item.get("expected", "")),
                    severity_if_fail=self._safe_severity(
                        item.get("severity_if_fail")
                    ),
                    steps=steps,
                    tags=item.get("tags", []) if isinstance(item.get("tags"), list) else [],
                )
                cases.append(case)
            except ValidationError as e:
                self.log(state, f"case {i} invalid: {e.errors()[:2]}")
            except Exception as e:
                self.log(state, f"case {i} unexpected: {e}")
        return cases

    def _build_steps(self, raw_steps, state: QAState, case_idx: int) -> list[Step]:
        steps: list[Step] = []
        if not isinstance(raw_steps, list):
            return steps
        for j, s in enumerate(raw_steps):
            if not isinstance(s, dict):
                continue
            try:
                steps.append(Step(
                    index=j,
                    description=str(s.get("description", "")),
                    action=self._safe_action(s.get("action")),
                    selector=str(s.get("selector", "")),
                    value=str(s.get("value", "")),
                    timeout_ms=int(s.get("timeout_ms", 5000) or 5000),
                ))
            except (ValidationError, ValueError, TypeError) as e:
                self.log(state, f"step {case_idx}.{j} bad: {e}")
        return steps

    # ── Enum coercion ─────────────────────────────────────────

    @staticmethod
    def _safe_category(v) -> TestCategory:
        try:
            return TestCategory(str(v).lower())
        except ValueError:
            return TestCategory.FUNCTIONAL

    @staticmethod
    def _safe_severity(v) -> Severity:
        try:
            return Severity(str(v).lower())
        except ValueError:
            return Severity.MEDIUM

    @staticmethod
    def _safe_action(v) -> ActionType:
        try:
            return ActionType(str(v).lower())
        except ValueError:
            return ActionType.CLICK

    # ── Formatting helpers ────────────────────────────────────

    def _fmt_pages(self, state: QAState) -> str:
        snaps = state.page_snapshots or (
            [state.initial_snapshot] if state.initial_snapshot else []
        )
        if not snaps:
            return "(none)"
        # cap it — with a lot of crawled pages this section alone could
        # blow past a reasonable prompt size
        blocks = []
        for snap in snaps[:8]:
            blocks.append(
                f"--- {snap.url} ---\n"
                f"Elements:\n{self._fmt_elements(snap)}\n"
                f"Forms:\n{self._fmt_forms(snap)}"
            )
        return "\n\n".join(blocks)

    def _fmt_elements(self, snap: PageSnapshot) -> str:
        if not snap.interactive_elements:
            return "(none)"
        lines = []
        for el in snap.interactive_elements[:150]:
            if not el.selector:
                continue  # scraper couldn't pin this one down uniquely
            # Hidden or disabled controls can't be driven — a step written
            # against one is guaranteed to burn its full timeout and fail,
            # which is what most "Timeout 5000ms exceeded" results really were.
            if not el.is_visible or not el.is_enabled:
                continue
            lines.append(f"- {el.selector}   # {el.text[:40]!r}")
            if len(lines) >= 60:
                break
        return "\n".join(lines) if lines else "(none)"

    def _fmt_forms(self, snap: PageSnapshot) -> str:
        if not snap.forms:
            return "(none)"
        out = []
        for f in snap.forms:
            names = ", ".join(fld.name for fld in f.fields)
            out.append(f"- action={f.action} method={f.method} fields=[{names}]")
        return "\n".join(out)