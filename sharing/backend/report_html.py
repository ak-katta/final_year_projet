"""
Turns a finished QAState into two static HTML files: a compact dashboard
(pass rate, bug summary, discovered pages) and a longer detailed report
with every test step and its before/after screenshots. Sits alongside
the markdown report the ReporterAgent already writes to state.full_report
— this is just a second, more visual artifact for the same run.

Ported over from an earlier standalone prototype and adapted to this
project's QAState shape.
"""

import html
from datetime import datetime
from pathlib import Path

from state import QAState


def render_pdf(html_path: Path) -> bytes:
    """Renders an already-generated report.html to PDF bytes using a
    throwaway headless Chromium page — no separate PDF library needed
    since Playwright's already a dependency here."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            return page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "14mm", "bottom": "14mm", "left": "10mm", "right": "10mm"},
            )
        finally:
            browser.close()


def generate(state: QAState) -> dict[str, Path]:
    out_dir = Path(state.output_dir) / state.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    data = _collect(state)

    dashboard_path = out_dir / "dashboard.html"
    report_path = out_dir / "report.html"

    dashboard_path.write_text(_dashboard_page(data), encoding="utf-8")
    report_path.write_text(_detailed_page(data), encoding="utf-8")

    return {"dashboard": dashboard_path, "report": report_path}


# ── Pull everything out of QAState into plain dicts ──────────────────

def _collect(state: QAState) -> dict:
    tests = [
        {
            "id": t.id,
            "name": t.name,
            "category": t.category.value,
            "status": t.status.value,
            "description": t.description,
            "expected": t.expected,
            "duration_ms": round(t.duration_ms, 1),
            "error": t.error or "",
            "steps": [
                {
                    "index": s.index + 1,
                    "description": s.description,
                    "action": s.action.value,
                    "selector": s.selector,
                    "status": s.status.value,
                    "error": s.error or "",
                    "duration_ms": round(s.duration_ms, 1),
                    "before": s.screenshot_before,
                    "after": s.screenshot_after,
                }
                for s in t.steps
            ],
        }
        for t in state.test_plan
    ]

    bugs = [
        {
            "id": b.id,
            "title": b.title,
            "description": b.description,
            "severity": b.severity.value,
            "category": b.category.value,
            "expected": b.expected,
            "actual": b.actual,
            "steps": b.steps_to_reproduce,
            "screenshots": b.screenshot_ids or ([b.screenshot_id] if b.screenshot_id else []),
        }
        for b in state.bugs
    ]

    snaps = state.page_snapshots or ([state.initial_snapshot] if state.initial_snapshot else [])
    pages = [
        {
            "url": s.url,
            "title": s.title,
            "load_ms": round(s.load_time_ms, 1),
            "interactive": len(s.interactive_elements),
            "forms": len(s.forms),
            "links": len(s.links),
            "images": len(s.images),
        }
        for s in snaps
    ]

    counts = {
        status: sum(1 for t in tests if t["status"] == status)
        for status in ("passed", "failed", "error", "skipped", "pending", "running")
    }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": state.run_id,
        "url": state.url,
        "title": state.initial_snapshot.title if state.initial_snapshot else "",
        "phase": state.phase.value,
        "pass_rate": round(state.pass_rate * 100, 1),
        "counts": counts,
        "tests": tests,
        "bugs": bugs,
        "pages": pages,
        "screenshots": [
            {"id": s.id, "relative_path": s.relative_path, "label": s.label}
            for s in state.screenshots
        ],
    }


# ── Rendering ──────────────────────────────────────────────────────

def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _img(shot_id, screenshots) -> str:
    shot = next((s for s in screenshots if s["id"] == shot_id), None)
    if not shot:
        return ""
    # dashboard.html / report.html live in the same folder as screenshots/,
    # so this can just be a relative link, no path math needed
    rel = f"screenshots/{Path(shot['relative_path']).name}"
    return f'<a href="{_esc(rel)}" target="_blank"><img class="shot" src="{_esc(rel)}" alt="{_esc(shot["label"])}"></a>'


def _dashboard_page(d: dict) -> str:
    e = _esc
    cards = "".join(
        f'<div class="card"><span>{k.upper()}</span><b>{d["counts"][k]}</b></div>'
        for k in ("passed", "failed", "error", "skipped")
    )

    if d["bugs"]:
        bug_rows = "".join(
            f'<tr><td><span class="pill {e(b["severity"])}">{e(b["severity"])}</span></td>'
            f'<td>{e(b["title"])}</td><td>{e(b["category"])}</td></tr>'
            for b in d["bugs"]
        )
    else:
        bug_rows = '<tr><td colspan="3" class="empty">No confirmed bugs.</td></tr>'

    pages_rows = "".join(
        f'<tr><td>{e(p["title"] or p["url"])}</td>'
        f'<td><a target="_blank" href="{e(p["url"])}">{e(p["url"])}</a></td>'
        f'<td>{p["load_ms"]} ms</td></tr>'
        for p in d["pages"]
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>QA dashboard &middot; {e(d['run_id'])}</title>
<style>{_css()}</style></head>
<body>
<header>
  <div>
    <div class="eyebrow">QA RUN DASHBOARD</div>
    <h1>{e(d['title'] or d['url'])}</h1>
    <p>{e(d['url'])}</p>
  </div>
  <div class="run">Run <b>{e(d['run_id'])}</b><br>{e(d['generated_at'])}<br>
    <a href="/api/runs/{e(d['run_id'])}/report.pdf">download PDF</a>
  </div>
</header>
<main>
  <section class="hero">
    <div class="score">{d['pass_rate']}%<small>pass rate</small></div>
    <div class="cards">{cards}
      <div class="card"><span>BUGS</span><b>{len(d['bugs'])}</b></div>
      <div class="card"><span>PAGES</span><b>{len(d['pages'])}</b></div>
      <div class="card"><span>SHOTS</span><b>{len(d['screenshots'])}</b></div>
    </div>
  </section>

  <section class="panel">
    <h2>Bugs</h2>
    <table><tr><th>Severity</th><th>Title</th><th>Category</th></tr>{bug_rows}</table>
  </section>

  <section class="panel">
    <h2>Pages discovered</h2>
    <table><tr><th>Title</th><th>URL</th><th>Load time</th></tr>{pages_rows}</table>
  </section>

  <p><a href="report.html">Open the detailed step-by-step report &rarr;</a></p>
</main>
</body></html>"""


def _detailed_page(d: dict) -> str:
    e = _esc
    screenshots = d["screenshots"]

    test_items = []
    for t in d["tests"]:
        steps = "".join(
            f'<li><b>{e(s["action"])}</b> {e(s["description"])} '
            f'<small>{e(s["duration_ms"])} ms</small>'
            f'<div class="mono">{e(s["selector"])}</div>'
            f'<div class="gallery">{_img(s["before"], screenshots)}{_img(s["after"], screenshots)}</div>'
            f'{"<pre>" + e(s["error"]) + "</pre>" if s["error"] else ""}</li>'
            for s in t["steps"]
        )
        test_items.append(
            f'<details class="item {e(t["status"])}" open>'
            f'<summary><span class="pill {e(t["status"])}">{e(t["status"])}</span> '
            f'<b>{e(t["id"])}</b> &middot; {e(t["name"])} '
            f'<small>{e(t["duration_ms"])} ms</small></summary>'
            f'<p>{e(t["description"])}</p>'
            f'<p><b>Expected:</b> {e(t["expected"])}</p>'
            f'<ol>{steps}</ol>'
            f'{"<pre>" + e(t["error"]) + "</pre>" if t["error"] else ""}'
            f'</details>'
        )
    test_html = "".join(test_items)

    if d["bugs"]:
        bug_items = []
        for b in d["bugs"]:
            repro = "".join(f"<li>{e(x)}</li>" for x in b["steps"]) or "<li>(no repro steps recorded)</li>"
            gallery = "".join(_img(sid, screenshots) for sid in b["screenshots"])
            bug_items.append(
                f'<article class="bug"><h3>{e(b["id"])} &middot; {e(b["title"])}</h3>'
                f'<p><span class="pill {e(b["severity"])}">{e(b["severity"])}</span> '
                f'<span class="pill">{e(b["category"])}</span></p>'
                f'<p>{e(b["description"])}</p>'
                f'<p><b>Expected:</b> {e(b["expected"])}<br><b>Actual:</b> {e(b["actual"])}</p>'
                f'<h4>Reproduction</h4><ol>{repro}</ol>'
                f'<div class="gallery">{gallery}</div></article>'
            )
        bugs_html = "".join(bug_items)
    else:
        bugs_html = '<div class="empty">No confirmed bugs.</div>'

    gallery_html = "".join(_img(s["id"], screenshots) for s in screenshots)

    search = (
        '<div class="toolbar">'
        '<input id="search" placeholder="Search tests..." oninput="filterAll()">'
        '<select id="status" onchange="filterAll()">'
        '<option value="all">All statuses</option>'
        '<option>passed</option><option>failed</option><option>error</option><option>skipped</option>'
        "</select></div>"
    )
    script = (
        "<script>function filterAll(){"
        "const q=document.getElementById('search').value.toLowerCase(),"
        "s=document.getElementById('status').value;"
        "document.querySelectorAll('.item').forEach(x=>{"
        "const ok=(s==='all'||x.classList.contains(s))&&x.innerText.toLowerCase().includes(q);"
        "x.style.display=ok?'block':'none';});"
        "}</script>"
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>QA report &middot; {e(d['run_id'])}</title>
<style>{_css()}</style></head>
<body>
<header>
  <div>
    <div class="eyebrow">DETAILED QA REPORT</div>
    <h1>{e(d['title'] or d['url'])}</h1>
    <p>{e(d['url'])}</p>
  </div>
  <div class="run">Run <b>{e(d['run_id'])}</b><br>{e(d['generated_at'])}<br>
    <a href="dashboard.html">&larr; dashboard</a> &middot;
    <a href="/api/runs/{e(d['run_id'])}/report.pdf">download PDF</a>
  </div>
</header>
<main>
  <section class="panel">
    <h2>Tests</h2>
    {search}
    {test_html}
  </section>
  <section class="panel">
    <h2>Bugs</h2>
    {bugs_html}
  </section>
  <section class="panel">
    <h2>Evidence gallery ({len(screenshots)})</h2>
    <div class="gallery large">{gallery_html}</div>
  </section>
</main>
{script}
</body></html>"""


def _css() -> str:
    return """
body{font-family:Arial,sans-serif;background:#0b1020;color:#e5e7eb;margin:0;line-height:1.5}
header{padding:28px 5%;background:#111827;border-bottom:1px solid #273449;display:flex;justify-content:space-between}
.eyebrow{font-size:11px;letter-spacing:2px;color:#60a5fa}
h1{margin:4px 0;font-size:26px}
.run,small,.mono{color:#94a3b8;font-family:monospace}
.run a{color:#93c5fd}
main{max-width:1150px;margin:24px auto;padding:0 20px}
.hero,.panel{background:#111827;border:1px solid #273449;border-radius:14px;padding:20px;margin-bottom:18px}
.hero{display:flex;gap:24px;align-items:center}
.score{font-size:42px;font-weight:800}
.score small{display:block;font-size:11px}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;flex:1}
.card{background:#0b1020;border:1px solid #273449;border-radius:9px;padding:12px}
.card span{font-size:9px;color:#94a3b8}
.card b{display:block;font-size:22px}
.toolbar{display:flex;gap:10px;margin:12px 0}
.toolbar input,.toolbar select{background:#0b1020;color:#e5e7eb;border:1px solid #374151;border-radius:8px;padding:9px}
.toolbar input{width:260px}
.item,.bug{background:#0b1020;border:1px solid #273449;border-radius:10px;padding:13px;margin:10px 0}
.item summary{cursor:pointer}
.pill{display:inline-block;padding:3px 8px;border-radius:999px;background:#334155;font-size:11px}
.pill.passed{background:#14532d}
.pill.failed{background:#7f1d1d}
.pill.error{background:#78350f}
.pill.skipped{background:#334155}
.pill.critical{background:#7f1d1d}
.pill.high{background:#9a3412}
.pill.medium{background:#854d0e}
.pill.low{background:#1e3a8a}
table{width:100%;border-collapse:collapse}
th,td{padding:8px;border-bottom:1px solid #273449;text-align:left}
.gallery{display:flex;gap:8px;flex-wrap:wrap}
.shot{width:170px;height:105px;object-fit:cover;border-radius:7px;border:1px solid #334155}
.large .shot{width:230px;height:150px}
pre{background:#050814;padding:12px;border-radius:8px;white-space:pre-wrap;max-height:300px;overflow:auto}
.empty{text-align:center;padding:20px;color:#94a3b8}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.hero{display:block}}
"""
