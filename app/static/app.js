"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  // Abort after a timeout so a wrong URL / dead host can't hang the UI.
  const ms = opts.timeoutMs || 15000;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      signal: ctrl.signal,
      ...opts,
    });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === "AbortError") throw new Error(`timed out after ${Math.round(ms / 1000)}s`);
    throw e;
  }
  clearTimeout(timer);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    const d = data && (data.detail ?? data.error);
    if (typeof d === "string") {
      msg = d;
    } else if (Array.isArray(d)) {
      // FastAPI validation errors: [{loc, msg, type}, ...]
      msg = d.map((e) => (e && e.msg) ? e.msg : JSON.stringify(e)).join("; ");
    } else if (d && typeof d === "object") {
      msg = d.msg || JSON.stringify(d);
    }
    throw new Error(msg);
  }
  return data;
};

function fmtBytes(n) {
  n = Number(n || 0);
  if (n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

// ----------------------------- tabs --------------------------------------
document.querySelectorAll("nav.tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    $(btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "logs") loadLogs();
    if (btn.dataset.tab === "scan") loadExclusions();
  });
});

// --------------------------- settings ------------------------------------
const BOOL_FIELDS = ["include_unrated", "auto_select_empty",
  "dry_run_only", "delete_files_enabled", "add_import_exclusion", "sonarr_unmonitor",
  "auto_unblock_on_request"];
const TEXT_FIELDS = ["radarr_url", "sonarr_url", "seerr_url"];
const SECRET_FIELDS = ["radarr_api_key", "sonarr_api_key", "tmdb_api_key", "seerr_api_key"];

async function loadSettings() {
  const s = await api("/api/settings");
  TEXT_FIELDS.forEach((k) => { if ($(k)) $(k).value = s[k] || ""; });
  BOOL_FIELDS.forEach((k) => { if ($(k)) $(k).checked = !!s[k]; });
  SECRET_FIELDS.forEach((k) => {
    // Secrets come back as booleans (set/unset). Show a placeholder if set.
    if ($(k)) $(k).placeholder = s[k] ? "•••••• saved - leave blank to keep current" : "leave blank to keep current";
  });
  $("min_rt_score").value = s.min_rt_score;
  $("rtVal").textContent = s.min_rt_score;
  $("rtImdb").textContent = `(TMDb ${(s.min_rt_score / 10).toFixed(1)})`;

  if ($("tzVal")) $("tzVal").textContent = s._tz;
  updateModeBadge(s.dry_run_only);
  updateTmdbGate(!!s.tmdb_api_key);
  updateWebhookInfo(s);
}

function updateWebhookInfo(s) {
  const box = $("webhookInfo");
  if (!box) return;
  const on = !!s.auto_unblock_on_request;
  const token = s.seerr_webhook_token || "";
  if (on && token) {
    const inp = $("webhookUrl");
    if (inp) inp.value = `${location.origin}/api/seerr/webhook?token=${encodeURIComponent(token)}`;
    box.style.display = "block";
  } else if (on) {
    box.style.display = "block";  // enabled but not yet saved → token pending
  } else {
    box.style.display = "none";
  }
}

let tmdbConfigured = false;
function updateTmdbGate(configured) {
  tmdbConfigured = configured;
  const gate = $("tmdbGate");
  if (gate) gate.style.display = configured ? "none" : "block";
  const btn = $("runScan");
  if (btn) {
    btn.disabled = !configured;
    btn.title = configured ? "" : "Add a TMDb API key in Setup first";
  }
}

function updateModeBadge(dryOnly) {
  const badge = $("modeBadge");
  if (dryOnly) {
    badge.textContent = "DRY RUN ONLY";
    badge.className = "mode dry";
    $("liveWarn").style.display = "none";
  } else {
    badge.textContent = "LIVE - DELETIONS ALLOWED";
    badge.className = "mode live";
    $("liveWarn").style.display = "block";
  }
}

$("min_rt_score").addEventListener("input", (e) => {
  $("rtVal").textContent = e.target.value;
  $("rtImdb").textContent = `(TMDb ${(e.target.value / 10).toFixed(1)})`;
});
$("dry_run_only").addEventListener("change", (e) => updateModeBadge(e.target.checked));
if ($("auto_unblock_on_request")) {
  $("auto_unblock_on_request").addEventListener("change", (e) => {
    const box = $("webhookInfo");
    if (box) box.style.display = e.target.checked ? "block" : "none";
  });
}

$("saveSettings").addEventListener("click", async () => {
  const body = {};
  TEXT_FIELDS.forEach((k) => { body[k] = $(k).value.trim(); });
  SECRET_FIELDS.forEach((k) => { const v = $(k).value.trim(); if (v) body[k] = v; });
  BOOL_FIELDS.forEach((k) => { body[k] = $(k).checked; });
  body.min_rt_score = parseInt($("min_rt_score").value, 10);
  const st = $("saveStatus");
  const btn = $("saveSettings");
  btn.disabled = true;
  st.textContent = "saving..."; st.className = "status pending";
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(body), timeoutMs: 12000 });
    st.textContent = "saved"; st.className = "status ok";
    SECRET_FIELDS.forEach((k) => { $(k).value = ""; });
    await loadSettings();
  } catch (e) {
    st.textContent = e.message; st.className = "status err";
  } finally {
    btn.disabled = false;
  }
});

async function testConn(which) {
  const statusEl = $(which + "Status");
  const btnEl = $("test" + which.charAt(0).toUpperCase() + which.slice(1));
  statusEl.textContent = "testing..."; statusEl.className = "status pending";
  if (btnEl) btnEl.disabled = true;
  const urlEl = $(which + "_url");           // TMDb has no URL field
  const body = {
    url: urlEl ? (urlEl.value.trim() || null) : null,
    api_key: $(which + "_api_key").value.trim() || null,
  };
  try {
    const r = await api(`/api/test/${which}`, { method: "POST", body: JSON.stringify(body), timeoutMs: 12000 });
    if (r.ok) {
      statusEl.textContent = r.detail
        ? `connected - ${r.detail}`
        : `connected - ${r.app || which} ${r.version || ""}`.trim();
      statusEl.className = "status ok";
    } else {
      statusEl.textContent = r.error || "failed"; statusEl.className = "status err";
    }
  } catch (e) {
    statusEl.textContent = e.message; statusEl.className = "status err";
  } finally {
    if (btnEl) btnEl.disabled = false;
  }
}
$("testRadarr").addEventListener("click", () => testConn("radarr"));
$("testSonarr").addEventListener("click", () => testConn("sonarr"));
$("testTmdb").addEventListener("click", () => testConn("tmdb"));
$("testSeerr").addEventListener("click", () => testConn("seerr"));

// ------------------------------ scan -------------------------------------
let currentScanId = null;

$("runScan").addEventListener("click", async () => {
  const st = $("scanStatus");
  if (!tmdbConfigured) {
    st.textContent = "Add a TMDb API key in Setup first."; st.className = "status err";
    return;
  }
  st.textContent = "starting..."; st.className = "status pending";
  $("runScan").disabled = true;
  showProgress(true);
  setBar(null, "Starting...");        // indeterminate until totals known
  try {
    // Persist the on-page controls so they take effect for this scan even if
    // the user didn't visit Setup → Save.
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        min_rt_score: parseInt($("min_rt_score").value, 10),
        include_unrated: $("include_unrated").checked,
        auto_select_empty: $("auto_select_empty").checked,
      }),
      timeoutMs: 12000,
    });
    await api("/api/scan", { method: "POST", body: JSON.stringify({ scope: $("scanScope").value }) });
    await pollProgress(st);
  } catch (e) {
    st.textContent = e.message; st.className = "status err";
    showProgress(false);
    $("runScan").disabled = false;
  }
});

function showProgress(on) {
  $("scanProgress").style.display = on ? "block" : "none";
}

function setBar(pct, phaseText) {
  const bar = $("progBar");
  if (pct == null) {
    bar.classList.add("indeterminate");
    $("progPct").textContent = "";
  } else {
    bar.classList.remove("indeterminate");
    bar.style.width = pct + "%";
    $("progPct").textContent = pct + "%";
  }
  if (phaseText) $("progPhase").textContent = phaseText;
}

const PHASE_LABEL = {
  fetching: "Fetching library from Radarr/Sonarr...",
  scanning: "Scanning & rating titles...",
  done: "Done",
  error: "Error",
};

function pollProgress(st) {
  return new Promise((resolve) => {
    const tick = async () => {
      let p;
      try { p = await api("/api/scan/progress", { timeoutMs: 10000 }); }
      catch (_) { setTimeout(tick, 700); return; }

      const total = p.total_items || 0;
      const done = p.processed_items || 0;
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : null;
      setBar(p.phase === "fetching" ? null : pct, PHASE_LABEL[p.phase] || p.phase);
      $("progItems").textContent = `${done} / ${total} items`;
      $("progBytes").textContent = `${fmtBytes(p.scanned_bytes)} / ${fmtBytes(p.total_bytes)} scanned`;
      $("progCurrent").textContent = p.current_title ? `current: ${p.current_title}` : "";

      if (p.phase === "error") {
        st.textContent = p.error || "scan failed"; st.className = "status err";
        showProgress(false); $("runScan").disabled = false;
        return resolve();
      }
      if (!p.running && p.phase === "done") {
        setBar(100, "Done");
        const scanId = p.scan_id;
        try {
          const d = await api(`/api/scan/${scanId}`, { timeoutMs: 15000 });
          currentScanId = scanId;
          renderScan(d.scan.summary || {}, d.items);
          st.textContent = `done - scan #${scanId}`;
          st.className = p.status === "completed" ? "status ok" : "status err";
        } catch (e) {
          st.textContent = e.message; st.className = "status err";
        }
        setTimeout(() => showProgress(false), 600);
        $("runScan").disabled = false;
        return resolve();
      }
      setTimeout(tick, 500);
    };
    tick();
  });
}

function actionPill(a) {
  if (a === "delete") return '<span class="pill delete">delete</span>';
  if (a === "review") return '<span class="pill review">needs review</span>';
  return '<span class="pill keep">keep</span>';
}

function renderScan(summary, items) {
  $("scanResults").style.display = "block";
  $("freedNum").textContent = fmtBytes(summary.total_freed_bytes);

  const parts = [];
  if ("movies_total" in summary)
    parts.push(`movies: ${summary.movies_flagged || 0} flagged / ${summary.movies_total} scanned`);
  if ("series_total" in summary)
    parts.push(`TV: ${summary.series_flagged || 0} flagged / ${summary.series_total} scanned`);
  $("freedSub").innerHTML = parts.join("<br>");

  const errBox = $("scanErrors");
  if (summary.errors && summary.errors.length) {
    errBox.innerHTML = summary.errors
      .map((e) => `<div class="banner err">${escapeHtml(e)}</div>`).join("");
  } else { errBox.innerHTML = ""; }

  const body = $("resultsBody");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="13" class="empty">Nothing below the threshold. Library is clean.</td></tr>';
    $("confirmGate").style.display = "none";
    return;
  }
  body.innerHTML = items.map((it) => {
    const deletable = it.proposed_action === "delete";
    const cb = deletable
      ? `<input type="checkbox" class="sel-cb" data-id="${it.id}" data-size="${it.size_bytes || 0}" ${it.selected ? "checked" : ""} />`
      : "";
    const isFolder = it.media_type === "folder";
    const typePill = isFolder
      ? `<span class="pill folder">folder</span>`
      : `<span class="pill ${it.media_type}">${it.media_type}</span>`;
    const exCell = isFolder
      ? "-"
      : `<input type="checkbox" class="excl-cb" ${isExcluded(it) ? "checked" : ""}
           data-mt="${it.media_type}" data-tmdb="${it.tmdb_id ?? ""}" data-tvdb="${it.tvdb_id ?? ""}"
           data-title="${escapeHtml(it.title || "")}" title="Exclude from future scans" />`;
    return `<tr>
      <td class="checkbox-cell">${cb}</td>
      <td class="title">${escapeHtml(it.title || "")}</td>
      <td class="num">${it.year || ""}</td>
      <td>${typePill}</td>
      <td class="score">${it.score == null ? "-" : it.score + "/100"}</td>
      <td class="muted">${escapeHtml(it.rating_source || "")}</td>
      <td class="muted">${escapeHtml(it.requested_by || "-")}</td>
      <td class="path">${escapeHtml(it.path || "")}</td>
      <td class="num">${fmtBytes(it.size_bytes)}</td>
      <td>${actionPill(it.proposed_action)}</td>
      <td class="num">${it.prevent_redl ? "yes" : "-"}</td>
      <td class="muted" style="font-size:11px;max-width:240px">${escapeHtml(it.reason || "")}</td>
      <td class="checkbox-cell">${exCell}</td>
    </tr>`;
  }).join("");

  body.querySelectorAll(".excl-cb").forEach((cb) => cb.addEventListener("change", onExcludeChange));

  body.querySelectorAll(".sel-cb").forEach((cb) => {
    cb.addEventListener("change", onSelectChange);
  });
  recalcSelection();
  $("confirmGate").style.display = "block";
}

async function onSelectChange(e) {
  const id = parseInt(e.target.dataset.id, 10);
  try { await api("/api/scan/select", { method: "POST", body: JSON.stringify({ item_id: id, selected: e.target.checked }) }); }
  catch (_) {}
  recalcSelection();
}

function selectedCheckboxes() {
  return Array.from($("resultsBody").querySelectorAll(".sel-cb:checked"));
}

function recalcSelection() {
  const sel = selectedCheckboxes();
  const size = sel.reduce((a, c) => a + Number(c.dataset.size || 0), 0);
  $("selCount").textContent = sel.length;
  $("selSize").textContent = fmtBytes(size);
  $("fileNote").textContent = "";
  validateDelete();
}

function validateDelete() {
  const ok = $("confirmText").value === "DELETE" && selectedCheckboxes().length > 0;
  $("runDelete").disabled = !ok;
}
$("confirmText").addEventListener("input", validateDelete);

$("runDelete").addEventListener("click", async () => {
  const ids = selectedCheckboxes().map((c) => parseInt(c.dataset.id, 10));
  const st = $("deleteStatus");
  if (!confirm(`Permanently act on ${ids.length} item(s)? This cannot be undone.`)) return;
  st.textContent = "deleting..."; st.className = "status pending";
  $("runDelete").disabled = true;
  try {
    const r = await api("/api/delete", {
      method: "POST",
      body: JSON.stringify({ scan_id: currentScanId, confirm: $("confirmText").value, item_ids: ids }),
      timeoutMs: 600000,
    });
    st.textContent = `done - ${r.deleted} deleted, ${r.failed} failed, ${fmtBytes(r.freed_bytes)} freed`;
    st.className = "status ok";
    $("confirmText").value = "";
    loadLogs();
  } catch (e) {
    st.textContent = e.message; st.className = "status err";
  } finally {
    validateDelete();
  }
});

// ------------------------------ logs -------------------------------------
async function loadLogs() {
  try {
    const r = await api("/api/logs");
    const box = $("logBody");
    if (!r.actions.length) { box.innerHTML = '<p class="empty">No actions logged yet.</p>'; }
    else {
      box.innerHTML = r.actions.map((a) => {
        const t = new Date(a.ts * 1000).toLocaleString();
        const cls = a.success ? "ok" : "fail";
        const tag = (a.action && a.action.indexOf("unblock") === 0)
          ? '<span class="tag unblock">UNBLOCK</span> ' : "";
        return `<div class="logline"><span class="ts">${t}</span> ·
          <span class="${cls}">${a.success ? "OK" : "FAIL"}</span> · ${tag}
          ${escapeHtml(a.media_type)} · ${escapeHtml(a.title || "")} ·
          <span class="muted">${escapeHtml(a.action)}</span>
          ${a.detail ? "· " + escapeHtml(a.detail) : ""}</div>`;
      }).join("");
    }
  } catch (_) {}
  try {
    const r = await api("/api/reports");
    const box = $("reportBody");
    if (!r.reports.length) { box.innerHTML = '<p class="empty">No reports yet.</p>'; }
    else {
      box.innerHTML = r.reports.map((f) =>
        `<div class="logline"><a href="/api/reports/${encodeURIComponent(f.name)}">${escapeHtml(f.name)}</a>
         <span class="muted">(${fmtBytes(f.size)})</span></div>`).join("");
    }
  } catch (_) {}
  loadBlocks();
}

async function loadBlocks() {
  try {
    const r = await api("/api/blocks");
    const box = $("blocksBody");
    if (!box) return;
    const list = r.blocks || [];
    if (!list.length) { box.innerHTML = '<p class="empty">No active blocks.</p>'; return; }
    const label = {
      radarr_exclusion: "Radarr exclusion",
      sonarr_unmonitor: "Sonarr unmonitored",
      sonarr_exclusion: "Sonarr exclusion",
    };
    box.innerHTML = list.map((b) => {
      const when = b.created_at ? new Date(b.created_at * 1000).toLocaleDateString() : "";
      return `<div class="excl-row"><span class="pill ${b.media_type}">${escapeHtml(b.media_type)}</span>
        <span class="excl-title">${escapeHtml(b.title || "?")}</span>
        <span class="muted">${escapeHtml(label[b.block_type] || b.block_type || "")}</span>
        <span class="muted">${when}</span></div>`;
    }).join("");
  } catch (_) {}
}
$("refreshLogs").addEventListener("click", loadLogs);

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ------------------------------ init -------------------------------------
async function loadVersion() {
  try {
    const h = await api("/api/health", { timeoutMs: 8000 });
    if ($("appVersion") && h && h.version) $("appVersion").textContent = "v" + h.version;
  } catch (_) { /* non-fatal */ }
}

loadSettings().catch((e) => console.error(e));
loadExclusions().catch((e) => console.error(e));
loadVersion();

// --------------------------- biggest items -------------------------------
let currentBiggestScanId = null;

if ($("biggestLimit")) {
  $("biggestLimit").addEventListener("input", (e) => {
    $("biggestLimitVal").textContent = e.target.value;
  });
}

if ($("runBiggest")) {
  $("runBiggest").addEventListener("click", async () => {
    const st = $("biggestStatus");
    st.textContent = "starting..."; st.className = "status pending";
    $("runBiggest").disabled = true;
    showBigProgress(true);
    setBigBar(null, "Starting...");
    try {
      await api("/api/biggest", {
        method: "POST",
        body: JSON.stringify({
          scope: $("biggestScope").value,
          limit: parseInt($("biggestLimit").value, 10),
          empty_cleanup: $("biggest_empty_cleanup") ? $("biggest_empty_cleanup").checked : false,
        }),
      });
      await pollBigProgress(st);
    } catch (e) {
      st.textContent = e.message; st.className = "status err";
      showBigProgress(false);
      $("runBiggest").disabled = false;
    }
  });
}

function showBigProgress(on) {
  if ($("biggestProgress")) $("biggestProgress").style.display = on ? "block" : "none";
}

function setBigBar(pct, phaseText) {
  const bar = $("bigProgBar");
  if (!bar) return;
  if (pct == null) {
    bar.classList.add("indeterminate");
    $("bigProgPct").textContent = "";
  } else {
    bar.classList.remove("indeterminate");
    bar.style.width = pct + "%";
    $("bigProgPct").textContent = pct + "%";
  }
  if (phaseText) $("bigProgPhase").textContent = phaseText;
}

function pollBigProgress(st) {
  const labels = {
    fetching: "Fetching library from Radarr/Sonarr...",
    scanning: "Measuring & ranking by size...",
    done: "Done", error: "Error",
  };
  return new Promise((resolve) => {
    const tick = async () => {
      let p;
      try { p = await api("/api/scan/progress", { timeoutMs: 10000 }); }
      catch (_) { setTimeout(tick, 700); return; }

      const total = p.total_items || 0;
      const done = p.processed_items || 0;
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : null;
      setBigBar(p.phase === "fetching" ? null : pct, labels[p.phase] || p.phase);
      $("bigProgItems").textContent = `${done} / ${total} items`;
      $("bigProgBytes").textContent = `${fmtBytes(p.scanned_bytes)} / ${fmtBytes(p.total_bytes)}`;
      $("bigProgCurrent").textContent = p.current_title ? `current: ${p.current_title}` : "";

      if (p.phase === "error") {
        st.textContent = p.error || "scan failed"; st.className = "status err";
        showBigProgress(false); $("runBiggest").disabled = false;
        return resolve();
      }
      if (!p.running && p.phase === "done") {
        setBigBar(100, "Done");
        try {
          const d = await api(`/api/scan/${p.scan_id}`, { timeoutMs: 20000 });
          currentBiggestScanId = p.scan_id;
          const items = (d.items || []).slice().sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0));
          renderBiggest(d.scan.summary || {}, items);
          st.textContent = `found ${items.length} item(s)`;
          st.className = p.status === "completed" ? "status ok" : "status err";
        } catch (e) {
          st.textContent = e.message; st.className = "status err";
        }
        setTimeout(() => showBigProgress(false), 600);
        $("runBiggest").disabled = false;
        return resolve();
      }
      setTimeout(tick, 400);
    };
    tick();
  });
}

function renderBiggest(summary, items) {
  $("biggestResults").style.display = "block";
  $("biggestTotal").textContent = fmtBytes(summary.total_bytes);
  const scopeLabel = { both: "movies + shows", movies: "movies", tv: "shows" }[summary.scope] || "items";
  let sub = `${summary.count} largest ${scopeLabel}`;
  if (summary.empty_cleanup) {
    const e = summary.empty_items || 0, f = summary.empty_folders || 0;
    if (e || f) sub += ` · ${e} empty entr${e === 1 ? "y" : "ies"} + ${f} orphaned folder${f === 1 ? "" : "s"} flagged`;
  }
  $("biggestSub").textContent = sub;

  const errBox = $("biggestErrors");
  errBox.innerHTML = (summary.errors && summary.errors.length)
    ? summary.errors.map((e) => `<div class="banner err">${escapeHtml(e)}</div>`).join("")
    : "";

  const body = $("biggestBody");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">Nothing found.</td></tr>';
    $("biggestConfirmGate").style.display = "none";
    return;
  }
  body.innerHTML = items.map((it) => {
    const deletable = it.proposed_action === "delete";
    const cb = deletable
      ? `<input type="checkbox" data-id="${it.id}" data-size="${it.size_bytes || 0}" ${it.selected ? "checked" : ""} />`
      : "";
    return `<tr>
      <td class="checkbox-cell">${cb}</td>
      <td class="title">${escapeHtml(it.title || "")}</td>
      <td class="num">${it.year || ""}</td>
      <td><span class="pill ${it.media_type}">${it.media_type}</span></td>
      <td class="num">${fmtBytes(it.size_bytes)}</td>
      <td class="path">${escapeHtml(it.path || "")}</td>
      <td class="muted" style="font-size:11px;max-width:240px">${escapeHtml(it.reason || "")}</td>
    </tr>`;
  }).join("");

  body.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", onBiggestSelectChange);
  });
  recalcBiggest();
  $("biggestConfirmGate").style.display = "block";
}

async function onBiggestSelectChange(e) {
  const id = parseInt(e.target.dataset.id, 10);
  try { await api("/api/scan/select", { method: "POST", body: JSON.stringify({ item_id: id, selected: e.target.checked }) }); }
  catch (_) {}
  recalcBiggest();
}

function biggestSelected() {
  return Array.from($("biggestBody").querySelectorAll('input[type="checkbox"]:checked'));
}

function recalcBiggest() {
  const sel = biggestSelected();
  const size = sel.reduce((a, c) => a + Number(c.dataset.size || 0), 0);
  $("biggestSelCount").textContent = sel.length;
  $("biggestSelSize").textContent = fmtBytes(size);
  validateBiggestDelete();
}

function validateBiggestDelete() {
  const ok = $("biggestConfirmText").value === "DELETE" && biggestSelected().length > 0;
  $("biggestDelete").disabled = !ok;
}
if ($("biggestConfirmText")) $("biggestConfirmText").addEventListener("input", validateBiggestDelete);

if ($("biggestDelete")) {
  $("biggestDelete").addEventListener("click", async () => {
    const ids = biggestSelected().map((c) => parseInt(c.dataset.id, 10));
    const st = $("biggestDeleteStatus");
    if (!confirm(`Permanently act on ${ids.length} item(s)? This cannot be undone.`)) return;
    st.textContent = "deleting..."; st.className = "status pending";
    $("biggestDelete").disabled = true;
    try {
      const r = await api("/api/delete", {
        method: "POST",
        body: JSON.stringify({ scan_id: currentBiggestScanId, confirm: $("biggestConfirmText").value, item_ids: ids }),
        timeoutMs: 600000,
      });
      st.textContent = `done - ${r.deleted} deleted, ${r.failed} failed, ${fmtBytes(r.freed_bytes)} freed`;
      st.className = "status ok";
      $("biggestConfirmText").value = "";
      loadLogs();
    } catch (e) {
      st.textContent = e.message; st.className = "status err";
    } finally {
      validateBiggestDelete();
    }
  });
}

// --------------------------- exclusions ----------------------------------
let excludedKeys = new Set();

function exKeys(it) {
  const k = [];
  if (it.tmdb_id != null && it.tmdb_id !== "") k.push(`${it.media_type}:tmdb:${it.tmdb_id}`);
  if (it.media_type === "tv" && it.tvdb_id != null && it.tvdb_id !== "") k.push(`tv:tvdb:${it.tvdb_id}`);
  return k;
}
function isExcluded(it) { return exKeys(it).some((k) => excludedKeys.has(k)); }
function toggleKey(k, on) { if (on) excludedKeys.add(k); else excludedKeys.delete(k); }

async function onExcludeChange(e) {
  const cb = e.target;
  const mt = cb.dataset.mt;
  const tmdb = cb.dataset.tmdb ? parseInt(cb.dataset.tmdb, 10) : null;
  const tvdb = cb.dataset.tvdb ? parseInt(cb.dataset.tvdb, 10) : null;
  try {
    await api("/api/exclude", {
      method: "POST",
      body: JSON.stringify({ media_type: mt, tmdb_id: tmdb, tvdb_id: tvdb, title: cb.dataset.title, excluded: cb.checked }),
      timeoutMs: 10000,
    });
    if (mt === "movie" && tmdb != null) toggleKey(`movie:tmdb:${tmdb}`, cb.checked);
    if (mt === "tv") {
      if (tmdb != null) toggleKey(`tv:tmdb:${tmdb}`, cb.checked);
      if (tvdb != null) toggleKey(`tv:tvdb:${tvdb}`, cb.checked);
    }
    await loadExclusions();
  } catch (err) {
    cb.checked = !cb.checked;  // revert on failure
  }
}

async function loadExclusions() {
  try {
    const d = await api("/api/exclusions", { timeoutMs: 10000 });
    const list = d.exclusions || [];
    excludedKeys = new Set();
    list.forEach((x) => {
      if (x.tmdb_id != null) excludedKeys.add(`${x.media_type}:tmdb:${x.tmdb_id}`);
      if (x.tvdb_id != null) excludedKeys.add(`${x.media_type}:tvdb:${x.tvdb_id}`);
    });
    renderExclusions(list);
  } catch (_) { /* non-fatal */ }
}

function renderExclusions(list) {
  const panel = $("exclusionsPanel");
  const box = $("exclusionsList");
  if (!panel) return;
  if (!list.length) { panel.style.display = "none"; box.innerHTML = ""; return; }
  panel.style.display = "block";
  box.innerHTML = list.map((x) => `<div class="excl-row">
      <span class="pill ${x.media_type}">${x.media_type}</span>
      <span class="excl-title">${escapeHtml(x.title || "?")}</span>
      <button class="btn ghost" data-id="${x.id}">remove</button>
    </div>`).join("");
  box.querySelectorAll("button[data-id]").forEach((b) => {
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await api(`/api/exclusions/${b.dataset.id}`, { method: "DELETE", timeoutMs: 10000 }); await loadExclusions(); }
      catch (_) { b.disabled = false; }
    });
  });
}
