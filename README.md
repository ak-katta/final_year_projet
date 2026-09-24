# Autonomous AI QA Engineer

Point it at a URL. It explores the site in a real browser, writes its own
test cases, runs them, audits what a model can't reliably judge, and writes
up what broke — with screenshots attached to every finding.

The whole run is **black box**: nothing here reads the application's source.
It only drives a real Chromium through Playwright and reads real HTTP
responses, which is what makes it work against a site you didn't write.

---

## What a run does

| Phase | What happens |
|---|---|
| **crawl** | Visits the URL and follows links to discover pages on its own |
| **plan** | An LLM writes functional / negative / validation test cases for what it found |
| **execute** | Each test is driven step by step through the browser, screenshotting as it goes |
| **checks** | Deterministic audits — broken links, accessibility, UI rendering, console errors, failed requests, page load |
| **load** | *Optional.* Many concurrent requests at the server |
| **regression** | *Optional.* Diffs this run's outcomes against the last run of the same URL |
| **report** | Markdown + HTML dashboard + PDF, with the evidence embedded |

The deterministic checks need no model at all, so if the LLM providers are
down or rate-limited the run degrades to those rather than failing outright.

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

API keys go in `backend/.env` — copy `backend/.env.example` and fill it in.
Groq is tried first (faster and cheaper), Gemini is the failover; numbered
suffixes (`GROQ_API_KEY_1`, `_2`, …) each become another provider in the
rotation.

### Run the web app

```bash
.venv/bin/uvicorn --app-dir backend main:app --reload --port 8000
```

Then open <http://localhost:8000>. `--app-dir` puts `backend/` on `sys.path`
so the project's bare imports resolve; run artifacts are resolved through
`backend/paths.py`, so it doesn't matter which directory you start from.

### Run one pass from the terminal

```bash
.venv/bin/python backend/orchestrator.py https://example.com --max-pages 3
```

`--headed` shows the browser, `--max-tests N` caps the planned tests (the
checks always run), `--replay` re-runs the last saved plan for that URL, and
`--load` adds the load test.

## The interface

Six stages across the top, and the view follows the run through them until
you click a tab yourself.

1. **Test Setup** — the target and what to run, plus every past run
2. **Test Cases** — what the agent decided to test, searchable and filterable
3. **Execution** — the live pipeline and the test currently running, step by step
4. **Results** — pass/fail/not-run, filterable
5. **Bugs** — each with reproduction steps, expected vs actual, and screenshots
6. **Dashboard** — pass rate, cost, coverage charts, and links to the artifacts

A run can be stopped from the bar at the top; it finishes the step it's on,
then writes the report for everything it managed to do rather than throwing
the work away.

## A note on the load test

It's opt-in on purpose. A few hundred concurrent requests is indistinguishable
from a small denial-of-service attack at the receiving end, so only point it
at a server you own or are authorised to test.

## Layout

```
backend/
  main.py          FastAPI app — starts runs, serves progress and artifacts
  orchestrator.py  one full QA pass, phase by phase
  registry.py      the catalogue of runs: live in memory, finished on disk
  paths.py         where a run's artifacts live
  state.py         QAState — the single source of truth, serializable
  capture.py       Playwright: launch, crawl, screenshot
  checks.py        the deterministic audits
  load_test.py     concurrent HTTP load
  regression.py    replay a saved plan and diff the outcomes
  report_html.py   dashboard.html + report.html + PDF
  llm_router.py    Groq → Gemini failover, cost and token accounting
  agents/          classifier, domain expert, planner, executor, judge,
                   healer, reporter
frontend/          the web UI — plain HTML/CSS/JS, no build step
runs/<run_id>/     screenshots, dashboard.html, report.html, state.json
```

Every run leaves a folder under `runs/`, and the history list reads from
there — so past runs survive a server restart. Deleting a run from the UI
removes that folder.
