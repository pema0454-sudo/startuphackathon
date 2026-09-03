// Bema Farm — frontend logic. Plain JS, no framework, no build step.
// fetch() against the FastAPI backend, DOM rendering by hand.

const OFFICER_NAME = "Prakash Rai";
const POLL_MS = 6000;

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.status === 204 ? null : res.json();
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch (e) { return iso; }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------------------------------------------------------------
// command.html
// ---------------------------------------------------------------------

let STATIONS = [];

async function loadStations() {
  const res = await fetchJSON("/api/stations");
  STATIONS = res.stations || [];
  const opts = STATIONS.map(s => `<option value="${esc(s.name)}">${esc(s.name)}${s.primary ? " (primary)" : ""}</option>`).join("");
  const manualSel = document.getElementById("manual-station");
  const spikeSel = document.getElementById("spike-station");
  if (manualSel) manualSel.innerHTML = opts;
  if (spikeSel) spikeSel.innerHTML = opts;
}

async function refreshStatus() {
  const status = await fetchJSON("/api/status");

  const level = status.river_level;
  document.getElementById("kpi-level-label").textContent = `${status.primary_station} river level`;
  document.getElementById("kpi-level").textContent = level ? `${level.value} m` : "—";
  document.getElementById("kpi-level-sub").textContent = level ? `as of ${fmtTime(level.timestamp)} · ${level.trend || ""}` : "no data";

  const rain = status.rainfall_3day;
  document.getElementById("kpi-trend").textContent = rain ? `${rain.value} mm` : "—";
  document.getElementById("kpi-trend-sub").textContent = rain ? `${status.primary_station} · ${rain.trend || ""}` : "no data";

  document.getElementById("kpi-risk").textContent = status.policies_at_risk ?? "—";
  document.getElementById("kpi-risk-sub").textContent = `of ${status.policies_total ?? "?"} active policies district-wide`;

  const badges = document.getElementById("mode-badges");
  badges.innerHTML = "";
  if (status.demo_ai_mode) {
    badges.insertAdjacentHTML("beforeend", `<span class="badge badge-ai">DEMO AI MODE — rule-based fallback</span>`);
  }
  if (status.sync?.stale) {
    const mins = status.sync.stale_minutes;
    const label = mins != null ? `Data stale — last synced ${mins}m ago` : "Data stale — Supabase unreachable";
    badges.insertAdjacentHTML("beforeend", `<span class="badge badge-stale">${esc(label)}</span>`);
  } else if (status.sync?.mode === "supabase") {
    badges.insertAdjacentHTML("beforeend", `<span class="badge badge-sim">Supabase live</span>`);
  }
  return status;
}

function paramLine(p) {
  const cls = p.met ? "met" : "not-met";
  const unit = p.name === "river_level" ? "m" : "mm";
  const val = p.missing || p.value == null || Number.isNaN(p.value) ? "no data" : `${p.value}${unit}`;
  return `<li class="${cls}">${p.met ? "✓" : "✕"} ${esc(p.name.replace("_", " "))}: ${val} ${esc(p.operator || "")} ${p.threshold}${unit} ${p.met ? "(met)" : "(not met)"}</li>`;
}

function claimCard(claim, { pending }) {
  const confPct = Math.round((claim.confidence || 0) * 100);
  const borderline = claim.borderline
    ? `<span class="badge badge-borderline">Borderline — recommend manual double-check</span>` : "";
  const hazardBadge = `<span class="badge ${claim.hazard_type === "drought" ? "badge-drought" : "badge-flood"}">${esc(claim.hazard_type)}</span>`;
  const sourceNote = claim.source === "rule_based" || claim.source === "rule_based_fallback"
    ? `<span class="badge badge-ai">rule-based draft</span>` : "";
  const paramsList = (claim.checked_parameters || []).map(paramLine).join("");

  let actions = "";
  if (pending) {
    actions = `
      <div class="actions">
        <span class="confidence">${confPct}% confidence</span>
        ${borderline}
        ${sourceNote}
        <button class="btn btn-approve btn-sm" data-approve="${claim.id}">Approve</button>
        <button class="btn btn-reject btn-sm" data-reject="${claim.id}">Reject</button>
      </div>`;
  } else {
    const audio = claim.voice_file
      ? `<audio controls src="${esc(claim.voice_file)}"></audio>`
      : `<span class="empty-note">voice unavailable — text-only fallback</span>`;
    actions = `
      <div class="actions">
        <span class="badge badge-dispatch">Sandbox Dispatch</span>
      </div>
      <div class="dispatch-fineprint">Not sent to a real phone number — simulated for this environment.</div>
      <div class="sms-text">${esc(claim.sms_text || "")}</div>
      <div class="actions">${audio}</div>`;
  }

  return `
    <div class="claim-card" data-claim-card="${claim.id}">
      <div class="row1">
        <div class="farmer">${esc(claim.farmer_name)} — Policy #${esc(claim.policy_id)} ${hazardBadge}</div>
        <div class="meta">Ward ${esc(claim.ward)}, ${esc(claim.municipality || "")} · ${esc(claim.station)} · ${fmtTime(claim.created_at)}</div>
      </div>
      <div class="evidence">${esc(claim.claim_text)}</div>
      <ul class="params">${paramsList}</ul>
      <div class="meta">Recommended payout NPR ${Number(claim.recommended_amount).toLocaleString()}</div>
      ${actions}
    </div>`;
}

async function refreshClaims() {
  const [pending, confirmed] = await Promise.all([
    fetchJSON("/api/claims?status=" + encodeURIComponent("Pending Review")),
    fetchJSON("/api/claims?status=Confirmed"),
  ]);

  document.getElementById("pending-count").textContent = pending.length ? `(${pending.length})` : "";
  const pendingEl = document.getElementById("pending-list");
  pendingEl.innerHTML = pending.length
    ? pending.map(c => claimCard(c, { pending: true })).join("")
    : `<div class="empty-note">No pending claims right now.</div>`;

  const confirmedEl = document.getElementById("confirmed-list");
  confirmedEl.innerHTML = confirmed.length
    ? confirmed.slice(0, 10).map(c => claimCard(c, { pending: false })).join("")
    : `<div class="empty-note">Nothing confirmed yet.</div>`;

  pendingEl.querySelectorAll("[data-approve]").forEach(btn => {
    btn.addEventListener("click", () => handleDecision(btn.dataset.approve, "approve"));
  });
  pendingEl.querySelectorAll("[data-reject]").forEach(btn => {
    btn.addEventListener("click", () => handleDecision(btn.dataset.reject, "reject"));
  });
}

async function handleDecision(claimId, action) {
  const buttons = document.querySelectorAll(`[data-claim-card="${claimId}"] button`);
  buttons.forEach(b => b.disabled = true);
  try {
    if (action === "approve") {
      await fetchJSON(`/api/claims/${claimId}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ officer_name: OFFICER_NAME }),
      });
    } else {
      const reason = prompt("Reason for rejecting this claim?", "Does not match field report");
      if (reason === null) { buttons.forEach(b => b.disabled = false); return; }
      await fetchJSON(`/api/claims/${claimId}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ officer_name: OFFICER_NAME, reason }),
      });
    }
  } catch (e) {
    alert(`Could not ${action} claim: ${e.message}`);
  }
  await Promise.all([refreshClaims(), refreshTrace(), refreshStatus()]);
}

async function refreshTrace() {
  const entries = await fetchJSON("/api/trace?limit=200");
  const panel = document.getElementById("trace-panel");
  const atBottom = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 20;

  panel.innerHTML = entries.length
    ? entries.map(e => `<div class="line ${esc(e.step_type)}">${esc(e.line)}</div>`).join("")
    : "watching for agent activity…";
  document.getElementById("trace-count-badge").textContent = `${entries.length} lines`;

  if (atBottom) panel.scrollTop = panel.scrollHeight;

  const last = [...entries].reverse().find(e => e.step_type === "DONE");
  const brief = document.getElementById("brief-text");
  if (last) {
    const runLines = entries.filter(e => e.run_id === last.run_id);
    const trigger = runLines.find(e => e.step_type === "TRIGGER");
    const fallback = runLines.find(e => e.step_type === "FALLBACK");
    const pendingNow = document.getElementById("pending-count").textContent;
    brief.textContent =
      `Run ${last.run_id} — ${trigger ? trigger.message : ""}\n` +
      `${last.message}\n` +
      (fallback ? `⚠ ${fallback.message}\n` : "") +
      `Human review required for any new claims before a farmer is contacted. ${pendingNow ? "Pending: " + pendingNow : ""}`;
  }
}

async function runCheckNow() {
  const btn = document.getElementById("btn-check-now");
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    await fetchJSON("/api/agent/run", { method: "POST" });
    await Promise.all([refreshStatus(), refreshClaims(), refreshTrace()]);
  } catch (e) {
    alert(`Agent run failed: ${e.message}`);
  }
  btn.disabled = false;
  btn.textContent = "Check Now";
}

async function runSpike() {
  const station = document.getElementById("spike-station").value;
  const btn = document.getElementById("btn-spike");
  btn.disabled = true;
  try {
    await fetchJSON("/api/simulate/spike", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ station, parameter: "river_level", delta: 0.8 }),
    });
    await fetchJSON("/api/simulate/spike", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ station, parameter: "rainfall_3day", delta: 45 }),
    });
    await refreshStatus();
  } catch (e) {
    alert(`Could not simulate spike: ${e.message}`);
  }
  btn.disabled = false;
}

async function runManualSubmit() {
  const station = document.getElementById("manual-station").value;
  const parameter = document.getElementById("manual-parameter").value;
  const valueEl = document.getElementById("manual-value");
  const value = parseFloat(valueEl.value);
  if (Number.isNaN(value)) { alert("Enter a numeric value first."); return; }
  const btn = document.getElementById("btn-manual-submit");
  btn.disabled = true;
  try {
    await fetchJSON("/api/readings/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ station, parameter, value }),
    });
    valueEl.value = "";
    await refreshStatus();
  } catch (e) {
    alert(`Could not submit reading: ${e.message}`);
  }
  btn.disabled = false;
}

async function initCommandPage() {
  await loadStations();
  document.getElementById("btn-check-now").addEventListener("click", runCheckNow);
  document.getElementById("btn-spike").addEventListener("click", runSpike);
  document.getElementById("btn-manual-submit").addEventListener("click", runManualSubmit);

  const tick = () => {
    refreshStatus().catch(console.error);
    refreshClaims().catch(console.error);
    refreshTrace().catch(console.error);
  };
  tick();
  setInterval(tick, POLL_MS);
}

// ---------------------------------------------------------------------
// audit.html
// ---------------------------------------------------------------------

function auditRow(entry) {
  const actionClass = entry.action === "approve" ? "action-approve" : "action-reject";
  const rec = entry.ai_recommendation || {};
  const recSummary = rec.confidence != null
    ? `${Math.round(rec.confidence * 100)}% conf. · NPR ${Number(rec.recommended_amount || 0).toLocaleString()} · ${esc((rec.claim_text || "").slice(0, 90))}${(rec.claim_text || "").length > 90 ? "…" : ""}`
    : "—";
  return `
    <tr>
      <td>${fmtTime(entry.timestamp)}</td>
      <td>#${esc(entry.claim_id)}</td>
      <td class="${actionClass}">${entry.action.toUpperCase()}</td>
      <td>${esc(entry.officer_name)}</td>
      <td>${recSummary}</td>
      <td>${esc(entry.reason || "—")}</td>
    </tr>`;
}

async function initAuditPage() {
  const body = document.getElementById("audit-body");
  try {
    const rows = await fetchJSON("/api/audit");
    body.innerHTML = rows.length
      ? rows.map(auditRow).join("")
      : `<tr><td colspan="6" class="empty-note">No decisions recorded yet.</td></tr>`;
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" class="empty-note">Could not load audit log: ${esc(e.message)}</td></tr>`;
  }
}

// ---------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "command") initCommandPage();
  if (page === "audit") initAuditPage();
});
