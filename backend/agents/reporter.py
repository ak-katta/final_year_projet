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
Broken links: {broken_links}
Accessibility issues: {a11y_count}
UI rendering issues: {ui_count}
Load test: {load_summary}
Regression vs last run: {regression_summary}

If nothing failed, say so plainly and keep the recommendations about
widening coverage — do not invent problems that the run did not find.

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
            broken_links=len(state.broken_links),
            a11y_count=len(state.a11y_issues),
            ui_count=len(state.ui_issues),
            load_summary=(state.load_test.verdict if state.load_test else "not run"),
            regression_summary=self._regression_one_liner(state),
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
            recommendations = (
                ["Review the failed tests and bugs below."]
                if state.failed_test_ids
                else ["Widen the test plan — this run found nothing to fix."]
            )

        report_md = self._assemble(state, summary, recommendations, tokens)

        state.summary = summary
        state.full_report = report_md
        state.report_format = "markdown"
        self.log(state, "report generated")
        return state

    @staticmethod
    def _regression_one_liner(state: QAState) -> str:
        r = state.regression
        if not r:
            return "no baseline to compare against"
        return (
            f"{len(r.regressions)} regression(s), {len(r.fixes)} fix(es), "
            f"{r.stable} unchanged (baseline {r.baseline_run_id})"
        )

    # ── Deterministic sections ──────────────────────────────────

    def _fmt_bug_list(self, state: QAState) -> str:
        lines = []
        if not state.bugs:
            ran = len(state.completed_test_ids) + len(state.failed_test_ids)
            if ran and not state.failed_test_ids:
                return f"No bugs were found — all {ran} tests that ran passed."
            return "No bugs were found."

        for b in state.bugs[:20]:
            lines.append(
                f"- [{b.severity.value.upper()}] {b.title}\n"
                f"  URL: {b.url}\n"
                f"  Expected: {b.expected}\n"
                f"  Actual: {b.actual[:200]}"
            )
        return "\n".join(lines)

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
        rows = ["| Test | Area | Technique | Status | Duration | Screenshots |",
                "|---|---|---|---|---|---|"]
        for t in state.test_plan:
            rows.append(
                f"| {t.name} | {t.area.value} | {t.technique.value} | {t.status.value} "
                f"| {t.duration_ms:.0f}ms | {len(self._shots_for_test(state, t))} |"
            )
        return "\n".join(rows)

    def _fmt_passed_tests(self, state: QAState) -> str:
        """What actually worked, and the evidence for it.

        A green run used to render as three empty sections in a row, which
        reads like the tool did nothing — so passing tests get the same
        write-up failing ones do.
        """
        passed = [t for t in state.test_plan if t.status == TestStatus.PASSED]
        if not passed:
            return "No tests passed."

        lines = []
        for t in passed:
            verified = t.expected or t.description or "completed without error"
            lines.append(
                f"- **{t.id} · {t.name}** ({t.duration_ms:.0f}ms) — verified: {verified}"
            )
            shots = self._shots_for_test(state, t)
            if shots:
                lines.append(
                    "  - evidence: "
                    + ", ".join(f"[{s.label or s.kind}]({self._shot_url(s)})" for s in shots)
                )
        return "\n".join(lines)

    def _shots_for_test(self, state: QAState, test) -> list:
        """Screenshots belonging to one test, newest capture order preserved."""
        wanted = set(test.screenshot_ids) | set(
            state.screenshots_by_test.get(test.id, [])
        )
        return [s for s in state.screenshots if s.id in wanted]

    @staticmethod
    def _shot_url(shot) -> str:
        # matches how main.py mounts runs/ as static files
        return f"/runs/{shot.relative_path}"

    def _fmt_screenshots(self, state: QAState) -> str:
        """Every screenshot the run produced, grouped under its test."""
        if not state.screenshots:
            return "No screenshots were captured."

        lines = []
        claimed = set()
        for t in state.test_plan:
            shots = self._shots_for_test(state, t)
            if not shots:
                continue
            lines.append(f"\n**{t.id} · {t.name}** ({t.status.value})\n")
            for s in shots:
                claimed.add(s.id)
                lines.append(f"![{s.label or s.kind}]({self._shot_url(s)})")

        loose = [s for s in state.screenshots if s.id not in claimed]
        if loose:
            lines.append("\n**Run-level captures**\n")
            for s in loose:
                lines.append(f"![{s.label or s.kind}]({self._shot_url(s)})")

        return "\n".join(lines)

    # ── Coverage & methodology ──────────────────────────────────

    def _fmt_coverage(self, state: QAState) -> str:
        """Per-area tally — the proof the run actually spread itself around."""
        matrix = state.coverage_matrix()
        if not matrix:
            return "No tests were planned."
        rows = ["| Area | Tests | Passed | Failed | Not run | Techniques |",
                "|---|---|---|---|---|---|"]
        for area in sorted(matrix):
            r = matrix[area]
            rows.append(
                f"| {area} | {r['planned']} | {r['passed']} | {r['failed']} "
                f"| {r['not_run'] + r['skipped']} | {', '.join(r['techniques'])} |"
            )
        return "\n".join(rows)

    def _fmt_methods(self, state: QAState) -> str:
        counts = state.technique_counts()
        if not counts:
            return "No tests were planned."
        lines = [
            "This run is **black-box** throughout: the agent drives the site "
            "through a real browser and never reads the application's source. "
            "Methods exercised:",
            "",
        ]
        labels = {
            "functional": "Functional — buttons, links, forms, navigation, search",
            "negative": "Negative — invalid inputs, wrong data, broken links",
            "validation": "Validation — expected vs actual comparison",
            "ui": "UI — visible elements and user interactions",
            "accessibility": "Accessibility — basic WCAG checks",
            "exploratory": "Exploratory — agent discovered the pages itself",
            "regression": "Regression — replay of a saved baseline plan",
            "load": "Load — many concurrent requests against the server",
        }
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- **{labels.get(name, name)}** — {n} test(s)")
        pages = state.site_metadata.get("pages_discovered")
        if pages:
            lines.append(
                f"- **Exploratory crawl** — {pages} page(s) discovered autonomously"
            )
        return "\n".join(lines)

    # ── Load / regression / findings ────────────────────────────

    def _fmt_load_test(self, state: QAState) -> str:
        lt = state.load_test
        if not lt:
            return "Not run. Enable it with `--load` (CLI) or `load_test: true` (API)."
        verdict = "PASSED" if lt.passed else "FAILED"
        lines = [
            f"**{verdict}** — {lt.verdict}",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Target | {lt.url} |",
            f"| Concurrency | {lt.concurrency} simultaneous clients |",
            f"| Requests sent | {lt.completed} of {lt.total_requests} |",
            f"| Wall time | {lt.duration_s:.2f}s |",
            f"| Throughput | {lt.requests_per_second:.1f} req/s |",
            f"| Succeeded / failed | {lt.succeeded} / {lt.failed} |",
            f"| Error rate | {lt.error_rate:.2%} (threshold {lt.max_error_rate:.0%}) |",
            f"| Latency min / mean / max | {lt.latency_min_ms:.0f} / {lt.latency_mean_ms:.0f} / {lt.latency_max_ms:.0f} ms |",
            f"| Latency p50 / p90 / p95 / p99 | {lt.latency_p50_ms:.0f} / {lt.latency_p90_ms:.0f} / {lt.latency_p95_ms:.0f} / {lt.latency_p99_ms:.0f} ms |",
            f"| Unloaded baseline | {lt.baseline_ms:.0f} ms |",
            f"| Degradation under load | {lt.degradation_factor:.2f}x slower at p50 |",
            f"| Status codes | {lt.status_counts or '(none)'} |",
            f"| Connection errors | {lt.errors or '(none)'} |",
        ]
        return "\n".join(lines)

    def _fmt_regression(self, state: QAState) -> str:
        r = state.regression
        if not r:
            return "No earlier run of this URL to compare against — this run becomes the baseline."
        lines = [
            f"Compared **{r.compared}** test(s) against baseline run "
            f"`{r.baseline_run_id}`"
            + (f" from {r.baseline_started_at:%Y-%m-%d %H:%M}" if r.baseline_started_at else "")
            + ".",
            "",
        ]
        if r.regressions:
            lines.append(f"**{len(r.regressions)} regression(s)** — these passed before and fail now:")
            lines += [f"- {x['test']} ({x['area']}) — {x['error'] or 'no error recorded'}"
                      for x in r.regressions]
            lines.append("")
        if r.fixes:
            lines.append(f"**{len(r.fixes)} fix(es)** — these failed before and pass now:")
            lines += [f"- {x['test']} ({x['area']})" for x in r.fixes]
            lines.append("")
        if r.still_failing:
            lines.append(f"**{len(r.still_failing)} still failing:**")
            lines += [f"- {x['test']} ({x['area']})" for x in r.still_failing]
            lines.append("")
        lines.append(f"{r.stable} test(s) unchanged and still passing.")
        if r.new_tests:
            lines.append(f"{len(r.new_tests)} test(s) are new in this run.")
        if r.missing_tests:
            lines.append(f"{len(r.missing_tests)} baseline test(s) were not run this time.")
        if not (r.regressions or r.fixes or r.still_failing):
            lines.append("No behaviour changed since the baseline.")
        return "\n".join(lines)

    def _fmt_broken_links(self, state: QAState) -> str:
        checked = state.site_metadata.get("links_checked", 0)
        if not state.broken_links:
            return f"Checked {checked} link(s) — none broken."
        lines = [f"Checked {checked} link(s), **{len(state.broken_links)} broken**:", ""]
        for b in state.broken_links[:25]:
            status = b.get("status") or "unreachable"
            text = f" — link text: {b['text']!r}" if b.get("text") else ""
            err = f" ({b['error']})" if b.get("error") else ""
            lines.append(f"- `{status}` {b['url']}{text}{err}")
        return "\n".join(lines)

    def _fmt_a11y(self, state: QAState) -> str:
        if not state.a11y_issues:
            return "No accessibility issues found by the basic checks."
        by_rule: dict[str, int] = {}
        for i in state.a11y_issues:
            by_rule[i.rule] = by_rule.get(i.rule, 0) + 1
        lines = [
            f"**{len(state.a11y_issues)} issue(s)** across "
            f"{len(by_rule)} rule(s): "
            + ", ".join(f"{k} ×{v}" for k, v in sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "",
            "| Impact | Rule | Element | Detail |",
            "|---|---|---|---|",
        ]
        order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3, "": 4}
        for i in sorted(state.a11y_issues, key=lambda x: order.get(x.impact, 4))[:30]:
            detail = i.description.replace("|", "\\|")[:160]
            lines.append(f"| {i.impact or '-'} | {i.rule} | `{i.selector}` | {detail} |")
        return "\n".join(lines)

    def _fmt_ui_issues(self, state: QAState) -> str:
        if not state.ui_issues:
            return "No UI rendering problems found."
        by_rule: dict[str, int] = {}
        for i in state.ui_issues:
            by_rule[i["rule"]] = by_rule.get(i["rule"], 0) + 1
        lines = [
            f"**{len(state.ui_issues)} finding(s)**: "
            + ", ".join(f"{k} ×{v}" for k, v in sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "",
            "| Rule | Element | Detail |",
            "|---|---|---|",
        ]
        for i in state.ui_issues[:30]:
            lines.append(
                f"| {i['rule']} | `{i['selector']}` | {str(i['description']).replace('|', chr(92) + '|')[:160]} |"
            )
        return "\n".join(lines)

    def _fmt_console_network(self, state: QAState) -> str:
        lines = []
        errs = list(state.console_errors) + list(state.page_errors)
        if errs:
            lines.append(f"**{len(errs)} console error(s):**")
            lines += [f"- `{e[:200]}`" for e in errs[:15]]
        else:
            lines.append("No JavaScript console errors.")
        lines.append("")
        fails = [f"{e.status or 'failed'} {e.url}" for e in state.network_failures]
        fails += [f"failed {u}" for u in state.request_failures]
        fails = list(dict.fromkeys(fails))
        if fails:
            lines.append(f"**{len(fails)} failed network request(s):**")
            lines += [f"- `{f[:200]}`" for f in fails[:20]]
        else:
            lines.append("No failed HTTP or API requests.")
        return "\n".join(lines)

    def _fmt_recommendations(self, recommendations: list[str]) -> str:
        return "\n".join(f"- {r}" for r in recommendations)

    def _assemble(
        self, state: QAState, summary: str, recommendations: list[str], tokens: int
    ) -> str:
        return f"""# QA Report — Run `{state.run_id}`

**URL:** {state.url}
**Site type:** {state.site_type.value}
**Duration:** {state.duration_ms:.0f}ms
**Pass rate:** {state.pass_rate:.0%} — {len(state.completed_test_ids)} passed, {len(state.failed_test_ids)} failed, of {len(state.completed_test_ids) + len(state.failed_test_ids) + len(state.skipped_test_ids)} run ({len(state.test_plan)} planned)
**Screenshots:** {len(state.screenshots)}
**Approach:** black-box — the agent drives a real browser and never reads the app's source

## Summary
{summary}

## Testing methods used
{self._fmt_methods(state)}

## Coverage by area
{self._fmt_coverage(state)}

## Test results
{self._fmt_test_table(state)}

## Passed tests
{self._fmt_passed_tests(state)}

## Why tests failed
{self._fmt_failure_reasons(state)}

## Bugs found
{self._fmt_bug_list(state)}

## Load test
{self._fmt_load_test(state)}

## Regression vs previous run
{self._fmt_regression(state)}

## Broken links
{self._fmt_broken_links(state)}

## Accessibility
{self._fmt_a11y(state)}

## UI rendering
{self._fmt_ui_issues(state)}

## Console & network
{self._fmt_console_network(state)}

## Screenshots
{self._fmt_screenshots(state)}

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
