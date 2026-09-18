"""Agent #4 — Judge.

When a test fails, classify: real bug? flake? infra error? severity?
"""

from state import (
    QAState, TestCase, Bug, Severity, TestCategory, TestStatus,
)
from agents.base import Agent


SYSTEM = """You are a QA triage expert.
Distinguish real bugs from test flakes and infrastructure errors.
Reply with ONLY valid JSON. No prose."""


PROMPT = """A test failed. Classify it.

Test name:     {name}
Category:      {category}
Expected:      {expected}

Failed step:   {step_desc}
Action:        {action}   selector={selector}
Error:         {error}

Signals:
- Console errors:   {console}
- Failed requests:  {network}
- Page URL:         {url}

Reply with ONLY:
{{
  "is_real_bug": true,
  "reason": "why you decided this",
  "severity": "critical|high|medium|low|info",
  "category": "functional|ui|a11y|performance|security|compatibility|visual|content|seo",
  "title": "short bug title",
  "description": "what went wrong",
  "repro_steps": ["step 1", "step 2"]
}}"""


class JudgeAgent(Agent):
    name = "judge"
    purpose = "judge"

    def run(self, state: QAState, test: TestCase | None = None) -> QAState:
        if test is None and state.current_test_id:
            test = state.get_test(state.current_test_id)
        if test is None:
            return state
        if test.status not in (TestStatus.FAILED, TestStatus.ERROR):
            return state

        verdict = self._judge(test, state)
        if not verdict:
            return state

        if verdict.get("is_real_bug"):
            self._record_bug(test, verdict, state)
        else:
            self.log(
                state,
                f"not a bug ({verdict.get('reason', 'unknown')}): {test.name}",
            )
        return state

    # ── Bug recording ─────────────────────────────────────────

    def _record_bug(self, test: TestCase, verdict: dict, state: QAState) -> None:
        hero = next(
            (
                s for s in state.screenshots
                if s.test_id == test.id and s.kind == "on_failure"
            ),
            None,
        )

        severity = self._safe_severity(verdict.get("severity"))
        category = self._safe_category(verdict.get("category"))

        bug = Bug(
            test_id=test.id,
            title=str(verdict.get("title") or f"{test.name} failed"),
            description=str(verdict.get("description", test.error or "")),
            severity=severity,
            category=category,
            steps_to_reproduce=list(verdict.get("repro_steps") or []),
            expected=test.expected,
            actual=test.error or "",
            url=state.url,
            screenshot_id=hero.id if hero else None,
            screenshot_ids=list(test.screenshot_ids),
            console_errors=state.console_errors[-5:],
            network_failures=[n.url for n in state.network_failures[-5:]],
        )

        bug.fingerprint = self._fingerprint(bug)

        if hero:
            hero.bug_id = bug.id
            state.screenshots_by_bug.setdefault(bug.id, []).append(hero.id)

        state.add_bug(bug)
        self.log(
            state,
            f"bug recorded: {bug.title} [{bug.severity.value}]",
        )

    # ── Judge call ────────────────────────────────────────────

    def _judge(self, test: TestCase, state: QAState) -> dict | None:
        failed_step = next(
            (s for s in test.steps if s.status == TestStatus.FAILED),
            test.steps[-1] if test.steps else None,
        )
        if failed_step is None:
            return None

        prompt = PROMPT.format(
            name=test.name,
            category=test.category.value,
            expected=test.expected or "(not specified)",
            step_desc=failed_step.description,
            action=failed_step.action.value,
            selector=failed_step.selector,
            error=(failed_step.error or test.error or "")[:400],
            console="; ".join(state.console_errors[-3:]) or "(none)",
            network="; ".join(n.url for n in state.network_failures[-3:]) or "(none)",
            url=state.url,
        )
        raw = self.ask(prompt, state, system=SYSTEM)
        data = self.extract_json(raw)
        if not isinstance(data, dict):
            self.log(state, "judge output not a dict; assuming real bug")
            return {"is_real_bug": True, "severity": "medium"}
        return data

    # ── Enum coercion ─────────────────────────────────────────

    @staticmethod
    def _safe_severity(v) -> Severity:
        try:
            return Severity(str(v).lower())
        except ValueError:
            return Severity.MEDIUM

    @staticmethod
    def _safe_category(v) -> TestCategory:
        try:
            return TestCategory(str(v).lower())
        except ValueError:
            return TestCategory.FUNCTIONAL

    # ── Fingerprint ───────────────────────────────────────────

    @staticmethod
    def _fingerprint(bug: Bug) -> str:
        import hashlib
        key = f"{bug.test_id}|{bug.title}|{bug.url}".lower()
        return hashlib.sha1(key.encode()).hexdigest()[:16]