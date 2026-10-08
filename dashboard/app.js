// Dot Lab dashboard — plain JavaScript, no build step.
// Polls the API every few seconds and renders tasks, approvals, monitors and notifications.
"use strict";

const POLL_MS = 3000;
const state = { openTaskId: null, statusFilter: "" };

// ---------------------------------------------------------------- helpers --
const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function apiKey() {
  try { return localStorage.getItem("dotlab.apiKey") || ""; } catch { return ""; }
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const key = apiKey();
  if (key) headers["X-API-Key"] = key;
  const res = await fetch(`/api${path}`, { ...options, headers });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((d) => d.msg).join("; ")
      : body.detail || res.statusText;
    throw new Error(detail);
  }
  return body;
}

function showError(message) {
  const banner = $("error-banner");
  if (!message) { banner.hidden = true; return; }
  banner.textContent = message;
  banner.hidden = false;
}

const status = (s) => `<span class="status ${esc(s)}">${esc(s)}</span>`;

// ------------------------------------------------------------- rendering --
function renderOverview(o) {
  $("ov-active").textContent = o.active_tasks;
  $("ov-completed").textContent = o.completed_tasks;
  $("ov-approvals").textContent = o.waiting_approvals;
  $("ov-failed").textContent = o.failed_tasks;
  $("ov-monitors").textContent = o.monitoring_jobs;
}

function renderTasks(tasks) {
  const rows = $("task-rows");
  if (!tasks.length) {
    rows.innerHTML = `<tr><td colspan="7" class="muted">No tasks yet — start one above.</td></tr>`;
    return;
  }
  rows.innerHTML = tasks.map((t) => {
    const pct = t.progress_total ? Math.round((100 * t.progress_done) / t.progress_total) : 0;
    return `<tr data-task="${esc(t.id)}">
      <td class="goal">${esc(t.goal)}${t.monitor_id ? ' <span class="tag">monitor</span>' : ""}</td>
      <td>${status(t.status)}</td>
      <td>${esc(t.current_step || "–")}</td>
      <td class="nowrap">${t.progress_done}/${t.progress_total}<div class="progress"><span style="width:${pct}%"></span></div></td>
      <td class="nowrap">${fmtTime(t.created_at)}</td>
      <td class="nowrap">${fmtTime(t.started_at)}</td>
      <td class="nowrap">${fmtTime(t.updated_at)}</td>
    </tr>`;
  }).join("");
}

function approvalCard(a) {
  return `<div class="approval-item">
    <div><strong>Action:</strong> ${esc(a.action)}</div>
    <div><strong>Reason:</strong> ${esc(a.reason)}</div>
    <div><strong>Agent explanation:</strong> ${esc(a.explanation)}</div>
    <details><summary class="muted">Exact tool input</summary><pre>${esc(JSON.stringify(a.input, null, 2))}</pre></details>
    <div class="buttons">
      <button class="approve" data-approve="${esc(a.id)}">Approve</button>
      <button class="reject" data-reject="${esc(a.id)}">Reject</button>
    </div>
  </div>`;
}

function renderApprovals(approvals) {
  const section = $("approvals-section");
  section.hidden = approvals.length === 0;
  $("approvals-list").innerHTML = approvals.map(approvalCard).join("");
}

function renderMonitors(monitors) {
  const el = $("monitor-list");
  if (!monitors.length) { el.innerHTML = `<span class="muted">No monitors yet.</span>`; return; }
  el.innerHTML = monitors.map((m) => `<div class="list-item">
      <div class="row"><strong>${esc(m.name)}</strong>
        <span class="status ${m.enabled ? "COMPLETED" : ""}">${m.enabled ? "ENABLED" : "DISABLED"}</span></div>
      <div class="muted">${esc(m.goal)}</div>
      <div class="muted">Every ${m.interval_minutes} min · last run ${fmtTime(m.last_run_at)} · next ${fmtTime(m.next_run_at)}</div>
      ${m.last_change_summary ? `<div>Last check: ${esc(m.last_change_summary)}</div>` : ""}
      <div class="row">
        <button class="small" data-monitor-run="${esc(m.id)}">Run now</button>
        <button class="small ghost" data-monitor-toggle="${esc(m.id)}" data-enabled="${m.enabled}">${m.enabled ? "Disable" : "Enable"}</button>
        <button class="small ghost" data-monitor-delete="${esc(m.id)}">Delete</button>
        ${m.last_task_id ? `<button class="small ghost" data-task-open="${esc(m.last_task_id)}">Last task</button>` : ""}
      </div>
    </div>`).join("");
}

function renderNotifications(items) {
  const el = $("notification-list");
  if (!items.length) { el.innerHTML = `<span class="muted">No notifications.</span>`; return; }
  el.innerHTML = items.slice(0, 20).map((n) => `<div class="list-item">
      <div class="row"><strong>${esc(n.title)}</strong>${n.read ? "" : '<span class="tag">new</span>'}</div>
      <div>${esc(n.message)}</div>
      <div class="row muted">${fmtTime(n.created_at)}
        ${n.task_id ? `<button class="small ghost" data-task-open="${esc(n.task_id)}">Open task</button>` : ""}
        ${n.read ? "" : `<button class="small ghost" data-notification-read="${n.id}">Mark read</button>`}
      </div>
    </div>`).join("");
}

// --------------------------------------------------------- task drawer --
function renderPlan(t) {
  if (!t.plan) return `<p class="muted">No plan yet.</p>`;
  const steps = t.plan.steps.map((s) => `<li class="${s.status === "RUNNING" || s.status === "WAITING_APPROVAL" ? "current" : ""}">
      <div class="step-head">${status(s.status)} <strong>${esc(s.id)}</strong>
        <span class="step-tool">${esc(s.tool || "reasoning")}${s.requires_approval ? " · needs approval" : ""}${s.attempts > 1 ? ` · ${s.attempts} attempts` : ""}</span></div>
      <div>${esc(s.description)}</div>
      ${s.result ? `<details><summary class="muted">Output</summary><pre>${esc(s.result)}</pre></details>` : ""}
      ${s.error && s.status !== "COMPLETED" ? `<div class="muted">${esc(s.error)}</div>` : ""}
    </li>`).join("");
  return `${t.plan.summary ? `<p>${esc(t.plan.summary)}</p>` : ""}<ol class="steps">${steps}</ol>`;
}

function renderDetail(t) {
  $("d-status").className = `status ${t.status}`;
  $("d-status").textContent = t.status;
  $("d-goal").textContent = t.goal;
  $("d-meta").textContent = `Created ${fmtTime(t.created_at)} · updated ${fmtTime(t.updated_at)} · ${t.iterations} iterations · id ${t.id}`;

  const actions = [];
  if (["PENDING", "PLANNING", "RUNNING"].includes(t.status)) actions.push(`<button class="ghost" data-act="pause">Pause</button>`);
  if (t.status === "PAUSED") actions.push(`<button data-act="resume">Resume</button>`);
  if (!["COMPLETED", "FAILED", "CANCELLED"].includes(t.status)) actions.push(`<button class="reject" data-act="cancel">Cancel</button>`);
  $("d-actions").innerHTML = actions.join("");

  const pending = t.approvals.filter((a) => a.status === "PENDING");
  const html = [];
  if (pending.length) html.push(`<div class="panel approvals"><h2>Agent is waiting for your approval</h2>${pending.map(approvalCard).join("")}</div>`);
  if (t.error) html.push(`<div class="banner error">${esc(t.error)}</div>`);
  if (t.result) html.push(`<h3>Final result</h3><pre class="result">${esc(t.result)}</pre>`);
  html.push(`<h3>Current step</h3><p>${esc(t.current_step || "–")}</p>`);
  html.push(`<h3>Plan</h3>${renderPlan(t)}`);

  html.push(`<details class="block"><summary>Execution history (${t.events.length})</summary><ul class="timeline">${
    t.events.map((e) => `<li><time>${fmtTime(e.created_at)}</time><span class="ev">${esc(e.event)}</span><span>${esc(e.message)}</span></li>`).join("")
  }</ul></details>`);

  html.push(`<details class="block"><summary>Tool calls (${t.tool_calls.length})</summary>${
    t.tool_calls.map((c) => `<div class="list-item"><div class="row">${status(c.status)} <strong>${esc(c.tool)}</strong>
        <span class="muted">${esc(c.step_id)} · attempt ${c.attempt} · ${c.duration_ms} ms</span></div>
        <pre>${esc(JSON.stringify(c.input, null, 2))}</pre>
        ${c.error ? `<div class="muted">${esc(c.error)}</div>` : ""}
        ${c.output ? `<details><summary class="muted">Output</summary><pre>${esc(c.output)}</pre></details>` : ""}</div>`).join("") || '<p class="muted">None.</p>'
  }</details>`);

  html.push(`<details class="block"><summary>Approvals (${t.approvals.length})</summary>${
    t.approvals.map((a) => `<div class="list-item"><div class="row">${status(a.status)} ${esc(a.action)}</div>
      <div class="muted">${esc(a.reason)}${a.decision_note ? ` · note: ${esc(a.decision_note)}` : ""}</div></div>`).join("") || '<p class="muted">None.</p>'
  }</details>`);

  html.push(`<details class="block"><summary>Memory (${t.memory.length})</summary>${
    t.memory.map((m) => `<div class="list-item"><span class="tag">${esc(m.kind)}</span><pre>${esc(m.content)}</pre></div>`).join("") || '<p class="muted">None.</p>'
  }</details>`);

  // Preserve which <details> blocks are open across refreshes.
  const content = $("d-content");
  const open = [...content.querySelectorAll("details.block")].map((d) => d.open);
  content.innerHTML = html.join("");
  content.querySelectorAll("details.block").forEach((d, i) => { if (open[i]) d.open = true; });
}

async function openTask(id) {
  state.openTaskId = id;
  $("d-content").innerHTML = `<p class="muted">Loading…</p>`;
  $("drawer").hidden = false;
  await refreshDetail();
}

function closeDrawer() {
  state.openTaskId = null;
  $("drawer").hidden = true;
}

async function refreshDetail() {
  if (!state.openTaskId) return;
  try { renderDetail(await api(`/tasks/${encodeURIComponent(state.openTaskId)}`)); }
  catch (err) { $("d-content").innerHTML = `<div class="banner error">${esc(err.message)}</div>`; }
}

// --------------------------------------------------------------- refresh --
async function refresh() {
  try {
    const qs = state.statusFilter ? `?status=${encodeURIComponent(state.statusFilter)}` : "";
    const [overview, tasks, approvals, monitors, notifications] = await Promise.all([
      api("/overview"), api(`/tasks${qs}`), api("/approvals?status=PENDING"),
      api("/monitors"), api("/notifications"),
    ]);
    renderOverview(overview);
    renderTasks(tasks);
    renderApprovals(approvals);
    renderMonitors(monitors);
    renderNotifications(notifications);
    showError(null);
  } catch (err) {
    showError(`Could not reach the API: ${err.message}`);
  }
  await refreshDetail();
}

async function refreshHealth() {
  const pill = $("llm-status");
  try {
    const res = await fetch("/api/health");
    const h = await res.json();
    pill.textContent = h.llm_configured ? "AI configured" : "AI not configured";
    pill.className = `pill ${h.llm_configured ? "ok" : "bad"}`;
    pill.title = h.llm_configured ? "" : "Set OPENAI_API_KEY and OPENAI_MODEL in .env and restart.";
  } catch {
    pill.textContent = "offline";
    pill.className = "pill bad";
  }
}

// ---------------------------------------------------------------- events --
async function act(fn) {
  try { await fn(); showError(null); } catch (err) { showError(err.message); }
  await refresh();
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-approve],[data-reject],[data-task],[data-task-open],[data-close],[data-act],[data-monitor-run],[data-monitor-toggle],[data-monitor-delete],[data-notification-read]");
  if (!el) return;
  const d = el.dataset;
  if (d.approve) act(() => api(`/approvals/${d.approve}/approve`, { method: "POST", body: "{}" }));
  else if (d.reject) {
    const note = prompt("Reject this action? Optional note for the agent:", "");
    if (note === null) return; // dialog cancelled
    act(() => api(`/approvals/${d.reject}/reject`, { method: "POST", body: JSON.stringify({ note: note || null }) }));
  }
  else if (d.task) openTask(d.task);
  else if (d.taskOpen) openTask(d.taskOpen);
  else if (d.close !== undefined) closeDrawer();
  else if (d.act) act(() => api(`/tasks/${state.openTaskId}/${d.act}`, { method: "POST" }));
  else if (d.monitorRun) act(() => api(`/monitors/${d.monitorRun}/run`, { method: "POST" }));
  else if (d.monitorToggle) act(() => api(`/monitors/${d.monitorToggle}/${d.enabled === "true" ? "disable" : "enable"}`, { method: "POST" }));
  else if (d.monitorDelete) { if (confirm("Delete this monitor?")) act(() => api(`/monitors/${d.monitorDelete}`, { method: "DELETE" })); }
  else if (d.notificationRead) act(() => api(`/notifications/${d.notificationRead}/read`, { method: "POST" }));
});

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

$("task-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const goal = $("goal").value.trim();
  if (!goal) return;
  act(async () => {
    const created = await api("/tasks", { method: "POST", body: JSON.stringify({ goal }) });
    $("goal").value = "";
    openTask(created.task_id);
  });
});

$("monitor-form").addEventListener("submit", (e) => {
  e.preventDefault();
  act(async () => {
    await api("/monitors", {
      method: "POST",
      body: JSON.stringify({
        name: $("m-name").value.trim(),
        goal: $("m-goal").value.trim(),
        interval_minutes: Number($("m-interval").value),
      }),
    });
    $("m-name").value = ""; $("m-goal").value = "";
  });
});

$("status-filter").addEventListener("change", (e) => { state.statusFilter = e.target.value; refresh(); });

$("api-key").value = apiKey();
$("api-key").addEventListener("change", (e) => {
  try { localStorage.setItem("dotlab.apiKey", e.target.value.trim()); } catch { /* storage unavailable */ }
  refresh();
});

refreshHealth();
refresh();
setInterval(refresh, POLL_MS);
setInterval(refreshHealth, POLL_MS * 10);
