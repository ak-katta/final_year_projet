const form = document.getElementById("run-form");
const urlInput = document.getElementById("url-input");
const maxTestsInput = document.getElementById("max-tests-input");
const maxPagesInput = document.getElementById("max-pages-input");
const submitBtn = form.querySelector("button");

const statusBox = document.getElementById("status-box");
const statusUrl = document.getElementById("status-url");
const statusPhaseBadge = document.getElementById("status-phase-badge");
const statusSitetype = document.getElementById("status-sitetype");
const statusPages = document.getElementById("status-pages");
const statusLinks = document.getElementById("status-links");

const results = document.getElementById("results");
const testsTableBody = document.querySelector("#tests-table tbody");
const bugsList = document.getElementById("bugs-list");
const reportText = document.getElementById("report-text");

const historyTableBody = document.querySelector("#history-table tbody");

let pollTimer = null;
const DONE_PHASES = ["done", "failed", "aborted"];

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const body = { url: urlInput.value };
  const maxTests = parseInt(maxTestsInput.value, 10);
  if (maxTests > 0) body.max_tests = maxTests;
  const maxPages = parseInt(maxPagesInput.value, 10);
  if (maxPages > 0) body.max_pages = maxPages;

  submitBtn.disabled = true;
  submitBtn.textContent = "starting...";

  try {
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    const { run_id } = await res.json();

    statusBox.classList.remove("hidden");
    results.classList.add("hidden");
    watchRun(run_id);
  } catch (err) {
    alert("couldn't start the run: " + err.message);
    submitBtn.disabled = false;
    submitBtn.textContent = "run it";
  }
});

function watchRun(runId) {
  if (pollTimer) clearInterval(pollTimer);

  const poll = async () => {
    const res = await fetch(`/api/runs/${runId}`);
    if (!res.ok) return;
    const data = await res.json();
    render(data);

    if (DONE_PHASES.includes(data.phase)) {
      clearInterval(pollTimer);
      submitBtn.disabled = false;
      submitBtn.textContent = "run it";
      loadHistory();
    }
  };

  poll();
  pollTimer = setInterval(poll, 2000);
}

function render(data) {
  statusUrl.textContent = data.url;
  statusPhaseBadge.textContent = data.phase;
  statusPhaseBadge.className = `status-badge phase-${data.phase}`;
  statusSitetype.textContent = data.site_type;
  statusPages.textContent = data.pages_discovered ?? "-";

  const links = [];
  if (data.dashboard_url) links.push(`<a href="${data.dashboard_url}" target="_blank">dashboard</a>`);
  if (data.report_html_url) links.push(`<a href="${data.report_html_url}" target="_blank">detailed HTML report</a>`);
  if (data.report_html_url) links.push(`<a href="/api/runs/${data.run_id}/report.pdf">download PDF</a>`);
  statusLinks.innerHTML = links.join("");

  if (data.tests.length === 0 && !data.fatal_error) return;

  results.classList.remove("hidden");

  testsTableBody.innerHTML = "";
  for (const t of data.tests) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(t.name)}</td>
      <td>${escapeHtml(t.category)}</td>
      <td class="status-${t.status}">${t.status}</td>
    `;
    testsTableBody.appendChild(row);
  }

  bugsList.innerHTML = "";
  if (data.bugs.length === 0) {
    bugsList.innerHTML = "<p style='color:#999'>none so far</p>";
  }
  for (const b of data.bugs) {
    const div = document.createElement("div");
    div.className = `bug sev-${b.severity}`;
    div.innerHTML = `
      <h3>${escapeHtml(b.title)}</h3>
      <div class="meta">${b.severity} · ${b.category} · ${escapeHtml(b.url)}</div>
      <div>${escapeHtml(b.description)}</div>
      ${b.expected ? `<div><strong>expected:</strong> ${escapeHtml(b.expected)}</div>` : ""}
      ${b.actual ? `<div><strong>actual:</strong> ${escapeHtml(b.actual)}</div>` : ""}
      ${b.screenshot ? `<img src="${b.screenshot}" alt="screenshot of bug">` : ""}
    `;
    bugsList.appendChild(div);
  }

  reportText.textContent = data.report || (data.fatal_error ? `run failed: ${data.fatal_error}` : "not generated yet");
}

async function loadHistory() {
  const res = await fetch("/api/runs");
  if (!res.ok) return;
  const runs = await res.json();

  historyTableBody.innerHTML = "";
  for (const r of runs) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(r.url)}</td>
      <td>${r.phase}</td>
      <td>${r.tests_passed}</td>
      <td>${r.tests_failed}</td>
      <td>${r.bugs}</td>
    `;
    row.addEventListener("click", () => {
      statusBox.classList.remove("hidden");
      watchRun(r.run_id);
    });
    historyTableBody.appendChild(row);
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

loadHistory();
