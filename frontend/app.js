/*
 * Front end for the QA agent.
 *
 * Flow is Setup → Test Cases → Execution → Results → Bugs → Dashboard, and
 * the tab you're on auto-advances as the run moves through its phases —
 * but only until you click a tab yourself, after which it stops yanking
 * the view out from under you.
 *
 * One run's payload is kept in `current`, so re-filtering a table or
 * switching tabs re-renders from memory instead of hitting the API again.
 */

// ── Elements ──────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

const form = $("run-form");
const urlInput = $("url-input");
const urlError = $("url-error");
const modeInput = $("mode-input");
const maxTestsInput = $("max-tests-input");
const maxPagesInput = $("max-pages-input");
const checksInput = $("checks-input");
const replayInput = $("replay-input");
const loadInput = $("load-input");
const loadOptions = $("load-options");
const loadConcurrencyInput = $("load-concurrency-input");
const loadRequestsInput = $("load-requests-input");
const startBtn = $("start-btn");
const formNote = $("form-note");

const runpill = $("runpill");
const rpPhase = $("rp-phase");
const rpUrl = $("rp-url");
const rpClock = $("rp-clock");
const stopBtn = $("stop-btn");
const topProgress = $("topbar-progress");

const casesTableBody = document.querySelector("#cases-table tbody");
const resultsTableBody = document.querySelector("#results-table tbody");
const historyTableBody = document.querySelector("#history-table tbody");
const historyCount = $("history-count");
const pipelineEl = $("pipeline");
const execProgress = $("exec-progress");
const currentTestEl = $("current-test");
const resultCards = $("result-cards");
const bugsList = $("bugs-list");
const dashboardBody = $("dashboard-body");

// ── Run state held by the page ────────────────────────────────────
let current = null;          // last payload from GET /api/runs/{id}
let currentRunId = null;
let pollTimer = null;
let clockTimer = null;
let clockBase = null;        // { elapsed, at } — lets the clock tick between polls
let userPickedTab = false;

const DONE_PHASES = ["done", "failed", "aborted"];
const filters = { cases: "all", results: "all", bugs: "all" };
const search = { cases: "", results: "" };

// The pipeline on the Execution tab. Each entry lists the run phases that
// mean "this stage is finished", and optionally the ones that mean
// "this stage is what's happening right now".
const PIPELINE = [
  { label: "Exploration",         done: ["analyzed", "planned", "executing", "reporting", "done"], active: ["navigated"] },
  { label: "Test case generation", done: ["planned", "executing", "reporting", "done"], active: ["analyzed"] },
  { label: "Browser execution",    done: ["reporting", "done"], active: ["executing"] },
  { label: "Validation & checks",  done: ["reporting", "done"], active: ["executing"] },
  { label: "Bug analysis",         done: ["reporting", "done"] },
  { label: "Report generation",    done: ["done"], active: ["reporting"] },
];

const SEV_COLORS = {
  critical: "#be123c", high: "#ea580c", medium: "#ca8a04",
  low: "#2563eb", info: "#64748b",
};

// ── Theme ─────────────────────────────────────────────────────────
$("theme-btn").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("qa-theme", root.dataset.theme); } catch (e) {}
});

// ── Tabs ──────────────────────────────────────────────────────────
const tabButtons = [...document.querySelectorAll(".tab")];

$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  userPickedTab = true;
  showTab(btn.dataset.tab);
});

// Arrow-key navigation, as the tablist role promises.
$("tabs").addEventListener("keydown", (e) => {
  const keys = { ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last" };
  if (!(e.key in keys)) return;
  e.preventDefault();
  const i = tabButtons.findIndex((b) => b.getAttribute("aria-selected") === "true");
  const move = keys[e.key];
  const next = move === "first" ? 0
    : move === "last" ? tabButtons.length - 1
    : (i + move + tabButtons.length) % tabButtons.length;
  userPickedTab = true;
  showTab(tabButtons[next].dataset.tab);
  tabButtons[next].focus();
});

function showTab(name) {
  const already = document.querySelector(`.tab[data-tab="${name}"]`)
    ?.getAttribute("aria-selected") === "true";

  for (const b of tabButtons) {
    const on = b.dataset.tab === name;
    b.setAttribute("aria-selected", String(on));
    b.tabIndex = on ? 0 : -1;
  }
  for (const p of document.querySelectorAll(".panel")) {
    p.classList.toggle("active", p.id === `panel-${name}`);
  }

  // Panels differ wildly in height, so arriving on a short one while
  // scrolled deep into a long one would show nothing but empty page.
  if (!already && window.scrollY > 0) window.scrollTo({ top: 0, behavior: "instant" });
}

/** Follow the run through the flow until the user takes the wheel. */
function autoTab(phase) {
  if (userPickedTab) return;
  if (phase === "planned") showTab("cases");
  else if (["init", "navigated", "analyzed", "executing"].includes(phase)) showTab("execution");
  else if (phase === "reporting") showTab("results");
  else if (DONE_PHASES.includes(phase)) showTab("dashboard");
}

function setTabCount(which, n, bad) {
  const el = document.querySelector(`[data-count="${which}"]`);
  if (!el) return;
  el.textContent = n;
  el.classList.toggle("hidden", !n);
  el.classList.toggle("bad", Boolean(bad));
}

// ── Toasts ────────────────────────────────────────────────────────
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span>${escapeHtml(message)}</span>
    <button class="x" type="button" aria-label="Dismiss">✕</button>`;
  el.querySelector(".x").addEventListener("click", () => el.remove());
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 7000);
}

// ── Setup form ────────────────────────────────────────────────────
loadInput.addEventListener("change", () => {
  loadOptions.classList.toggle("hidden", !loadInput.checked);
});

urlInput.addEventListener("input", () => {
  urlInput.setAttribute("aria-invalid", "false");
  urlError.textContent = "";
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  let url = urlInput.value.trim();
  if (!url) return invalidUrl("Enter the URL of the site to test.");
  // a bare "example.com" is what people actually type — assume https
  if (!/^https?:\/\//i.test(url)) url = "https://" + url;
  try { new URL(url); } catch { return invalidUrl("That doesn't look like a URL."); }
  urlInput.value = url;

  const body = {
    url,
    headless: modeInput.value === "headless",
    run_checks: checksInput.checked,
    reuse_baseline_plan: replayInput.checked,
    load_test: loadInput.checked,
  };
  const maxTests = parseInt(maxTestsInput.value, 10);
  if (maxTests > 0) body.max_tests = maxTests;
  const maxPages = parseInt(maxPagesInput.value, 10);
  if (maxPages > 0) body.max_pages = maxPages;
  if (loadInput.checked) {
    const c = parseInt(loadConcurrencyInput.value, 10);
    const n = parseInt(loadRequestsInput.value, 10);
    if (c > 0) body.load_concurrency = c;
    if (n > 0) body.load_requests = n;
  }

  setStarting(true);
  try {
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await errorText(res));
    const { run_id } = await res.json();

    userPickedTab = false;
    showTab("execution");
    watchRun(run_id);
    toast(`Testing ${url}`, "ok");
  } catch (err) {
    toast("Couldn't start the run: " + err.message, "bad");
    setStarting(false);
  }
});

function invalidUrl(msg) {
  urlInput.setAttribute("aria-invalid", "true");
  urlError.textContent = msg;
  urlInput.focus();
}

function setStarting(on) {
  startBtn.disabled = on;
  startBtn.innerHTML = on
    ? '<span class="spin"></span> Starting…'
    : "Start testing";
}

function setRunning(on) {
  startBtn.disabled = on;
  startBtn.innerHTML = on ? '<span class="spin"></span> Running…' : "Start testing";
  formNote.textContent = on ? "A run is in flight — stop it from the bar above to start another." : "";
  topProgress.classList.toggle("on", on);
}

// ── Stop ──────────────────────────────────────────────────────────
stopBtn.addEventListener("click", async () => {
  if (!currentRunId) return;
  stopBtn.disabled = true;
  try {
    const res = await fetch(`/api/runs/${currentRunId}/cancel`, { method: "POST" });
    if (!res.ok) throw new Error(await errorText(res));
    toast("Stopping — the run will finish its current step and write what it has.");
  } catch (err) {
    toast("Couldn't stop the run: " + err.message, "bad");
  } finally {
    stopBtn.disabled = false;
  }
});

// ── Polling ───────────────────────────────────────────────────────
function watchRun(runId) {
  if (pollTimer) clearInterval(pollTimer);
  currentRunId = runId;

  const poll = async () => {
    try {
      const res = await fetch(`/api/runs/${runId}`);
      if (!res.ok) return;
      const data = await res.json();
      current = data;
      render(data);
      autoTab(data.phase);

      const finished = DONE_PHASES.includes(data.phase);
      setRunning(!finished);
      stopBtn.classList.toggle("hidden", finished);

      if (finished) {
        clearInterval(pollTimer);
        stopClock();
        loadHistory();
      }
    } catch (err) {
      // a transient fetch failure shouldn't kill the poll loop
    }
  };

  poll();
  pollTimer = setInterval(poll, 2000);
}

/** Show a finished run without starting a poll loop for it. */
async function openRun(runId) {
  if (pollTimer) clearInterval(pollTimer);
  stopClock();
  try {
    const res = await fetch(`/api/runs/${runId}`);
    if (!res.ok) throw new Error(await errorText(res));
    const data = await res.json();
    currentRunId = runId;
    current = data;
    render(data);
    if (!DONE_PHASES.includes(data.phase)) {
      watchRun(runId);          // still going — follow it live
    } else {
      setRunning(false);
      stopBtn.classList.add("hidden");
      userPickedTab = false;
      autoTab(data.phase);
    }
  } catch (err) {
    toast("Couldn't open that run: " + err.message, "bad");
  }
}

// ── The elapsed clock ─────────────────────────────────────────────
function syncClock(data) {
  clockBase = { elapsed: data.elapsed_ms ?? 0, at: Date.now() };
  const live = !DONE_PHASES.includes(data.phase);
  if (live && !clockTimer) clockTimer = setInterval(tickClock, 1000);
  if (!live) stopClock();
  tickClock();
}

function tickClock() {
  if (!clockBase) return;
  const live = current && !DONE_PHASES.includes(current.phase);
  const ms = clockBase.elapsed + (live ? Date.now() - clockBase.at : 0);
  rpClock.textContent = clock(ms);
}

function stopClock() {
  if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
}

// ── Render ────────────────────────────────────────────────────────
function render(data) {
  runpill.classList.remove("hidden");
  rpUrl.textContent = data.url;
  rpUrl.title = data.url;

  const phase = data.cancelling ? "stopping" : data.phase;
  rpPhase.textContent = phase;
  rpPhase.className = "badge " + phaseClass(data);
  syncClock(data);

  setTabCount("cases", data.tests.length, false);
  setTabCount("bugs", data.bugs.length, data.bugs.length > 0);

  renderCases(data);
  renderPipeline(data);
  renderCurrent(data);
  renderResults(data);
  renderBugs(data);
  renderDashboard(data);
}

function phaseClass(data) {
  if (data.cancelling) return "warn";
  if (data.phase === "done") return data.counts?.failed ? "warn" : "ok";
  if (data.phase === "failed") return "bad";
  if (data.phase === "aborted") return "warn";
  return "running";
}

// 2. Test cases
function renderCases(data) {
  const rows = data.tests
    .map((t, i) => ({ t, label: `TC-${String(i + 1).padStart(3, "0")}` }))
    .filter(({ t, label }) => matchesStatus(t.status, filters.cases)
      && matchesSearch(`${label} ${t.name} ${t.area} ${t.technique}`, search.cases));

  if (!rows.length) {
    casesTableBody.innerHTML = emptyRow(6, data.tests.length
      ? "No test case matches those filters."
      : "No test cases generated yet.");
    return;
  }

  casesTableBody.innerHTML = rows.map(({ t, label }) => `
    <tr>
      <td class="num">${label}</td>
      <td>${escapeHtml(t.name)}
        ${t.source !== "planner" ? '<span class="chip auto">auto</span>' : ""}
        ${t.description ? `<span class="cell-sub">${escapeHtml(t.description)}</span>` : ""}</td>
      <td class="tight"><span class="chip">${escapeHtml(t.area ?? "-")}</span></td>
      <td class="tight"><span class="chip">${escapeHtml(t.technique ?? "-")}</span></td>
      <td class="tight"><span class="pri pri-${escapeHtml(t.priority ?? "info")}">${escapeHtml(t.priority ?? "-")}</span></td>
      <td class="tight status-${escapeHtml(t.status)}">${statusLabel(t.status)}</td>
    </tr>`).join("");
}

// 3. Execution
function renderPipeline(data) {
  const phase = data.phase;
  pipelineEl.innerHTML = PIPELINE.map((stage) => {
    let mark = "", cls = "todo";
    if (stage.done.includes(phase)) { mark = "✓"; cls = "done"; }
    else if ((stage.active ?? []).includes(phase)) { mark = "●"; cls = "running"; }
    if (["failed", "aborted"].includes(phase) && cls === "todo") { mark = "×"; cls = "failed"; }
    return `<li class="${cls}"><span class="dot">${mark}</span>
      <span>${escapeHtml(stage.label)}</span></li>`;
  }).join("");

  const c = data.counts ?? { total: 0, passed: 0, failed: 0 };
  const done = c.passed + c.failed;
  const pct = (n) => (c.total ? (100 * n) / c.total : 0);
  execProgress.innerHTML = `
    <div class="pl"><span>Tests executed</span><span>${done} / ${c.total}</span></div>
    <div class="track">
      <i class="p" style="width:${pct(c.passed)}%"></i>
      <i class="f" style="width:${pct(c.failed)}%"></i>
    </div>`;
}

function renderCurrent(data) {
  // whichever test is running now, else the last one that produced a result
  const running = data.tests.find((t) => t.id === data.current_test_id);
  const finished = [...data.tests].reverse()
    .find((t) => t.status === "passed" || t.status === "failed");
  const test = running ?? finished;

  const fatal = data.fatal_error
    ? `<div class="notice ${data.phase === "aborted" ? "warn" : ""}">
         <span>${data.phase === "aborted" ? "Run stopped" : "Run failed"}:
         ${escapeHtml(data.fatal_error)}</span></div>`
    : "";

  if (!test) {
    currentTestEl.innerHTML = fatal ||
      '<div class="empty"><span class="glyph">⏵</span> Nothing running yet.</div>';
    return;
  }

  // an older backend won't send per-step detail — render the test without
  // it rather than throwing and blanking the whole tab
  const steps = (test.steps ?? []).map((s) => {
    const isNow = running && s.index === data.current_step_index
      && !["passed", "failed", "error"].includes(s.status);
    const cls = isNow ? "running" : s.status;
    const icon = s.status === "passed" ? "✓"
      : s.status === "failed" || s.status === "error" ? "✗"
      : isNow ? "●" : "○";
    return `<li class="step-${escapeHtml(cls)}">
      <span class="mark">${icon}</span>
      <div>
        <span class="act">${escapeHtml(s.action)}</span>${escapeHtml(s.description)}
        ${s.selector ? `<br><code>${escapeHtml(s.selector)}</code>` : ""}
        ${s.error ? `<div class="err">${escapeHtml(s.error)}</div>` : ""}
        ${s.screenshot ? `<div class="shots">${shotImg(s.screenshot, s.description)}</div>` : ""}
      </div>
    </li>`;
  }).join("");

  currentTestEl.innerHTML = `
    <div class="current-head">
      <span class="badge ${running ? "running" : test.status === "passed" ? "ok" : "bad"}">
        ${running ? "running" : statusLabel(test.status)}</span>
      <b>${escapeHtml(test.name)}</b>
    </div>
    ${test.expected ? `<p class="lead" style="margin-top:6px">Expected: ${escapeHtml(test.expected)}</p>` : ""}
    <ol class="steps">${steps || '<li class="step-pending"><span class="mark">○</span><div>No steps recorded.</div></li>'}</ol>
    ${fatal}`;
}

// 4. Results
function renderResults(data) {
  const c = data.counts ?? { total: 0, passed: 0, failed: 0, blocked: 0 };
  resultCards.innerHTML = `
    ${stat("Total tests", c.total, "")}
    ${stat("Passed", c.passed, "ok")}
    ${stat("Failed", c.failed, "bad")}
    ${stat("Not run", c.blocked, "warn")}
    ${stat("Bugs", data.bugs.length, data.bugs.length ? "bad" : "")}`;

  const rows = data.tests.filter((t) =>
    matchesStatus(t.status, filters.results)
    && matchesSearch(`${t.name} ${t.area} ${t.error ?? ""}`, search.results));

  if (!rows.length) {
    resultsTableBody.innerHTML = emptyRow(5, data.tests.length
      ? "No result matches those filters."
      : "No results yet.");
    return;
  }

  resultsTableBody.innerHTML = rows.map((t) => `
    <tr>
      <td>${escapeHtml(t.name)}</td>
      <td class="tight"><span class="chip">${escapeHtml(t.area ?? "-")}</span></td>
      <td class="tight status-${escapeHtml(t.status)}">${statusLabel(t.status)}</td>
      <td class="num">${t.duration_ms ? Math.round(t.duration_ms) + " ms" : "—"}</td>
      <td>${escapeHtml(t.error ?? "") || (t.status === "passed" ? escapeHtml(t.expected ?? "") : "—")}</td>
    </tr>`).join("");
}

// 5. Bugs
function renderBugs(data) {
  if (!data.bugs.length) {
    bugsList.innerHTML = DONE_PHASES.includes(data.phase)
      ? '<div class="empty"><span class="glyph">✅</span> No bugs detected — everything that ran passed.</div>'
      : '<div class="empty"><span class="glyph">🔍</span> None so far.</div>';
    return;
  }

  const shown = data.bugs
    .map((b, i) => ({ b, label: `BUG-${String(i + 1).padStart(3, "0")}` }))
    .filter(({ b }) => filters.bugs === "all" || b.severity === filters.bugs);

  if (!shown.length) {
    bugsList.innerHTML = '<div class="empty"><span class="glyph">🔍</span> No bug at that severity.</div>';
    return;
  }

  bugsList.innerHTML = shown.map(({ b, label }, idx) => {
    const shots = (b.screenshots ?? []).slice(0, 4).map((u, j) =>
      `<figure>${shotImg(u, `${label} evidence`)}
        <figcaption>${j === 0 ? "Before / context" : "After"}</figcaption></figure>`).join("");
    const repro = (b.steps_to_reproduce ?? []).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
    return `
      <details class="bug sev-${escapeHtml(b.severity)}" ${idx === 0 ? "open" : ""}>
        <summary>
          <span class="bug-id">${label}</span>
          <span class="bug-title">${escapeHtml(b.title)}</span>
          <span class="pri pri-${escapeHtml(b.severity)}">${escapeHtml(b.severity)}</span>
          <span class="chip">${escapeHtml(b.category)}</span>
        </summary>
        <div class="bug-body">
          ${b.test_name ? `<p class="lead" style="margin-top:12px">From: ${escapeHtml(b.test_name)}</p>` : ""}
          ${b.description ? `<p>${escapeHtml(b.description)}</p>` : ""}
          ${repro ? `<h4>Steps to reproduce</h4><ol>${repro}</ol>` : ""}
          <div class="ea">
            <div><h4>Expected</h4><pre>${escapeHtml(b.expected || "—")}</pre></div>
            <div><h4>Actual</h4><pre>${escapeHtml(b.actual || "—")}</pre></div>
          </div>
          ${shots ? `<h4>Evidence</h4><div class="gallery">${shots}</div>` : ""}
          <h4>Share</h4>
          <button class="btn ghost sm" type="button" data-copy-bug="${idx}">Copy as text</button>
        </div>
      </details>`;
  }).join("");

  bugsList.querySelectorAll("[data-copy-bug]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const { b, label } = shown[Number(btn.dataset.copyBug)];
      copyText(bugAsText(b, label), btn);
    });
  });
}

function bugAsText(b, label) {
  const steps = (b.steps_to_reproduce ?? []).map((s, i) => `  ${i + 1}. ${s}`).join("\n");
  return [
    `${label}: ${b.title}`,
    `Severity: ${b.severity}   Category: ${b.category}`,
    b.url ? `URL: ${b.url}` : "",
    b.description ? `\n${b.description}` : "",
    steps ? `\nSteps to reproduce:\n${steps}` : "",
    `\nExpected: ${b.expected || "—"}`,
    `Actual:   ${b.actual || "—"}`,
  ].filter(Boolean).join("\n");
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const was = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = was; }, 1500);
  } catch {
    toast("Couldn't reach the clipboard — your browser blocked it.", "bad");
  }
}

// 6. Dashboard
function renderDashboard(data) {
  const c = data.counts ?? { total: 0, passed: 0, failed: 0, blocked: 0 };
  const rate = Math.round((data.pass_rate ?? 0) * 100);
  const ringColor = rate >= 90 ? "var(--ok)" : rate >= 60 ? "var(--warn)" : "var(--bad)";

  const links = [];
  if (data.dashboard_url) links.push(linkBtn(data.dashboard_url, "Static dashboard", true));
  if (data.report_html_url) links.push(linkBtn(data.report_html_url, "Detailed HTML report", true));
  if (data.report_html_url) links.push(linkBtn(`/api/runs/${data.run_id}/report.pdf`, "Download PDF", false));

  const warnings = (data.warnings ?? []).filter((w) => !/^\[\w+\] (planned|report generated)/.test(w));

  dashboardBody.innerHTML = `
    <div class="card"><div class="card-body">
      <div class="summary">
        <div class="big">${c.total}<small>total tests</small></div>
        <div class="big ok">${c.passed}<small>passed</small></div>
        <div class="big bad">${c.failed}<small>failed</small></div>
        <div class="ring">
          <div class="ring-wrap">
            <div class="ring-dial" style="--pct:${rate};--ring-color:${ringColor}"></div>
            <span class="ring-num">${rate}%</span>
          </div>
          <small>pass rate</small>
        </div>
      </div>
    </div></div>

    <div class="card">
      <div class="card-head"><h3>Run facts</h3>
        <div class="spacer"></div>
        <span class="lead mono">${escapeHtml(data.run_id)}</span>
      </div>
      <div class="card-body">
        <div class="facts">
          ${fact("Pages explored", data.pages_discovered ?? "—")}
          ${fact("Bugs detected", data.bugs.length)}
          ${fact("Screenshots", data.screenshots ?? "—")}
          ${fact("Execution time", clock(data.elapsed_ms))}
          ${fact("LLM calls", data.llm_calls ?? "—")}
          ${fact("Cost", "$" + (data.cost_usd ?? 0).toFixed(4))}
          ${fact("Broken links", data.broken_links ?? 0)}
          ${fact("Console errors", data.console_errors ?? 0)}
          ${fact("Failed requests", data.network_failures ?? 0)}
          ${fact("A11y issues", data.a11y_issues ?? 0)}
        </div>
      </div>
    </div>

    <div class="card"><div class="card-body">
      <div class="charts">
        <div><h3 style="margin:0 0 12px;font-size:14px">Test distribution by area</h3>
          ${barChart(areaTotals(data))}</div>
        <div><h3 style="margin:0 0 12px;font-size:14px">Bug severity</h3>
          ${barChart(severityTotals(data), SEV_COLORS)}</div>
      </div>
    </div></div>

    ${data.load_test ? loadBlock(data.load_test) : ""}
    ${data.regression ? regressionBlock(data.regression) : ""}

    ${warnings.length ? `
      <div class="card">
        <div class="card-head"><h3>Warnings</h3></div>
        <div class="card-body"><div class="notice warn"><div>
          ${warnings.map((w) => escapeHtml(w)).join("<br>")}
        </div></div></div>
      </div>` : ""}

    ${links.length ? `
      <div class="card">
        <div class="card-head"><h3>Artifacts</h3></div>
        <div class="card-body"><div class="action-links">${links.join("")}</div></div>
      </div>` : ""}`;
}

function linkBtn(href, label, external) {
  return `<a class="btn ghost sm" href="${escapeAttr(href)}"
    ${external ? 'target="_blank" rel="noopener"' : ""}>${escapeHtml(label)}</a>`;
}

function areaTotals(data) {
  const out = {};
  for (const t of data.tests) { const a = t.area ?? "other"; out[a] = (out[a] ?? 0) + 1; }
  return out;
}

function severityTotals(data) {
  const out = {};
  for (const b of data.bugs) out[b.severity] = (out[b.severity] ?? 0) + 1;
  return out;
}

/** Horizontal bars — readable at any count, and no charting library. */
function barChart(totals, colors) {
  const keys = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
  if (!keys.length) return '<div class="empty" style="padding:28px">Nothing to chart.</div>';
  const max = Math.max(...keys.map((k) => totals[k]));
  return `<div class="bars">${keys.map((k) => `
    <div class="bar">
      <span class="lbl" title="${escapeAttr(k)}">${escapeHtml(k)}</span>
      <span class="rail"><i class="fill" style="width:${Math.max(3, (100 * totals[k]) / max)}%${
        colors?.[k] ? `;background:${colors[k]}` : ""}"></i></span>
      <span class="val">${Math.round(totals[k])}</span>
    </div>`).join("")}</div>`;
}

function loadBlock(lt) {
  return `
    <div class="card">
      <div class="card-head"><h3>Load test</h3>
        <span class="badge ${lt.passed ? "ok" : "bad"}">${lt.passed ? "passed" : "failed"}</span>
        <span class="lead">${escapeHtml(lt.verdict)}</span>
      </div>
      <div class="card-body">
        <div class="facts">
          ${fact("Concurrency", lt.concurrency)}
          ${fact("Requests", `${lt.completed} / ${lt.total_requests}`)}
          ${fact("Throughput", lt.requests_per_second.toFixed(1) + " req/s")}
          ${fact("Error rate", (lt.error_rate * 100).toFixed(2) + "%")}
          ${fact("p95 latency", lt.latency_p95_ms.toFixed(0) + " ms")}
          ${fact("Slowdown", lt.degradation_factor.toFixed(2) + "×")}
        </div>
        <h4 style="margin:18px 0 10px;font-size:13px">Latency percentiles (ms)</h4>
        ${barChart({
          p50: lt.latency_p50_ms, p90: lt.latency_p90_ms,
          p95: lt.latency_p95_ms, p99: lt.latency_p99_ms,
        })}
      </div>
    </div>`;
}

function regressionBlock(r) {
  const list = (title, items, cls) =>
    items.length
      ? `<h4 style="margin:16px 0 6px;font-size:13px" class="status-${cls}">${title} (${items.length})</h4>
         <ul style="margin:0;padding-left:20px;font-size:13.5px">${items
          .map((x) => `<li>${escapeHtml(x.test)} <span class="chip">${escapeHtml(x.area)}</span></li>`)
          .join("")}</ul>`
      : "";
  return `
    <div class="card">
      <div class="card-head"><h3>Regression vs previous run</h3>
        <span class="lead mono">baseline ${escapeHtml(r.baseline_run_id)}</span>
      </div>
      <div class="card-body">
        <p class="lead">Compared ${r.compared} test(s); ${r.stable} unchanged and still passing.</p>
        ${list("Regressions — passed before, failing now", r.regressions, "failed")}
        ${list("Fixes — failing before, passing now", r.fixes, "passed")}
        ${list("Still failing", r.still_failing, "failed")}
      </div>
    </div>`;
}

// ── History ───────────────────────────────────────────────────────
async function loadHistory() {
  let runs;
  try {
    const res = await fetch("/api/runs");
    if (!res.ok) return;
    runs = await res.json();
  } catch { return; }

  historyCount.textContent = runs.length
    ? `${runs.length} run${runs.length === 1 ? "" : "s"} on record`
    : "";

  if (!runs.length) {
    historyTableBody.innerHTML = emptyRow(7, "No runs yet — start one above.");
    return;
  }

  historyTableBody.innerHTML = runs.map((r) => {
    const total = r.total || 0;
    const w = (n) => (total ? (100 * n) / total : 0);
    return `
      <tr class="clickable" data-run="${escapeAttr(r.run_id)}">
        <td>${escapeHtml(r.url)}<span class="cell-sub mono">${escapeHtml(r.run_id)}</span></td>
        <td class="tight">${escapeHtml(when(r.started_at))}</td>
        <td class="tight"><span class="badge ${historyPhaseClass(r)}">${escapeHtml(r.live ? "live" : r.phase)}</span></td>
        <td>
          <div class="ratio">
            <span class="ratio-bar">
              <i class="p" style="width:${w(r.passed)}%"></i>
              <i class="f" style="width:${w(r.failed)}%"></i>
              <i class="b" style="width:${w(r.blocked)}%"></i>
            </span>
            <span class="ratio-txt">${r.passed}/${total}</span>
          </div>
        </td>
        <td class="num">${r.bugs}</td>
        <td class="num">${r.duration_ms ? clock(r.duration_ms) : "—"}</td>
        <td><div class="row-actions">
          <button class="iconbtn danger" type="button" data-delete="${escapeAttr(r.run_id)}"
                  title="Delete this run" aria-label="Delete run ${escapeAttr(r.run_id)}">🗑</button>
        </div></td>
      </tr>`;
  }).join("");

  historyTableBody.querySelectorAll("tr[data-run]").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("[data-delete]")) return;
      openRun(tr.dataset.run);
    });
  });

  historyTableBody.querySelectorAll("[data-delete]").forEach((btn) => {
    btn.addEventListener("click", () => deleteRun(btn.dataset.delete));
  });
}

function historyPhaseClass(r) {
  if (r.live) return "running";
  if (r.phase === "done") return r.failed ? "warn" : "ok";
  if (r.phase === "failed") return "bad";
  return "warn";
}

async function deleteRun(runId) {
  if (!confirm("Delete this run? Its screenshots and report are removed from disk too.")) return;
  try {
    const res = await fetch(`/api/runs/${runId}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await errorText(res));
    toast("Run deleted.", "ok");
    if (currentRunId === runId) {
      currentRunId = null;
      current = null;
      runpill.classList.add("hidden");
    }
    loadHistory();
  } catch (err) {
    toast("Couldn't delete: " + err.message, "bad");
  }
}

$("refresh-history").addEventListener("click", loadHistory);

// ── Filters & search ──────────────────────────────────────────────
function wireFilters(containerId, key, attr, rerender) {
  const box = $(containerId);
  if (!box) return;
  box.addEventListener("click", (e) => {
    const btn = e.target.closest(".filter");
    if (!btn) return;
    for (const b of box.querySelectorAll(".filter")) {
      b.setAttribute("aria-pressed", String(b === btn));
    }
    filters[key] = btn.dataset[attr];
    if (current) rerender(current);
  });
}

wireFilters("cases-filters", "cases", "status", renderCases);
wireFilters("results-filters", "results", "status", renderResults);
wireFilters("bugs-filters", "bugs", "sev", renderBugs);

function wireSearch(inputId, key, rerender) {
  const el = $(inputId);
  if (!el) return;
  el.addEventListener("input", () => {
    search[key] = el.value.trim().toLowerCase();
    if (current) rerender(current);
  });
}

wireSearch("cases-search", "cases", renderCases);
wireSearch("results-search", "results", renderResults);

function matchesStatus(status, filter) {
  if (filter === "all") return true;
  if (filter === "failed") return status === "failed" || status === "error";
  if (filter === "other") return !["passed", "failed", "error"].includes(status);
  return status === filter;
}

function matchesSearch(haystack, needle) {
  return !needle || haystack.toLowerCase().includes(needle);
}

// ── Lightbox ──────────────────────────────────────────────────────
const lightbox = $("lightbox");

document.addEventListener("click", (e) => {
  const img = e.target.closest("img.shot");
  if (!img) return;
  $("lightbox-img").src = img.dataset.full || img.src;
  $("lightbox-img").alt = img.alt;
  $("lightbox-caption").textContent = img.alt || "Screenshot";
  $("lightbox-open").href = img.dataset.full || img.src;
  lightbox.showModal();
});

$("lightbox-close").addEventListener("click", () => lightbox.close());
lightbox.addEventListener("click", (e) => {
  // click the backdrop (i.e. outside the dialog's own box) to dismiss
  const box = lightbox.getBoundingClientRect();
  const outside = e.clientX < box.left || e.clientX > box.right
    || e.clientY < box.top || e.clientY > box.bottom;
  if (outside) lightbox.close();
});

function shotImg(url, alt) {
  return `<img class="shot" loading="lazy" src="${escapeAttr(url)}"
    data-full="${escapeAttr(url)}" alt="${escapeAttr(alt || "screenshot")}">`;
}

// ── Small helpers ─────────────────────────────────────────────────
function stat(label, value, cls) {
  return `<div class="stat ${cls}"><span>${escapeHtml(label)}</span><b>${value}</b></div>`;
}

function fact(label, value) {
  return `<div class="fact"><span>${escapeHtml(label)}</span><b>${escapeHtml(String(value))}</b></div>`;
}

function emptyRow(cols, text) {
  return `<tr><td colspan="${cols}"><div class="empty">${escapeHtml(text)}</div></td></tr>`;
}

function statusLabel(s) {
  return s === "pending" ? "ready" : s;
}

function clock(ms) {
  if (!ms) return "00:00";
  const total = Math.round(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** "3 min ago" for anything recent, an absolute date once it isn't. */
function when(iso) {
  if (!iso) return "—";
  const then = new Date(iso);
  if (isNaN(then)) return "—";
  const secs = (Date.now() - then.getTime()) / 1000;
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} h ago`;
  if (secs < 604800) return `${Math.floor(secs / 86400)} d ago`;
  return then.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

async function errorText(res) {
  try {
    const body = await res.json();
    return body.detail ?? JSON.stringify(body);
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

function escapeAttr(s) {
  return escapeHtml(s).replaceAll('"', "&quot;");
}

// Sticky table headers park directly under the app bar, whatever height
// it currently is — it grows a row when the run pill wraps on mobile.
const appbar = document.querySelector(".appbar");
function measureAppbar() {
  document.documentElement.style.setProperty("--appbar-h", `${appbar.offsetHeight}px`);
}
measureAppbar();
if (window.ResizeObserver) new ResizeObserver(measureAppbar).observe(appbar);
else addEventListener("resize", measureAppbar);

loadHistory();
