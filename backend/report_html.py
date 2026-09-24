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

import paths
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
    out_dir = paths.run_dir(state.output_dir, state.run_id)
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
            "area": t.area.value,
            "technique": t.technique.value,
            "status": t.status.value,
            "description": t.description,
            "expected": t.expected,
            "duration_ms": round(t.duration_ms, 1),
            "error": t.error or "",
            # evidence attached to the test itself (the end-of-test shot),
            # as opposed to the per-step before/after pair below
            "screenshots": list(
                dict.fromkeys(
                    list(t.screenshot_ids)
                    + state.screenshots_by_test.get(t.id, [])
                )
            ),
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
        "coverage": state.coverage_matrix(),
        "techniques": state.technique_counts(),
        "load_test": state.load_test.model_dump() if state.load_test else None,
        "regression": state.regression.model_dump(mode="json") if state.regression else None,
        "broken_links": state.broken_links,
        "links_checked": state.site_metadata.get("links_checked", 0),
        "a11y": [
            {"rule": i.rule, "impact": i.impact, "selector": i.selector,
             "description": i.description}
            for i in state.a11y_issues
        ],
        "ui_issues": state.ui_issues,
        "console_errors": list(state.console_errors) + list(state.page_errors),
        "network_failures": list(dict.fromkeys(
            [f"{e.status or 'failed'} {e.url}" for e in state.network_failures]
            + [f"failed {u}" for u in state.request_failures]
        )),
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


def _coverage_table(d: dict) -> str:
    e = _esc
    cov = d["coverage"]
    if not cov:
        return '<div class="empty">No tests were planned.</div>'
    rows = "".join(
        f'<tr><td><b>{e(area)}</b></td><td>{r["planned"]}</td>'
        f'<td class="ok">{r["passed"]}</td><td class="bad">{r["failed"]}</td>'
        f'<td>{r["not_run"] + r["skipped"]}</td>'
        f'<td>{"".join(f"<span class=chip>{e(t)}</span>" for t in r["techniques"])}</td></tr>'
        for area, r in sorted(cov.items())
    )
    return (
        '<table><tr><th>Area</th><th>Tests</th><th>Passed</th><th>Failed</th>'
        f'<th>Not run</th><th>Techniques</th></tr>{rows}</table>'
    )


def _methods_block(d: dict) -> str:
    e = _esc
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
    chips = "".join(
        f'<div class="method"><b>{e(labels.get(k, k))}</b><span>{v} test(s)</span></div>'
        for k, v in sorted(d["techniques"].items(), key=lambda kv: -kv[1])
    )
    return (
        '<p class="note">Black-box throughout: the agent drives the site through a real '
        'browser and never reads the application\'s source.</p>'
        f'<div class="methods">{chips}</div>'
    )


def _load_block(d: dict) -> str:
    e = _esc
    lt = d["load_test"]
    if not lt:
        return ('<div class="empty">Load test not run. Enable it with '
                '<code>--load</code> (CLI) or <code>"load_test": true</code> (API).</div>')
    cls = "passed" if lt["passed"] else "failed"
    rows = [
        ("Target", lt["url"]),
        ("Concurrency", f'{lt["concurrency"]} simultaneous clients'),
        ("Requests sent", f'{lt["completed"]} of {lt["total_requests"]}'),
        ("Wall time", f'{lt["duration_s"]:.2f}s'),
        ("Throughput", f'{lt["requests_per_second"]:.1f} req/s'),
        ("Succeeded / failed", f'{lt["succeeded"]} / {lt["failed"]}'),
        ("Error rate", f'{lt["error_rate"]:.2%} (threshold {lt["max_error_rate"]:.0%})'),
        ("Latency min / mean / max",
         f'{lt["latency_min_ms"]:.0f} / {lt["latency_mean_ms"]:.0f} / {lt["latency_max_ms"]:.0f} ms'),
        ("Latency p50 / p90 / p95 / p99",
         f'{lt["latency_p50_ms"]:.0f} / {lt["latency_p90_ms"]:.0f} / '
         f'{lt["latency_p95_ms"]:.0f} / {lt["latency_p99_ms"]:.0f} ms'),
        ("Unloaded baseline", f'{lt["baseline_ms"]:.0f} ms'),
        ("Degradation under load", f'{lt["degradation_factor"]:.2f}x slower at p50'),
        ("Status codes", str(lt["status_counts"] or "(none)")),
        ("Connection errors", str(lt["errors"] or "(none)")),
    ]
    body = "".join(f'<tr><td>{e(k)}</td><td>{e(v)}</td></tr>' for k, v in rows)

    # a compact latency bar chart, so the percentile spread reads at a glance
    pcts = [("p50", lt["latency_p50_ms"]), ("p90", lt["latency_p90_ms"]),
            ("p95", lt["latency_p95_ms"]), ("p99", lt["latency_p99_ms"]),
            ("max", lt["latency_max_ms"])]
    top = max([v for _, v in pcts] + [1])
    bars = "".join(
        f'<div class="bar"><span class="lbl">{e(k)}</span>'
        f'<span class="fill" style="width:{max(2, 100 * v / top):.1f}%"></span>'
        f'<span class="val">{v:.0f} ms</span></div>'
        for k, v in pcts
    )
    return (
        f'<p><span class="pill {cls}">{"passed" if lt["passed"] else "failed"}</span> '
        f'{e(lt["verdict"])}</p>'
        f'<div class="bars">{bars}</div>'
        f'<table>{body}</table>'
    )


def _regression_block(d: dict) -> str:
    e = _esc
    r = d["regression"]
    if not r:
        return ('<div class="empty">No earlier run of this URL to compare against '
                '&mdash; this run becomes the baseline.</div>')
    def group(title, items, cls):
        if not items:
            return ""
        rows = "".join(
            f'<li><b>{e(x["test"])}</b> <span class="chip">{e(x["area"])}</span>'
            + (f'<div class="mono">{e(x["error"])}</div>' if x.get("error") else "")
            + "</li>"
            for x in items
        )
        return f'<h4 class="{cls}">{e(title)} ({len(items)})</h4><ul>{rows}</ul>'

    head = (f'<p>Compared <b>{r["compared"]}</b> test(s) against baseline run '
            f'<code>{e(r["baseline_run_id"])}</code>.</p>')
    body = (
        group("Regressions — passed before, failing now", r["regressions"], "bad")
        + group("Fixes — failing before, passing now", r["fixes"], "ok")
        + group("Still failing", r["still_failing"], "bad")
    )
    if not body:
        body = '<div class="empty">No behaviour changed since the baseline.</div>'
    tail = f'<p class="note">{r["stable"]} test(s) unchanged and still passing.'
    if r["new_tests"]:
        tail += f' {len(r["new_tests"])} new in this run.'
    if r["missing_tests"]:
        tail += f' {len(r["missing_tests"])} baseline test(s) not run this time.'
    return head + body + tail + "</p>"


def _findings_block(d: dict) -> str:
    """Broken links, a11y, UI, console and network, as one set of tables."""
    e = _esc
    out = []

    # broken links
    if d["broken_links"]:
        rows = "".join(
            f'<tr><td><span class="pill failed">{e(b.get("status") or "unreachable")}</span></td>'
            f'<td class="mono">{e(b["url"])}</td><td>{e(b.get("text", ""))}</td>'
            f'<td>{e(b.get("error", ""))}</td></tr>'
            for b in d["broken_links"][:40]
        )
        out.append(
            f'<h3>Broken links ({len(d["broken_links"])} of {d["links_checked"]} checked)</h3>'
            f'<table><tr><th>Status</th><th>URL</th><th>Link text</th><th>Error</th></tr>{rows}</table>'
        )
    else:
        out.append(f'<h3>Broken links</h3><div class="empty">Checked '
                   f'{d["links_checked"]} link(s) &mdash; none broken.</div>')

    # accessibility
    if d["a11y"]:
        order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3, "": 4}
        rows = "".join(
            f'<tr><td><span class="pill {e(i["impact"] or "info")}">{e(i["impact"] or "-")}</span></td>'
            f'<td>{e(i["rule"])}</td><td class="mono">{e(i["selector"])}</td>'
            f'<td>{e(i["description"])}</td></tr>'
            for i in sorted(d["a11y"], key=lambda x: order.get(x["impact"], 4))[:60]
        )
        out.append(
            f'<h3>Accessibility ({len(d["a11y"])} issues)</h3>'
            f'<table><tr><th>Impact</th><th>Rule</th><th>Element</th><th>Detail</th></tr>{rows}</table>'
        )
    else:
        out.append('<h3>Accessibility</h3><div class="empty">No issues found by the basic checks.</div>')

    # UI
    if d["ui_issues"]:
        rows = "".join(
            f'<tr><td>{e(i["rule"])}</td><td class="mono">{e(i["selector"])}</td>'
            f'<td>{e(i["description"])}</td></tr>'
            for i in d["ui_issues"][:60]
        )
        out.append(
            f'<h3>UI rendering ({len(d["ui_issues"])} findings)</h3>'
            f'<table><tr><th>Rule</th><th>Element</th><th>Detail</th></tr>{rows}</table>'
        )
    else:
        out.append('<h3>UI rendering</h3><div class="empty">No rendering problems found.</div>')

    # console + network
    if d["console_errors"]:
        items = "".join(f'<li class="mono">{e(x)}</li>' for x in d["console_errors"][:30])
        out.append(f'<h3>Console errors ({len(d["console_errors"])})</h3><ul>{items}</ul>')
    else:
        out.append('<h3>Console errors</h3><div class="empty">Console stayed clean.</div>')

    if d["network_failures"]:
        items = "".join(f'<li class="mono">{e(x)}</li>' for x in d["network_failures"][:30])
        out.append(f'<h3>Failed network requests ({len(d["network_failures"])})</h3><ul>{items}</ul>')
    else:
        out.append('<h3>Failed network requests</h3><div class="empty">Every request succeeded.</div>')

    return "".join(out)


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
        bug_rows = ('<tr><td colspan="3" class="empty">'
                    'No confirmed bugs &mdash; nothing failed in this run.</td></tr>')

    if d["tests"]:
        test_rows = "".join(
            f'<tr><td><span class="pill {e(t["status"])}">{e(t["status"])}</span></td>'
            f'<td>{e(t["name"])}</td><td><span class="chip">{e(t["area"])}</span></td>'
            f'<td><span class="chip">{e(t["technique"])}</span></td>'
            f'<td>{t["duration_ms"]} ms</td>'
            f'<td>{len(t["screenshots"])}</td></tr>'
            for t in d["tests"]
        )
    else:
        test_rows = '<tr><td colspan="6" class="empty">No tests were planned.</td></tr>'

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
    <h2>Testing methods</h2>
    {_methods_block(d)}
  </section>

  <section class="panel">
    <h2>Coverage by area</h2>
    {_coverage_table(d)}
  </section>

  <section class="panel">
    <h2>Test results</h2>
    <table><tr><th>Status</th><th>Test</th><th>Area</th><th>Technique</th><th>Duration</th><th>Shots</th></tr>{test_rows}</table>
  </section>

  <section class="panel">
    <h2>Load test</h2>
    {_load_block(d)}
  </section>

  <section class="panel">
    <h2>Regression vs previous run</h2>
    {_regression_block(d)}
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
            f'<li><span class="pill {e(s["status"])}">{e(s["status"])}</span> '
            f'<b>{e(s["action"])}</b> {e(s["description"])} '
            f'<small>{e(s["duration_ms"])} ms</small>'
            f'<div class="mono">{e(s["selector"])}</div>'
            f'<div class="gallery">{_img(s["before"], screenshots)}{_img(s["after"], screenshots)}</div>'
            f'{"<pre>" + e(s["error"]) + "</pre>" if s["error"] else ""}</li>'
            for s in t["steps"]
        )
        # Evidence for the test as a whole — on a passing test this is the
        # only proof the flow actually worked, so it always gets rendered.
        test_shots = "".join(_img(sid, screenshots) for sid in t["screenshots"])
        evidence = (
            f'<h4>Evidence</h4><div class="gallery">{test_shots}</div>'
            if test_shots else ""
        )
        test_items.append(
            f'<details class="item {e(t["status"])}" open>'
            f'<summary><span class="pill {e(t["status"])}">{e(t["status"])}</span> '
            f'<b>{e(t["id"])}</b> &middot; {e(t["name"])} '
            f'<span class="chip">{e(t["area"])}</span>'
            f'<span class="chip">{e(t["technique"])}</span> '
            f'<small>{e(t["duration_ms"])} ms &middot; '
            f'{len(t["screenshots"])} screenshot{"" if len(t["screenshots"]) == 1 else "s"}</small>'
            f'</summary>'
            f'<p>{e(t["description"])}</p>'
            f'<p><b>Expected:</b> {e(t["expected"])}</p>'
            f'<ol>{steps}</ol>'
            f'{evidence}'
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
        bugs_html = (
            '<div class="empty">No confirmed bugs &mdash; every test that ran '
            'passed. The evidence for each one is above.</div>'
        )

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
    <h2>Coverage by area</h2>
    {_coverage_table(d)}
    {_methods_block(d)}
  </section>
  <section class="panel">
    <h2>Load test</h2>
    {_load_block(d)}
  </section>
  <section class="panel">
    <h2>Regression vs previous run</h2>
    {_regression_block(d)}
  </section>
  <section class="panel">
    <h2>Findings</h2>
    {_findings_block(d)}
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
.chip{display:inline-block;padding:2px 7px;margin:0 3px;border-radius:6px;background:#1e293b;border:1px solid #334155;font-size:10px;color:#cbd5e1}
.note{color:#94a3b8;font-size:13px}
.methods{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px;margin-top:10px}
.method{background:#0b1020;border:1px solid #273449;border-radius:9px;padding:10px}
.method b{display:block;font-size:12px;font-weight:600}
.method span{font-size:11px;color:#94a3b8}
td.ok,h4.ok{color:#4ade80}
td.bad,h4.bad{color:#f87171}
.bars{margin:14px 0}
.bar{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:12px}
.bar .lbl{width:34px;color:#94a3b8;font-family:monospace}
.bar .fill{height:14px;border-radius:4px;background:linear-gradient(90deg,#2563eb,#60a5fa)}
.bar .val{color:#cbd5e1;font-family:monospace}
h3{margin:22px 0 8px;font-size:16px}
h4{margin:14px 0 6px;font-size:13px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.hero{display:block}}
"""
