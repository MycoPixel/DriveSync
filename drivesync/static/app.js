/* DriveSync dashboard — vanilla JS front-end for the local API */
"use strict";

const $ = (sel) => document.querySelector(sel);
/* escape every server value before it goes into innerHTML: file names,
   folder names and log messages are user/Drive-controlled text */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const api = async (path, opts = {}) => {
  const resp = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-DriveSync": "1",
      ...(opts.headers || {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await resp.json().catch(() => ({ ok: false, error: "bad JSON" }));
  if (!resp.ok || data.ok === false) throw new Error(data.error || resp.statusText);
  return data;
};

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

function setMsg(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (cls || "");
}

/* ---------- tabs ---------- */
document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
    loadTab(btn.dataset.tab);
  });
});
document.querySelectorAll("[data-goto]").forEach((a) =>
  a.addEventListener("click", (e) => {
    e.preventDefault();
    document.querySelector(`#tabs button[data-tab="${a.dataset.goto}"]`).click();
  }));

function loadTab(name) {
  if (name === "dashboard") loadDashboard();
  if (name === "pairs") loadPairs();
  if (name === "accounts") loadAccounts();
  if (name === "activity") loadEvents();
  if (name === "settings") loadSettings();
}

/* ---------- state ---------- */
let state = { accounts: [], pairs: [], newScope: [],
  picker: { stack: [], current: { id: "root", name: "My Drive" } },
  scope: { accountId: null, remoteId: null, selected: [], target: "new" } };

/* ---------- dashboard ---------- */
async function loadDashboard() {
  try {
    const st = await api("/api/status");
    $("#version").textContent = "v" + st.version;
    const w = st.worker || {};
    const running = w.running ? '<span class="pill on">syncing</span>'
      : '<span class="pill off">idle</span>';
    const last = w.last_run ? new Date(w.last_run * 1000).toLocaleString() : "—";
    const ls = w.last_stats ? `pushed ${w.last_stats.pushed} · pulled ${w.last_stats.pulled}` +
      ` · deleted ${w.last_stats.deleted} · conflicts ${w.last_stats.conflicts}` : "—";
    $("#workerStatus").innerHTML =
      `<b>State</b><span>${running}</span>
       <b>Interval</b><span>${st.interval}s</span>
       <b>Last run</b><span>${last}</span>
       <b>Last result</b><span>${ls}</span>` +
      (w.last_error ? `<b>Last error</b><span class="msg err">${esc(w.last_error)}</span>` : "");
  } catch (e) { $("#workerStatus").textContent = "status unavailable: " + e.message; }

  try {
    const { accounts } = await api("/api/accounts");
    state.accounts = accounts;
    $("#accountCards").innerHTML = accounts.length
      ? accounts.map((a) => `<div class="acct"><span>📮 ${esc(a.email)}</span>
          <span class="muted small">${esc(a.name || "")}</span></div>`).join("")
      : '<p class="muted small">No Google account connected yet.</p>';
  } catch (e) { /* ignore */ }

  try {
    const { pairs } = await api("/api/pairs");
    state.pairs = pairs;
    $("#pairsSummary").innerHTML = pairs.length
      ? `<table class="tbl"><tbody>${pairs.map((p) => `<tr>
          <td>#${p.id}</td><td>${esc(p.local_path)}</td>
          <td>${esc(p.remote_path || p.remote_id)}</td>
          <td>${esc(p.direction)}</td><td>${p.enabled ? "enabled" : "paused"}</td>
          <td><button class="btn ghost small" data-sync="${p.id}">Sync</button></td>
        </tr>`).join("")}</tbody></table>`
      : '<p class="muted small">No sync pairs yet — add one in the "Sync pairs" tab.</p>';
    bindSyncButtons();
  } catch (e) { /* ignore */ }

  await renderEvents($("#recentEvents"), 12);
}

function bindSyncButtons() {
  document.querySelectorAll("[data-sync]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/pairs/${btn.dataset.sync}/sync`, { method: "POST" });
      toast("Sync started for pair #" + btn.dataset.sync);
    }));
}

/* ---------- accounts ---------- */
async function loadAccounts() {
  try {
    const { accounts } = await api("/api/accounts");
    state.accounts = accounts;
    $("#accountsTable tbody").innerHTML = accounts.length
      ? accounts.map((a) => {
          const q = a.quota || {};
          const used = humanBytes(+q.usage || 0), limit = +q.limit || 0;
          const storage = limit ? `${used} / ${humanBytes(limit)}` : used || "—";
          return `<tr><td>${a.id}</td><td>${esc(a.email)}</td><td>${storage}</td>
            <td><button class="btn danger small" data-del-acct="${a.id}">Remove</button></td></tr>`;
        }).join("")
      : '<tr><td colspan="4" class="muted">No accounts connected.</td></tr>';
    document.querySelectorAll("[data-del-acct]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Remove this account and all of its sync pairs?")) return;
        await api(`/api/accounts/${b.dataset.delAcct}`, { method: "DELETE" });
        toast("Account removed");
        loadAccounts();
      }));
    const sel = $("#pairAccount");
    const prev = sel.value;
    sel.innerHTML = accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.email)}</option>`).join("");
    // keep the user's choice across refreshes instead of snapping to #1
    if (prev && accounts.some((a) => String(a.id) === prev)) sel.value = prev;
  } catch (e) { /* ignore */ }
}

$("#authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  setMsg("#authMsg", "A Google window should open in your browser; approve access there…", "");
  $("#authBtn").disabled = true;
  try {
    const { account } = await api("/api/accounts", {
      method: "POST",
      body: { client_id: $("#clientId").value.trim(), client_secret: $("#clientSecret").value.trim() },
    });
    setMsg("#authMsg", `Connected ${account.email} 🎉`, "ok");
    loadAccounts();
  } catch (err) {
    setMsg("#authMsg", err.message, "err");
  } finally {
    $("#authBtn").disabled = false;
  }
});

/* ---------- pairs ---------- */
async function loadPairs() {
  await loadAccounts();
  try {
    const { pairs } = await api("/api/pairs");
    state.pairs = pairs;
    $("#pairsTable tbody").innerHTML = pairs.length
      ? pairs.map((p) => {
        const scopeTxt = (p.include_patterns && p.include_patterns.length)
          ? `${p.include_patterns.length} subfolder(s)` : "everything";
        return `<tr>
          <td>#${p.id}</td><td>${esc(p.local_path)}</td>
          <td>${esc(p.remote_path || p.remote_id)}</td><td>${esc(p.direction)}</td>
          <td>${esc(p.conflict_policy)}</td>
          <td>${p.allow_deletes
            ? `<button class="btn ghost small" data-nodel="${p.id}" title="Deletions are allowed (files go to trash)">On</button>`
            : `<button class="btn ghost small" data-yodel="${p.id}" title="Deletions blocked — files are kept on both sides">Off</button>`}</td>
          <td>${scopeTxt}
            <button class="btn ghost small" data-scope="${p.id}" data-account="${p.account_id}" data-remote="${esc(p.remote_id)}">Edit</button></td>
          <td>${p.enabled
            ? `<button class="btn ghost small" data-pause="${p.id}">Pause</button>`
            : `<button class="btn ghost small" data-resume="${p.id}">Resume</button>`}</td>
          <td>
            <button class="btn ghost small" data-sync="${p.id}">Sync</button>
            <button class="btn danger small" data-del="${p.id}">Remove</button>
          </td></tr>`;
      }).join("")
      : '<tr><td colspan="9" class="muted">No sync pairs yet.</td></tr>';

    document.querySelectorAll("[data-nodel],[data-yodel]").forEach((b) =>
      b.addEventListener("click", async () => {
        const on = !!b.dataset.yodel;  // currently off -> turning on
        await api(`/api/pairs/${b.dataset.nodel || b.dataset.yodel}`,
                  { method: "PATCH", body: { allow_deletes: on ? 1 : 0 } });
        toast(on ? "Deletions allowed again" : "Deletions blocked — files will be kept");
        loadPairs();
      }));
    document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/pairs/${b.dataset.del}`, { method: "DELETE" });
      loadPairs();
    }));
    document.querySelectorAll("[data-pause],[data-resume]").forEach((b) =>
      b.addEventListener("click", async () => {
        const id = b.dataset.pause || b.dataset.resume;
        await api(`/api/pairs/${id}`, { method: "PATCH", body: { enabled: b.dataset.resume ? 1 : 0 } });
        loadPairs();
      }));
    document.querySelectorAll("[data-scope]").forEach((b) =>
      b.addEventListener("click", () => {
        const pair = state.pairs.find((p) => p.id === +b.dataset.scope);
        openScope(+b.dataset.account, b.dataset.remote,
                  (pair && pair.include_patterns) || [], b.dataset.scope);
      }));
    bindSyncButtons();
  } catch (e) { /* ignore */ }
}

$("#pairForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const remoteId = $("#pairRemoteId").value;
  if (!remoteId) return setMsg("#pairFormMsg", "Pick a Drive folder first.", "err");
  try {
    await api("/api/pairs", {
      method: "POST",
      body: {
        account_id: +$("#pairAccount").value,
        local_path: $("#pairLocal").value.trim(),
        remote_id: remoteId,
        remote_path: $("#pairRemoteName").value,
        direction: $("#pairDirection").value,
        conflict_policy: $("#pairPolicy").value,
        include_patterns: state.newScope,
      },
    });
    const scopeMsg = state.newScope.length
      ? ` Only ${state.newScope.length} chosen subfolder(s) will sync.` : "";
    setMsg("#pairFormMsg", "Pair added — first sync starts in the background." + scopeMsg, "ok");
    state.newScope = [];
    $("#pairScopeSummary").value = "Everything in this folder";
    loadPairs();
  } catch (err) { setMsg("#pairFormMsg", err.message, "err"); }
});

/* ---------- folder picker ---------- */
$("#pickFolderBtn").addEventListener("click", () => {
  state.picker = { stack: [], current: { id: "root", name: "My Drive" } };
  renderPicker();
  $("#pickerBackdrop").classList.remove("hidden");
});
$("#pickerClose").addEventListener("click", () => $("#pickerBackdrop").classList.add("hidden"));
$("#pickerBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#pickerBackdrop")) $("#pickerBackdrop").classList.add("hidden");
});
$("#pickerUp").addEventListener("click", () => {
  const prev = state.picker.stack.pop();
  if (prev) { state.picker.current = prev; renderPicker(); }
});
$("#pickerChoose").addEventListener("click", () => {
  setPairRemote(state.picker.current.id, state.picker.current.name);
  $("#pickerBackdrop").classList.add("hidden");
});

/* ---------- selective-sync scope ---------- */
function setPairRemote(id, name) {
  $("#pairRemoteId").value = id;
  $("#pairRemoteName").value = name;
  $("#pairScopeBtn").disabled = !id;
  state.newScope = [];
  $("#pairScopeSummary").value = id
    ? "Everything in this folder" : "Pick a Drive folder first";
}

async function openScope(accountId, remoteId, selected, target) {
  if (!remoteId) { toast("Pick a Drive folder first."); return; }
  state.scope = { accountId, remoteId, selected: [...selected], target };
  $("#scopeTitle").textContent = target === "new"
    ? "Choose subfolders to sync"
    : `Subfolders — pair #${target}`;
  $("#scopeBackdrop").classList.remove("hidden");
  $("#scopeList").innerHTML = '<p class="muted">loading…</p>';
  $("#scopeSave").disabled = true;
  try {
    const { folders } = await api(
      `/api/drive/${accountId}/folders?parent=${encodeURIComponent(remoteId)}`);
    $("#scopeList").innerHTML = folders.length
      ? folders.map((f) => {
        const on = selected.some((p) => p === f.name
          || p.startsWith(f.name + "/"));
        return `<label class="picker-item"><span>
          <input type="checkbox" value="${esc(f.name)}"${on ? " checked" : ""}>
          ${esc(f.name)}</span></label>`;
      }).join("")
      : '<p class="muted">This Drive folder has no subfolders.</p>';
    $("#scopeSave").disabled = false;
  } catch (e) {
    $("#scopeList").innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
    // the folder list failed, but "no boxes ticked" still means the safe
    // default (sync everything), so let the user save that
    $("#scopeSave").disabled = false;
  }
}

$("#pairScopeBtn").addEventListener("click", () => {
  const acct = +$("#pairAccount").value
    || (state.accounts[0] && state.accounts[0].id);
  openScope(acct, $("#pairRemoteId").value, state.newScope, "new");
});
$("#pairAccount").addEventListener("change", () => {
  // the chosen Drive folder belongs to the previous account — clear it so
  // a pair can never be created against another account's folder id
  setPairRemote("", "");
});
$("#scopeClose").addEventListener("click",
  () => $("#scopeBackdrop").classList.add("hidden"));
$("#scopeBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#scopeBackdrop")) {
    $("#scopeBackdrop").classList.add("hidden");
  }
});
$("#scopeAll").addEventListener("click", () =>
  document.querySelectorAll("#scopeList input[type=checkbox]")
    .forEach((c) => { c.checked = true; }));
$("#scopeNone").addEventListener("click", () =>
  document.querySelectorAll("#scopeList input[type=checkbox]")
    .forEach((c) => { c.checked = false; }));
$("#scopeSave").addEventListener("click", async () => {
  const chosen = [...document.querySelectorAll(
    "#scopeList input[type=checkbox]:checked")].map((c) => c.value);
  const sc = state.scope;
  if (sc.target === "new") {
    state.newScope = chosen;
    $("#pairScopeSummary").value = chosen.length
      ? `${chosen.length} subfolder(s): ${chosen.slice(0, 3).join(", ")}${chosen.length > 3 ? "…" : ""}`
      : "Everything in this folder";
    $("#scopeBackdrop").classList.add("hidden");
    return;
  }
  try {
    await api(`/api/pairs/${sc.target}`,
              { method: "PATCH", body: { include_patterns: chosen } });
    toast(chosen.length
      ? `Only ${chosen.length} subfolder(s) will sync`
      : "Scope reset — everything syncs");
    $("#scopeBackdrop").classList.add("hidden");
    loadPairs();
  } catch (e) { toast("Could not save scope: " + e.message); }
});

async function renderPicker() {
  const cur = state.picker.current;
  $("#pickerPath").textContent = state.picker.stack.map((s) => s.name).concat(cur.name).join(" / ");
  $("#pickerList").innerHTML = '<p class="muted">loading…</p>';
  try {
    const acct = $("#pairAccount").value || (state.accounts[0] && state.accounts[0].id);
    const { folders } = await api(`/api/drive/${acct}/folders?parent=${encodeURIComponent(cur.id)}`);
    const list = folders.length
      ? folders.map((f) => `<div class="picker-item" data-id="${esc(f.id)}" data-name="${esc(f.name)}">
          <span>📁 ${esc(f.name)}</span><span class="muted small">open ›</span></div>`).join("")
      : `<p class="muted">${cur.id === "shared"
          ? "No folders shared with this account yet." : "No subfolders."}</p>`;
    const useRow = cur.id === "shared" ? "" :
      `<div class="picker-item" data-id="${esc(cur.id)}" data-name="${esc(cur.name)}" data-choose="1">
        <span>✅ Use “${esc(cur.name)}”${folders.length ? "" : " (no subfolders)"}</span>
        <span class="muted small">select</span></div>`;
    $("#pickerList").innerHTML = list + useRow;
    document.querySelectorAll(".picker-item").forEach((item) =>
      item.addEventListener("click", () => {
        if (item.dataset.choose) {
          setPairRemote(item.dataset.id, item.dataset.name);
          $("#pickerBackdrop").classList.add("hidden");
        } else {
          state.picker.stack.push(state.picker.current);
          state.picker.current = { id: item.dataset.id, name: item.dataset.name };
          renderPicker();
        }
      }));
  } catch (e) {
    $("#pickerList").innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
}

/* ---------- activity ---------- */
async function renderEvents(container, limit = 200) {
  const filter = $("#eventFilter") ? $("#eventFilter").value : "";
  const { events } = await api("/api/events?limit=" + limit + (filter ? "&level=" + filter : ""));
  container.innerHTML = events.length
    ? events.map((e) => `<div class="ev ${esc(e.level)}">
        <span class="t">${esc(e.ts.replace("T", " "))}</span>
        <span class="a">${esc(e.action)}</span>
        <span class="p">${esc(e.path)}</span>
        <span class="m muted">${esc(e.message)}</span></div>`).join("")
    : '<p class="muted small">Nothing logged yet.</p>';
}
async function loadEvents() {
  await renderEvents($("#eventList")).catch((e) =>
    { $("#eventList").textContent = "failed to load events: " + e.message; });
}
$("#refreshEvents").addEventListener("click", loadEvents);
$("#eventFilter").addEventListener("change", loadEvents);

/* ---------- settings ---------- */
async function loadSettings() {
  try {
    const { settings } = await api("/api/settings");
    $("#setInterval").value = settings.sync_interval;
    $("#setPolicy").value = settings.conflict_policy;
    $("#setIgnores").value = settings.ignore_patterns.join("\n");
    $("#setDeleteLimit").value = settings.delete_safety_limit;
    $("#setConcurrency").value = settings.transfer_concurrency;
    $("#setBandwidth").value = settings.bandwidth_limit_kb;
    $("#setActiveFrom").value = settings.active_from || "";
    $("#setActiveTo").value = settings.active_to || "";
    $("#setWatch").checked = settings.watch_enabled !== false;
    $("#setIncremental").checked = settings.incremental_scan !== false;
    $("#setConvertDocs").checked = settings.convert_google_docs !== false;
    $("#aboutText").textContent =
      `DriveSync ${settings.server_port ? "" : ""}— local dashboard port ${settings.server_port}. ` +
      "Data lives in " + (await api("/api/status")).db + ".";
  } catch (e) { /* ignore */ }
}
$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings", {
      method: "POST",
      body: {
        sync_interval: +$("#setInterval").value,
        conflict_policy: $("#setPolicy").value,
        delete_safety_limit: +$("#setDeleteLimit").value,
        transfer_concurrency: +$("#setConcurrency").value,
        bandwidth_limit_kb: +$("#setBandwidth").value,
        active_from: $("#setActiveFrom").value,
        active_to: $("#setActiveTo").value,
        watch_enabled: $("#setWatch").checked,
        incremental_scan: $("#setIncremental").checked,
        convert_google_docs: $("#setConvertDocs").checked,
        ignore_patterns: $("#setIgnores").value.split("\n").map((s) => s.trim()).filter(Boolean),
      },
    });
    setMsg("#settingsMsg", "Saved.", "ok");
    toast("Settings saved");
  } catch (err) { setMsg("#settingsMsg", err.message, "err"); }
});

/* ---------- global actions ---------- */
$("#syncNowBtn").addEventListener("click", async () => {
  try {
    await api("/api/sync", { method: "POST" });
    toast("Sync started for all pairs");
  } catch (e) { toast("Could not start sync: " + e.message); }
});

function humanBytes(n) {
  if (!n) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + " " + units[i];
}

/* ---------- boot ---------- */
loadDashboard();
