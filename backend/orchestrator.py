"""
Runs one full QA pass on a URL: load the page, figure out what kind of
site it is, plan some tests for it, run them, audit what the LLM can't
reliably judge, optionally load the server, and write up what broke.

Everything gets written onto the QAState object as it goes, so a caller
(the API, a script, whatever) can just keep reading `state` to see how
far along a run is.

The run is black-box throughout: nothing here reads the application's
source, it only drives a real browser and reads real HTTP responses.

Phases, in order:
  crawl        → exploratory: the agent discovers pages on its own
  plan         → LLM writes functional / negative / validation tests
                 (or a saved baseline plan is reused, for regression)
  execute      → each planned test is driven through the browser
  checks       → deterministic audits (links, a11y, UI, console,
                 network, page load) that don't need an LLM
  load         → optional: many concurrent requests at the server
  regression   → optional: diff against the last run of the same URL
  report       → markdown + HTML + PDF, with screenshots attached
"""

from datetime import datetime

import capture
import checks
import paths
import registry
import load_test as load_test_mod
import regression as regression_mod
import report_html
from state import QAState, Phase, TestStatus, TestTechnique
from llm_router import LLMRouter
from agents.classifier import ClassifierAgent
from agents.domain_expert import DomainExpertAgent
from agents.planner import PlannerAgent
from agents.executor import ExecutorAgent
from agents.judge import JudgeAgent
from agents.healer import HealerAgent
from agents.reporter import ReporterAgent


def run_qa(
    state: QAState,
    headless: bool = True,
    max_tests: int | None = None,
    max_pages: int = 1,
    run_checks: bool = True,
    load_test: bool = False,
    load_concurrency: int = load_test_mod.DEFAULT_CONCURRENCY,
    load_requests: int = load_test_mod.DEFAULT_REQUESTS,
    regression: bool = True,
    reuse_baseline_plan: bool = False,
) -> QAState:
    """
    One full QA pass.

    max_tests limits only the LLM-planned tests — the deterministic checks
    always run, since they're cheap and they're what covers links, a11y,
    console, network and UI.

    load_test is opt-in on purpose: hundreds of concurrent requests look
    like an attack from the receiving end, so it should only ever be
    pointed at a server you own or are authorised to test.
    """
    router = LLMRouter.from_env()
    healer = HealerAgent(router)

    classifier = ClassifierAgent(router)
    domain_expert = DomainExpertAgent(router)
    planner = PlannerAgent(router, healer=healer)
    executor = ExecutorAgent(router, healer=healer)
    judge = JudgeAgent(router)
    reporter = ReporterAgent(router)

    try:
        page = capture.launch(state, headless=headless)
        state.mark_phase(Phase.NAVIGATED)

        # ── Exploratory: let the agent discover the site itself ──
        snapshots = capture.crawl(state, page, max_pages=max_pages)
        if snapshots:
            state.initial_snapshot = snapshots[0]
        if page.url != state.url:
            # crawl left us on whatever page it visited last — come back to
            # the starting URL so execution begins from a known page
            page.goto(state.url, wait_until="domcontentloaded", timeout=30_000)

        # Everything from here to the end of planning needs an LLM. When
        # the providers are down or rate-limited that used to abort the
        # whole run — losing the deterministic checks and the load test,
        # neither of which needs a model at all. Degrade instead: keep an
        # empty plan and carry on to the parts that still work.
        try:
            classifier.run(state)
            domain_expert.run(state)
            state.mark_phase(Phase.ANALYZED)
            _build_plan(state, planner, reuse_baseline_plan)
        except Exception as e:
            state.add_warning(
                f"LLM planning unavailable ({e}); continuing with the "
                f"deterministic checks only"
            )
        state.mark_phase(Phase.PLANNED)

        tests = state.test_plan[:max_tests] if max_tests else list(state.test_plan)
        state.mark_phase(Phase.EXECUTING)
        state.execution_started_at = datetime.now()

        for test in tests:
            if state.cancel_requested:
                break
            _execute_test(state, page, test, executor, judge)

        # Anything the cancel cut short is reported as skipped rather than
        # left sitting at "pending", so the results read honestly.
        if state.cancel_requested:
            for test in tests:
                if test.status == TestStatus.PENDING:
                    state.finish_test(test.id, TestStatus.SKIPPED)

        # ── Deterministic audits — these append their own test cases ──
        # Skipped on a cancel: they're quick, but the whole point of
        # cancelling is to stop touching the target site.
        if run_checks and not state.cancel_requested:
            try:
                checks.run_all(state, page)
            except Exception as e:
                state.add_warning(f"deterministic checks failed: {e}")

        state.execution_finished_at = datetime.now()

        # ── Load test: opt-in, and last, so a struggling server can't
        #    poison the functional results that already ran ──
        if load_test and not state.cancel_requested:
            try:
                load_test_mod.run_for_state(
                    state,
                    concurrency=load_concurrency,
                    total_requests=load_requests,
                )
            except Exception as e:
                state.add_warning(f"load test failed: {e}")

        # ── Regression: diff against the last run of this same URL ──
        if regression and not state.cancel_requested:
            try:
                result = regression_mod.run(state)
                if result:
                    state.site_metadata["regression_baseline"] = result.baseline_run_id
            except Exception as e:
                state.add_warning(f"regression comparison failed: {e}")

        state.mark_phase(Phase.REPORTING)

        # Roll the numbers up first — the reporter prints pass rate and
        # duration, and finish() used to compute them only afterwards
        state.compute_metrics()
        try:
            reporter.run(state)
        except Exception as e:
            # only the summary and recommendations come from the model —
            # the rest of the report is assembled from state, so write it
            # with a placeholder summary rather than losing it entirely
            state.add_warning(f"LLM summary unavailable ({e}); report written without it")
            state.summary = (
                f"{len(state.completed_test_ids)} of "
                f"{len(state.completed_test_ids) + len(state.failed_test_ids)} "
                f"executed tests passed."
            )
            state.full_report = reporter._assemble(
                state, state.summary,
                ["Review the results below."],
                state.total_prompt_tokens + state.total_completion_tokens,
            )
            state.report_format = "markdown"

        if state.cancel_requested:
            # keep everything that was produced, but don't call this a
            # completed pass — finish() would mark the run DONE
            state.compute_metrics()
            state.mark_aborted("cancelled by user")
        else:
            state.finish()

    except Exception as e:
        state.mark_failed(str(e))

    finally:
        capture.close(state)
        # Always write the HTML out, even for a failed run — otherwise the
        # dashboard the frontend links to simply doesn't exist and the user
        # is left staring at a blank page with no clue what went wrong.
        try:
            html_paths = report_html.generate(state)
            state.site_metadata["dashboard_url"] = (
                f"/runs/{state.run_id}/{html_paths['dashboard'].name}"
            )
            state.site_metadata["report_html_url"] = (
                f"/runs/{state.run_id}/{html_paths['report'].name}"
            )
        except Exception as e:
            state.add_warning(f"could not write HTML report: {e}")
        try:
            state.save(paths.run_dir(state.output_dir, state.run_id) / "state.json")
        except Exception as e:
            state.add_warning(f"could not save state to disk: {e}")
        # small sidecar the history list reads instead of re-parsing the
        # (multi-megabyte) state.json for every run on every page load
        registry.write_summary(state)

    return state


# ── Pieces of the run ───────────────────────────────────────────────

def _build_plan(state: QAState, planner: PlannerAgent, reuse_baseline_plan: bool) -> None:
    """Replay the last run's plan when asked, otherwise plan fresh.

    Replaying is what makes a comparison meaningful: if the LLM invents a
    different set of tests each time, a red test in run 2 tells you nothing
    about run 1.
    """
    if reuse_baseline_plan:
        baseline = regression_mod.find_baseline(state)
        if baseline is not None:
            plan = regression_mod.reuse_plan(baseline)
            if plan:
                for t in plan:
                    t.technique = TestTechnique.REGRESSION
                state.test_plan = plan
                state.plan_generated_by = f"replay of run {baseline.run_id}"
                state.site_metadata["replayed_from"] = baseline.run_id
                return
        state.add_warning(
            "no baseline run found for this URL — planning fresh tests instead"
        )

    planner.run(state)


def _execute_test(state: QAState, page, test, executor: ExecutorAgent, judge: JudgeAgent) -> None:
    """Drive one test's steps through the browser and record the outcome."""
    state.start_test(test.id)

    for step in test.steps:
        if state.cancel_requested:
            break
        state.current_step_index = step.index
        executor.run(state, step=step)
        if step.status == TestStatus.FAILED:
            break  # no point chasing later steps once one's broken

    first_error = next((s.error for s in test.steps if s.error), None)
    if first_error:
        status = TestStatus.FAILED
    elif any(s.status == TestStatus.PENDING for s in test.steps):
        # a cancel stopped this test part-way. Its steps hadn't failed, but
        # they hadn't finished either — calling that a pass would put a
        # green tick against an assertion that never ran
        status = TestStatus.SKIPPED
    else:
        status = TestStatus.PASSED

    # One final full-page shot per test, taken before finish_test()
    # clears current_test_id. A passing test is evidence too — this
    # is what the report shows to prove the flow actually worked.
    if state.screenshot_config.get("on_test_end"):
        try:
            capture.capture(
                state, page, kind="test_end",
                label=f"{status.value.capitalize()}: {test.name}",
                description=test.expected,
                test_id=test.id,
            )
        except Exception as e:
            state.add_warning(f"could not capture final shot for {test.id}: {e}")

    state.finish_test(test.id, status, error=first_error)

    if status == TestStatus.FAILED:
        judge.run(state, test=test)


def run_qa_for_url(url: str, **kwargs) -> QAState:
    return run_qa(QAState(url=url), **kwargs)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run one QA pass against a URL.")
    ap.add_argument("url", nargs="?", default="https://example.com")
    ap.add_argument("--max-tests", type=int, default=None,
                    help="cap on LLM-planned tests (checks always run)")
    ap.add_argument("--max-pages", type=int, default=1, help="how many pages to crawl")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--no-checks", action="store_true",
                    help="skip the deterministic link/a11y/UI/console/network audits")
    ap.add_argument("--load", action="store_true",
                    help="also load-test the server (only do this to a server you own)")
    ap.add_argument("--load-concurrency", type=int, default=load_test_mod.DEFAULT_CONCURRENCY)
    ap.add_argument("--load-requests", type=int, default=load_test_mod.DEFAULT_REQUESTS)
    ap.add_argument("--replay", action="store_true",
                    help="re-run the last saved plan for this URL instead of planning fresh")
    args = ap.parse_args()

    result = run_qa_for_url(
        args.url,
        headless=not args.headed,
        max_tests=args.max_tests,
        max_pages=args.max_pages,
        run_checks=not args.no_checks,
        load_test=args.load,
        load_concurrency=args.load_concurrency,
        load_requests=args.load_requests,
        reuse_baseline_plan=args.replay,
    )

    print(f"\nphase: {result.phase.value}")
    print(result.health_snapshot())
    if result.full_report:
        print("\n" + result.full_report)
    elif result.fatal_error:
        print("\nrun failed:", result.fatal_error)
