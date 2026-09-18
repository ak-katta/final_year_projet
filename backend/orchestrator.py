"""
Runs one full QA pass on a URL: load the page, figure out what kind of
site it is, plan some tests for it, run them, and write up what broke.

Everything gets written onto the QAState object as it goes, so a caller
(the API, a script, whatever) can just keep reading `state` to see how
far along a run is.
"""

from datetime import datetime

import capture
from state import QAState, Phase, TestStatus
from llm_router import LLMRouter
from agents.classifier import ClassifierAgent
from agents.domain_expert import DomainExpertAgent
from agents.planner import PlannerAgent
from agents.executor import ExecutorAgent
from agents.judge import JudgeAgent
from agents.healer import HealerAgent
from agents.reporter import ReporterAgent


def run_qa(state: QAState, headless: bool = True, max_tests: int | None = None) -> QAState:
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

        snap = capture.snapshot(page)
        state.initial_snapshot = snap
        state.add_snapshot(snap)
        capture.capture(state, page, kind="phase_change", label="initial load")

        classifier.run(state)
        domain_expert.run(state)
        state.mark_phase(Phase.ANALYZED)

        planner.run(state)
        state.mark_phase(Phase.PLANNED)

        tests = state.test_plan[:max_tests] if max_tests else state.test_plan
        state.mark_phase(Phase.EXECUTING)
        state.execution_started_at = datetime.now()

        for test in tests:
            state.start_test(test.id)

            for step in test.steps:
                state.current_step_index = step.index
                executor.run(state, step=step)
                if step.status == TestStatus.FAILED:
                    break  # no point chasing later steps once one's broken

            first_error = next((s.error for s in test.steps if s.error), None)
            status = TestStatus.FAILED if first_error else TestStatus.PASSED
            state.finish_test(test.id, status, error=first_error)

            if status == TestStatus.FAILED:
                judge.run(state, test=test)

        state.execution_finished_at = datetime.now()
        state.mark_phase(Phase.REPORTING)

        reporter.run(state)
        state.finish()

    except Exception as e:
        state.mark_failed(str(e))

    finally:
        capture.close(state)
        try:
            state.save(f"{state.output_dir}/{state.run_id}/state.json")
        except Exception as e:
            state.add_warning(f"could not save state to disk: {e}")

    return state


def run_qa_for_url(url: str, **kwargs) -> QAState:
    return run_qa(QAState(url=url), **kwargs)


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    result = run_qa_for_url(target, headless=True, max_tests=5)

    print(f"\nphase: {result.phase.value}")
    print(result.health_snapshot())
    if result.full_report:
        print("\n" + result.full_report)
    elif result.fatal_error:
        print("\nrun failed:", result.fatal_error)
