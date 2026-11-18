"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let plan = null;          // current plan record from the backend
let draft = null;         // editable copy of plan.topology
let resolved = {};        // ambiguity text -> note
let discovered = {};      // router -> probe result
let dirty = false;
let adminRequired = false;
let pollTimer = null;

async function api(method, path, body, isForm) {
  const opts = { method, headers: {} };
  const tok = $("#adminToken").value;
  if (tok) opts.headers["X-Admin-Token"] = tok;
  if (body && !isForm) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  if (isForm) opts.body = body;
  const r = await fetch(path, opts);
  const text = await r.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    let d = data && data.detail !== undefined ? data.detail : data;
    const err = new Error(typeof d === "string" ? d : (d && d.error) || JSON.stringify(d));
    err.data = d; err.status = r.status; throw err;
  }
  return data;
}
function setStatus(id, msg, kind) { const el = $(id); el.textContent = msg || ""; el.className = "status" + (el.classList.contains("inline") || ["#editStatus","#reviewStatus","#approveStatus","#deployStatus"].includes(id) ? " inline" : "") + (kind ? " " + kind : ""); }

// ---------------------------------------------------------------- health
async function loadHealth() {
  try {
    const h = await api("GET", "/health");
    adminRequired = h.admin_token_required;
    $("#adminToken").hidden = !adminRequired;
    const routers = Object.entries(h.routers).map(([n, ip]) => `${n} ${ip}`).join(", ") || "none";
    $("#health").innerHTML = `<span>model ${esc(h.model)}</span><span>topic ${esc(h.topic)}</span>` +
      `<span class="${h.groq_key_set ? "ok" : "bad"}">Groq key ${h.groq_key_set ? "set" : "missing"}</span>` +
      `<span class="${h.ssh_credentials_set ? "ok" : "bad"}">SSH creds ${h.ssh_credentials_set ? "set" : "missing"}</span>` +
      `<span>routers: ${esc(routers)}</span>`;
  } catch (e) { $("#health").innerHTML = `<span class="bad">backend unreachable</span>`; }
  try {
    const plans = await api("GET", "/api/plans");
    const sel = $("#resume");
    sel.innerHTML = '<option value="">— previous plan —</option>' + plans.slice(0, 20).map((p) =>
      `<option value="${esc(p.id)}">${esc(p.created_at.replace("T", " ").slice(0, 19))} · ${esc(p.image_name || "")} · ${esc(p.status)} / ${esc(p.deployment_status)}</option>`).join("");
  } catch {}
}

// ---------------------------------------------------------------- upload + generate
$("#file").addEventListener("change", () => {
  const f = $("#file").files[0];
  $("#btnGenerate").disabled = !f;
  if (!f) return;
  if (!["image/png", "image/jpeg"].includes(f.type)) { setStatus("#genStatus", "Only PNG or JPEG images.", "err"); $("#btnGenerate").disabled = true; return; }
  $("#preview").src = URL.createObjectURL(f); $("#preview").hidden = false;
  setStatus("#genStatus", `${f.name} · ${(f.size / 1024).toFixed(0)} KB — click Generate to analyse with AI`);
});

$("#btnGenerate").addEventListener("click", async () => {
  const f = $("#file").files[0]; if (!f) return;
  const fd = new FormData(); fd.append("image", f);
  $("#btnGenerate").disabled = true;
  setStatus("#genStatus", "Analysing diagram with the image model… (10–30 s)");
  try {
    const p = await api("POST", "/api/analyze", fd, true);
    setStatus("#genStatus", "AI analysis complete.", "ok");
    loadPlan(p);
    loadHealth();
  } catch (e) {
    setStatus("#genStatus", "AI analysis failed: " + e.message + (e.data && e.data.raw ? "\n\nRaw output:\n" + e.data.raw : ""), "err");
  } finally { $("#btnGenerate").disabled = false; }
});

$("#resume").addEventListener("change", async () => {
  const id = $("#resume").value; if (!id) return;
  try { const p = await api("GET", `/api/plans/${id}`); $("#preview").src = `/api/plans/${id}/image`; $("#preview").hidden = false; loadPlan(p); }
  catch (e) { setStatus("#genStatus", e.message, "err"); }
});

// ---------------------------------------------------------------- plan rendering
function loadPlan(p) {
  plan = p; draft = JSON.parse(JSON.stringify(p.topology)); resolved = {}; dirty = false; discovered = {};
  ["#secAnalysis", "#secEdit", "#secReview", "#secDeploy"].forEach((s) => ($(s).hidden = false));
  $("#commands").innerHTML = ""; $("#validation").innerHTML = ""; $("#jobBox").innerHTML = "";
  ["#editStatus", "#reviewStatus", "#approveStatus", "#deployStatus"].forEach((s) => setStatus(s, ""));
  renderAnalysis(); renderEditor(); updateButtons();
  if (plan.current_job_id) watchJob(plan.current_job_id);
  if (plan.status === "APPROVED") reviewCommands();
}

function renderAnalysis() {
  $("#planMeta").innerHTML = `plan ${esc(plan.id.slice(0, 8))} · <span class="badge b-${esc(plan.status)}">${esc(plan.status)}</span> · deployment ${esc(plan.deployment_status)} · model ${esc(plan.ai?.model)}`;
  $("#jsonView").textContent = JSON.stringify(plan.topology, null, 2);
  $("#rawView").textContent = JSON.stringify(plan.ai?.raw_model_json, null, 2);
  const notes = (plan.ai?.postprocess_notes || []).map((n) => `<div class="note">Post-processing: ${esc(n)}</div>`);
  const t = plan.topology;
  const summary = `<div class="note">Found ${t.routers.length} router(s), ${t.pcs.length} PC(s), ${t.links.length} link(s); OSPF process ${esc(t.ospf_process_id)}, area ${esc(t.ospf_area)}; ` +
    `<b>${t.ambiguities.length}</b> open ambiguit${t.ambiguities.length === 1 ? "y" : "ies"}${plan.resolved_ambiguities.length ? `, ${plan.resolved_ambiguities.length} resolved` : ""}.</div>`;
  $("#notes").innerHTML = summary + notes.join("");
}

function editable() { return plan && plan.status === "DRAFT"; }

function renderEditor() {
  const ro = !editable();
  // ambiguities
  const amb = draft.ambiguities.map((a, i) => {
    const done = a in resolved;
    return `<div class="amb ${done ? "done" : ""}"><label><input type="checkbox" data-amb="${i}" ${done ? "checked" : ""} ${ro ? "disabled" : ""}> <b>Ambiguity:</b> ${esc(a)}</label>` +
      (done ? `<input type="text" data-ambnote="${i}" placeholder="How was this resolved? (required)" value="${esc(resolved[a])}">` : "") + `</div>`;
  });
  const res = (plan.resolved_ambiguities || []).map((r) => `<div class="amb done">✔ <b>Resolved:</b> ${esc(r.ambiguity)}<br><small>${esc(r.note)}</small></div>`);
  $("#ambiguities").innerHTML = amb.join("") + res.join("") || `<div class="note">No ambiguities reported.</div>`;

  // routers
  $("#routers").innerHTML = draft.routers.map((r, ri) => {
    const probe = discovered[r.name];
    const dl = probe && probe.interfaces ? `<datalist id="dl-${ri}">${probe.interfaces.map((x) => `<option value="${esc(x.name)}">${esc(x.ip)} ${esc(x.status)}${x.carries_ssh ? " (SSH)" : ""}</option>`).join("")}</datalist>` : "";
    const rows = r.interfaces.map((it, ii) => {
      const mg = it.role === "management";
      const lbl = it.diagram_label ? `<small class="sep">diagram label: ${esc(it.diagram_label)}</small>` : "";
      return `<tr><td><input list="dl-${ri}" data-r="${ri}" data-i="${ii}" data-f="name" value="${esc(it.name)}" ${ro ? "disabled" : ""}>${lbl}</td>` +
        `<td><select data-r="${ri}" data-i="${ii}" data-f="role" ${ro ? "disabled" : ""}>${["lan", "transit", "management", "unknown"].map((o) => `<option ${o === it.role ? "selected" : ""}>${o}</option>`).join("")}</select></td>` +
        `<td><input data-r="${ri}" data-i="${ii}" data-f="ipv4_cidr" value="${esc(mg ? "" : it.ipv4_cidr)}" placeholder="${mg ? "(management — never configured)" : "a.b.c.d/nn"}" ${ro || mg ? "disabled" : ""}></td>` +
        `<td>${checkMark(r.name, it)}</td></tr>`;
    }).join("");
    const p = probe ? (probe.reachable ? `<span class="mark-ok">✔ reachable as ${esc(probe.hostname)} (${esc(probe.host)}), SSH on ${esc(probe.ssh_interface)}</span>` : `<span class="mark-bad">✘ ${esc(probe.error)}</span>`) : "";
    return `<div class="rtr"><div class="rtr-head"><b>${esc(r.name)}</b><label>Router ID <input data-r="${ri}" data-f="router_id" value="${esc(r.router_id)}" size="14" ${ro ? "disabled" : ""}></label>${p}</div>${dl}
      <table class="grid"><thead><tr><th>Interface</th><th>Role</th><th>IPv4/prefix</th><th>On router</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }).join("");

  // PCs
  $("#pcTable tbody").innerHTML = draft.pcs.map((pc, pi) =>
    `<tr><td><input data-pc="${pi}" data-f="name" value="${esc(pc.name)}" ${ro ? "disabled" : ""}></td>` +
    `<td><input data-pc="${pi}" data-f="ipv4_cidr" value="${esc(pc.ipv4_cidr)}" ${ro ? "disabled" : ""}></td>` +
    `<td><input data-pc="${pi}" data-f="gateway" value="${esc(pc.gateway)}" ${ro ? "disabled" : ""}></td>` +
    `<td>${ro ? "" : `<button data-delpc="${pi}">Remove</button>`}</td></tr>`).join("") || `<tr><td colspan="4" class="sep">No PCs in this plan</td></tr>`;
  $("#btnAddPc").disabled = ro; $("#btnSave").disabled = ro || !dirty;
}

function checkMark(router, it) {
  const probe = discovered[router];
  if (!probe || !probe.interfaces || !it.name) return "";
  const row = probe.interfaces.find((x) => x.name.toLowerCase() === it.name.toLowerCase());
  if (!row) return `<span class="mark-bad">✘ not on router</span>`;
  if (row.carries_ssh && it.role !== "management") return `<span class="mark-bad">✘ carries SSH</span>`;
  if (row.carries_ssh) return `<span class="mark-ok">✔ management (${esc(row.ip)})</span>`;
  return `<span class="mark-ok">✔ exists</span> <small>${esc(row.ip)} ${esc(row.status)}</small>`;
}

function hasBlockingLocalIssue() {
  for (const r of draft.routers) for (const it of r.interfaces) {
    const probe = discovered[r.name];
    if (!probe || !probe.interfaces || !it.name || it.role === "management") continue;
    const row = probe.interfaces.find((x) => x.name.toLowerCase() === it.name.toLowerCase());
    if (!row || row.carries_ssh) return true;
  }
  return false;
}

function markDirty() { dirty = true; updateButtons(); setStatus("#editStatus", "Unsaved changes — Save before reviewing."); }

$("#secEdit").addEventListener("input", (e) => {
  const t = e.target; if (!draft) return;
  if (t.dataset.ambnote !== undefined) { resolved[draft.ambiguities[+t.dataset.ambnote]] = t.value; markDirty(); return; }
  if (t.dataset.pc !== undefined) { draft.pcs[+t.dataset.pc][t.dataset.f] = t.value || null; markDirty(); return; }
  if (t.dataset.r === undefined) return;
  const r = draft.routers[+t.dataset.r];
  if (t.dataset.i === undefined) { r[t.dataset.f] = t.value || null; markDirty(); return; }
  const it = r.interfaces[+t.dataset.i];
  if (t.dataset.f === "name") {
    const old = it.name;
    for (const l of draft.links) {          // propagate renames into links
      if (l.device_a === r.name && l.interface_a === old) l.interface_a = t.value;
      if (l.device_b === r.name && l.interface_b === old) l.interface_b = t.value;
    }
  }
  it[t.dataset.f] = t.value || null;
  if (it.role === "management") it.ipv4_cidr = null;
  markDirty();
});
$("#secEdit").addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.r !== undefined && (t.dataset.f === "role" || t.dataset.f === "name")) { renderEditor(); updateButtons(); return; }
  if (t.dataset.amb !== undefined) {
    const a = draft.ambiguities[+t.dataset.amb];
    if (t.checked) resolved[a] = resolved[a] || ""; else delete resolved[a];
    renderEditor(); markDirty();
  }
});
$("#secEdit").addEventListener("click", (e) => {
  const t = e.target;
  if (t.dataset.delpc !== undefined) { draft.pcs.splice(+t.dataset.delpc, 1); renderEditor(); markDirty(); }
});
$("#btnAddPc").addEventListener("click", () => { draft.pcs.push({ name: `PC${draft.pcs.length + 1}`, ipv4_cidr: null, gateway: null, port: "e0" }); renderEditor(); markDirty(); });

$("#btnDiscover").addEventListener("click", async () => {
  setStatus("#editStatus", "Reading 'show ip interface brief' from each router (read-only)…");
  $("#btnDiscover").disabled = true;
  try { discovered = await api("POST", `/api/plans/${plan.id}/discover`); renderEditor(); updateButtons();
    setStatus("#editStatus", "Discovery complete" + (hasBlockingLocalIssue() ? " — fix the ✘ rows before review." : "."), hasBlockingLocalIssue() ? "err" : "ok"); }
  catch (e) { setStatus("#editStatus", e.message, "err"); }
  finally { $("#btnDiscover").disabled = false; }
});

$("#btnSave").addEventListener("click", async () => {
  const items = Object.entries(resolved).map(([ambiguity, note]) => ({ ambiguity, note }));
  if (items.some((x) => !x.note.trim())) { setStatus("#editStatus", "Each resolved ambiguity needs a note.", "err"); return; }
  try {
    const p = await api("PUT", `/api/plans/${plan.id}/topology`, { topology: draft, resolved: items });
    const keep = discovered; loadPlan(p); discovered = keep; renderEditor();
    setStatus("#editStatus", "Saved. Re-run Review and Validate.", "ok");
  } catch (e) { setStatus("#editStatus", e.message, "err"); }
});

// ---------------------------------------------------------------- review / validate / approve
async function reviewCommands() {
  try {
    const r = await api("GET", `/api/plans/${plan.id}/review`);
    $("#commands").innerHTML = Object.entries(r.commands).map(([n, lines]) => `<div><h3>${esc(n)}</h3><pre>${esc(lines.join("\n"))}</pre></div>`).join("") +
      (r.generation_errors.length ? `<div><h3 class="mark-bad">Generation errors</h3><ul class="checks">${r.generation_errors.map((e) => `<li class="mark-bad">✘ ${esc(e)}</li>`).join("")}</ul></div>` : "");
    if (r.validation) renderValidation(r.validation);
    setStatus("#reviewStatus", r.config_sha256 ? `Approved config SHA-256 ${r.config_sha256.slice(0, 16)}…` : `${Object.keys(r.commands).length} router configuration(s) generated.`, r.generation_errors.length ? "err" : "ok");
  } catch (e) { setStatus("#reviewStatus", e.message, "err"); }
}
$("#btnReview").addEventListener("click", reviewCommands);

function renderValidation(v) {
  const lis = [...v.errors.map((e) => `<li class="mark-bad">✘ ${esc(e)}</li>`), ...v.warnings.map((w) => `<li class="mark-warn">⚠ ${esc(w)}</li>`)];
  const live = Object.values(v.live || {}).map((p) => `<li class="${p.reachable ? "mark-ok" : "mark-bad"}">${p.reachable ? "✔" : "✘"} ${esc(p.router)} management SSH ${esc(p.host)} ${p.reachable ? `reachable, hostname ${esc(p.hostname)}, privilege 15 ${p.privilege_15 ? "yes" : "NO"}` : esc(p.error)}</li>`);
  $("#validation").innerHTML = `<h3>Validation ${v.passed ? '<span class="badge b-SUCCESS">PASSED</span>' : '<span class="badge b-FAILED">FAILED</span>'} <small>${esc(v.at || "")}</small></h3>` +
    `<ul class="checks">${v.passed && !lis.length ? '<li class="mark-ok">✔ addressing, subnet consistency, interface mapping, OSPF settings</li>' : ""}${lis.join("")}${live.join("")}</ul>`;
  plan.validation = v; updateButtons();
}
$("#btnValidate").addEventListener("click", async () => {
  setStatus("#reviewStatus", "Validating (static checks + read-only SSH to each router)…");
  $("#btnValidate").disabled = true;
  try { const v = await api("POST", `/api/plans/${plan.id}/validate`); renderValidation(v); setStatus("#reviewStatus", v.passed ? "Validation passed." : "Validation failed.", v.passed ? "ok" : "err"); await reviewCommands(); }
  catch (e) { setStatus("#reviewStatus", e.message, "err"); }
  finally { updateButtons(); }
});

$("#chkApprove").addEventListener("change", updateButtons);
$("#btnApprove").addEventListener("click", async () => {
  setStatus("#approveStatus", "Re-validating and approving…");
  try {
    const p = await api("POST", `/api/plans/${plan.id}/approve`, { confirmation: "APPROVE", approved_by: $("#approver").value });
    plan = p; renderAnalysis(); renderEditor(); updateButtons(); await reviewCommands();
    setStatus("#approveStatus", `Approved by ${p.approved_by}. SHA-256 ${p.config_sha256.slice(0, 16)}…`, "ok");
  } catch (e) {
    if (e.data && e.data.validation) renderValidation(e.data.validation);
    setStatus("#approveStatus", e.message, "err");
  }
});

// ---------------------------------------------------------------- deploy + job watch
$("#btnDeploy").addEventListener("click", async () => {
  if (!confirm(`Deploy the approved configuration to ${Object.keys(plan.approved_config || {}).join(", ")} via Kafka → worker → SSH?`)) return;
  $("#btnDeploy").disabled = true;
  try {
    const r = await api("POST", `/api/plans/${plan.id}/deploy`);
    setStatus("#deployStatus", `Job ${r.job_id.slice(0, 8)} published to ${r.kafka.topic} (partition ${r.kafka.partition}, offset ${r.kafka.offset}).`, "ok");
    plan.deployment_status = "QUEUED"; plan.current_job_id = r.job_id; watchJob(r.job_id);
  } catch (e) { setStatus("#deployStatus", e.message, "err"); updateButtons(); }
});

function watchJob(id) {
  clearInterval(pollTimer);
  const tick = async () => {
    try {
      const j = await api("GET", `/api/jobs/${id}`);
      const last = (j.history || []).slice(-6).map((h) => `<div>${esc(h.at.slice(11, 19))} <b>${esc(h.status)}</b> ${esc(h.stage)} ${esc(h.message)}</div>`).join("");
      $("#jobBox").innerHTML = `<p>Job <a href="/jobs?job=${esc(j.id)}">${esc(j.id)}</a> <span class="badge b-${esc(j.status)}">${esc(j.status)}</span> stage <b>${esc(j.stage)}</b>${j.error ? ` — <span class="mark-bad">${esc(j.error)}</span>` : ""}</p><div class="timeline">${last}</div>`;
      plan.deployment_status = { PENDING: "QUEUED" }[j.status] || j.status;
      if (["SUCCESS", "FAILED", "REVOKED"].includes(j.status)) { clearInterval(pollTimer); renderAnalysis(); updateButtons(); }
    } catch {}
  };
  tick(); pollTimer = setInterval(tick, 2000);
}

function updateButtons() {
  if (!plan) return;
  const v = plan.validation;
  const unresolved = draft.ambiguities.filter((a) => !(a in resolved)).length;
  $("#btnSave").disabled = !editable() || !dirty;
  const block = dirty || hasBlockingLocalIssue();
  $("#btnReview").disabled = block;
  $("#btnValidate").disabled = block || !editable();
  $("#btnApprove").disabled = block || !editable() || !$("#chkApprove").checked || !v || !v.passed || unresolved > 0;
  $("#chkApprove").disabled = !editable();
  const ds = plan.deployment_status;
  $("#btnDeploy").disabled = plan.status !== "APPROVED" || ["QUEUED", "RUNNING", "SUCCESS"].includes(ds);
}

loadHealth();
