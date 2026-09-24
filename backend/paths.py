"""
Where a run's artifacts live.

Every path in this project used to be resolved relative to the current
working directory, which meant `uvicorn --app-dir backend` (from the repo
root) and `uvicorn main:app` (from inside backend/) wrote their runs to
two different `runs/` directories — and each one was blind to the other's
history. Anchor everything to the repo root instead, so it stops mattering
where the process was started from.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def runs_root(output_dir: str = "runs") -> Path:
    """Absolute path to the directory holding every run's artifacts.

    An absolute `output_dir` is honoured as-is (tests point it at a temp
    directory); a relative one is resolved against the repo root rather
    than the process's working directory.
    """
    p = Path(output_dir)
    return p if p.is_absolute() else PROJECT_ROOT / p


def run_dir(output_dir: str, run_id: str) -> Path:
    """Absolute path to one run's own folder."""
    return runs_root(output_dir) / run_id
