"""
Regression testing — re-run an earlier run's test plan and diff the result.

Two halves, and they're useful separately:

  reuse_plan()  takes the test cases a previous run against the same URL
                already produced and runs *those* again, instead of asking
                the LLM to invent a fresh plan. Same tests, later build.

  compare()     diffs this run's outcomes against the baseline's and says
                which tests went green→red (regressions), red→green (fixes)
                and which never moved.

Tests are matched by name rather than id, because ids are generated per
run; the name is what stays stable across builds.
"""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import paths
from state import QAState, TestCase, TestStatus, TestArea, RegressionResult


# Areas only the deterministic checks and the load test ever produce. A
# baseline saved before tests carried a `source` tag has everything marked
# "planner", so match on the area too or a replay duplicates every check.
GENERATED_AREAS = {
    TestArea.PAGE_LOAD, TestArea.CONSOLE, TestArea.NETWORK,
    TestArea.LINKS, TestArea.ACCESSIBILITY, TestArea.LOAD,
}


def _run_dirs(output_dir: str) -> list[Path]:
    root = paths.runs_root(output_dir)
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.is_dir() and (p / "state.json").exists()]


def find_baseline(state: QAState) -> QAState | None:
    """
    The most recent completed run against the same URL, excluding this one.

    Reads state.json files directly rather than going through the API's
    in-memory RUNS dict, so a baseline survives a server restart.
    """
    candidates: list[tuple[datetime, Path]] = []

    for d in _run_dirs(state.output_dir):
        if d.name == state.run_id:
            continue
        path = d / "state.json"
        try:
            # peek at the two fields we filter on before paying to parse
            # and validate the whole (often large) state document
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if raw.get("url") != state.url:
            continue
        if not raw.get("test_plan"):
            continue
        started = raw.get("started_at")
        try:
            when = datetime.fromisoformat(started) if started else datetime.min
        except (TypeError, ValueError):
            when = datetime.min
        candidates.append((when, path))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    for _, path in candidates:
        try:
            return QAState.load(path)
        except Exception:
            continue   # corrupt or unreadable — fall through to the next one
    return None


def reuse_plan(baseline: QAState) -> list[TestCase]:
    """
    Fresh copies of the baseline's test cases, reset to pending.

    Deep-copied through the model so re-running can't mutate the baseline's
    recorded results, and every per-run artifact (ids, timings, screenshot
    links) is cleared — only the definition of the test carries over.
    """
    plan: list[TestCase] = []
    for old in baseline.test_plan:
        # deterministic checks and the load test rebuild themselves on
        # every run — replaying them would just duplicate their results
        if old.source != "planner" or old.area in GENERATED_AREAS:
            continue
        fresh = old.model_copy(deep=True)
        fresh.id = f"T-{uuid4().hex[:6]}"
        fresh.status = TestStatus.PENDING
        fresh.error = None
        fresh.started_at = None
        fresh.finished_at = None
        fresh.duration_ms = 0.0
        fresh.retries = 0
        fresh.screenshot_ids = []
        fresh.trace_path = None
        fresh.video_path = None

        for step in fresh.steps:
            step.status = TestStatus.PENDING
            step.error = None
            step.started_at = None
            step.finished_at = None
            step.duration_ms = 0.0
            step.screenshot_before = None
            step.screenshot_after = None

        if fresh.steps:
            plan.append(fresh)
    return plan


def compare(state: QAState, baseline: QAState) -> RegressionResult:
    """Diff this run's per-test outcomes against the baseline's."""
    result = RegressionResult(
        baseline_run_id=baseline.run_id,
        baseline_started_at=baseline.started_at,
    )

    def outcome(t: TestCase) -> str:
        if t.status == TestStatus.PASSED:
            return "passed"
        if t.status in (TestStatus.FAILED, TestStatus.ERROR):
            return "failed"
        return "not_run"

    before = {t.name: t for t in baseline.test_plan}
    after = {t.name: t for t in state.test_plan}

    for name, now in after.items():
        was = before.get(name)
        if was is None:
            result.new_tests.append(name)
            continue

        old_outcome, new_outcome = outcome(was), outcome(now)
        if old_outcome == "not_run" or new_outcome == "not_run":
            continue   # nothing to compare — one side never executed

        result.compared += 1
        entry = {
            "test": name,
            "area": now.area.value,
            "was": old_outcome,
            "now": new_outcome,
            "error": (now.error or "")[:300],
        }
        if old_outcome == "passed" and new_outcome == "failed":
            result.regressions.append(entry)
        elif old_outcome == "failed" and new_outcome == "passed":
            result.fixes.append(entry)
        elif old_outcome == "failed" and new_outcome == "failed":
            result.still_failing.append(entry)
        else:
            result.stable += 1

    result.missing_tests = [n for n in before if n not in after]
    return result


def run(state: QAState) -> RegressionResult | None:
    """Finds a baseline for this URL and records the comparison on state."""
    baseline = find_baseline(state)
    if baseline is None:
        return None
    result = compare(state, baseline)
    state.regression = result
    return result
