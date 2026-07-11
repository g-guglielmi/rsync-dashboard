const REFRESH_MS = 30000;
const STATUS_LABEL = {
  success: "Success",
  warning: "Warning",
  failed: "Failed",
  running: "Running",
  interrupted: "Interrupted",
  no_data: "No runs yet",
};

let state = { data: null, tab: "overview" };

const app = document.getElementById("app");
const loading = document.getElementById("loading");
const lastUpdatedEl = document.getElementById("last-updated");
const liveDot = document.getElementById("live-dot");

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, val = bytes;
  while (val >= 1024 && i < units.length - 1) { val /= 1024; i++; }
  return `${val.toFixed(val >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "\u2014";
  if (seconds < 60) return `${seconds}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${s}s`;
}

function formatTimestamp(iso) {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return `Today ${time}`;
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return `Yesterday ${time}`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + ` ${time}`;
}

function jobKey(job) { return `${job.server}::${job.category}`; }

function renderOverview(data) {
  const o = data.overview;
  const cards = [
    { label: "Success", value: o.success, cls: "success" },
    { label: "Warning", value: o.warning, cls: "warning" },
    { label: "Failed", value: o.failed, cls: "fail" },
    { label: "Running", value: o.running, cls: "accent" },
    { label: "Transferred", value: formatBytes(o.total_transferred_bytes), cls: "accent" },
    { label: "Deleted files", value: o.total_deleted_files, cls: "" },
  ];
  return `<div class="overview">${cards.map(c => `
    <div class="stat-card ${c.cls ? "stat-card--" + c.cls : ""}">
      <div class="stat-card__value">${c.value}</div>
      <div class="stat-card__label">${c.label}</div>
    </div>`).join("")}</div>`;
}

function renderPulseStrip(runs, size = 20) {
  const slots = runs.slice(0, 12).reverse();
  return `<div class="pulse-strip" style="height:${size}px">
    ${slots.map(r => `<div class="pulse-block pulse-block--${r.status}"
        title="${formatTimestamp(r.start_time)} \u2014 ${STATUS_LABEL[r.status] || r.status}"></div>`).join("")}
  </div>`;
}

function renderTabs(data) {
  const tabs = [{ key: "overview", label: "Overview", dotCls: null }].concat(
    data.jobs.map(j => ({
      key: jobKey(j),
      label: j.category,
      dotCls: j.latest ? j.latest.status : "no_data",
    }))
  );
  return `<div class="tabs">${tabs.map(t => `
    <button class="tab ${state.tab === t.key ? "active" : ""}" data-tab="${t.key}">
      ${t.dotCls ? `<span class="tab__dot" style="background:var(--${dotColorVar(t.dotCls)})"></span>` : ""}
      ${t.label}
    </button>`).join("")}</div>`;
}

function dotColorVar(status) {
  return { success: "success", warning: "warning", failed: "fail", running: "accent",
           interrupted: "interrupted", no_data: "neutral" }[status] || "neutral";
}

function renderJobGrid(data) {
  return `<div class="job-grid">${data.jobs.map(j => {
    const latest = j.latest;
    const status = latest ? latest.status : "no_data";
    return `<div class="job-card" data-tab="${jobKey(j)}">
      <div class="job-card__head">
        <div class="job-card__name">${j.category}</div>
        <span class="badge badge--${status}">${STATUS_LABEL[status] || status}</span>
      </div>
      ${latest ? `
      <div class="job-card__meta">Started <strong>${formatTimestamp(latest.start_time)}</strong> &middot; took <strong>${formatDuration(latest.duration_seconds)}</strong></div>
      ${renderPulseStrip(j.runs)}
      <div class="job-card__stats">
        <div><span class="job-card__stat-label">Transferred</span><br><span class="job-card__stat-value">${formatBytes(latest.size_transferred_bytes)}</span></div>
        <div><span class="job-card__stat-label">Deleted</span><br><span class="job-card__stat-value">${latest.files_deleted} files</span></div>
      </div>` : `<div class="job-card__meta">No log files found yet for this job.</div>`}
    </div>`;
  }).join("")}</div>`;
}

function renderRunsTable(job) {
  const rows = job.runs.map((r, i) => {
    const hasErrors = r.errors && r.errors.length > 0;
    return `
    <tr class="${hasErrors ? "has-errors" : ""}" data-run-index="${i}">
      <td class="status-cell"><span class="status-dot status-dot--${r.status}"></span>${STATUS_LABEL[r.status] || r.status}</td>
      <td>${formatTimestamp(r.start_time)}</td>
      <td>${formatDuration(r.duration_seconds)}</td>
      <td>${formatBytes(r.size_transferred_bytes)}</td>
      <td>${r.files_transferred}</td>
      <td>${r.files_deleted}</td>
    </tr>
    ${hasErrors ? `<tr class="errors-row" data-run-errors="${i}" style="display:none"><td colspan="6">${r.errors.map(e => escapeHtml(e)).join("\n")}</td></tr>` : ""}`;
  }).join("");

  return `<table class="runs">
    <thead><tr>
      <th>Status</th><th>Started</th><th>Duration</th><th>Transferred</th><th>Files</th><th>Deleted</th>
    </tr></thead>
    <tbody>${rows || `<tr><td colspan="6" style="color:var(--text-dim)">No runs recorded yet.</td></tr>`}</tbody>
  </table>`;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderJobDetail(job) {
  const latest = job.latest;
  return `
    <div class="job-detail__head">
      <h2>${job.category}</h2>
      ${latest ? `<span class="badge badge--${latest.status}">${STATUS_LABEL[latest.status] || latest.status}</span>` : ""}
    </div>
    ${job.runs.length ? `<div class="job-detail__pulse">
      <div class="job-card__stat-label">Recent runs</div>
      ${renderPulseStrip(job.runs, 28)}
    </div>` : ""}
    ${renderRunsTable(job)}
  `;
}

function render() {
  const data = state.data;
  if (!data) return;

  if (!data.logs_root_found) {
    app.innerHTML = document.getElementById("tpl-error-state").innerHTML;
    liveDot.className = "live-dot down";
    return;
  }

  let body;
  if (state.tab === "overview") {
    body = renderOverview(data) + renderTabs(data) + renderJobGrid(data);
  } else {
    const job = data.jobs.find(j => jobKey(j) === state.tab);
    if (!job) { state.tab = "overview"; return render(); }
    body = renderOverview(data) + renderTabs(data) + renderJobDetail(job);
  }
  app.innerHTML = body;

  app.querySelectorAll("[data-tab]").forEach(el => {
    el.addEventListener("click", () => { state.tab = el.dataset.tab; render(); });
  });
  app.querySelectorAll("tr.has-errors").forEach(el => {
    el.addEventListener("click", () => {
      const idx = el.dataset.runIndex;
      const errRow = app.querySelector(`[data-run-errors="${idx}"]`);
      if (errRow) errRow.style.display = errRow.style.display === "none" ? "table-row" : "none";
    });
  });
}

async function fetchData() {
  try {
    const res = await fetch("/api/dashboard");
    if (!res.ok) throw new Error(res.status);
    state.data = await res.json();
    loading.style.display = "none";
    liveDot.className = "live-dot";
    lastUpdatedEl.textContent = "updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    render();
  } catch (e) {
    liveDot.className = "live-dot stale";
    lastUpdatedEl.textContent = "connection lost \u2014 retrying\u2026";
  }
}

fetchData();
setInterval(fetchData, REFRESH_MS);
