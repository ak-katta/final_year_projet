"""Agent #5 — Reporter.

Builds the markdown report from final state. The bulk of it (test
results, why each test failed, bug list) is assembled directly from
state data so it's always in the same structured shape — only the
summary and recommendations come from the LLM, and even those are asked
for as JSON so a paragraph-happy model can't turn the whole thing back
into prose.
"""

from state import QAState, TestStatus
from agents.base import Agent


SYSTEM = """You are a QA lead writing an executive summary.
Reply with ONLY valid JSON. No prose, no markdown fences."""


PROMPT = """Summarize this QA run.

Run: {run_id}
URL: {url}
Site type: {site_type}
Duration: {duration_ms:.0f}ms

Tests planned: {planned}
Tests passed:  {passed}
Tests failed:  {failed}
Pass rate:     {pass_rate:.0%}

Bugs found: {bug_count}
Severity breakdown: {severities}

Bug details:
{bug_list}

Console errors on page: {console_count}
Failed network requests: {network_count}

Reply with ONLY:
{{
  "summary": "2-3 sentences on the overall health of this run",
  "recommendations": ["short actionable next step", "...", "..."]
}}
"""


class ReporterAgent(Agent):
    name = "reporter"
    purpose = "report"

    def run(self, state: QAState) -> QAState:
        bug_list = self._fmt_bug_list(state)
        tokens = state.total_prompt_tokens + state.total_completion_tokens

        prompt = PROMPT.format(
            run_id=state.run_id,
            url=state.url,
            site_type=state.site_type.value,
            duration_ms=state.duration_ms,
            planned=len(state.test_plan),
            passed=len(state.completed_test_ids),
            failed=len(state.failed_test_ids),
            pass_rate=state.pass_rate,
            bug_count=len(state.bugs),
            severities=state.severity_counts,
            bug_list=bug_list,
            console_count=len(state.console_errors),
            network_count=len(state.network_failures),
        )

        raw = self.ask(prompt, state, system=SYSTEM)
        data = self.extract_json(raw)
        if isinstance(data, dict):
            summary = str(data.get("summary") or "").strip()
            recommendations = [
                str(r).strip() for r in (data.get("recommendations") or []) if str(r).strip()
            ]
        else:
            # model ignored the JSON instruction — fall back to using its
            # raw text as the summary rather than losing the run entirely
            summary = raw.strip()
            recommendations = []

        if not summary:
            summary = f"{len(state.completed_test_ids)}/{len(state.test_plan)} tests passed."
        if not recommendations:
            recommendations = ["Review the failed tests and bugs below."]

        report_md = self._assemble(state, summary, recommendations, tokens)

        state.summary = summary
        state.full_report = report_md
        state.report_format = "markdown"
        self.log(state, "report generated")
        return state

    # ── Deterministic sections ──────────────────────────────────

    def _fmt_bug_list(self, state: QAState) -> str:
        lines = []
        for b in state.bugs[:20]:
            lines.append(
                f"- [{b.severity.value.upper()}] {b.title}\n"
                f"  URL: {b.url}\n"
                f"  Expected: {b.expected}\n"
                f"  Actual: {b.actual[:200]}"
            )
        return "\n".join(lines) or "(no bugs)"

    def _fmt_failure_reasons(self, state: QAState) -> str:
        failed = [t for t in state.test_plan if t.status in (TestStatus.FAILED, TestStatus.ERROR)]
        if not failed:
            return "No tests failed."

        bugs_by_test = {b.test_id: b for b in state.bugs}
        lines = []
        for t in failed:
            bug = bugs_by_test.get(t.id)
            if bug:
                reason = bug.description or bug.actual or t.error or "no reason recorded"
                lines.append(f"- **{t.id} · {t.name}** [{bug.severity.value}] — {reason}")
            else:
                reason = t.error or "failed, but not confirmed as an application bug — likely a flake or a stale selector"
                lines.append(f"- **{t.id} · {t.name}** — {reason}")
        return "\n".join(lines)

    def _fmt_test_table(self, state: QAState) -> str:
        if not state.test_plan:
            return "No tests were planned."
        rows = ["| Test | Category | Status |", "|---|---|---|"]
        for t in state.test_plan:
            rows.append(f"| {t.name} | {t.category.value} | {t.status.value} |")
        return "\n".join(rows)

    def _fmt_recommendations(self, recommendations: list[str]) -> str:
        return "\n".join(f"- {r}" for r in recommendations)

    def _assemble(
        self, state: QAState, summary: str, recommendations: list[str], tokens: int
    ) -> str:
        return f"""# QA Report — Run `{state.run_id}`

**URL:** {state.url}
**Site type:** {state.site_type.value}
**Duration:** {state.duration_ms:.0f}ms
**Pass rate:** {state.pass_rate:.0%} ({len(state.completed_test_ids)}/{len(state.test_plan)} tests passed, {len(state.failed_test_ids)} failed)

## Summary
{summary}

## Why tests failed
{self._fmt_failure_reasons(state)}

## Test results
{self._fmt_test_table(state)}

## Bugs found
{self._fmt_bug_list(state)}

## Recommendations
{self._fmt_recommendations(recommendations)}

## Run metadata
- LLM calls: {state.total_llm_calls}
- Tokens used: {tokens}
- Estimated cost: ${state.estimated_cost_usd:.4f}
- Screenshots captured: {len(state.screenshots)}
- Console errors: {len(state.console_errors)}
- Failed network requests: {len(state.network_failures)}
"""
