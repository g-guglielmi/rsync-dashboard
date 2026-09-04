// Settings panel — alert thresholds and notification channels.
// Values are saved server-side (STATE_DIR/settings.json) via /api/settings and
// applied immediately. Secrets are write-only: the server only tells us whether
// one is set, never its value.
(() => {
  const PW_KEY = "rw_settings_pw";
  const modal = document.getElementById("settings-modal");
  const openBtn = document.getElementById("settings-btn");
  if (!modal || !openBtn) return;

  let current = null;            // last payload from GET /api/settings
  const cleared = new Set();     // channels the user clicked "Remove" on (pending save)

  const getPw = () => { try { return sessionStorage.getItem(PW_KEY) || ""; } catch (e) { return ""; } };
  const setPw = v => { try { v ? sessionStorage.setItem(PW_KEY, v) : sessionStorage.removeItem(PW_KEY); } catch (e) { /* private mode */ } };

  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json" };
    const pw = getPw();
    if (pw) headers["X-Settings-Password"] = pw;
    const res = await fetch(path, { ...opts, headers });
    let body = {};
    try { body = await res.json(); } catch (e) { /* no body */ }
    if (res.status === 401) { const err = new Error("Password required"); err.code = 401; throw err; }
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    return body;
  }

  // ---------------------------------------------------------------- views --

  function open() { modal.hidden = false; document.body.classList.add("modal-open"); load(); }
  function close() { modal.hidden = true; document.body.classList.remove("modal-open"); cleared.clear(); }

  async function load() {
    modal.innerHTML = `<div class="modal__panel"><p class="muted">Loading settings…</p></div>`;
    try {
      current = await api("/api/settings");
      cleared.clear();
      renderForm();
    } catch (e) {
      if (e.code === 401) renderPassword(getPw() ? "Wrong password." : "");
      else renderError(e.message);
    }
  }

  function renderPassword(msg) {
    setPw("");
    modal.innerHTML = `
      <div class="modal__panel modal__panel--narrow">
        <header class="modal__head"><h2>Settings are locked</h2><button class="icon-btn" data-close aria-label="Close">✕</button></header>
        <form id="pw-form">
          <p class="muted">Enter the settings password (the <code>SETTINGS_PASSWORD</code> of the container).</p>
          <label class="field">Password<input type="password" name="pw" autofocus autocomplete="current-password"></label>
          ${msg ? `<p class="form-error">${escapeHtml(msg)}</p>` : ""}
          <footer class="modal__foot"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn btn--primary">Unlock</button></footer>
        </form>
      </div>`;
    modal.querySelector("#pw-form").addEventListener("submit", ev => {
      ev.preventDefault();
      setPw(modal.querySelector("[name=pw]").value);
      load();
    });
  }

  function renderError(msg) {
    modal.innerHTML = `
      <div class="modal__panel modal__panel--narrow">
        <header class="modal__head"><h2>Settings</h2><button class="icon-btn" data-close aria-label="Close">✕</button></header>
        <p class="form-error">${escapeHtml(msg)}</p>
        <footer class="modal__foot"><button type="button" class="btn" data-close>Close</button></footer>
      </div>`;
  }

  const secretInput = (name, isSet, label) => `
    <label class="field">${label}
      <input type="password" name="${name}" data-secret="1" autocomplete="off"
             placeholder="${isSet ? "•••• set — leave blank to keep" : "not set"}">
    </label>`;

  const textInput = (name, value, label, extra = "") => `
    <label class="field">${label}<input type="text" name="${name}" value="${escapeHtml(value ?? "")}" ${extra}></label>`;

  function channelChip(on) {
    return on ? `<span class="chip chip--on">configured</span>` : `<span class="chip">off</span>`;
  }

  function renderForm() {
    const s = current.settings;
    const tg = s.telegram, dc = s.discord, sm = s.smtp;
    const ev = new Set(s.events);
    const jobs = current.jobs || [];
    const rows = jobs.length ? jobs.map(j => `
        <tr><td>${escapeHtml(j)}</td>
            <td><input type="number" min="0" step="1" inputmode="numeric" data-job="${escapeHtml(j)}"
                       value="${s.job_intervals[j] != null ? escapeHtml(s.job_intervals[j]) : ""}" placeholder="${escapeHtml(s.overdue_hours)}"></td></tr>`).join("")
      : `<tr><td colspan="2" class="muted">No jobs found yet.</td></tr>`;

    modal.innerHTML = `
      <div class="modal__panel">
        <header class="modal__head">
          <h2>Alert settings</h2>
          <button class="icon-btn" data-close aria-label="Close">✕</button>
        </header>
        <form id="settings-form" autocomplete="off">
          <section class="card">
            <h3>When to alert</h3>
            <div class="field-grid">
              <label class="field">Overdue after (hours)
                <input type="number" name="overdue_hours" min="0" step="0.5" value="${escapeHtml(s.overdue_hours)}">
                <small>A job with no run for this long is <b>Overdue</b>. 26 = daily jobs + 2h grace.</small></label>
              <label class="field">Check every (minutes)
                <input type="number" name="check_minutes" min="1" step="1" value="${escapeHtml(s.check_minutes)}"></label>
              <label class="field">Dashboard link in messages
                <input type="text" name="dashboard_url" value="${escapeHtml(s.dashboard_url)}" placeholder="http://192.168.1.10:8686"></label>
            </div>
            <div class="checks">
              <label><input type="checkbox" name="ev" value="overdue" ${ev.has("overdue") ? "checked" : ""}> Overdue — job didn't run when expected</label>
              <label><input type="checkbox" name="ev" value="interrupted" ${ev.has("interrupted") ? "checked" : ""}> Interrupted — run never finished</label>
              <label><input type="checkbox" name="ev" value="failed" ${ev.has("failed") ? "checked" : ""}> Failed run</label>
              <label><input type="checkbox" name="ev" value="warning" ${ev.has("warning") ? "checked" : ""}> Run with warnings</label>
              <label><input type="checkbox" name="recovery" ${s.recovery ? "checked" : ""}> Send a recovery message when a problem clears</label>
            </div>
          </section>

          <section class="card">
            <h3>Per-job schedule</h3>
            <p class="muted">Hours between runs for jobs that differ from the default. Blank = default above. <b>0</b> = never mark this job overdue.</p>
            <table class="intervals"><thead><tr><th>Job</th><th>Every (hours)</th></tr></thead><tbody>${rows}</tbody></table>
          </section>

          <section class="card channel" data-channel="telegram">
            <h3>Telegram ${channelChip(tg.token_set && tg.chat_id)}</h3>
            <div class="field-grid">
              ${secretInput("telegram.token", tg.token_set, "Bot token")}
              ${textInput("telegram.chat_id", tg.chat_id, "Chat ID", 'placeholder="-1001234567890"')}
              ${textInput("telegram.thread_id", tg.thread_id, "Topic ID (optional)", 'inputmode="numeric" placeholder="forum topic id"')}
            </div>
            <div class="actions">
              <button type="button" class="btn" data-test="telegram">Send test</button>
              <button type="button" class="btn btn--danger" data-clear="telegram">Remove channel</button>
              <span class="result" data-result="telegram"></span>
            </div>
          </section>

          <section class="card channel" data-channel="discord">
            <h3>Discord ${channelChip(dc.webhook_set)}</h3>
            <div class="field-grid">
              ${secretInput("discord.webhook", dc.webhook_set, "Webhook URL")}
            </div>
            <div class="actions">
              <button type="button" class="btn" data-test="discord">Send test</button>
              <button type="button" class="btn btn--danger" data-clear="discord">Remove channel</button>
              <span class="result" data-result="discord"></span>
            </div>
          </section>

          <section class="card channel" data-channel="smtp">
            <h3>E-mail ${channelChip(sm.host && sm.to)}</h3>
            <div class="field-grid">
              ${textInput("smtp.host", sm.host, "SMTP host", 'placeholder="smtp.example.com"')}
              ${textInput("smtp.port", sm.port, "Port", 'inputmode="numeric"')}
              <label class="field">Encryption
                <select name="smtp.tls">
                  <option value="auto" ${!sm.tls ? "selected" : ""}>Auto (465 = SSL, else STARTTLS)</option>
                  <option value="starttls" ${sm.tls === "starttls" ? "selected" : ""}>STARTTLS</option>
                  <option value="ssl" ${sm.tls === "ssl" ? "selected" : ""}>SSL/TLS</option>
                  <option value="none" ${sm.tls === "none" ? "selected" : ""}>None</option>
                </select></label>
              ${textInput("smtp.user", sm.user, "Username")}
              ${secretInput("smtp.password", sm.password_set, "Password")}
              ${textInput("smtp.from", sm.from, "From address (optional)")}
              ${textInput("smtp.to", sm.to, "To (comma-separated)")}
            </div>
            <div class="actions">
              <button type="button" class="btn" data-test="smtp">Send test</button>
              <button type="button" class="btn btn--danger" data-clear="smtp">Remove channel</button>
              <span class="result" data-result="smtp"></span>
            </div>
          </section>

          <footer class="modal__foot">
            <span id="settings-status" class="muted"></span>
            <button type="button" class="btn" data-close>Close</button>
            <button type="submit" class="btn btn--primary">Save</button>
          </footer>
        </form>
      </div>`;

    const form = modal.querySelector("#settings-form");
    form.addEventListener("submit", onSave);
    form.querySelectorAll("[data-test]").forEach(b => b.addEventListener("click", () => onTest(b.dataset.test)));
    form.querySelectorAll("[data-clear]").forEach(b => b.addEventListener("click", () => onClear(b.dataset.clear)));
  }

  // ------------------------------------------------------------ collect --

  const CHANNEL_FIELDS = {
    telegram: ["token", "chat_id", "thread_id"],
    discord: ["webhook"],
    smtp: ["host", "port", "tls", "user", "password", "from", "to"],
  };

  // Secret inputs left blank mean "keep the current value" and are omitted
  // from the payload; a cleared channel sends "" for everything.
  function channelPayload(form, name) {
    const out = {};
    for (const f of CHANNEL_FIELDS[name]) {
      const el = form.querySelector(`[name="${name}.${f}"]`);
      if (!el) continue;
      if (cleared.has(name)) { out[f] = f === "port" ? 587 : (f === "tls" ? "auto" : ""); continue; }
      if (el.dataset.secret === "1" && el.value === "") continue;
      out[f] = el.value;
    }
    return out;
  }

  function collect(form) {
    const v = n => form.querySelector(`[name="${n}"]`).value;
    return {
      overdue_hours: v("overdue_hours"),
      check_minutes: v("check_minutes"),
      dashboard_url: v("dashboard_url"),
      recovery: form.querySelector("[name=recovery]").checked,
      events: [...form.querySelectorAll("[name=ev]:checked")].map(c => c.value),
      job_intervals: Object.fromEntries([...form.querySelectorAll("[data-job]")].map(i => [i.dataset.job, i.value])),
      telegram: channelPayload(form, "telegram"),
      discord: channelPayload(form, "discord"),
      smtp: channelPayload(form, "smtp"),
    };
  }

  // ------------------------------------------------------------ actions --

  function setStatus(msg, isError = false) {
    const el = modal.querySelector("#settings-status");
    if (el) { el.textContent = msg; el.className = isError ? "form-error" : "muted"; }
  }

  async function onSave(ev) {
    ev.preventDefault();
    const form = ev.target;
    setStatus("Saving…");
    try {
      current = await api("/api/settings", { method: "PUT", body: JSON.stringify(collect(form)) });
      cleared.clear();
      renderForm();
      setStatus("Saved ✓ — applied immediately.");
      if (typeof fetchData === "function") fetchData();   // overdue badges may change
    } catch (e) {
      if (e.code === 401) return renderPassword("Wrong password.");
      setStatus(e.message, true);
    }
  }

  async function onTest(channel) {
    const form = modal.querySelector("#settings-form");
    const out = form.querySelector(`[data-result="${channel}"]`);
    out.textContent = "Sending…"; out.className = "result";
    try {
      const payload = { channel, settings: { [channel]: channelPayload(form, channel), dashboard_url: form.querySelector("[name=dashboard_url]").value } };
      const res = await api("/api/alerts/test", { method: "POST", body: JSON.stringify(payload) });
      const r = res.results[channel];
      out.textContent = r === "ok" ? "Delivered ✓" : r;
      out.className = r === "ok" ? "result result--ok" : "result result--err";
    } catch (e) {
      if (e.code === 401) return renderPassword("Wrong password.");
      out.textContent = e.message; out.className = "result result--err";
    }
  }

  function onClear(channel) {
    cleared.add(channel);
    const sec = modal.querySelector(`.channel[data-channel="${channel}"]`);
    sec.querySelectorAll("input").forEach(i => { i.value = ""; if (i.dataset.secret) i.placeholder = "will be removed on save"; });
    sec.querySelector("h3 .chip").outerHTML = `<span class="chip chip--off">removed on save</span>`;
    setStatus(`${channel === "smtp" ? "E-mail" : channel[0].toUpperCase() + channel.slice(1)} channel will be removed when you save.`);
  }

  // ------------------------------------------------------------- wiring --

  openBtn.addEventListener("click", open);
  modal.addEventListener("click", ev => {
    if (ev.target === modal || ev.target.closest("[data-close]")) close();
  });
  document.addEventListener("keydown", ev => { if (ev.key === "Escape" && !modal.hidden) close(); });
})();
