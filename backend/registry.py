"""
The catalogue of runs the API serves.

A run lives in two places over its life. While it's executing it's a live
QAState object being mutated by the orchestrator thread, and the API reads
straight off that object so polling shows real progress. Once it finishes,
what survives is `runs/<run_id>/` on disk.

The API used to only know about the first kind: restart the server and
every past run vanished from the UI even though its screenshots, report
and state.json were all still sitting on disk. This module joins the two,
so history is whatever is actually on disk plus whatever is running now.

Listing all of them by parsing each state.json is too slow to do on every
page load — those files run to several megabytes. So each finished run
also gets a small `summary.json`; runs written before that existed get one
derived from their state.json the first time they're listed.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

import paths
from state import QAState

TERMINAL = {"done", "failed", "aborted"}

SUMMARY_FILE = "summary.json"
STATE_FILE = "state.json"

# run_id -> QAState for runs this process started. The orchestrator thread
# mutates these in place, which is exactly what makes live polling work.
_live: dict[str, QAState] = {}
_lock = threading.Lock()


# ── Live runs ───────────────────────────────────────────────────────

def register(state: QAState) -> None:
    with _lock:
        _live[state.run_id] = state


def live(run_id: str) -> Optional[QAState]:
    with _lock:
        return _live.get(run_id)


def live_ids() -> set[str]:
    with _lock:
        return set(_live)


def get(run_id: str, output_dir: str = "runs") -> Optional[QAState]:
    """The live state if this process is running it, else load it from disk."""
    state = live(run_id)
    if state is not None:
        return state

    path = paths.run_dir(output_dir, run_id) / STATE_FILE
    if not path.exists():
        return None
    try:
        return QAState.load(path)
    except Exception:
        return None


# ── Summaries ───────────────────────────────────────────────────────

def summarize(state: QAState) -> dict[str, Any]:
    """The handful of fields the history list needs, from a full state."""
    total = len(state.test_plan)
    passed = len(state.completed_test_ids)
    failed = len(state.failed_test_ids)
    return {
        "run_id": state.run_id,
        "url": state.url,
        "phase": state.phase.value,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "duration_ms": round(state.duration_ms or 0.0, 1),
        "total": total,
        "passed": passed,
        "failed": failed,
        "blocked": max(0, total - passed - failed),
        "bugs": len(state.bugs),
        "pass_rate": round(state.pass_rate or 0.0, 4),
        "cost_usd": round(state.estimated_cost_usd or 0.0, 5),
        "pages_discovered": state.site_metadata.get("pages_discovered"),
        "has_report": bool(state.full_report),
    }


def write_summary(state: QAState) -> None:
    """Drop a summary.json next to the run's state.json. Best effort."""
    try:
        d = paths.run_dir(state.output_dir, state.run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / SUMMARY_FILE).write_text(
            json.dumps(summarize(state), indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _summary_from_disk(run_path: Path) -> Optional[dict[str, Any]]:
    """Read a run folder's summary, deriving and caching one if it has none."""
    cached = run_path / SUMMARY_FILE
    if cached.exists():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except Exception:
            pass   # corrupt cache — fall through and rebuild it

    state_path = run_path / STATE_FILE
    if not state_path.exists():
        return None
    try:
        state = QAState.load(state_path)
    except Exception:
        return None

    summary = summarize(state)
    try:
        cached.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    except Exception:
        pass
    return summary


def list_all(output_dir: str = "runs") -> list[dict[str, Any]]:
    """Every run, newest first — live ones and everything left on disk.

    A run that's executing right now is summarized from its live state
    rather than from the stale summary.json of a previous write, so an
    in-flight run shows its real phase and counts.
    """
    with _lock:
        in_memory = dict(_live)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for rid, state in in_memory.items():
        # "live" means still executing, not merely still in memory — a run
        # this process finished half an hour ago is history like any other
        out.append({**summarize(state), "live": state.phase.value not in TERMINAL})
        seen.add(rid)

    root = paths.runs_root(output_dir)
    if root.exists():
        for d in root.iterdir():
            if not d.is_dir() or d.name in seen:
                continue
            summary = _summary_from_disk(d)
            if summary:
                out.append({**summary, "live": False})

    out.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    return out


# ── Deletion ────────────────────────────────────────────────────────

def delete(run_id: str, output_dir: str = "runs") -> bool:
    """Forget a run and remove its artifacts. False if there was nothing there."""
    with _lock:
        _live.pop(run_id, None)

    d = paths.run_dir(output_dir, run_id)
    # never let a crafted run_id walk out of the runs directory
    root = paths.runs_root(output_dir).resolve()
    try:
        resolved = d.resolve()
    except OSError:
        return False
    if resolved.parent != root or not resolved.is_dir():
        return False

    shutil.rmtree(resolved)
    return True
