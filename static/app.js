const REFRESH_MS = 30000;
const STATUS_LABEL = {
  success: "Success",
  warning: "Warning",
  failed: "Failed",
  running: "Running",
  interrupted: "Interrupted",
  no_data: "No runs yet",
};

let state = {
  data: null,
  tab: "overview",
  fullRuns: {},              // jobKey -> full run history from /api/jobs/.../runs
  expandedErrors: new Set(), // `${jobKey}::${filename}` of error rows left open
};

const app = document.getElementById("app");
const loading = document.getElementById("loading");
const lastUpdatedEl = document.getElementById("last-updated");
const liveDot = document.getElementById("live-dot");

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, val = bytes;
  while (val >= 1024 && i < units.length - 1) { val /= 1024; i++; }
  return `${val.toFixed(val >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${s}s`;
}

function formatTimestamp(iso) {
  if (!iso) return "—";
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

function hasMultipleServers(data) {
  return new Set(data.jobs.map(j => j.server)).size > 1;
}

function jobLabel(job, multiServer) {
  return multiServer ? `${job.server} / ${job.category}` : job.category;
}

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
      <div class="stat-card__value">${escapeHtml(c.value)}</div>
      <div class="stat-card__label">${c.label}</div>
    </div>`).join("")}</div>`;
}

// Rounds a byte count up to a "nice" chart ceiling (1024-based, matching
// the Windows-style formatBytes display).
function niceBytesCeil(bytes) {
  if (!bytes || bytes <= 0) return 1;
  let div = 1;
  while (bytes / div >= 1024) div *= 1024;
  const steps = [1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024];
  const v = bytes / div;
  const s = steps.find(x => x >= v) || 1024;
  return s * div;
}

function chartDayLabel(isoDate, index, total) {
  if (index === total - 1) return "Today";
  const d = new Date(isoDate + "T00:00:00");
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderTransferChart(days, label) {
  if (!days || !days.length) return "";

  const W = 720, H = 170;
  const PAD = { top: 14, right: 18, bottom: 26, left: 58 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const maxY = niceBytesCeil(Math.max(...days.map(d => d.bytes)));

  const x = i => PAD.left + (days.length === 1 ? plotW / 2 : (plotW * i) / (days.length - 1));
  const y = b => PAD.top + plotH - (plotH * b) / maxY;

  const pts = days.map((d, i) => `${x(i).toFixed(1)},${y(d.bytes).toFixed(1)}`);
  const linePath = "M" + pts.join(" L");
  const areaPath = `${linePath} L${x(days.length - 1).toFixed(1)},${y(0)} L${x(0).toFixed(1)},${y(0)} Z`;

  const gridlines = [0, 0.5, 1].map(f => {
    const gy = y(maxY * f).toFixed(1);
    return `
      <line x1="${PAD.left}" y1="${gy}" x2="${W - PAD.right}" y2="${gy}" class="chart-grid"></line>
      <text x="${PAD.left - 8}" y="${gy}" class="chart-label chart-label--y">${escapeHtml(formatBytes(maxY * f))}</text>`;
  }).join("");

  const xLabels = days.map((d, i) =>
    `<text x="${x(i).toFixed(1)}" y="${H - 8}" class="chart-label chart-label--x">${escapeHtml(chartDayLabel(d.date, i, days.length))}</text>`
  ).join("");

  const dots = days.map((d, i) => `
    <circle cx="${x(i).toFixed(1)}" cy="${y(d.bytes).toFixed(1)}" r="4" class="chart-dot"></circle>
    <circle cx="${x(i).toFixed(1)}" cy="${y(d.bytes).toFixed(1)}" r="12" class="chart-hit">
      <title>${escapeHtml(chartDayLabel(d.date, i, days.length))} — ${escapeHtml(formatBytes(d.bytes))}</title>
    </circle>`
  ).join("");

  return `<div class="chart-panel">
    <div class="job-card__stat-label">${escapeHtml(label)}</div>
    <svg viewBox="0 0 ${W} ${H}" class="transfer-chart" role="img"
         aria-label="${escapeHtml(label)}">
      ${gridlines}
      <path d="${areaPath}" class="chart-area"></path>
      <path d="${linePath}" class="chart-line"></path>
      ${dots}
      ${xLabels}
    </svg>
  </div>`;
}

function renderPulseStrip(runs, size = 20) {
  const slots = runs.slice(0, 12).reverse();
  return `<div class="pulse-strip" style="height:${size}px">
    ${slots.map(r => `<div class="pulse-block pulse-block--${escapeHtml(r.status)}"
        title="${escapeHtml(formatTimestamp(r.start_time))} — ${escapeHtml(STATUS_LABEL[r.status] || r.status)}"></div>`).join("")}
  </div>`;
}

function renderTabs(data) {
  const multiServer = hasMultipleServers(data);
  const tabs = [{ key: "overview", label: "Overview", dotCls: null }].concat(
    data.jobs.map(j => ({
      key: jobKey(j),
      label: jobLabel(j, multiServer),
      dotCls: j.latest ? j.latest.status : "no_data",
    }))
  );
  return `<div class="tabs">${tabs.map(t => `
    <button class="tab ${state.tab === t.key ? "active" : ""}" data-tab="${escapeHtml(t.key)}">
      ${t.dotCls ? `<span class="tab__dot" style="background:var(--${dotColorVar(t.dotCls)})"></span>` : ""}
      ${escapeHtml(t.label)}
    </button>`).join("")}</div>`;
}

function dotColorVar(status) {
  return { success: "success", warning: "warning", failed: "fail", running: "accent",
           interrupted: "interrupted", no_data: "neutral" }[status] || "neutral";
}

function renderJobGrid(data) {
  const multiServer = hasMultipleServers(data);
  return `<div class="job-grid">${data.jobs.map(j => {
    const latest = j.latest;
    const status = latest ? latest.status : "no_data";
    return `<div class="job-card" data-tab="${escapeHtml(jobKey(j))}">
      <div class="job-card__head">
        <div class="job-card__name">${escapeHtml(jobLabel(j, multiServer))}</div>
        <span class="badge badge--${escapeHtml(status)}">${escapeHtml(STATUS_LABEL[status] || status)}</span>
      </div>
      ${latest ? `
      <div class="job-card__meta">Started <strong>${escapeHtml(formatTimestamp(latest.start_time))}</strong> &middot; took <strong>${escapeHtml(formatDuration(latest.duration_seconds))}</strong></div>
      ${renderPulseStrip(j.runs)}
      <div class="job-card__stats">
        <div><span class="job-card__stat-label">Transferred</span><br><span class="job-card__stat-value">${escapeHtml(formatBytes(latest.size_transferred_bytes))}</span></div>
        <div><span class="job-card__stat-label">Deleted</span><br><span class="job-card__stat-value">${escapeHtml(latest.files_deleted)} files</span></div>
      </div>` : `<div class="job-card__meta">No log files found yet for this job.</div>`}
    </div>`;
  }).join("")}</div>`;
}

function renderRunsTable(runs, key) {
  const rows = runs.map(r => {
    const hasErrors = r.errors && r.errors.length > 0;
    const errKey = `${key}::${r.filename}`;
    const expanded = state.expandedErrors.has(errKey);
    return `
    <tr class="${hasErrors ? "has-errors" : ""}" ${hasErrors ? `data-err-key="${escapeHtml(errKey)}"` : ""}>
      <td class="status-cell"><span class="status-dot status-dot--${escapeHtml(r.status)}"></span>${escapeHtml(STATUS_LABEL[r.status] || r.status)}</td>
      <td>${escapeHtml(formatTimestamp(r.start_time))}</td>
      <td>${escapeHtml(formatDuration(r.duration_seconds))}</td>
      <td>${escapeHtml(formatBytes(r.size_transferred_bytes))}</td>
      <td>${escapeHtml(r.files_transferred)}</td>
      <td>${escapeHtml(r.files_deleted)}</td>
    </tr>
    ${hasErrors ? `<tr class="errors-row" data-errors-for="${escapeHtml(errKey)}" style="display:${expanded ? "table-row" : "none"}"><td colspan="6">${r.errors.map(e => escapeHtml(e)).join("\n")}</td></tr>` : ""}`;
  }).join("");

  return `<table class="runs">
    <thead><tr>
      <th>Status</th><th>Started</th><th>Duration</th><th>Transferred</th><th>Files</th><th>Deleted</th>
    </tr></thead>
    <tbody>${rows || `<tr><td colspan="6" style="color:var(--text-dim)">No runs recorded yet.</td></tr>`}</tbody>
  </table>`;
}

function renderJobDetail(job, multiServer) {
  const key = jobKey(job);
  // The dashboard payload only carries the most recent runs; the per-job
  // endpoint returns the full history and replaces it once loaded.
  const runs = state.fullRuns[key] || job.runs;
  const latest = job.latest;
  return `
    <div class="job-detail__head">
      <h2>${escapeHtml(jobLabel(job, multiServer))}</h2>
      ${latest ? `<span class="badge badge--${escapeHtml(latest.status)}">${escapeHtml(STATUS_LABEL[latest.status] || latest.status)}</span>` : ""}
    </div>
    ${renderTransferChart(job.daily_transfers, "Data transferred per day — last 7 days")}
    ${renderRunsTable(runs, key)}
  `;
}

async function loadFullRuns(job) {
  const key = jobKey(job);
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(job.server)}/${encodeURIComponent(job.category)}/runs`);
    if (!res.ok) throw new Error(res.status);
    const payload = await res.json();
    state.fullRuns[key] = payload.runs;
    if (state.tab === key) render();
  } catch (e) {
    // Keep showing the dashboard payload's runs.
  }
}

function render() {
  const data = state.data;
  if (!data) return;

  if (!data.logs_root_found) {
    app.innerHTML = document.getElementById("tpl-error-state").innerHTML;
    liveDot.className = "live-dot down";
    return;
  }

  const multiServer = hasMultipleServers(data);
  let body;
  if (state.tab === "overview") {
    body = renderOverview(data) + renderTabs(data)
      + renderTransferChart(data.daily_transfers, "Data transferred per day, all jobs — last 7 days")
      + renderJobGrid(data);
  } else {
    const job = data.jobs.find(j => jobKey(j) === state.tab);
    if (!job) { state.tab = "overview"; return render(); }
    body = renderOverview(data) + renderTabs(data) + renderJobDetail(job, multiServer);
  }
  app.innerHTML = body;

  app.querySelectorAll("[data-tab]").forEach(el => {
    el.addEventListener("click", () => {
      state.tab = el.dataset.tab;
      const job = data.jobs.find(j => jobKey(j) === state.tab);
      if (job) loadFullRuns(job);
      render();
    });
  });
  app.querySelectorAll("tr.has-errors").forEach(el => {
    el.addEventListener("click", () => {
      const key = el.dataset.errKey;
      const errRow = app.querySelector(`[data-errors-for="${CSS.escape(key)}"]`);
      if (!errRow) return;
      const open = errRow.style.display === "none";
      errRow.style.display = open ? "table-row" : "none";
      if (open) state.expandedErrors.add(key);
      else state.expandedErrors.delete(key);
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
    // Keep the open job's full history fresh too.
    const job = state.data.jobs.find(j => jobKey(j) === state.tab);
    if (job) loadFullRuns(job);
  } catch (e) {
    liveDot.className = "live-dot stale";
    lastUpdatedEl.textContent = "connection lost — retrying…";
  }
}

// Clicking the title in the topbar returns to the overview tab.
document.querySelector(".topbar__title").addEventListener("click", () => {
  if (state.tab !== "overview") {
    state.tab = "overview";
    render();
  }
});

fetchData();
setInterval(fetchData, REFRESH_MS);
