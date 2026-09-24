"""
API on top of the orchestrator. Kicks off a run in a background thread
(Playwright's sync API is blocking, so it can't just live inside an
async route handler) and lets the frontend poll it for progress.

Run with:
    uvicorn --app-dir backend main:app --reload
from the repo root. --app-dir puts backend/ on sys.path so the bare
imports across this project resolve; run artifacts are resolved through
paths.py, so it no longer matters which directory you start from.
"""

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import load_test as load_test_mod
import orchestrator
import paths
import registry
import report_html
from state import QAState


app = FastAPI(title="QA Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Runs are tracked by registry.py: live ones in memory (the orchestrator
# thread mutates the same object this module reads, which is what makes
# polling show live progress) and finished ones on disk under runs/, so
# history outlives a server restart.


class NewRunRequest(BaseModel):
    url: str
    headless: bool = True
    max_tests: int | None = None
    max_pages: int = 1

    # deterministic audits: links, a11y, UI, console, network, page load
    run_checks: bool = True

    # Load testing is opt-in: a few hundred concurrent requests is
    # indistinguishable from a small DoS at the receiving end, so it only
    # happens when the caller asks for it against a server they control.
    load_test: bool = False
    load_concurrency: int = load_test_mod.DEFAULT_CONCURRENCY
    load_requests: int = load_test_mod.DEFAULT_REQUESTS

    # compare against the last run of the same URL
    regression: bool = True
    # re-run that baseline's plan verbatim instead of planning fresh
    reuse_baseline_plan: bool = False


@app.post("/api/runs")
def start_run(body: NewRunRequest):
    try:
        state = QAState(url=body.url)
    except Exception as e:
        raise HTTPException(400, f"bad url: {e}")

    registry.register(state)

    thread = threading.Thread(
        target=orchestrator.run_qa,
        args=(state,),
        kwargs={
            "headless": body.headless,
            "max_tests": body.max_tests,
            "max_pages": max(1, body.max_pages),
            "run_checks": body.run_checks,
            "load_test": body.load_test,
            "load_concurrency": body.load_concurrency,
            "load_requests": body.load_requests,
            "regression": body.regression,
            "reuse_baseline_plan": body.reuse_baseline_plan,
        },
        daemon=True,
    )
    thread.start()

    return {"run_id": state.run_id}


@app.get("/api/runs")
def list_runs():
    """Newest first: everything running now, plus every run left on disk."""
    return registry.list_all()


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    """Ask a run to stop early, keeping whatever it has already produced.

    Only a run this process is executing can be cancelled — one loaded
    back from disk has no thread left to tell.
    """
    state = registry.live(run_id)
    if not state:
        raise HTTPException(404, "no run with that id is currently executing")
    if state.phase.value in registry.TERMINAL:
        return {"run_id": run_id, "cancelled": False, "reason": "already finished"}
    state.request_cancel()
    return {"run_id": run_id, "cancelled": True}


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    """Remove a run and its artifacts — screenshots, report, state."""
    running = registry.live(run_id)
    if running and running.phase.value not in registry.TERMINAL:
        raise HTTPException(409, "that run is still executing — cancel it first")
    if not registry.delete(run_id):
        raise HTTPException(404, "no run with that id")
    return {"run_id": run_id, "deleted": True}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    state = _find(run_id)

    return {
        "run_id": state.run_id,
        "url": state.url,
        "phase": state.phase.value,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        # a live run that's been asked to stop but hasn't unwound yet —
        # the UI shows "stopping…" rather than pretending it's still going
        "cancelling": state.cancel_requested and state.phase.value not in registry.TERMINAL,
        "site_type": state.site_type.value,
        "tests": [_test_json(state, t) for t in state.test_plan],
        "current_test_id": state.current_test_id,
        "current_step_index": state.current_step_index,
        "counts": _counts(state),
        "duration_ms": state.duration_ms,
        "elapsed_ms": _elapsed_ms(state),
        "screenshots": len(state.screenshots),
        "coverage": state.coverage_matrix(),
        "techniques": state.technique_counts(),
        "load_test": state.load_test.model_dump() if state.load_test else None,
        "regression": (
            state.regression.model_dump(mode="json") if state.regression else None
        ),
        "broken_links": len(state.broken_links),
        "a11y_issues": len(state.a11y_issues),
        "ui_issues": len(state.ui_issues),
        "console_errors": len(state.console_errors) + len(state.page_errors),
        "network_failures": len(state.network_failures) + len(state.request_failures),
        "bugs": [_bug_json(state, b) for b in state.bugs],
        "pass_rate": state.pass_rate,
        "llm_calls": state.total_llm_calls,
        "cost_usd": round(state.estimated_cost_usd, 5),
        "warnings": state.warnings[-10:],
        "fatal_error": state.fatal_error,
        "report": state.full_report or None,
        "dashboard_url": state.site_metadata.get("dashboard_url"),
        "report_html_url": state.site_metadata.get("report_html_url"),
        "pages_discovered": state.site_metadata.get("pages_discovered"),
    }


@app.get("/api/runs/{run_id}/report")
def get_report(run_id: str):
    state = _find(run_id)
    if not state.full_report:
        raise HTTPException(409, "report isn't ready yet")
    return {"report": state.full_report}


@app.get("/api/runs/{run_id}/report.pdf")
def download_report_pdf(run_id: str):
    state = _find(run_id)
    html_path = paths.run_dir(state.output_dir, state.run_id) / "report.html"
    if not html_path.exists():
        raise HTTPException(409, "report isn't ready yet")

    pdf_bytes = report_html.render_pdf(html_path)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="qa_report_{run_id}.pdf"'},
    )


def _find(run_id: str) -> QAState:
    state = registry.get(run_id)
    if not state:
        raise HTTPException(404, "no run with that id")
    return state


def _test_json(state: QAState, t) -> dict:
    """One test, with enough detail to drive the live execution view."""
    return {
        "id": t.id,
        "name": t.name,
        "category": t.category.value,
        "area": t.area.value,
        "technique": t.technique.value,
        "source": t.source,
        "description": t.description,
        "expected": t.expected,
        "priority": t.severity_if_fail.value,
        "status": t.status.value,
        "error": t.error,
        "duration_ms": round(t.duration_ms, 1),
        "steps": [
            {
                "index": s.index,
                "description": s.description,
                "action": s.action.value,
                "selector": s.selector,
                "value": s.value,
                "status": s.status.value,
                "error": s.error,
                "duration_ms": round(s.duration_ms, 1),
                "screenshot": _shot_url(state, s.screenshot_after or s.screenshot_before),
            }
            for s in t.steps
        ],
        "screenshots": [
            u for u in (_shot_url(state, sid) for sid in t.screenshot_ids) if u
        ],
    }


def _counts(state: QAState) -> dict:
    """Total / passed / failed / blocked, for the results summary cards.

    "Blocked" is anything that never produced a verdict — skipped, or still
    pending because a cap or an earlier failure stopped the run reaching it.
    """
    total = len(state.test_plan)
    passed = len(state.completed_test_ids)
    failed = len(state.failed_test_ids)
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "blocked": max(0, total - passed - failed),
    }


def _elapsed_ms(state: QAState) -> float:
    """Wall time so far — duration_ms is only filled in once the run ends."""
    if state.duration_ms:
        return state.duration_ms
    from datetime import datetime
    return (datetime.now() - state.started_at).total_seconds() * 1000


def _shot_url(state: QAState, shot_id) -> str | None:
    if not shot_id:
        return None
    shot = state.get_screenshot(shot_id)
    return f"/runs/{shot.relative_path}" if shot else None


def _bug_json(state: QAState, bug) -> dict:
    ids = bug.screenshot_ids or ([bug.screenshot_id] if bug.screenshot_id else [])
    shots = [u for u in (_shot_url(state, sid) for sid in ids) if u]
    test = state.get_test(bug.test_id) if bug.test_id else None
    return {
        "id": bug.id,
        "test_id": bug.test_id,
        "test_name": test.name if test else "",
        "title": bug.title,
        "description": bug.description,
        "severity": bug.severity.value,
        "category": bug.category.value,
        "url": bug.url,
        "expected": bug.expected,
        "actual": bug.actual,
        "steps_to_reproduce": bug.steps_to_reproduce,
        "screenshot": shots[0] if shots else None,
        "screenshots": shots,
    }


# everything a run produces — screenshots, dashboard.html, report.html,
# state.json — lands under runs/<run_id>/, so one mount serves all of it
RUNS_DIR = paths.runs_root()
RUNS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")

# serve the frontend itself, if it's there
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
