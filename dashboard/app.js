/* Dot Lab dashboard. Plain JavaScript, no build step.
 *
 * Structure:
 *   helpers (api, escaping, formatting) -> views (one object per section)
 *   -> task drawer -> modal -> event wiring -> polling loop.
 * Every value that comes from the server is passed through esc() before it is
 * put into HTML.
 */
"use strict";

const POLL_MS = 4000;
const ACTIVE = ["PENDING", "PLANNING", "RUNNING", "REVIEWING"];
const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];
const STATUS_COLORS = {
  PENDING: "#7aa2ff", PLANNING: "#7aa2ff", RUNNING: "#4f7cff", REVIEWING: "#8b5cf6",
  WAITING_INPUT: "#fbbf24", WAITING_APPROVAL: "#f59e0b", PAUSED: "#d4a72c",
  COMPLETED: "#34d399", FAILED: "#f87171", CANCELLED: "#67709a",
};

const state = {
  view: "overview",
  openTaskId: null,
  convId: null,
  chatSignature: "",
  tasksStatus: "",
  history: { q: "", status: "", offset: 0, pageSize: 25 },
  activity: { kind: "all", items: [], hasMore: false },
  memory: { category: null, q: "", kind: "", offset: 0, pageSize: 30, items: [] },
  overview: null,
};

// ------------------------------------------------------------------ helpers --
const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storageSet(key, value) {
  try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* private mode */ }
}
const apiKey = () => storageGet("dotlab-api-key") || "";

function authHeaders(extra = {}) {
  const headers = { ...extra };
  const key = apiKey();
  if (key) headers["X-API-Key"] = key;
  return headers;
}

async function api(path, options = {}) {
  const headers = authHeaders({ "Content-Type": "application/json", ...(options.headers || {}) });
  const res = await fetch(`/api${path}`, { ...options, headers });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401) throw new Error("This server needs an API key. Enter it in Settings.");
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new Error(detail || res.statusText || `Request failed (${res.status})`);
  }
  return body;
}

/** Download a file from the API (fetch, so the X-API-Key header is sent). */
async function downloadFile(path, filename) {
  const res = await fetch(`/api${path}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`Download failed (${res.status})`);
  const url = URL.createObjectURL(await res.blob());
  const link = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function fetchText(path) {
  const res = await fetch(`/api${path}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`Could not load (${res.status})`);
  return res.text();
}

function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtRel(iso) {
  if (!iso) return "–";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return fmtTime(iso);
}

function fmtDuration(start, end) {
  if (!start || !end) return "–";
  const s = Math.max(0, Math.round((new Date(end) - new Date(start)) / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const fmtNum = (n) => Number(n || 0).toLocaleString();
const status = (s) => `<span class="status ${esc(s)}">${esc(String(s).replace(/_/g, " "))}</span>`;
const empty = (text) => `<div class="empty">${esc(text)}</div>`;
const debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function showError(message) {
  const el = $("error-banner");
  if (!message) { el.hidden = true; return; }
  el.innerHTML = `<span>${esc(message)}</span><span class="spacer"></span><button class="link" data-action="dismiss-error">Dismiss</button>`;
  el.hidden = false;
}

let toastTimer;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

/** Run an action, show success/failure, refresh the current view. */
async function act(fn, success) {
  try {
    await fn();
    showError(null);
    if (success) toast(success);
  } catch (err) {
    showError(err.message);
  }
  await refreshAll();
}

function eventClass(name) {
  if (/FAILED|REJECTED/.test(name)) return "bad";
  if (/APPROVAL_REQUESTED|CLARIFICATION_REQUESTED|PAUSED/.test(name)) return "warn";
  if (/COMPLETED|GRANTED|ANSWERED/.test(name)) return "good";
  return "";
}

function taskLink(id, text) {
  return `<button class="link" data-action="open-task" data-id="${esc(id)}">${esc(text)}</button>`;
}

// ------------------------------------------------------------------ views --
const views = {
  overview: {
    title: "Overview",
    subtitle: "What the agent is doing and what needs your attention.",
    async load() {
      const [ov, tasks, activity, approvals, waiting, notes] = await Promise.all([
        api("/overview"), api("/tasks?limit=6"), api("/activity?limit=12"),
        api("/approvals?status=PENDING"), api("/tasks?status=WAITING_INPUT&limit=20"), api("/notifications"),
      ]);
      state.overview = ov;
      updateBadges(ov);
      renderKpis(ov);
      renderAttention(approvals, waiting);
      renderStatusChart(ov.status_counts);
      $("overview-tasks").innerHTML = tasks.length
        ? tasks.map((t) => `<div class="item"><div class="row">${status(t.status)}
            <span class="spacer"></span><span class="faint small">${esc(fmtRel(t.updated_at))}</span></div>
            <div>${taskLink(t.id, t.goal)}</div></div>`).join("")
        : empty("No tasks yet. Start one with “New task” or in the AI Workspace.");
      $("overview-activity").innerHTML = activity.length ? activity.map(activityItem).join("") : `<li>${empty("No activity yet.")}</li>`;
      renderNotifications(notes);
    },
  },

  workspace: {
    title: "AI Workspace",
    subtitle: "Chat with Dot Lab. Each message starts a task; follow-ups use the conversation as context.",
    async load() {
      const convs = await api("/conversations");
      if (state.convId && !convs.some((c) => c.id === state.convId)) state.convId = null;
      if (!state.convId && convs.length) state.convId = convs[0].id;
      $("conv-items").innerHTML = convs.length
        ? convs.map((c) => `<button class="conv ${c.id === state.convId ? "active" : ""}" data-action="select-conversation" data-id="${esc(c.id)}">
            ${esc(c.title)}<small>${c.message_count} messages · ${esc(fmtRel(c.updated_at))}</small></button>`).join("")
        : empty("No conversations yet.");
      await loadChat();
    },
  },

  tasks: {
    title: "Tasks",
    subtitle: "Start tasks, follow their progress and manage scheduled monitors.",
    async load() {
      const qs = state.tasksStatus ? `?status=${encodeURIComponent(state.tasksStatus)}&limit=100` : "?limit=100";
      const [tasks, monitors] = await Promise.all([api(`/tasks${qs}`), api("/monitors")]);
      $("task-rows").innerHTML = tasks.length ? tasks.map((t) => {
        const pct = t.progress_total ? Math.round((t.progress_done / t.progress_total) * 100) : 0;
        return `<tr class="clickable" data-action="open-task" data-id="${esc(t.id)}">
          <td class="goal"><span>${esc(t.goal)}</span></td><td>${status(t.status)}</td>
          <td><div class="progress" title="${t.progress_done}/${t.progress_total} steps"><span style="width:${pct}%"></span></div>
            <span class="faint small">${t.progress_done}/${t.progress_total}</span></td>
          <td class="muted">${esc(t.current_step || "–")}</td><td class="faint small">${esc(fmtRel(t.updated_at))}</td></tr>`;
      }).join("") : `<tr><td colspan="5">${empty("No tasks match this filter.")}</td></tr>`;
      renderMonitors(monitors);
    },
  },

  history: {
    title: "Execution History",
    subtitle: "Finished tasks: search, review and download reports.",
    async load(append = false) {
      const h = state.history;
      if (!append) h.offset = 0;
      const params = new URLSearchParams({ finished: "true", limit: String(h.pageSize + 1), offset: String(h.offset) });
      if (h.q) params.set("q", h.q);
      if (h.status) params.set("status", h.status);
      const rows = await api(`/tasks?${params}`);
      const page = rows.slice(0, h.pageSize);
      const html = page.map((t) => `<tr class="clickable" data-action="open-task" data-id="${esc(t.id)}">
          <td class="goal"><span>${esc(t.goal)}</span></td><td>${status(t.status)}</td>
          <td class="faint">${t.progress_done}/${t.progress_total}</td>
          <td class="faint small">${esc(fmtTime(t.completed_at))}</td>
          <td class="faint small">${esc(fmtDuration(t.started_at, t.completed_at))}</td>
          <td><button class="ghost small" data-action="task-report" data-id="${esc(t.id)}">Report</button></td></tr>`).join("");
      const body = $("history-rows");
      if (append) body.insertAdjacentHTML("beforeend", html);
      else body.innerHTML = html || `<tr><td colspan="6">${empty(h.q || h.status ? "No finished tasks match." : "No finished tasks yet.")}</td></tr>`;
      $("history-more").hidden = rows.length <= h.pageSize;
      $("history-count").textContent = `${body.querySelectorAll("tr.clickable").length} shown`;
    },
    refreshable: false,
  },

  activity: {
    title: "Agent Activity",
    subtitle: "Everything the agent does, across all tasks, newest first.",
    async load() {
      const items = await api(`/activity?kind=${state.activity.kind}&limit=50`);
      state.activity.items = items;
      state.activity.hasMore = items.length === 50;
      renderActivity();
    },
    async refresh() {
      if (!$("activity-live").checked) return;
      const a = state.activity;
      const latest = await api(`/activity?kind=${a.kind}&limit=50`);
      const maxId = a.items.length ? a.items[0].id : 0;
      const fresh = latest.filter((e) => e.id > maxId);
      if (fresh.length) { a.items = [...fresh, ...a.items]; renderActivity(); }
    },
  },

  memory: {
    title: "Memory",
    subtitle: "Long-term knowledge the agent uses when planning. Add notes, recategorise or remove entries.",
    async load(append = false) {
      const m = state.memory;
      if (!append) m.offset = 0;
      const [categories] = await Promise.all([api("/memory/categories")]);
      renderCategories(categories);
      let items;
      if (m.q.length >= 2) {
        const params = new URLSearchParams({ q: m.q, limit: "50" });
        if (m.category) params.set("category", m.category);
        items = (await api(`/memory/search?${params}`)).filter((r) => !m.kind || r.kind === m.kind);
        $("memory-more").hidden = true;
      } else {
        const params = new URLSearchParams({ limit: String(m.pageSize + 1), offset: String(m.offset) });
        if (m.category) params.set("category", m.category);
        if (m.kind) params.set("kind", m.kind);
        const rows = await api(`/memory?${params}`);
        items = rows.slice(0, m.pageSize);
        $("memory-more").hidden = rows.length <= m.pageSize;
      }
      m.items = append ? [...m.items, ...items] : items;
      renderMemory();
    },
    refreshable: false,
  },

  reports: {
    title: "Reports",
    subtitle: "Reports saved by the agent, and downloadable summaries of finished tasks.",
    async load() {
      const [files, tasks] = await Promise.all([api("/reports"), api("/tasks?finished=true&limit=20")]);
      $("report-files").innerHTML = files.length ? files.map((f) => `<div class="item"><div class="row">
          <span class="title">${esc(f.name)}</span><span class="spacer"></span>
          <button class="ghost small" data-action="view-report" data-name="${esc(f.name)}">View</button>
          <button class="small" data-action="download-report" data-name="${esc(f.name)}">Download</button></div>
          <div class="faint small">${esc(fmtBytes(f.bytes))} · ${esc(fmtTime(f.modified_at))}</div></div>`).join("")
        : empty("No saved reports yet. Ask the agent to “save a report”.");
      $("report-tasks").innerHTML = tasks.length ? tasks.map((t) => `<div class="item"><div class="row">
          ${status(t.status)}<span class="spacer"></span><span class="faint small">${esc(fmtTime(t.completed_at))}</span>
          <button class="small" data-action="task-report" data-id="${esc(t.id)}">Download</button></div>
          <div>${taskLink(t.id, t.goal)}</div></div>`).join("")
        : empty("No finished tasks yet.");
    },
    refreshable: false,
  },

  settings: {
    title: "Settings",
    subtitle: "Connection, configuration and tools. Values are read from .env.",
    async load() {
      $("api-key").placeholder = apiKey() ? "Key saved in this browser" : "X-API-Key";
      const [cfg, tools] = await Promise.all([api("/settings"), api("/tools")]);
      const check = cfg.model_check;
      $("settings-connection").innerHTML = kv([
        ["Status", cfg.llm_configured ? `<span class="pill ok">Configured</span>` : `<span class="pill bad">Not configured</span>`],
        ["Model", cfg.openai_model ? `<code>${esc(cfg.openai_model)}</code>` : "–"],
        ["Model check", `${status(check.status)} <span class="muted small">${esc(check.detail)}</span>`],
        ["API key protection", cfg.api_key_protection ? "On (DOT_LAB_API_KEY)" : "Off"],
        ["Version", esc(cfg.version)],
      ]);
      const limits = Object.entries(cfg.limits).map(([k, v]) => [`<code>${esc(k)}</code>`, esc(fmtNum(v))]);
      $("settings-config").innerHTML = kv([
        ["Tool permission level", `<code>${esc(cfg.tool_permission_level)}</code>`],
        ["Always needs approval", cfg.approval_required_tools.length ? esc(cfg.approval_required_tools.join(", ")) : "–"],
        ["On rejection", esc(cfg.approval_rejection_policy)],
        ["Token budget per task", cfg.max_task_tokens ? esc(fmtNum(cfg.max_task_tokens)) : "No limit"],
        ["Cost budget per task", cfg.max_task_cost_usd != null ? `$${esc(cfg.max_task_cost_usd)}` : "–"],
        ["Token prices set", cfg.prices_configured ? "Yes (costs are estimates)" : "No"],
        ["Web search", esc(cfg.search_provider)],
        ["Browser", esc(cfg.browser_backend)],
        ["Monitor scheduler", cfg.monitor_scheduler_enabled ? "On" : "Off"],
        ...limits,
      ]);
      $("settings-tools").innerHTML = tools.map((t) => {
        const usable = t.available && t.permitted;
        const why = !t.permitted ? "Above TOOL_PERMISSION_LEVEL" : t.unavailable_reason || "";
        return `<tr><td><strong>${esc(t.name)}</strong><div class="faint small">${esc(t.description)}</div>
            ${usable ? "" : `<div class="small" style="color:var(--warn);margin-top:4px">${esc(why)}</div>`}</td>
          <td><span class="tag">${esc(t.permission)}</span></td><td class="faint">${t.timeout_seconds ? `${esc(t.timeout_seconds)} s` : "global"}</td>
          <td>${status(usable ? "available" : "unavailable")}</td></tr>`;
      }).join("");
    },
    refreshable: false,
  },
};

const kv = (pairs) => pairs.map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join("");

// ------------------------------------------------------------------ overview renderers --
function updateBadges(ov) {
  const attention = ov.waiting_approvals + ov.waiting_input;
  const a = $("count-attention");
  a.hidden = attention === 0;
  a.textContent = attention;
  const active = $("count-active");
  active.hidden = ov.active_tasks === 0;
  active.textContent = ov.active_tasks;
  active.style.background = "var(--blue)";
  active.style.color = "#fff";
}

function renderKpis(ov) {
  const card = (label, value, cls = "") => `<div class="kpi ${cls}"><span class="label">${esc(label)}</span><span class="value">${esc(value)}</span></div>`;
  $("kpis").innerHTML = [
    card("Active tasks", fmtNum(ov.active_tasks)),
    card("Needs approval", fmtNum(ov.waiting_approvals), ov.waiting_approvals ? "warn" : ""),
    card("Waiting for answer", fmtNum(ov.waiting_input), ov.waiting_input ? "warn" : ""),
    card("Completed", fmtNum(ov.completed_tasks), "good"),
    card("Failed", fmtNum(ov.failed_tasks), ov.failed_tasks ? "bad" : ""),
    card("Monitors", fmtNum(ov.monitoring_jobs), "purple"),
    card("LLM calls", fmtNum(ov.llm_calls_total), "purple"),
    card("Tokens used", fmtNum(ov.tokens_used_total), "purple"),
  ].join("");
}

function approvalCard(a) {
  return `<div class="approval-card">
    <div class="title">${esc(a.action)}</div>
    <div class="muted small">${esc(a.reason)}</div>
    ${a.explanation ? `<div class="small" style="margin-top:4px">Agent: ${esc(a.explanation)}</div>` : ""}
    <details style="margin-top:6px"><summary class="faint small">Exact input that will run</summary><pre>${esc(JSON.stringify(a.input, null, 2))}</pre></details>
    <div class="row"><button class="success small" data-action="approve" data-id="${esc(a.id)}">Approve</button>
      <button class="danger small" data-action="reject" data-id="${esc(a.id)}">Reject</button>
      <span class="spacer"></span>${taskLink(a.task_id, "Open task")}</div></div>`;
}

function renderAttention(approvals, waiting) {
  if (!approvals.length && !waiting.length) { $("attention").innerHTML = ""; return; }
  $("attention").innerHTML = `<div class="attention"><h2>Needs your attention</h2>
    ${approvals.map(approvalCard).join("")}
    ${waiting.map((t) => `<div class="approval-card"><div class="row">${status("WAITING_INPUT")}<span class="title">${esc(t.goal)}</span></div>
      <div class="row"><button class="small" data-action="open-task" data-id="${esc(t.id)}">Answer the agent's question</button></div></div>`).join("")}
  </div>`;
}

function renderStatusChart(counts) {
  const entries = Object.entries(counts).filter(([, n]) => n > 0);
  const total = entries.reduce((s, [, n]) => s + n, 0);
  if (!total) { $("status-chart").innerHTML = empty("No tasks yet."); return; }
  $("status-chart").innerHTML = `<div class="status-bar" role="img" aria-label="Tasks by status">${
    entries.map(([s, n]) => `<span style="width:${(n / total) * 100}%;background:${STATUS_COLORS[s] || "#67709a"}" title="${esc(s)}: ${n}"></span>`).join("")
  }</div><div class="legend">${
    entries.map(([s, n]) => `<span><i style="background:${STATUS_COLORS[s] || "#67709a"}"></i>${esc(s.replace(/_/g, " "))} ${n}</span>`).join("")
  }</div>`;
}

function activityItem(e) {
  return `<li><time title="${esc(e.created_at)}">${esc(fmtRel(e.created_at))}</time><div>
    <span class="ev ${eventClass(e.event)}">${esc(e.event)}</span>${esc(e.message)}
    <button class="link goal-link" data-action="open-task" data-id="${esc(e.task_id)}">${esc(e.task_goal)}</button></div></li>`;
}

function renderActivity() {
  const a = state.activity;
  $("activity-list").innerHTML = a.items.length ? a.items.map(activityItem).join("") : `<li>${empty("No activity for this filter yet.")}</li>`;
  $("activity-more").hidden = !a.hasMore;
}

function renderNotifications(items) {
  $("notifications").innerHTML = items.length ? items.slice(0, 15).map((n) => `<div class="item">
      <div class="row"><span class="title">${esc(n.title)}</span>${n.read ? "" : '<span class="tag new">new</span>'}
        <span class="spacer"></span><span class="faint small">${esc(fmtRel(n.created_at))}</span></div>
      <div class="muted">${esc(n.message)}</div>
      <div class="row">${n.task_id ? taskLink(n.task_id, "Open task") : ""}
        ${n.read ? "" : `<button class="ghost small" data-action="mark-read" data-id="${n.id}">Mark read</button>`}</div></div>`).join("")
    : empty("No notifications.");
}

function renderMonitors(monitors) {
  $("monitor-list").innerHTML = monitors.length ? monitors.map((m) => `<div class="item">
      <div class="row"><span class="title">${esc(m.name)}</span><span class="spacer"></span><span class="status ${m.enabled ? "COMPLETED" : "CANCELLED"}">${m.enabled ? "ENABLED" : "DISABLED"}</span></div>
      <div class="muted">${esc(m.goal)}</div>
      <div class="faint small">Every ${esc(m.interval_minutes)} min · last run ${esc(fmtRel(m.last_run_at))} · next ${esc(fmtTime(m.next_run_at))}</div>
      ${m.last_change_summary ? `<div class="small">Last check: ${esc(m.last_change_summary)}</div>` : ""}
      <div class="row"><button class="small" data-action="monitor-run" data-id="${esc(m.id)}">Run now</button>
        <button class="ghost small" data-action="monitor-toggle" data-id="${esc(m.id)}" data-enabled="${m.enabled}">${m.enabled ? "Disable" : "Enable"}</button>
        <button class="danger small" data-action="monitor-delete" data-id="${esc(m.id)}">Delete</button>
        ${m.last_task_id ? taskLink(m.last_task_id, "Last task") : ""}</div></div>`).join("")
    : empty("No monitors yet.");
}

// ------------------------------------------------------------------ memory renderers --
function renderCategories(categories) {
  const m = state.memory;
  const total = categories.reduce((s, c) => s + c.count, 0);
  $("memory-categories").innerHTML = [
    `<button class="chip ${m.category ? "" : "active"}" data-action="memory-category" data-category="">All<span class="n">${total}</span></button>`,
    ...categories.map((c) => `<button class="chip ${m.category === c.category ? "active" : ""}" data-action="memory-category" data-category="${esc(c.category)}">${esc(c.category)}<span class="n">${c.count}</span></button>`),
  ].join("");
  $("category-options").innerHTML = categories.map((c) => `<option value="${esc(c.category)}">`).join("");
}

function renderMemory() {
  const items = state.memory.items;
  $("memory-count").textContent = `${items.length} shown`;
  $("memory-list").innerHTML = items.length ? items.map((r) => `<div class="memory-card">
      <div class="head"><span class="tag">${esc(r.kind)}</span><span class="tag new">${esc(r.category)}</span>
        <span class="faint small">${esc(fmtTime(r.created_at))}${r.updated_at ? ` · edited ${esc(fmtRel(r.updated_at))}` : ""}</span>
        <span class="spacer"></span>
        ${r.task_id ? taskLink(r.task_id, "Task") : ""}
        <button class="ghost small" data-action="memory-edit" data-id="${r.id}">Edit</button>
        <button class="danger small" data-action="memory-delete" data-id="${r.id}">Delete</button></div>
      <div class="content">${esc(r.content)}</div></div>`).join("")
    : empty(state.memory.q ? "Nothing matches this search." : "No memories here yet.");
}

// ------------------------------------------------------------------ chat --
async function loadChat() {
  const title = $("chat-title");
  const del = $("chat-delete");
  if (!state.convId) {
    title.textContent = "New conversation";
    del.hidden = true;
    state.chatSignature = "";
    $("chat-messages").innerHTML = `<div class="chat-empty"><h2>What should Dot Lab do?</h2>
      <p class="muted" style="margin-top:6px">Each message becomes a task. Follow-ups like “now the same for June” use this conversation as context.</p>
      <div class="suggestions">
        <button class="ghost" data-action="suggest" data-text="Analyse demo/demo_sales.csv and rank branches by total revenue.">Rank branches by revenue in the demo data</button>
        <button class="ghost" data-action="suggest" data-text="Summarise gross profit in demo/demo_sales.csv by month and save a short report.">Monthly gross profit report</button>
        <button class="ghost" data-action="suggest" data-text="Use the calculator to compute 17.5% of 48,250.">A quick calculation</button>
      </div></div>`;
    return;
  }
  const [detail, tasks] = await Promise.all([
    api(`/conversations/${encodeURIComponent(state.convId)}`),
    api(`/tasks?conversation_id=${encodeURIComponent(state.convId)}&limit=200`),
  ]);
  title.textContent = detail.conversation.title;
  del.hidden = false;
  const byId = Object.fromEntries(tasks.map((t) => [t.id, t]));
  const signature = JSON.stringify([detail.messages.map((m) => m.id), tasks.map((t) => [t.id, t.status])]);
  if (signature === state.chatSignature) return; // nothing changed: keep scroll position
  state.chatSignature = signature;

  const box = $("chat-messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const html = detail.messages.map((m) => {
    const t = m.task_id ? byId[m.task_id] : null;
    const meta = m.role === "user" && t
      ? `<div class="meta">${status(t.status)}<button class="link" data-action="open-task" data-id="${esc(t.id)}">View task</button></div>`
      : `<div class="meta faint">${esc(fmtTime(m.created_at))}</div>`;
    return `<div class="msg ${m.role}">${esc(m.content)}${meta}</div>`;
  });
  const working = tasks.find((t) => ACTIVE.includes(t.status));
  const approval = tasks.find((t) => t.status === "WAITING_APPROVAL");
  const question = tasks.find((t) => t.status === "WAITING_INPUT");
  if (working) html.push(`<div class="typing"><span class="dots"><span></span><span></span><span></span></span>Dot Lab is working (${esc(working.status.toLowerCase())})…</div>`);
  if (approval) html.push(`<div class="typing">${status("WAITING_APPROVAL")} The agent needs your approval. ${taskLink(approval.id, "Review")}</div>`);
  if (question) html.push(`<div class="typing">${status("WAITING_INPUT")} Your next message answers the agent's question.</div>`);
  box.innerHTML = html.join("") || empty("No messages yet.");
  if (nearBottom || !box.dataset.loaded) box.scrollTop = box.scrollHeight;
  box.dataset.loaded = "1";
  $("chat-input").placeholder = question ? "Type your answer…" : "Ask Dot Lab to do something… (Enter to send, Shift+Enter for a new line)";
}

async function sendChat() {
  const input = $("chat-input");
  const content = input.value.trim();
  if (content.length < 3) { showError("Please write at least 3 characters."); return; }
  $("chat-send").disabled = true;
  try {
    if (!state.convId) state.convId = (await api("/conversations", { method: "POST", body: "{}" })).id;
    await api(`/conversations/${encodeURIComponent(state.convId)}/messages`, { method: "POST", body: JSON.stringify({ content }) });
    input.value = "";
    showError(null);
    $("chat-messages").dataset.loaded = "";
    await views.workspace.load();
  } catch (err) {
    showError(err.message);
  } finally {
    $("chat-send").disabled = false;
    input.focus();
  }
}

// ------------------------------------------------------------------ task drawer --
function renderPlan(t) {
  if (!t.plan) return `<p class="muted">No plan yet.</p>`;
  const steps = t.plan.steps.map((s) => `<li class="${s.status === "RUNNING" || s.status === "WAITING_APPROVAL" ? "current" : ""}">
      <div class="step-head">${status(s.status)}<strong>${esc(s.id)}</strong>
        <span class="step-tool">${esc(s.tool || "reasoning")}${s.requires_approval ? " · needs approval" : ""}${s.attempts > 1 ? ` · ${s.attempts} attempts` : ""}</span></div>
      <div>${esc(s.description)}</div>
      ${s.result ? `<details><summary class="faint small">Output</summary><pre>${esc(s.result)}</pre></details>` : ""}
      ${s.error && s.status !== "COMPLETED" ? `<div class="muted small">${esc(s.error)}</div>` : ""}</li>`).join("");
  return `${t.plan.summary ? `<p class="muted" style="margin-bottom:8px">${esc(t.plan.summary)}</p>` : ""}<ol class="steps">${steps}</ol>`;
}

function renderDetail(t) {
  $("d-status").className = `status ${t.status}`;
  $("d-status").textContent = t.status.replace(/_/g, " ");
  $("d-goal").textContent = t.goal;
  const u = t.usage;
  const tokens = u ? ` · ${fmtNum(u.total_tokens)}${u.max_tokens ? ` / ${fmtNum(u.max_tokens)}` : ""} tokens${u.estimated_cost_usd != null ? ` (~$${u.estimated_cost_usd.toFixed(4)})` : ""}` : "";
  $("d-meta").textContent = `Created ${fmtTime(t.created_at)} · updated ${fmtRel(t.updated_at)} · ${t.iterations} iterations${tokens}`;

  const actions = [];
  if (ACTIVE.includes(t.status)) actions.push(`<button class="ghost small" data-action="task-act" data-act="pause">Pause</button>`);
  if (t.status === "PAUSED") actions.push(`<button class="small" data-action="task-act" data-act="resume">Resume</button>`);
  if (!TERMINAL.includes(t.status)) actions.push(`<button class="danger small" data-action="task-act" data-act="cancel">Cancel</button>`);
  actions.push(`<button class="ghost small" data-action="task-report" data-id="${esc(t.id)}">Download report</button>`);
  $("d-actions").innerHTML = actions.join("");

  const html = [];
  const open = (t.clarifications || []).filter((c) => c.answer === null);
  if (t.status === "WAITING_INPUT" && open.length) {
    html.push(`<div class="question-box"><h2>Agent has a question for you</h2><p>${esc(open[open.length - 1].question)}</p>
      <textarea id="answer-input" rows="3" maxlength="5000" placeholder="Your answer" style="margin-top:10px"></textarea>
      <div class="row" style="margin-top:8px"><button data-action="answer" data-id="${esc(t.id)}">Send answer</button></div></div>`);
  }
  const pending = t.approvals.filter((a) => a.status === "PENDING");
  if (pending.length) html.push(`<div class="question-box"><h2>Agent is waiting for your approval</h2>${pending.map(approvalCard).join("")}</div>`);
  if (t.error) html.push(`<div class="banner error" style="margin-top:14px">${esc(t.error)}</div>`);
  const answered = (t.clarifications || []).filter((c) => c.answer !== null);
  if (answered.length) html.push(`<h3>Clarifications</h3>${answered.map((c) => `<div class="item"><div><strong>Q:</strong> ${esc(c.question)}</div><div><strong>A:</strong> ${esc(c.answer)}</div></div>`).join("")}`);
  if (t.result) html.push(`<h3>Final result</h3><pre>${esc(t.result)}</pre>`);
  html.push(`<h3>Current step</h3><p>${esc(t.current_step || "–")}</p>`);
  html.push(`<h3>Plan</h3>${renderPlan(t)}`);
  html.push(`<details class="block"><summary>Execution history (${t.events.length})</summary><ul class="timeline">${
    t.events.map((e) => `<li><time>${esc(fmtTime(e.created_at))}</time><div><span class="ev ${eventClass(e.event)}">${esc(e.event)}</span>${esc(e.message)}</div></li>`).join("")}</ul></details>`);
  html.push(`<details class="block"><summary>Tool calls (${t.tool_calls.length})</summary>${
    t.tool_calls.map((c) => `<div class="item"><div class="row">${status(c.status)}<strong>${esc(c.tool)}</strong>
        <span class="faint small">${esc(c.step_id)} · attempt ${c.attempt} · ${c.duration_ms} ms</span></div>
        <pre>${esc(JSON.stringify(c.input, null, 2))}</pre>${c.error ? `<div class="muted small">${esc(c.error)}</div>` : ""}
        ${c.output ? `<details><summary class="faint small">Output</summary><pre>${esc(c.output)}</pre></details>` : ""}</div>`).join("") || empty("No tool calls.")}</details>`);
  html.push(`<details class="block"><summary>Approvals (${t.approvals.length})</summary>${
    t.approvals.map((a) => `<div class="item"><div class="row">${status(a.status)}${esc(a.action)}</div>
      <div class="muted small">${esc(a.reason)}${a.decision_note ? ` · note: ${esc(a.decision_note)}` : ""}</div></div>`).join("") || empty("No approvals.")}</details>`);
  html.push(`<details class="block"><summary>Memory (${t.memory.length})</summary>${
    t.memory.map((m) => `<div class="item"><div><span class="tag">${esc(m.kind)}</span> <span class="tag new">${esc(m.category)}</span></div><pre>${esc(m.content)}</pre></div>`).join("") || empty("Nothing was stored.")}</details>`);

  // Keep open <details> blocks and a half-typed answer across refreshes.
  const content = $("d-content");
  const openBlocks = [...content.querySelectorAll("details.block")].map((d) => d.open);
  const draft = $("answer-input") ? $("answer-input").value : "";
  content.innerHTML = html.join("");
  content.querySelectorAll("details.block").forEach((d, i) => { if (openBlocks[i]) d.open = true; });
  if ($("answer-input") && draft) $("answer-input").value = draft;
}

async function openTask(id) {
  state.openTaskId = id;
  $("d-content").innerHTML = `<div class="loading">Loading</div>`;
  $("drawer").hidden = false;
  await refreshDetail();
}

async function refreshDetail() {
  if (!state.openTaskId) return;
  try { renderDetail(await api(`/tasks/${encodeURIComponent(state.openTaskId)}`)); } catch (err) { showError(err.message); }
}

function closeDrawer() { $("drawer").hidden = true; state.openTaskId = null; }

// ------------------------------------------------------------------ modal --
function openModal(title, html) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = html;
  $("modal").hidden = false;
  const first = $("modal-body").querySelector("textarea, input");
  if (first) first.focus();
}
function closeModal() { $("modal").hidden = true; $("modal-body").innerHTML = ""; }

function openNewTaskModal() {
  openModal("New task", `<form id="modal-task-form">
    <div class="field"><label for="modal-goal">Goal</label>
      <textarea id="modal-goal" rows="4" maxlength="5000" required placeholder="Describe what the agent should achieve."></textarea></div>
    <div class="row"><span class="spacer"></span><button type="button" class="ghost" data-action="close-modal">Cancel</button><button type="submit">Start task</button></div></form>`);
}

async function createTask(goal) {
  const created = await api("/tasks", { method: "POST", body: JSON.stringify({ goal }) });
  toast("Task started");
  await openTask(created.task_id);
}

// ------------------------------------------------------------------ navigation & polling --
function setView(name) {
  if (!views[name]) name = "overview";
  state.view = name;
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === name));
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  $("view-title").textContent = views[name].title;
  $("view-subtitle").textContent = views[name].subtitle;
  document.title = `${views[name].title} · Dot Lab`;
  $("sidebar").classList.remove("open");
  loadView();
}

async function loadView() {
  try { await views[state.view].load(); showError(null); } catch (err) { showError(err.message); }
}

async function refreshHealth() {
  const pill = $("llm-status");
  try {
    const h = await fetch("/api/health").then((r) => r.json());
    const check = h.model_check || {};
    if (!h.llm_configured) { pill.className = "pill bad"; pill.textContent = "AI not configured"; }
    else if (check.status === "unavailable" || check.status === "error") { pill.className = "pill bad"; pill.textContent = "Model check failed"; pill.title = check.detail || ""; }
    else { pill.className = "pill ok"; pill.textContent = check.status === "available" ? "AI connected" : "AI configured"; pill.title = check.detail || ""; }
  } catch { pill.className = "pill bad"; pill.textContent = "Server unreachable"; }
}

/** Refresh the badges, the current view (if it auto-refreshes) and the open task. */
async function refreshAll() {
  try {
    const view = views[state.view];
    if (state.view !== "overview") updateBadges(await api("/overview"));
    if (view.refresh) await view.refresh();
    else if (view.refreshable !== false) await view.load();
    await refreshDetail();
    $("last-refresh").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    showError(err.message);
  }
}

// ------------------------------------------------------------------ actions --
const actions = {
  "dismiss-error": () => showError(null),
  "open-task": (d) => openTask(d.id),
  "close-drawer": closeDrawer,
  "close-modal": closeModal,
  "task-act": (d) => act(() => api(`/tasks/${encodeURIComponent(state.openTaskId)}/${d.act}`, { method: "POST" }), `Task ${d.act === "cancel" ? "cancelled" : d.act + "d"}`),
  "task-report": (d) => downloadFile(`/tasks/${encodeURIComponent(d.id)}/report`, `dot-lab-task-${d.id.slice(0, 8)}.md`).then(() => toast("Report downloaded"), (e) => showError(e.message)),
  approve: (d) => act(() => api(`/approvals/${encodeURIComponent(d.id)}/approve`, { method: "POST", body: "{}" }), "Approved"),
  reject: (d) => {
    const note = prompt("Optional note for the agent (why you rejected it):");
    if (note === null) return;
    act(() => api(`/approvals/${encodeURIComponent(d.id)}/reject`, { method: "POST", body: JSON.stringify({ note: note || null }) }), "Rejected");
  },
  answer: (d) => {
    const answer = ($("answer-input")?.value || "").trim();
    if (!answer) { showError("Please type an answer first."); return; }
    act(() => api(`/tasks/${encodeURIComponent(d.id)}/answer`, { method: "POST", body: JSON.stringify({ answer }) }), "Answer sent");
  },
  "mark-read": (d) => act(() => api(`/notifications/${encodeURIComponent(d.id)}/read`, { method: "POST" })),
  "monitor-run": (d) => act(() => api(`/monitors/${encodeURIComponent(d.id)}/run`, { method: "POST" }), "Monitor started"),
  "monitor-toggle": (d) => act(() => api(`/monitors/${encodeURIComponent(d.id)}/${d.enabled === "true" ? "disable" : "enable"}`, { method: "POST" })),
  "monitor-delete": (d) => { if (confirm("Delete this monitor? Its past tasks are kept.")) act(() => api(`/monitors/${encodeURIComponent(d.id)}`, { method: "DELETE" }), "Monitor deleted"); },
  "new-conversation": () => { state.convId = null; state.chatSignature = ""; views.workspace.load(); $("chat-input").focus(); },
  "select-conversation": (d) => { state.convId = d.id; state.chatSignature = ""; $("chat-messages").dataset.loaded = ""; views.workspace.load(); },
  "delete-conversation": () => {
    if (!state.convId || !confirm("Delete this conversation? Its messages are removed; its tasks are kept.")) return;
    const id = state.convId;
    state.convId = null;
    act(() => api(`/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }), "Conversation deleted");
  },
  suggest: (d) => { $("chat-input").value = d.text; $("chat-input").focus(); },
  "history-more": () => { state.history.offset += state.history.pageSize; views.history.load(true).catch((e) => showError(e.message)); },
  "activity-kind": (d) => {
    state.activity.kind = d.kind;
    document.querySelectorAll("#activity-filters .chip").forEach((c) => c.classList.toggle("active", c.dataset.kind === d.kind));
    views.activity.load().catch((e) => showError(e.message));
  },
  "activity-more": async () => {
    const a = state.activity;
    const last = a.items[a.items.length - 1];
    try {
      const older = await api(`/activity?kind=${a.kind}&limit=50&before_id=${last.id}`);
      a.items = [...a.items, ...older];
      a.hasMore = older.length === 50;
      renderActivity();
    } catch (e) { showError(e.message); }
  },
  "memory-category": (d) => { state.memory.category = d.category || null; views.memory.load().catch((e) => showError(e.message)); },
  "memory-more": () => { state.memory.offset += state.memory.pageSize; views.memory.load(true).catch((e) => showError(e.message)); },
  "memory-edit": (d) => {
    const r = state.memory.items.find((x) => String(x.id) === d.id);
    if (!r) return;
    openModal("Edit memory", `<form id="memory-edit-form" data-id="${r.id}">
      <div class="field"><label for="edit-content">Content</label><textarea id="edit-content" rows="8" maxlength="20000" required>${esc(r.content)}</textarea></div>
      <div class="field"><label for="edit-category">Category</label><input id="edit-category" maxlength="50" value="${esc(r.category)}" list="category-options" required></div>
      <div class="row"><span class="spacer"></span><button type="button" class="ghost" data-action="close-modal">Cancel</button><button type="submit">Save changes</button></div></form>`);
  },
  "memory-delete": (d) => {
    if (!confirm("Delete this memory permanently? The agent will no longer use it.")) return;
    act(async () => { await api(`/memory/${encodeURIComponent(d.id)}`, { method: "DELETE" }); await views.memory.load(); }, "Memory deleted");
  },
  "view-report": async (d) => {
    try {
      const text = await fetchText(`/reports/${encodeURIComponent(d.name)}?download=false`);
      openModal(d.name, `<pre style="max-height:none">${esc(text)}</pre><div class="row" style="margin-top:12px"><span class="spacer"></span>
        <button data-action="download-report" data-name="${esc(d.name)}">Download</button></div>`);
    } catch (e) { showError(e.message); }
  },
  "download-report": (d) => downloadFile(`/reports/${encodeURIComponent(d.name)}`, d.name).then(() => toast("Report downloaded"), (e) => showError(e.message)),
  "clear-api-key": () => { storageSet("dotlab-api-key", null); $("api-key").value = ""; toast("API key removed from this browser"); loadView(); },
};

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el || !actions[el.dataset.action]) return;
  if (el.tagName === "TR" && e.target.closest("button, a")) return; // inner buttons win over row clicks
  e.preventDefault();
  actions[el.dataset.action](el.dataset);
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("modal").hidden) closeModal();
  else if (!$("drawer").hidden) closeDrawer();
});

document.addEventListener("submit", async (e) => {
  const form = e.target;
  e.preventDefault();
  if (form.id === "task-form") {
    const goal = $("goal").value.trim();
    if (goal.length < 3) { showError("Please describe the goal (at least 3 characters)."); return; }
    try { await createTask(goal); $("goal").value = ""; showError(null); loadView(); } catch (err) { showError(err.message); }
  } else if (form.id === "modal-task-form") {
    const goal = $("modal-goal").value.trim();
    if (goal.length < 3) { showError("Please describe the goal (at least 3 characters)."); return; }
    try { closeModal(); await createTask(goal); showError(null); loadView(); } catch (err) { showError(err.message); }
  } else if (form.id === "chat-form") {
    await sendChat();
  } else if (form.id === "monitor-form") {
    await act(async () => {
      await api("/monitors", { method: "POST", body: JSON.stringify({
        name: $("m-name").value.trim(), goal: $("m-goal").value.trim(), interval_minutes: Number($("m-interval").value),
      }) });
      form.reset();
    }, "Monitor created");
  } else if (form.id === "memory-form") {
    const content = $("memory-content").value.trim();
    const category = $("memory-category").value.trim() || "general";
    if (!content) { showError("Please write the note first."); return; }
    await act(async () => {
      await api("/memory", { method: "POST", body: JSON.stringify({ content, category }) });
      form.reset();
      await views.memory.load();
    }, "Note saved");
  } else if (form.id === "memory-edit-form") {
    const id = form.dataset.id;
    const content = $("edit-content").value.trim();
    const category = $("edit-category").value.trim();
    await act(async () => {
      await api(`/memory/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ content, category }) });
      closeModal();
      await views.memory.load();
    }, "Memory updated");
  } else if (form.id === "api-key-form") {
    const key = $("api-key").value.trim();
    if (!key) { showError("Enter a key, or use Clear to remove the saved one."); return; }
    storageSet("dotlab-api-key", key);
    $("api-key").value = "";
    toast("API key saved in this browser");
    loadView();
  }
});

$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
});
$("status-filter").addEventListener("change", (e) => { state.tasksStatus = e.target.value; loadView(); });
$("history-q").addEventListener("input", debounce((e) => { state.history.q = e.target.value.trim(); loadView(); }));
$("history-status").addEventListener("change", (e) => { state.history.status = e.target.value; loadView(); });
$("memory-q").addEventListener("input", debounce((e) => { state.memory.q = e.target.value.trim(); loadView(); }));
$("memory-kind").addEventListener("change", (e) => { state.memory.kind = e.target.value; loadView(); });
$("new-task-btn").addEventListener("click", openNewTaskModal);
$("menu-toggle").addEventListener("click", (e) => { e.stopPropagation(); $("sidebar").classList.toggle("open"); });
// On small screens, tapping anywhere outside the open menu closes it.
document.addEventListener("click", (e) => {
  const sidebar = $("sidebar");
  if (sidebar.classList.contains("open") && !sidebar.contains(e.target)) sidebar.classList.remove("open");
}, true);
window.addEventListener("hashchange", () => setView(location.hash.slice(1)));

setView(location.hash.slice(1) || "overview");
refreshHealth();
setInterval(() => { if (!document.hidden) refreshAll(); }, POLL_MS);
setInterval(() => { if (!document.hidden) refreshHealth(); }, POLL_MS * 5);
