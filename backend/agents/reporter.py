"""Agent #5 — Reporter.

Generates a markdown report from final state.
"""

from state import QAState
from agents.base import Agent


SYSTEM = """You are a QA lead writing an executive report.
Be concise, factual, actionable. Use markdown."""


PROMPT = """Write a QA report.

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
Screenshots captured: {shot_count}

LLM usage: {llm_calls} calls, {tokens} tokens, ${cost:.4f}

Write a markdown report with:
1. **Summary** — 2-3 sentences
2. **Critical issues** — bullet list (only if critical bugs exist)
3. **Test results** — table of pass/fail
4. **Recommendations** — top 3 next steps
5. **Run metadata** — model used, tokens, cost
"""


class ReporterAgent(Agent):
    name = "reporter"
    purpose = "report"

    def run(self, state: QAState) -> QAState:
        # Compact bug list
        bug_lines: list[str] = []
        for b in state.bugs[:20]:
            bug_lines.append(
                f"- [{b.severity.value.upper()}] {b.title}\n"
                f"  URL: {b.url}\n"
                f"  Expected: {b.expected}\n"
                f"  Actual: {b.actual[:200]}"
            )
        bug_list = "\n".join(bug_lines) or "(no bugs)"

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
            shot_count=len(state.screenshots),
            llm_calls=state.total_llm_calls,
            tokens=tokens,
            cost=state.estimated_cost_usd,
        )

        report_md = self.ask(prompt, state, system=SYSTEM)

        state.summary = report_md[:500]
        state.full_report = report_md
        state.report_format = "markdown"
        self.log(state, "report generated")
        return state