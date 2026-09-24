"""
API on top of the orchestrator. Kicks off a run in a background thread
(Playwright's sync API is blocking, so it can't just live inside an
async route handler) and lets the frontend poll it for progress.

Run with:
    uvicorn main:app --reload
from inside backend/, so the bare imports across this project resolve.
"""

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import orchestrator
import report_html
from state import QAState


app = FastAPI(title="QA Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# run_id -> QAState. Good enough for a single-machine project; the
# orchestrator thread mutates the same object the API reads from, so
# polling GET /api/runs/{id} naturally shows live progress.
RUNS: dict[str, QAState] = {}


class NewRunRequest(BaseModel):
    url: str
    headless: bool = True
    max_tests: int | None = None
    max_pages: int = 1


@app.post("/api/runs")
def start_run(body: NewRunRequest):
    try:
        state = QAState(url=body.url)
    except Exception as e:
        raise HTTPException(400, f"bad url: {e}")

    RUNS[state.run_id] = state

    thread = threading.Thread(
        target=orchestrator.run_qa,
        args=(state,),
        kwargs={
            "headless": body.headless,
            "max_tests": body.max_tests,
            "max_pages": max(1, body.max_pages),
        },
        daemon=True,
    )
    thread.start()

    return {"run_id": state.run_id}


@app.get("/api/runs")
def list_runs():
    runs = sorted(RUNS.values(), key=lambda s: s.started_at, reverse=True)
    return [
        {
            "run_id": s.run_id,
            "url": s.url,
            "phase": s.phase.value,
            "started_at": s.started_at,
            "bugs": len(s.bugs),
            "tests_passed": len(s.completed_test_ids),
            "tests_failed": len(s.failed_test_ids),
        }
        for s in runs
    ]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    state = _find(run_id)

    return {
        "run_id": state.run_id,
        "url": state.url,
        "phase": state.phase.value,
        "site_type": state.site_type.value,
        "tests": [
            {
                "id": t.id,
                "name": t.name,
                "category": t.category.value,
                "status": t.status.value,
                "error": t.error,
            }
            for t in state.test_plan
        ],
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
    html_path = Path(state.output_dir) / state.run_id / "report.html"
    if not html_path.exists():
        raise HTTPException(409, "report isn't ready yet")

    pdf_bytes = report_html.render_pdf(html_path)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="qa_report_{run_id}.pdf"'},
    )


def _find(run_id: str) -> QAState:
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(404, "no run with that id")
    return state


def _bug_json(state: QAState, bug) -> dict:
    shot = state.get_screenshot(bug.screenshot_id) if bug.screenshot_id else None
    return {
        "id": bug.id,
        "title": bug.title,
        "description": bug.description,
        "severity": bug.severity.value,
        "category": bug.category.value,
        "url": bug.url,
        "expected": bug.expected,
        "actual": bug.actual,
        "screenshot": f"/runs/{shot.relative_path}" if shot else None,
    }


# everything a run produces — screenshots, dashboard.html, report.html,
# state.json — lands under runs/<run_id>/, so one mount serves all of it
Path("runs").mkdir(exist_ok=True)
app.mount("/runs", StaticFiles(directory="runs"), name="runs")

# serve the frontend itself, if it's there
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
