// Glob — chat agent on the left, live project overview on the right.
// The overview is built from whatever folders the project has; nothing here knows folder names.

const $ = (sel) => document.querySelector(sel);
const state = {
  project: "",
  projects: [],
  sessions: [],
  sessionId: "",
  session: null,
  sending: false,
  attachments: [],
  models: [],
  model: "",
  effort: "medium",
  clis: [],
  live: {},
  overview: null,
  jobs: {},
  preview: null,
  trail: [], // views visited before the current one, for the Back button
  view: { type: "home" }, // {type:"home"} | {type:"vault"} | {type:"folder", path} | {type:"doc", path, editing}
};

// ---------- helpers ----------

async function api(path, options = {}) {
  const sep = path.includes("?") ? "&" : "?";
  const url = path.startsWith("/api/projects") || path.startsWith("/api/config") || path.startsWith("/api/agent/models")
    ? path
    : `${path}${sep}project=${encodeURIComponent(state.project)}`;
  const init = { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const res = await fetch(url, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function fileUrl(rel) {
  const parts = String(rel).split("/").map(encodeURIComponent).join("/");
  return `/projects/${encodeURIComponent(state.project)}/workspace/${parts}`;
}

function dirOf(rel) {
  const i = String(rel || "").lastIndexOf("/");
  return i < 0 ? "" : rel.slice(0, i);
}

function joinPath(base, rel) {
  const out = base ? base.split("/") : [];
  for (const part of rel.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") out.pop();
    else out.push(part);
  }
  return out.join("/");
}

function humanize(name) {
  const stem = String(name).replace(/\.[^.]+$/, "").replace(/^\d+[\s._-]+/, "").replace(/[_-]+/g, " ").trim();
  return stem ? stem[0].toUpperCase() + stem.slice(1) : name;
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast${isError ? " error" : ""}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

function storeGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storeSet(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }

// ---------- markdown ----------

function inline(text, baseDir) {
  let s = esc(text);
  s = s.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (_, alt, src) => `<img alt="${alt}" src="${resolveSrc(src, baseDir)}" loading="lazy">`);
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
    if (/^(https?:|mailto:)/i.test(href)) return `<a href="${href}" target="_blank" rel="noopener">${label}</a>`;
    if (href.startsWith("#")) return label;
    return `<a href="#" data-open="${esc(joinPath(baseDir, decodeURIComponent(href)))}">${label}</a>`;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>").replace(/(^|\W)_([^_\s][^_]*)_(?=\W|$)/g, "$1<em>$2</em>");
  return s;
}

function resolveSrc(src, baseDir) {
  const raw = src.replace(/&amp;/g, "&");
  if (/^(https?:|data:)/i.test(raw)) return esc(raw);
  return esc(fileUrl(joinPath(baseDir, decodeURIComponent(raw))));
}

const LABEL_RE = /^\*\*([^*]{1,40}?):?\*\*:?\s+(.+)$/;

function kvRow(match, baseDir) {
  return `<div class="kv-row"><span class="kv-k">${inline(match[1], baseDir)}</span><span class="kv-v">${inline(match[2], baseDir)}</span></div>`;
}

// "On this page" links for documents with several sections.
function withToc(html) {
  const heads = [...html.matchAll(/<h2 id="([^"]+)">(.*?)<\/h2>/g)];
  if (heads.length < 3) return `<div class="md">${html}</div>`;
  return `<div class="doc-layout"><div class="md">${html}</div><nav class="toc"><span>On this page</span>${heads.map((m) => `<a href="#" data-toc="${m[1]}">${m[2].replace(/<[^>]+>/g, "")}</a>`).join("")}</nav></div>`;
}

function renderMarkdown(md, baseDir = "") {
  const text = String(md || "").replace(/\r\n/g, "\n").replace(/^---\n[\s\S]*?\n---\n/, "");
  const lines = text.split("\n");
  const html = [];
  let i = 0;
  const isTableSep = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = line.match(/^```/);
    if (fence) {
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      i++;
      html.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const n = h[1].length;
      const id = n <= 3 ? ` id="h-${h[2].toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}"` : "";
      html.push(`<h${n}${id}>${inline(h[2], baseDir)}</h${n}>`);
      i++;
      continue;
    }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { html.push("<hr>"); i++; continue; }
    if (line.startsWith(">")) {
      const buf = [];
      while (i < lines.length && lines[i].startsWith(">")) buf.push(lines[i++].replace(/^>\s?/, ""));
      html.push(`<blockquote>${renderMarkdown(buf.join("\n"), baseDir)}</blockquote>`);
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      html.push(`<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${inline(c, baseDir)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c, baseDir)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    const list = line.match(/^(\s*)([-*+]|\d+[.)])\s+/);
    if (list) {
      const ordered = /\d/.test(list[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (m) { items.push({ depth: m[1].length, text: m[3] }); i++; continue; }
        if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) { items[items.length - 1].text += ` ${lines[i].trim()}`; i++; continue; }
        break;
      }
      // "- **Label:** text" lists read better as a label/value block.
      const labelled = items.map((it) => it.text.match(LABEL_RE));
      if (!ordered && items.length > 1 && labelled.every(Boolean)) {
        html.push(`<div class="kv">${labelled.map((m) => kvRow(m, baseDir)).join("")}</div>`);
        continue;
      }
      const tag = ordered ? "ol" : "ul";
      html.push(`<${tag}>${items.map((it) => {
        const box = it.text.match(/^\[([ xX])\]\s+(.*)$/);
        const body = box ? `<input type="checkbox" disabled ${box[1] !== " " ? "checked" : ""}>${inline(box[2], baseDir)}` : inline(it.text, baseDir);
        return `<li${it.depth >= 2 ? ' style="margin-left:1.2em"' : ""}>${body}</li>`;
      }).join("")}</${tag}>`);
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|>|\s*([-*+]|\d+[.)])\s)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) { buf.push(lines[i++]); }
    const label = buf.length === 1 && buf[0].match(LABEL_RE);
    if (label) {
      // Consecutive "**Label:** text" paragraphs join into one block.
      const prev = html[html.length - 1];
      if (prev?.startsWith('<div class="kv">')) html[html.length - 1] = prev.replace(/<\/div>$/, `${kvRow(label, baseDir)}</div>`);
      else html.push(`<div class="kv">${kvRow(label, baseDir)}</div>`);
      continue;
    }
    html.push(`<p>${inline(buf.join(" "), baseDir)}</p>`);
  }
  return html.join("\n");
}

// Front pages repeat their title and pitch in the hero; drop those from the body.
function stripLead(md) {
  const body = String(md || "").replace(/\r\n/g, "\n").replace(/^---\n[\s\S]*?\n---\n/, "");
  const lines = body.split("\n");
  let i = 0;
  while (i < lines.length && !lines[i].trim()) i++;
  if (lines[i] && /^#\s/.test(lines[i])) i++;
  while (i < lines.length && !lines[i].trim()) i++;
  if (lines[i] && !/^(#|>|\||-|\*|!|```|<|\d+\.)/.test(lines[i].trim())) {
    while (i < lines.length && lines[i].trim()) i++;
  }
  return lines.slice(i).join("\n");
}

// ---------- projects ----------

async function loadProjects(preferred) {
  const data = await api("/api/projects");
  state.projects = data.projects || [];
  const remembered = preferred || storeGet("forge.project") || data.default_project;
  state.project = state.projects.includes(remembered) ? remembered : (state.projects[0] || "default");
  $("#projectName").textContent = humanize(state.project);
  storeSet("forge.project", state.project);
}

// ---------- projects page ----------

const projectFileUrl = (project, rel) => `/projects/${encodeURIComponent(project)}/workspace/${String(rel).split("/").map(encodeURIComponent).join("/")}`;

function timeAgo(seconds) {
  if (!seconds) return "Empty";
  const diff = Date.now() / 1000 - seconds;
  if (diff < 60) return "Just now";
  const steps = [[3600, 60, "minute"], [86400, 3600, "hour"], [86400 * 30, 86400, "day"], [86400 * 365, 86400 * 30, "month"]];
  for (const [limit, unit, word] of steps) if (diff < limit) { const n = Math.floor(diff / unit); return `${n} ${word}${n === 1 ? "" : "s"} ago`; }
  return `${Math.floor(diff / (86400 * 365))} years ago`;
}

async function openProjects() {
  $("#projectsView").hidden = false;
  $("#closeProjectsBtn").hidden = !state.project;
  $("#projectGrid").innerHTML = `<div class="pv-loading">Loading projects…</div>`;
  try { state.cards = (await api("/api/projects/cards")).projects; } catch (err) { toast(err.message, true); state.cards = []; }
  renderProjectGrid();
}

function closeProjects() { $("#projectsView").hidden = true; }

function renderProjectGrid() {
  const query = $("#projectSearch").value.trim().toLowerCase();
  const sort = $("#projectSort").value;
  const cards = (state.cards || [])
    .filter((c) => !query || `${c.title} ${c.project} ${c.excerpt}`.toLowerCase().includes(query))
    .sort((a, b) => sort === "name" ? a.title.localeCompare(b.title) : b.updated - a.updated);
  $("#projectGrid").innerHTML = `
    <button class="pcard pcard-new" data-new-project><span>New project</span><i>+</i></button>
    ${cards.map((c) => {
      const imgs = c.images.map((img) => `<img src="${esc(projectFileUrl(c.project, img))}" alt="" loading="lazy">`);
      const initials = c.title.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
      return `<div class="pcard${c.project === state.project ? " current" : ""}" data-open-project="${esc(c.project)}" tabindex="0" role="button">
        <div class="pcard-head"><div><strong>${esc(c.title)}</strong><small>${esc(timeAgo(c.updated))}</small>
          <small>${c.sections} section${c.sections === 1 ? "" : "s"} · ${c.assets} asset${c.assets === 1 ? "" : "s"}</small></div>
          ${c.project === "default" ? "" : `<button class="pcard-del" data-delete-project="${esc(c.project)}" title="Delete project" aria-label="Delete ${esc(c.title)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg></button>`}</div>
        <div class="pcard-art n${Math.min(imgs.length, 3)}">${imgs.length ? imgs.join("") : `<span class="pcard-initials">${esc(initials || "F")}</span>`}</div>
        ${c.project === state.project ? `<span class="pcard-badge">Open</span>` : ""}
      </div>`;
    }).join("")}`;
}

function deleteProjectDialog(project) {
  const card = (state.cards || []).find((c) => c.project === project);
  openDialog(`
    <form class="dlg" id="deleteProjectForm">
      <h2>Delete ${esc(card?.title || humanize(project))}?</h2>
      <p>The project is moved to <code>projects/.trash/</code>, so you can still get it back from disk.</p>
      <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="danger">Delete project</button></div>
    </form>`);
  $("#deleteProjectForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api(`/api/projects/${encodeURIComponent(project)}`, { method: "DELETE" });
      closeDialog();
      toast("Project moved to trash");
      if (project === state.project) {
        storeSet("forge.project", "");
        await loadProjects();
        await switchProject(state.project);
      }
      await openProjects();
    } catch (err) { toast(err.message, true); }
  });
}

// ---------- new project onboarding ----------

const GENRES = ["Action RPG", "Survival horror", "Cozy sim", "Platformer", "Strategy", "Puzzle", "Racing", "Roguelike", "Visual novel", "Shooter"];
const LOOKS = ["Low-poly", "Stylized hand-painted", "Realistic PBR", "Toon / cel-shaded", "Pixel art", "Voxel", "Dark fantasy", "Sci-fi hard-surface"];

function startOnboarding() {
  closeProjects();
  state.ob = { step: 0, name: "", pitch: "", genres: [], looks: [], dimension: "3D", rules: "" };
  $("#onboarding").hidden = false;
  $("#onboarding").classList.remove("forging");
  spawnPetals();
  renderOnboarding();
}

function spawnPetals() {
  const box = $("#onboarding .petals");
  if (box.childElementCount) return;
  box.innerHTML = Array.from({ length: 22 }, (_, i) => `<i style="--x:${Math.random() * 100}%;--d:${8 + Math.random() * 10}s;--delay:${-Math.random() * 18}s;--s:${0.5 + Math.random() * 0.9};--r:${Math.random() * 360}deg;--sway:${20 + Math.random() * 60}px"></i>`).join("");
}

function chipGroup(name, options, selected) {
  return `<div class="ob-chips">${options.map((o) => `<button type="button" class="ob-chip${selected.includes(o) ? " on" : ""}" data-ob-chip="${esc(name)}" data-value="${esc(o)}">${esc(o)}</button>`).join("")}</div>`;
}

function renderOnboarding() {
  const ob = state.ob;
  const steps = [
    () => `<div class="ob-step intro"><img class="ob-glob" src="/ui/glob-forge.svg" alt=""><p class="ob-kicker">Glob</p><h1>Every world starts<br>with a <em>spark</em>.</h1>
      <p class="ob-sub">Answer three quick questions and the agent builds your project: sections, pages, first assets and a style bible.</p>
      <button class="ob-next" data-ob-next>Begin</button></div>`,
    () => `<div class="ob-step"><p class="ob-kicker">1 / 3 · Name</p><h1>What's it called?</h1>
      <input id="obName" class="ob-input" maxlength="60" placeholder="The Hollow Below" value="${esc(ob.name)}" autofocus>
      <button class="ob-next" data-ob-next>Continue</button></div>`,
    () => `<div class="ob-step"><p class="ob-kicker">2 / 3 · The game</p><h1>Tell me about it.</h1>
      <textarea id="obPitch" class="ob-input" rows="3" placeholder="A cozy farming game on floating islands where you tame storm spirits…">${esc(ob.pitch)}</textarea>
      ${chipGroup("genres", GENRES, ob.genres)}
      <div class="ob-nav"><button class="ob-back" data-ob-back>Back</button><button class="ob-next" data-ob-next>Continue</button></div></div>`,
    () => `<div class="ob-step"><p class="ob-kicker">3 / 3 · The look</p><h1>How should it look?</h1>
      ${chipGroup("looks", LOOKS, ob.looks)}
      <div class="ob-dim">${["3D", "2D"].map((d) => `<button type="button" class="ob-chip${ob.dimension === d ? " on" : ""}" data-ob-dim="${d}">${d}</button>`).join("")}</div>
      <textarea id="obRules" class="ob-input small" rows="3" placeholder="Asset rules for every image, e.g. T-pose, front view, under 10k triangles, muted palette…">${esc(ob.rules)}</textarea>
      <div class="ob-nav"><button class="ob-back" data-ob-back>Back</button><button class="ob-next forge" data-ob-forge>✦ Forge my world</button></div></div>`,
  ];
  $("#obStage").innerHTML = steps[ob.step]();
  $("#obStage").firstElementChild.classList.add("enter");
  setTimeout(() => $("#obStage [autofocus], #obStage textarea")?.focus(), 350);
}

function syncOnboarding() {
  const ob = state.ob;
  if ($("#obName")) ob.name = $("#obName").value.trim();
  if ($("#obPitch")) ob.pitch = $("#obPitch").value.trim();
  if ($("#obRules")) ob.rules = $("#obRules").value.trim();
}

async function createProject(name) {
  const created = await api("/api/projects", { method: "POST", body: { name: name || "Untitled project", unique: true } });
  state.project = created.project;
  storeSet("forge.project", created.project);
  return created.project;
}

async function skipOnboarding() {
  syncOnboarding();
  try {
    const project = await createProject(state.ob.name);
    $("#onboarding").hidden = true;
    await switchProject(project);
    toast(`Created ${humanize(project)}: a blank workspace`);
  } catch (err) { toast(err.message, true); }
}

async function forgeWorld() {
  syncOnboarding();
  const ob = state.ob;
  if (!ob.name) { ob.step = 1; renderOnboarding(); toast("Give your world a name first.", true); return; }
  $("#onboarding").classList.add("forging");
  $("#obStage").innerHTML = `<div class="ob-step forging-step enter"><div class="ob-orb"><span></span><span></span><span></span></div>
    <p class="ob-kicker">Forging</p><h1>${esc(ob.name)}</h1><ul class="ob-log" id="obLog"><li>Creating the project…</li></ul></div>`;
  const log = (text, cls = "") => { $("#obLog").insertAdjacentHTML("beforeend", `<li class="${cls}">${text}</li>`); };
  let poll;
  try {
    const project = await createProject(ob.name);
    const look = [ob.dimension, ...ob.looks].join(", ");
    const rules = [look && `- Look: ${look}`, ...ob.rules.split(/\n|;/).map((r) => r.trim()).filter(Boolean).map((r) => `- ${r}`)].filter(Boolean);
    if (rules.length) {
      await api("/api/overview/file", { method: "PUT", body: { path: "pillars/style-bible.md", content: `# Style bible\n\n${rules.join("\n")}\n` } });
      log("Style bible written: put in front of every image prompt", "ok");
    }
    const session = await api("/api/agent/sessions", { method: "POST", body: { title: "Project setup" } });
    log("The agent is designing your project…");
    const seen = new Set();
    poll = setInterval(async () => {
      try {
        const ov = await api("/api/overview");
        for (const s of ov.sections) if (!seen.has(s.path)) { seen.add(s.path); log(`＋ ${esc(s.number)} · ${esc(s.title)}`, "ok"); }
      } catch { /* keep polling */ }
    }, 1500);
    const text = [
      `Create a project structure for my game "${ob.name}".`,
      ob.pitch && `Pitch: ${ob.pitch}`,
      ob.genres.length && `Genre: ${ob.genres.join(", ")}.`,
      look && `Look: ${look}.`,
      rules.length && "A style bible already exists at pillars/style-bible.md; keep it and refine it if useful.",
      "Start with the root README.md front page (title and a one-line pitch), then the sections, then a few first assets in the collections.",
    ].filter(Boolean).join("\n");
    const result = await api(`/api/agent/sessions/${session.id}/messages`, { method: "POST", body: { text, model: state.model || undefined } });
    clearInterval(poll);
    const ov = await api("/api/overview").catch(() => ({ sections: [] }));
    for (const sec of ov.sections) if (!seen.has(sec.path)) log(`＋ ${esc(sec.number)} · ${esc(sec.title)}`, "ok");
    const done = (result.messages?.at(-1)?.actions || []).filter((a) => a.changed).length;
    log(`Done: ${done} change${done === 1 ? "" : "s"} made`, "ok");
  } catch (err) {
    clearInterval(poll);
    log(`The agent stopped: ${esc(err.message)}. Your project is still created; you can continue in the chat.`, "bad");
  }
  $("#obLog").insertAdjacentHTML("afterend", `<button class="ob-next forge" data-ob-enter>Enter your world →</button>`);
}

async function finishOnboarding() {
  $("#onboarding").hidden = true;
  await switchProject(state.project);
}

async function switchProject(name) {
  await loadProjects(name);
  state.view = { type: "home" };
  state.trail = [];
  await loadJobs();
  await Promise.all([loadSessions(), loadOverview()]);
}

// ---------- chat ----------

async function loadSessions() {
  const data = await api("/api/agent/sessions");
  state.sessions = (data.sessions || []).filter((s) => s.group !== "Archive" || s.message_count);
  for (const s of state.sessions) if (s.running && !state.live[s.id]) state.live[s.id] = { text: "", tools: [] };
  const remembered = storeGet(`forge.session.${state.project}`);
  const pick = state.sessions.find((s) => s.id === remembered) || state.sessions[state.sessions.length - 1];
  if (pick) await openSession(pick.id);
  else { state.sessionId = ""; state.session = null; renderTabs(); renderMessages(); }
}

async function openSession(id) {
  state.sessionId = id;
  state.session = await api(`/api/agent/sessions/${id}`);
  storeSet(`forge.session.${state.project}`, id);
  if (state.session.model && state.models.some((m) => m.id === state.session.model && m.ready)) state.model = state.session.model;
  if (state.session.effort) state.effort = state.session.effort;
  renderModelPill();
  renderTabs();
  renderMessages();
}

async function ensureSession() {
  if (state.sessionId) return state.sessionId;
  const created = await api("/api/agent/sessions", { method: "POST", body: { title: "New chat", model: state.model, effort: state.effort, agent: state.newAgent || "glob" } });
  state.newAgent = "glob";
  state.sessions.push(created);
  state.sessionId = created.id;
  state.session = created;
  storeSet(`forge.session.${state.project}`, created.id);
  renderTabs();
  return created.id;
}

async function newChat(agent = "glob") {
  state.sessionId = "";
  state.session = null;
  state.newAgent = agent;
  await ensureSession();
  renderMessages();
  $("#input").focus();
}

async function deleteChat(id) {
  if (!confirm("Delete this chat? It moves to the project's .trash folder.")) return;
  await api(`/api/agent/sessions/${id}`, { method: "DELETE" });
  state.sessions = state.sessions.filter((s) => s.id !== id);
  if (state.sessionId === id) {
    state.sessionId = "";
    state.session = null;
    const last = state.sessions[state.sessions.length - 1];
    if (last) await openSession(last.id);
    else { renderTabs(); renderMessages(); }
  } else renderTabs();
}

function renderTabs() {
  const tabs = state.sessions.map((s) => `
    <button class="tab${s.id === state.sessionId ? " active" : ""}${state.live[s.id] ? " running" : ""}" data-session="${s.id}" role="tab" title="${esc(s.title)}">
      ${agentIcon(s.agent)}<span>${esc(s.title || "New chat")}</span><span class="x" data-delete="${s.id}" aria-label="Delete chat">×</span>
    </button>`).join("");
  $("#sessionTabs").innerHTML = `${tabs}<button class="tab add" id="newChatBtn" title="New chat" aria-label="New chat">+</button>`;
}

const CLI_BADGE = { claude: ["C", "#d97757"], codex: ["X", "#10a37f"], opencode: ["O", "#a1a1aa"], gemini: ["G", "#4f8cff"] };

function agentIcon(agent) {
  if (!agent || agent === "glob") return `<img src="/ui/glob-face.svg" alt="">`;
  const [letter, color] = CLI_BADGE[agent] || ["?", "#71717a"];
  return `<span class="cli-badge" style="--c:${color}">${letter}</span>`;
}

function agentName(agent) {
  return agent && agent !== "glob" ? (state.clis.find((c) => c.id === agent)?.label || agent) : "Glob";
}

async function loadClis() {
  try { state.clis = (await api("/api/agent/clis")).clis; } catch { state.clis = []; }
}

function renderNewTabPop() {
  $("#newTabPop").innerHTML = `
    <div class="pop-group">New chat with</div>
    <button type="button" class="pop-model" data-new-chat="glob">${agentIcon("glob")}<span>Glob<small>organizes and generates your project</small></span></button>
    <div class="pop-group">Coding agents · your own login</div>
    ${state.clis.map((c) => c.installed
      ? `<button type="button" class="pop-model" data-new-chat="${c.id}">${agentIcon(c.id)}<span>${esc(c.label)}<small>works in this project folder</small></span></button>`
      : `<button type="button" class="pop-model off" data-settings-coding>${agentIcon(c.id)}<span>${esc(c.label)}<small>not installed · set up</small></span></button>`).join("")}`;
}

const SUGGESTIONS = [
  ["✦", "Create a project structure for my game. It's about "],
  ["◎", "Look at my project and suggest what's missing."],
  ["✎", "Write a short pitch and a feature list on the front page."],
];
const TOOL_ICON = { write: "✎", append: "✎", create_folder: "▣", move: "↦", delete: "✕", generate: "✦", pick_winner: "★" };

function renderMessages() {
  const box = $("#messages");
  const messages = state.session?.messages || [];
  const noModel = !state.models.some((m) => m.ready);
  const banner = noModel ? `<div class="banner">Glob needs a brain first. <button class="ghost" data-settings>Connect a model</button></div>` : "";
  const agent = state.session?.agent || "glob";
  const live = state.live[state.sessionId];
  if (agent !== "glob" && !messages.length && !live) {
    box.innerHTML = `<div class="chat-empty cli">
      <div class="cli-hero">${agentIcon(agent)}</div>
      <h2>${esc(agentName(agent))}</h2>
      <p>Runs in this project's folder with your own ${esc(agentName(agent))} login. Glob keeps the overview on the right up to date while it works.</p>
    </div>`;
    syncComposer();
    return;
  }
  if (!messages.length && !state.sending) {
    box.innerHTML = `${banner}<div class="chat-empty">
      <img class="hero-glob" src="/ui/glob-forge.svg" alt="">
      <h2>What are we forging today?</h2>
      <p>Tell me about your game. I build and organize the project on the right and make images and 3D for your assets.</p>
      <div class="suggestions">${SUGGESTIONS.map(([icon, s]) => `<button class="suggestion" data-suggest="${esc(s)}"><i>${icon}</i><span>${esc(s.trim())}${s.endsWith(" ") ? "…" : ""}</span></button>`).join("")}</div>
    </div>`;
    return;
  }
  const thinking = live
    ? `<div class="msg agent thinking cli">${avatarFor(agent)}<div class="bubble"><div class="who">${esc(agentName(agent))}</div>${live.text ? `<div class="md">${renderMarkdown(live.text)}</div>` : ""}${live.tools.length ? `<ul class="live-tools">${live.tools.slice(-6).map((t) => `<li><i>⚙</i>${esc(t)}</li>`).join("")}</ul>` : ""}<span class="shimmer">Working…</span></div></div>`
    : state.sending ? `<div class="msg agent thinking"><img class="avatar forge" src="/ui/glob-forge.svg" alt=""><div class="bubble"><div class="who">Glob</div><span class="shimmer">Forging…</span></div></div>` : "";
  box.innerHTML = (agent === "glob" ? banner : "") + messages.map((m) => renderMessage(m, agent)).join("") + thinking;
  box.scrollTop = box.scrollHeight;
  syncComposer();
}

function avatarFor(agent) {
  return agent && agent !== "glob" ? `<span class="avatar">${agentIcon(agent)}</span>` : `<img class="avatar" src="/ui/glob-face.svg" alt="">`;
}

function renderMessage(m, agent = "glob") {
  if (m.role === "user") {
    const imgs = (m.attachments || []).map((a) => `<img class="thumb" src="${esc(fileUrl(a.path))}" alt="">`).join("");
    return `<div class="msg user"><div class="bubble">${esc(m.text)}${imgs}</div></div>`;
  }
  if (m.role === "note") return `<div class="msg note">${esc(m.text)}</div>`;
  const actions = (m.actions || []).filter((a) => (a.tool !== "list" && a.tool !== "read" && a.tool !== "legacy") || a.ok === false);
  const failed = actions.filter((a) => a.ok === false).length;
  const rows = actions.map((a) => {
    const openable = a.ok !== false && a.path && a.tool !== "delete";
    return `<li class="${a.ok === false ? "bad" : ""}${openable ? " link" : ""}"${openable ? ` data-open="${esc(a.path)}"` : ""}><i>${a.ok === false ? "!" : TOOL_ICON[a.tool] || "•"}</i><span>${esc(a.summary)}</span></li>`;
  }).join("");
  const card = actions.length ? `<details class="changes"${actions.length <= 3 ? " open" : ""}><summary><span class="spark">✦</span>${actions.length} change${actions.length === 1 ? "" : "s"}${failed ? ` <em>${failed} failed</em>` : ""}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></summary><ul>${rows}</ul></details>` : "";
  return `<div class="msg agent${m.error ? " error" : ""}">${avatarFor(agent)}<div class="bubble"><div class="who">${esc(agentName(agent))}${m.model && agent === "glob" ? ` <small>${esc(m.model)}</small>` : ""}</div><div class="md">${renderMarkdown(m.text)}</div>${card}</div></div>`;
}

function syncComposer() {
  const agent = state.session?.agent || state.newAgent || "glob";
  const running = Boolean(state.live[state.sessionId]);
  $("#cliPill").hidden = true;
  renderModelPill();
  $("#sendBtn").hidden = running;
  $("#stopBtn").hidden = !running;
  $("#input").placeholder = agent === "glob" ? "Ask Glob to build, change or generate anything…" : `Tell ${agentName(agent)} what to do in this project…`;
  const used = state.session?.context_tokens || 0;
  const limit = state.agentCfg?.agent_compact_at_tokens || 48000;
  $("#contextMeter").innerHTML = agent === "glob" && used
    ? `<span class="meter"><i style="width:${Math.min(100, (used / limit) * 100)}%"></i></span>Context ${(used / 1000).toFixed(1)}k / ${limit / 1000}k · <button type="button" class="link-btn" data-compact-now>Compact now</button>`
    : "Enter to send · Shift+Enter for a new line";
}

function renderAttachments() {
  $("#attachments").innerHTML = state.attachments.map((a, i) => `<figure><img src="${a.data_url}" alt="${esc(a.name)}"><button type="button" data-unattach="${i}" aria-label="Remove">×</button></figure>`).join("");
}

function addFiles(files) {
  for (const file of [...files].slice(0, 4 - state.attachments.length)) {
    if (!file.type.startsWith("image/")) continue;
    const reader = new FileReader();
    reader.onload = () => { state.attachments.push({ name: file.name, data_url: reader.result }); renderAttachments(); };
    reader.readAsDataURL(file);
  }
}

async function sendCli(text) {
  const id = state.sessionId;
  state.session.messages = [...(state.session.messages || []), { role: "user", text, attachments: [] }];
  state.live[id] = { text: "", tools: [] };
  $("#input").value = "";
  autoGrow();
  renderTabs();
  renderMessages();
  try {
    await api(`/api/agent/sessions/${id}/messages`, { method: "POST", body: { text } });
  } catch (err) {
    delete state.live[id];
    toast(err.message, true);
    await openSession(id).catch(() => {});
  }
}

async function stopCli() {
  try { await api(`/api/agent/sessions/${state.sessionId}/stop`, { method: "POST" }); } catch (err) { toast(err.message, true); }
}

async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (state.session?.agent && state.session.agent !== "glob") {
    if (text && !state.live[state.sessionId]) await sendCli(text);
    return;
  }
  if ((!text && !state.attachments.length) || state.sending) return;
  state.sending = true;
  $("#sendBtn").disabled = true;
  const attachments = state.attachments;
  try {
    const id = await ensureSession();
    state.session.messages = [...(state.session.messages || []), { role: "user", text, attachments: [] }];
    input.value = "";
    autoGrow();
    state.attachments = [];
    renderAttachments();
    renderMessages();
    const page = state.view.path ? { viewing: state.view.path } : {};
    state.session = await api(`/api/agent/sessions/${id}/messages`, { method: "POST", body: { text, attachments, page, model: state.model || undefined, effort: state.effort } });
    const entry = state.sessions.find((s) => s.id === id);
    if (entry) entry.title = state.session.title;
    renderTabs();
    const changed = (state.session.messages.at(-1)?.actions || []).some((a) => a.changed);
    if (changed) await loadOverview();
  } catch (err) {
    toast(err.message, true);
    if (state.sessionId) await openSession(state.sessionId).catch(() => {});
  } finally {
    state.sending = false;
    $("#sendBtn").disabled = false;
    renderMessages();
  }
}

// ---------- models & settings ----------

async function loadModels() {
  try {
    const data = await api("/api/agent/models");
    state.models = data.models || [];
    state.modelInfo = data;
    const ready = state.models.filter((m) => m.ready);
    if (!ready.some((m) => m.id === state.model)) state.model = ready.some((m) => m.id === data.selected) ? data.selected : (ready[0]?.id || "");
    if (data.effort && !state.sessionId) state.effort = data.effort;
  } catch {
    state.models = [];
  }
  renderModelPill();
}

const EFFORT_LABEL = { low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Max" };

function currentModel() { return state.models.find((m) => m.id === state.model); }

// Coding-agent tabs pick from their CLI's own models; "" keeps the CLI's default.
function cliAgent() {
  const agent = state.session?.agent || state.newAgent || "glob";
  return agent === "glob" ? "" : agent;
}

async function cliModels(cli) {
  state.cliModels ||= {};
  if (!state.cliModels[cli]) {
    state.cliModels[cli] = { models: [], efforts: [], loading: true };
    try { state.cliModels[cli] = await api(`/api/agent/clis/${cli}/models`); } catch { state.cliModels[cli] = { models: [], efforts: [] }; }
    if (cliAgent() === cli && !$("#modelPop").hidden) renderModelPop();
  }
  return state.cliModels[cli];
}

function renderModelPill() {
  const cli = cliAgent();
  if (cli) {
    const model = state.session?.cli_model || "";
    const label = state.cliModels?.[cli]?.models?.find((x) => x.id === model)?.label || model || "Default model";
    const effort = state.session?.cli_effort;
    $("#statusDot").className = "dot live";
    $("#modelPillLabel").innerHTML = `${esc(agentName(cli))} · ${esc(label)}${effort ? ` <em>· ${EFFORT_LABEL[effort] || effort}</em>` : ""}`;
    return;
  }
  const m = currentModel();
  const ready = state.models.some((x) => x.ready);
  $("#statusDot").className = `dot ${ready ? "live" : "off"}`;
  $("#modelPillLabel").innerHTML = m
    ? `${esc(m.label)}${m.efforts?.length ? ` <em>· ${EFFORT_LABEL[state.effort] || "Medium"}</em>` : ""}`
    : ready ? "Pick a model" : "Connect a model";
}

function renderCliPop(cli) {
  const info = state.cliModels?.[cli];
  if (!info) { cliModels(cli); }
  const model = state.session?.cli_model || "";
  const effort = state.session?.cli_effort || "";
  const groups = {};
  for (const m of info?.models || []) (groups[m.group || agentName(cli)] ||= []).push(m);
  const row = (id, label, sub = "") => `<button type="button" class="pop-model${id === model ? " on" : ""}" data-pick-cli-model="${esc(id)}"><span>${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ""}</span>${id === model ? "<b>✓</b>" : ""}</button>`;
  const custom = model && !(info?.models || []).some((m) => m.id === model);
  $("#modelPop").innerHTML = `
    <div class="pop-group">${esc(agentName(cli))}</div>
    ${row("", "Default model", "whatever is set up in the CLI")}
    ${custom ? row(model, model, "typed in") : ""}
    ${!info || info.loading ? `<p class="pop-empty">Loading models…</p>` : ""}
    ${Object.entries(groups).map(([group, items]) => `${Object.keys(groups).length > 1 || group !== agentName(cli) ? `<div class="pop-group">${esc(group)}</div>` : ""}${items.map((m) => row(m.id, m.label)).join("")}`).join("")}
    <form class="pop-custom" data-cli-custom><input name="model" placeholder="Other model id…" autocomplete="off" spellcheck="false"><button>Use</button></form>
    ${info?.efforts?.length ? `<div class="pop-group">Reasoning effort</div><div class="effort-seg">${["", ...info.efforts].map((e) => `<button type="button" class="${e === effort ? "on" : ""}" data-pick-cli-effort="${e || "default"}">${e ? EFFORT_LABEL[e] || e : "Default"}</button>`).join("")}</div>` : ""}`;
}

async function pickCli(fields) {
  if (!state.session) return;
  Object.assign(state.session, fields);
  renderModelPill();
  renderModelPop();
  if (!state.sessionId) return;
  try { await api(`/api/agent/sessions/${state.sessionId}`, { method: "PATCH", body: fields }); } catch (err) { toast(err.message, true); }
}

function renderModelPop() {
  const cli = cliAgent();
  if (cli) return renderCliPop(cli);
  const groups = {};
  for (const m of state.models.filter((x) => x.ready)) (groups[m.group || m.provider] ||= []).push(m);
  const m = currentModel();
  $("#modelPop").innerHTML = `
    ${Object.keys(groups).length ? Object.entries(groups).map(([group, items]) => `<div class="pop-group">${esc(group)}</div>
      ${items.map((x) => `<button type="button" class="pop-model${x.id === state.model ? " on" : ""}" data-pick-model="${esc(x.id)}"><span>${esc(x.label)}</span>${x.id === state.model ? "<b>✓</b>" : ""}</button>`).join("")}`).join("")
      : `<p class="pop-empty">No model connected yet.</p>`}
    ${m?.efforts?.length ? `<div class="pop-group">Reasoning effort</div><div class="effort-seg">${m.efforts.map((e) => `<button type="button" class="${e === state.effort ? "on" : ""}" data-pick-effort="${e}">${EFFORT_LABEL[e]}</button>`).join("")}</div>` : ""}
    <button type="button" class="pop-add" data-settings>＋ Add a model…</button>`;
}

function toggleModelPop(force) {
  const pop = $("#modelPop");
  const open = force ?? pop.hidden;
  if (open) renderModelPop();
  pop.hidden = !open;
}

async function pickModel(id) {
  state.model = id;
  const m = currentModel();
  if (m && !m.efforts?.length) state.effort = "medium";
  renderModelPill();
  renderModelPop();
  try {
    await api("/api/config", { method: "PUT", body: { agent_model: id } });
    if (state.sessionId) await api(`/api/agent/sessions/${state.sessionId}`, { method: "PATCH", body: { model: id, effort: state.effort } });
  } catch (err) { toast(err.message, true); }
}

async function pickEffort(effort) {
  state.effort = effort;
  renderModelPill();
  renderModelPop();
  try {
    await api("/api/config", { method: "PUT", body: { agent_reasoning_effort: effort } });
    if (state.sessionId) await api(`/api/agent/sessions/${state.sessionId}`, { method: "PATCH", body: { effort } });
  } catch (err) { toast(err.message, true); }
}

function autoGrow() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
}

async function settingsDialog(tab) {
  toggleModelPop(false);
  state.settings = { tab: tab || state.settings?.tab || "models", adding: null };
  $("#settingsView").hidden = false;
  await renderSettings();
}

function closeSettings() { $("#settingsView").hidden = true; }

const KEY_PROVIDERS = [
  { key: "gemini", name: "Google Gemini", use: "2D images (Nano Banana)", url: "https://aistudio.google.com/apikey", hint: "AIza…" },
  { key: "meshy", name: "Meshy", use: "3D models and Meshy image models", url: "https://www.meshy.ai/settings/api", hint: "msy_…" },
  { key: "elevenlabs", name: "ElevenLabs", use: "GPT Image, Seedream and more image models", url: "https://elevenlabs.io/app/settings/api-keys", hint: "sk_…" },
];
const PRESET_ICON = { anthropic: "✳", openai: "◯", openrouter: "⇄", gemini: "✦", ollama: "◉", lmstudio: "▣", custom: "⚙" };

async function renderSettings() {
  const st = state.settings;
  await loadModels();
  const info = state.modelInfo || { presets: {}, endpoints: [] };
  const nav = [["models", "AI models"], ["agent", "Agent"], ["coding", "Coding agents"], ["images", "Image & 3D"]].map(([id, label]) => `<button class="${st.tab === id ? "on" : ""}" data-settings-tab="${id}">${label}</button>`).join("");
  let body = "";
  if (st.tab === "models") {
    const ready = state.models.filter((m) => m.ready);
    const groups = {};
    for (const m of ready) (groups[m.group || m.provider] ||= []).push(m);
    body = `
      <h1>AI models</h1><p class="st-lede">Glob's chat runs on any of these. Connect one in a few clicks: pick a provider, paste a key, choose models.</p>
      ${st.adding ? addModelCard(info) : `
        <div class="st-label">Add a model</div>
        <div class="preset-grid">
          ${info.vscode_importable ? `<button class="preset featured" data-import-vscode><i>⇣</i><strong>Import from VS Code</strong><small>${info.vscode_importable} model${info.vscode_importable === 1 ? "" : "s"} found, keys included</small></button>` : ""}
          ${Object.entries(info.presets).map(([id, p]) => `<button class="preset" data-add-preset="${id}"><i>${PRESET_ICON[id] || "•"}</i><strong>${esc(p.label)}</strong><small>${p.needs_key ? "API key" : id === "custom" ? "Any OpenAI-compatible URL" : "No key needed"}</small></button>`).join("")}
        </div>`}
      <div class="st-label">Connected · ${ready.length}</div>
      ${ready.length ? Object.entries(groups).map(([group, items]) => {
        const ep = items[0].endpoint;
        return `<div class="conn-group"><div class="conn-head"><strong>${esc(group)}</strong>${ep ? `<button class="link-btn danger-text" data-remove-endpoint="${esc(ep)}">Remove</button>` : ""}</div>
          ${items.map((m) => `<div class="conn-row"><span class="dot live"></span><span>${esc(m.label)}</span>${m.id === state.model ? `<em class="in-use">In use</em>` : ""}<span class="grow"></span><span class="test-result" id="test-${esc(m.id).replace(/[^a-z0-9]/gi, "_")}"></span><button class="mini" data-test-model="${esc(m.id)}">Test</button></div>`).join("")}</div>`;
      }).join("") : `<p class="st-empty">Nothing connected yet. Pick a provider above.</p>`}`;
  } else if (st.tab === "agent") {
    const cfg = await api("/api/config");
    const opt = (values, current, label = (v) => v) => values.map((v) => `<option value="${v}" ${String(v) === String(current) ? "selected" : ""}>${label(v)}</option>`).join("");
    const k = (v) => `${v / 1000}k tokens`;
    body = `
      <h1>Agent</h1><p class="st-lede">How Glob thinks and how much of the chat it keeps in mind.</p>
      <form id="agentForm" class="agent-form">
        <div class="set-row"><div><strong>Default reasoning effort</strong><small>For new chats. Each chat can change it in the model pill.</small></div>
          <select name="agent_reasoning_effort">${opt(["low", "medium", "high", "max"], cfg.agent_reasoning_effort, (v) => EFFORT_LABEL[v])}</select></div>
        <div class="set-row"><div><strong>Auto-compact long chats</strong><small>When a chat gets long, Glob folds the older messages into a summary so it stays fast and cheap without forgetting decisions.</small></div>
          <label class="switch"><input type="checkbox" name="agent_auto_compact" ${cfg.agent_auto_compact ? "checked" : ""}><span></span></label></div>
        <div class="set-row"><div><strong>Compact when the context reaches</strong><small>Estimated size of the chat sent with each message.</small></div>
          <select name="agent_compact_at_tokens">${opt([16000, 32000, 48000, 64000, 100000, 128000, 200000], cfg.agent_compact_at_tokens, k)}</select></div>
        <div class="set-row"><div><strong>Keep recent messages</strong><small>The newest messages always stay word for word.</small></div>
          <select name="agent_keep_recent_messages">${opt([2, 4, 6, 8, 12, 16], cfg.agent_keep_recent_messages)}</select></div>
        <div class="set-row"><div><strong>Steps per message</strong><small>How many read/write rounds Glob may take for one message.</small></div>
          <select name="agent_max_tool_rounds">${opt([4, 8, 12, 16, 24], cfg.agent_max_tool_rounds)}</select></div>
        <div class="set-row"><div><strong>Coding agents may edit files without asking</strong><small>Claude Code: file edits only. Codex: sandboxed to the workspace. OpenCode and Gemini CLI: auto-approve.</small></div>
          <label class="switch"><input type="checkbox" name="cli_allow_edits" ${cfg.cli_allow_edits ? "checked" : ""}><span></span></label></div>
        <div class="row"><button class="primary">Save</button></div>
      </form>`;
  } else if (st.tab === "coding") {
    await loadClis();
    body = `
      <h1>Coding agents</h1><p class="st-lede">Use Claude Code, Codex, OpenCode or Gemini CLI right inside Glob. Each one runs in your project folder with your own login, in its own chat tab, and several can work at the same time. Glob stays the workspace: the overview updates live while they work.</p>
      <div class="cli-cards">${state.clis.map((c) => `<div class="cli-card${c.installed ? " on" : ""}">
        <div class="cli-top">${agentIcon(c.id)}<div><strong>${esc(c.label)}</strong><small>${c.installed ? `Installed · ${esc(c.path)}` : "Not installed"}</small></div>
          <a href="${esc(c.site)}" target="_blank" rel="noopener">About ↗</a></div>
        ${c.installed
          ? `<div class="cli-actions"><button class="primary" data-new-chat="${c.id}">Open a tab</button><button class="ghost" data-cli-login="${c.id}">Log in</button></div>
             <small class="cli-note">Not logged in yet? <b>Log in</b> opens a terminal with <code>${esc(c.login)}</code>, then come back.</small>`
          : `<div class="cli-actions"><button class="primary" data-cli-login="${c.id}">Install</button><code class="cli-cmd">${esc(c.install)}</code></div>
             <small class="cli-note">Opens a terminal that runs the install command (needs Node.js). Press <b>Refresh</b> when it's done.</small>`}
      </div>`).join("")}</div>
      <div class="row"><button class="ghost" data-settings-tab="coding">Refresh</button></div>
      <p class="st-note">Edits: Claude Code may edit files without asking (not run commands), Codex runs sandboxed to the workspace, OpenCode and Gemini CLI auto-approve file edits. Claude Code also gets Glob's own tools.</p>`;
  } else {
    const cfg = await api("/api/config");
    const studio = await studioOptions();
    body = `
      <h1>Image & 3D</h1><p class="st-lede">Paste a key and Glob can generate 2D images and 3D models. Keys stay on this computer.</p>
      <form id="keysForm" class="key-cards">
        ${KEY_PROVIDERS.map((k) => `<div class="key-card${cfg[`${k.key}_configured`] ? " on" : ""}">
          <div class="key-top"><span class="dot ${cfg[`${k.key}_configured`] ? "live" : ""}"></span><strong>${k.name}</strong><a href="${k.url}" target="_blank" rel="noopener">Get a key ↗</a></div>
          <small>${k.use}</small>
          <input name="${k.key}_api_key" type="password" autocomplete="off" placeholder="${cfg[`${k.key}_configured`] ? `Connected (${esc(cfg[`${k.key}_api_key`])}). Paste to replace` : k.hint}">
        </div>`).join("")}
        <label class="st-select">Default image model
          <select name="default_image_model">${studio.models.map((m) => `<option value="${esc(m.id)}" ${m.id === studio.selected ? "selected" : ""} ${m.available ? "" : "disabled"}>${esc(m.label)} · $${m.price.toFixed(3)}${m.available ? "" : " (no key)"}</option>`).join("")}</select></label>
        <div class="row"><button class="primary">Save</button></div>
      </form>`;
  }
  $("#settingsView").innerHTML = `
    <header class="pv-top"><div class="glob-brand static"><img class="glob-mark" src="/ui/glob.svg" alt=""><span class="glob-word">Glob<small>settings</small></span></div>
      <button class="icon-btn" type="button" data-close-settings title="Close" aria-label="Close">✕</button></header>
    <div class="st-main"><nav class="st-nav">${nav}</nav><div class="st-body">${body}</div></div>`;
  $("#keysForm")?.addEventListener("submit", saveKeys);
  $("#agentForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const updates = {
      agent_reasoning_effort: form.agent_reasoning_effort.value,
      agent_auto_compact: form.agent_auto_compact.checked,
      agent_compact_at_tokens: Number(form.agent_compact_at_tokens.value),
      agent_keep_recent_messages: Number(form.agent_keep_recent_messages.value),
      agent_max_tool_rounds: Number(form.agent_max_tool_rounds.value),
      cli_allow_edits: form.cli_allow_edits.checked,
    };
    try { await api("/api/config", { method: "PUT", body: updates }); state.agentCfg = updates; toast("Saved"); syncComposer(); } catch (err) { toast(err.message, true); }
  });
}

function addModelCard(info) {
  const a = state.settings.adding;
  const preset = info.presets[a.preset];
  const showUrl = a.preset === "custom" || !preset.needs_key;
  return `<div class="add-card">
    <div class="add-head"><i>${PRESET_ICON[a.preset] || "•"}</i><div><strong>Connect ${esc(preset.label)}</strong><small>${preset.key_url ? `<a href="${preset.key_url}" target="_blank" rel="noopener">Get a key ↗</a>` : "Runs on your machine or your own server."}</small></div>
      <button class="link-btn" data-cancel-add>Cancel</button></div>
    ${showUrl ? `<label>Endpoint URL<input id="addUrl" value="${esc(a.base_url ?? preset.base_url)}" placeholder="https://my-server/v1"></label>` : ""}
    ${preset.needs_key || a.preset === "custom" ? `<label>API key${preset.needs_key ? "" : " (if needed)"}<input id="addKey" type="password" autocomplete="off" value="${esc(a.api_key || "")}" placeholder="Paste your key"></label>` : ""}
    ${a.models ? `<label>Name<input id="addName" value="${esc(a.name ?? preset.label)}"></label>
      <div class="model-check-head"><span>${a.models.length} models found · pick the ones you want</span><input id="addFilter" placeholder="Filter…" value="${esc(a.filter || "")}"></div>
      <div class="model-checks">${a.models.filter((m) => !a.filter || m.toLowerCase().includes(a.filter.toLowerCase())).slice(0, 200).map((m) => `<label class="model-check"><input type="checkbox" data-pick-check="${esc(m)}" ${a.picked.includes(m) ? "checked" : ""}><span>${esc(m)}</span></label>`).join("")}</div>
      <div class="row"><button class="primary" data-save-endpoint ${a.picked.length ? "" : "disabled"}>Add ${a.picked.length || ""} model${a.picked.length === 1 ? "" : "s"}</button></div>`
      : `<div class="row"><button class="primary" data-probe>${a.busy ? "Connecting…" : "Connect"}</button></div>`}
  </div>`;
}

function syncAddCard() {
  const a = state.settings.adding;
  if (!a) return;
  if ($("#addUrl")) a.base_url = $("#addUrl").value.trim();
  if ($("#addKey")) a.api_key = $("#addKey").value.trim();
  if ($("#addName")) a.name = $("#addName").value.trim();
  if ($("#addFilter")) a.filter = $("#addFilter").value;
}

async function probeEndpoint() {
  syncAddCard();
  const a = state.settings.adding;
  a.busy = true;
  await renderSettings();
  try {
    const { models } = await api("/api/agent/endpoints/probe", { method: "POST", body: a });
    if (!models.length) throw new Error("Connected, but the endpoint lists no models.");
    a.models = models;
    a.picked = models.length <= 3 ? [...models] : [];
  } catch (err) { toast(err.message, true); }
  a.busy = false;
  await renderSettings();
}

async function saveEndpoint() {
  syncAddCard();
  const a = state.settings.adding;
  try {
    await api("/api/agent/endpoints", { method: "POST", body: { preset: a.preset, name: a.name, base_url: a.base_url, api_key: a.api_key, models: a.picked } });
    state.settings.adding = null;
    toast(`Added ${a.picked.length} model${a.picked.length === 1 ? "" : "s"}`);
    await renderSettings();
    renderMessages();
  } catch (err) { toast(err.message, true); }
}

async function saveKeys(event) {
  event.preventDefault();
  const updates = {};
  for (const [key, value] of new FormData(event.target)) if (String(value).trim()) updates[key] = String(value).trim();
  if (updates.default_image_model) updates.user_locked_image_model = true;
  try {
    await api("/api/config", { method: "PUT", body: updates });
    state.studio = null;
    toast("Saved");
    await renderSettings();
  } catch (err) { toast(err.message, true); }
}

async function testModel(id) {
  const out = $(`#test-${id.replace(/[^a-z0-9]/gi, "_")}`);
  if (out) { out.className = "test-result"; out.textContent = "Testing…"; }
  try {
    await api("/api/agent/test", { method: "POST", body: { model: id } });
    if (out) { out.className = "test-result ok"; out.textContent = "Works"; }
  } catch (err) {
    if (out) { out.className = "test-result bad"; out.textContent = "Failed"; out.title = err.message; }
    toast(err.message, true);
  }
}

function openDialog(html) {
  const dlg = $("#dialog");
  dlg.innerHTML = html;
  dlg.showModal();
}
function closeDialog() { $("#dialog").close(); }

// ---------- overview ----------

async function loadOverview() {
  try {
    state.overview = await api("/api/overview");
  } catch (err) {
    toast(err.message, true);
    return;
  }
  $("#projectName").textContent = projectTitle();
  await renderView();
}

function projectTitle() {
  return state.overview?.title || humanize(state.project);
}

function renderSections() {
  const ov = state.overview;
  const active = state.view.type === "home" || state.view.type === "vault" ? "" : (state.view.path || "").split("/")[0];
  const initials = projectTitle().split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  $("#sections").innerHTML = `
    <div class="proj-card"><span class="proj-badge">${esc(initials || "F")}</span><div><strong>${esc(projectTitle())}</strong><small>${esc(ov.excerpt || "Your game project")}</small></div></div>
    <button class="sec${state.view.type === "home" ? " active" : ""}" data-home><small>00</small>Overview</button>
    <button class="sec vault-sec${state.view.type === "vault" ? " active" : ""}" data-vault>${icon("box")}<span class="sec-name">Final Vault</span></button>
    ${ov.sections.length ? `<div class="sec-label">Sections</div>` : ""}
    ${ov.sections.map((s) => `<button class="sec${s.path === active ? " active" : ""}" data-folder="${esc(s.path)}"><small>${s.number}</small><span class="sec-name">${esc(s.title)}</span>${s.children?.length ? `<span class="caret${s.path === active ? " open" : ""}"></span>` : ""}</button>${s.path === active ? subTree(s.children) : ""}`).join("")}
    <div class="spacer"></div>
    <div class="tip">Ask the agent to add, rename or reorder sections.</div>`;
}

// Sub-folders under a section: the first level is always shown, deeper levels open along the current path.
function subTree(nodes) {
  if (!nodes?.length) return "";
  const cur = state.view.type === "home" ? "" : state.view.path || "";
  const here = state.view.type === "doc" ? dirOf(cur) : cur;
  return `<div class="sub">${nodes.map((n) => {
    const onPath = cur === n.path || cur.startsWith(`${n.path}/`);
    return `<button class="sub-item${here === n.path ? " active" : onPath ? " on" : ""}" data-folder="${esc(n.path)}">${esc(n.title)}</button>${onPath ? subTree(n.children) : ""}`;
  }).join("")}</div>`;
}

function renderCrumbs() {
  const parts = [`<button data-home>Workspace</button>`, `<span class="sep">/</span>`];
  const path = state.view.path || "";
  if (state.view.type === "home") parts.push(`<strong>${esc(projectTitle())}</strong>`);
  else if (state.view.type === "vault") parts.push(`<button data-home>${esc(projectTitle())}</button>`, `<span class="sep">/</span>`, `<strong>Final Vault</strong>`);
  else {
    parts.push(`<button data-home>${esc(projectTitle())}</button>`);
    const segs = path.split("/");
    segs.forEach((seg, idx) => {
      const sub = segs.slice(0, idx + 1).join("/");
      parts.push(`<span class="sep">/</span>`);
      const last = idx === segs.length - 1;
      parts.push(last ? `<strong>${esc(humanize(seg))}</strong>` : `<button data-folder="${esc(sub)}">${esc(humanize(seg))}</button>`);
    });
  }
  $("#crumbs").innerHTML = parts.join("");
  $("#backBtn").disabled = state.view.type === "home";
}

const FOLDER_ICON = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2h8.8A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/></svg>`;

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function folderCards(folders, numbered) {
  if (!folders.length) return "";
  return `<div class="folder-list">${folders.map((f) => {
    const thumbs = f.thumbs?.length ? f.thumbs : f.image ? [f.image] : [];
    const chips = [f.assets ? plural(f.assets, "asset") : "", f.subfolders ? plural(f.subfolders, "folder") : ""].filter(Boolean);
    return `<button class="folder-row" data-folder="${esc(f.path)}">
      <span class="folder-icon">${numbered ? `<small>${f.number}</small>` : FOLDER_ICON}</span>
      <span class="folder-text"><strong>${esc(f.title)}</strong>${f.excerpt ? `<span>${esc(f.excerpt)}</span>` : ""}</span>
      ${thumbs.length ? `<span class="folder-thumbs">${thumbs.map((t) => `<img src="${esc(fileUrl(t))}" alt="" loading="lazy">`).join("")}</span>` : ""}
      <span class="folder-meta">${chips.length ? chips.map((c) => `<em>${c}</em>`).join("") : `<em>${plural(f.count, "file")}</em>`}</span>
      <span class="chev">›</span>
    </button>`;
  }).join("")}</div>`;
}

function assetTiles(assets, folderPath) {
  return `<div class="asset-grid">${assets.map((f) => `
    <button class="asset-tile" data-open="${esc(f.path)}">
      <span class="asset-img">${f.thumb ? `<img src="${esc(fileUrl(f.thumb))}" alt="" loading="lazy">` : `<span class="asset-empty">No image yet</span>`}<span class="kind-tag">${esc(f.asset_kind)}</span></span>
      <strong>${esc(f.title)}</strong>
    </button>`).join("")}
    ${folderPath !== undefined ? `<button class="asset-tile add" data-new-asset="${esc(folderPath)}"><span class="asset-img"><span class="plus">+</span></span><strong>New asset</strong></button>` : ""}</div>`;
}

function fileBlocks(files) {
  const assets = files.filter((f) => f.kind === "asset");
  const docs = files.filter((f) => f.kind === "doc" || f.kind === "file");
  const images = files.filter((f) => f.kind === "image");
  const models = files.filter((f) => f.kind === "model");
  let html = "";
  if (assets.length) html += assetTiles(assets);
  if (docs.length) {
    html += `<div class="grid">${docs.map((f) => `
      <button class="card" data-open="${esc(f.path)}"><div class="body"><strong>${esc(f.title)}</strong>${f.excerpt ? `<p>${esc(f.excerpt)}</p>` : ""}<span class="meta">${esc(f.name)}</span></div></button>`).join("")}</div>`;
  }
  if (images.length) {
    html += `<div class="gallery">${images.map((f) => `<a href="${esc(fileUrl(f.path))}" target="_blank" rel="noopener"><img src="${esc(fileUrl(f.path))}" alt="${esc(f.title)}" loading="lazy"><span>${esc(f.name)}</span></a>`).join("")}</div>`;
  }
  if (models.length) {
    html += `<div class="grid">${models.map((f) => `<a class="card" href="${esc(fileUrl(f.path))}" download><div class="body"><strong>${esc(f.title)}</strong><span class="meta">3D model · ${esc(f.name)}</span></div></a>`).join("")}</div>`;
  }
  return html;
}

function pillarStrip(pillars, folderPath, label, texts = []) {
  const target = folderPath ? `${folderPath}/pillars` : "pillars";
  return `<section class="pillars">
    <div class="pillars-head"><div><strong>${esc(label)}</strong>
      <span>${folderPath ? "Sent with every image generated for assets in this folder and below it." : "The project bible: sent with every image generation."}</span></div>
      <div class="pillars-actions"><button class="ghost" data-style-text="${esc(target)}">+ Style text</button><button class="ghost" data-upload="${esc(target)}">+ Add image</button></div></div>
    ${texts.map((t) => `<div class="bible-text"><button class="bible-open" data-open="${esc(t.path)}"><span>Style bible · put in front of every prompt</span><p>${esc(t.excerpt.replace(/^#\s+.*\n?/, "").trim() || "Empty")}</p></button><div class="bible-actions"><button class="mini" data-open="${esc(t.path)}">Edit</button><button class="mini danger-mini" data-remove-style="${esc(t.path)}">Remove</button></div></div>`).join("")}
    ${pillars.length ? `<div class="pillar-row">${pillars.map((p) => `<div class="pillar-item"><a href="${esc(fileUrl(p))}" target="_blank" rel="noopener" title="${esc(p)}"><img src="${esc(fileUrl(p))}" alt="" loading="lazy"></a><button class="pillar-remove" data-remove-pillar="${esc(p)}" title="Remove pillar" aria-label="Remove pillar">×</button></div>`).join("")}</div>`
      : texts.length ? "" : `<p class="pillar-empty">No pillars yet. Add reference images whose art style, pose and lighting every new image should match, and style text such as pose, polygon budget or palette.</p>`}
  </section>`;
}

async function removeStyleText(path) {
  if (!confirm("Remove this style text? It is moved to .trash in the workspace, so it can be restored.")) return;
  try { await api(`/api/overview/style-text?path=${encodeURIComponent(path)}`, { method: "DELETE" }); toast("Style text removed"); await loadOverview(); } catch (err) { toast(err.message, true); }
}

async function removePillar(path) {
  if (!confirm("Remove this pillar image? It is moved to .trash in the workspace, so it can be restored.")) return;
  try { await api(`/api/overview/pillar?path=${encodeURIComponent(path)}`, { method: "DELETE" }); toast("Pillar removed"); await loadOverview(); } catch (err) { toast(err.message, true); }
}

async function addStyleText(target) {
  const path = `${target}/style-bible.md`;
  const exists = await api(`/api/overview/file?path=${encodeURIComponent(path)}`).then(() => true, () => false);
  if (!exists) {
    const content = `# Style bible\n\n- Pose: T-pose, front view, full body, neutral expression\n- Geometry: low-poly, clean silhouette, under 10k triangles\n- Rendering: soft studio light, plain background\n- Palette: \n`;
    try { await api("/api/overview/file", { method: "PUT", body: { path, content } }); } catch (err) { toast(err.message, true); return; }
  }
  await loadOverview();
  go({ type: "doc", path, editing: true });
}

async function makePillarDialog(image) {
  let targets;
  try { targets = (await api(`/api/studio/pillar-targets?path=${encodeURIComponent(state.view.path)}`)).targets; } catch (err) { toast(err.message, true); return; }
  openDialog(`
    <form class="dlg pillar-pick" id="pillarForm">
      <div class="pillar-pick-head"><img src="${esc(fileUrl(image))}" alt=""><div><h2>Make it a pillar</h2>
        <p>Pick the folder this image should style. It is sent with every generation in that folder and everything below it.</p></div></div>
      <div class="target-list">${targets.map((t, i) => `<label class="target"><input type="radio" name="folder" value="${esc(t.path)}" ${i === targets.length - 1 ? "checked" : ""}>
        <span><strong>${esc(t.path ? humanize(t.path.split("/").pop()) : "Project bible")}</strong><small>${esc(t.path ? t.path : "Every image in the whole project")}</small></span></label>`).join("")}</div>
      <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">Make pillar</button></div>
    </form>`);
  $("#pillarForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const folder = new FormData(event.target).get("folder") || "";
    try {
      await api("/api/studio/make-pillar", { method: "POST", body: { image, folder } });
      closeDialog();
      toast(`Pillar added to ${folder ? humanize(folder.split("/").pop()) : "the project bible"}`);
      renderView();
    } catch (err) { toast(err.message, true); }
  });
}

function uploadTo(folder, onDone) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/png,image/jpeg,image/webp";
  input.multiple = true;
  input.onchange = () => uploadFiles(folder, input.files, onDone);
  input.click();
}

async function uploadFiles(folder, files, onDone) {
  const form = new FormData();
  for (const file of files) if (/^image\/(png|jpeg|webp|gif)$/.test(file.type)) form.append("files", file);
  if (!form.has("files")) { toast("Drop a PNG, JPG or WebP image.", true); return; }
  try {
    const res = await fetch(`/api/overview/upload?project=${encodeURIComponent(state.project)}&path=${encodeURIComponent(folder)}`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    toast(`Added ${data.saved.length} image${data.saved.length === 1 ? "" : "s"}`);
    if (onDone) await onDone(data.saved); else await loadOverview();
  } catch (err) { toast(err.message, true); }
}

// An image dropped on the asset page becomes a candidate and the winner.
async function addCandidateImages(files) {
  const asset = state.view.path;
  await uploadFiles(`${asset.replace(/\.md$/i, "")}/candidates`, files, async (saved) => {
    await api("/api/studio/asset", { method: "PATCH", body: { path: asset, fields: { winner: saved[0] } } });
    toast("Saved as a candidate and picked as the winner");
    renderView();
  });
}

const KIND_ICON = { character: "☺", creature: "◆", item: "◇", weapon: "⚔", prop: "▣", environment: "⛰", vehicle: "➤", ui: "▤", vfx: "✺" };

function kindTags(kinds, selected) {
  return `<div class="kind-tags" role="radiogroup" aria-label="Kind">${kinds.map((k) => `<label class="kind-chip"><input type="radio" name="kind" value="${esc(k)}" ${k === selected ? "checked" : ""} required><span>${KIND_ICON[k] || "•"} ${esc(k === "ui" ? "UI" : k === "vfx" ? "VFX" : k[0].toUpperCase() + k.slice(1))}</span></label>`).join("")}</div>`;
}

async function newAssetDialog(folder) {
  const { kinds } = await studioOptions();
  // Suggest the kind from the folder name (creatures → creature) when it matches one.
  const guess = kinds.find((k) => (folder || "").toLowerCase().split("/").some((part) => part.replace(/^\d+[-_ ]*/, "").replace(/s$/, "").includes(k))) || "";
  openDialog(`
    <form class="dlg" id="newAssetForm">
      <h2>New asset</h2>
      <p>Creates a document in <code>${esc(folder || "the project root")}</code> that you can generate images and 3D for.</p>
      <label>Name<input name="title" required maxlength="80" placeholder="Bronze Knight" autofocus></label>
      <div class="dlg-field"><span>Kind</span>${kindTags(kinds, guess)}</div>
      <label>What does it look like?<textarea name="prompt" rows="3" placeholder="A stout knight in dented bronze armour with a red tabard"></textarea></label>
      <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">Create</button></div>
    </form>`);
  $("#newAssetForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    const slug = data.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "asset";
    const path = folder ? `${folder}/${slug}.md` : `${slug}.md`;
    const yaml = (v) => JSON.stringify(String(v).trim());
    const content = `---\nkind: ${yaml(data.kind)}\nprompt: ${yaml(data.prompt || data.title)}\n---\n\n# ${data.title.trim()}\n`;
    try {
      await api("/api/overview/file", { method: "PUT", body: { path, content } });
      closeDialog();
      await loadOverview();
      go({ type: "doc", path });
    } catch (err) { toast(err.message, true); }
  });
}

async function readPage(path) {
  if (!path) return "";
  try { return (await api(`/api/overview/file?path=${encodeURIComponent(path)}`)).content; } catch { return ""; }
}

async function renderView() {
  const ov = state.overview;
  if (!ov) return;
  if (state.view.type === "doc" && /(^|\/)README\.md$/i.test(state.view.path) && dirOf(state.view.path)) state.view = { type: "folder", path: dirOf(state.view.path) };
  saveView();
  renderSections();
  renderCrumbs();
  const content = $("#content");
  delete content.dataset.dropAsset;
  const view = state.view;

  if (view.type === "home") {
    if (ov.empty) {
      content.innerHTML = `<div class="empty"><h1>This project is empty</h1>
        <p>Tell the agent on the left about your game, for example:</p>
        <p><em>“Create a project structure for a cozy farming game set on a floating island.”</em></p>
        <p>Sections, documents and pages will appear here as the agent creates them.</p></div>`;
      return;
    }
    const body = await readPage(ov.page);
    content.innerHTML = `
      <div class="kicker">Project / ${esc(projectTitle())}</div>
      <h1 class="title">${esc(projectTitle())}</h1>
      ${ov.excerpt ? `<p class="lede">${esc(ov.excerpt)}</p>` : ""}
      ${ov.cover ? `<img class="hero" src="${esc(fileUrl(ov.cover))}" alt="">` : ""}
      ${ov.page ? `<div class="actions-row"><button class="ghost" data-open="${esc(ov.page)}">Open ${esc(ov.page)}</button></div>` : ""}
      ${body ? `<div class="md">${renderMarkdown(stripLead(body), "")}</div>` : ""}
      ${ov.sections.length ? `<h2 class="block-title">Sections</h2>${folderCards(ov.sections, true)}` : ""}
      ${pillarStrip(ov.pillars || [], "", "Bible pillars", ov.text_pillars || [])}
      ${ov.files.length ? `<h2 class="block-title">Files</h2>${fileBlocks(ov.files)}` : ""}`;
    return;
  }

  if (view.type === "vault") return renderVault();

  if (view.type === "folder") {
    let folder;
    try { folder = await api(`/api/overview/folder?path=${encodeURIComponent(view.path)}`); } catch (err) {
      state.view = { type: "home" };
      return renderView();
    }
    state.view.page = folder.page || "";
    if (view.editing && folder.page) {
      const file = await api(`/api/overview/file?path=${encodeURIComponent(folder.page)}`);
      content.innerHTML = `<div class="doc-bar"><span class="path">${esc(folder.page)}</span><div><button class="ghost" data-cancel-edit>Cancel</button> <button class="primary" data-save>Save</button></div></div>
        <textarea class="editor" id="editor" spellcheck="false">${esc(file.content)}</textarea>`;
      return;
    }
    const body = await readPage(folder.page);
    const empty = !folder.page && !folder.folders.length && !folder.files.length;
    const assets = folder.files.filter((f) => f.kind === "asset");
    const others = folder.files.filter((f) => f.kind !== "asset");
    content.innerHTML = `
      <div class="doc-bar"><span class="path">${esc(folder.page || folder.path)}</span><div>${folder.page ? `<button class="ghost" data-edit>Edit page</button> ` : ""}<button class="ghost" data-new-folder="${esc(folder.path)}">+ New folder</button> <button class="ghost" data-new-asset="${esc(folder.path)}">+ New asset</button> <button class="ghost" data-export="${esc(folder.path)}">Export</button></div></div>
      <div class="kicker">Section / ${esc(humanize(view.path.split("/")[0]))}</div>
      <h1 class="title">${esc(folder.title)}</h1>
      ${folder.excerpt ? `<p class="lede">${esc(folder.excerpt)}</p>` : ""}
      ${folder.cover ? `<img class="hero" src="${esc(fileUrl(folder.cover))}" alt="">` : ""}
      ${folder.folders.length ? `<h2 class="group-title">Folders <span>${folder.folders.length}</span></h2>${folderCards(folder.folders, false)}` : ""}
      ${assets.length ? `<h2 class="group-title">Assets <span>${assets.length}</span></h2>${assetTiles(assets, folder.path)}` : ""}
      ${body ? `<div class="page-body">${withToc(renderMarkdown(stripLead(body), folder.path))}</div>` : ""}
      ${others.length ? `<h2 class="block-title">Documents & files</h2>${fileBlocks(others)}` : ""}
      ${pillarStrip(folder.pillars || [], folder.path, `Pillars · ${folder.title}`, folder.text_pillars || [])}
      ${empty ? `<p class="lede">Nothing here yet. Ask the agent to fill this section.</p>` : ""}`;
    return;
  }

  if (view.type === "doc") {
    let file;
    try { file = await api(`/api/overview/file?path=${encodeURIComponent(view.path)}`); } catch (err) {
      toast(err.message, true);
      state.view = { type: "home" };
      return renderView();
    }
    const isMd = view.path.toLowerCase().endsWith(".md");
    if (isMd && !view.editing) {
      const asset = await api(`/api/studio/asset?path=${encodeURIComponent(view.path)}`).catch(() => null);
      if (asset?.is_asset) { content.innerHTML = renderAssetPage(asset); content.dataset.dropAsset = ""; queueThumbs(asset); return; }
    }
    content.innerHTML = `
      <div class="doc-bar"><span class="path">${esc(file.path)}</span>
        <div>${view.editing ? `<button class="ghost" data-cancel-edit>Cancel</button> <button class="primary" data-save>Save</button>` : `${isMd && !/(^|\/)(README|index|GAME)\.md$/i.test(view.path) && !/(^|\/)pillars\//.test(view.path) ? `<button class="ghost" data-to-folder title="Make this document a folder (it becomes the folder's page) so you can put assets and sub-folders in it">Turn into folder</button> <button class="ghost" data-make-asset>Turn into asset</button> ` : ""}${/(^|\/)README\.md$/i.test(view.path) && dirOf(view.path) ? `<button class="ghost" data-new-folder="${esc(dirOf(view.path))}">+ New folder</button> <button class="ghost" data-new-asset="${esc(dirOf(view.path))}">+ New asset</button> ` : ""}<button class="ghost" data-edit>Edit</button>`}</div></div>
      ${view.editing ? `<textarea class="editor" id="editor" spellcheck="false">${esc(file.content)}</textarea>`
        : isMd ? withToc(renderMarkdown(file.content, dirOf(file.path))) : `<div class="md"><pre><code>${esc(file.content)}</code></pre></div>`}`;
  }
}

// ---------- final vault ----------

const VAULT_FILTERS = { all: "All", ready: "Export-ready", no3d: "Missing 3D", noimage: "Missing image" };

function meshSummary(s) {
  if (!s || !Object.keys(s).length) return "";
  const smart = s.model_type === "smart-topology";
  const poly = smart || s.should_remesh ? `${fmtCount(s.target_polycount)} ${s.topology === "quad" ? "quads" : "tris"}` : "raw mesh";
  return [MESH_MODEL_LABEL[s.ai_model]?.split(" · ")[0] || s.ai_model, poly, s.should_texture ? `${s.enable_pbr ? "PBR " : ""}${s.texture_resolution}` : "untextured", s.pose_mode].filter(Boolean).join(" · ");
}

function vaultRow(e) {
  const formats = Object.keys(e.model?.formats || {});
  const files = [e.winner ? e.winner.split(".").pop().toUpperCase() : "", ...formats.map((f) => f.toUpperCase())].filter(Boolean);
  const exportPath = [e.folder, e.name].filter(Boolean).join("/");
  const meshBtn = e.model
    ? `<button class="vault-thumb mesh" data-view-model="${esc(e.model.path)}" title="Open in the 3D viewer">${e.model.thumb ? `<img src="${esc(fileUrl(e.model.thumb))}" alt="" loading="lazy">` : icon("cube")}</button>`
    : `<button class="vault-thumb mesh missing" data-open="${esc(e.path)}" title="Make 3D"><span>No 3D</span></button>`;
  return `<div class="vault-row${e.ready ? " ready" : ""}">
    <button class="vault-thumb" data-open="${esc(e.path)}" title="Final image">${e.winner ? `<img src="${esc(fileUrl(e.winner))}" alt="" loading="lazy">` : `<span>No image</span>`}</button>
    ${meshBtn}
    <div class="vault-info">
      <div class="vault-name"><strong>${esc(e.title)}</strong><span class="kind-tag">${esc(e.kind)}</span>${e.ready ? `<span class="kind-tag win">Ready</span>` : ""}</div>
      <code title="Folder in the export">${esc(exportPath)}/</code>
      <div class="vault-files">${files.map((f) => `<span class="chip">${esc(f)}</span>`).join("")}${e.model_settings && Object.keys(e.model_settings).length ? `<small>${esc(meshSummary(e.model_settings))}</small>` : ""}</div>
    </div>
    <div class="vault-actions">
      ${e.model ? `<button class="act" data-view-model="${esc(e.model.path)}">${icon("eye")}View</button>` : ""}
      <button class="act" data-open="${esc(e.path)}">${e.model ? "Open" : `${icon("cube")}Make 3D`}</button>
      <button class="act" data-vault-zip="${esc(e.path)}" title="Download this asset as a ZIP">${icon("download")}ZIP</button>
    </div>
  </div>`;
}

async function renderVault() {
  const content = $("#content");
  let vault;
  try { vault = await api("/api/vault"); } catch (err) { content.innerHTML = `<div class="empty"><h1>Vault unavailable</h1><p>${esc(err.message)}</p></div>`; return; }
  const filter = state.vaultFilter || "all";
  const keep = { all: () => true, ready: (e) => e.ready, no3d: (e) => !e.model, noimage: (e) => !e.winner }[filter];
  const t = vault.totals;
  const groups = vault.groups.map((g) => ({ ...g, assets: g.assets.filter(keep) })).filter((g) => g.assets.length);
  content.innerHTML = `
    <div class="kicker">Project / ${esc(projectTitle())}</div>
    <h1 class="title">Final Vault</h1>
    <p class="lede">Every asset with a chosen image or a 3D model, named and laid out folder by folder so it drops straight into your engine.</p>
    <div class="vault-stats">
      <div><b>${t.assets}</b><span>Assets</span></div>
      <div class="ok"><b>${t.ready}</b><span>Export-ready</span></div>
      <div><b>${t.images}</b><span>Final images</span></div>
      <div><b>${t.models}</b><span>3D models</span></div>
    </div>
    ${t.assets ? `<div class="vault-bar">
      <button class="primary" data-export="">${icon("box")} Export for Godot / Unity / Unreal</button>
      <button class="ghost" data-vault-zip="">${icon("download")} Vault ZIP</button>
      <button class="ghost" data-vault-folder title="Write the same layout into this project's exports folder">Write to exports folder</button>
      <label class="inline"><input type="checkbox" id="vaultHistory"${state.vaultHistory ? " checked" : ""}> Include history (candidates, variants, edits, older 3D)</label>
    </div>
    <div class="seg">${Object.entries(VAULT_FILTERS).map(([key, label]) => `<button class="${key === filter ? "on" : ""}" data-vault-filter="${key}">${label}</button>`).join("")}</div>
    ${groups.map((g) => `<h2 class="group-title">${esc(g.title)} <span>${g.assets.length}</span></h2><div class="vault-list">${g.assets.map(vaultRow).join("")}</div>`).join("") || `<p class="lede">Nothing matches this filter.</p>`}
    <details class="vault-layout"><summary>Vault ZIP layout</summary><pre><code>${esc(state.project)}-vault/
  manifest.json
  &lt;folder&gt;/&lt;asset&gt;/
    &lt;asset&gt;.png          final image
    &lt;asset&gt;.glb (.obj…)  newest 3D model, every format made
    &lt;asset&gt;_thumb.png    model preview
    &lt;asset&gt;.json         kind, prompt, 3D settings
    history/…            only with “Include history”</code></pre></details>`
    : `<div class="empty"><h1>The vault is empty</h1><p>Pick a winner image or make a 3D model on any asset and it shows up here, ready to export.</p></div>`}`;
}

function vaultZip(path) {
  const params = new URLSearchParams({ project: state.project });
  if (path) params.append("path", path);
  if ($("#vaultHistory")?.checked) params.set("history", "true");
  const link = Object.assign(document.createElement("a"), { href: `/api/vault/export?${params}`, download: "" });
  document.body.append(link);
  link.click();
  link.remove();
}

async function vaultToFolder() {
  try {
    const { folder } = await api("/api/vault/export", { method: "POST", body: { history: !!$("#vaultHistory")?.checked } });
    toast(`Exported to ${folder}`);
  } catch (err) { toast(err.message, true); }
}

// ---------- asset studio ----------

const ACTION_LABEL = { candidates: "Generate 2D", variant: "Variant", edit: "Edit image", views: "Turnaround", make_3d: "Make 3D", rig: "Rig", animate: "Animate" };

// Failed or interrupted runs stay on the asset page until dismissed, so a lost result is never silent.
function dismissedJobs() { try { return new Set(JSON.parse(storeGet("forge.dismissedJobs") || "[]")); } catch { return new Set(); } }

function problemJobs(path) {
  const gone = dismissedJobs();
  const day = Date.now() / 1000 - 86400;
  return Object.values(state.jobs).filter((j) => j.asset === path && (j.status === "failed" || j.status === "interrupted") && !gone.has(j.id) && (j.finished_at || 0) > day);
}

function runningJobs(path) {
  return Object.values(state.jobs).filter((j) => j.asset === path && j.status === "running");
}

const ICON = {
  sparkle: `<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 16v4M17 18h4"/>`,
  variant: `<rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6.5A2.5 2.5 0 0 0 13.5 4h-7A2.5 2.5 0 0 0 4 6.5v7A2.5 2.5 0 0 0 6.5 16H8"/>`,
  edit: `<path d="M4 20l1-4L16.5 4.5a2.1 2.1 0 0 1 3 3L8 19z"/><path d="M14.5 6.5l3 3"/>`,
  cube: `<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>`,
  star: `<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>`,
  layers: `<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 12.5l9 5 9-5"/>`,
  download: `<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>`,
  back: `<path d="M15 5l-7 7 7 7"/>`,
  turn: `<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v4h-4"/><circle cx="12" cy="12" r="2.5"/>`,
  bones: `<circle cx="12" cy="4.5" r="2"/><path d="M12 6.5v7M12 13.5l-4 6.5M12 13.5l4 6.5M6 9.5h12"/>`,
  play: `<path d="M7 4.5v15l12-7.5z"/>`,
  scissors: `<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><path d="M8 7.5 20 18M8 16.5 20 6"/>`,
  grid: `<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>`,
  eye: `<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>`,
  upload: `<path d="M12 20V9M7 13l5-5 5 5M5 4h14"/>`,
  box: `<path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/>`,
  film: `<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M8 4.5v15M16 4.5v15M3.5 9h4.5M3.5 15h4.5M16 9h4.5M16 15h4.5"/>`,
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[name]}</svg>`;

// Image outputs that can become the winner and be worked on further.
const WORK_KINDS = new Set(["candidates", "variants", "edits"]);

function modelCard(o, kind) {
  const glb = /\.glb$/i.test(o.path);
  const badge = { "3d": ["cube", "3D"], rig: ["bones", "Rigged"], animations: ["film", "Animation"] }[kind] || ["cube", "3D"];
  const ext = o.path.split(".").pop().toUpperCase();
  const formats = Object.entries(o.formats || { [ext.toLowerCase()]: o.path });
  return `<div class="out model"><button type="button" class="out-img" data-view-model="${esc(o.path)}" title="Open in the 3D viewer">${o.thumb ? `<img src="${esc(fileUrl(o.thumb))}" alt="">` : `<div class="no-thumb">${icon("cube")}<span>${esc(ext)} model</span></div>`}<span class="out-badge mesh">${icon(badge[0])}${badge[1]} · ${esc(ext)}</span></button>
    <div class="out-bar"><button class="act" data-view-model="${esc(o.path)}">${icon("eye")}View</button>
    ${kind === "3d" && glb ? `<button class="act" data-action="rig" data-source="${esc(o.path)}">${icon("bones")}Rig</button>` : kind === "rig" && glb ? `<button class="act" data-action="animate">${icon("play")}Animate</button>` : `<button class="act" data-sprites="${esc(o.path)}">${icon("grid")}Sprites</button>`}
    <div class="fmt-links">${icon("download")}${formats.map(([fmt, path]) => `<a href="${esc(fileUrl(path))}" download>${esc(fmt.toUpperCase())}</a>`).join("")}</div></div></div>`;
}

function outputGrid(items, winner, kind = "candidates") {
  if (!items.length) return "";
  return `<div class="out-grid">${items.map((o) => {
    if (o.type === "model") return modelCard(o, kind);
    const isWinner = o.path === winner;
    const img = `<a class="out-img" href="${esc(fileUrl(o.path))}" target="_blank" rel="noopener"><img src="${esc(fileUrl(o.path))}" alt="" loading="lazy">${isWinner ? `<span class="out-badge win">${icon("star")}Winner</span>` : ""}</a>`;
    if (!WORK_KINDS.has(kind)) {
      return `<div class="out${kind === "icons" || kind === "sprites" ? " checker" : ""}">${img}<div class="out-bar">
        ${kind === "parts" || kind === "views" ? `<button class="act" data-icons="${esc(o.path)}">${icon("grid")}Icons</button><button class="act" data-winner="${esc(o.path)}">${icon("star")}Winner</button>` : ""}
        <a class="act wide" href="${esc(fileUrl(o.path))}" download>${icon("download")}Download</a></div></div>`;
    }
    return `<div class="out${isWinner ? " winner" : ""}">${img}
      <div class="out-bar">${isWinner ? "" : `<button class="act wide hero-act" data-winner="${esc(o.path)}">${icon("star")}Pick winner</button>`}
      <button class="act" data-action="variant" data-source="${esc(o.path)}">${icon("variant")}Variant</button>
      <button class="act" data-action="edit" data-source="${esc(o.path)}">${icon("edit")}Edit</button>
      <button class="act" data-split="${esc(o.path)}">${icon("scissors")}Split</button>
      <button class="act" data-icons="${esc(o.path)}">${icon("grid")}Icons</button>
      <button class="act wide pillar-act" data-make-pillar="${esc(o.path)}" title="Use this image as a style reference for a whole folder">${icon("layers")}Make pillar</button></div></div>`;
  }).join("")}</div>`;
}

const OUTPUT_SECTIONS = [
  ["3d", "3D models"], ["rig", "Rigged"], ["animations", "Animations"], ["sprites", "Sprite sheets"], ["icons", "Icons"],
  ["views", "Turnaround views"], ["candidates", "Candidates"], ["variants", "Variants"], ["edits", "Edits"], ["parts", "Parts"],
];

function renderAssetPage(a) {
  const title = (a.body.match(/^#\s+(.+)$/m) || [])[1] || humanize(a.path.split("/").pop());
  const jobs = runningJobs(a.path);
  const groups = {};
  for (const p of a.pillars) (groups[p.source] ||= []).push(p);
  const o = a.outputs;
  return `
    <div class="kicker">Asset / ${esc(a.path)}</div>
    <div class="asset-head"><h1 class="title">${esc(title)}</h1><span class="kind-tag">${esc(a.kind)}</span></div>
    ${problemJobs(a.path).map((j) => `<div class="job-banner bad"><span class="bad-dot">!</span><span class="grow">${esc(ACTION_LABEL[j.action] || j.action)} ${j.status === "interrupted" ? "was interrupted" : "failed"}: ${esc(j.error || "no details")}</span><button type="button" class="mini" data-action="${esc(j.action)}">Try again</button><button type="button" class="mini" data-dismiss-job="${esc(j.id)}">Dismiss</button></div>`).join("")}
    ${jobs.length ? `<div class="job-banner"><span class="spinner"></span>${jobs.map((j) => esc(ACTION_LABEL[j.action] || j.action)).join(", ")} running… results appear here automatically.</div>` : ""}
    <div class="asset-top">
      <div class="winner-box drop-zone" data-drop-winner title="Drop an image here to use it as the winner">${a.winner ? `<img src="${esc(fileUrl(a.winner))}" alt="Winner"><span class="drop-hint">Drop an image to replace</span>` : `<div class="no-thumb big"><p class="drop-copy"><span class="drop-icon">⇣</span>No winner yet.<br>Generate 2D candidates, <b>drop an image here</b><br>or <button type="button" class="link-btn" data-pick-winner-file>choose a file</button>.</p></div>`}</div>
      <div class="asset-side">
        <label class="field-label" for="assetPrompt">Prompt: what this ${esc(a.kind)} looks like</label>
        <textarea id="assetPrompt" class="prompt-box" rows="6" data-asset="${esc(a.path)}">${esc(a.prompt)}</textarea>
        <div class="action-row">
          <button class="primary" data-action="candidates">Generate 2D</button>
          <button class="ghost" data-action="variant"${a.winner ? "" : " disabled"}>Variant</button>
          <button class="ghost" data-action="edit"${a.winner ? "" : " disabled"}>Edit image</button>
          <button class="ghost sem-mesh" data-action="make_3d"${a.winner ? "" : " disabled"}>Make 3D</button>
        </div>
        ${gameTools(a)}
        <div class="applied">
          <div class="field-label">Pillars sent with every generation</div>
          ${a.pillars.length ? Object.entries(groups).map(([src, items]) => `<div class="applied-group"><span>${esc(src)}</span><div class="pillar-row small">${items.map((p) => `<img src="${esc(fileUrl(p.path))}" title="${esc(p.path)}" alt="">`).join("")}</div></div>`).join("")
            : `<p class="pillar-empty">No pillars apply yet. Add reference images on the project front page (bible) or on this asset's folder.</p>`}
          ${(a.text_pillars || []).length ? `<details class="bible-mini"><summary><span>Style bible</span>${a.text_pillars.map((t) => esc(t.source)).join(" · ")}<em>View</em></summary>
            ${a.text_pillars.map((t) => `<div class="md">${renderMarkdown(t.text)}</div>`).join("")}</details>` : ""}
          ${a.style_rules.map((r) => `<div class="applied-group"><span>Style · ${esc(r.source)}</span><p>${esc(r.text)}</p></div>`).join("")}
        </div>
      </div>
    </div>
    ${OUTPUT_SECTIONS.map(([kind, label]) => (o[kind] || []).length ? `<h2 class="block-title">${label}</h2>${outputGrid(o[kind], a.winner, kind)}` : "").join("")}
    <h2 class="block-title">Notes</h2>
    <div class="doc-bar"><span class="path">${esc(a.path)}</span><button class="ghost" data-edit>Edit document</button></div>
    <div class="md">${renderMarkdown(a.body.replace(/^#\s+.+$/m, ""), dirOf(a.path))}</div>`;
}

async function saveAssetPrompt() {
  const box = $("#assetPrompt");
  if (!box) return;
  await api("/api/studio/asset", { method: "PATCH", body: { path: box.dataset.asset, fields: { prompt: box.value.trim() } } });
}

async function openPreview(action, source = "", instruction = "") {
  try {
    await saveAssetPrompt();
    await studioOptions();
    const req = await api("/api/studio/preview", { method: "POST", body: { path: state.view.path, action, source, instruction } });
    // A variant or edit starts from an existing image, so the style bible stays off unless switched on.
    if (action === "rig") { state.preview = req; return renderRigSheet(); }
    if (action === "animate") { state.preview = { ...req, picked: [], search: "" }; return renderAnimateSheet(); }
    const bible = action === "variant" || action === "edit" || action === "views" ? false : req.use_bible !== false;
    state.preview = applyBible({ ...req, use_bible: bible, instruction, references: req.references.map((r) => ({ ...r, on: true })) });
    renderPreview();
  } catch (err) { toast(err.message, true); }
}

function refLabel(r) {
  if (r.role === "source") return "Source image";
  if (r.role === "extra") return "Added for this run";
  if (r.role === "view") return "Turnaround view";
  return `Pillar · ${r.source}`;
}

const ACTION_THEME = {
  candidates: { tone: "sakura", icon: "sparkle", blurb: "New images from the asset prompt, styled by the pillars." },
  variant: { tone: "amber", icon: "variant", blurb: "A new take on the source image. Same subject, pose and style." },
  edit: { tone: "violet", icon: "edit", blurb: "Change one thing in the source image and keep the rest." },
  make_3d: { tone: "sky", icon: "cube", blurb: "Turn the winner image into a textured 3D model." },
  views: { tone: "amber", icon: "turn", blurb: "Side and back views of the winner. Make 3D then uses them for a better model." },
  rig: { tone: "sky", icon: "bones", blurb: "Add a skeleton to a humanoid model so it can be animated. Includes walk and run clips." },
  animate: { tone: "violet", icon: "play", blurb: "Put preset animations on the rigged model: idle, attack, death and more." },
};
const PART_ROLE = { bible: "Style bible", subject: "Subject", style: "Style rule", rule: "Reference rule" };

async function studioOptions() {
  if (!state.studio) state.studio = await api("/api/studio/options").catch(() => ({ models: [], kinds: [], selected: "" }));
  return state.studio;
}

function renderPreview() {
  const p = state.preview;
  const theme = ACTION_THEME[p.action];
  const needsInstruction = p.action === "variant" || p.action === "edit";
  const is3d = p.action === "make_3d";
  const models = state.studio?.models || [];
  const model = models.find((m) => m.id === p.model);
  const on = p.references.filter((r) => r.on).length;
  const cost = is3d ? ` · ≈ $${(p.settings.should_texture ? 0.3 : 0.2).toFixed(2)} (${p.settings.should_texture ? 30 : 20} Meshy credits)` : model ? ` · ≈ $${(model.price * (p.count || 1)).toFixed(2)}` : "";
  const refs = p.references.map((r, i) => `
    <label class="ref role-${esc(r.role)}${r.on ? "" : " off"}"><input type="checkbox" data-ref="${i}" ${r.on ? "checked" : ""}>
      <img src="${esc(fileUrl(r.path))}" alt=""><span title="${esc(refLabel(r))}">${esc(refLabel(r).split(" · ")[0])}</span></label>`).join("");
  const assetName = humanize(p.asset.split("/").pop());
  openDialog(`
    <div class="dlg sheet tone-${theme.tone}">
      <header class="sheet-head">
        <span class="sheet-icon">${icon(theme.icon)}</span>
        <div><h2>${esc(ACTION_LABEL[p.action])} <span>· ${esc(assetName)}</span></h2><p>${esc(theme.blurb)}</p></div>
      </header>
      ${is3d ? "" : `<div class="sheet-model">
        <label class="model-pick">Image model
          <select id="pvModel">${Object.entries(Object.groupBy(models, (m) => m.label.split(" · ")[0])).map(([provider, items]) => `<optgroup label="${esc(provider)}${items.some((m) => m.available) ? "" : " · no key"}">${items.map((m) => `<option value="${esc(m.id)}" ${m.id === p.model ? "selected" : ""} ${m.available ? "" : "disabled"}>${esc(m.label)} · $${m.price.toFixed(3)}</option>`).join("")}</optgroup>`).join("")}
          ${model ? "" : `<option value="${esc(p.model)}" selected>${esc(p.model)}</option>`}</select></label>
        ${Object.entries(p.settings).map(([k, v]) => `<span class="chip">${esc(k.replace(/_/g, " "))} <b>${esc(Array.isArray(v) ? v.join(", ") : String(v))}</b></span>`).join("")}
      </div>`}
      ${needsInstruction ? `<div class="sheet-instruction"><label>What should change?<textarea id="pvInstruction" rows="2" placeholder="${p.action === "variant" ? "Frost-themed armour, icy blue trim" : "Remove the shield"}"></textarea></label>
        <button class="ghost" data-rebuild>Update prompt</button></div>` : ""}
      <div class="sheet-body${is3d ? " mesh-body" : ""}">
        ${is3d ? meshOptions(p) : `<section>
          ${p.action === "views" ? `<p class="hint-line">One image per view: ${esc(p.settings.views || "")}. <code>{view}</code> is replaced for each image.</p>` : bibleSwitch(p)}
          <label class="field-label" for="pvPrompt">Prompt <em>exactly what is sent</em></label>
          <textarea id="pvPrompt" class="final-prompt" rows="6">${esc(p.prompt)}</textarea>
          <details class="recipe-details"><summary>How the prompt is built</summary>
            <div class="recipe">${p.parts.map((part) => `<div class="recipe-part role-${esc(part.role || "subject")}"><span>${esc(PART_ROLE[part.role] || part.source)}<em>${esc(part.source.replace(/^[^·]*·\s*/, ""))}</em></span><p>${esc(part.text || "—")}</p></div>`).join("")}</div></details>
        </section>`}
        <section>
          <div class="field-label">Images · ${on}${is3d ? (p.references.length > 1 ? " · several views make a better model" : "") : " of 8"}</div>
          <div class="refs">${refs || `<p class="pillar-empty">No reference images. Add pillars to keep the style consistent.</p>`}
            ${is3d ? "" : `<button type="button" class="ref add" data-add-ref>+ Add</button>`}</div>
        </section>
      </div>
      <footer class="sheet-foot">
        ${is3d ? "" : `<label class="inline">Images <select id="pvCount">${[1, 2, 3, 4].map((n) => `<option ${n === p.count ? "selected" : ""}>${n}</option>`).join("")}</select></label>`}
        <span class="cost">${on} image${on === 1 ? "" : "s"} attached${cost}</span>
        <span class="grow"></span>
        <button type="button" class="ghost" data-close>Cancel</button><button type="button" class="run-btn" data-run>${icon(theme.icon)}${esc(ACTION_LABEL[p.action])}</button>
      </footer>
    </div>`);
  const instruction = $("#pvInstruction");
  if (instruction) instruction.value = p.instruction || "";
  $("#pvModel")?.addEventListener("change", () => { syncPreviewFields(); renderPreview(); });
  $("#pvCount")?.addEventListener("change", () => { syncPreviewFields(); renderPreview(); });
  document.querySelectorAll("#dialog [data-mesh], #dialog [data-fmt]").forEach((el) => el.addEventListener("change", () => { syncMesh(); renderPreview(); }));
}

const MESH_MODEL_LABEL = { "meshy-7.1": "Meshy 7.1 · best detail", "meshy-6": "Meshy 6", "meshy-6-lite": "Meshy 6 Lite · fast", "meshy-t2.5": "Meshy T2.5 · game topology", "meshy-t2": "Meshy T2 · game topology" };
const fmtCount = (n) => (n >= 1000 ? `${+(n / 1000).toFixed(1)}k` : String(n));

// Make 3D settings: generator, polycount, texture, pose and export formats.
function meshOptions(p) {
  const s = p.settings;
  const c = p.choices || {};
  const t2 = s.ai_model === "meshy-t2" || s.ai_model === "meshy-t2.5";
  const smart = t2 || s.model_type === "smart-topology";
  const cap = smart ? (s.ai_model === "meshy-t2" ? 15000 : 30000) : 300000;
  const usesCount = smart || s.should_remesh;
  const select = (key, labels = {}, disabled = false) => `<select data-mesh="${key}"${disabled ? " disabled" : ""}>${(c[key] || []).map((v) => `<option value="${esc(v)}" ${v === s[key] ? "selected" : ""}>${esc(labels[v] ?? v)}</option>`).join("")}</select>`;
  const toggle = (key, label, hint, disabled = false) => `<label class="mesh-toggle${disabled ? " off" : ""}"><span class="switch"><input type="checkbox" data-mesh="${key}" ${s[key] && !disabled ? "checked" : ""}${disabled ? " disabled" : ""}><span></span></span><span>${label}<small>${hint}</small></span></label>`;
  return `<section class="mesh-opts">
    <div class="field-label">Generator</div>
    <div class="mesh-grid">
      <label>Model ${select("ai_model", MESH_MODEL_LABEL)}</label>
      <label>Mesh ${t2 ? `<select disabled><option>Smart topology · game-ready</option></select>` : select("model_type", { standard: "Standard · high detail", "smart-topology": "Smart topology · game-ready" })}</label>
    </div>
    <div class="field-label">Polycount</div>
    ${smart ? "" : toggle("should_remesh", "Remesh to a target polycount", "Off keeps Meshy's raw high-detail mesh")}
    <div class="poly-row${usesCount ? "" : " off"}">
      <input type="number" data-mesh="target_polycount" min="100" max="${cap}" step="100" value="${Math.min(s.target_polycount, cap)}"${usesCount ? "" : " disabled"}>
      <span class="poly-presets">${(c.polycount_presets || []).filter((n) => n <= cap).map((n) => `<button type="button" class="${n === s.target_polycount ? "on" : ""}" data-poly="${n}"${usesCount ? "" : " disabled"}>${fmtCount(n)}</button>`).join("")}</span>
    </div>
    ${smart ? `<p class="hint-line">Smart topology always hits the target, up to ${fmtCount(cap)} triangles.</p>` : usesCount ? `<div class="mesh-grid"><label>Faces ${select("topology", { triangle: "Triangles", quad: "Quads" })}</label></div>` : ""}
    <div class="field-label">Texture</div>
    ${toggle("should_texture", "Texture the model", "Off gives a grey clay mesh and costs less")}
    ${toggle("enable_pbr", "PBR maps", "Metallic, roughness and normal maps", !s.should_texture)}
    <div class="mesh-grid"><label>Resolution ${select("texture_resolution", {}, !s.should_texture || s.ai_model === "meshy-6-lite")}</label></div>
    <div class="field-label">Pose &amp; export</div>
    <div class="mesh-grid">
      <label>Pose ${select("pose_mode", { "": "As in the image", "a-pose": "A-pose", "t-pose": "T-pose" })}</label>
      <label>Origin ${select("origin_at", { bottom: "Bottom (feet)", center: "Center" })}</label>
    </div>
    <div class="fmt-row">${(c.target_formats || []).map((f) => `<label class="fmt${s.target_formats.includes(f) ? " on" : ""}"><input type="checkbox" data-fmt="${f}" ${s.target_formats.includes(f) ? "checked" : ""}${f === "glb" ? " disabled" : ""}>${f.toUpperCase()}</label>`).join("")}</div>
  </section>`;
}

function syncMesh() {
  const p = state.preview;
  if (p?.action !== "make_3d") return;
  const s = p.settings;
  document.querySelectorAll("#dialog [data-mesh]").forEach((el) => {
    if (el.disabled) return;
    const key = el.dataset.mesh;
    s[key] = el.type === "checkbox" ? el.checked : el.type === "number" ? Math.max(100, Number(el.value) || s[key]) : el.value;
  });
  s.target_formats = ["glb", ...[...document.querySelectorAll("#dialog [data-fmt]:checked")].map((el) => el.dataset.fmt).filter((f) => f !== "glb")];
  if (s.ai_model === "meshy-t2" || s.ai_model === "meshy-t2.5") s.model_type = "smart-topology";
  if (!s.should_texture) s.enable_pbr = false;
}

function bibleSwitch(p) {
  if (!p.bible?.length) return `<p class="hint-line">No style bible yet. Add one under <b>Bible pillars</b> on the project overview (pose, polygon budget, palette…).</p>`;
  return `<div class="bible-switch${p.use_bible ? " on" : ""}">
    <label class="switch"><input type="checkbox" data-bible-toggle ${p.use_bible ? "checked" : ""}><span></span></label>
    <div><strong>Style bible</strong><small>${p.use_bible ? "In the final prompt below" : "Off for this run"} · ${p.bible.map((b) => esc(b.source)).join(", ")}</small></div>
    <button type="button" class="link-btn" data-bible-view>${p.showBible ? "Hide" : "View"}</button>
  </div>
  ${p.showBible ? `<div class="bible-view">${p.bible.map((b) => `<div><span>${esc(b.source)}</span><div class="md">${renderMarkdown(b.text)}</div></div>`).join("")}</div>` : ""}`;
}

// The style bible sits at the top of the final prompt, so the prompt can be copied as a whole.
function bibleBlock(p) {
  return p.bible?.length ? `Style bible:\n${p.bible.map((b) => b.text.trim()).join("\n\n")}` : "";
}

// Subject first, then the style bible, then the reference rule, so the subject is never buried under the style text.
function applyBible(p) {
  const block = bibleBlock(p);
  if (!block) return p;
  const prompt = String(p.prompt || "").replace(block, "").replace(/\n{3,}/g, "\n\n").trim();
  if (p.use_bible === false) { p.prompt = prompt; return p; }
  const rule = (p.parts || []).find((part) => part.role === "rule")?.text?.trim();
  const at = rule ? prompt.indexOf(rule) : -1;
  p.prompt = at >= 0
    ? [prompt.slice(0, at).trim(), block, prompt.slice(at).trim()].filter(Boolean).join("\n\n")
    : [prompt, block].filter(Boolean).join("\n\n");
  return p;
}

function syncPreviewFields() {
  const p = state.preview;
  if (!p) return;
  if ($("#pvPrompt")) p.prompt = $("#pvPrompt").value;
  if ($("#pvCount")) p.count = Number($("#pvCount").value);
  if ($("#pvInstruction")) p.instruction = $("#pvInstruction").value;
  if ($("#pvModel")) p.model = $("#pvModel").value;
  syncMesh();
}

async function rebuildPreview() {
  syncPreviewFields();
  const p = state.preview;
  const source = (p.references.find((r) => r.role === "source") || {}).path || "";
  const extras = p.references.filter((r) => r.role === "extra");
  try {
    const req = await api("/api/studio/preview", { method: "POST", body: { path: p.asset, action: p.action, source, instruction: p.instruction, model: p.model } });
    state.preview = applyBible({ ...req, instruction: p.instruction, count: p.count, use_bible: p.use_bible, showBible: p.showBible, references: [...req.references.map((r) => ({ ...r, on: true })), ...extras] });
    renderPreview();
  } catch (err) { toast(err.message, true); }
}

async function runPreview() {
  syncPreviewFields();
  if (!prepareGameRun()) return;
  const p = state.preview;
  if ((p.action === "variant" || p.action === "edit") && !String(p.instruction || "").trim()) { toast("Describe what should change, then press Update prompt.", true); return; }
  const request = { action: p.action, asset: p.asset, prompt: p.prompt, count: p.count, settings: p.settings, action_ids: p.action_ids, names: p.names, model: p.action === "make_3d" ? "" : p.model, use_bible: p.use_bible !== false, references: p.references.filter((r) => r.on).map(({ path, source, role }) => ({ path, source, role })) };
  try {
    const job = await api("/api/studio/run", { method: "POST", body: { request } });
    if (!state.jobs[job.id]) state.jobs[job.id] = job; // the socket may already have reported it finished
    closeDialog();
    toast(`${ACTION_LABEL[p.action]} started`);
    renderView();
  } catch (err) { toast(err.message, true); }
}

async function loadJobs() {
  try {
    const data = await api("/api/studio/jobs");
    state.jobs = Object.fromEntries((data.jobs || []).map((j) => [j.id, j]));
  } catch { state.jobs = {}; }
}

async function pickWinner(file) {
  try {
    await api("/api/studio/asset", { method: "PATCH", body: { path: state.view.path, fields: { winner: file } } });
    toast("Winner picked");
    renderView();
  } catch (err) { toast(err.message, true); }
}

function newFolderDialog(parent) {
  openDialog(`
    <form class="dlg" id="newFolderForm">
      <h2>New folder</h2>
      <p>Creates a folder in <code>${esc(parent || "the project root")}</code> for a group of assets, e.g. creatures, factions or weapons.</p>
      <label>Name<input name="title" required maxlength="80" placeholder="Creatures" autofocus></label>
      <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">Create</button></div>
    </form>`);
  $("#newFolderForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const { title } = Object.fromEntries(new FormData(event.target));
    try {
      const { path } = await api("/api/overview/folder", { method: "POST", body: { parent, title } });
      closeDialog();
      await loadOverview();
      go({ type: "folder", path });
    } catch (err) { toast(err.message, true); }
  });
}

async function docToFolder() {
  try {
    const { path } = await api("/api/overview/to-folder", { method: "POST", body: { path: state.view.path } });
    toast("Now a folder: add assets and sub-folders to it");
    await loadOverview();
    go({ type: "folder", path });
  } catch (err) { toast(err.message, true); }
}

async function makeAsset() {
  const { kinds } = await studioOptions();
  openDialog(`
    <form class="dlg" id="makeAssetForm">
      <h2>Turn into asset</h2>
      <p>Pick what this is. You can then generate images and 3D for it.</p>
      <div class="dlg-field"><span>Kind</span>${kindTags(kinds, "")}</div>
      <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">Turn into asset</button></div>
    </form>`);
  $("#makeAssetForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const kind = new FormData(event.target).get("kind");
    try {
      await api("/api/studio/asset", { method: "PATCH", body: { path: state.view.path, fields: { kind } } });
      closeDialog();
      renderView();
    } catch (err) { toast(err.message, true); }
  });
}

// The current page and the pages before it survive a refresh, per project.
const viewKey = () => `forge.view.${state.project}`;
const plainView = (v) => (v.type === "home" || v.type === "vault" ? { type: v.type } : { type: v.type, path: v.path });

function saveView() {
  const hash = state.view.type === "home" ? "" : state.view.type === "vault" ? "#vault" : `#${state.view.type}=${encodeURIComponent(state.view.path)}`;
  try { history.replaceState(null, "", hash || location.pathname); } catch { /* ignore */ }
  storeSet(viewKey(), JSON.stringify({ view: plainView(state.view), trail: state.trail.slice(-30).map(plainView) }));
}

function restoreView() {
  if (location.hash === "#vault") { state.view = { type: "vault" }; return; }
  const hash = location.hash.match(/^#(folder|doc)=(.+)$/);
  if (hash) {
    state.view = { type: hash[1], path: decodeURIComponent(hash[2]) };
    try { state.trail = (JSON.parse(storeGet(viewKey()) || "null")?.trail || []).filter((v) => v.type === "home" || typeof v.path === "string"); } catch { /* no trail */ }
    return;
  }
  try {
    const saved = JSON.parse(storeGet(viewKey()) || "null");
    const ok = (v) => v && (v.type === "home" || v.type === "vault" || ((v.type === "folder" || v.type === "doc") && typeof v.path === "string"));
    if (!ok(saved?.view)) return;
    state.view = plainView(saved.view);
    state.trail = (saved.trail || []).filter(ok);
  } catch { /* start at the overview */ }
}

function goBack() {
  const cur = state.view;
  if (cur.type === "home") return;
  let prev = state.trail.pop();
  if (!prev) {
    const parent = (cur.path || "").split("/").slice(0, -1).join("/");
    prev = parent ? { type: "folder", path: parent } : { type: "home" };
  }
  state.view = prev;
  $("#content").scrollTop = 0;
  showPanel("workspace", true);
  renderView();
}

function go(view) {
  // A folder's README is that folder's page, so it opens in the folder view with its sub-folders and assets.
  if (view.type === "doc" && /(^|\/)README\.md$/i.test(view.path) && dirOf(view.path)) view = { type: "folder", path: dirOf(view.path) };
  const same = state.view.type === view.type && state.view.path === view.path;
  if (!same) state.trail.push(plainView(state.view));
  state.view = view;
  $("#content").scrollTop = 0;
  showPanel("workspace", true);
  renderView();
}

function openPath(path) {
  if (!path) return;
  const isFile = /\.[a-z0-9]+$/i.test(path.split("/").pop());
  if (/\.(png|jpe?g|webp|gif|svg|glb|gltf|obj|fbx)$/i.test(path)) { window.open(fileUrl(path), "_blank", "noopener"); return; }
  go(isFile ? { type: "doc", path } : { type: "folder", path });
}

async function saveDoc() {
  try {
    await api("/api/overview/file", { method: "PUT", body: { path: state.view.type === "folder" ? state.view.page : state.view.path, content: $("#editor").value } });
    state.view = { ...state.view, editing: false };
    toast("Saved");
    await loadOverview();
  } catch (err) { toast(err.message, true); }
}

// ---------- game tools: turnaround, rig, animate, split, icons, sprites, 3D viewer, export ----------

const MODEL_RE = /\.(glb|gltf|obj|fbx)$/i;

function gameTools(a) {
  const glb = a.outputs["3d"].some((o) => /\.glb$/i.test(o.path));
  const rigged = Boolean(a.meta?.rig_task);
  const winner = esc(a.winner || "");
  const tool = (attrs, name, label, enabled, tip) => `<button type="button" class="tool" ${attrs}${enabled ? "" : " disabled"} title="${esc(tip)}">${icon(name)}${label}</button>`;
  return `<div class="tool-row"><div class="field-label">Game tools</div><div class="tool-btns">
    ${tool(`data-action="views"`, "turn", "Turnaround", a.winner, a.winner ? "Side and back views of the winner, for a better 3D model" : "Pick a winner first")}
    ${tool(`data-action="rig"`, "bones", "Rig", glb, glb ? "Add a skeleton to the GLB model" : "Make or drop a GLB model first")}
    ${tool(`data-action="animate"`, "play", "Animate", rigged, rigged ? "Preset animations for the rigged model" : "Rig the model first")}
    ${tool(`data-split="${winner}"`, "scissors", "Split", a.winner, "Cut the winner into separate parts or assets")}
    ${tool(`data-body-parts="${winner}"`, "bones", "Body parts", a.winner, "Glob boxes the head, torso, arms and legs so you can cut them apart")}
    ${tool(`data-icons="${winner}"`, "grid", "Icons", a.winner, "Square inventory icons from the winner")}
    ${tool(`data-add-model`, "upload", "Add 3D model", true, "GLB, glTF, OBJ or FBX — or drop the file anywhere on this page")}
    ${tool(`data-export="${esc(a.path)}"`, "box", "Export", true, "ZIP for Godot, Unity or Unreal")}
  </div></div>`;
}

function sheetFrame(action, assetName, body, foot) {
  const theme = ACTION_THEME[action];
  return `<div class="dlg sheet tone-${theme.tone}">
    <header class="sheet-head"><span class="sheet-icon">${icon(theme.icon)}</span>
      <div><h2>${esc(ACTION_LABEL[action])} <span>· ${esc(assetName)}</span></h2><p>${esc(theme.blurb)}</p></div></header>
    ${body}
    <footer class="sheet-foot">${foot}<span class="grow"></span><button type="button" class="ghost" data-close>Cancel</button>
      <button type="button" class="run-btn" data-run>${icon(theme.icon)}${esc(ACTION_LABEL[action])}</button></footer></div>`;
}

function renderRigSheet() {
  const p = state.preview;
  p.references = p.references.map((r) => ({ ...r, on: true }));
  const model = p.references[0]?.path || "";
  openDialog(sheetFrame("rig", humanize(p.asset.split("/").pop()), `
    <div class="sheet-body only-refs">
      <section class="rig-pick">
        <div class="rig-thumb">${p.thumb ? `<img src="${esc(fileUrl(p.thumb))}" alt="">` : icon("cube")}</div>
        <div class="rig-info"><strong>${esc(model.split("/").pop())}</strong>
          <label class="field-label" for="rigHeight">Character height</label>
          <div class="rig-height"><input id="rigHeight" type="number" min="0.2" max="20" step="0.05" value="${esc(p.settings.height_meters)}"> <span>metres</span></div>
          <p class="hint-line">Works on humanoid models that face forward, up to 380k triangles. You also get a walk and a run clip.</p></div>
      </section>
    </div>`, `<span class="cost">${p.credits} Meshy credits · ≈ $${p.cost_usd.toFixed(2)}</span>`));
}

async function renderAnimateSheet() {
  const p = state.preview;
  if (!state.animLib) {
    openDialog(sheetFrame("animate", humanize(p.asset.split("/").pop()), `<div class="sheet-body only-refs"><p class="hint-line">Loading Meshy's animation list…</p></div>`, ""));
    try { state.animLib = (await api("/api/studio/animations")).animations; } catch (err) { state.animLib = null; p.error = err.message; }
  }
  const lib = state.animLib || [];
  const q = (p.search || "").toLowerCase();
  const shown = lib.filter((a) => !q || `${a.name} ${a.category} ${a.sub_category}`.toLowerCase().includes(q)).slice(0, 120);
  const picked = new Set(p.picked);
  const credits = p.picked.length * p.credits_each;
  openDialog(sheetFrame("animate", humanize(p.asset.split("/").pop()), `
    <div class="sheet-model"><label class="model-pick">Search<input id="animSearch" type="search" value="${esc(p.search)}" placeholder="idle, attack, death…"></label>
      <label class="model-pick">FPS<select id="animFps">${[24, 25, 30, 60].map((f) => `<option ${f === Number(p.settings.fps) ? "selected" : ""}>${f}</option>`).join("")}</select></label>
      <span class="chip">Picked <b>${p.picked.length} / 10</b></span></div>
    <div class="sheet-body only-refs"><section>
      ${p.error ? `<p class="hint-line">${esc(p.error)}</p>` : ""}
      <div class="anim-grid">${shown.map((a) => `<button type="button" class="anim${picked.has(a.id) ? " on" : ""}" data-anim="${a.id}">
        ${/\.(gif|png|jpe?g|webp)(\?|$)/i.test(a.preview) ? `<img src="${esc(a.preview)}" alt="" loading="lazy">` : `<span class="anim-icon">${icon("play")}</span>`}
        <strong>${esc(a.name)}</strong><small>${esc([a.category, a.sub_category].filter(Boolean).join(" · "))}</small></button>`).join("")}</div>
      ${!shown.length && !p.error ? `<p class="hint-line">No animations match.</p>` : ""}
    </section></div>`, `<span class="cost">${p.picked.length ? `${credits} Meshy credits · ≈ $${(credits / 100).toFixed(2)} · one file with a clip per animation` : "Pick up to 10 animations"}</span>`));
  const search = $("#animSearch");
  search?.addEventListener("input", () => { p.search = search.value; clearTimeout(state.animTimer); state.animTimer = setTimeout(() => renderAnimateSheet().then(() => { const s = $("#animSearch"); s.focus(); s.setSelectionRange(s.value.length, s.value.length); }), 200); });
  $("#animFps")?.addEventListener("change", (e) => { p.settings.fps = Number(e.target.value); });
}

function prepareGameRun() {
  const p = state.preview;
  if (!p) return true;
  if (p.action === "rig") {
    const h = Number($("#rigHeight")?.value);
    if (!(h > 0)) { toast("Enter the character height in metres.", true); return false; }
    p.settings = { ...p.settings, height_meters: h };
  }
  if (p.action === "animate") {
    if (!p.picked.length) { toast("Pick at least one animation.", true); return false; }
    p.action_ids = [...p.picked];
    p.names = p.picked.map((id) => (state.animLib || []).find((a) => a.id === id)?.name || String(id));
  }
  return true;
}

// ---- split an image into parts ----

async function splitDialog(image, body = false) {
  state.split = { image, boxes: [], asAssets: false, note: "" };
  if (body) return guessBodyParts();
  renderSplit();
  try {
    const { boxes } = await api(`/api/studio/detect-parts?path=${encodeURIComponent(image)}`);
    if (state.split?.image === image && !state.split.boxes.length) { state.split.boxes = boxes.map((b, i) => ({ ...b, name: "" })); renderSplit(); }
  } catch { /* draw by hand */ }
}

// Glob looks at the picture and boxes the head, torso, arms, legs and held items.
async function guessBodyParts() {
  const sp = state.split;
  syncSplitNames();
  sp.busy = true;
  sp.note = "Glob is looking for the head, torso, arms and legs…";
  renderSplit();
  try {
    const res = await api("/api/studio/guess-body-parts", { method: "POST", body: { image: sp.image } });
    if (state.split !== sp) return;
    sp.boxes = res.boxes;
    sp.note = res.source === "glob" ? `Boxes by Glob${res.model ? ` (${res.model})` : ""}. Adjust or redraw any that are off.`
      : `${res.note || "Glob had no answer"}, so these boxes come from typical body proportions. Redraw any that are off.`;
  } catch (err) { sp.note = err.message; }
  sp.busy = false;
  renderSplit();
}

function renderSplit() {
  const sp = state.split;
  openDialog(`<div class="dlg wide split-dlg">
    <h2>${icon("scissors")} Split into parts</h2>
    <p>Drag on the picture to draw a box around each part, click × to remove one. <b>Separate figures</b> boxes each character on a sheet; <b>Body parts</b> asks Glob for the head, torso, arms and legs.</p>
    <div class="split-layout">
      <div class="split-stage" id="splitStage"><img src="${esc(fileUrl(sp.image))}" alt="" draggable="false">
        ${sp.boxes.map((b, i) => `<div class="split-box" style="left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%"><span>${i + 1}</span><button type="button" data-split-remove="${i}" aria-label="Remove box">×</button></div>`).join("")}</div>
      <div class="split-side">
        <div class="field-label">Parts · ${sp.boxes.length}</div>
        <div class="split-names">${sp.boxes.map((b, i) => `<label><span>${i + 1}</span><input data-split-name="${i}" value="${esc(b.name)}" placeholder="Part ${i + 1} name"></label>`).join("") || `<p class="hint-line">No boxes yet.</p>`}</div>
        <div class="split-guess"><button type="button" class="ghost" data-split-auto ${sp.busy ? "disabled" : ""}>Separate figures</button><button type="button" class="ghost" data-split-body ${sp.busy ? "disabled" : ""}>${icon("bones")} Body parts</button></div>
        ${sp.note ? `<p class="hint-line split-note">${sp.busy ? `<span class="spinner"></span>` : ""}${esc(sp.note)}</p>` : ""}
        <div class="field-label">Save as</div>
        <label class="target"><input type="radio" name="splitMode" value="parts" ${sp.asAssets ? "" : "checked"}><span>Parts of this asset<small>Cut-outs under Parts</small></span></label>
        <label class="target"><input type="radio" name="splitMode" value="assets" ${sp.asAssets ? "checked" : ""}><span>Separate assets<small>One asset each, the cut-out as its winner</small></span></label>
      </div>
    </div>
    <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button type="button" class="primary" data-split-run ${sp.boxes.length ? "" : "disabled"}>Cut ${sp.boxes.length || ""} part${sp.boxes.length === 1 ? "" : "s"}</button></div>
  </div>`);
  const stage = $("#splitStage");
  stage.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    const rect = stage.getBoundingClientRect();
    const start = { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height };
    const ghost = document.createElement("div");
    ghost.className = "split-box drawing";
    stage.appendChild(ghost);
    stage.setPointerCapture(event.pointerId);
    let box = null;
    const move = (e) => {
      const x = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      const y = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
      box = { x: Math.min(start.x, x), y: Math.min(start.y, y), w: Math.abs(x - start.x), h: Math.abs(y - start.y), name: "" };
      Object.assign(ghost.style, { left: `${box.x * 100}%`, top: `${box.y * 100}%`, width: `${box.w * 100}%`, height: `${box.h * 100}%` });
    };
    const up = () => {
      stage.removeEventListener("pointermove", move);
      syncSplitNames();
      if (box && box.w > 0.02 && box.h > 0.02) sp.boxes.push(box);
      renderSplit();
    };
    stage.addEventListener("pointermove", move);
    stage.addEventListener("pointerup", up, { once: true });
  });
}

function syncSplitNames() {
  document.querySelectorAll("[data-split-name]").forEach((input) => { const b = state.split.boxes[Number(input.dataset.splitName)]; if (b) b.name = input.value; });
  const mode = document.querySelector("input[name=splitMode]:checked");
  if (mode) state.split.asAssets = mode.value === "assets";
}

async function runSplit() {
  syncSplitNames();
  const sp = state.split;
  try {
    const { created } = await api("/api/studio/split", { method: "POST", body: { asset: state.view.path, image: sp.image, boxes: sp.boxes, as_assets: sp.asAssets } });
    closeDialog();
    toast(sp.asAssets ? `Made ${created.length} new asset${created.length === 1 ? "" : "s"}` : `Cut ${created.length} part${created.length === 1 ? "" : "s"}`);
    await loadOverview();
  } catch (err) { toast(err.message, true); }
}

// ---- icons ----

function iconsDialog(image) {
  openDialog(`<form class="dlg" id="iconsForm">
    <h2>${icon("grid")} Inventory icons</h2>
    <p>Square PNGs with a transparent background, trimmed to the subject.</p>
    <div class="icons-preview checker"><img src="${esc(fileUrl(image))}" alt=""></div>
    <div class="dlg-field"><span>Sizes</span><div class="kind-tags">${[32, 64, 128, 256, 512].map((n) => `<label class="kind-chip"><input type="checkbox" name="size" value="${n}" ${[64, 128, 256].includes(n) ? "checked" : ""}><span>${n}px</span></label>`).join("")}</div></div>
    <label>Padding around the subject <input type="range" name="padding" min="0" max="30" value="8"></label>
    <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">Make icons</button></div>
  </form>`);
  $("#iconsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const sizes = [...form.querySelectorAll("input[name=size]:checked")].map((i) => Number(i.value));
    try {
      const { created } = await api("/api/studio/icons", { method: "POST", body: { asset: state.view.path, image, sizes, padding: Number(form.padding.value) / 100 } });
      closeDialog();
      toast(`Made ${created.length} icon${created.length === 1 ? "" : "s"}`);
      renderView();
    } catch (err) { toast(err.message, true); }
  });
}

// ---- engine export ----

function exportDialog(path) {
  const remembered = storeGet("forge.engine") || "godot";
  openDialog(`<form class="dlg" id="exportForm">
    <h2>${icon("box")} Export for your engine</h2>
    <p>A ZIP with models, rigs, animations, icons and sprites of <code>${esc(path || "the whole project")}</code>, laid out for the engine's project folder.</p>
    <div class="target-list">
      ${[["godot", "Godot", "assets/… · GLB"], ["unity", "Unity", "Assets/Art/… · FBX and GLB"], ["unreal", "Unreal", "Content/Art/… · FBX"]].map(([id, label, info]) => `<label class="target"><input type="radio" name="engine" value="${id}" ${id === remembered ? "checked" : ""}><span>${label}<small>${info}</small></span></label>`).join("")}
    </div>
    <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">Download ZIP</button></div>
  </form>`);
  $("#exportForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const engine = event.target.engine.value;
    storeSet("forge.engine", engine);
    try {
      const res = await fetch(`/api/studio/export?project=${encodeURIComponent(state.project)}&engine=${engine}&path=${encodeURIComponent(path)}`);
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Export failed");
      const name = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || `export-${engine}.zip`;
      const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(await res.blob()), download: name });
      link.click();
      URL.revokeObjectURL(link.href);
      closeDialog();
    } catch (err) { toast(err.message, true); }
  });
}

// ---- 3D models: drop, thumbnails, viewer, sprite sheets ----

async function uploadModels(files) {
  const models = [...files].filter((f) => MODEL_RE.test(f.name));
  if (!models.length) return;
  const form = new FormData();
  models.forEach((f) => form.append("files", f));
  try {
    const res = await fetch(`/api/studio/model-upload?project=${encodeURIComponent(state.project)}&path=${encodeURIComponent(state.view.path)}`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    toast(`Added ${data.saved.length} 3D model${data.saved.length === 1 ? "" : "s"}`);
    renderView();
  } catch (err) { toast(err.message, true); }
}

function pickModelFile() {
  const input = Object.assign(document.createElement("input"), { type: "file", accept: ".glb,.gltf,.obj,.fbx", multiple: true });
  input.onchange = () => uploadModels(input.files);
  input.click();
}

const viewerModule = () => import("/ui/viewer.js");
const thumbTried = new Set();

// Models without a preview get one rendered in the browser, once per session.
async function queueThumbs(asset) {
  const missing = ["3d", "rig", "animations"].flatMap((k) => asset.outputs[k] || []).filter((o) => o.type === "model" && !o.thumb && !/\.gltf$/i.test(o.path) && !thumbTried.has(o.path));
  if (!missing.length) return;
  missing.forEach((o) => thumbTried.add(o.path));
  let saved = 0;
  try {
    const viewer = await viewerModule();
    for (const o of missing) {
      try {
        const png = await viewer.renderThumb(fileUrl(o.path), 384);
        await api("/api/studio/thumb", { method: "POST", body: { model: o.path, png } });
        saved += 1;
      } catch { /* leave the placeholder */ }
    }
  } catch { return; }
  if (saved && state.view.path === asset.path && !state.view.editing) renderView();
}

async function viewModel(path, focusSprites = false) {
  openDialog(`<div class="dlg wide viewer-dlg">
    <div class="viewer-head"><h2>${icon("cube")} ${esc(path.split("/").pop())}</h2><button type="button" class="icon-btn" data-close aria-label="Close">✕</button></div>
    <div class="viewer-layout">
      <div class="viewer-stage" id="viewerStage"><p class="hint-line">Loading model…</p></div>
      <aside class="viewer-side" id="viewerSide"></aside>
    </div></div>`);
  const dlg = $("#dialog");
  let view;
  dlg.addEventListener("close", () => view?.dispose(), { once: true });
  try {
    const viewer = await viewerModule();
    const stage = $("#viewerStage");
    stage.innerHTML = "";
    view = await viewer.createViewer(stage, fileUrl(path));
    state.viewer = { path, view, viewer, sheet: null };
  } catch (err) {
    $("#viewerStage").innerHTML = `<p class="hint-line">Could not open this model: ${esc(err.message)}</p>`;
    return;
  }
  const { stats, clips } = view;
  const clipOptions = (sel) => `<option value="-1" ${sel === -1 ? "selected" : ""}>No animation</option>${clips.map((c, i) => `<option value="${i}" ${sel === i ? "selected" : ""}>${esc(c)}</option>`).join("")}`;
  $("#viewerSide").innerHTML = `
    <div class="viewer-stats"><span><b>${stats.triangles.toLocaleString()}</b> triangles</span><span><b>${stats.meshes}</b> meshes</span><span><b>${stats.bones}</b> bones</span><span><b>${clips.length}</b> clips</span></div>
    ${clips.length ? `<label class="field-label">Animation</label><select id="viewerClip">${clipOptions(0)}</select>` : ""}
    <button type="button" class="ghost" data-viewer-thumb>${icon("eye")} Use this view as preview</button>
    <details class="sprite-box" ${focusSprites ? "open" : ""}><summary>${icon("grid")} Sprite sheet</summary>
      <div class="sprite-form">
        <label>Animation<select id="spClip">${clipOptions(clips.length ? 0 : -1)}</select></label>
        <label>Directions<select id="spDirs"><option>1</option><option>4</option><option selected>8</option></select></label>
        <label>Frames<select id="spFrames">${[4, 6, 8, 12, 16, 24].map((n) => `<option ${n === 8 ? "selected" : ""}>${n}</option>`).join("")}</select></label>
        <label>Frame size<select id="spSize">${[64, 96, 128, 256].map((n) => `<option ${n === 128 ? "selected" : ""}>${n}</option>`).join("")}</select></label>
        <label>Camera<select id="spAngle"><option value="iso">Isometric</option><option value="side">Side</option><option value="front">Front</option><option value="top">Top-down</option></select></label>
      </div>
      <button type="button" class="primary" data-sprite-render>Render sheet</button>
      <div id="spriteOut"></div>
    </details>`;
  if (clips.length) { view.play(0); $("#viewerClip").addEventListener("change", (e) => view.play(Number(e.target.value))); }
}

async function renderSprites() {
  const v = state.viewer;
  const out = $("#spriteOut");
  out.innerHTML = `<p class="hint-line">Rendering…</p>`;
  try {
    v.sheet = await v.viewer.renderSpriteSheet(fileUrl(v.path), {
      clip: Number($("#spClip").value), directions: Number($("#spDirs").value), frames: Number($("#spFrames").value),
      size: Number($("#spSize").value), angle: $("#spAngle").value,
    });
    const m = v.sheet.meta;
    out.innerHTML = `<div class="sprite-preview checker"><img src="${v.sheet.dataUrl}" alt=""></div>
      <p class="hint-line">${m.columns}×${m.rows} frames of ${m.frame_width}px${m.fps ? ` · ${m.fps} fps` : ""}</p>
      <button type="button" class="primary" data-sprite-save>Save to asset</button>`;
  } catch (err) { out.innerHTML = `<p class="hint-line">${esc(err.message)}</p>`; }
}

async function saveSprites() {
  const v = state.viewer;
  const m = v.sheet.meta;
  const base = v.path.split("/").pop().replace(/\.[^.]+$/, "");
  try {
    await api("/api/studio/sprite", { method: "POST", body: { asset: state.view.path, name: `${base}-${m.clip ? m.clip.replace(/\W+/g, "-") : "turn"}-${m.directions}dir`, png: v.sheet.dataUrl, meta: m } });
    toast("Sprite sheet saved");
    closeDialog();
    renderView();
  } catch (err) { toast(err.message, true); }
}

async function saveViewerThumb() {
  const v = state.viewer;
  try {
    await api("/api/studio/thumb", { method: "POST", body: { model: v.path, png: v.view.still() } });
    toast("Preview updated");
    renderView();
  } catch (err) { toast(err.message, true); }
}

document.addEventListener("click", (event) => {
  const t = event.target.closest("[data-dismiss-job],[data-view-model],[data-sprites],[data-split],[data-split-body],[data-body-parts],[data-split-remove],[data-split-auto],[data-split-run],[data-icons],[data-export],[data-add-model],[data-anim],[data-viewer-thumb],[data-sprite-render],[data-sprite-save]");
  if (!t || t.disabled) return;
  if (t.dataset.dismissJob) { storeSet("forge.dismissedJobs", JSON.stringify([...dismissedJobs(), t.dataset.dismissJob].slice(-100))); return renderView(); }
  if (t.dataset.viewModel) return viewModel(t.dataset.viewModel);
  if (t.dataset.sprites) return viewModel(t.dataset.sprites, true);
  if (t.dataset.split) return splitDialog(t.dataset.split);
  if (t.dataset.splitRemove) { syncSplitNames(); state.split.boxes.splice(Number(t.dataset.splitRemove), 1); return renderSplit(); }
  if (t.dataset.splitAuto !== undefined) { state.split.boxes = []; return splitDialog(state.split.image); }
  if (t.dataset.splitRun !== undefined) return runSplit();
  if (t.dataset.splitBody !== undefined) return guessBodyParts();
  if (t.dataset.bodyParts) return splitDialog(t.dataset.bodyParts, true);
  if (t.dataset.icons) return iconsDialog(t.dataset.icons);
  if (t.dataset.export !== undefined) return exportDialog(t.dataset.export);
  if (t.dataset.addModel !== undefined) return pickModelFile();
  if (t.dataset.anim) {
    const p = state.preview;
    const id = Number(t.dataset.anim);
    if (p.picked.includes(id)) p.picked = p.picked.filter((x) => x !== id);
    else if (p.picked.length < 10) p.picked.push(id);
    else toast("Up to 10 animations per run.", true);
    return renderAnimateSheet();
  }
  if (t.dataset.viewerThumb !== undefined) return saveViewerThumb();
  if (t.dataset.spriteRender !== undefined) return renderSprites();
  if (t.dataset.spriteSave !== undefined) return saveSprites();
});

// ---------- mobile ----------

function showPanel(which, onlyIfMobile = false) {
  if (onlyIfMobile && !matchMedia("(max-width: 900px)").matches) return;
  document.querySelector(".shell").dataset.active = which;
  document.querySelectorAll(".mobile-switch button").forEach((b) => b.classList.toggle("active", b.dataset.show === which));
}

// ---------- live updates ----------

function connectSocket() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  let pending;
  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === "studio_job" && msg.project === state.project) {
      const known = state.jobs[msg.job.id];
      state.jobs[msg.job.id] = msg.job;
      if (msg.job.status === "failed") toast(`${ACTION_LABEL[msg.job.action] || "Job"} failed: ${msg.job.error}`, true);
      else if (msg.job.status === "done" && known?.status !== "done") toast(`${ACTION_LABEL[msg.job.action] || "Job"} finished`);
      if (state.view.path === msg.job.asset && !state.view.editing) renderView();
    }
    if (msg.type === "cli_event" && msg.project === state.project) {
      const live = (state.live[msg.session_id] ||= { text: "", tools: [] });
      if (msg.event.kind === "text") live.text += (live.text ? "\n\n" : "") + msg.event.text;
      else if (msg.event.kind === "tool") live.tools.push(msg.event.text);
      if (msg.session_id === state.sessionId) renderMessages();
    }
    if (msg.type === "cli_done" && msg.project === state.project) {
      delete state.live[msg.session_id];
      if (msg.session_id === state.sessionId) openSession(msg.session_id).catch(() => {});
      else renderTabs();
    }
    if (msg.type === "workspace_changed" && (!msg.project || msg.project === state.project) && !state.view.editing) {
      clearTimeout(pending);
      pending = setTimeout(loadOverview, 250);
    }
  };
  ws.onclose = () => setTimeout(connectSocket, 3000);
}

// ---------- events ----------

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-compact-now],[data-pick-winner-file],[data-bible-toggle],[data-bible-view],[data-new-chat],[data-settings-coding],[data-cli-login],[data-pick-model],[data-pick-effort],[data-pick-cli-model],[data-pick-cli-effort],[data-close-settings],[data-settings-tab],[data-add-preset],[data-cancel-add],[data-probe],[data-save-endpoint],[data-pick-check],[data-test-model],[data-remove-endpoint],[data-import-vscode],[data-new-project],[data-delete-project],[data-open-project],[data-ob-skip],[data-ob-next],[data-ob-back],[data-ob-forge],[data-ob-enter],[data-ob-dim],[data-ob-chip],[data-make-pillar],[data-style-text],[data-remove-style],[data-remove-pillar],[data-toc],[data-upload],[data-new-asset],[data-new-folder],[data-to-folder],[data-make-asset],[data-action],[data-winner],[data-ref],[data-add-ref],[data-rebuild],[data-run],[data-open],[data-folder],[data-home],[data-vault],[data-vault-filter],[data-vault-zip],[data-vault-folder],[data-poly],[data-session],[data-delete],[data-suggest],[data-unattach],[data-settings],[data-close],[data-edit],[data-cancel-edit],[data-save],#newChatBtn,[data-show]");
  if (!t) return;
  if (t.dataset.delete) { event.stopPropagation(); return deleteChat(t.dataset.delete); }
  if (t.dataset.upload) return uploadTo(t.dataset.upload);
  if (t.dataset.newAsset !== undefined) return newAssetDialog(t.dataset.newAsset);
  if (t.dataset.toc) { event.preventDefault(); return document.getElementById(t.dataset.toc)?.scrollIntoView({ behavior: "smooth", block: "start" }); }
  if (t.dataset.pickWinnerFile !== undefined) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp";
    input.onchange = () => addCandidateImages(input.files);
    return input.click();
  }
  if (t.dataset.compactNow !== undefined) {
    try { state.session = await api(`/api/agent/sessions/${state.sessionId}/compact`, { method: "POST" }); toast("Chat compacted"); renderMessages(); } catch (err) { toast(err.message, true); }
    return;
  }
  if (t.dataset.bibleToggle !== undefined) { if (event.target.tagName !== "INPUT") return; syncPreviewFields(); state.preview.use_bible = event.target.checked; applyBible(state.preview); return renderPreview(); }
  if (t.dataset.bibleView !== undefined) { syncPreviewFields(); state.preview.showBible = !state.preview.showBible; return renderPreview(); }
  if (t.dataset.pickCliModel !== undefined) return pickCli({ cli_model: t.dataset.pickCliModel });
  if (t.dataset.pickCliEffort) return pickCli({ cli_effort: t.dataset.pickCliEffort === "default" ? "" : t.dataset.pickCliEffort });
  if (t.dataset.pickModel) return pickModel(t.dataset.pickModel);
  if (t.dataset.pickEffort) return pickEffort(t.dataset.pickEffort);
  if (t.dataset.closeSettings !== undefined) return closeSettings();
  if (t.dataset.settingsTab) { state.settings.tab = t.dataset.settingsTab; state.settings.adding = null; return renderSettings(); }
  if (t.dataset.addPreset) { state.settings.adding = { preset: t.dataset.addPreset, picked: [] }; return renderSettings(); }
  if (t.dataset.cancelAdd !== undefined) { state.settings.adding = null; return renderSettings(); }
  if (t.dataset.probe !== undefined) return probeEndpoint();
  if (t.dataset.saveEndpoint !== undefined) return saveEndpoint();
  if (t.dataset.pickCheck) {
    syncAddCard();
    const a = state.settings.adding;
    a.picked = event.target.checked ? [...new Set([...a.picked, t.dataset.pickCheck])] : a.picked.filter((m) => m !== t.dataset.pickCheck);
    const btn = $("[data-save-endpoint]");
    if (btn) { btn.disabled = !a.picked.length; btn.textContent = `Add ${a.picked.length || ""} model${a.picked.length === 1 ? "" : "s"}`; }
    return;
  }
  if (t.dataset.testModel) return testModel(t.dataset.testModel);
  if (t.dataset.removeEndpoint) {
    try { await api(`/api/agent/endpoints/${t.dataset.removeEndpoint}`, { method: "DELETE" }); toast("Removed"); } catch (err) { toast(err.message, true); }
    return renderSettings();
  }
  if (t.dataset.importVscode !== undefined) {
    try { const { imported } = await api("/api/agent/endpoints/import-vscode", { method: "POST" }); toast(`Imported ${imported.reduce((n, e) => n + e.models.length, 0)} models from VS Code`); } catch (err) { toast(err.message, true); }
    return renderSettings();
  }
  if (t.dataset.newProject !== undefined) return startOnboarding();
  if (t.dataset.deleteProject) { event.stopPropagation(); return deleteProjectDialog(t.dataset.deleteProject); }
  if (t.dataset.openProject) { closeProjects(); return switchProject(t.dataset.openProject); }
  if (t.dataset.obSkip !== undefined) return skipOnboarding();
  if (t.dataset.obNext !== undefined) { syncOnboarding(); state.ob.step += 1; return renderOnboarding(); }
  if (t.dataset.obBack !== undefined) { syncOnboarding(); state.ob.step -= 1; return renderOnboarding(); }
  if (t.dataset.obForge !== undefined) return forgeWorld();
  if (t.dataset.obEnter !== undefined) return finishOnboarding();
  if (t.dataset.obDim) { syncOnboarding(); state.ob.dimension = t.dataset.obDim; return renderOnboarding(); }
  if (t.dataset.obChip) {
    syncOnboarding();
    const list = state.ob[t.dataset.obChip];
    const i = list.indexOf(t.dataset.value);
    if (i >= 0) list.splice(i, 1); else list.push(t.dataset.value);
    return renderOnboarding();
  }
  if (t.dataset.makePillar) return makePillarDialog(t.dataset.makePillar);
  if (t.dataset.styleText) return addStyleText(t.dataset.styleText);
  if (t.dataset.removeStyle) return removeStyleText(t.dataset.removeStyle);
  if (t.dataset.removePillar) return removePillar(t.dataset.removePillar);
  if (t.dataset.newFolder !== undefined) return newFolderDialog(t.dataset.newFolder);
  if (t.dataset.toFolder !== undefined) return docToFolder();
  if (t.dataset.makeAsset !== undefined) return makeAsset();
  if (t.dataset.action) return openPreview(t.dataset.action, t.dataset.source || "");
  if (t.dataset.winner) return pickWinner(t.dataset.winner);
  if (t.dataset.ref !== undefined) {
    if (event.target.tagName !== "INPUT") return;
    syncPreviewFields();
    state.preview.references[Number(t.dataset.ref)].on = event.target.checked;
    return renderPreview();
  }
  if (t.dataset.addRef !== undefined) {
    syncPreviewFields();
    const p = state.preview;
    return uploadTo(`${p.asset.replace(/\.md$/i, "")}/refs`, (saved) => {
      p.references.push(...saved.map((path) => ({ path, source: "Added", role: "extra", on: true })));
      renderPreview();
    });
  }
  if (t.dataset.rebuild !== undefined) return rebuildPreview();
  if (t.dataset.run !== undefined) return runPreview();
  if (t.dataset.open !== undefined) { event.preventDefault(); return openPath(t.dataset.open); }
  if (t.dataset.folder !== undefined) return go({ type: "folder", path: t.dataset.folder });
  if (t.dataset.home !== undefined) return go({ type: "home" });
  if (t.dataset.vault !== undefined) return go({ type: "vault" });
  if (t.dataset.vaultFilter) { state.vaultHistory = !!$("#vaultHistory")?.checked; state.vaultFilter = t.dataset.vaultFilter; return renderVault(); }
  if (t.dataset.vaultZip !== undefined) return vaultZip(t.dataset.vaultZip);
  if (t.dataset.vaultFolder !== undefined) return vaultToFolder();
  if (t.dataset.poly) {
    syncPreviewFields();
    state.preview.settings.target_polycount = Number(t.dataset.poly);
    return renderPreview();
  }
  if (t.dataset.session) return openSession(t.dataset.session);
  if (t.id === "newChatBtn") { await loadClis(); renderNewTabPop(); $("#newTabPop").hidden = !$("#newTabPop").hidden; return; }
  if (t.dataset.newChat) { $("#newTabPop") && ($("#newTabPop").hidden = true); closeSettings(); return newChat(t.dataset.newChat); }
  if (t.dataset.settingsCoding !== undefined) return settingsDialog("coding");
  if (t.dataset.cliLogin) {
    try { await api(`/api/agent/clis/${t.dataset.cliLogin}/login`, { method: "POST" }); toast("A terminal window opened. Finish there, then come back."); } catch (err) { toast(err.message, true); }
    return;
  }
  if (t.dataset.suggest) { const input = $("#input"); input.value = t.dataset.suggest; input.focus(); input.setSelectionRange(input.value.length, input.value.length); return; }
  if (t.dataset.unattach !== undefined) { state.attachments.splice(Number(t.dataset.unattach), 1); return renderAttachments(); }
  if (t.dataset.settings !== undefined) return settingsDialog("models");
  if (t.dataset.close !== undefined) return closeDialog();
  if (t.dataset.edit !== undefined) { state.view = { ...state.view, editing: true }; return renderView(); }
  if (t.dataset.cancelEdit !== undefined) { state.view = { ...state.view, editing: false }; return renderView(); }
  if (t.dataset.save !== undefined) return saveDoc();
  if (t.dataset.show) return showPanel(t.dataset.show);
});

$("#composer").addEventListener("submit", (event) => { event.preventDefault(); send(); });
$("#input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
});
$("#input").addEventListener("paste", (event) => {
  const files = [...(event.clipboardData?.files || [])];
  if (files.length) { event.preventDefault(); addFiles(files); }
});
$("#fileInput").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
$("#projectsBtn").addEventListener("click", openProjects);
$("#closeProjectsBtn").addEventListener("click", closeProjects);
$("#newProjectBtn").addEventListener("click", startOnboarding);
$("#projectSearch").addEventListener("input", renderProjectGrid);
$("#projectSort").addEventListener("change", renderProjectGrid);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || $("#onboarding").hidden || event.target.tagName === "TEXTAREA") return;
  if ($("#obName")) { event.preventDefault(); syncOnboarding(); state.ob.step = 2; renderOnboarding(); }
});
$("#settingsBtn").addEventListener("click", () => settingsDialog());
$("#refreshBtn").addEventListener("click", loadOverview);
$("#modelPill").addEventListener("click", (event) => { event.stopPropagation(); toggleModelPop(); });
$("#brandBtn").addEventListener("click", openProjects);
$("#input").addEventListener("input", autoGrow);
document.addEventListener("click", (event) => {
  if (!event.target.closest(".model-pill-wrap")) toggleModelPop(false);
  if (!event.target.closest("#newTabPop, #newChatBtn")) $("#newTabPop").hidden = true;
});
$("#stopBtn").addEventListener("click", stopCli);
$("#backBtn").addEventListener("click", goBack);
function setChatHidden(hidden) {
  document.querySelector(".shell").classList.toggle("chat-hidden", hidden);
  storeSet("forge.chatHidden", hidden ? "1" : "0");
}
setChatHidden(storeGet("forge.chatHidden") === "1");
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-toggle-chat]")) setChatHidden(!document.querySelector(".shell").classList.contains("chat-hidden"));
});
document.addEventListener("submit", (event) => {
  if (!event.target.matches("[data-cli-custom]")) return;
  event.preventDefault();
  const id = event.target.model.value.trim();
  if (id) pickCli({ cli_model: id });
});
// Drag images from the desktop onto an asset's winner box.
// Drag images or 3D models from the desktop onto an asset page: images become candidates (and the winner),
// models go to the asset's 3D section.
const dropZone = (event) => event.target.closest?.("[data-drop-winner]") || event.target.closest?.("[data-drop-asset]");
document.addEventListener("dragover", (event) => {
  const zone = dropZone(event);
  if (!zone || !event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault();
  zone.classList.add("over");
});
document.addEventListener("dragleave", (event) => {
  if (event.relatedTarget && dropZone({ target: event.relatedTarget })) return;
  document.querySelectorAll("[data-drop-winner].over, [data-drop-asset].over").forEach((z) => z.classList.remove("over"));
});
document.addEventListener("drop", (event) => {
  const zone = dropZone(event);
  if (!zone) return;
  event.preventDefault();
  document.querySelectorAll("[data-drop-winner].over, [data-drop-asset].over").forEach((z) => z.classList.remove("over"));
  const files = [...event.dataTransfer.files];
  const models = files.filter((f) => MODEL_RE.test(f.name));
  const images = files.filter((f) => !MODEL_RE.test(f.name));
  if (models.length) uploadModels(models);
  if (images.length) addCandidateImages(images);
});
$("#settingsView").addEventListener("input", (event) => {
  if (event.target.id === "addFilter") { syncAddCard(); renderSettings().then(() => { const f = $("#addFilter"); f.focus(); f.setSelectionRange(f.value.length, f.value.length); }); }
});

(async function init() {
  try {
    await loadProjects();
    restoreView();
    await loadModels();
    state.agentCfg = await api("/api/config").catch(() => null);
    await loadClis();
    await loadJobs();
    await Promise.all([loadSessions(), loadOverview()]);
    connectSocket();
  } catch (err) {
    toast(`Could not start: ${err.message}`, true);
  }
})();
