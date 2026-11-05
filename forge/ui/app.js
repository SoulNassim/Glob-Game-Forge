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
  connect: null, // the game folder this project is connected to (coding agent + deliveries)
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
// `action` adds one button to the toast, e.g. { label: "Undo", run() {…} }, and keeps it up a little longer.
function toast(message, isError = false, action = null) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast${isError ? " error" : ""}${action ? " has-action" : ""}`;
  if (action) {
    const btn = Object.assign(document.createElement("button"), { type: "button", className: "toast-act", textContent: action.label });
    btn.addEventListener("click", () => { el.hidden = true; action.run(); }, { once: true });
    el.append(btn);
  }
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, action ? 7000 : 3500);
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
    if (!line.trim() || /^\s*<!--.*-->\s*$/.test(line)) { i++; continue; }
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
  // `?project=` comes from a coding agent opening Glob on the project it works on; read it once.
  const url = new URL(location.href);
  const linked = url.searchParams.get("project");
  if (linked) { url.searchParams.delete("project"); history.replaceState(null, "", url); }
  const remembered = preferred || linked || storeGet("forge.project") || data.default_project;
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
  showScene($("#projectsView"));
  $("#closeProjectsBtn").hidden = !state.project;
  $("#projectGrid").innerHTML = `<div class="pv-loading">Loading projects…</div>`;
  try { state.cards = (await api("/api/projects/cards")).projects; } catch (err) { toast(err.message, true); state.cards = []; }
  renderProjectGrid();
  api("/api/storage").then((info) => {
    $("#projectsFoot").innerHTML = `Projects are saved in <code>${esc(info.projects_dir)}</code> <button class="link-btn" data-st-open="projects">Open folder</button> · <button class="link-btn" data-st-move>Change</button>`;
  }).catch(() => {});
}

function closeProjects() { hideScene($("#projectsView")); }

// ---------- full-screen scenes ----------
// Projects, settings, setup, new project and import each cover the app. Going from one to the next
// cross-fades: the old scene stays up under the new one until it has faded in, so the app never shows
// through. A scene is only taken down after a short buffer, in case the next one opens right after.
const SCENE_BUFFER = 140;
const SCENE_FADE = 520;
const sceneCovers = () => [...document.querySelectorAll("#projectsView, #settingsView, .onboard")];

function showScene(el) {
  const behind = sceneCovers().some((s) => s !== el && !s.hidden);
  clearTimeout(el._sceneTimer);
  delete el.dataset.leaving;
  el.classList.remove("scene-out");
  if (!el.hidden) return;
  el.classList.toggle("scene-over", behind);
  el.hidden = false;
  el._sceneTimer = setTimeout(() => el.classList.remove("scene-over"), SCENE_FADE);
}

function hideScene(el) {
  if (el.hidden || el.dataset.leaving) return;
  el.dataset.leaving = "1";
  el._sceneTimer = setTimeout(() => {
    const next = sceneCovers().some((s) => s !== el && !s.hidden && !s.dataset.leaving);
    const done = () => { el.hidden = true; el.classList.remove("scene-out", "scene-over"); delete el.dataset.leaving; };
    if (next) { el._sceneTimer = setTimeout(done, SCENE_FADE); return; } // the next scene fades in over this one
    el.classList.add("scene-out");
    el._sceneTimer = setTimeout(done, 320);
  }, SCENE_BUFFER);
}

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
      // Two projects with one name: say which folder each is.
      const twin = cards.filter((x) => x.title.toLowerCase() === c.title.toLowerCase()).length > 1;
      return `<div class="pcard${c.project === state.project ? " current" : ""}" data-open-project="${esc(c.project)}" tabindex="0" role="button">
        <div class="pcard-head"><div><strong>${esc(c.title)}</strong>${twin ? `<small class="pcard-folder">${esc(humanize(c.project))}</small>` : ""}<small>${esc(timeAgo(c.updated))}</small>
          <small>${c.sections} section${c.sections === 1 ? "" : "s"} · ${c.assets} asset${c.assets === 1 ? "" : "s"}</small></div>
          <button class="pcard-del" data-st-export="${esc(c.project)}" title="Export workspace as .zip" aria-label="Export ${esc(c.title)} workspace as .zip"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></button>
          ${c.project === "default" ? "" : `<button class="pcard-del" data-delete-project="${esc(c.project)}" title="Delete project" aria-label="Delete ${esc(c.title)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg></button>`}</div>
        ${c.cover ? `<div class="pcard-art cover"><img src="${esc(projectFileUrl(c.project, c.cover))}" alt="" loading="lazy"></div>`
          : `<div class="pcard-art n${Math.min(imgs.length, 3)}">${imgs.length ? imgs.join("") : `<span class="pcard-initials">${esc(initials || "F")}</span>`}
            ${c.assets ? `<button class="pcard-keyart" data-key-art="${esc(c.project)}" title="Generate a cover image for this project from its pitch">${icon("sparkle")}Make key art</button>` : ""}</div>`}
        ${c.project === state.project ? `<span class="pcard-badge">Open</span>` : ""}
      </div>`;
    }).join("")}
    ${(state.cards || []).some((c) => c.project.startsWith("pocket_kaiju")) || query ? "" : `<button class="pcard pcard-sample" data-install-sample><span class="pcard-sample-art"><img src="/ui/sample-cover.jpg" alt=""></span>
      <span class="pcard-sample-text"><strong>Explore a finished project</strong><small>Pocket Kaiju: characters, 3D models and sounds, ready to click through. No credits used.</small></span></button>`}`;
}

function deleteProjectDialog(project) {
  const card = (state.cards || []).find((c) => c.project === project);
  openDialog(`
    <form class="dlg" id="deleteProjectForm">
      <h2>Delete ${esc(card?.title || humanize(project))}?</h2>
      <p>Glob forgets the project (its entry goes to <code>projects/.trash/</code>). A project that lives in a game folder keeps its files there, in <code>glob/</code>.</p>
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

const GENRES = ["Action", "Arcade", "Action RPG", "Survival horror", "Cozy sim", "Platformer", "Strategy", "Puzzle", "Racing", "Roguelike", "Visual novel", "Shooter"];
const LOOKS = ["Low-poly", "Stylized hand-painted", "Realistic PBR", "Toon / cel-shaded", "Pixel art", "Voxel", "Dark fantasy", "Sci-fi hard-surface"];

// `seamless` comes from first-run setup: same backdrop, so it swaps in without a fade and skips the intro.
function startOnboarding(seamless = false) {
  closeProjects();
  state.ob = { step: seamless === true ? 1 : 0, seamless: seamless === true, game: "", home: "", name: "", pitch: "", genres: [], looks: [], dimension: "3D", rules: "" };
  const el = $("#onboarding");
  spawnPetals(el);
  el.classList.toggle("instant", seamless === true);
  showScene(el);
  el.classList.remove("forging");
  renderOnboarding();
}

// One set of petals for every scene, timed from page load: a scene that takes over from another
// shows them exactly where they were, so nothing jumps.
const PETALS = Array.from({ length: 22 }, () => ({ x: Math.random() * 100, d: 8 + Math.random() * 10, phase: Math.random() * 18, s: 0.5 + Math.random() * 0.9, r: Math.random() * 360, sway: 20 + Math.random() * 60 }));

function spawnPetals(scene) {
  const t = performance.now() / 1000;
  scene.querySelector(".petals").innerHTML = PETALS.map((p) => `<i style="--x:${p.x}%;--d:${p.d}s;--delay:${-(p.phase + t).toFixed(2)}s;--s:${p.s};--r:${p.r}deg;--sway:${p.sway}px"></i>`).join("");
}

function chipGroup(name, options, selected) {
  return `<div class="ob-chips">${options.map((o) => `<button type="button" class="ob-chip${selected.includes(o) ? " on" : ""}" data-ob-chip="${esc(name)}" data-value="${esc(o)}">${esc(o)}</button>`).join("")}</div>`;
}

// The game folder, and where Glob keeps the project: in the game's glob/ folder unless the user picks another place.
// Where the game is: pick it (a Windows/macOS folder window), paste its path, or have none yet.
function obFolders(ob) {
  if (ob.noGame) {
    return `<div class="ob-folder on static"><strong>No game folder yet</strong><small>Glob keeps the project in its own folder. Link your game any time in Settings → Your game.</small></div>
      <p class="ob-alt"><button type="button" class="link-btn" data-ob-hasgame>I have a game folder</button></p>`;
  }
  const where = ob.home ? `${ob.home.replace(/[\\/]+$/, "")}/${ob.name || "<project name>"}` : ob.game ? `${ob.game.replace(/[\\/]+$/, "")}/glob` : "your game folder, in glob/";
  return `<button type="button" class="ob-folder${ob.game ? " on" : ""}" data-ob-game>${ob.game ? `<strong>${esc(ob.game.split(/[\\/]/).filter(Boolean).pop())}</strong><small>${esc(ob.game)}</small>` : `<strong>Choose game folder…</strong><small>Godot, Unity, Unreal or anything else</small>`}</button>
    ${ob.picking ? `<p class="ob-picking"><span class="spinner"></span>A folder window opened. If you can't see it, it's behind this window.</p>` : ""}
    <div class="ob-path"><input id="obGamePath" spellcheck="false" placeholder="or paste the folder's path, e.g. D:\\Games\\MyGame" value="${esc(ob.typed || "")}"><button type="button" class="ob-back" data-ob-path>Use</button></div>
    ${ob.game ? `<div class="ob-home"><span>Glob's workspace</span><code title="${esc(where)}"><bdi>${esc(where)}</bdi></code>
      <button type="button" class="link-btn" data-ob-home>${ob.home ? "Change…" : "Keep it somewhere else…"}</button>
      ${ob.home ? `<button type="button" class="link-btn" data-ob-home-reset>Back to the game folder</button>` : ""}</div>` : ""}
    <p class="ob-alt"><button type="button" class="link-btn" data-ob-nogame>No game folder yet? Start without one</button></p>`;
}

function renderOnboarding() {
  const ob = state.ob;
  const steps = [
    () => `<div class="ob-step intro"><img class="ob-glob" src="/ui/glob-forge.svg" alt=""><p class="ob-kicker">Glob</p><h1>Every world starts<br>with a <em>spark</em>.</h1>
      <p class="ob-sub">Four quick questions and the agent builds your project: sections, pages, first assets and a style bible.</p>
      <div class="ob-nav"><button class="ob-next" data-ob-next>Begin</button><button class="ob-back" data-imp-open title="Import a Godot, Unity or Unreal project">Import a game</button></div>
      <button class="ob-sample" data-install-sample>${icon("eye")} Or look around a finished sample project first</button></div>`,
    () => `<div class="ob-step"><p class="ob-kicker">1 / 4 · Your game</p><h1>Where's your <em>game</em>?</h1>
      <p class="ob-sub">Pick the game's folder, even an empty one. Glob keeps this project inside it in <code>glob/</code> (or wherever you choose), and your coding agent finds finished assets in <code>glob/ASSETS.md</code>.</p>
      ${obFolders(ob)}
      <div class="ob-nav">${ob.step > 0 && !ob.seamless ? `<button class="ob-back" data-ob-back>Back</button>` : ""}<button class="ob-next" data-ob-next ${ob.game || ob.noGame ? "" : "disabled"}>Continue</button></div>
      ${ob.game || ob.noGame ? "" : `<p class="ob-why">Pick, paste or skip the game folder to continue.</p>`}</div>`,
    () => `<div class="ob-step"><p class="ob-kicker">2 / 4 · Name</p><h1>What's it called?</h1>
      <input id="obName" class="ob-input" maxlength="60" placeholder="The Hollow Below" value="${esc(ob.name)}" autofocus>
      <div class="ob-nav"><button class="ob-back" data-ob-back>Back</button><button class="ob-next" data-ob-next>Continue</button></div></div>`,
    () => `<div class="ob-step"><p class="ob-kicker">3 / 4 · The game</p><h1>Tell me about it.</h1>
      <textarea id="obPitch" class="ob-input" rows="3" placeholder="A cozy farming game on floating islands where you tame storm spirits…">${esc(ob.pitch)}</textarea>
      ${chipGroup("genres", GENRES, ob.genres)}
      <div class="ob-nav"><button class="ob-back" data-ob-back>Back</button><button class="ob-next" data-ob-next>Continue</button></div></div>`,
    () => `<div class="ob-step"><p class="ob-kicker">4 / 4 · The look</p><h1>How should it look?</h1>
      ${chipGroup("looks", LOOKS, ob.looks)}
      <div class="ob-dim">${["3D", "2D"].map((d) => `<button type="button" class="ob-chip${ob.dimension === d ? " on" : ""}" data-ob-dim="${d}">${d}</button>`).join("")}</div>
      <textarea id="obRules" class="ob-input small" rows="3" placeholder="Asset rules for every image, e.g. T-pose, front view, under 10k triangles, muted palette…">${esc(ob.rules)}</textarea>
      <div class="ob-nav"><button class="ob-back" data-ob-back>Back</button><button class="ob-next forge" data-ob-forge>✦ Forge my world</button></div></div>`,
    // "Skip · start blank": no questions, but the project still needs its game and a home.
    () => `<div class="ob-step"><p class="ob-kicker">Blank project</p><h1>Start <em>blank</em>.</h1>
      <p class="ob-sub">An empty project. Fill it yourself or ask Glob in the chat.</p>
      <input id="obName" class="ob-input" maxlength="60" placeholder="Project name" value="${esc(ob.name)}">
      ${obFolders(ob)}
      <div class="ob-nav">${ob.seamless ? "" : `<button class="ob-back" data-ob-restart>Answer the questions instead</button>`}<button class="ob-next" data-ob-blank ${ob.game || ob.noGame ? "" : "disabled"}>Create the project</button></div></div>`,
  ];
  $("#onboarding").classList.toggle("blank", ob.step === OB_BLANK);
  $("#obStage").innerHTML = steps[ob.step]();
  // Animate in only on a new step: picking a folder or a chip redraws the same step, which should stay still.
  if (ob.shown !== ob.step) {
    ob.shown = ob.step;
    $("#obStage").firstElementChild.classList.add("enter");
    setTimeout(() => $("#obStage [autofocus], #obStage textarea")?.focus(), 350);
  }
}

function syncOnboarding() {
  const ob = state.ob;
  if ($("#obGamePath")) ob.typed = $("#obGamePath").value.trim();
  if ($("#obName")) ob.name = $("#obName").value.trim();
  if ($("#obPitch")) ob.pitch = $("#obPitch").value.trim();
  if ($("#obRules")) ob.rules = $("#obRules").value.trim();
}

async function createProject(name, gameFolder, homeFolder = "") {
  const created = await api("/api/projects", { method: "POST", body: { name: name || "Untitled project", unique: true, game_folder: gameFolder || undefined, home_folder: homeFolder || undefined, create_game_folder: Boolean(gameFolder) } });
  state.project = created.project;
  storeSet("forge.project", created.project);
  if (created.existing) toast(`This game already has a Glob project: opened ${humanize(created.project)}`);
  return created.project;
}

async function chooseGameFolder() {
  syncOnboarding();
  state.ob.picking = true;
  renderOnboarding();
  const folder = await chooseFolder("Choose your game's folder");
  state.ob.picking = false;
  if (!folder) return renderOnboarding();
  state.ob.game = folder;
  state.ob.noGame = false;
  if (!state.ob.name) state.ob.name = folder.split(/[\\/]/).filter(Boolean).pop().replace(/[-_]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
  renderOnboarding();
}

const OB_BLANK = 5;

// Back to where the user was, with nothing created.
function cancelOnboarding() {
  hideScene($("#onboarding"));
}

function skipOnboarding() {
  syncOnboarding();
  state.ob.step = OB_BLANK;
  renderOnboarding();
}

async function chooseHomeFolder() {
  syncOnboarding();
  const folder = await chooseFolder("Where should Glob keep this project's workspace?");
  if (!folder) return;
  state.ob.home = folder;
  renderOnboarding();
}

async function createBlank() {
  syncOnboarding();
  if (!state.ob.game && !state.ob.noGame) return toast("Pick, paste or skip your game's folder first.", true);
  try {
    const project = await createProject(state.ob.name, state.ob.game, state.ob.home);
    await switchProject(project); // load behind the scene, so the app is ready when it fades out
    hideScene($("#onboarding"));
    toast(`Created ${humanize(project)}: a blank workspace`);
  } catch (err) { toast(err.message, true); }
}

async function forgeWorld() {
  syncOnboarding();
  const ob = state.ob;
  if (!ob.game && !ob.noGame) { ob.step = 1; renderOnboarding(); toast("Pick, paste or skip your game's folder first.", true); return; }
  if (!ob.name) { ob.step = 2; renderOnboarding(); toast("Give your world a name first.", true); return; }
  $("#onboarding").classList.add("forging");
  $("#obStage").innerHTML = `<div class="ob-step forging-step enter"><div class="ob-orb"><span></span><span></span><span></span></div>
    <p class="ob-kicker">Forging</p><h1>${esc(ob.name)}</h1><ul class="ob-log" id="obLog"><li>Creating the project…</li></ul></div>`;
  const log = (text, cls = "") => { $("#obLog").insertAdjacentHTML("beforeend", `<li class="${cls}">${text}</li>`); };
  let poll;
  try {
    const project = await createProject(ob.name, ob.game, ob.home);
    await api("/api/bible", { method: "PUT", body: { project_dimension: ob.dimension === "2D" ? "2d" : "3d" } }).catch(() => {});
    const look = [ob.dimension, ...ob.looks].join(", ");
    // Spell the dimension out: image models read a bare "3D, Toon" as a flat cartoon drawing.
    const lookLine = ob.dimension === "3D"
      ? `- Look: rendered 3D game asset${ob.looks.length ? ` (${ob.looks.join(", ")})` : ""}, like a model in the game engine, not a flat 2D drawing`
      : `- Look: 2D game art${ob.looks.length ? ` (${ob.looks.join(", ")})` : ""}`;
    const rules = [lookLine, ...ob.rules.split(/\n|;/).map((r) => r.trim()).filter(Boolean).map((r) => `- ${r}`)].filter(Boolean);
    if (rules.length) {
      await api("/api/overview/file", { method: "PUT", body: { path: "pillars/style-bible.md", content: `# Style bible\n\n${rules.join("\n")}\n` } });
      log("Style bible written: put in front of every image prompt", "ok");
    }
    if (!state.models.some((m) => m.ready)) {
      // No chat model: the coding agent (or the user) builds the structure with the glob skill instead.
      const brief = [`/glob Create a project structure for my game "${ob.name}".`, ob.pitch && `Pitch: ${ob.pitch}`, ob.genres.length && `Genre: ${ob.genres.join(", ")}.`, look && `Look: ${look}.`].filter(Boolean).join(" ");
      await api("/api/overview/file", { method: "PUT", body: { path: "README.md", content: `# ${ob.name}\n\n${ob.pitch || "A new game."}\n` } });
      log("Front page written", "ok");
      log("No chat model is connected, so Glob can't design the sections here. Connect your game in Settings → Coding agent and give your agent this:", "");
      $("#obLog").insertAdjacentHTML("afterend", `<pre class="cn-cmd ob-brief"><code>${esc(brief)}</code></pre><button class="ob-back" data-cn-copy="${esc(brief)}">Copy</button>`);
      $(".ob-brief + button").insertAdjacentHTML("afterend", `<button class="ob-next forge" data-ob-enter>Enter your world →</button>`);
      forged();
      return;
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
  forged();
}

// The world is made: the "Forging" screen settles so it no longer looks busy.
function forged() {
  $(".forging-step")?.classList.add("forged");
  const kicker = $(".forging-step .ob-kicker");
  if (kicker) kicker.textContent = "Ready";
  $("[data-ob-enter]")?.focus();
}

async function finishOnboarding() {
  await switchProject(state.project);
  hideScene($("#onboarding"));
}

async function switchProject(name) {
  await loadProjects(name);
  state.view = { type: "home" };
  state.trail = [];
  await loadJobs();
  renderJobDock();
  state.palette = state.board = state.boardView = null;
  await Promise.all([loadSessions(), loadOverview(), loadConnect()]);
}

// ---------- chat ----------

async function loadSessions() {
  const data = await api("/api/agent/sessions");
  state.sessions = (data.sessions || []).filter((s) => s.group !== "Archive" || s.message_count);
  applyChatMode();
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
  const created = await api("/api/agent/sessions", { method: "POST", body: { title: "New chat", model: state.model, effort: state.effort } });
  state.sessions.push(created);
  state.sessionId = created.id;
  state.session = created;
  storeSet(`forge.session.${state.project}`, created.id);
  renderTabs();
  return created.id;
}

async function newChat() {
  state.sessionId = "";
  state.session = null;
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
    <button class="tab${s.id === state.sessionId ? " active" : ""}" data-session="${s.id}" role="tab" title="${esc(s.title)}">
      <img src="/ui/glob-face.svg" alt=""><span>${esc(s.title || "New chat")}</span><span class="x" data-delete="${s.id}" aria-label="Delete chat">×</span>
    </button>`).join("");
  $("#sessionTabs").innerHTML = `${tabs}<button class="tab add" id="newChatBtn" title="New chat" aria-label="New chat">+</button>`;
}

const SUGGESTIONS = [
  ["✦", "Create a project structure for my game. It's about "],
  ["◎", "Look at my project and suggest what's missing."],
  ["✎", "Write a short pitch and a feature list on the front page."],
];
const TOOL_ICON = { write: "✎", append: "✎", create_folder: "▣", move: "↦", delete: "✕", generate: "✦", pick_winner: "★", file_order: "⇢", request_access: "🔒", import_game: "⇣", copy_in: "⇣" };

// No model ready: say exactly why, with the one button that fixes it.
function brainBanner() {
  const clis = (state.modelInfo?.clis || []).filter((c) => c.installed);
  const waiting = clis.find((c) => state.cliWait?.[c.id]);
  if (waiting) return `<div class="banner">Waiting for you to log in to ${esc(waiting.label)} in the window that opened…</div>`;
  const checking = clis.find((c) => c.status === "unknown");
  if (checking) return `<div class="banner">Checking your ${esc(checking.label)} login…</div>`;
  const out = clis.find((c) => c.status === "login") || clis.find((c) => c.status !== "error") || clis[0];
  if (out) return `<div class="banner">${esc(out.label)} isn't logged in yet, so Glob's chat can't answer. <button class="ghost" data-cli-login="${esc(out.id)}">Log in to ${esc(out.label)}</button></div>`;
  return `<div class="banner">Glob's chat runs on Claude Code or Codex, with your own login. <button class="ghost" data-settings>Set it up</button></div>`;
}

function renderMessages() {
  const box = $("#messages");
  const messages = state.session?.messages || [];
  const noModel = !state.models.some((m) => m.ready);
  const banner = noModel ? brainBanner() : "";
  if (!messages.length && !state.sending) {
    box.innerHTML = `${banner}<div class="chat-empty">
      <img class="hero-glob" src="/ui/glob-forge.svg" alt="">
      <h2>What are we forging today?</h2>
      <p>Tell me about your game. I build and organize the project on the right and make images and 3D for your assets.</p>
      <div class="suggestions">${SUGGESTIONS.map(([icon, s]) => `<button class="suggestion" data-suggest="${esc(s)}"><i>${icon}</i><span>${esc(s.trim())}${s.endsWith(" ") ? "…" : ""}</span></button>`).join("")}</div>
    </div>`;
    return;
  }
  const thinking = state.sending ? `<div class="msg agent thinking"><img class="avatar forge" src="/ui/glob-forge.svg" alt=""><div class="bubble"><div class="who">Glob</div><span class="shimmer">Forging…</span></div></div>` : "";
  // Only messages that are new since the last draw animate in; redraws of the same chat stay still.
  const seen = state.drawn?.session === state.sessionId ? state.drawn.count : messages.length;
  state.drawn = { session: state.sessionId, count: messages.length };
  box.innerHTML = banner + messages.map((m, i) => renderMessage(m).replace('<div class="msg ', `<div class="msg ${i >= seen ? "fresh " : ""}`)).join("") + thinking;
  box.scrollTo({ top: box.scrollHeight, behavior: "instant" }); // the box scrolls smoothly; a redraw must not glide through history
  syncComposer();
}

function renderMessage(m) {
  if (m.role === "user") {
    const imgs = (m.attachments || []).map((a) => `<img class="thumb" src="${esc(fileUrl(a.path))}" alt="">`).join("");
    return `<div class="msg user"><div class="bubble">${esc(m.text)}${imgs}</div></div>`;
  }
  if (m.role === "note") return `<div class="msg note">${esc(m.text)}</div>`;
  const actions = (m.actions || []).filter((a) => !["list", "read", "legacy"].includes(a.tool) || a.ok === false);
  const failed = actions.filter((a) => a.ok === false).length;
  const rows = actions.map((a) => {
    const openable = a.ok !== false && a.path && a.tool !== "delete";
    return `<li class="${a.ok === false ? "bad" : ""}${openable ? " link" : ""}"${openable ? ` data-open="${esc(a.path)}"` : ""}><i>${a.ok === false ? "!" : TOOL_ICON[a.tool] || "•"}</i><span>${esc(a.summary)}</span></li>`;
  }).join("");
  const card = actions.length ? `<details class="changes"${actions.length <= 3 ? " open" : ""}><summary><span class="spark">✦</span>${actions.length} change${actions.length === 1 ? "" : "s"}${failed ? ` <em>${failed} failed</em>` : ""}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></summary><ul>${rows}</ul></details>` : "";
  const last = m === state.session?.messages?.at(-1);
  const asks = (m.actions || []).filter((a) => a.access_request).map((a) => accessCard(a.access_request, last)).join("");
  return `<div class="msg agent${m.error ? " error" : ""}"><img class="avatar" src="/ui/glob-face.svg" alt=""><div class="bubble"><div class="who">Glob${m.model ? ` <small>${esc(m.model)}</small>` : ""}</div><div class="md">${renderMarkdown(m.text)}</div>${asks}${card}</div></div>`;
}

function syncComposer() {
  renderModelPill();
  const used = state.session?.context_tokens || 0;
  const limit = state.agentCfg?.agent_compact_at_tokens || 48000;
  $("#contextMeter").innerHTML = used
    ? `<span class="meter"><i style="width:${Math.min(100, (used / limit) * 100)}%"></i></span>Context ${(used / 1000).toFixed(1)}k / ${limit / 1000}k · <button type="button" class="link-btn" data-compact-now>Compact now</button>`
    : "Enter to send · Shift+Enter for a new line";
}

function renderAttachments() {
  $("#attachments").innerHTML = state.attachments.map((a, i) => `<figure><img src="${a.data_url}" alt="${esc(a.name)}"><button type="button" data-unattach="${i}" aria-label="Remove">×</button></figure>`).join("");
}

function addFiles(files) {
  const room = 4 - state.attachments.length;
  if ([...files].length > room) toast(room ? `Up to 4 images per message; added ${room}.` : "Up to 4 images per message.", true);
  for (const file of [...files].slice(0, Math.max(0, room))) {
    if (!file.type.startsWith("image/")) continue;
    const reader = new FileReader();
    reader.onload = () => { state.attachments.push({ name: file.name, data_url: reader.result }); renderAttachments(); };
    reader.readAsDataURL(file);
  }
}

async function send() {
  const input = $("#input");
  const text = input.value.trim();
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
    await loadModels(); // a failed call marks its model as not ready: the pill and the banner say so
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

function renderModelPill() {
  const m = currentModel();
  const ready = state.models.some((x) => x.ready);
  $("#statusDot").className = `dot ${ready ? "live" : "off"}`;
  $("#modelPillLabel").innerHTML = m
    ? `${esc(m.label)}${m.efforts?.length ? ` <em>· ${EFFORT_LABEL[state.effort] || "Medium"}</em>` : ""}`
    : ready ? "Pick a model" : "Connect a model";
}

// Coding agents running on the user's own login, with their logos (Glob chat model menu, setup, settings).
const AGENT_LOGO = { claude: "/ui/logos/claude.svg", codex: "/ui/logos/codex.svg", gemini: "/ui/logos/gemini.svg", opencode: "/ui/logos/opencode.svg" };
const MODEL_TABS = [["fav", "Favorites"], ["claude", "Claude Code"], ["codex", "Codex"], ["gemini", "Gemini"], ["opencode", "OpenCode"], ["custom", "API & custom"]];

const modelTab = (m) => (m.provider === "cli" ? m.id.slice(4).split("/")[0] : "custom");

function favoriteModels() {
  try { return JSON.parse(storeGet("glob.favoriteModels") || "[]"); } catch { return []; }
}

function toggleFavorite(id) {
  const favs = favoriteModels();
  storeSet("glob.favoriteModels", JSON.stringify(favs.includes(id) ? favs.filter((f) => f !== id) : [...favs, id]));
  renderModelPop();
}

function renderModelPop() {
  const ready = state.models.filter((x) => x.ready);
  const favs = favoriteModels().filter((id) => ready.some((m) => m.id === id));
  const tabs = MODEL_TABS.filter(([id]) => (id === "fav" ? favs.length : ready.some((m) => modelTab(m) === id)));
  const m = currentModel();
  if (!tabs.some(([id]) => id === state.modelTab)) state.modelTab = favs.length ? "fav" : (m ? modelTab(m) : tabs[0]?.[0]);
  const shown = state.modelTab === "fav" ? ready.filter((x) => favs.includes(x.id)) : ready.filter((x) => modelTab(x) === state.modelTab);
  const groups = {};
  for (const x of shown) (groups[state.modelTab === "custom" ? x.group || x.provider : ""] ||= []).push(x);
  const row = (x) => `<button type="button" class="pop-model${x.id === state.model ? " on" : ""}" data-pick-model="${esc(x.id)}">
    ${x.provider === "cli" && state.modelTab === "fav" ? `<img src="${AGENT_LOGO[modelTab(x)]}" alt="">` : ""}<span>${esc(x.label)}</span>
    <i class="pop-star${favs.includes(x.id) ? " on" : ""}" data-fav-model="${esc(x.id)}" title="${favs.includes(x.id) ? "Remove from favorites" : "Add to favorites"}">★</i>${x.id === state.model ? "<b>✓</b>" : ""}</button>`;
  $("#modelPop").innerHTML = `
    ${tabs.length > 1 ? `<div class="pop-tabs">${tabs.map(([id, label]) => `<button type="button" class="${id === state.modelTab ? "on" : ""}" data-model-tab="${id}" title="${label}">${id === "fav" ? "★" : AGENT_LOGO[id] ? `<img src="${AGENT_LOGO[id]}" alt="">` : "API"}</button>`).join("")}</div>
      <div class="pop-tab-name">${esc(MODEL_TABS.find(([id]) => id === state.modelTab)?.[1] || "")}</div>` : ""}
    <div class="pop-list">${shown.length ? Object.entries(groups).map(([group, items]) => `${group ? `<div class="pop-group">${esc(group)}</div>` : ""}${items.map(row).join("")}`).join("")
      : `<p class="pop-empty">No model connected yet.</p>`}</div>
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
  if (!$("#settingsView").hidden && state.settings?.tab === "models") renderSettings();
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
  state.settings = { tab: tab || state.settings?.tab || "accounts", adding: null };
  showScene($("#settingsView"));
  await renderSettings();
}

function closeSettings() {
  hideScene($("#settingsView"));
  // "Connect a provider…" in first-run setup comes back to the setup afterwards.
  if (state.setupReturn && state.setup) { state.setupReturn = false; showScene($("#setup")); renderSetup(); }
}

// Generation services the user signs in to (hosted MCP + OAuth), shown in setup and Settings → Generators.
const SERVICE_LOGO = { elevenlabs: "/ui/logos/elevenlabs.svg", meshy: "/ui/logos/meshy.png", fal: "/ui/logos/fal.png", higgsfield: "/ui/logos/higgsfield.png" };

async function loadServices() {
  try { state.services = (await api("/api/services")).services; } catch { state.services = state.services || []; }
  return state.services;
}

function serviceCardsHTML() {
  return `<div class="agent-cards" id="serviceCards">${(state.services || []).map((sv) => {
    const waiting = state.serviceWaiting === sv.id;
    const cls = sv.connected ? "ok" : waiting ? "login" : sv.error ? "error" : "idle";
    const note = sv.connected ? `Connected · ${sv.use === "generators" ? `powers the ${sv.label} models` : "the Glob chat can use it"}` : waiting ? "Sign in in your browser…" : sv.error || sv.makes;
    return `<div class="agent-card ${cls}" role="button" tabindex="0" ${sv.connected ? "" : `data-service-connect="${sv.id}"`} title="${esc(sv.makes)}">
      <span class="agent-logo"><img src="${SERVICE_LOGO[sv.id]}" alt=""></span>
      <span class="agent-text"><strong>${esc(sv.label)}</strong><small>${esc(note)}</small></span>
      <span class="agent-mark">${sv.connected ? `<button type="button" class="link-btn" data-service-disconnect="${sv.id}" title="Sign out">✓</button>` : waiting ? `<i class="agent-spin"></i>` : "→"}</span></div>`;
  }).join("")}</div>`;
}

function renderServiceCards() {
  const box = $("#serviceCards");
  if (box) box.outerHTML = serviceCardsHTML();
  if ($("#acctList") && !$("#settingsView").hidden) renderSettings();
}

// Opens the service's sign-in page; the browser comes back to Glob, which finishes the sign-in.
async function connectService(id) {
  // Open the tab during the click, so the browser doesn't block it while Glob prepares the sign-in.
  const tab = window.open("about:blank", "_blank");
  if (tab) tab.opener = null;
  state.serviceWaiting = id;
  renderServiceCards();
  let url;
  try { url = (await api(`/api/services/${id}/connect`, { method: "POST" })).authorize_url; } catch (err) {
    // Say why in the tab too, in case the browser won't let Glob close it.
    try { tab.document.body.innerHTML = `<p style="font:16px system-ui;padding:40px">Sign-in could not start: ${esc(err.message)}</p>`; } catch { /* already gone */ }
    tab?.close();
    state.serviceWaiting = null;
    renderServiceCards();
    return toast(err.message, true);
  }
  if (tab) tab.location.href = url;
  else window.open(url, "_blank", "noopener");
  clearTimeout(state.serviceTimer);
  let tries = 0;
  const poll = async () => {
    await loadServices();
    const sv = state.services.find((x) => x.id === id);
    if (sv?.connected) return serviceConnected(id);
    if (++tries < 90 && state.serviceWaiting === id) state.serviceTimer = setTimeout(poll, 2000);
    else { state.serviceWaiting = null; renderServiceCards(); }
  };
  state.serviceTimer = setTimeout(poll, 2000);
}

function serviceConnected(id) {
  if (state.serviceWaiting !== id && !$("#serviceCards") && !$("#acctList")) return;
  clearTimeout(state.serviceTimer);
  state.serviceWaiting = null;
  renderServiceCards();
  const sv = (state.services || []).find((x) => x.id === id);
  playFistbump(SERVICE_LOGO[id], sv?.label || "The service", $("#serviceFistbump"));
}

const KEY_PROVIDERS = [
  { key: "gemini", name: "Google Gemini", use: "2D images (Nano Banana)", url: "https://aistudio.google.com/apikey", hint: "AIza…" },
  { key: "meshy", name: "Meshy", use: "3D models, rigs, animations", url: "https://www.meshy.ai/settings/api", hint: "msy_…" },
  { key: "tripo", name: "Tripo", use: "3D models (second 3D engine)", url: "https://platform.tripo3d.ai/api-keys", hint: "tsk_…" },
  { key: "elevenlabs", name: "ElevenLabs", use: "Sound effects, voices, more image models", url: "https://elevenlabs.io/app/settings/api-keys", hint: "sk_…" },
  { key: "stability", name: "Stability AI", use: "Music and ambient loops (Stable Audio)", url: "https://platform.stability.ai/account/keys", hint: "sk-…" },
];
const PRESET_ICON = { anthropic: "✳", openai: "◯", openrouter: "⇄", gemini: "✦", ollama: "◉", lmstudio: "▣", custom: "⚙" };

// What a coding agent's card says. Only "ok" (a real call worked) means Glob's chat can use it.
const CLI_STATUS_TEXT = { ok: "✓ Logged in", login: "Not logged in · Log in", error: "Didn't answer · Try again", unknown: "Checking the login…", missing: "Not installed · Install", waiting: "Waiting for you to log in in the window that opened…" };
const cliStatus = (c) => (state.cliWait?.[c.id] ? "waiting" : c.status || (c.installed ? "unknown" : "missing"));

// Log in (or install) in a real terminal window, then check every few seconds until it works.
async function cliLogin(id) {
  const label = state.modelInfo?.clis?.find((c) => c.id === id)?.label || "The coding agent";
  try { await api(`/api/agent/cli/${id}/login`, { method: "POST" }); } catch (err) { return toast(err.message, true); }
  state.cliWait = { ...(state.cliWait || {}), [id]: true };
  refreshModelViews();
  for (let i = 0; i < 60 && state.cliWait?.[id]; i++) {
    await new Promise((r) => setTimeout(r, 4000));
    let r;
    try { r = await api(`/api/agent/cli/${id}/check`, { method: "POST" }); } catch { continue; }
    if (!r.ok) continue;
    delete state.cliWait[id];
    await loadModels();
    if (!state.models.some((m) => m.id === state.model && m.ready)) {
      const pick = state.models.find((m) => m.id === `cli:${id}/`);
      if (pick) await pickModel(pick.id);
    }
    toast(`✓ ${label} is logged in. Glob's chat is ready.`);
    return refreshModelViews();
  }
  if (state.cliWait?.[id]) toast(`${label} still isn't logged in. Finish in the window that opened, then press Log in again.`, true);
  delete state.cliWait?.[id];
  refreshModelViews();
}

function refreshModelViews() {
  renderModelPill();
  renderMessages();
  if (!$("#settingsView").hidden && state.settings?.tab === "models") renderSettings();
}

// Settings: five plain tabs. Each one draws at once from what Glob already knows and fills in as answers arrive.
const SETTINGS_TABS = [["accounts", "Accounts"], ["images", "New art"], ["models", "Glob chat"], ["connect", "Your game"], ["about", "Files & app"]];
const SETTINGS_ALIAS = { agent: "models", storage: "about" };

// Every provider in one list: sign in where the provider allows it, otherwise (or as well) paste a key.
const PROVIDERS = [
  { id: "elevenlabs", name: "ElevenLabs", makes: "Images, sound effects, voices and music", key: "elevenlabs", service: true },
  { id: "meshy", name: "Meshy", makes: "3D models, rigs, animations and images", key: "meshy", service: true },
  { id: "gemini", name: "Google Gemini", makes: "Images (Nano Banana)", key: "gemini" },
  { id: "fal", name: "fal", makes: "Hundreds of image, video and 3D models", service: true },
  { id: "higgsfield", name: "Higgsfield", makes: "Images and cinematic video", service: true },
  { id: "tripo", name: "Tripo", makes: "3D models", key: "tripo" },
  { id: "stability", name: "Stability AI", makes: "Music and ambient loops", key: "stability" },
];
const PROVIDER_LOGO = { ...SERVICE_LOGO, gemini: "/ui/logos/gemini.svg" };

// What Glob can make right now, and with what: the four lights at the top of Accounts.
function capabilities(cfg) {
  const sv = Object.fromEntries((state.services || []).map((s) => [s.id, s.connected]));
  const sm = state.serviceModels || { image: [], "3d": [] };
  const keyImages = (state.studio?.models || []).some((m) => m.available);
  const chat = state.models.find((m) => m.id === state.model && m.ready) || state.models.find((m) => m.ready);
  return [
    { id: "images", label: "Images", icon: "character", ok: keyImages || sm.image.length > 0, via: imageVia() },
    { id: "3d", label: "3D models", icon: "cube", ok: !!(cfg.meshy_configured || cfg.tripo_configured || cfg.meshy_signed_in || sm["3d"].length), via: cfg.meshy_configured || cfg.meshy_signed_in ? "Meshy" : cfg.tripo_configured ? "Tripo" : sm["3d"][0]?.label.split(" · ")[0] },
    { id: "sounds", label: "Sounds", icon: "sound", ok: !!(cfg.elevenlabs_configured || sv.elevenlabs || cfg.stability_configured), via: cfg.elevenlabs_configured || sv.elevenlabs ? "ElevenLabs" : "Stability AI" },
    { id: "chat", label: "Glob chat", icon: "send", ok: !!chat, via: chat?.label, tab: "models" },
  ];
}

// The service the default image model is paid through, as the Images light says it.
function imageVia() {
  const id = String(state.studio?.selected || "");
  const svc = (state.serviceModels?.image || []).find((m) => m.id === id);
  if (svc || id.startsWith("svc:")) return `${svc?.label.split(" · ")[0] || humanize(id.slice(4).split("/")[0])} credits`;
  const own = (state.studio?.models || []).find((m) => m.id === id);
  return own ? `your ${own.label.split(" · ")[0]} key` : "ready";
}

function accountCard(p, cfg) {
  const sv = (state.services || []).find((s) => s.id === p.id);
  const signedIn = !!sv?.connected, keyed = !!(p.key && cfg[`${p.key}_configured`]);
  const waiting = state.serviceWaiting === p.id;
  const keyInfo = KEY_PROVIDERS.find((k) => k.key === p.key);
  const status = signedIn ? "Signed in" : keyed ? "Key saved" : waiting ? "Finish signing in in your browser…" : "Not connected";
  const logo = PROVIDER_LOGO[p.id] ? `<img src="${PROVIDER_LOGO[p.id]}" alt="">` : `<b>${esc(p.name[0])}</b>`;
  const keyForm = p.key ? `<details class="acct-key"><summary>${keyed ? "Replace the key" : p.service ? "Or paste an API key" : "Paste an API key"}</summary>
      <form class="acct-key-form"><input name="${esc(p.key)}_api_key" type="password" autocomplete="off" placeholder="${esc(keyInfo?.hint || "Paste the key")}" aria-label="${esc(p.name)} API key">
      <button class="mini">Save</button><a href="${esc(keyInfo?.url || "#")}" target="_blank" rel="noopener">Get a key ↗</a></form></details>` : "";
  return `<div class="acct${signedIn || keyed ? " on" : ""}${waiting ? " wait" : ""}" data-acct="${p.id}">
    <span class="acct-logo">${logo}</span>
    <div class="acct-text"><strong>${esc(p.name)}</strong><small>${esc(p.makes)}</small>
      <span class="acct-status">${signedIn || keyed ? "✓ " : waiting ? `<i class="agent-spin"></i>` : ""}${esc(status)}</span>${keyForm}</div>
    <div class="acct-act">${signedIn ? `<button class="ghost" data-service-disconnect="${p.id}">Sign out</button>`
      : p.service ? `<button class="primary" data-service-connect="${p.id}">${waiting ? "Waiting…" : "Sign in"}</button>` : ""}</div></div>`;
}

function accountsBody(cfg, budget) {
  const caps = capabilities(cfg);
  const missing = caps.filter((c) => !c.ok);
  return `<h1>Accounts</h1>
    <p class="st-lede">Glob makes art with services you sign in to. Glob is free; you pay each service directly for what you make, and everything stays on this computer.</p>
    <div class="caps">${caps.map((c) => `<button class="cap ${c.ok ? "ok" : "no"}" ${c.tab ? `data-settings-tab="${c.tab}"` : `data-cap-jump="${c.id}"`}>
      <span class="cap-icon">${icon(c.icon)}</span><span><strong>${c.label}</strong><small>${c.ok ? `✓ ${esc(c.via || "ready")}` : "Not set up yet"}</small></span></button>`).join("")}</div>
    ${missing.length ? `<p class="st-hint">${missing.length === caps.length ? "Start with <b>ElevenLabs</b>: one sign-in gives you images and sounds." : `To make ${missing.map((c) => c.label.toLowerCase()).join(" and ")}, connect a service below that says so.`}</p>` : ""}
    <div class="acct-list" id="acctList">${PROVIDERS.map((p) => accountCard(p, cfg)).join("")}</div>
    <div class="fistbump" id="serviceFistbump" hidden></div>
    ${budgetCard(budget)}`;
}

// Who bills an image model: a signed-in service's credits, or one of your API keys.
const m0Via = (m, who) => (String(m.id).startsWith("svc:") ? `${who} credits` : `your ${who} key`);

// New art: which service makes images and 3D models, and the shape and background of each kind.
function defaultsHTML(cfg, studio) {
  const sm = state.serviceModels || { image: [], "3d": [] };
  const group = (items) => Object.entries(Object.groupBy(items, (m) => m.label.split(" · ")[0]));
  const opt = (id, label, selected) => `<option value="${esc(id)}" ${id === selected ? "selected" : ""}>${esc(label)}</option>`;
  const images = [...(studio.models || []).filter((m) => m.available), ...sm.image];
  const imageSel = images.length ? `<select name="default_image_model" aria-label="Images are made with">${images.some((m) => m.id === studio.selected) ? "" : opt(studio.selected, `${humanize(String(studio.selected).split("/").pop())}${sm.loading ? " · checking…" : ""}`, studio.selected)}
      ${group(images).map(([who, items]) => `<optgroup label="Paid with ${esc(m0Via(items[0], who))}">${items.map((m) => opt(m.id, `${m.label.split(" · ").slice(1).join(" · ") || m.label} · ${m.price ? `$${m.price.toFixed(3)}, ` : ""}${m0Via(m, who)}`, studio.selected)).join("")}</optgroup>`).join("")}</select>`
    : `<button type="button" class="ghost" data-settings-tab="accounts">Connect an account first →</button>`;
  const current3d = cfg.default_3d_service_model || `keys:${cfg.default_3d_provider || "meshy"}`;
  const own3d = [["meshy", "Meshy (rigs and animations too)"], ["tripo", "Tripo"]].filter(([id]) => cfg[`${id}_configured`] || cfg[`${id}_signed_in`]).map(([id, label]) => ({ id: `keys:${id}`, label: `Your account · ${label}` }));
  const meshes = [...own3d, ...sm["3d"]];
  const meshSel = meshes.length ? `<select name="default_3d" aria-label="3D models are made with">${group(meshes).map(([who, items]) => `<optgroup label="${esc(who)}">${items.map((m) => opt(m.id, m.label.split(" · ").slice(1).join(" · "), current3d)).join("")}</optgroup>`).join("")}</select>`
    : `<button type="button" class="ghost" data-settings-tab="accounts">Connect Meshy first →</button>`;
  const dim = state.overview?.dimension || "3d";
  return `
    ${state.project ? `<div class="set-row"><div><strong>${esc(humanize(state.project))} is a</strong><small>2D hides 3D models, rigs and animations from this project. You can switch any time.</small></div>
      <div class="seg" role="radiogroup">${[["2d", "2D game"], ["3d", "3D game"]].map(([v, l]) => `<label><input type="radio" name="project_dimension" value="${v}" ${(dim === "hybrid" ? "3d" : dim) === v ? "checked" : ""}><span>${l}</span></label>`).join("")}</div></div>` : ""}
    <div class="set-row"><div><strong>Images are made with</strong><small>Every Generate window starts here and can switch for one run.</small></div>${imageSel}</div>
    <div class="set-row"><div><strong>3D models are made with</strong><small>Turns an asset's picked image into a 3D model.</small></div>${meshSel}</div>
    <div class="st-label">Each kind of asset</div>
    <div class="kind-table">${Object.entries(studio.kind_defaults || {}).map(([kind, k]) => `<div class="kind-row">
      <span class="kind-name">${kindArt(kind, "")}<strong>${esc(KIND_PLURAL[kind] || humanize(kind))}</strong></span>
      <select data-kind-aspect="${esc(kind)}" aria-label="Shape for ${esc(kind)}">${aspectOptions(k.aspect)}</select>
      <select data-kind-bg="${esc(kind)}" aria-label="Background for ${esc(kind)}"><option value="cutout" ${k.background === "cutout" ? "selected" : ""}>Background removed</option><option value="scene" ${k.background === "scene" ? "selected" : ""}>Full scene</option></select></div>`).join("")}</div>
    <p class="st-note"><b>Background removed</b> suits characters, props and anything that becomes 3D or a sprite. <b>Full scene</b> keeps the background, for places and key art. Changes save at once.</p>`;
}

function readDefaults(form) {
  const mesh = form.default_3d?.value || "";
  const out = { kind_defaults: Object.fromEntries([...form.querySelectorAll("[data-kind-aspect]")].map((el) => [el.dataset.kindAspect, { aspect: el.value, background: form.querySelector(`[data-kind-bg="${el.dataset.kindAspect}"]`)?.value || "cutout" }])) };
  if (form.default_image_model) Object.assign(out, { default_image_model: form.default_image_model.value, user_locked_image_model: true });
  if (mesh.startsWith("keys:")) Object.assign(out, { default_3d_provider: mesh.slice(5), default_3d_service_model: "" });
  else if (mesh) out.default_3d_service_model = mesh;
  return out;
}

async function saveDefaults(form, changed) {
  if (changed?.name === "project_dimension") {
    try {
      await api("/api/bible", { method: "PUT", body: { project_dimension: changed.value } });
      if (state.overview) state.overview.dimension = changed.value;
      return toast(changed.value === "2d" ? "2D game: 3D tools are hidden" : "3D game: 3D tools are back");
    } catch (err) { return toast(err.message, true); }
  }
  try {
    await api("/api/config", { method: "PUT", body: readDefaults(form) });
    state.studio = null;
    toast("Saved");
  } catch (err) { toast(err.message, true); }
}

function chatBody(info, cfg) {
  const ready = state.models.filter((m) => m.ready);
  const current = ready.find((m) => m.id === state.model);
  const groups = Object.groupBy(ready, (m) => m.group || m.provider);
  const opt = (values, cur, label = (v) => v) => values.map((v) => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${label(v)}</option>`).join("");
  const k = (v) => `${v / 1000}k tokens`;
  return `<h1>Glob chat</h1>
    <p class="st-lede">Glob's chat plans your game's art with you and makes it. It runs on an AI you already have: log in to a coding agent once, no API key needed.</p>
    <div class="chat-now ${current ? "ok" : "no"}">${current ? `<span class="dot live"></span><span>The chat uses <b>${esc(current.label)}</b></span>` : `<span class="dot"></span><span>The chat has no AI yet. Log in to one below.</span>`}</div>
    <div class="st-label">Your coding agents</div>
    <div class="preset-grid">${(info.clis || []).map((c) => `<button class="preset cli-${cliStatus(c)}" data-cli-login="${esc(c.id)}" title="${esc(c.installed ? c.login : c.install)}"><i><img class="preset-logo" src="${AGENT_LOGO[c.id]}" alt=""></i><strong>${esc(c.label)}</strong><small>${CLI_STATUS_TEXT[cliStatus(c)]}</small>${c.installed && cliStatus(c) !== "waiting" ? `<span class="cli-logout" role="button" tabindex="0" data-cli-logout="${esc(c.id)}" title="Sign ${esc(c.label)} out, then log in again">Log out</span>` : ""}</button>`).join("")}</div>
    ${ready.length ? `<div class="st-label">Ready to use · ${ready.length}</div>
    ${Object.entries(groups).map(([name, items]) => `<div class="conn-group"><div class="conn-head"><strong>${esc(name)}</strong>${items[0].endpoint ? `<button class="link-btn danger-text" data-remove-endpoint="${esc(items[0].endpoint)}">Remove</button>` : ""}</div>
      ${items.map((m) => `<div class="conn-row"><span class="dot live"></span><span>${esc(m.label)}</span><span class="grow"></span>${m.id === state.model ? `<em class="in-use">In use</em>` : `<button class="mini" data-pick-model="${esc(m.id)}" data-settings-refresh>Use</button>`}<span class="test-result" id="test-${esc(m.id).replace(/[^a-z0-9]/gi, "_")}"></span><button class="mini ghost" data-test-model="${esc(m.id)}">Test</button></div>`).join("")}</div>`).join("")}` : ""}
    <details class="st-more"${state.settings.adding ? " open" : ""}><summary>Use an API key or a local model instead</summary>
      ${state.settings.adding ? addModelCard(info) : `<div class="preset-grid">
        ${Object.entries(info.presets).map(([id, p]) => `<button class="preset" data-add-preset="${id}"><i>${PRESET_ICON[id] || "•"}</i><strong>${esc(p.label)}</strong><small>${p.needs_key ? "API key" : id === "custom" ? "Any OpenAI-compatible URL" : "No key needed"}</small></button>`).join("")}</div>`}
    </details>
    <details class="st-more"><summary>Advanced</summary>
      <form id="agentForm" class="agent-form">
        <div class="set-row"><div><strong>Show the chat</strong><small>Automatic hides it while your coding agent works on the project.</small></div>
          <select name="chat_mode">${opt(["auto", "on", "off"], cfg.chat_mode || "auto", (v) => ({ auto: "Automatic", on: "Always", off: "Never" })[v])}</select></div>
        <div class="set-row"><div><strong>How hard it thinks</strong><small>For new chats. Each chat can change it in the model menu.</small></div>
          <select name="agent_reasoning_effort">${opt(["low", "medium", "high", "max"], cfg.agent_reasoning_effort, (v) => EFFORT_LABEL[v])}</select></div>
        <div class="set-row"><div><strong>Shorten long chats</strong><small>Older messages fold into a summary, so long chats stay quick and cheap.</small></div>
          <label class="switch"><input type="checkbox" name="agent_auto_compact" ${cfg.agent_auto_compact ? "checked" : ""}><span></span></label></div>
        <div class="set-row"><div><strong>Shorten at</strong><small>The size of the chat sent with each message.</small></div>
          <select name="agent_compact_at_tokens">${opt([16000, 32000, 48000, 64000, 100000, 128000, 200000], cfg.agent_compact_at_tokens, k)}</select></div>
        <div class="set-row"><div><strong>Keep word for word</strong><small>The newest messages never fold.</small></div>
          <select name="agent_keep_recent_messages">${opt([2, 4, 6, 8, 12, 16], cfg.agent_keep_recent_messages)}</select></div>
        <div class="set-row"><div><strong>Steps per message</strong><small>How many rounds the chat may take for one message.</small></div>
          <select name="agent_max_tool_rounds">${opt([4, 8, 12, 16, 24], cfg.agent_max_tool_rounds)}</select></div>
      </form>
    </details>`;
}

async function renderSettings() {
  const st = state.settings;
  st.tab = SETTINGS_ALIAS[st.tab] || st.tab;
  if (!SETTINGS_TABS.some(([id]) => id === st.tab)) st.tab = "accounts";
  const tab = st.tab;
  const nav = SETTINGS_TABS.map(([id, label]) => `<button class="${tab === id ? "on" : ""}" data-settings-tab="${id}">${label}</button>`).join("");
  const draw = (body) => {
    if (state.settings !== st || st.tab !== tab || $("#settingsView").hidden) return;
    const same = $("#settingsView .st-body")?.dataset.tab === tab;
    const scroll = same ? $("#settingsView .st-body").scrollTop : 0;
    const open = same ? [...document.querySelectorAll("#settingsView details[open] > summary")].map((s) => s.textContent) : [];
    $("#settingsView").innerHTML = `
      <header class="pv-top"><div class="glob-brand static"><img class="glob-mark" src="/ui/glob.svg" alt=""><span class="glob-word">Glob<small>settings</small></span></div>
        <button class="icon-btn" type="button" data-close-settings title="Close" aria-label="Close">✕</button></header>
      <div class="st-main"><nav class="st-nav">${nav}</nav><div class="st-body" data-tab="${tab}">${body}</div></div>`;
    document.querySelectorAll("#settingsView details > summary").forEach((s) => { if (open.includes(s.textContent)) s.parentElement.open = true; });
    $("#settingsView .st-body").scrollTop = scroll;
  };
  if ($("#settingsView .st-nav")) $("#settingsView .st-nav").innerHTML = nav; // the tab lights up at once
  // Draw what is known now; answers that take a moment (signed-in services' models) redraw when they come.
  if (tab === "accounts" || tab === "images") {
    const [cfg, budget] = await Promise.all([api("/api/config"), tab === "accounts" ? api("/api/budget").catch(() => null) : null, studioOptions(), state.services ? null : loadServices(), state.models.length ? null : loadModels()]);
    st.cfg = cfg;
    const body = () => (tab === "accounts" ? accountsBody(cfg, budget)
      : `<h1>New art</h1><p class="st-lede">How Glob makes new art: which service, and what each kind of asset looks like. One asset can still change this in its Generate window.</p><form id="defaultsForm" class="gen-defaults">${defaultsHTML(cfg, state.studio)}</form>`);
    draw(body());
    if (state.serviceModels?.loading) state.serviceModelsReady.then(() => draw(body()));
  } else if (tab === "models") {
    const [cfg] = await Promise.all([api("/api/config"), state.modelInfo ? null : loadModels()]);
    draw(chatBody(state.modelInfo || { presets: {}, clis: [] }, cfg));
    loadModels().then(() => draw(chatBody(state.modelInfo || { presets: {}, clis: [] }, cfg)));
  } else if (tab === "connect") {
    draw(await connectSettings());
  } else {
    draw(await filesSettings());
  }
}

// Settings save as they change: no Save button to forget.
$("#settingsView").addEventListener("change", async (event) => {
  const form = event.target.form;
  if (form?.id === "defaultsForm") return saveDefaults(form, event.target);
  if (form?.id !== "agentForm") return;
  const updates = {
    agent_reasoning_effort: form.agent_reasoning_effort.value,
    agent_auto_compact: form.agent_auto_compact.checked,
    agent_compact_at_tokens: Number(form.agent_compact_at_tokens.value),
    agent_keep_recent_messages: Number(form.agent_keep_recent_messages.value),
    agent_max_tool_rounds: Number(form.agent_max_tool_rounds.value),
    chat_mode: form.chat_mode.value,
  };
  try { await api("/api/config", { method: "PUT", body: updates }); state.agentCfg = { ...state.agentCfg, ...updates }; toast("Saved"); syncComposer(); applyChatMode(); } catch (err) { toast(err.message, true); }
});

$("#settingsView").addEventListener("submit", async (event) => {
  const form = event.target.closest(".acct-key-form");
  if (!form) return;
  event.preventDefault();
  const input = form.querySelector("input");
  if (!input.value.trim()) return input.focus();
  try {
    await api("/api/config", { method: "PUT", body: { [input.name]: input.value.trim() } });
    state.studio = null;
    loadServiceModels();
    toast("✓ Key saved");
    await renderSettings();
  } catch (err) { toast(err.message, true); }
});

// A capability that isn't set up points at the services that give it.
document.addEventListener("click", (event) => {
  const jump = event.target.closest("[data-cap-jump]");
  if (!jump) return;
  const want = { images: /image/i, "3d": /3d/i, sounds: /sound|music/i }[jump.dataset.capJump];
  const cards = [...document.querySelectorAll("#acctList .acct")].filter((el) => want?.test(el.querySelector("small")?.textContent || ""));
  cards[0]?.scrollIntoView({ behavior: "smooth", block: "center" });
  for (const card of cards) { card.classList.add("flash"); setTimeout(() => card.classList.remove("flash"), 1600); }
});

async function filesSettings() {
  const [a, info, grants, health] = await Promise.all([api("/api/about"), api("/api/storage"), loadGrants(), api("/api/health").catch(() => ({})), state.update ? null : checkForUpdate()]);
  const shortcut = a.shortcuts.length
    ? `<p class="st-note">Open Glob any time with the <strong>Glob</strong> shortcut on your desktop or in the Start menu.${a.background ? " Closing the window leaves Glob running; its icon in the taskbar tray opens or quits it." : ""}</p>`
    : `<p class="st-note">There's no Glob shortcut yet. Add one to open Glob without a terminal or an agent.</p>`;
  return `
    <h1>Files & app</h1><p class="st-lede">Glob ${esc(a.version)} runs on this computer. Here is where your work is saved and how to open Glob again.</p>
    <div class="st-label">Open Glob</div>
    ${shortcut}
    <div class="row wrap"><button class="ghost" data-st-shortcut>${a.shortcuts.length ? "Make the shortcut again" : "Add a Glob shortcut"}</button>${health.background ? `<button class="ghost danger-text" data-st-quit>Quit Glob</button>` : ""}</div>
    <div class="st-label">Your projects</div>
    <div class="path-row"><code>${esc(info.projects_dir)}</code><button class="mini" data-st-open="projects">Open</button><button class="mini" data-st-move>Move…</button></div>
    ${state.project ? `<div class="st-label">This project · ${esc(humanize(state.project))}</div>
    <div class="path-row"><code>${esc(info.workspace_dir)}</code><button class="mini" data-st-open="workspace">Open</button></div>` : ""}
    <div class="row wrap">${state.project ? `<button class="ghost" data-st-export="${esc(state.project)}">Save as .zip…</button>` : ""}<button class="ghost" data-st-import-zip>Open a .zip…</button><button class="ghost" data-imp-open>Import a game…</button></div>
    ${updateSettings()}
    <details class="st-more"><summary>Advanced</summary>
      <div class="st-label">Folders Glob may use</div>
      <div class="grant-list">${grantRows(grants)}</div>
      <div class="row"><button class="ghost" data-grant-add>+ Add a folder</button></div>
      <p class="st-note">Glob's chat can read these folders, and write in them when allowed. API keys and .env files are always off-limits. When Glob needs another folder, it asks in the chat.</p>
      <div class="st-label">Where Glob is installed · ${esc(INSTALL_KIND[a.install_kind] || a.install_kind)}</div>
      <div class="path-row"><code>${esc(a.install_dir)}</code><button class="mini" data-st-open="install">Open</button></div>
      <div class="st-label">Settings and keys</div>
      <div class="path-row"><code>${esc(a.config_dir)}</code><button class="mini" data-st-open="config">Open</button></div>
      <p class="st-note">If something goes wrong, the log is <code>${esc(a.log)}</code>.</p>
      <div class="row"><button class="ghost" data-setup-open>Run the welcome setup again</button></div>
    </details>`;
}

// ---------- coding agent: connect the game folder ----------

const TRY_PROMPTS = [
  "Copy the Glob art for the main character into the game and use it instead of the placeholder.",
  "Check glob/ASSETS.md and tell me which art the game still needs.",
];

async function loadConnect() {
  try { state.connect = await api("/api/connect"); } catch { state.connect = null; }
  applyChatMode();
  return state.connect;
}

async function connectSettings() {
  const cn = await loadConnect();
  const draft = (state.cnDraft ||= { folder: "" });
  const how = `<div class="cn-how">
      <div><b>1</b><span>You make the art here in Glob, with Glob's chat or by hand.</span></div>
      <div><b>2</b><span>Glob keeps <code>glob/ASSETS.md</code> in your game: every asset and its finished files.</span></div>
      <div><b>3</b><span>Your coding agent reads it and copies the art it needs into the game. Nothing to install or restart.</span></div>
    </div>`;
  if (!cn?.connected) {
    return `<h1>Your game</h1>
      <p class="st-lede">Link ${esc(humanize(state.project))} to your game folder. Glob writes a short note into <code>AGENTS.md</code> and <code>CLAUDE.md</code> so any coding agent (Claude Code, Codex, Cursor, Copilot…) knows where the finished art is.</p>
      ${how}
      <div class="st-label">Game folder</div>
      <div class="cn-pick"><input id="cnFolder" placeholder="/path/to/your/game" value="${esc(draft.folder)}" spellcheck="false"><button class="ghost" data-cn-browse>Browse…</button></div>
      <div class="row"><button class="primary" data-cn-connect>Link ${esc(humanize(state.project))}</button></div>
      <p class="st-note">Glob only adds its own note (between <code>glob:start</code> and <code>glob:end</code>) and <code>glob/ASSETS.md</code>. Your code is never changed, and Unlink removes the note again.</p>`;
  }
  return `<h1>Your game</h1>
    <p class="st-lede">${esc(humanize(state.project))} is linked to your game. Coding agents working there copy finished art from <code>glob/ASSETS.md</code>.</p>
    <div class="cn-card">
      <div class="cn-top"><span class="dot ${cn.healthy ? "live" : "warn"}"></span><div><strong>${esc(cn.game_name)}</strong><small>${esc(cn.game_folder)}</small></div>
        <button class="mini" data-cn-open>Open folder</button></div>
      ${cn.healthy ? `<p class="st-note">Note in ${(cn.notes || ["AGENTS.md"]).map((n) => `<code>${esc(n)}</code>`).join(", ")}.</p>` : `<p class="cn-warn">Glob's note in <code>AGENTS.md</code> is missing. <button class="link-btn" data-cn-connect>Write it again</button></p>`}
    </div>
    ${how}
    <div class="st-label">Try it: say this to your agent</div>
    <div class="cn-tries">${TRY_PROMPTS.map((p) => `<button type="button" class="cn-try" data-cn-copy="${esc(p)}"><span>${esc(p)}</span><i>Copy</i></button>`).join("")}</div>
    <p class="st-note"><button class="link-btn danger-text" data-cn-disconnect>Unlink</button></p>`;
}

async function connectGame() {
  const folder = $("#cnFolder") ? $("#cnFolder").value.trim() : state.connect?.game_folder;
  if (!folder) return toast("Choose your game folder first.", true);
  try {
    state.connect = await api("/api/connect", { method: "POST", body: { folder } });
    state.cnDraft = null;
    toast(`Linked to ${state.connect.game_name}. Your coding agent finds the art in glob/ASSETS.md.`);
  } catch (err) { return toast(err.message, true); }
  applyChatMode();
  return inSetup() ? renderSetup() : renderSettings();
}

const inSetup = () => Boolean(state.setup) && !$("#setup").hidden;

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-cn-browse],[data-cn-connect],[data-cn-disconnect],[data-cn-open],[data-cn-copy]");
  if (!t) return;
  const d = t.dataset;
  const draft = (state.cnDraft ||= { folder: "" });
  if ($("#cnFolder")) draft.folder = $("#cnFolder").value.trim();
  const rerender = () => (inSetup() ? renderSetup() : renderSettings());
  if (d.cnBrowse !== undefined) {
    const folder = await chooseFolder("Choose your game folder");
    if (!folder) return;
    draft.folder = folder;
    return rerender();
  }
  if (d.cnConnect !== undefined) return connectGame();
  if (d.cnDisconnect !== undefined) {
    if (!confirm("Unlink the game? Glob removes its note from the game folder; the art in glob/ stays.")) return;
    try { state.connect = await api("/api/connect", { method: "DELETE" }); } catch (err) { return toast(err.message, true); }
    applyChatMode();
    return rerender();
  }
  if (d.cnOpen !== undefined) {
    try { await api("/api/storage/open", { method: "POST", body: { target: "grant", path: state.connect.game_folder } }); } catch (err) { toast(err.message, true); }
    return;
  }
  if (d.cnCopy) {
    try { await navigator.clipboard.writeText(d.cnCopy); toast("Copied"); } catch { toast("Copy failed", true); }
  }
});

function budgetCard(b) {
  if (!b) return "";
  const spent = Number(b.spent_usd ?? b.total_spent_usd ?? b.spent ?? 0);
  const cap = Number(b.spend_cap_usd ?? b.cap_usd ?? 0);
  const pct = cap ? Math.min(100, (spent / cap) * 100) : 0;
  return `<div class="budget-card"><div><strong>$${spent.toFixed(2)}</strong><span> spent in ${esc(humanize(state.project))}${cap ? ` of a $${cap.toFixed(2)} limit` : ""}</span></div>
    ${cap ? `<span class="meter"><i style="width:${pct}%"></i></span>` : ""}<small>Counted from what the providers report for each image, model and sound.</small></div>`;
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

const KIND_PLURAL = { character: "Characters", creature: "Creatures", item: "Items", weapon: "Weapons", prop: "Props", vehicle: "Vehicles", environment: "Places", level: "Levels", material: "Materials", kit: "Modular kits", ui: "UI", vfx: "Effects" };

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

// Image models grouped by provider, each group saying how Glob reaches it: a pasted key, a sign-in, or nothing yet.
const MODEL_VIA = { "API key": "uses your API key", "signed in": "uses your Meshy sign-in" };
function imageModelOptions(models, selected) {
  return Object.entries(Object.groupBy(models, (m) => m.label.split(" · ")[0])).map(([provider, items]) => {
    const via = items.find((m) => m.via)?.via;
    const how = via ? MODEL_VIA[via] || via : "not set up yet: Settings → Generators";
    return `<optgroup label="${esc(provider)} · ${esc(how)}">${items.map((m) => `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""} ${m.available ? "" : "disabled"}>${esc(m.label.split(" · ").slice(1).join(" · ") || m.label)} · $${m.price.toFixed(3)}</option>`).join("")}</optgroup>`;
  }).join("");
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
  const active = PLAIN_VIEWS.includes(state.view.type) ? "" : (state.view.path || "").split("/")[0];
  const initials = projectTitle().split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  const sections = ov.sections.filter((s) => s.path !== "levels");  // the Levels button above is that folder's way in
  $("#sections").innerHTML = `
    <div class="proj-card"><span class="proj-badge">${esc(initials || "F")}</span><div><strong>${esc(projectTitle())}</strong><small>${esc(ov.excerpt || "Your game project")}</small></div></div>
    <button class="sec${state.view.type === "home" ? " active" : ""}" data-home><small>00</small>Overview</button>
    <button class="sec vault-sec${state.view.type === "vault" ? " active" : ""}" data-vault>${icon("box")}<span class="sec-name">Final Vault</span></button>
    <button class="sec vault-sec${state.view.type === "levels" || (state.view.path || "").startsWith("levels/") ? " active" : ""}" data-levels>${icon("map")}<span class="sec-name">Levels</span></button>
    ${sections.length ? `<div class="sec-label">Sections</div>` : ""}
    ${sections.map((s) => `<button class="sec${s.path === active ? " active" : ""}" data-folder="${esc(s.path)}"><small>${s.number}</small><span class="sec-name">${esc(s.title)}</span>${s.children?.length ? `<span class="caret${s.path === active ? " open" : ""}"></span>` : ""}</button>${s.path === active ? subTree(s.children) : ""}`).join("")}
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
  else if (state.view.type === "levels") parts.push(`<button data-home>${esc(projectTitle())}</button>`, `<span class="sep">/</span>`, `<strong>Levels</strong>`);
  else if (state.view.type === "board") parts.push(`<button data-home>${esc(projectTitle())}</button>`, `<span class="sep">/</span>`, `<strong>Board</strong>`);
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
    <button class="asset-tile" data-open="${esc(f.path)}"${f.audio ? ` data-hover-audio="${esc(f.audio)}"` : ""}${f.model ? ` data-hover-model="${esc(f.model)}"` : ""}>
      <span class="asset-img">${f.thumb ? `<img src="${esc(fileUrl(f.thumb))}" alt="" loading="lazy">` : f.audio ? `<span class="asset-sound"><span class="wave" aria-hidden="true">${"<i></i>".repeat(14)}</span><small>Hover to listen</small></span>` : kindArt(f.asset_kind)}<span class="kind-tag">${esc(f.asset_kind)}</span>${f.model ? `<span class="tile-3d">${icon("cube")}3D</span>` : ""}${f.thumb ? `<span class="tile-pillar" role="button" tabindex="0" data-make-pillar="${esc(f.thumb)}" title="Make this image a pillar: a style reference for a whole folder">${icon("layers")}</span>` : ""}</span>
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

const KIND_ICON = new Proxy({}, { get: (_, k) => (ICON[k] ? icon(k) : AUDIO_KINDS.includes(k) ? icon("sound") : "") });

const KIND_LABEL = { environment: "Place", level: "Level", kit: "Modular kit", ui: "UI", vfx: "Effect", sfx: "Sound effect", voice: "Voice line", music: "Music" };

function kindTags(kinds, selected) {
  return `<div class="kind-tags" role="radiogroup" aria-label="Kind">${kinds.map((k) => `<label class="kind-chip"><input type="radio" name="kind" value="${esc(k)}" ${k === selected ? "checked" : ""}><span>${KIND_ICON[k] || "•"} ${esc(KIND_LABEL[k] || humanize(k))}</span></label>`).join("")}</div>`;
}

// Example name and description per kind, shown as placeholders in New asset.
const KIND_EXAMPLE = {
  character: ["Bronze Knight", "A stout knight in dented bronze armour with a red tabard"],
  creature: ["Cave Slime", "A wobbly green slime with a glowing core and two tiny eyes"],
  item: ["Health Potion", "A round glass bottle of bubbling red liquid with a cork"],
  weapon: ["Rusty Sword", "A short iron sword with a chipped blade and a leather grip"],
  prop: ["Wooden Barrel", "An old oak barrel with iron bands"],
  environment: ["Moonlit Forest", "A misty forest clearing at night with tall pines"],
  level: ["Crypt Room", "Top-down view of a small crypt room with coffins, torches and a broken pillar"],
  vehicle: ["Mine Cart", "A rusty mine cart on rails, filled with coal"],
  ui: ["Health Bar", "A red health bar in a carved stone frame"],
  vfx: ["Fire Burst", "A round burst of orange flames and sparks"],
  sfx: ["Coin Pickup", "A short bright metallic coin chime, under half a second"],
  voice: ["Shopkeeper Greeting", "Welcome, traveller! Take a look, everything's on sale."],
  music: ["Forest Theme", "Calm looping flute and harp melody, gentle and mysterious"],
  material: ["Wet Flagstone", "Worn dark flagstones with moss in the cracks"],
  kit: ["Crypt Kit", "Walls, floors and doorways for the sunken crypt"],
};
const askFor = (kind) => (kind === "voice" ? "What does the character say?" : AUDIO_KINDS.includes(kind) ? "What does it sound like?" : kind === "kit" ? "What is it for?" : "What does it look like?");

async function newAssetDialog(folder) {
  const kinds = (await studioOptions()).kinds.filter((k) => k !== "kit" || !is2d());  // kits are 3D pieces
  // Suggest the kind from the folder name (creatures → creature) when it matches one.
  const guess = kinds.find((k) => (folder || "").toLowerCase().split("/").some((part) => part.replace(/^\d+[-_ ]*/, "").replace(/s$/, "").includes(k))) || "";
  openDialog(`
    <form class="dlg" id="newAssetForm">
      <h2>New asset</h2>
      <p>Creates a document in <code>${esc(folder || "the project root")}</code> that you can generate images and 3D for.</p>
      <label>Name<input name="title" maxlength="80" placeholder="${esc(KIND_EXAMPLE[guess]?.[0] || KIND_EXAMPLE.character[0])}" autofocus></label>
      <div class="dlg-field"><span>Kind</span>${kindTags(kinds, guess)}</div>
      <label><span id="naWhat">${askFor(guess)}</span><textarea name="prompt" rows="3" placeholder="${esc(KIND_EXAMPLE[guess]?.[1] || KIND_EXAMPLE.character[1])}"></textarea></label>
      <p class="form-why" id="naWhy" hidden>Give it a name first.</p>
      <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">Create</button></div>
    </form>`);
  // The examples follow the kind: a sound gets a sound, a level a level.
  $("#newAssetForm").addEventListener("change", (event) => {
    if (event.target.name !== "kind") return;
    const [name, prompt] = KIND_EXAMPLE[event.target.value] || KIND_EXAMPLE.character;
    const form = event.target.form;
    form.title.placeholder = name;
    form.prompt.placeholder = prompt;
    $("#naWhat").textContent = askFor(event.target.value);
  });
  $("#newAssetForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    if (!String(data.title || "").trim()) { $("#naWhy").hidden = false; return event.target.title.focus(); }
    if (!data.kind) { $("#naWhy").textContent = "Pick what kind of asset it is."; $("#naWhy").hidden = false; return; }
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
  await renderViewBody();
  mountHero();
  mountWave();
  if (state.view.type === "board") mountBoard();
}

// The live 3D hero on an asset page. Its canvas survives re-renders of the same asset, so the
// model is loaded once and keeps turning instead of reloading on every workspace change.
let hero = null;

async function mountHero() {
  const slot = $("#content [data-hero-model]");
  const path = slot?.dataset.heroModel;
  if (hero && hero.path !== path) { hero.view?.dispose(); hero.host.remove(); hero = null; }
  if (!slot) return;
  if (hero) {
    slot.querySelector(".hero-stage").replaceWith(hero.host);
    if (hero.view) slot.classList.add("live");
    return;
  }
  const h = hero = { path, host: Object.assign(document.createElement("div"), { className: "hero-stage" }), view: null };
  slot.querySelector(".hero-stage").replaceWith(h.host);
  try {
    const viewer = await viewerModule();
    const view = await viewer.createViewer(h.host, fileUrl(path), { autoRotate: true, grid: false });
    if (hero !== h) { view.dispose(); return; }
    h.view = view;
    if (view.clips.length) view.play(0);
    h.host.closest("[data-hero-model]")?.classList.add("live");
  } catch { /* the poster image stays */ }
}

async function renderViewBody() {
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
    if (ov.empty && ov.game) {
      const g = ov.game;
      content.innerHTML = `<div class="empty game-start"><h1>Let's set up ${esc(g.title)}</h1>
        <p>Glob is connected to your ${esc(g.engine)} game in <code>${esc(g.folder)}</code>. Describe the game once and Glob builds its workspace:
        a front page, a section per part of the game, a style bible, and a page for every character, creature, item, place and sound to make.
        Nothing is generated until you say so.</p>
        <button class="keyart-cta" data-game-build>${icon("sparkle")}<span><strong>Build it in Glob's chat</strong><small>Paste your pitch or design notes, or let Glob read the docs in your game folder</small></span></button>
        <button class="keyart-cta sample-cta" data-install-sample>${icon("eye")}<span><strong>Or explore a finished project first</strong><small>Pocket Kaiju: characters, 3D models and sounds. Opens as its own project; no credits used.</small></span></button></div>`;
      return;
    }
    if (ov.empty) {
      content.innerHTML = `<div class="empty"><h1>This project is empty</h1>
        <p>Tell the agent on the left about your game, for example:</p>
        <p><em>“Create a project structure for a cozy farming game set on a floating island.”</em></p>
        <p>Sections, documents and pages will appear here as the agent creates them.</p>
        <button class="keyart-cta sample-cta" data-install-sample>${icon("eye")}<span><strong>Or explore a finished project first</strong><small>Pocket Kaiju: characters, 3D models and sounds. Opens as its own project; no credits used.</small></span></button></div>`;
      return;
    }
    const body = await readPage(ov.page);
    const jobsHere = Object.values(state.jobs).some((j) => j.asset?.endsWith?.("key-art.md") && ["running", "queued"].includes(j.status));
    content.innerHTML = `
      <header class="proj-hero${ov.cover ? " has-cover" : ""}">
        ${ov.cover ? `<div class="proj-hero-blur" style="background-image:url('${esc(fileUrl(ov.cover))}')"></div><img class="proj-hero-img" src="${esc(fileUrl(ov.cover))}" alt="">` : ""}
        <div class="proj-hero-text">
          <div class="kicker">Project / ${esc(projectTitle())}</div>
          <h1 class="title">${esc(projectTitle())}</h1>
          ${ov.excerpt ? `<p class="lede">${esc(ov.excerpt)}</p>` : ""}
        </div>
        ${ov.cover ? "" : `<button class="keyart-cta" data-key-art="${esc(state.project)}"${jobsHere ? " disabled" : ""}>${icon("sparkle")}<span><strong>${jobsHere ? "Painting key art…" : "Make key art"}</strong><small>${jobsHere ? "It becomes the cover when it lands" : "A cover image from your pitch, for this page and the projects overview"}</small></span></button>`}
      </header>
      <div class="actions-row">
        <button class="ghost" data-batch="">${icon("sparkle")} Generate all missing</button>
        <button class="ghost" data-press-kit>${icon("download")} Press kit</button>
        ${ov.page ? `<button class="ghost" data-open="${esc(ov.page)}">Open ${esc(ov.page)}</button>` : ""}
      </div>
      ${showcaseStrip(ov.showcase || [])}
      ${body ? `<div class="md">${renderMarkdown(stripLead(body), "")}</div>` : ""}
      ${ov.sections.length ? `<h2 class="block-title">Sections</h2>${folderCards(ov.sections, true)}` : ""}
      ${pillarStrip(ov.pillars || [], "", "Bible pillars", ov.text_pillars || [])}
      ${ov.files.length ? `<h2 class="block-title">Files</h2>${fileBlocks(ov.files)}` : ""}`;
    return;
  }

  if (view.type === "vault") return renderVault();
  if (view.type === "levels") return renderLevels();
  if (view.type === "board") return renderBoard();

  if (view.type === "folder") {
    let folder;
    try { folder = await api(`/api/overview/folder?path=${encodeURIComponent(view.path)}`); } catch (err) {
      state.view = { type: "home" };
      return renderView();
    }
    state.view.page = folder.page || "";
    if (view.editing && folder.page) {
      const file = await api(`/api/overview/file?path=${encodeURIComponent(folder.page)}`);
      content.innerHTML = `<div class="doc-bar"><span class="path"><bdi>${esc(folder.page)}</bdi></span><div><button class="ghost" data-cancel-edit>Cancel</button> <button class="primary" data-save>Save</button></div></div>
        <textarea class="editor" id="editor" spellcheck="false">${esc(file.content)}</textarea>`;
      return;
    }
    const body = await readPage(folder.page);
    const empty = !folder.page && !folder.folders.length && !folder.files.length;
    const assets = folder.files.filter((f) => f.kind === "asset");
    const others = folder.files.filter((f) => f.kind !== "asset");
    content.innerHTML = `
      <div class="doc-bar"><span class="path"><bdi>${esc(folder.page || folder.path)}</bdi></span><div>${folder.page ? `<button class="ghost" data-edit>Edit page</button> ` : ""}<button class="ghost" data-new-folder="${esc(folder.path)}">+ New folder</button> <button class="ghost" data-new-asset="${esc(folder.path)}">+ New asset</button> ${assets.length || folder.folders.length ? `<button class="ghost" data-batch="${esc(folder.path)}" title="Generate candidates for every asset in here that has none yet">${icon("sparkle")} Generate missing</button> ` : ""}<button class="ghost" data-export="${esc(folder.path)}">Export</button></div></div>
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
      // A view restored from last time can point at a file that's gone since: just start at the overview.
      if (!view.restored) toast(err.message, true);
      state.view = { type: "home" };
      return renderView();
    }
    const isMd = view.path.toLowerCase().endsWith(".md");
    if (isMd && !view.editing) {
      const asset = await api(`/api/studio/asset?path=${encodeURIComponent(view.path)}`).catch(() => null);
      if (asset?.kind === "level") state.pack = await api(`/api/studio/level-pack?path=${encodeURIComponent(asset.path)}`).catch(() => null);
      if (asset?.kind === "kit") state.materials = (await api("/api/studio/materials").catch(() => ({ materials: [] }))).materials;
      if (asset?.is_asset) { content.innerHTML = renderAssetPage(asset); content.dataset.dropAsset = ""; queueThumbs(asset); return; }
    }
    content.innerHTML = `
      <div class="doc-bar"><span class="path"><bdi>${esc(file.path)}</bdi></span>
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
  const meshBtn = AUDIO_KINDS.includes(e.kind) || e.kind === "material" || (is2d() && !e.model) ? "" : e.model
    ? `<button class="vault-thumb mesh" data-view-model="${esc(e.model.path)}" title="Open in the 3D viewer">${e.model.thumb ? `<img src="${esc(fileUrl(e.model.thumb))}" alt="" loading="lazy">` : icon("cube")}</button>`
    : `<button class="vault-thumb mesh missing" data-open="${esc(e.path)}" title="Make 3D"><span>No 3D</span></button>`;
  return `<div class="vault-row${e.ready ? " ready" : ""}">
    <button class="vault-thumb" data-open="${esc(e.path)}" title="${AUDIO_KINDS.includes(e.kind) ? "Final take" : "Final image"}">${e.winner && !AUDIO_KINDS.includes(e.kind) ? `<img src="${esc(fileUrl(e.winner))}" alt="" loading="lazy">` : kindArt(AUDIO_KINDS.includes(e.kind) ? "sound" : e.kind, "")}</button>
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

const ACTION_LABEL = { candidates: "Generate 2D", variant: "Variant", edit: "Edit image", views: "Turnaround", prop_sheet: "Prop sheet", trim_sheet: "Trim sheet", make_3d: "Make 3D", rig: "Rig", animate: "Animate" };

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
  material: `<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 9.5h16M4 15h16M9 4v5.5M15 9.5V15M9 15v5"/>`,
  kit: `<path d="M4 20V9l4-2v13M8 20h12V11h-5v-3h-3v3H8"/><path d="M4 20h16"/>`,
  folder: `<path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h4l2 2.5h7a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>`,
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
  copy: `<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.2"/><path d="M15.5 8.5V6.2A2.2 2.2 0 0 0 13.3 4H6.2A2.2 2.2 0 0 0 4 6.2v7.1a2.2 2.2 0 0 0 2.2 2.2h2.3"/>`,
  inbox: `<path d="M4 13.5 6.5 5h11l2.5 8.5V19a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 19z"/><path d="M4 13.5h4.5l1.5 2.5h4l1.5-2.5H20"/>`,
  send: `<path d="M4 12 20 4l-5 16-3.5-6.5z"/><path d="M11.5 13.5 20 4"/>`,
  film: `<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M8 4.5v15M16 4.5v15M3.5 9h4.5M3.5 15h4.5M16 9h4.5M16 15h4.5"/>`,
  // One per kind of asset: the picture an asset shows until it has art of its own.
  character: `<circle cx="12" cy="7.5" r="3.6"/><path d="M4.8 20.5c0-4.1 3.2-7.2 7.2-7.2s7.2 3.1 7.2 7.2"/>`,
  creature: `<path d="M4.5 19.5c0-6.2 3.3-11 7.5-11s7.5 4.8 7.5 11z"/><path d="M8.2 9.6 6 4.5M15.8 9.6 18 4.5"/><circle cx="9.6" cy="14" r=".9"/><circle cx="14.4" cy="14" r=".9"/>`,
  item: `<path d="M9.5 3.5h5M10.3 3.5v5.2L5.8 16.6A2.6 2.6 0 0 0 8.1 20.5h7.8a2.6 2.6 0 0 0 2.3-3.9l-4.5-7.9V3.5"/><path d="M7.4 14h9.2"/>`,
  weapon: `<path d="M20 4 10.5 13.5M20 4h-4.2l-8.3 8.3M20 4v4.2l-8.3 8.3"/><path d="M5.8 13.6l4.6 4.6M8.1 15.9 4 20"/>`,
  prop: `<rect x="4" y="4.5" width="16" height="16" rx="1.6"/><path d="M4 9h16M4 16h16M8 9l8 7M16 9l-8 7"/>`,
  vehicle: `<path d="M3.5 16v-3.4l2.2-5.1h12.6l2.2 5.1V16z"/><path d="M3.5 12.6h17"/><circle cx="7.6" cy="16.6" r="1.9"/><circle cx="16.4" cy="16.6" r="1.9"/>`,
  environment: `<path d="M2.5 19.5 9 9.2l4 5.9 2.6-3.6 6 8z"/><circle cx="17" cy="6" r="2"/>`,
  level: `<path d="M3.5 6.5 9 4.5l6 2 5.5-2v13l-5.5 2-6-2-5.5 2z"/><path d="M9 4.5v13M15 6.5v13"/>`,
  ui: `<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M3.5 9h17M7 13h6M7 16h4"/>`,
  vfx: `<path d="M12 3v4.2M12 16.8V21M3 12h4.2M16.8 12H21M5.6 5.6l3 3M15.4 15.4l3 3M5.6 18.4l3-3M15.4 8.6l3-3"/>`,
  sound: `<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>`,
  map: `<path d="M3.5 6.5 9 4.5l6 2 5.5-2v13l-5.5 2-6-2-5.5 2z"/><path d="M9 4.5v13M15 6.5v13"/>`,
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[name]}</svg>`;
// What an asset shows before it has a picture: its kind's icon on a soft tile, so an empty folder still reads.
const KIND_HUE = { character: 340, creature: 20, item: 160, weapon: 0, prop: 35, vehicle: 200, environment: 130, level: 100, ui: 260, vfx: 290, sound: 190 };
const kindArt = (kind, text = "No image yet") => {
  const k = ICON[kind] ? kind : "prop";
  return `<span class="kind-art" style="--hue:${KIND_HUE[k] ?? 330}">${icon(k)}${text ? `<small>${esc(text)}</small>` : ""}</span>`;
};

// Image outputs that can become the winner and be worked on further.
const WORK_KINDS = new Set(["candidates", "variants", "edits"]);

// A card's ⋯ menu closes when you pick something in it or click anywhere else.
document.addEventListener("click", (event) => {
  document.querySelectorAll(".out-more[open]").forEach((menu) => {
    if (!menu.contains(event.target) || event.target.closest(".out-menu button")) menu.open = false;
  });
});

// Drag a picture or model out of Glob into Explorer / Finder (or any folder) to copy the file there.
const DRAG_TYPES = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", glb: "model/gltf-binary", fbx: "application/octet-stream", obj: "model/obj", wav: "audio/wav", mp3: "audio/mpeg" };
document.addEventListener("dragstart", (event) => {
  const el = event.target.closest?.("[data-drag-file]");
  if (!el || !event.dataTransfer) return;
  const path = el.dataset.dragFile;
  const name = path.split("/").pop();
  const url = `${location.origin}${fileUrl(path)}`;
  event.dataTransfer.setData("DownloadURL", `${DRAG_TYPES[name.split(".").pop().toLowerCase()] || "application/octet-stream"}:${name}:${url}`);
  event.dataTransfer.setData("text/uri-list", url);
  event.dataTransfer.effectAllowed = "copy";
});

const removeBtn = (path) =>`<button type="button" class="out-del" data-remove-output="${esc(path)}" title="Move to trash (Undo right after)" aria-label="Delete">×</button>`;

function modelCard(o, kind) {
  const glb = /\.glb$/i.test(o.path);
  const badge = { "3d": ["cube", "3D"], rig: ["bones", "Rigged"], animations: ["film", "Animation"] }[kind] || ["cube", "3D"];
  const ext = o.path.split(".").pop().toUpperCase();
  const formats = Object.entries(o.formats || { [ext.toLowerCase()]: o.path });
  return `<div class="out model">${removeBtn(o.path)}<button type="button" class="out-img" data-view-model="${esc(o.path)}" draggable="true" data-drag-file="${esc(o.path)}" title="Open in the 3D viewer · drag into a folder to copy the model">${o.thumb ? `<img src="${esc(fileUrl(o.thumb))}" alt="">` : `<div class="no-thumb">${icon("cube")}<span>${esc(ext)} model</span></div>`}<span class="out-badge mesh">${icon(badge[0])}${badge[1]} · ${esc(ext)}</span></button>
    <div class="out-bar"><button class="act" data-view-model="${esc(o.path)}">${icon("eye")}View</button>
    ${kind === "3d" && glb ? `<button class="act" data-action="rig" data-source="${esc(o.path)}">${icon("bones")}Rig</button>` : kind === "rig" && glb ? `<button class="act" data-action="animate">${icon("play")}Animate</button>` : `<button class="act" data-sprites="${esc(o.path)}">${icon("grid")}Sprites</button>`}
    <div class="fmt-links">${icon("download")}${formats.map(([fmt, path]) => `<a href="${esc(fileUrl(path))}" download>${esc(fmt.toUpperCase())}</a>`).join("")}</div></div></div>`;
}

function outputGrid(all, winner, kind = "candidates") {
  if (!all.length) return "";
  // Engine files the browser can't show (Godot .res clips, .blend…) are listed as compact chips.
  const files = all.filter((o) => o.type === "file");
  const items = all.filter((o) => o.type !== "file");
  const chips = files.length ? `<div class="file-chips">${files.map((o) => {
    const name = o.path.split("/").pop();
    return `<span class="file-chip-wrap"><a class="file-chip" href="${esc(fileUrl(o.path))}" download title="Download ${esc(name)}">${icon(kind === "animations" ? "film" : "download")}<span>${esc(name)}</span></a><button type="button" class="chip-del" data-remove-output="${esc(o.path)}" title="Move to trash" aria-label="Delete ${esc(name)}">×</button></span>`;
  }).join("")}</div>` : "";
  return (items.length ? "" : chips) + (items.length ? `<div class="out-grid">${items.map((o) => {
    if (o.type === "model") return modelCard(o, kind);
    if (o.type === "audio") return audioCard(o, winner);
    const isWinner = o.path === winner;
    const img = `<a class="out-img" href="${esc(fileUrl(o.path))}" target="_blank" rel="noopener" data-lightbox="${esc(o.path)}" data-drag-file="${esc(o.path)}" title="Click to enlarge · drag into a folder to copy it"><img src="${esc(fileUrl(o.path))}" alt="" loading="lazy">${isWinner ? `<span class="out-badge win">${icon("star")}Winner</span>` : ""}</a>`;
    if (!WORK_KINDS.has(kind)) {
      return `<div class="out${kind === "icons" || kind === "sprites" ? " checker" : ""}">${removeBtn(o.path)}${img}<div class="out-bar">
        ${kind === "parts" || kind === "views" ? `<button class="act" data-icons="${esc(o.path)}">${icon("grid")}Icons</button><button class="act" data-winner="${esc(o.path)}">${icon("star")}Winner</button>` : ""}
        ${kind === "sheets" ? `<button class="act hero-act" data-sheet-cut="${esc(o.path)}">${icon("scissors")}Cut into props</button>` : ""}
        <a class="act wide" href="${esc(fileUrl(o.path))}" download>${icon("download")}Download</a></div></div>`;
    }
    // One clear next step per card; everything else waits in the ⋯ menu.
    return `<div class="out${isWinner ? " winner" : ""}">${removeBtn(o.path)}${img}
      <div class="out-bar compact">${isWinner ? `<span class="act win-tag">${icon("star")}Winner</span>` : `<button class="act hero-act" data-winner="${esc(o.path)}">${icon("star")}Pick winner</button>`}
      <details class="out-more"><summary class="act" title="More actions" aria-label="More actions">⋯</summary>
        <div class="out-menu">
          <button type="button" data-action="variant" data-source="${esc(o.path)}">${icon("variant")}Variant</button>
          <button type="button" data-action="edit" data-source="${esc(o.path)}">${icon("edit")}Edit</button>
          <button type="button" data-split="${esc(o.path)}">${icon("scissors")}Split into parts</button>
          <button type="button" data-icons="${esc(o.path)}">${icon("grid")}Make icons</button>
          <hr><button type="button" class="pillar-item-btn" data-make-pillar="${esc(o.path)}" title="Use this image as a style reference for a whole folder">${icon("layers")}Make pillar</button>
        </div></details></div></div>`;
  }).join("")}</div>${chips}` : "");
}

const AUDIO_KINDS = ["sfx", "voice", "music"];
const AUDIO_RE = /\.(mp3|wav|ogg|flac)$/i;
const AUDIO_COPY = {
  sfx: { prompt: "Prompt: what this sound is", run: "Generate sounds", empty: "No sound picked yet." },
  voice: { prompt: "Line: what the character says", run: "Generate line", empty: "No take picked yet." },
  music: { prompt: "Prompt: mood, instruments, tempo", run: "Generate music", empty: "No track picked yet." },
};

function audioCard(o, winner) {
  const isWinner = o.path === winner;
  const name = o.path.split("/").pop();
  return `<div class="out audio${isWinner ? " winner" : ""}">${removeBtn(o.path)}
    <div class="audio-face">${isWinner ? `<span class="out-badge win">${icon("star")}Winner</span>` : ""}<span class="wave" aria-hidden="true">${"<i></i>".repeat(18)}</span><small>${esc(name)}</small></div>
    <audio controls preload="none" src="${esc(fileUrl(o.path))}"></audio>
    <div class="out-bar">${isWinner ? "" : `<button class="act wide hero-act" data-winner="${esc(o.path)}">${icon("star")}Pick winner</button>`}
      <a class="act wide" href="${esc(fileUrl(o.path))}" download>${icon("download")}Download</a></div></div>`;
}

// Animation clips as one compact list: the same clip in several files (Walk.fbx and walk.res) is one row, and
// an FBX clip plays on the model in the hero viewer. Glob's own animated GLBs (mesh included) keep their cards.
function animationBlock(list, canPlay) {
  const cards = list.filter((o) => o.type === "model" && /\.glb$/i.test(o.path));
  const clips = {};
  for (const o of list.filter((x) => !cards.includes(x))) {
    const file = o.path.split("/").pop();
    const stem = file.replace(/\.[^.]+$/, "");
    (clips[stem.toLowerCase().replace(/[^a-z0-9]+/g, "")] ||= { label: humanize(stem.replace(/[_-]+/g, " ")), files: [] }).files.push(o.path);
  }
  const rows = Object.values(clips).sort((x, y) => x.label.localeCompare(y.label)).map((c) => {
    const playable = canPlay && c.files.find((p) => /\.(fbx|glb|gltf)$/i.test(p));
    return `<div class="clip">${playable ? `<button type="button" class="clip-play" data-play-clip="${esc(playable)}" title="Play on the model above">${icon("play")}</button>` : `<span class="clip-play off">${icon("film")}</span>`}
      <span class="clip-name">${esc(c.label)}</span>
      <span class="clip-files">${c.files.map((p) => `<a href="${esc(fileUrl(p))}" download title="${esc(p.split("/").pop())}">${esc(p.split(".").pop().toUpperCase())}</a>`).join("")}</span></div>`;
  }).join("");
  const count = Object.keys(clips).length;
  return `<h2 class="block-title" id="out-animations">Animations <span class="block-count">${count + cards.length}</span></h2>
    ${cards.length ? outputGrid(cards, "", "animations") : ""}
    ${count ? `<div class="clip-list">${rows}</div>${canPlay ? "" : `<p class="hint-line">Add the character's 3D model to play these clips on it here.</p>`}` : ""}`;
}

// Textures are the model's material maps: a small strip, not a wall of cards.
function textureStrip(list) {
  const images = list.filter((o) => o.type === "image");
  const files = list.filter((o) => o.type !== "image");
  return `<details class="tex-block"${list.length <= 8 ? " open" : ""}><summary><h2 class="block-title" id="out-textures">Textures <span class="block-count">${list.length}</span></h2></summary>
    <div class="tex-strip">${images.map((o) => `<a href="${esc(fileUrl(o.path))}" data-lightbox="${esc(o.path)}" title="${esc(o.path.split("/").pop())}"><img src="${esc(fileUrl(o.path))}" alt="" loading="lazy"></a>`).join("")}
    ${files.map((o) => `<a class="tex-file" href="${esc(fileUrl(o.path))}" download>${esc(o.path.split("/").pop())}</a>`).join("")}</div></details>`;
}

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-play-clip]");
  if (!t) return;
  if (!hero?.view) return toast("The 3D model is still loading.", true);
  document.querySelectorAll(".clip.playing").forEach((c) => c.classList.remove("playing"));
  try {
    if (await hero.view.playFrom(fileUrl(t.dataset.playClip))) {
      t.closest(".clip").classList.add("playing");
      $("#content [data-hero-model]")?.scrollIntoView({ behavior: "smooth", block: "center" });
    } else toast("This clip's bones don't match the model, so it can't play here.", true);
  } catch { toast("Couldn't load this clip.", true); }
});

const OUTPUT_SECTIONS = [
  ["audio", "Takes"], ["3d", "3D models"], ["rig", "Rigged"], ["animations", "Animations"], ["sprites", "Sprite sheets"], ["icons", "Icons"],
  ["sheets", "Prop sheets"], ["views", "Turnaround views"], ["candidates", "Candidates"], ["variants", "Variants"], ["edits", "Edits"], ["textures", "Textures"], ["parts", "Parts"],
];

function renderAssetPage(a) {
  const title = (a.body.match(/^#\s+(.+)$/m) || [])[1] || humanize(a.path.split("/").pop());
  const jobs = runningJobs(a.path);
  const groups = {};
  for (const p of a.pillars) (groups[p.source] ||= []).push(p);
  const o = a.outputs;
  // With no winner image, the newest model fills the hero slot: animated first, then rigged, then plain 3D. An
  // animation counts only as a GLB (Glob's own, with the mesh): an imported FBX clip is usually bones alone.
  const heroModel = [...(o.animations || []).filter((m) => /\.glb$/i.test(m.path)), ...(o.rig || []), ...(o["3d"] || [])].find((m) => m.type === "model");
  if (AUDIO_KINDS.includes(a.kind)) return renderAudioAsset(a, title, jobs);
  if (a.kind === "kit") return renderKitAsset(a, title);
  return `
    <div class="kicker">Asset / ${esc(a.path)}</div>
    <div class="asset-head"><h1 class="title">${esc(title)}</h1><span class="kind-tag">${esc(a.kind)}</span></div>
    ${problemJobs(a.path).map((j) => `<div class="job-banner bad"><span class="bad-dot">!</span><span class="grow">${esc(ACTION_LABEL[j.action] || j.action)} ${j.status === "interrupted" ? "was interrupted" : "failed"}: ${esc(j.error || "no details")}</span><button type="button" class="mini" data-action="${esc(j.action)}">Try again</button><button type="button" class="mini" data-dismiss-job="${esc(j.id)}">Dismiss</button></div>`).join("")}
    ${jobs.length ? `<div class="job-banner"><span class="spinner"></span>${jobs.map((j) => esc(ACTION_LABEL[j.action] || j.action)).join(", ")} running… results appear here automatically.</div>` : ""}
    <div class="asset-top">
      <div class="winner-box drop-zone${heroModel && !a.winner ? " hero-3d" : ""}" data-drop-winner${heroModel && !a.winner ? ` data-hero-model="${esc(heroModel.path)}"` : ""} title="${is2d() ? "Drop an image here to use it as the winner" : "Drop an image to use it as the winner, or a 3D model to add it"}">${a.winner ? `<img src="${esc(fileUrl(a.winner))}" alt="Winner"><span class="drop-hint">${is2d() ? "Drop an image to replace" : "Drop an image to replace, or a 3D model to add"}</span>` : heroModel ? `
        ${heroModel.thumb ? `<img class="hero-poster" src="${esc(fileUrl(heroModel.thumb))}" alt="">` : ""}<div class="hero-stage"></div>
        <span class="out-badge mesh hero-badge">${icon("cube")}3D · drag to turn</span>
        <button type="button" class="mini hero-open" data-view-model="${esc(heroModel.path)}">${icon("eye")}Open viewer</button>
        <span class="drop-hint">Drop an image to make it the winner</span>` : `<div class="no-thumb big"><p class="drop-copy">${kindArt(a.kind, "")}No picture yet.<br>${(o.candidates || []).length ? `<button type="button" class="link-btn" data-scroll-candidates>Pick ${(o.candidates || []).length === 1 ? "the candidate" : `one of the ${(o.candidates || []).length} candidates`} below</button>, <b>drop or paste an image${is2d() ? "" : " or 3D model"} here</b>` : `Generate 2D candidates, <b>drop or paste an image${is2d() ? "" : " or 3D model"} here</b>`}<br>or <button type="button" class="link-btn" data-pick-winner-file>choose a file</button>.</p></div>`}</div>
      <div class="asset-side">
        <label class="field-label" for="assetPrompt">Prompt: what this ${esc(a.kind)} looks like</label>
        <textarea id="assetPrompt" class="prompt-box" rows="6" data-asset="${esc(a.path)}">${esc(a.prompt)}</textarea>
        <div class="action-row">
          <button class="primary" data-action="candidates">Generate 2D</button>
          <button class="ghost" data-action="variant"${a.winner ? "" : " disabled"}>Variant</button>
          <button class="ghost" data-action="edit"${a.winner ? "" : " disabled"}>Edit image</button>
          ${is2d() || a.kind === "material" || a.kind === "level" ? "" : `<button class="ghost sem-mesh" data-action="make_3d"${a.winner ? "" : " disabled"}>Make 3D</button>`}
        </div>
        ${a.kind === "level" ? levelPanel(a) : ""}
        ${a.kind === "material" ? materialPanel(a) : ""}
        ${gameTools(a)}
        <div class="applied">
          <div class="field-label pillar-head">Pillars sent with every generation
            <select class="pillar-strength" data-pillar-strength title="How strongly the pillars steer this asset">${[["off", "Off"], ["light", "Light · palette only"], ["normal", "Normal"], ["strong", "Strong · match closely"]].map(([v, l]) => `<option value="${v}" ${(a.meta.pillars || "normal") === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
          ${a.pillars.length ? Object.entries(groups).map(([src, items]) => `<div class="applied-group"><span>${esc(src)}</span><div class="pillar-row small">${items.map((p) => `<img src="${esc(fileUrl(p.path))}" title="${esc(p.path)}" alt="">`).join("")}</div></div>`).join("")
            : `<p class="pillar-empty">${(a.text_pillars || []).length ? "No reference images yet: add some" : "No pillars apply yet. Add reference images"} on the project front page (bible) or on this asset's folder.</p>`}
          ${(a.text_pillars || []).length ? `<details class="bible-mini"><summary><span>Style bible</span>${a.text_pillars.map((t) => esc(t.source)).join(" · ")}<em>View</em></summary>
            ${a.text_pillars.map((t) => `<div class="md">${renderMarkdown(t.text)}</div>`).join("")}</details>` : ""}
          ${a.style_rules.map((r) => `<div class="applied-group"><span>Style · ${esc(r.source)}</span><p>${esc(r.text)}</p></div>`).join("")}
        </div>
      </div>
    </div>
    ${a.kind === "level" ? packPicks() : ""}
    ${OUTPUT_SECTIONS.map(([kind, label]) => (o[kind] || []).length ? (kind === "animations" ? animationBlock(o[kind], Boolean(heroModel && !a.winner))
      : kind === "textures" ? textureStrip(o[kind]) : `<h2 class="block-title" id="out-${kind}">${label}</h2>${outputGrid(o[kind], a.winner, kind)}`) : "").join("")}
    <h2 class="block-title">Notes</h2>
    <div class="doc-bar"><span class="path"><bdi>${esc(a.path)}</bdi></span><button class="ghost" data-edit>Edit document</button></div>
    <div class="md">${renderMarkdown(a.body.replace(/^#\s+.+$/m, ""), dirOf(a.path))}</div>`;
}

// Sound assets: a player for the winner take, the prompt (or the line to speak), and the takes below.
function renderAudioAsset(a, title, jobs) {
  const copy = AUDIO_COPY[a.kind];
  const takes = a.outputs.audio || [];
  return `
    <div class="kicker">Asset / ${esc(a.path)}</div>
    <div class="asset-head"><h1 class="title">${esc(title)}</h1><span class="kind-tag">${esc(a.kind)}</span></div>
    ${problemJobs(a.path).map((j) => `<div class="job-banner bad"><span class="bad-dot">!</span><span class="grow">${esc(copy.run)} failed: ${esc(j.error || "no details")}</span><button type="button" class="mini" data-action="candidates">Try again</button><button type="button" class="mini" data-dismiss-job="${esc(j.id)}">Dismiss</button></div>`).join("")}
    ${jobs.length ? `<div class="job-banner"><span class="spinner"></span>${esc(copy.run)} running… takes appear here automatically.</div>` : ""}
    <div class="asset-top">
      <div class="winner-box audio-hero drop-zone" data-drop-winner title="Drop an audio file here">${a.winner && AUDIO_RE.test(a.winner)
        ? `<div class="audio-big" data-wave-src="${esc(a.winner)}"><canvas class="wave-canvas" height="120"></canvas><audio controls src="${esc(fileUrl(a.winner))}"></audio>
            <div class="wave-tools"><small>${esc(a.winner.split("/").pop())}</small><span class="grow"></span><span class="wave-sel" hidden></span>
              <button type="button" class="mini" data-wave-loop title="Play the last 2 seconds straight into the first 2, to hear the loop seam">${icon("turn")}Loop check</button>
              <button type="button" class="mini" data-wave-trim disabled title="Drag across the waveform to choose a part, then save it as a new take">${icon("scissors")}Save selection</button></div></div><span class="drop-hint">Drop an audio file to add a take</span>`
        : `<div class="no-thumb big"><p class="drop-copy"><span class="drop-icon">♪</span>${copy.empty}<br>Generate takes or <b>drop an MP3 / WAV here</b>.</p></div>`}</div>
      <div class="asset-side">
        <label class="field-label" for="assetPrompt">${copy.prompt}</label>
        <textarea id="assetPrompt" class="prompt-box" rows="6" data-asset="${esc(a.path)}">${esc(a.prompt)}</textarea>
        <div class="action-row"><button class="primary" data-action="candidates">${icon("sparkle")}${copy.run}</button></div>
        ${a.kind === "voice" ? `<p class="hint-line">Voice: <b>${esc(a.meta.voice_name || a.meta.voice || "pick one when you generate")}</b></p>` : ""}
      </div>
    </div>
    ${takes.length ? `<h2 class="block-title">Takes</h2>${outputGrid(takes, a.winner, "audio")}` : ""}
    <h2 class="block-title">Notes</h2>
    <div class="doc-bar"><span class="path"><bdi>${esc(a.path)}</bdi></span><button class="ghost" data-edit>Edit document</button></div>
    <div class="md">${renderMarkdown(a.body.replace(/^#\s+.+$/m, ""), dirOf(a.path))}</div>`;
}

async function uploadAudio(files) {
  const sounds = [...files].filter((f) => AUDIO_RE.test(f.name));
  if (!sounds.length) return;
  const form = new FormData();
  sounds.forEach((f) => form.append("files", f));
  try {
    const res = await fetch(`/api/studio/audio-upload?project=${encodeURIComponent(state.project)}&path=${encodeURIComponent(state.view.path)}`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    toast(`Added ${plural(data.saved.length, "take")}`);
    renderView();
  } catch (err) { toast(err.message, true); }
}

async function renderAudioSheet() {
  const p = state.preview;
  const copy = AUDIO_COPY[p.kind];
  const s = p.settings;
  if (p.kind === "voice" && !state.voices) {
    state.voices = (await api("/api/studio/voices").catch(() => ({ voices: [] }))).voices || [];
  }
  const voices = state.voices || [];
  if (p.kind === "voice" && !s.voice_id && voices[0]) s.voice_id = voices[0].id;
  const model = p.models.find((m) => m.id === p.model);
  openDialog(`
    <div class="dlg sheet tone-sakura">
      <header class="sheet-head"><span class="sheet-icon">${icon("sparkle")}</span>
        <div><h2>${esc(copy.run)} <span>· ${esc(humanize(p.asset.split("/").pop()))}</span></h2><p>${p.kind === "voice" ? "The line is spoken by the voice you pick." : "Takes land under the asset; pick the winner by ear."}</p></div></header>
      <div class="sheet-model">
        <label class="model-pick">Generator<select id="auModel">${p.models.map((m) => `<option value="${esc(m.id)}" ${m.id === p.model ? "selected" : ""} ${m.available ? "" : "disabled"}>${esc(m.label)}${m.available ? "" : m.service ? " · not signed in" : " · no key"}</option>`).join("")}</select></label>
      </div>
      <div class="sheet-body"><section>
        <label class="field-label" for="pvPrompt">${p.kind === "voice" ? "Line to speak" : "Prompt"} <em>exactly what is sent</em></label>
        <textarea id="pvPrompt" class="final-prompt" rows="4">${esc(p.prompt)}</textarea>
      </section><section class="audio-opts">
        ${p.kind === "voice" ? `<label>Voice<select id="auVoice">${voices.map((v) => `<option value="${esc(v.id)}" ${v.id === s.voice_id ? "selected" : ""}>${esc(v.name)}${v.category ? ` · ${esc(v.category)}` : ""}</option>`).join("") || `<option value="">${model?.available ? "Loading voices failed" : "Sign in to ElevenLabs or add a key first"}</option>`}</select></label>` : ""}
        ${p.kind !== "voice" ? `<label>Length<select id="auDuration">${(p.kind === "music" ? [15, 30, 60, 90, 120, 180] : [0, 1, 2, 3, 5, 8, 12, 20, 30]).map((d) => `<option value="${d}" ${Number(s.duration) === d ? "selected" : ""}>${d ? `${d} s` : "Automatic"}</option>`).join("")}</select></label>
          <label class="mesh-toggle"><span class="switch"><input type="checkbox" id="auLoop" ${s.loop ? "checked" : ""}><span></span></span><span>Seamless loop<small>For ambience, engines, music beds</small></span></label>` : ""}
      </section></div>
      <footer class="sheet-foot">
        <label class="inline">${p.kind === "sfx" ? "Variations" : "Takes"} <select id="pvCount">${(p.kind === "sfx" ? [1, 2, 3, 4, 5, 6] : [1, 2, 3, 4]).map((n) => `<option ${n === p.count ? "selected" : ""}>${n}</option>`).join("")}</select></label>
        ${p.kind === "sfx" ? `<span class="cost">A pool of variations keeps repeated sounds (footsteps, hits) from sounding robotic</span>` : ""}
        <span class="grow"></span>
        <button type="button" class="ghost" data-close>Cancel</button><button type="button" class="run-btn" data-run>${icon("sparkle")}${esc(copy.run)}</button>
      </footer>
    </div>`);
}

function syncAudioFields() {
  const p = state.preview;
  if (!p?.audio) return;
  if ($("#auModel")) p.model = $("#auModel").value;
  if ($("#auVoice")) p.settings.voice_id = $("#auVoice").value;
  if ($("#auDuration")) p.settings.duration = Number($("#auDuration").value);
  if ($("#auLoop")) p.settings.loop = $("#auLoop").checked;
}

async function saveAssetPrompt() {
  const box = $("#assetPrompt");
  if (!box) return;
  await api("/api/studio/asset", { method: "PATCH", body: { path: box.dataset.asset, fields: { prompt: box.value.trim() } } });
}

async function openPreview(action, source = "", instruction = "") {
  try {
    await saveAssetPrompt();
    await Promise.all([studioOptions(), loadCredits()]);
    const req = await api("/api/studio/preview", { method: "POST", body: { path: state.view.path, action, source, instruction } });
    // A variant or edit starts from an existing image, so the style bible stays off unless switched on.
    if (action === "rig") { state.preview = req; return renderRigSheet(); }
    if (action === "animate") { state.preview = { ...req, picked: [], search: "" }; return renderAnimateSheet(); }
    if (req.audio) { state.preview = req; return renderAudioSheet(); }
    const bible = action === "variant" || action === "edit" || action === "views" ? false : req.use_bible !== false;
    state.preview = applyBible({ ...req, model3d: String(req.model || "").startsWith("svc:") ? req.model : "", use_bible: bible, instruction, references: req.references.map((r) => ({ ...r, on: true })) });
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
  trim_sheet: { tone: "amber", icon: "grid", blurb: "Pillars, arches and door frames from the level picture, on one sheet. They also hide the joins between walls." },
  prop_sheet: { tone: "amber", icon: "grid", blurb: "Every prop in the level picture, drawn once on one sheet. One image instead of one per prop; Glob cuts it up for free." },
  rig: { tone: "sky", icon: "bones", blurb: "Add a skeleton to a humanoid model so it can be animated. Includes walk and run clips." },
  animate: { tone: "violet", icon: "play", blurb: "Put preset animations on the rigged model: idle, attack, death and more." },
};
const PART_ROLE = { bible: "Style bible", subject: "Subject", style: "Style rule", rule: "Reference rule" };

async function studioOptions() {
  if (!state.studio) state.studio = await api("/api/studio/options").catch(() => ({ models: [], kinds: [], selected: "" }));
  if (!state.serviceModels) loadServiceModels();
  return state.studio;
}

// Models of the services the user signed in to. Asking each service takes a moment, so the sheet redraws when they arrive.
function loadServiceModels() {
  state.serviceModels = { image: [], "3d": [], loading: true };
  state.serviceModelsReady = api("/api/studio/service-models").catch(() => ({ image: [], "3d": [] })).then((models) => {
    state.serviceModels = models;
    if (state.preview && $("#dialog")?.open && $("#pvSource")) { syncPreviewFields(); renderPreview(); }
    return models;
  });
  return state.serviceModelsReady;
}

const ASPECTS = ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16"];
const ASPECT_NAME = { "1:1": "Square", "4:3": "Wide", "3:4": "Tall", "3:2": "Wide", "2:3": "Tall", "16:9": "Very wide", "9:16": "Very tall" };
const aspectOptions = (value) => [...new Set([value || "1:1", ...ASPECTS])].map((a) => `<option value="${esc(a)}" ${a === (value || "1:1") ? "selected" : ""}>${ASPECT_NAME[a] ? `${ASPECT_NAME[a]} · ` : ""}${esc(a)}</option>`).join("");

// Where a studio model runs: "keys" (Glob's providers, with your API keys) or a signed-in service's id.
const modelSource = (model) => (String(model || "").startsWith("svc:") ? model.slice(4).split("/")[0] : "keys");

function sourcePicker(p, kind) {
  const list = state.serviceModels?.[kind] || [];
  const services = [...new Map(list.map((m) => [m.service, m.label.split(" · ")[0]])).entries()];
  const current = modelSource(kind === "3d" ? p.model3d : p.model);
  // The default can be a signed-in service whose models haven't arrived yet: show it chosen, not "keys".
  if (current !== "keys" && !services.some(([id]) => id === current)) services.push([current, humanize(current)]);
  const keysLabel = kind === "3d" ? "Glob · Meshy / Tripo settings" : "Glob · your API keys";
  return `<label class="model-pick">Source <select id="pvSource">
    <option value="keys" ${current === "keys" ? "selected" : ""}>${keysLabel}</option>
    ${services.map(([id, label]) => `<option value="${esc(id)}" ${current === id ? "selected" : ""}>${esc(label)} · signed in (MCP)</option>`).join("")}
    ${state.serviceModels?.loading ? `<option disabled>Checking signed-in services…</option>` : ""}</select></label>`;
}

function serviceModelOptions(kind, service, selected) {
  const list = (state.serviceModels?.[kind] || []).filter((m) => m.service === service);
  const pending = selected && selected.startsWith(`svc:${service}/`) && !list.some((m) => m.id === selected)
    ? `<option value="${esc(selected)}" selected>${esc(humanize(selected.split("/").pop()))}${state.serviceModels?.loading ? " · loading…" : ""}</option>` : "";
  return pending + list.map((m) => `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""}>${esc(m.label.split(" · ").slice(1).join(" · "))}</option>`).join("");
}

// Each service's own recommended model, picked when the user switches to it.
const SERVICE_DEFAULT_MODEL = { "elevenlabs/image": "svc:elevenlabs/gpt-image-2.5-sunburst", "higgsfield/image": "svc:higgsfield/gpt_image_2_5", "higgsfield/3d": "svc:higgsfield/meshy_v7_image_to_3d" };

function onSourceChange(kind) {
  const p = state.preview;
  const source = $("#pvSource").value;
  const pick = source === "keys"
    ? (kind === "3d" ? "" : (state.studio.models.find((m) => m.id === state.studio.selected && m.available) || state.studio.models.find((m) => m.available) || {}).id || "")
    : (state.serviceModels[kind].find((m) => m.id === SERVICE_DEFAULT_MODEL[`${source}/${kind}`]) || state.serviceModels[kind].find((m) => m.service === source) || {}).id || "";
  if (kind === "3d") p.model3d = pick; else p.model = pick;
  renderPreview();
}

function renderPreview() {
  const p = state.preview;
  const theme = ACTION_THEME[p.action];
  const needsInstruction = p.action === "variant" || p.action === "edit" || p.action === "prop_sheet";
  const is3d = p.action === "make_3d";
  const models = state.studio?.models || [];
  const model = models.find((m) => m.id === p.model);
  const on = p.references.filter((r) => r.on).length;
  const source = modelSource(is3d ? p.model3d : p.model);
  const serviceLabel = (state.serviceModels?.[is3d ? "3d" : "image"] || []).find((m) => m.service === source)?.label.split(" · ")[0] || source;
  const cost = source !== "keys" ? ` · paid in your ${serviceLabel} credits`
    : is3d ? ` · ≈ $${(p.settings.should_texture ? 0.3 : 0.2).toFixed(2)} (${p.settings.should_texture ? 30 : 20} Meshy credits)` : model ? ` · ≈ $${(model.price * (p.count || 1)).toFixed(2)}` : "";
  const alert = creditAlert(source !== "keys" ? source : is3d ? "meshy" : (p.model || "").split(":")[0]);
  const balance = is3d && source === "keys" && state.credits?.meshy_balance != null ? ` · ${state.credits.meshy_balance} Meshy credits left` : "";
  // Not enough Meshy credits for this model: say so and don't let it start a run that will fail.
  const need = p.settings?.should_texture ? 30 : 20;
  const short = is3d && source === "keys" && state.credits?.meshy_balance != null && state.credits.meshy_balance < need;
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
      ${is3d ? `<div class="sheet-model">${sourcePicker(p, "3d")}
        ${source === "keys" ? "" : `<label class="model-pick">Model <select id="pvModel3d">${serviceModelOptions("3d", source, p.model3d)}</select></label>`}</div>` : `<div class="sheet-model">
        ${sourcePicker(p, "image")}
        <label class="model-pick">Model
          <select id="pvModel">${source === "keys" ? imageModelOptions(models, p.model) : serviceModelOptions("image", source, p.model)}
          ${model || source !== "keys" ? "" : `<option value="${esc(p.model)}" selected>${esc(p.model)}</option>`}</select></label>
        ${"aspect_ratio" in p.settings ? `<label class="model-pick">Aspect <select id="pvAspect">${aspectOptions(p.settings.aspect_ratio)}</select></label>` : ""}
        ${"transparent_background" in p.settings ? `<label class="mesh-toggle sheet-bg"><span class="switch"><input type="checkbox" id="pvTransparent" ${p.settings.transparent_background ? "checked" : ""}><span></span></span><span>Transparent background<small>${source === "keys" ? "Cut out by the generator" : `${esc(serviceLabel)} cuts it out after (a few extra credits)`}</small></span></label>` : ""}
        ${Object.entries(p.settings).filter(([k]) => k !== "aspect_ratio" && k !== "transparent_background").map(([k, v]) => `<span class="chip">${esc(k.replace(/_/g, " "))} <b>${esc(Array.isArray(v) ? v.join(", ") : String(v))}</b></span>`).join("")}
      </div>`}
      ${alert ? `<div class="credit-warn"><span class="bad-dot">!</span><span><b>${esc(humanize(alert.provider))} ran out of credits ${esc(timeAgo(alert.at).toLowerCase())}.</b> Top up first, or pick another Source above.</span></div>` : ""}
      ${needsInstruction ? `<div class="sheet-instruction"><label>${p.action === "prop_sheet" ? "Which props? (optional: leave empty for everything in the picture)" : "What should change?"}<textarea id="pvInstruction" rows="2" placeholder="${p.action === "variant" ? "Frost-themed armour, icy blue trim" : p.action === "prop_sheet" ? "barrel, torch, broken pillar, chest, bones" : "Remove the shield"}"></textarea></label>
        <button class="ghost" data-rebuild>Update prompt</button></div>` : ""}
      <div class="sheet-body${is3d ? " mesh-body" : ""}">
        ${is3d ? (source === "keys" ? meshOptions(p) : `<section class="mesh-opts"><p class="hint-line">${esc(serviceLabel)} makes the model from the ticked image${p.references.length > 1 ? "s" : ""}, with its own settings. The GLB lands here like any other output.</p></section>`) : `<section>
          ${p.action === "views" ? `<p class="hint-line">One image per view: ${esc(p.settings.views || "")}. <code>{view}</code> is replaced for each image.</p>` : bibleSwitch(p)}
          <label class="field-label" for="pvPrompt">Prompt <em>exactly what is sent</em></label>
          <textarea id="pvPrompt" class="final-prompt" rows="6">${esc(p.prompt)}</textarea>
          <details class="recipe-details"><summary>How the prompt is built</summary>
            <div class="recipe">${p.parts.map((part) => `<div class="recipe-part role-${esc(part.role || "subject")}"><span>${esc(PART_ROLE[part.role] || part.source)}<em>${esc(part.source.replace(/^[^·]*·\s*/, ""))}</em></span><p>${esc(part.text || "—")}</p></div>`).join("")}</div></details>
        </section>`}
        <section>
          ${p.picking && !is3d ? `<div class="field-label">Pick from this project</div>
          <div class="refs">${(p.pool || []).map((e) => `<button type="button" class="ref pick" data-pick-ref="${esc(e.path)}" title="${esc(e.title)}">
              <img src="${esc(fileUrl(e.path))}" alt="" loading="lazy"><span>${esc(e.title)}</span></button>`).join("") || `<p class="pillar-empty">No finished images in this project yet.</p>`}</div>
          <div class="row"><button type="button" class="ghost" data-upload-ref>Upload from computer</button><button type="button" class="ghost" data-close-picker>Cancel</button></div>` : `<div class="field-label">Images · ${on}${is3d ? (p.references.length > 1 ? " · several views make a better model" : "") : " of 8"}</div>
          <div class="refs">${refs || `<p class="pillar-empty">No reference images. Add pillars to keep the style consistent.</p>`}
            ${is3d ? "" : `<button type="button" class="ref add" data-add-ref>+ Add</button>`}</div>`}
        </section>
      </div>
      <footer class="sheet-foot">
        ${is3d ? "" : `<label class="inline">Images <select id="pvCount">${[1, 2, 3, 4].map((n) => `<option ${n === p.count ? "selected" : ""}>${n}</option>`).join("")}</select></label>`}
        <span class="cost">${on} image${on === 1 ? "" : "s"} attached${cost}${balance}${short ? `<b class="short">You need ${need - state.credits.meshy_balance} more Meshy credits. <a href="https://www.meshy.ai/settings/subscription" target="_blank" rel="noopener">Top up ↗</a></b>` : ""}</span>
        <span class="grow"></span>
        ${is3d ? "" : `<button type="button" class="ghost copy-req" data-copy-request title="Copy this prompt and the ticked images, to paste into ChatGPT, Gemini or another tool yourself (no API cost)">${icon("copy")}Copy prompt + images</button>`}
        <button type="button" class="ghost" data-close>Cancel</button><button type="button" class="run-btn" data-run ${short ? "disabled" : ""}>${icon(theme.icon)}${esc(ACTION_LABEL[p.action])}</button>
      </footer>
    </div>`);
  const instruction = $("#pvInstruction");
  if (instruction) instruction.value = p.instruction || "";
  $("#pvModel")?.addEventListener("change", () => { syncPreviewFields(); renderPreview(); });
  $("#pvModel3d")?.addEventListener("change", () => { syncPreviewFields(); renderPreview(); });
  $("#pvSource")?.addEventListener("change", () => { syncPreviewFields(); onSourceChange(is3d ? "3d" : "image"); });
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
  // An empty select means its models are still loading: keep the model the sheet opened with.
  if ($("#pvModel")?.value) p.model = $("#pvModel").value;
  if ($("#pvModel3d")?.value) p.model3d = $("#pvModel3d").value;
  if ($("#pvAspect")) p.settings.aspect_ratio = $("#pvAspect").value;
  if ($("#pvTransparent")) p.settings.transparent_background = $("#pvTransparent").checked;
  syncMesh();
  syncAudioFields();
}

async function rebuildPreview() {
  syncPreviewFields();
  const p = state.preview;
  const source = (p.references.find((r) => r.role === "source") || {}).path || "";
  const extras = p.references.filter((r) => r.role === "extra");
  try {
    const req = await api("/api/studio/preview", { method: "POST", body: { path: p.asset, action: p.action, source, instruction: p.instruction, model: p.model } });
    state.preview = applyBible({ ...req, model3d: p.model3d, instruction: p.instruction, count: p.count, use_bible: p.use_bible, showBible: p.showBible, references: [...req.references.map((r) => ({ ...r, on: true })), ...extras] });
    renderPreview();
  } catch (err) { toast(err.message, true); }
}

async function runPreview() {
  syncPreviewFields();
  if (!prepareGameRun()) return;
  const p = state.preview;
  if ((p.action === "variant" || p.action === "edit") && !String(p.instruction || "").trim()) { toast("Describe what should change, then press Update prompt.", true); return; }
  const request = { action: p.action, asset: p.asset, prompt: p.prompt, count: p.count, settings: p.settings, action_ids: p.action_ids, names: p.names, model: p.action === "make_3d" ? (p.model3d || "") : p.model, use_bible: p.use_bible !== false, references: p.references.filter((r) => r.on).map(({ path, source, role }) => ({ path, source, role })) };
  if (p.audio) {
    Object.assign(request, { audio: true, kind: p.kind });
    if (p.kind === "voice") {
      if (!p.settings.voice_id) { toast("Pick a voice first.", true); return; }
      const voice = (state.voices || []).find((v) => v.id === p.settings.voice_id);
      await api("/api/studio/asset", { method: "PATCH", body: { path: p.asset, fields: { voice: p.settings.voice_id, voice_name: voice?.name || "" } } }).catch(() => {});
    }
  }
  try {
    const job = await api("/api/studio/run", { method: "POST", body: { request } });
    if (!state.jobs[job.id]) state.jobs[job.id] = job; // the socket may already have reported it finished
    closeDialog();
    toast(`${p.audio ? AUDIO_COPY[p.kind].run : ACTION_LABEL[p.action]} started`);
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
    await renderView();
    if (state.lb) renderLightbox();
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
const PLAIN_VIEWS = ["home", "vault", "levels"];
const plainView = (v) => (PLAIN_VIEWS.includes(v.type) ? { type: v.type } : { type: v.type, path: v.path });

function saveView() {
  const hash = state.view.type === "home" ? "" : PLAIN_VIEWS.includes(state.view.type) ? `#${state.view.type}` : `#${state.view.type}=${encodeURIComponent(state.view.path)}`;
  try { history.replaceState(null, "", hash || location.pathname); } catch { /* ignore */ }
  storeSet(viewKey(), JSON.stringify({ view: plainView(state.view), trail: state.trail.slice(-30).map(plainView) }));
}

function restoreView() {
  if (location.hash === "#vault" || location.hash === "#levels") { state.view = { type: location.hash.slice(1) }; return; }
  const hash = location.hash.match(/^#(folder|doc)=(.+)$/);
  if (hash) {
    state.view = { type: hash[1], path: decodeURIComponent(hash[2]), restored: true };
    try { state.trail = (JSON.parse(storeGet(viewKey()) || "null")?.trail || []).filter((v) => v.type === "home" || typeof v.path === "string"); } catch { /* no trail */ }
    return;
  }
  try {
    const saved = JSON.parse(storeGet(viewKey()) || "null");
    const ok = (v) => v && (PLAIN_VIEWS.includes(v.type) || ((v.type === "folder" || v.type === "doc") && typeof v.path === "string"));
    if (!ok(saved?.view)) return;
    state.view = { ...plainView(saved.view), restored: true };
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
  // A dialog or the lightbox belongs to the page being left (also on browser Back or a pasted link).
  if (!same && $("#dialog").open) closeDialog();
  if (!same && state.lb) closeLightbox();
  if (!$("#projectsView").hidden && state.project) closeProjects();
  state.view = view;
  $("#content").scrollTop = 0;
  showPanel("workspace", true);
  renderView();
}

// A link pasted or edited in the address bar (#doc=…, #folder=…, #vault) opens that view.
window.addEventListener("hashchange", () => {
  const hash = location.hash.match(/^#(folder|doc)=(.+)$/);
  if (hash) go({ type: hash[1], path: decodeURIComponent(hash[2]) });
  else if (location.hash === "#vault" || location.hash === "#levels") go({ type: location.hash.slice(1) });
});

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

// Level kit: a picture of a level section, then one sheet with all its props, cut up on this computer into props.
// A 2D project (Settings → New art, or chosen when the project was made) hides the 3D tools; models already made still show.
const is2d = () => state.overview?.dimension === "2d";

function levelPanel(a) {
  const pack = state.pack;
  const items = pack?.items || [];
  const left = items.filter((i) => !i.done).length;
  const started = items.some((i) => i.done);
  const step = (n, done, title, text, button) => `<li class="${done ? "done" : ""}"><b>${done ? "✓" : n}</b><div><strong>${title}</strong><small>${text}</small></div>${button}</li>`;
  const label = started ? "Finish pack" : "Turn into asset pack";
  const kit = pack?.kit ? `<button class="mini" data-open="${esc(pack.kit)}">Open kit</button>` : "";
  const packText = pack?.running ? "Glob is making it now. You can leave this page."
    : !a.winner ? "Pick a level picture first."
    : is2d() ? "Every prop in the picture, cut out and named."
    : !started ? "Matching walls, floors, ceiling, pillars and props, all made from this picture, ready for modular or procedural levels."
    : left ? `${plural(items.length - left, "part")} of ${items.length} done. Finishing only makes what's missing.`
    : "Walls, floors, trim and props are made, and your coding agent can find them in ASSETS.md.";
  return `<div class="level-kit"><div class="field-label">${icon("map")} Level</div><ol>
    ${step(1, a.winner, "Picture the level", "Describe the place above, generate, and pick the one you like.", `<button class="mini" data-action="candidates">Generate</button>`)}
    ${step(2, started && !left, is2d() ? "Cut out its props" : "Make its asset pack", packText, pack?.running ? `<span class="spinner"></span>` : `<span class="pack-btns">${kit}${started && !left ? "" : `<button class="mini${started ? "" : " pack-go"}" data-level-pack="${esc(a.path)}" ${a.winner ? "" : "disabled"}>${label}</button>`}</span>`)}
  </ol>${pack?.note ? `<p class="pack-note">${esc(pack.note)}</p>` : ""}</div>`;
}

// Below the level: what the pack made, and the props and trim to tick for 3D, with the total before anything is spent.
function packPicks() {
  const pack = state.pack;
  if (!pack) return "";
  const ready = (pack.surfaces || []).filter((s) => s.image);
  const picks = pack.pickable || [];
  if (!ready.length && !picks.length) return "";
  const groups = {};
  for (const p of picks) (groups[p.group] ||= []).push(p);
  const wants3d = picks.some((p) => !p.model) && !is2d();
  return `${ready.length ? `<h2 class="block-title">Walls and floors <span class="block-count">${ready.length}</span></h2>
    <div class="pack-tiles">${ready.map((s) => `<button type="button" class="pack-tile" data-open="${esc(s.asset)}" title="Open ${esc(s.title)}"><img src="${esc(fileUrl(s.image))}" alt="" loading="lazy"><span>${esc(s.title)}</span></button>`).join("")}</div>` : ""}
  ${Object.entries(groups).map(([group, list]) => `<h2 class="block-title">${esc(group)} <span class="block-count">${list.length}</span></h2>
    <div class="pack-tiles">${list.map((p) => `<label class="pack-tile${p.model ? " has-3d" : ""}">
      <img src="${esc(fileUrl(p.image))}" alt="" loading="lazy"><span>${esc(p.title)}</span>
      ${p.model ? `<em>${icon("cube")}3D</em>` : is2d() ? "" : `<input type="checkbox" data-pack-pick="${esc(p.asset)}" aria-label="Make ${esc(p.title)} 3D">`}</label>`).join("")}</div>`).join("")}
  ${wants3d ? `<div class="pack-3d"><span id="packTotal">Tick the ones you want in 3D. Everything else stays a free cut-out.</span><button type="button" class="primary" id="packMake3d" disabled>Make 3D</button></div>` : ""}`;
}

function packDialog(path) {
  const items = state.pack?.items || [];
  const model = state.studio?.models?.find((m) => m.id === state.studio.selected);
  const row = (i) => `<label><input type="checkbox" name="item" value="${esc(i.id)}" ${i.done ? "disabled" : "checked"}><span>${esc(i.label)}</span><small>${i.done ? "Done" : esc(i.hint || "")}</small></label>`;
  const total = () => {
    const n = [...document.querySelectorAll("#packForm input[name=item]:checked")].length;
    const price = model?.price && !state.agentCfg?.demo_mode ? ` · ≈ $${(model.price * n).toFixed(2)}` : "";
    $("#packCost").textContent = `${plural(n, "image")}${price}. The cutting, tiling and kit building after that are free.`;
  };
  openDialog(`<form class="dlg" id="packForm">
    <h2>${icon("box")} ${items.some((i) => i.done) ? "Finish the asset pack" : "Turn into an asset pack"}</h2>
    <p>Glob redraws each surface from your level picture, flat-on and evenly lit, then builds pieces that always line up. They'll match the picture's look, but they won't be exact copies of it.</p>
    <div class="batch-list">${items.map(row).join("")}</div>
    <p class="st-note" id="packCost"></p>
    <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">${icon("sparkle")} Make pack</button></div></form>`);
  total();
  $("#packForm").addEventListener("change", total);
  $("#packForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const wanted = [...event.target.querySelectorAll("input[name=item]:checked")].map((i) => i.value);
    if (!wanted.length) return toast("Tick at least one thing to make.", true);
    try {
      await api("/api/studio/level-pack", { method: "POST", body: { asset: path, items: wanted } });
      closeDialog();
      state.dockOpen = true;
      renderJobDock();
      await renderView();
    } catch (err) { toast(err.message, true); }
  });
}

async function renderLevels() {
  const content = $("#content");
  let data;
  try { data = await api("/api/studio/levels"); } catch (err) { content.innerHTML = `<p class="hint-line">${esc(err.message)}</p>`; return; }
  state.levelFolder = data.folder;
  content.innerHTML = `
    <div class="kicker">Project / Levels</div>
    <div class="asset-head"><h1 class="title">Levels</h1></div>
    <p class="levels-lead">${is2d() ? "Describe a place, pick the picture you like, and Glob cuts out every prop in it." : "Describe a place, pick the picture you like, and Glob turns it into a matching asset pack: walls, floors, pillars and props that fit together, for hand-built or procedural levels."}</p>
    <div class="action-row"><button class="primary" data-new-level>${icon("sparkle")} New level</button></div>
    ${data.levels.length ? `<div class="level-cards">${data.levels.map((l) => `<button type="button" class="level-card" data-open="${esc(l.path)}">
      ${l.image ? `<img src="${esc(fileUrl(l.image))}" alt="" loading="lazy">` : `<span class="level-blank">${icon("map")}</span>`}
      <strong>${esc(l.title)}</strong><small>${!l.image ? "No picture yet" : l.kit ? `Asset pack${l.props ? ` · ${plural(l.props, "prop")}` : ""}` : l.props ? plural(l.props, "prop") : "Picture picked"}</small></button>`).join("")}</div>` : `<p class="hint-line levels-empty">Your levels will show up here. Make your first one to see how it works.</p>`}`;
}

function newLevelDialog() {
  openDialog(`<form class="dlg" id="newLevelForm">
    <h2>${icon("map")} New level</h2>
    <p>Describe one place in your game. You'll get a picture to approve before anything else is made.</p>
    <label>Name<input name="title" maxlength="80" placeholder="Cold storage plant" autofocus></label>
    <label>What's it like?<textarea name="prompt" rows="3" placeholder="An abandoned cold-storage plant: rusted pipes, frosted concrete walls, flickering strip lights, seen from eye level"></textarea></label>
    <p class="form-why" id="nlWhy" hidden>Give it a name first.</p>
    <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">Create</button></div></form>`);
  $("#newLevelForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    const name = String(data.title || "").trim();
    if (!name) { $("#nlWhy").hidden = false; return event.target.title.focus(); }
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "level";
    const path = `${state.levelFolder || "levels"}/${slug}.md`;
    const content = `---\nkind: "level"\nprompt: ${JSON.stringify(String(data.prompt || name).trim())}\n---\n\n# ${name}\n`;
    try {
      await api("/api/overview/file", { method: "PUT", body: { path, content } });
      closeDialog();
      await loadOverview();
      go({ type: "doc", path });
    } catch (err) { toast(err.message, true); }
  });
}

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-level-pack],[data-new-level],#packMake3d");
  if (!t || t.disabled) return;
  if (t.dataset.levelPack) {
    await studioOptions().catch(() => null);
    return packDialog(t.dataset.levelPack);
  }
  if (t.dataset.newLevel !== undefined) return newLevelDialog();
  const assets = [...document.querySelectorAll("[data-pack-pick]:checked")].map((i) => i.dataset.packPick);
  if (!assets.length) return;
  t.disabled = true;
  try {
    const res = await api("/api/studio/level-pack/make-3d", { method: "POST", body: { assets } });
    res.jobs.forEach((j) => { state.jobs[j.id] ||= j; });
    state.dockOpen = true;
    renderJobDock();
    toast(`Making ${plural(res.jobs.length, "3D model")}${res.skipped.length ? ` · ${res.skipped.length} skipped: ${res.skipped[0].reason}` : ""}`, Boolean(res.skipped.length));
    await renderView();
  } catch (err) { toast(err.message, true); t.disabled = false; }
});

document.addEventListener("change", (event) => {
  if (!event.target.matches?.("[data-pack-pick]")) return;
  const n = document.querySelectorAll("[data-pack-pick]:checked").length;
  const usd = (state.pack?.mesh_usd || 0.3) * n;
  $("#packTotal").textContent = n ? `${plural(n, "3D model")} · ≈ $${usd.toFixed(2)} at Glob's Meshy estimate` : "Tick the ones you want in 3D. Everything else stays a free cut-out.";
  $("#packMake3d").disabled = !n;
});

// A material: the picked image made seamless, with its maps, shown repeating so a visible seam or grid is easy to spot.
function materialPanel(a) {
  const albedo = (a.outputs.textures || []).find((o) => /-albedo\.png$/.test(o.path));
  const label = !albedo ? "Make tileable" : a.meta.material_source === a.winner ? "Make tileable again" : "Update from the new picture";
  const url = albedo ? `${fileUrl(albedo.path)}?v=${encodeURIComponent(a.meta.material_built || "")}` : "";
  return `<div class="level-kit mat-panel"><div class="field-label">${icon("material")} Tiling material</div>
    ${albedo ? `<div class="mat-tiles" role="img" aria-label="The texture repeated three times each way" style="background-image:url(&quot;${esc(url)}&quot;)"></div>` : `<p class="hint-line">${a.winner ? "Free, on this computer: makes the picture seamless and adds normal, roughness and height maps." : "Generate an image and pick it, then make it tile. That step is free."}</p>`}
    <div class="mat-actions"><button type="button" class="mini" data-material-build="${esc(a.path)}" ${a.winner ? "" : "disabled"}>${label}</button>
      <label>One tile covers <input type="number" data-kit-field="tile_m" data-kit-asset="${esc(a.path)}" min="0.1" max="50" step="0.1" value="${esc(a.meta.tile_m || 2)}"> metres</label></div></div>`;
}

// A modular kit: exact-size pieces Glob builds itself, painted with the project's materials.
function renderKitAsset(a, title) {
  const models = a.outputs["3d"] || [];
  const room = models.find((m) => /-sample-room\.glb$/.test(m.path));
  const m = a.meta;
  const field = (name, label, value, min, max, step) => `<label>${label}<span><input type="number" data-kit-field="${name}" data-kit-asset="${esc(a.path)}" min="${min}" max="${max}" step="${step}" value="${esc(m[name] ?? value)}"> m</span></label>`;
  const known = state.materials || [];
  const slot = (name, label) => `<label>${label}<select data-kit-field="kit_${name}" data-kit-asset="${esc(a.path)}"><option value="">Plain grey</option>
    ${known.map((x) => `<option value="${esc(x.path)}" ${x.path === m[`kit_${name}`] ? "selected" : ""} ${x.built ? "" : "disabled"}>${esc(x.title)}${x.built ? "" : " (press Make tileable on it first)"}</option>`).join("")}</select></label>`;
  return `
    <div class="kicker">Asset / ${esc(a.path)}</div>
    <div class="asset-head"><h1 class="title">${esc(title)}</h1><span class="kind-tag">modular kit</span></div>
    <div class="asset-top">
      <div class="winner-box${room ? " hero-3d" : ""}"${room ? ` data-hero-model="${esc(room.path)}"` : ""}>${room ? `
        ${room.thumb ? `<img class="hero-poster" src="${esc(fileUrl(room.thumb))}" alt="">` : ""}<div class="hero-stage"></div>
        <span class="out-badge mesh hero-badge">${icon("cube")}Sample room · drag to turn</span>
        <button type="button" class="mini hero-open" data-view-model="${esc(room.path)}">${icon("eye")}Open viewer</button>` : `<div class="no-thumb big"><p class="drop-copy">${kindArt(a.kind, "")}Nothing built yet.<br>Press <b>Build kit</b>: it's free and takes seconds.</p></div>`}</div>
      <div class="asset-side">
        <p class="hint-line">Floors, walls, doorways, corners and stairs at exact sizes, so they always line up. Glob builds them on this computer, and your coding agent gets a map of how they fit for procedural levels.</p>
        <div class="field-label">Grid</div>
        <div class="kit-form">${field("kit_cell", "Cell size", 2, 0.5, 20, 0.5)}${field("kit_height", "Wall height", 3, 0.5, 30, 0.5)}${field("kit_thickness", "Wall thickness", 0.2, 0.02, 2, 0.05)}${field("kit_relief", "Surface depth", 0.05, 0, 0.5, 0.01)}</div>
        <div class="field-label">Materials</div>
        <div class="kit-form">${slot("wall", "Walls")}${slot("wall_2", "Walls, variant")}${slot("wall_3", "Walls, variant 2")}${slot("floor", "Floors")}${slot("floor_2", "Floors, variant")}${slot("ceiling", "Ceilings")}</div>
        ${known.length ? "" : `<p class="hint-line">No materials yet. Make an asset of kind Material for each surface, or build in plain grey to try the layout first.</p>`}
        <div class="action-row"><button class="primary" data-kit-build="${esc(a.path)}">${room ? "Rebuild kit" : "Build kit"}</button>
          ${room ? `<a class="ghost btn-link" href="${esc(fileUrl(`${dirOf(room.path)}/kit.json`))}" download>${icon("download")}kit.json</a>` : ""}
          <button type="button" class="ghost" data-export="${esc(a.path)}">${icon("box")}Export</button></div>
      </div>
    </div>
    ${models.length ? `<h2 class="block-title" id="out-3d">3D models</h2>${outputGrid(models, "", "kit")}` : ""}
    <h2 class="block-title">Notes</h2>
    <div class="doc-bar"><span class="path"><bdi>${esc(a.path)}</bdi></span><button class="ghost" data-edit>Edit document</button></div>
    <div class="md">${renderMarkdown(a.body.replace(/^#\s+.+$/m, ""), dirOf(a.path))}</div>`;
}

// The hero viewer keeps its model for the same path, so a rebuilt file needs a new one.
function resetHero() {
  if (hero) { hero.view?.dispose(); hero.host.remove(); hero = null; }
}

async function buildKit(path, quiet = false) {
  await api("/api/studio/kit/build", { method: "POST", body: { asset: path } });
  resetHero();
  if (!quiet) toast("Kit built");
  await renderView();
}

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-material-build],[data-kit-build]");
  if (!t || t.disabled) return;
  t.disabled = true;
  try {
    if (t.dataset.kitBuild) return await buildKit(t.dataset.kitBuild);
    await api("/api/studio/material/build", { method: "POST", body: { asset: t.dataset.materialBuild } });
    toast("Material ready: seamless, with its maps");
    await renderView();
  } catch (err) { toast(err.message, true); t.disabled = false; }
});

// Settings save as they change; a kit already built follows them, so swapping a material reskins it.
document.addEventListener("change", async (event) => {
  const t = event.target.closest("[data-kit-field]");
  if (!t) return;
  const path = t.dataset.kitAsset;
  try {
    await api("/api/studio/asset", { method: "PATCH", body: { path, fields: { [t.dataset.kitField]: t.type === "number" ? Number(t.value) : t.value } } });
    if (t.dataset.kitField.startsWith("kit_") && $("#content [data-hero-model]")) await buildKit(path, true);
  } catch (err) { toast(err.message, true); }
});

function gameTools(a) {
  const glb = a.outputs["3d"].some((o) => /\.glb$/i.test(o.path));
  const rigged = Boolean(a.meta?.rig_task);
  const winner = esc(a.winner || "");
  const tool = (attrs, name, label, enabled, tip) => `<button type="button" class="tool" ${attrs}${enabled ? "" : " disabled"} title="${esc(tip)}">${icon(name)}${label}</button>`;
  const three = !is2d() || glb;
  if (a.kind === "material" || a.kind === "level") return `<div class="tool-row"><div class="field-label">Game tools</div><div class="tool-btns">${tool(`data-export="${esc(a.path)}"`, "box", "Export", true, "ZIP for Godot, Unity or Unreal")}</div></div>`;
  return `<div class="tool-row"><div class="field-label">Game tools</div><div class="tool-btns">
    ${three ? `${tool(`data-action="rig"`, "bones", "Rig", glb, glb ? "Add a skeleton to the GLB model. Works on textured humanoid characters facing forward" : "Make a GLB model, or drop one on this page, first")}
    ${tool(`data-action="animate"`, "play", "Animate", rigged, rigged ? "Preset animations for the rigged model" : "Rig the model first")}` : ""}
    ${tool(`data-icons="${winner}"`, "grid", "Icons", a.winner, a.winner ? "Square inventory icons from the winner" : "Pick a winner first")}
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

async function splitDialog(image, sheet = false) {
  state.split = { image, boxes: [], asAssets: sheet, sheet, note: "" };
  renderSplit();
  try {
    const { boxes } = await api(`/api/studio/detect-parts?path=${encodeURIComponent(image)}${sheet ? "&sheet=1" : ""}`);
    if (state.split?.image === image && !state.split.boxes.length) { state.split.boxes = boxes.map((b, i) => ({ ...b, name: "" })); renderSplit(); }
  } catch { /* draw by hand */ }
  if (sheet && state.split?.boxes.length) nameSheetParts();
}

async function nameSheetParts() {
  const sp = state.split;
  sp.busy = true;
  sp.note = `Found ${sp.boxes.length} props. Glob is naming them…`;
  renderSplit();
  try {
    const res = await api("/api/studio/name-parts", { method: "POST", body: { image: sp.image, boxes: sp.boxes } });
    if (state.split !== sp) return;
    syncSplitNames();
    sp.boxes.forEach((b, i) => { if (!b.name) b.name = res.names[i] || ""; });
    sp.note = res.source === "glob" ? `${sp.boxes.length} props, named by Glob. Fix a name or a box if one is off.` : `${res.note || "No names"}: name them yourself, or keep the numbers.`;
  } catch (err) { sp.note = err.message; }
  sp.busy = false;
  renderSplit();
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
    <h2>${icon("scissors")} ${sp.sheet ? "Cut into props" : "Split into parts"}</h2>
    <p>${sp.sheet ? "Each box becomes its own prop asset with the background removed, in a folder next to the level. Drag on the picture to add a box, click × to drop one." : "Drag on the picture to draw a box around each part, click × to remove one. <b>Separate figures</b> boxes each character on a sheet; <b>Body parts</b> asks Glob for the head, torso, arms and legs."}</p>
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
    <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button type="button" class="primary" data-split-run ${sp.boxes.length ? "" : "disabled"}>Cut ${sp.boxes.length || ""} ${sp.sheet ? "prop" : "part"}${sp.boxes.length === 1 ? "" : "s"}</button></div>
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
    if (sp.sheet && sp.asAssets && created.length) {
      toast(`✓ ${created.length} props made from ${humanize(state.view.path.split("/").pop().replace(/\.md$/, ""))}`);
      await loadOverview();
      return go({ type: "folder", path: dirOf(created[0]) });
    }
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

// A model's own files (glTF buffers, OBJ materials, textures) travel with it, so they stay next to it.
const MODEL_SIDECAR_RE = /\.(bin|mtl|png|jpe?g|webp|tga|bmp)$/i;

async function uploadModels(files) {
  const models = [...files].filter((f) => MODEL_RE.test(f.name));
  if (!models.length) return;
  const form = new FormData();
  [...files].filter((f) => MODEL_RE.test(f.name) || MODEL_SIDECAR_RE.test(f.name)).forEach((f) => form.append("files", f));
  try {
    const res = await fetch(`/api/studio/model-upload?project=${encodeURIComponent(state.project)}&path=${encodeURIComponent(state.view.path)}`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    toast(`Added ${data.saved.length} 3D model${data.saved.length === 1 ? "" : "s"}`);
    renderView();
  } catch (err) { toast(err.message, true); }
}

function pickModelFile() {
  const input = Object.assign(document.createElement("input"), { type: "file", accept: ".glb,.gltf,.obj,.fbx,.bin,.mtl,.png,.jpg,.jpeg,.webp,.tga,.bmp", multiple: true });
  input.onchange = () => uploadModels(input.files);
  input.click();
}

const viewerModule = () => import("./viewer.js");
const thumbTried = new Set();

// Models without a preview get one rendered in the browser, once per session.
async function queueThumbs(asset) {
  const missing = [...(asset.outputs["3d"] || []), ...(asset.outputs.rig || []), ...(asset.outputs.animations || []).filter((o) => /\.glb$/i.test(o.path))]
    .filter((o) => o.type === "model" && !o.thumb && !/\.gltf$/i.test(o.path) && !thumbTried.has(o.path));
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
    state.viewerAsset = state.view.type === "doc" ? await api(`/api/studio/asset?path=${encodeURIComponent(state.view.path)}`).catch(() => null) : null;
    state.viewer = { path, view, viewer, sheet: null };
  } catch (err) {
    $("#viewerStage").innerHTML = `<p class="hint-line">Could not open this model: ${esc(err.message)}</p>`;
    return;
  }
  const { stats, clips } = view;
  const clipOptions = (sel) => `<option value="-1" ${sel === -1 ? "selected" : ""}>No animation</option>${clips.map((c, i) => `<option value="${i}" ${sel === i ? "selected" : ""}>${esc(c)}</option>`).join("")}`;
  $("#viewerSide").innerHTML = `
    <div class="viewer-stats"><span><b>${stats.triangles.toLocaleString()}</b> triangles</span><span><b>${stats.meshes}</b> meshes</span><span><b>${stats.bones}</b> bones</span><span><b>${clips.length}</b> clips</span><span><b id="viewerBytes">…</b> file</span><span><b>${[stats.size.x, stats.size.y, stats.size.z].map((n) => n.toFixed(2)).join(" × ")}</b> units</span></div>
    ${state.view.type === "doc" ? `<label class="field-label" for="viewerHeight">Height in the game</label>
    <div class="height-row"><input id="viewerHeight" type="number" min="0.05" max="200" step="0.05" value="${esc(state.viewerAsset?.meta?.height_m || "")}" placeholder="${esc(String(KIND_HEIGHT[state.viewerAsset?.kind] || 1))}"><span>m</span><button type="button" class="ghost" data-height-save>Save</button><button type="button" class="link-btn" data-lineup>Compare in lineup</button></div>` : ""}
    <div class="field-label">Download as</div>
    <div class="fmt-links export-row"><a href="${esc(fileUrl(path))}" download>${esc(path.split(".").pop().toUpperCase())}</a>${["obj", "stl", "usdz"].filter((f) => !path.toLowerCase().endsWith(`.${f}`)).map((f) => `<button type="button" class="link-btn" data-viewer-export="${f}" title="Converted in your browser">${f.toUpperCase()}</button>`).join("")}</div>
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
  fetch(fileUrl(path), { method: "HEAD" }).then((r) => { const n = Number(r.headers.get("content-length")); if ($("#viewerBytes")) $("#viewerBytes").textContent = n ? fmtBytes(n) : "?"; }).catch(() => {});
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

// Outputs go to the workspace's .trash/ (models with every format and their preview), with Undo in the toast.
async function removeOutput(path, card) {
  const asset = state.view.path;
  card?.classList.add("leaving");
  try {
    const res = await api(`/api/studio/output?path=${encodeURIComponent(asset)}&output=${encodeURIComponent(path)}`, { method: "DELETE" });
    toast(`Moved ${path.split("/").pop()} to trash`, false, {
      label: "Undo",
      async run() {
        try {
          await api("/api/studio/output/restore", { method: "POST", body: { moves: res.removed } });
          if (res.was_winner) await api("/api/studio/asset", { method: "PATCH", body: { path: asset, fields: { winner: path } } });
          toast("Restored");
          renderView();
        } catch (err) { toast(err.message, true); }
      },
    });
    renderView();
  } catch (err) { card?.classList.remove("leaving"); toast(err.message, true); }
}

// Copy the exact prompt and the ticked reference images, to paste into a chat app or image tool by hand
// instead of paying for an API run. Text, HTML (prompt + images) and one PNG sheet of the images go on the
// clipboard together, so the target app picks what it understands.
async function copyRequest(btn) {
  syncPreviewFields();
  const p = state.preview;
  const refs = p.references.filter((r) => r.on);
  const text = p.prompt.trim();
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Copying…";
  // The ClipboardItem is made right away with promises for its parts: browsers (Safari above all) only allow
  // the write during the click itself, and the images are still loading at that point.
  const loaded = Promise.all(refs.map((r) => loadImage(fileUrl(r.path)).catch(() => null))).then((list) => list.filter(Boolean));
  const html = loaded.then((images) => new Blob([`<p>${esc(text).replace(/\n/g, "<br>")}</p>${images.map((img) => `<img src="${imageDataUrl(img, 1024)}">`).join("")}`], { type: "text/html" }));
  const item = { "text/plain": new Blob([text], { type: "text/plain" }) };
  if (refs.length) {
    item["text/html"] = html;
    item["image/png"] = loaded.then((images) => (images.length ? imageSheet(images) : Promise.reject(new Error("The images could not be loaded."))));
  }
  try {
    let count = 0;
    try {
      await navigator.clipboard.write([new ClipboardItem(item)]);
      count = (await loaded).length;
    } catch {
      await navigator.clipboard.writeText(text); // some browsers take only plain text
    }
    toast(count ? `Copied the prompt and ${count} image${count === 1 ? "" : "s"}` : refs.length ? "Copied the prompt only: this browser would not take the images" : "Copied the prompt");
  } catch (err) { toast(`Could not copy: ${err.message}`, true); }
  btn.disabled = false;
  btn.innerHTML = label;
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`Could not load ${src}`));
    img.src = src;
  });
}

function imageDataUrl(img, max) {
  const scale = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
  const canvas = Object.assign(document.createElement("canvas"), { width: Math.round(img.naturalWidth * scale), height: Math.round(img.naturalHeight * scale) });
  canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/png");
}

// Every reference side by side in one PNG, since the clipboard holds a single image.
function imageSheet(images) {
  const toBlob = (canvas) => new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  if (images.length === 1) {
    const img = images[0];
    const scale = Math.min(1, 1536 / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = Object.assign(document.createElement("canvas"), { width: Math.round(img.naturalWidth * scale), height: Math.round(img.naturalHeight * scale) });
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    return toBlob(canvas);
  }
  const cell = 768;
  const gap = 16;
  const cols = Math.min(images.length, 3);
  const rows = Math.ceil(images.length / cols);
  const canvas = Object.assign(document.createElement("canvas"), { width: cols * cell + (cols - 1) * gap, height: rows * cell + (rows - 1) * gap });
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  images.forEach((img, i) => {
    const scale = Math.min(cell / img.naturalWidth, cell / img.naturalHeight);
    const w = img.naturalWidth * scale;
    const h = img.naturalHeight * scale;
    ctx.drawImage(img, (i % cols) * (cell + gap) + (cell - w) / 2, Math.floor(i / cols) * (cell + gap) + (cell - h) / 2, w, h);
  });
  return toBlob(canvas);
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
  const t = event.target.closest("[data-remove-output],[data-copy-request],[data-dismiss-job],[data-view-model],[data-sprites],[data-split],[data-sheet-cut],[data-split-body],[data-split-remove],[data-split-auto],[data-split-run],[data-icons],[data-export],[data-anim],[data-viewer-thumb],[data-sprite-render],[data-sprite-save]");
  if (!t || t.disabled) return;
  if (t.dataset.removeOutput) { event.preventDefault(); event.stopPropagation(); return removeOutput(t.dataset.removeOutput, t.closest(".out, .file-chip-wrap")); }
  if (t.dataset.copyRequest !== undefined) return copyRequest(t);
  if (t.dataset.dismissJob) { storeSet("forge.dismissedJobs", JSON.stringify([...dismissedJobs(), t.dataset.dismissJob].slice(-100))); return renderView(); }
  if (t.dataset.viewModel) return viewModel(t.dataset.viewModel);
  if (t.dataset.sprites) return viewModel(t.dataset.sprites, true);
  if (t.dataset.split) return splitDialog(t.dataset.split);
  if (t.dataset.sheetCut) return splitDialog(t.dataset.sheetCut, true);
  if (t.dataset.splitRemove) { syncSplitNames(); state.split.boxes.splice(Number(t.dataset.splitRemove), 1); return renderSplit(); }
  if (t.dataset.splitAuto !== undefined) { state.split.boxes = []; return splitDialog(state.split.image); }
  if (t.dataset.splitRun !== undefined) return runSplit();
  if (t.dataset.splitBody !== undefined) return guessBodyParts();
  if (t.dataset.icons) return iconsDialog(t.dataset.icons);
  if (t.dataset.export !== undefined) return exportDialog(t.dataset.export);
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
    if (msg.type === "models_changed") loadModels().then(refreshModelViews);
    if (msg.type === "service_connected") loadServices().then(() => serviceConnected(msg.service));
    // A cover being painted for another project shows on its card as soon as it lands.
    if (msg.type === "studio_job" && msg.job.status === "done" && !$("#projectsView").hidden) openProjects();
    if (msg.type === "studio_job" && msg.job.status === "failed") state.credits = null; // may have been "out of credits"
    if (msg.type === "studio_job" && msg.project === state.project) {
      const known = state.jobs[msg.job.id];
      state.jobs[msg.job.id] = msg.job;
      const name = humanize((msg.job.asset || "").split("/").pop() || "");
      if (msg.job.status === "failed") toast(`${ACTION_LABEL[msg.job.action] || "Job"} failed${msg.job.batch ? ` (${name})` : ""}: ${msg.job.error}`, true);
      else if (msg.job.status === "done" && known?.status !== "done") toast(msg.job.batch ? `${name} is ready to pick` : `${ACTION_LABEL[msg.job.action] || "Job"} finished`);
      renderJobDock();
      if (state.view.path === msg.job.asset && !state.view.editing) renderView();
      else if (msg.job.status === "done" && ["board", "home"].includes(state.view.type)) renderView();
    }
    if (msg.type === "workspace_changed" && (!msg.project || msg.project === state.project)) { state.palette = null; state.board = null; }
    if (msg.type === "workspace_changed" && (!msg.project || msg.project === state.project) && !state.view.editing) {
      clearTimeout(pending);
      pending = setTimeout(loadOverview, 250);
    }
  };
  ws.onclose = () => setTimeout(connectSocket, 3000);
}

// ---------- events ----------

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-ob-cancel],[data-compact-now],[data-pick-winner-file],[data-scroll-candidates],[data-bible-toggle],[data-bible-view],[data-fav-model],[data-model-tab],[data-service-connect],[data-service-disconnect],[data-pick-model],[data-pick-effort],[data-close-settings],[data-settings-tab],[data-add-preset],[data-cancel-add],[data-probe],[data-save-endpoint],[data-pick-check],[data-test-model],[data-remove-endpoint],[data-cli-logout],[data-cli-login],[data-new-project],[data-delete-project],[data-open-project],[data-ob-skip],[data-ob-game],[data-ob-path],[data-ob-nogame],[data-ob-hasgame],[data-ob-home],[data-ob-home-reset],[data-ob-blank],[data-ob-restart],[data-ob-next],[data-ob-back],[data-ob-forge],[data-ob-enter],[data-ob-dim],[data-ob-chip],[data-make-pillar],[data-style-text],[data-remove-style],[data-remove-pillar],[data-toc],[data-upload],[data-new-asset],[data-new-folder],[data-to-folder],[data-make-asset],[data-action],[data-winner],[data-ref],[data-add-ref],[data-pick-ref],[data-upload-ref],[data-close-picker],[data-rebuild],[data-run],[data-open],[data-folder],[data-home],[data-vault],[data-levels],[data-vault-filter],[data-vault-zip],[data-vault-folder],[data-poly],[data-session],[data-delete],[data-suggest],[data-unattach],[data-settings],[data-close],[data-edit],[data-cancel-edit],[data-save],#newChatBtn,[data-show]");
  if (!t || event.target.closest("[data-st-export],[data-imp-use],[data-key-art]")) return;
  if (t.dataset.delete) { event.stopPropagation(); return deleteChat(t.dataset.delete); }
  if (t.dataset.upload) return uploadTo(t.dataset.upload);
  if (t.dataset.newAsset !== undefined) return newAssetDialog(t.dataset.newAsset);
  if (t.dataset.toc) { event.preventDefault(); return document.getElementById(t.dataset.toc)?.scrollIntoView({ behavior: "smooth", block: "start" }); }
  if (t.dataset.obCancel !== undefined) return cancelOnboarding();
  if (t.dataset.scrollCandidates !== undefined) return $("#out-candidates")?.scrollIntoView({ behavior: "smooth", block: "start" });
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
  if (t.dataset.serviceDisconnect) {
    event.stopImmediatePropagation();
    try { state.services = (await api(`/api/services/${t.dataset.serviceDisconnect}`, { method: "DELETE" })).services; toast("Signed out"); } catch (err) { toast(err.message, true); }
    return renderServiceCards();
  }
  if (t.dataset.serviceConnect) return connectService(t.dataset.serviceConnect);
  if (t.dataset.favModel) { event.stopImmediatePropagation(); return toggleFavorite(t.dataset.favModel); }
  if (t.dataset.modelTab) { event.stopImmediatePropagation(); state.modelTab = t.dataset.modelTab; return renderModelPop(); }
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
  if (t.dataset.cliLogout) {
    const id = t.dataset.cliLogout;
    const cli = state.modelInfo?.clis?.find((c) => c.id === id);
    delete (state.cliWait || {})[id];
    try { await api(`/api/agent/cli/${id}/logout`, { method: "POST" }); } catch (err) { return toast(err.message, true); }
    toast(`Logged out of ${cli?.label || "the coding agent"}. Click its card to log in again.`);
    await loadModels();
    return renderSettings();
  }
  if (t.dataset.cliLogin) {
    if (state.cliWait?.[t.dataset.cliLogin]) return;
    const cli = state.modelInfo?.clis?.find((c) => c.id === t.dataset.cliLogin);
    if (cli?.status === "ok") return toast(`${cli.label} is logged in and ready.`);
    cliLogin(t.dataset.cliLogin);
    return;
  }
  if (t.dataset.newProject !== undefined) return startOnboarding();
  if (t.dataset.obGame !== undefined) return chooseGameFolder();
  if (t.dataset.obHome !== undefined) return chooseHomeFolder();
  if (t.dataset.obHomeReset !== undefined) { syncOnboarding(); state.ob.home = ""; return renderOnboarding(); }
  if (t.dataset.obBlank !== undefined) return createBlank();
  if (t.dataset.obRestart !== undefined) { syncOnboarding(); state.ob.step = 0; return renderOnboarding(); }
  if (t.dataset.deleteProject) { event.stopPropagation(); return deleteProjectDialog(t.dataset.deleteProject); }
  if (t.dataset.openProject) { closeProjects(); return switchProject(t.dataset.openProject); }
  if (t.dataset.obSkip !== undefined) return skipOnboarding();
  if (t.dataset.obNext !== undefined) { syncOnboarding(); state.ob.step += 1; return renderOnboarding(); }
  if (t.dataset.obBack !== undefined) { syncOnboarding(); state.ob.step -= 1; return renderOnboarding(); }
  if (t.dataset.obForge !== undefined) return forgeWorld();
  if (t.dataset.obEnter !== undefined) return finishOnboarding();
  if (t.dataset.obPath !== undefined) {
    syncOnboarding();
    const ob = state.ob;
    if (!/^([a-z]:[\\/]|[\\/]|~)/i.test(ob.typed || "")) { toast("Paste the whole path, starting with the drive, like D:\\Games\\MyGame.", true); return $("#obGamePath")?.focus(); }
    ob.game = ob.typed;
    ob.noGame = false;
    if (!ob.name) ob.name = ob.game.split(/[\\/]/).filter(Boolean).pop().replace(/[-_]+/g, " ");
    return renderOnboarding();
  }
  if (t.dataset.obNogame !== undefined) { syncOnboarding(); Object.assign(state.ob, { noGame: true, game: "", home: "" }); return renderOnboarding(); }
  if (t.dataset.obHasgame !== undefined) { state.ob.noGame = false; return renderOnboarding(); }
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
    try {
      const vault = await api("/api/vault");
      const seen = new Set();
      p.pool = vault.groups.flatMap((g) => g.assets)
        .filter((e) => e.winner && !AUDIO_KINDS.includes(e.kind) && /\.(png|jpe?g|webp|gif)$/i.test(e.winner) && !seen.has(e.winner) && seen.add(e.winner))
        .map((e) => ({ path: e.winner, title: e.title || e.name }));
    } catch (err) { p.pool = []; }
    p.picking = true;
    return renderPreview();
  }
  if (t.dataset.closePicker !== undefined) { syncPreviewFields(); state.preview.picking = false; return renderPreview(); }
  if (t.dataset.pickRef) {
    syncPreviewFields();
    const p = state.preview;
    const existing = p.references.find((r) => r.path === t.dataset.pickRef);
    if (existing) existing.on = true;
    else p.references.push({ path: t.dataset.pickRef, source: "Project image", role: "extra", on: true });
    p.picking = false;
    return renderPreview();
  }
  if (t.dataset.uploadRef !== undefined) {
    syncPreviewFields();
    const p = state.preview;
    return uploadTo(`${p.asset.replace(/\.md$/i, "")}/refs`, (saved) => {
      p.references.push(...saved.map((path) => ({ path, source: "Added", role: "extra", on: true })));
      p.picking = false;
      renderPreview();
    });
  }
  if (t.dataset.rebuild !== undefined) return rebuildPreview();
  if (t.dataset.run !== undefined) return runPreview();
  if (t.dataset.open !== undefined) { event.preventDefault(); return openPath(t.dataset.open); }
  if (t.dataset.folder !== undefined) return go({ type: "folder", path: t.dataset.folder });
  if (t.dataset.home !== undefined) return go({ type: "home" });
  if (t.dataset.vault !== undefined) return go({ type: "vault" });
  if (t.dataset.levels !== undefined) return go({ type: "levels" });
  if (t.dataset.vaultFilter) { state.vaultHistory = !!$("#vaultHistory")?.checked; state.vaultFilter = t.dataset.vaultFilter; return renderVault(); }
  if (t.dataset.vaultZip !== undefined) return vaultZip(t.dataset.vaultZip);
  if (t.dataset.vaultFolder !== undefined) return vaultToFolder();
  if (t.dataset.poly) {
    syncPreviewFields();
    state.preview.settings.target_polycount = Number(t.dataset.poly);
    return renderPreview();
  }
  if (t.dataset.session) return openSession(t.dataset.session);
  if (t.id === "newChatBtn") return newChat();
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
$("#newProjectBtn").addEventListener("click", () => startOnboarding());
$("#projectSearch").addEventListener("input", renderProjectGrid);
$("#projectSort").addEventListener("change", renderProjectGrid);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || $("#onboarding").hidden || event.target.tagName === "TEXTAREA") return;
  if (state.ob?.step === OB_BLANK) { event.preventDefault(); return createBlank(); }
  if ($("#obName")) { event.preventDefault(); syncOnboarding(); state.ob.step = 3; renderOnboarding(); }
});
$("#settingsBtn").addEventListener("click", () => settingsDialog());
$("#refreshBtn").addEventListener("click", loadOverview);
$("#modelPill").addEventListener("click", (event) => { event.stopPropagation(); toggleModelPop(); });
$("#brandBtn").addEventListener("click", openProjects);
$("#input").addEventListener("input", autoGrow);
document.addEventListener("click", (event) => {
  if (!event.target.closest(".model-pill-wrap")) toggleModelPop(false);
});
$("#backBtn").addEventListener("click", goBack);
// The Glob chat is optional (Settings → Glob chat): "on", "off", or "auto", which hides it once a coding agent is
// connected to the project, unless the project already has Glob chats. The hide/show buttons override that per project.
function applyChatMode() {
  const shell = document.querySelector(".shell");
  const mode = state.agentCfg?.chat_mode || "auto";
  const manual = storeGet(`forge.chatHidden.${state.project}`);
  const chatted = (state.sessions || []).some((s) => s.message_count);
  const hidden = mode === "off" || (manual !== null && manual !== "" ? manual === "1" : mode === "auto" && Boolean(state.connect?.connected) && !chatted);
  shell.classList.toggle("chat-off", mode === "off");
  shell.classList.toggle("chat-hidden", hidden);
  if (hidden) showPanel("workspace");
}
function setChatHidden(hidden) {
  storeSet(`forge.chatHidden.${state.project}`, hidden ? "1" : "0");
  applyChatMode();
}
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-toggle-chat]")) setChatHidden(!document.querySelector(".shell").classList.contains("chat-hidden"));
});
// Drag images from the desktop onto an asset's winner box.
// Drag images or 3D models from the desktop onto an asset page: images become candidates (and the winner),
// models go to the asset's 3D section.
// Images dropped on the chat are attached to the next message; .zip files on the projects page are imported.
const dropZone = (event) => event.target.closest?.("[data-drop-winner]") || event.target.closest?.("[data-drop-asset]")
  || event.target.closest?.(".panel.chat") || event.target.closest?.("#projectsView");
const clearDrop = () => document.querySelectorAll("[data-drop-winner].over, [data-drop-asset].over, .panel.chat.over, #projectsView.over").forEach((z) => z.classList.remove("over"));
document.addEventListener("dragover", (event) => {
  if (!event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault();
  const zone = dropZone(event);
  event.dataTransfer.dropEffect = zone ? "copy" : "none";
  if (zone && !zone.classList.contains("over")) { clearDrop(); zone.classList.add("over"); }
});
document.addEventListener("dragleave", (event) => {
  if (event.relatedTarget && dropZone({ target: event.relatedTarget })) return;
  clearDrop();
});
document.addEventListener("drop", (event) => {
  if (!event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault();
  clearDrop();
  const zone = dropZone(event);
  if (!zone) return;
  const files = [...event.dataTransfer.files];
  if (zone.id === "projectsView") {
    const zip = files.find((f) => /\.zip$/i.test(f.name));
    return zip ? importArchive(zip) : toast("Drop a Glob project .zip here to import it.", true);
  }
  if (zone.classList.contains("panel")) {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (images.length) addFiles(images);
    if (images.length < files.length) toast(images.length ? "Only images can be attached; the other files were skipped." : "Only images can be attached to a message.", !images.length);
    return;
  }
  const sounds = files.filter((f) => AUDIO_RE.test(f.name));
  if (sounds.length) uploadAudio(sounds);
  // Images dropped together with a model are its textures, not new winners.
  if (files.some((f) => MODEL_RE.test(f.name))) return uploadModels(files);
  const images = files.filter((f) => !AUDIO_RE.test(f.name));
  if (images.length) addCandidateImages(images);
});
$("#settingsView").addEventListener("input", (event) => {
  if (event.target.id === "addFilter") { syncAddCard(); renderSettings().then(() => { const f = $("#addFilter"); f.focus(); f.setSelectionRange(f.value.length, f.value.length); }); }
});

// Windows and Linux press Ctrl, not ⌘.
if (!/Mac|iPhone|iPad/.test(navigator.platform)) {
  const btn = $("#paletteBtn");
  if (btn) { btn.title = "Jump anywhere (Ctrl+K)"; const kbd = btn.querySelector("kbd"); if (kbd) kbd.textContent = "Ctrl K"; }
}

(async function init() {
  try {
    state.agentCfg = await api("/api/config").catch(() => null);
    // First run: the setup covers the app at once, before anything behind it has a chance to show.
    if (state.agentCfg && !state.agentCfg.onboarded) openSetup();
    await loadProjects();
    restoreView();
    await loadModels();
    await loadGrants();
    await loadConnect();
    await loadJobs();
    renderJobDock();
    await Promise.all([loadSessions(), loadOverview()]);
    connectSocket();
    if (state.agentCfg?.auto_update_check !== false) checkForUpdate().then(renderUpdateBanner);
  } catch (err) {
    toast(`Could not start: ${err.message}`, true);
  }
})();

// ---------- folders: access grants, game import, storage, first-run setup ----------

const fmtBytes = (n) => (n >= 1073741824 ? `${(n / 1073741824).toFixed(1)} GB` : n >= 1048576 ? `${Math.round(n / 1048576)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const SHAPE_LABEL = { asset: "", kit: "kit", gallery: "images", audio: "audio", docs: "docs", data: "needs art" };

async function loadGrants() {
  try { state.grants = (await api("/api/access")).grants; } catch { state.grants = []; }
  return state.grants;
}

const isGranted = (path, write = false) => (state.grants || []).some((g) => {
  const root = g.path.toLowerCase().replace(/[\\/]+$/, "");
  const p = String(path).toLowerCase().replace(/[\\/]+$/, "");
  return (p === root || p.startsWith(`${root}\\`) || p.startsWith(`${root}/`)) && (g.write || !write);
});

async function chooseFolder(title) {
  try { return (await api("/api/system/choose-folder", { method: "POST", body: { title } })).folder_path || ""; }
  catch (err) { toast(err.message, true); return ""; }
}

async function grantFolder(path, write = false) {
  state.grants = (await api("/api/access", { method: "POST", body: { path, write } })).grants;
}

// The agent asked for a folder in the chat: Allow / Don't allow.
function accessCard(request, last) {
  const allowed = isGranted(request.path, request.write);
  const what = request.write ? "read and write" : "read";
  return `<div class="access-card${allowed ? " ok" : ""}">
    <div class="ac-head">${icon("folder")}<span>${allowed ? "Allowed" : `Glob asks to ${what} a folder`}</span></div>
    <code>${esc(request.path)}</code>
    ${request.reason ? `<p>${esc(request.reason)}</p>` : ""}
    ${!request.exists ? `<p class="ac-warn">This folder doesn't exist on this computer. Check the path.</p>` : ""}
    ${!allowed && last && request.exists ? `<div class="ac-actions"><button class="primary" data-access-allow="${esc(request.path)}" data-write="${request.write ? 1 : 0}">Allow</button>
      <button class="ghost" data-access-deny="${esc(request.path)}">Don't allow</button>
      <small>You can remove access any time in Settings → Storage & access.</small></div>` : ""}
  </div>`;
}

async function answerAccess(path, allow, write) {
  try {
    if (allow) await grantFolder(path, write);
    $("#input").value = allow
      ? `I allowed access to ${path}${write ? " (read and write)" : ""}. Go ahead.`
      : `Don't access ${path}.`;
    renderMessages();
    await send();
  } catch (err) { toast(err.message, true); }
}

// ----- importing a game project -----

function openImport(folder = "") {
  closeProjects();
  hideScene($("#onboarding"));
  state.imp = { step: "pick", folder, preview: null, target: "new", name: "", describe: state.models.some((m) => m.ready), log: [], phase: "", result: null };
  spawnPetals($("#importView"));
  showScene($("#importView"));
  renderImport();
  if (folder) scanImport();
}

function closeImport() { hideScene($("#importView")); state.imp = null; }

function unitStats(u) {
  return [
    u.models && plural(u.models, "model"), u.animations && plural(u.animations, "animation"), u.textures && plural(u.textures, "texture"),
    u.variants && plural(u.variants, "variant"), u.images && plural(u.images, "image"), u.audio && plural(u.audio, "sound"),
  ].filter(Boolean).join(" · ");
}

function renderImport() {
  const imp = state.imp;
  if (!imp) return;
  let body = "";
  if (imp.step === "pick") {
    const recent = (state.grants || []).filter((g) => g.exists).slice(-6).reverse();
    body = `<div class="ob-step enter"><p class="ob-kicker">Import a game</p><h1>Bring your <em>game</em> in.</h1>
      <p class="ob-sub">Godot, Unity, Unreal or any folder of game art. Glob finds the characters, models, animations, textures, variants, kits and design docs, copies them into a project and lays them out in the overview. Your game folder is never changed.</p>
      <div class="imp-pick"><input id="impPath" class="ob-input small" placeholder="D:\\Games\\MyGame" value="${esc(imp.folder)}" spellcheck="false">
        <button class="ob-back" data-imp-browse>Browse…</button><button class="ob-next" data-imp-scan>Scan</button></div>
      ${recent.length ? `<div class="imp-recent"><span>Folders Glob may use:</span>${recent.map((g) => `<button class="ob-chip" data-imp-use="${esc(g.path)}">${esc(g.path.split(/[\\/]/).pop() || g.path)}</button>`).join("")}</div>` : ""}
    </div>`;
  } else if (imp.step === "scanning") {
    body = `<div class="ob-step enter"><div class="ob-orb"><span></span><span></span><span></span></div><p class="ob-kicker">Scanning</p><h1>Looking through<br>your game…</h1><p class="ob-sub">${esc(imp.folder)}</p></div>`;
  } else if (imp.step === "preview") {
    const p = imp.preview;
    const c = p.counts;
    const tiles = [["Characters", c.characters], ["3D models", c.models], ["Animations", c.animations], ["Textures", c.textures], ["Images", c.images], ["Sounds", c.audio], ["Needs art", c.needs_art], ["Docs", c.docs]].filter(([, n]) => n);
    const current = humanize(state.project || "");
    body = `<div class="imp-preview${imp.shown === "preview" ? "" : " enter"}">
      <div class="imp-top"><div><p class="ob-kicker">${esc(p.engine_label)} project</p><h2>${esc(p.title)}</h2><code>${esc(p.folder)}</code></div>
        <button class="ob-back" data-imp-back>Choose another folder</button></div>
      <div class="imp-tiles">${tiles.map(([label, n]) => `<div><strong>${n}</strong><span>${label}</span></div>`).join("")}</div>
      <p class="imp-review">Review before anything is added: untick sections or click assets to leave them out, rename sections, or merge one into another.</p>
      <div class="imp-sections">${p.sections.map((s) => {
        const sp = imp.plan.sections[s.key] || {};
        const off = sp.include === false;
        const kept = s.units.filter((u) => !imp.plan.exclude.has(u.id)).length;
        return `<section class="${off ? "off" : ""}${sp.into ? " merged" : ""}"><h3>
          <label class="imp-sec-check"><input type="checkbox" data-imp-sec="${esc(s.key)}" ${off ? "" : "checked"}></label>
          <input class="imp-sec-title" data-imp-title="${esc(s.key)}" value="${esc(sp.title || s.title)}" ${off || sp.into ? "disabled" : ""} maxlength="60" aria-label="Section name">
          <small>${kept}/${s.units.length}</small>
          <select class="imp-merge" data-imp-into="${esc(s.key)}" ${off ? "disabled" : ""} title="Merge into another section"><option value="">Own section</option>${p.sections.filter((o) => o.key !== s.key).map((o) => `<option value="${esc(o.key)}" ${sp.into === o.key ? "selected" : ""}>→ ${esc((imp.plan.sections[o.key] || {}).title || o.title)}</option>`).join("")}</select></h3>
          <div class="imp-units">${s.units.map((u) => `
          <button type="button" class="imp-unit${u.shape === "data" ? " needs" : ""}${imp.plan.exclude.has(u.id) ? " out" : ""}" data-imp-unit="${esc(u.id)}" title="${esc(u.source || u.description || "")}${imp.plan.exclude.has(u.id) ? " · left out" : " · click to leave out"}"><strong>${esc(u.title)}</strong>
            <small>${esc(unitStats(u) || (u.description ? u.description.slice(0, 70) : ""))}</small>${SHAPE_LABEL[u.shape] ? `<em>${SHAPE_LABEL[u.shape]}</em>` : ""}</button>`).join("")}</div></section>`;
      }).join("")}</div>
      <div class="imp-options">
        <label class="imp-radio"><input type="radio" name="impTarget" value="new" ${imp.target === "new" ? "checked" : ""}><span>New project</span>
          <input id="impName" class="imp-name" value="${esc(imp.name || p.title)}" maxlength="60"></label>
        ${state.project ? `<label class="imp-radio"><input type="radio" name="impTarget" value="current" ${imp.target === "current" ? "checked" : ""}><span>Into <b>${esc(current)}</b> (merges with its sections and assets)</span></label>` : ""}
        <label class="imp-check"><input type="checkbox" id="impDescribe" ${imp.describe ? "checked" : ""}><span>Let Glob look at the art and write a prompt for each asset (uses your chat model)</span></label>
        <p class="imp-note">Adds ${c.files} files (${fmtBytes(c.bytes)}) to the project. On the same drive they're linked, not copied, so they take no extra space. ${p.skipped.archives ? `${p.skipped.archives} archives (.zip) are skipped. ` : ""}Glob's agent may read this folder afterwards; change that in Settings → Storage & access.</p>
      </div>
      <div class="ob-nav"><button class="ob-back" data-imp-close>Cancel</button><button class="ob-next forge" data-imp-run>✦ Import ${esc(p.title)}</button></div>
    </div>`;
  } else {
    const done = imp.step === "done";
    body = `<div class="ob-step enter">${done ? `<img class="ob-glob" src="/ui/glob-forge.svg" alt="">` : `<div class="ob-orb"><span></span><span></span><span></span></div>`}
      <p class="ob-kicker">${done ? "Imported" : esc(imp.phase || "Importing")}</p><h1>${esc((imp.target === "new" ? imp.name : humanize(imp.result?.project || state.project)) || imp.preview?.title || "Your game")}</h1>
      <ul class="ob-log">${imp.log.slice(-14).map((l) => `<li class="${l.cls || ""}">${esc(l.text)}</li>`).join("")}</ul>
      ${done ? `<div class="ob-nav"><button class="ob-next forge" data-imp-enter>Open the project →</button></div>` : ""}</div>`;
  }
  const scroll = $("#importStage .imp-sections")?.scrollTop || 0;
  $("#importStage").innerHTML = body;
  if (imp.shown === "preview" && imp.step === "preview") $("#importStage .imp-sections").scrollTop = scroll;
  imp.shown = imp.step;
}

function syncImport() {
  const imp = state.imp;
  if ($("#impPath")) imp.folder = $("#impPath").value.trim().replace(/^"|"$/g, "");
  if ($("#impName")) imp.name = $("#impName").value.trim();
  const target = document.querySelector("input[name=impTarget]:checked");
  if (target) imp.target = target.value;
  if ($("#impDescribe")) imp.describe = $("#impDescribe").checked;
  document.querySelectorAll("[data-imp-title]").forEach((el) => {
    const sp = (imp.plan.sections[el.dataset.impTitle] ||= {});
    const original = imp.preview?.sections.find((s) => s.key === el.dataset.impTitle)?.title;
    sp.title = el.value.trim() && el.value.trim() !== original ? el.value.trim() : "";
  });
}

// What the user approved in the preview, in the shape game_import.import_game expects.
function importPlan() {
  const imp = state.imp;
  const move = {};
  const sections = {};
  for (const s of imp.preview.sections) {
    const sp = imp.plan.sections[s.key] || {};
    sections[s.key] = { include: sp.include !== false, ...(sp.title ? { title: sp.title } : {}) };
    if (sp.into && sp.include !== false) for (const u of s.units) move[u.id] = sp.into;
  }
  return { exclude: [...imp.plan.exclude], move, sections };
}

async function scanImport() {
  const imp = state.imp;
  if (!imp.folder) { toast("Choose or paste a game folder first.", true); return; }
  imp.step = "scanning";
  renderImport();
  try {
    imp.preview = await api("/api/import/analyze", { method: "POST", body: { folder: imp.folder } });
    imp.name = imp.preview.title;
    imp.plan = { exclude: new Set(), sections: {} };
    imp.step = "preview";
  } catch (err) {
    imp.step = "pick";
    toast(err.message, true);
  }
  renderImport();
}

async function runImport() {
  syncImport();
  const imp = state.imp;
  const log = (text, cls = "") => { imp.log.push({ text, cls }); renderImport(); };
  imp.step = "running";
  imp.phase = "Copying files";
  renderImport();
  try {
    const body = { folder: imp.folder, title: imp.target === "new" ? imp.name : "", plan: importPlan() };
    if (imp.target === "current") body.project = state.project;
    else body.name = imp.name;
    let job = await api("/api/import/start", { method: "POST", body });
    const project = job.project;
    let seen = 0;
    while (job.status === "running") {
      await new Promise((r) => setTimeout(r, 600));
      job = await api(`/api/import/jobs/${job.id}`);
      for (const line of job.log.slice(seen)) log(line);
      seen = job.log.length;
    }
    if (job.status !== "done") throw new Error(job.error || "The import stopped.");
    const r = job.result;
    log(`${r.assets} assets in ${Object.keys(r.sections).length} sections · ${r.copied} files${r.linked ? ` (${r.linked} linked, no extra space)` : ""}`, "ok");
    imp.result = { project };
    await loadGrants();
    state.project = project;
    storeSet("forge.project", project);
    // Previews for the 3D models, rendered here in the browser.
    const { models } = await api(`/api/import/models?project=${encodeURIComponent(project)}`);
    if (models.length) {
      imp.phase = "Rendering previews";
      log(`Rendering previews for ${plural(models.length, "model")}…`);
      const viewer = await viewerModule().catch(() => null);
      let made = 0;
      for (const [i, model] of models.entries()) {
        try {
          const png = await viewer.renderThumb(projectFileUrl(project, model), 384);
          await api(`/api/studio/thumb?project=${encodeURIComponent(project)}`, { method: "POST", body: { model, png } });
          made += 1;
        } catch { /* keep the placeholder */ }
        if (i % 8 === 7 || i === models.length - 1) { imp.log.at(-1).text = `Rendering previews ${i + 1} / ${models.length}…`; renderImport(); }
      }
      log(`${made} previews rendered`, "ok");
    }
    if (imp.describe) {
      imp.phase = "Looking at the art";
      log("Glob is looking at your characters and items…");
      try {
        const { described } = await api(`/api/import/describe?project=${encodeURIComponent(project)}`, { method: "POST" });
        log(described ? `Wrote prompts for ${plural(described, "asset")}` : "The chat model couldn't look at images; prompts stay empty for now.", described ? "ok" : "");
      } catch (err) { log(`Couldn't describe the art: ${err.message}`, "bad"); }
    }
    imp.step = "done";
  } catch (err) {
    log(err.message, "bad");
    imp.step = "done";
  }
  renderImport();
}

async function finishImport() {
  const project = state.imp?.result?.project || state.project;
  closeImport();
  await switchProject(project);
  showPanel("workspace", true);
}

// ----- storage: where projects live, export and import -----

async function exportProject(project) {
  try {
    const result = await api(`/api/projects/${encodeURIComponent(project)}/export`, { method: "POST", body: {} });
    if (result.cancelled) return;
    toast(`Exported ${humanize(project)} to ${result.path} (${fmtBytes(result.size)})`);
  } catch (err) {
    if (/dialog/i.test(err.message)) { window.location.href = `/api/projects/${encodeURIComponent(project)}/export.zip`; return; }
    toast(err.message, true);
  }
}

async function importArchive(file) {
  const form = new FormData();
  if (file) form.append("file", file);
  toast(file ? `Importing ${file.name}…` : "Choose a Glob project .zip…");
  try {
    const res = await fetch("/api/projects/import-archive", { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Import failed (${res.status})`);
    if (data.cancelled) return;
    toast(`Imported ${humanize(data.project)}`);
    closeProjects();
    closeSettings();
    await switchProject(data.project);
  } catch (err) { toast(err.message, true); }
}

function pickArchive() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".zip,application/zip";
  input.onchange = () => input.files[0] && importArchive(input.files[0]);
  input.click();
}

async function moveProjectsFolder() {
  const folder = await chooseFolder("Choose where Glob keeps your projects");
  if (!folder) return;
  try {
    await api("/api/storage/location", { method: "POST", body: { path: folder } });
    toast(`Projects now live in ${folder}`);
    await loadProjects();
    await switchProject(state.projects.includes(state.project) ? state.project : state.projects[0] || "default");
    if (!$("#settingsView").hidden) renderSettings();
    if (state.setup) renderSetup();
  } catch (err) { toast(err.message, true); }
}

function grantRows(grants) {
  if (!grants.length) return `<p class="st-empty">No folders yet. Glob asks in the chat when it needs one, or add your game folder here.</p>`;
  return grants.map((g) => `<div class="grant-row${g.exists ? "" : " missing"}"><span class="dot ${g.exists ? "live" : ""}"></span><code title="${esc(g.path)}">${esc(g.path)}</code>
    <select data-grant-mode="${esc(g.path)}" aria-label="Access"><option value="read" ${g.write ? "" : "selected"}>Read</option><option value="write" ${g.write ? "selected" : ""}>Read & write</option></select>
    ${g.exists ? `<button class="mini" data-imp-use="${esc(g.path)}" title="Import this folder as a game">Import</button><button class="mini" data-st-open="grant" data-path="${esc(g.path)}">Open</button>` : `<small>missing</small>`}
    <button class="link-btn danger-text" data-grant-remove="${esc(g.path)}">Remove</button></div>`).join("");
}

// ----- first-run setup -----

const SETUP_STEPS = 4;
const AGENT_BUSY = new Set(["checking", "installing", "login"]);
const AGENT_STATUS = { missing: "Not installed · Install", idle: "Installed · Connect", checking: "Checking…", installing: "Finish installing in Terminal…", login: "Sign in in the Terminal window…", ok: "Connected", error: "Couldn't connect · Retry" };

async function openSetup() {
  state.setup = { step: 0, agents: {} };
  spawnPetals($("#setup"));
  showScene($("#setup"));
  // Setup counts as done when it is finished or skipped, not when it is seen: `glob connect` opens it in a
  // browser tab the user may close unread, and the next visit must still start with it.
  await renderSetup();
}

function stopAgentPolls() {
  for (const a of Object.values(state.setup?.agents || {})) clearTimeout(a.timer);
}

async function closeSetup(done = true) {
  stopAgentPolls();
  hideScene($("#setup"));
  state.setup = null;
  if (done && state.agentCfg) state.agentCfg.onboarded = true;
  if (done) await api("/api/config", { method: "PUT", body: { onboarded: true } }).catch(() => {});
}

// ----- setup: coding agents, plug and play -----

function agentCardsHTML() {
  const st = state.setup;
  return (state.modelInfo?.clis || []).map((c) => {
    const a = (st.agents[c.id] ||= { status: c.installed ? "idle" : "missing" });
    const mark = a.status === "ok" ? "✓" : AGENT_BUSY.has(a.status) ? `<i class="agent-spin"></i>` : "→";
    return `<button type="button" class="agent-card ${a.status}" data-setup-agent="${c.id}" title="${esc(a.error || (c.installed ? c.login : c.install))}">
      <span class="agent-logo"><img src="${AGENT_LOGO[c.id]}" alt=""></span>
      <span class="agent-text"><strong>${esc(c.label)}</strong><small>${AGENT_STATUS[a.status]}</small></span>
      <span class="agent-mark">${mark}</span></button>`;
  }).join("");
}

function renderAgentCards() {
  const box = $("#agentCards");
  if (!box) return;
  box.innerHTML = agentCardsHTML();
  const next = $("#setupStage [data-setup-next]");
  if (next) next.textContent = Object.values(state.setup.agents).some((a) => a.status === "ok") ? "Continue" : "Skip for now";
}

async function openAgentTerminal(id) {
  try { await api(`/api/agent/cli/${id}/login`, { method: "POST" }); } catch (err) { toast(err.message, true); }
}

// Checks whether an agent answers. `interactive` (a click) opens the Terminal to install it or sign in,
// then keeps checking until it's connected.
async function checkAgent(id, interactive = false) {
  const st = state.setup;
  const a = st?.agents[id];
  if (!a) return;
  clearTimeout(a.timer);
  const waiting = a.status === "installing" || a.status === "login";
  if (!waiting) a.status = "checking";
  renderAgentCards();
  let r;
  try { r = await api(`/api/agent/cli/${id}/check`, { method: "POST" }); } catch (err) { r = { installed: true, ok: false, error: err.message }; }
  if (state.setup !== st || st.step !== 1) return;
  a.error = r.error || "";
  if (r.ok) {
    a.status = "ok";
    renderAgentCards();
    return agentConnected(id);
  }
  if (!r.installed) {
    if (interactive && !waiting) await openAgentTerminal(id); // the install command
    a.status = interactive || a.status === "installing" ? "installing" : "missing";
  } else if (interactive || waiting) {
    if (a.status !== "login") await openAgentTerminal(id); // just installed, or a click: sign in
    a.status = "login";
  } else {
    a.status = "idle";
  }
  if (AGENT_BUSY.has(a.status)) {
    a.tries = (a.tries || 0) + 1;
    if (a.tries <= 45) a.timer = setTimeout(() => checkAgent(id), 4000);
    else { a.tries = 0; a.status = r.installed ? "error" : "missing"; }
  }
  renderAgentCards();
}

async function agentConnected(id) {
  playFistbump(AGENT_LOGO[id], state.modelInfo?.clis?.find((c) => c.id === id)?.label || "Your agent");
  await loadModels();
  const st = state.setup;
  // The first agent that connects runs the Glob chat, unless a model was already picked here.
  const pick = state.models.find((m) => m.id === `cli:${id}/`);
  if (st && pick && !st.pickedModel) { st.pickedModel = true; await pickModel(pick.id); }
}

function playFistbump(logo, label, box = $("#fistbump")) {
  if (!box) return toast(`${label} is connected`);
  box.innerHTML = `<div class="fb-row"><div class="fb-side left"><img class="fb-glob" src="/ui/glob-face.svg" alt="Glob"><span class="fb-fist">🤜</span></div>
    <div class="fb-burst">${Array.from({ length: 10 }, (_, i) => `<i style="--a:${i * 36}deg"></i>`).join("")}</div>
    <div class="fb-side right"><span class="fb-fist">🤛</span><span class="fb-logo"><img src="${logo}" alt="${esc(label)}"></span></div></div>
    <p class="fb-text"><b>${esc(label)}</b> and Glob are connected</p>`;
  box.hidden = false;
  box.classList.remove("play");
  void box.offsetWidth; // restart the animation
  box.classList.add("play");
}

async function renderSetup() {
  const st = state.setup;
  if (!st) return;
  const nav = (next = "Continue") => `<div class="ob-nav"><button class="ob-back" data-setup-back>Back</button><button class="ob-next" data-setup-next>${next}</button></div>`;
  const dots = `<div class="setup-dots">${Array.from({ length: SETUP_STEPS }, (_, i) => `<i class="${i === st.step ? "on" : i < st.step ? "done" : ""}"></i>`).join("")}</div>`;
  let body = "";
  if (st.step === 0) {
    body = `<div class="ob-step intro enter"><img class="ob-glob" src="/ui/glob-forge.svg" alt=""><p class="ob-kicker">Welcome to Glob</p><h1>Your game's<br><em>asset studio</em>.</h1>
      <p class="ob-sub">Glob makes the art, 3D models and sounds your game needs. Code with any agent you like: you shape the art here, and your agent copies it into the game. Two quick steps.</p>
      <button class="ob-next" data-setup-next>Start setup</button></div>`;
  } else if (st.step === 1) {
    await loadModels();
    const connected = Object.values(st.agents).some((a) => a.status === "ok");
    body = `<div class="ob-step enter"><p class="ob-kicker">1 / 2 · Your AI</p><h1>Plug in your<br><em>coding agent</em>.</h1>
      <p class="ob-sub">Glob runs on the AI you already pay for. Pick yours and sign in once: no API key needed.</p>
      <div class="agent-cards" id="agentCards">${agentCardsHTML()}</div>
      <div class="fistbump" id="fistbump" hidden></div>
      <p class="setup-alt">No coding agent? <button type="button" class="link-btn" data-setup-settings="models">Use an API key instead</button></p>
      ${nav(connected ? "Continue" : "Skip for now")}</div>`;
  } else if (st.step === 2) {
    const cfg = await api("/api/config");
    body = `<div class="ob-step enter"><p class="ob-kicker">2 / 2 · Generators</p><h1>Keys for art,<br><em>3D</em> and sound.</h1>
      <p class="ob-sub">Glob is free; generation runs on your own provider accounts, so you only pay them for what you make. Sign in to one, or paste a key. Everything stays on this computer.</p>
      ${(await loadServices(), serviceCardsHTML())}
      <div class="fistbump" id="serviceFistbump" hidden></div>
      <details class="setup-more"><summary>Or paste API keys</summary>
      <form id="setupKeys" class="setup-keys">${KEY_PROVIDERS.map((k) => `<label><span><b>${k.name}</b> ${k.use} · <a href="${k.url}" target="_blank" rel="noopener">Get a key ↗</a></span>
        <input name="${k.key}_api_key" type="password" autocomplete="off" placeholder="${cfg[`${k.key}_configured`] ? "Connected. Paste to replace" : k.hint}"></label>`).join("")}</form></details>
      ${nav((state.services || []).some((sv) => sv.connected) || KEY_PROVIDERS.some((k) => cfg[`${k.key}_configured`]) ? "Continue" : "Skip for now")}</div>`;
  } else {
    body = `<div class="ob-step enter"><img class="ob-glob" src="/ui/glob-forge.svg" alt=""><p class="ob-kicker">All set</p><h1>What are we<br><em>forging</em>?</h1>
      <div class="setup-actions big"><button class="ob-next forge" data-setup-finish="new">Start a new project</button><button class="ob-next" data-setup-finish="import">Import a game project</button>
      ${state.project ? `<button class="ob-back" data-setup-finish="open">Open ${esc(humanize(state.project))}</button>` : ""}</div>
      <p class="ob-sub small">Each project lives in its game's folder, in <code>glob/</code>.</p></div>`;
  }
  $("#setupStage").innerHTML = dots + body;
  $("#setup .ob-skip").hidden = st.step === SETUP_STEPS - 1; // the last step is the way out
  if (st.step === 1 && !st.checked) {
    st.checked = true; // installed agents that are already signed in connect on their own
    for (const c of state.modelInfo?.clis || []) if (c.installed) checkAgent(c.id);
  }
}

async function setupNext() {
  const st = state.setup;
  if (st.step === 2) {
    const form = $("#setupKeys");
    const updates = {};
    for (const input of form.querySelectorAll("input")) if (input.value.trim()) updates[input.name] = input.value.trim();
    if (Object.keys(updates).length) {
      try { await api("/api/config", { method: "PUT", body: updates }); toast("Keys saved"); } catch (err) { return toast(err.message, true); }
    }
  }
  stopAgentPolls();
  st.step = Math.min(SETUP_STEPS - 1, st.step + 1);
  await renderSetup();
}

// ----- events -----

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-access-allow],[data-access-deny],[data-imp-open],[data-imp-close],[data-imp-browse],[data-imp-scan],[data-imp-use],[data-imp-back],[data-imp-run],[data-imp-unit],[data-imp-enter],[data-st-open],[data-st-move],[data-st-export],[data-st-import-zip],[data-st-quit],[data-st-shortcut],[data-grant-add],[data-grant-remove],[data-setup-open],[data-setup-next],[data-setup-back],[data-setup-skip],[data-setup-finish],[data-setup-settings],[data-setup-refresh],[data-setup-agent]");
  if (!t) return;
  const d = t.dataset;
  if (d.accessAllow) return answerAccess(d.accessAllow, true, d.write === "1");
  if (d.accessDeny) return answerAccess(d.accessDeny, false, false);
  if (d.impOpen !== undefined) { closeSettings(); return openImport(); }
  if (d.impClose !== undefined) return closeImport();
  if (d.impBrowse !== undefined) {
    syncImport();
    const folder = await chooseFolder("Choose your game folder");
    if (folder) { state.imp.folder = folder; await scanImport(); }
    return;
  }
  if (d.impScan !== undefined) { syncImport(); return scanImport(); }
  if (d.impUse) {
    event.stopPropagation();
    if (!state.imp) { closeSettings(); if (state.setup) await closeSetup(); return openImport(d.impUse); }
    state.imp.folder = d.impUse;
    return scanImport();
  }
  if (d.impBack !== undefined) { state.imp.step = "pick"; return renderImport(); }
  if (d.impRun !== undefined) return runImport();
  if (d.impUnit) {
    syncImport();
    const ex = state.imp.plan.exclude;
    if (ex.has(d.impUnit)) ex.delete(d.impUnit); else ex.add(d.impUnit);
    return renderImport();
  }
  if (d.impEnter !== undefined) return finishImport();
  if (d.stOpen) {
    try { await api("/api/storage/open", { method: "POST", body: { target: d.stOpen, path: d.path || "" } }); } catch (err) { toast(err.message, true); }
    return;
  }
  if (d.stMove !== undefined) return moveProjectsFolder();
  if (d.stExport) { event.stopPropagation(); return exportProject(d.stExport); }
  if (d.stImportZip !== undefined) return pickArchive();
  if (d.stShortcut !== undefined) {
    try { const { shortcuts } = await api("/api/shortcut", { method: "POST" }); toast(`Glob shortcut added: ${shortcuts.join(", ")}`); if (state.settings?.tab === "about") renderSettings(); } catch (err) { toast(err.message, true); }
    return;
  }
  if (d.stQuit !== undefined) {
    if (!confirm("Stop Glob? Open it again any time with the Glob shortcut.")) return;
    try { await api("/api/quit", { method: "POST" }); } catch (err) { return toast(err.message, true); }
    document.body.innerHTML = `<div class="empty" style="padding:15vh 24px;text-align:center"><h1>Glob stopped</h1><p>Double-click the Glob shortcut on your desktop, ask your coding agent to open Glob, or run <code>glob open</code>, to start it again.</p></div>`;
    return;
  }
  if (d.grantAdd !== undefined) {
    const folder = await chooseFolder("Choose a folder Glob may use");
    if (!folder) return;
    try { await grantFolder(folder); toast(`Glob may now read ${folder}`); } catch (err) { return toast(err.message, true); }
    if (state.setup) return renderSetup();
    return renderSettings();
  }
  if (d.grantRemove) {
    try { state.grants = (await api("/api/access", { method: "DELETE", body: { path: d.grantRemove } })).grants; } catch (err) { return toast(err.message, true); }
    if (state.setup) return renderSetup();
    return renderSettings();
  }
  if (d.setupOpen !== undefined) { closeSettings(); return openSetup(); }
  if (d.setupNext !== undefined) return setupNext();
  if (d.setupBack !== undefined) { stopAgentPolls(); state.setup.step = Math.max(0, state.setup.step - 1); return renderSetup(); }
  if (d.setupAgent) {
    const a = state.setup?.agents[d.setupAgent];
    if (!a || AGENT_BUSY.has(a.status)) return;
    if (a.status === "ok") return agentConnected(d.setupAgent);
    return checkAgent(d.setupAgent, true);
  }
  if (d.setupSkip !== undefined) return closeSetup();
  if (d.setupRefresh !== undefined) return renderSetup();
  if (d.setupSettings) {
    hideScene($("#setup"));
    state.setupReturn = true;
    return settingsDialog(d.setupSettings);
  }
  if (d.setupFinish) {
    if (d.setupFinish === "new") {
      // Same backdrop: fade out setup's text, then the new-project questions take over in place.
      $("#setupStage").classList.add("stage-out");
      await new Promise((r) => setTimeout(r, 260));
      startOnboarding(true);
      await closeSetup();
      setTimeout(() => $("#setupStage").classList.remove("stage-out"), 900);
      return;
    }
    // Open the next scene first so it fades in over setup.
    if (d.setupFinish === "import") openImport();
    await closeSetup();
  }
});

document.addEventListener("change", (event) => {
  const el = event.target;
  if (!state.imp?.plan || !el.matches?.("[data-imp-sec], [data-imp-into]")) return;
  syncImport();
  if (el.dataset.impSec) (state.imp.plan.sections[el.dataset.impSec] ||= {}).include = el.checked;
  if (el.dataset.impInto) (state.imp.plan.sections[el.dataset.impInto] ||= {}).into = el.value;
  renderImport();
});

document.addEventListener("change", async (event) => {
  const select = event.target.closest?.("[data-grant-mode]");
  if (!select) return;
  try { await grantFolder(select.dataset.grantMode, select.value === "write"); toast(select.value === "write" ? "Glob may now read and write there" : "Read only"); } catch (err) { toast(err.message, true); }
});

// Asset page shortcuts: Ctrl/⌘+Enter in the prompt opens Generate 2D, and on a generation sheet it runs it.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
  if (event.target.id === "assetPrompt") { event.preventDefault(); openPreview("candidates"); }
  else if ($("#dialog").open && state.preview && $("#dialog [data-run]")) { event.preventDefault(); runPreview(); }
});

// The prompt saves itself while typing, so leaving the page never loses an edit.
let promptTimer;
document.addEventListener("input", (event) => {
  if (event.target.id !== "assetPrompt") return;
  clearTimeout(promptTimer);
  promptTimer = setTimeout(async () => {
    try {
      await saveAssetPrompt();
      const label = $("label[for=assetPrompt]");
      label?.classList.add("saved");
      setTimeout(() => label?.classList.remove("saved"), 1400);
    } catch (err) { toast(err.message, true); }
  }, 700);
});

// An image pasted on an asset page (e.g. one made by hand from "Copy prompt + images") becomes a candidate and the winner.
document.addEventListener("paste", (event) => {
  if (state.view?.type !== "doc" || !$("#content [data-drop-winner]") || $("#dialog").open) return;
  if (event.target.closest?.("input, textarea, [contenteditable]")) return;
  const files = [...(event.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (!files.length) return;
  event.preventDefault();
  addCandidateImages(files);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.id === "impPath") { event.preventDefault(); syncImport(); scanImport(); }
  if (event.key === "Escape" && state.imp && ["pick", "preview"].includes(state.imp.step)) closeImport();
  if (event.key === "Escape" && !$("#onboarding").hidden && !$("#onboarding").classList.contains("forging")) cancelOnboarding();
});

// Images pasted into the message box are attached like dropped ones.
$("#input").addEventListener("paste", (event) => {
  const files = [...(event.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (!files.length) return;
  event.preventDefault();
  addFiles(files);
});

// ---------- updates ----------

const INSTALL_KIND = { "windows-app": "Windows app", "mac-app": "macOS app", "uv-tool": "installed with the terminal one-liner", source: "running from source" };

async function checkForUpdate(force = false) {
  try { state.update = await api(`/api/update${force ? "?force=true" : ""}`); } catch (err) { state.update = { error: err.message }; }
  return state.update;
}

function renderUpdateBanner() {
  const u = state.update;
  const box = $("#updateBanner");
  const busy = ["downloading", "installing", "restart", "failed"].includes(u?.state?.status);
  if (!u || (!u.available && !busy) || (!busy && storeGet("forge.skipUpdate") === u.latest)) { box.hidden = true; return; }
  const s = u.state || {};
  let body;
  if (s.status === "downloading" || s.status === "installing") {
    body = `<span class="spinner"></span><span class="grow">${esc(s.message || "Updating…")}</span>${s.status === "downloading" && s.progress ? `<span class="ub-pct">${Math.round(s.progress * 100)}%</span>` : ""}`;
  } else if (s.status === "restart") {
    body = `<span class="ub-dot"></span><span class="grow">${esc(s.message)}</span>`;
  } else if (s.status === "failed") {
    body = `<span class="ub-dot bad"></span><span class="grow">${esc(s.error || "The update failed.")}</span><a class="mini" href="${esc(u.url || "#")}" target="_blank" rel="noopener">Download</a><button class="mini" data-update-dismiss>×</button>`;
  } else {
    const action = u.can_install
      ? `<button class="mini on" data-update-install>Update now</button>`
      : u.command ? `<code title="Run this in the Glob folder">${esc(u.command)}</code>` : `<a class="mini on" href="${esc(u.url)}" target="_blank" rel="noopener">Download</a>`;
    body = `<span class="ub-dot"></span><span class="grow"><b>Glob ${esc(u.latest)}</b> is out <small>(you have ${esc(u.current)})</small> · <a href="${esc(u.url)}" target="_blank" rel="noopener">What's new</a></span>${action}<button class="mini" data-update-dismiss title="Not now">Later</button>`;
  }
  box.innerHTML = body;
  box.hidden = false;
}

async function installUpdate() {
  try {
    state.update.state = await api("/api/update/install", { method: "POST" });
    renderUpdateBanner();
    if (!$("#settingsView").hidden && state.settings?.tab === "about") renderSettings();
    while (["downloading", "installing"].includes(state.update.state.status)) {
      await new Promise((r) => setTimeout(r, 800));
      try { state.update.state = await api("/api/update/state"); } catch { break; } // the app quit to install
      renderUpdateBanner();
    }
  } catch (err) { toast(err.message, true); }
}

function updateSettings() {
  const u = state.update || {};
  const s = u.state || {};
  const status = u.error ? esc(u.error)
    : ["downloading", "installing", "restart", "failed"].includes(s.status) ? esc(s.error || s.message)
    : u.available ? `Glob ${esc(u.latest)} is available.`
    : u.latest ? "You have the latest version." : "Not checked yet.";
  return `<div class="st-label">Updates</div>
    <div class="set-row"><div><strong>Glob ${esc(u.current || "")}</strong><small>${esc(INSTALL_KIND[u.kind] || "")} · ${status}</small></div>
      <div class="update-actions">${u.available && u.can_install ? `<button class="primary" data-update-install>Update to ${esc(u.latest)}</button>` : ""}<button class="ghost" data-update-check>Check now</button></div></div>
    ${u.available && u.command ? `<p class="st-note">To update this copy, run <code>${esc(u.command)}</code> in the Glob folder.</p>` : ""}
    <div class="set-row"><div><strong>Check for updates when Glob starts</strong><small>Asks GitHub for the latest release; nothing is installed without your click.</small></div>
      <label class="switch"><input type="checkbox" data-update-auto ${state.agentCfg?.auto_update_check === false ? "" : "checked"}><span></span></label></div>`;
}

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-update-install],[data-update-dismiss],[data-update-check]");
  if (!t) return;
  if (t.dataset.updateInstall !== undefined) return installUpdate();
  if (t.dataset.updateDismiss !== undefined) {
    if (state.update?.latest) storeSet("forge.skipUpdate", state.update.latest);
    if (state.update?.state?.status === "failed") state.update.state = {};
    $("#updateBanner").hidden = true;
    return;
  }
  if (t.dataset.updateCheck !== undefined) {
    t.disabled = true;
    await checkForUpdate(true);
    storeSet("forge.skipUpdate", "");
    renderUpdateBanner();
    return renderSettings();
  }
});

document.addEventListener("change", async (event) => {
  if (!event.target.matches?.("[data-update-auto]")) return;
  try {
    await api("/api/config", { method: "PUT", body: { auto_update_check: event.target.checked } });
    state.agentCfg = { ...(state.agentCfg || {}), auto_update_check: event.target.checked };
  } catch (err) { toast(err.message, true); }
});

// ---------- whole-project tools: board, batch runs, key art, sample, press kit ----------

const KIND_HEIGHT = { character: 1.7, creature: 2.5, vehicle: 2, prop: 1, item: 0.4, weapon: 1 };

// api() for another project than the open one: the URL is built before api() awaits anything.
function apiIn(project, path, options) {
  const open = state.project;
  state.project = project;
  try { return api(path, options); } finally { state.project = open; }
}

async function loadCredits() {
  if (state.credits && Date.now() - state.credits.loaded < 60000) return state.credits;
  state.credits = { alerts: {}, ...(await api("/api/studio/credits").catch(() => ({}))), loaded: Date.now() };
  return state.credits;
}

function creditAlert(provider) {
  const hit = provider && state.credits?.alerts?.[provider];
  return hit ? { ...hit, provider } : null;
}

async function renderBoard() {
  const content = $("#content");
  let data;
  try { data = await api("/api/board"); } catch (err) { content.innerHTML = `<div class="empty"><h1>Board unavailable</h1><p>${esc(err.message)}</p></div>`; return; }
  state.board = data;
  const all = data.groups.flatMap((g) => g.assets);
  const models = all.filter((a) => /\.(glb|gltf)$/i.test(a.model));
  const unpicked = all.filter((a) => !a.picked).length;
  content.innerHTML = `
    <div class="kicker">Project / ${esc(projectTitle())}</div>
    <h1 class="title">Board</h1>
    <p class="lede">Every asset on one canvas, to spot the ones that don't match. Drag to move; zoom with + and −, or ⌘ / Ctrl + scroll.</p>
    <div class="vault-bar">
      <div class="zoom-btns"><button class="icon-btn" data-board-zoom="-1" aria-label="Zoom out">−</button><span id="boardZoom">100%</span><button class="icon-btn" data-board-zoom="1" aria-label="Zoom in">+</button><button class="ghost" data-board-zoom="0">Fit</button></div>
      <span class="hint-line">${plural(all.length, "asset")}${unpicked ? ` · ${unpicked} without a winner` : ""}</span>
      <span class="grow"></span>
      ${models.length ? `<button class="ghost" data-lineup>${icon("cube")} 3D lineup · ${models.length}</button>` : ""}
      <button class="ghost" data-press-kit>${icon("download")} Press kit</button>
    </div>
    ${all.length ? `<div class="board" id="board"><div class="board-canvas" id="boardCanvas">${data.groups.map((g) => `
      <section class="board-col"><h2><small>${esc(g.number)}</small>${esc(g.title)}<span>${g.assets.length}</span></h2>
        ${g.assets.map((a) => `<button class="board-card${a.picked ? "" : " unpicked"}" data-open="${esc(a.path)}"${a.audio ? ` data-hover-audio="${esc(a.audio)}"` : ""} title="${esc(a.prompt)}">
          ${a.image ? `<img src="${esc(fileUrl(a.image))}" alt="" loading="lazy" draggable="false">` : a.audio ? `<span class="asset-sound"><span class="wave" aria-hidden="true">${"<i></i>".repeat(14)}</span><small>Hover to listen</small></span>` : kindArt(f.asset_kind)}
          <span class="board-label"><strong>${esc(a.title)}</strong><em>${esc(a.kind)}${a.model ? " · 3D" : ""}${a.picked ? "" : " · no winner"}</em></span></button>`).join("")}
      </section>`).join("")}</div></div>` : `<div class="empty"><h1>Nothing on the board yet</h1><p>Assets appear here as soon as they exist. Add some on a section page, or ask the agent.</p></div>`}`;
}

// Pan and zoom the board with a CSS transform; the position is kept while the project stays open.
function mountBoard() {
  const board = $("#board");
  const canvas = $("#boardCanvas");
  if (!board || !canvas) return;
  const first = !state.boardView;
  const v = state.boardView ||= { x: 0, y: 0, z: 1 };
  const apply = () => {
    canvas.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.z})`;
    if ($("#boardZoom")) $("#boardZoom").textContent = `${Math.round(v.z * 100)}%`;
  };
  const zoomAt = (factor, cx, cy) => {
    const z = Math.min(2.5, Math.max(0.2, v.z * factor));
    v.x = cx - ((cx - v.x) * z) / v.z;
    v.y = cy - ((cy - v.y) * z) / v.z;
    v.z = z;
    apply();
  };
  board.fit = (tries = 30) => {
    // Right after a render the canvas may not be laid out yet: wait a frame or two for real sizes.
    if (!canvas.scrollWidth || !board.clientWidth) { if (tries) requestAnimationFrame(() => board.fit(tries - 1)); return; }
    const scale = Math.min(1, board.clientWidth / canvas.scrollWidth, board.clientHeight / canvas.scrollHeight);
    Object.assign(v, { x: 0, y: 0, z: Math.max(0.2, scale) });
    apply();
  };
  board.zoomBy = (dir) => zoomAt(dir > 0 ? 1.25 : 0.8, board.clientWidth / 2, board.clientHeight / 2);
  board.addEventListener("wheel", (event) => {
    event.preventDefault();
    const box = board.getBoundingClientRect();
    if (event.ctrlKey || event.metaKey) zoomAt(Math.exp(-event.deltaY * 0.01), event.clientX - box.left, event.clientY - box.top);
    else { v.x -= event.deltaX; v.y -= event.deltaY; apply(); }
  }, { passive: false });
  board.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const drag = { x: event.clientX, y: event.clientY, ox: v.x, oy: v.y, moved: false };
    const move = (e) => {
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 5) return;
      drag.moved = true;
      board.classList.add("dragging");
      v.x = drag.ox + dx;
      v.y = drag.oy + dy;
      apply();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      board.classList.remove("dragging");
      // A drag that ends on a card must not open it.
      if (drag.moved) board.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); }, { capture: true, once: true });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up, { once: true });
  });
  if (first) board.fit(); // the first look shows every section at once
  else apply();
}

async function openLineup() {
  let data = state.board;
  if (!data) try { data = state.board = await api("/api/board"); } catch (err) { return toast(err.message, true); }
  const items = data.groups.flatMap((g) => g.assets).filter((a) => /\.(glb|gltf)$/i.test(a.model));
  if (!items.length) return toast("No 3D models in this project yet.", true);
  openDialog(`<div class="dlg wide viewer-dlg">
    <div class="viewer-head"><h2>${icon("cube")} Lineup · ${plural(items.length, "model")}</h2><button type="button" class="icon-btn" data-close aria-label="Close">✕</button></div>
    <div class="viewer-stage lineup-stage" id="lineupStage"><p class="hint-line">Loading models…</p></div>
    <p class="hint-line">Each model stands at its height in the game, next to a 1.7 m person (the grey figure). Set an asset's height in its 3D viewer.</p>
    <div class="lineup-legend">${items.map((a) => `<span class="chip">${esc(a.title)} <b>${esc(String(a.height_m))} m</b></span>`).join("")}</div></div>`);
  let view;
  $("#dialog").addEventListener("close", () => view?.dispose(), { once: true });
  try {
    const viewer = await viewerModule();
    const stage = $("#lineupStage");
    stage.innerHTML = "";
    view = await viewer.createLineup(stage, items.map((a) => ({ url: fileUrl(a.model), height: a.height_m, label: a.title })));
  } catch (err) { $("#lineupStage").innerHTML = `<p class="hint-line">Could not load the models: ${esc(err.message)}</p>`; }
}

async function batchDialog(folder) {
  let plan;
  try { [plan] = await Promise.all([api(`/api/studio/batch?folder=${encodeURIComponent(folder)}`), studioOptions()]); } catch (err) { return toast(err.message, true); }
  const where = folder ? humanize(folder.split("/").pop()) : "the project";
  if (!plan.assets.length) return toast(`Every asset in ${where} already has something to pick from.`);
  const images = plan.assets.filter((a) => !a.audio);
  const sounds = plan.assets.filter((a) => a.audio);
  const price = images.reduce((sum, a) => sum + (state.studio.models.find((m) => m.id === a.model)?.price || 0) * a.count, 0);
  const viaService = images.some((a) => String(a.model).startsWith("svc:"));
  const count = (list) => list.reduce((n, a) => n + a.count, 0);
  openDialog(`<form class="dlg" id="batchForm">
    <h2>${icon("sparkle")} Generate ${plural(plan.assets.length, "asset")}</h2>
    <p>Candidates for every asset in <b>${esc(where)}</b> that has nothing to pick from yet. Each uses its own prompt and pillars; two run at a time. Nothing is picked for you.</p>
    <div class="batch-list">${plan.assets.map((a) => `<label><input type="checkbox" name="asset" value="${esc(a.asset)}" checked><span>${esc(humanize(a.asset.split("/").pop()))}</span><small>${esc(dirOf(a.asset) || "root")} · ${a.count} ${a.audio ? "take" : "image"}${a.count === 1 ? "" : "s"}</small></label>`).join("")}</div>
    <p class="st-note">${[images.length ? `${plural(count(images), "image")}${state.agentCfg?.demo_mode ? "" : price ? ` · ≈ $${price.toFixed(2)}` : viaService ? " · paid in your signed-in service's credits" : ""}` : "", sounds.length ? plural(count(sounds), "sound") : ""].filter(Boolean).join(" · ")}${state.agentCfg?.demo_mode ? " · demo mode: placeholders, no cost" : ""}</p>
    <div class="row"><button type="button" class="ghost" data-close>Cancel</button><button class="primary">${icon("sparkle")} Generate</button></div></form>`);
  $("#batchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const assets = [...event.target.querySelectorAll("input[name=asset]:checked")].map((i) => i.value);
    if (!assets.length) return toast("Tick at least one asset.", true);
    event.submitter && (event.submitter.disabled = true);
    try {
      const res = await api("/api/studio/batch", { method: "POST", body: { assets } });
      res.jobs.forEach((j) => { state.jobs[j.id] ||= j; });
      closeDialog();
      state.dockOpen = true;
      renderJobDock();
      toast(`Generating ${plural(res.jobs.length, "asset")}${res.skipped.length ? ` · ${res.skipped.length} skipped: ${res.skipped[0].reason}` : ""}`, Boolean(res.skipped.length));
    } catch (err) { toast(err.message, true); event.submitter && (event.submitter.disabled = false); }
  });
}

// The key art is an ordinary asset: make it, open its page and its Generate sheet, so the prompt, model and
// framing are on screen before anything is spent. Its first winner becomes the cover.
// The front page's lineup: the project's assets with a picture, a few from each section, big enough to see.
function showcaseStrip(items) {
  if (items.length < 3) return "";
  return `<div class="showcase">${items.map((s) => `<button type="button" class="show-card" data-open="${esc(s.path)}" title="${esc(s.title)}">
    <span class="show-img"><img src="${esc(fileUrl(s.thumb))}" alt="" loading="lazy"></span>
    <span class="show-meta"><strong>${esc(s.title)}</strong><small>${esc(s.kind)}</small></span></button>`).join("")}</div>`;
}

// The first message that turns an empty game project into a workspace, for Glob's chat or a coding agent.
function gameBuildPrompt(game, agent = false) {
  return agent
    ? `Build the Glob workspace for this game (${game.title}) from its design docs, using Glob's tools. Don't generate any images yet.`
    : `Build the workspace for my game ${game.title}. Read its design docs in ${game.folder} (README, design notes), then make the front page with the pitch, one section per part of the game, pillars/style-bible.md from the art direction, and an asset page for every character, creature, item, place and sound it names. Don't generate anything yet.\n\nMy pitch, if you want it on top: `;
}

document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-game-build]") || !state.overview?.game) return;
  setChatHidden(false);
  showPanel("chat");
  const input = $("#input");
  input.value = gameBuildPrompt(state.overview.game);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
});

async function makeKeyArt(project, btn) {
  if (btn) btn.disabled = true;
  try {
    if (project !== state.project) await switchProject(project);
    const res = await api("/api/overview/key-art?start=false", { method: "POST" });
    go({ type: "doc", path: res.asset });
    await openPreview("candidates");
  } catch (err) { toast(err.message, true); } finally { if (btn) btn.disabled = false; }
}

async function installSample(btn) {
  if (btn) { btn.disabled = true; btn.classList.add("busy"); }
  try {
    const res = await api("/api/projects/sample", { method: "POST", body: {} });
    if (!$("#onboarding").hidden) hideScene($("#onboarding"));
    closeProjects();
    await switchProject(res.project);
    toast("Pocket Kaiju is yours to explore. Looking around costs nothing; generating spends credits as usual.");
  } catch (err) { toast(err.message, true); if (btn) { btn.disabled = false; btn.classList.remove("busy"); } }
}

function downloadPressKit() {
  const a = Object.assign(document.createElement("a"), { href: `/api/press-kit?project=${encodeURIComponent(state.project)}`, download: `${state.project}-press-kit.zip` });
  document.body.append(a);
  a.click();
  a.remove();
  toast("Press kit downloading: unzip and open index.html, or upload the folder to itch.io or any static host.");
}

// Live and waiting generation jobs, bottom right, on every page.
function renderJobDock() {
  let dock = $("#jobDock");
  if (!dock) { dock = Object.assign(document.createElement("div"), { id: "jobDock", className: "job-dock" }); document.body.append(dock); }
  const live = Object.values(state.jobs).filter((j) => j.status === "running" || j.status === "queued");
  dock.hidden = !live.length;
  if (!live.length) { state.dockOpen = false; return; }
  const running = live.filter((j) => j.status === "running").length;
  const waiting = live.length - running;
  dock.innerHTML = `<button type="button" class="dock-head" data-dock-toggle aria-expanded="${Boolean(state.dockOpen)}"><span class="spinner"></span>${running} running${waiting ? ` · ${waiting} waiting` : ""}<span class="caret${state.dockOpen ? " open" : ""}"></span></button>
    ${state.dockOpen ? `<ul>${live.map((j) => `<li><button type="button" class="link-btn" data-open="${esc(j.asset)}">${esc(humanize(String(j.asset).split("/").pop()))}</button>
      <small>${j.status === "queued" ? "waiting" : esc(ACTION_LABEL[j.action] || j.action)}</small>${j.status === "queued" ? `<button type="button" class="chip-del" data-job-cancel="${esc(j.id)}" title="Don't run this one" aria-label="Cancel">×</button>` : ""}</li>`).join("")}</ul>` : ""}`;
}

// ---------- candidate lightbox: arrows to step, Enter to pick, the prompt each image came from ----------

function openLightbox(path) {
  const list = [...document.querySelectorAll("#content [data-lightbox]")].map((a) => a.dataset.lightbox);
  if (!list.length) return;
  state.lb = { list: [...new Set(list)], index: 0, history: null, asset: state.view.path };
  state.lb.index = Math.max(0, state.lb.list.indexOf(path));
  renderLightbox();
  api(`/api/studio/history?path=${encodeURIComponent(state.view.path)}`).then((d) => { if (state.lb) { state.lb.history = d.history; renderLightbox(); } }).catch(() => {});
}

function renderLightbox() {
  const lb = state.lb;
  let box = $("#lightbox");
  if (!box) { box = Object.assign(document.createElement("div"), { id: "lightbox", className: "lightbox" }); box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true"); document.body.append(box); }
  if (!lb) { box.hidden = true; return; }
  const path = lb.list[lb.index];
  const winner = document.querySelector("#content .out.winner [data-lightbox]")?.dataset.lightbox === path;
  const made = lb.history?.[path];
  const model = made?.model ? String(made.model).replace(/^svc:/, "").split("/").pop() : "";
  box.hidden = false;
  box.innerHTML = `<button type="button" class="lb-close" data-lb-close aria-label="Close">✕</button>
    <button type="button" class="lb-nav prev" data-lb-step="-1" aria-label="Previous"${lb.index ? "" : " disabled"}>‹</button>
    <figure><img src="${esc(fileUrl(path))}" alt="">
      <figcaption>
        <span class="lb-count">${lb.index + 1} / ${lb.list.length}</span>
        ${winner ? `<span class="out-badge win static">${icon("star")}Winner</span>` : `<button type="button" class="primary" data-lb-pick>${icon("star")} Pick winner <kbd>↵</kbd></button>`}
        <span class="grow"></span>
        <a class="ghost btn-link" href="${esc(fileUrl(path))}" target="_blank" rel="noopener">Open full size</a>
      </figcaption>
      ${made ? `<details class="lb-prompt"><summary>${made.asset_prompt ? "Prompt it came from" : "Prompt sent"}${model ? ` · ${esc(model)}` : ""}${made.created_at ? ` · ${esc(timeAgo(made.created_at).toLowerCase())}` : ""}</summary>
        <p>${esc(made.asset_prompt || made.prompt || "")}</p>
        ${made.asset_prompt ? `<button type="button" class="ghost" data-use-prompt="${esc(path)}">Use this prompt again</button>` : ""}</details>` : ""}
    </figure>
    <button type="button" class="lb-nav next" data-lb-step="1" aria-label="Next"${lb.index < lb.list.length - 1 ? "" : " disabled"}>›</button>`;
}

function closeLightbox() { state.lb = null; renderLightbox(); }

async function usePrompt(path) {
  const made = state.lb?.history?.[path];
  if (!made?.asset_prompt) return;
  const box = $("#assetPrompt");
  const before = box?.value ?? "";
  try {
    await api("/api/studio/asset", { method: "PATCH", body: { path: state.view.path, fields: { prompt: made.asset_prompt } } });
    closeLightbox();
    await renderView();
    toast("Prompt brought back. Generate 2D to try it again.", false, { label: "Undo", run: async () => { await api("/api/studio/asset", { method: "PATCH", body: { path: state.view.path, fields: { prompt: before } } }); renderView(); } });
  } catch (err) { toast(err.message, true); }
}

// ---------- sound tools: waveform, selection, loop check, trimmed take ----------

let wave = null;

async function mountWave() {
  const host = $("#content [data-wave-src]");
  if (!host) { wave = null; return; }
  const src = host.dataset.waveSrc;
  const canvas = host.querySelector("canvas");
  if (wave?.src === src && wave.buffer) { drawWave(canvas); return; }
  wave = { src, buffer: null, sel: null };
  try {
    const ctx = wave.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const data = await (await fetch(fileUrl(src))).arrayBuffer();
    const buffer = await ctx.decodeAudioData(data);
    if (wave?.src !== src) return;
    wave.buffer = buffer;
    drawWave($("#content [data-wave-src] canvas"));
  } catch { /* the plain player still works */ }
}

function drawWave(canvas) {
  if (!canvas || !wave?.buffer) return;
  const w = canvas.width = canvas.clientWidth * devicePixelRatio;
  const h = canvas.height = 120 * devicePixelRatio;
  const g = canvas.getContext("2d");
  const data = wave.buffer.getChannelData(0);
  const step = Math.max(1, Math.floor(data.length / w));
  const css = getComputedStyle(canvas);
  g.clearRect(0, 0, w, h);
  if (wave.sel) {
    const [a, b] = wave.sel.map((t) => (t / wave.buffer.duration) * w);
    g.fillStyle = css.getPropertyValue("--wave-sel") || "rgba(255,126,182,.18)";
    g.fillRect(Math.min(a, b), 0, Math.abs(b - a), h);
  }
  g.fillStyle = css.getPropertyValue("--wave") || "#ff7eb6";
  for (let x = 0; x < w; x += 2) {
    let peak = 0;
    for (let i = x * step; i < (x + 1) * step && i < data.length; i += 4) peak = Math.max(peak, Math.abs(data[i]));
    const bar = Math.max(1, peak * h * 0.92);
    g.fillRect(x, (h - bar) / 2, 1.5 * devicePixelRatio, bar);
  }
  const host = canvas.closest("[data-wave-src]");
  const label = host?.querySelector(".wave-sel");
  const trim = host?.querySelector("[data-wave-trim]");
  const len = wave.sel ? Math.abs(wave.sel[1] - wave.sel[0]) : 0;
  if (label) { label.hidden = len < 0.05; label.textContent = wave.sel ? `${Math.min(...wave.sel).toFixed(2)}–${Math.max(...wave.sel).toFixed(2)} s` : ""; }
  if (trim) trim.disabled = len < 0.05;
}

function waveTime(event, canvas) {
  const box = canvas.getBoundingClientRect();
  return Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)) * wave.buffer.duration;
}

document.addEventListener("pointerdown", (event) => {
  const canvas = event.target.closest?.(".wave-canvas");
  if (!canvas || !wave?.buffer) return;
  const start = waveTime(event, canvas);
  wave.sel = [start, start];
  const move = (e) => { wave.sel[1] = waveTime(e, canvas); drawWave(canvas); };
  const up = (e) => {
    window.removeEventListener("pointermove", move);
    if (Math.abs(wave.sel[1] - wave.sel[0]) < 0.05) {
      // A click (no drag) seeks the player there.
      wave.sel = null;
      const audio = canvas.parentElement.querySelector("audio");
      if (audio) { audio.currentTime = waveTime(e, canvas); audio.play().catch(() => {}); }
    }
    drawWave(canvas);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up, { once: true });
});

function playLoopSeam() {
  if (!wave?.buffer) return toast("The sound is still loading.", true);
  const ctx = wave.ctx;
  ctx.resume?.();
  const d = wave.buffer.duration;
  const tail = Math.min(2, d / 2);
  const play = (offset, at, len) => { const n = ctx.createBufferSource(); n.buffer = wave.buffer; n.connect(ctx.destination); n.start(ctx.currentTime + at, offset, len); };
  play(d - tail, 0, tail);
  play(0, tail, tail);
  toast("Playing the end into the start: listen for a click or a jump at the seam.");
}

// 16-bit PCM WAV of a stretch of the decoded sound.
function wavBlob(buffer, from, to) {
  const rate = buffer.sampleRate;
  const a = Math.floor(from * rate);
  const frames = Math.max(1, Math.floor(to * rate) - a);
  const channels = buffer.numberOfChannels;
  const view = new DataView(new ArrayBuffer(44 + frames * channels * 2));
  const text = (o, str) => [...str].forEach((c, i) => view.setUint8(o + i, c.charCodeAt(0)));
  text(0, "RIFF"); view.setUint32(4, 36 + frames * channels * 2, true); text(8, "WAVEfmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, channels, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * channels * 2, true); view.setUint16(32, channels * 2, true); view.setUint16(34, 16, true);
  text(36, "data"); view.setUint32(40, frames * channels * 2, true);
  const data = [...Array(channels)].map((_, c) => buffer.getChannelData(c));
  let o = 44;
  for (let i = 0; i < frames; i += 1) for (let c = 0; c < channels; c += 1) { view.setInt16(o, Math.max(-1, Math.min(1, data[c][a + i])) * 0x7fff, true); o += 2; }
  return new Blob([view], { type: "audio/wav" });
}

async function saveTrim() {
  if (!wave?.buffer || !wave.sel) return;
  const [from, to] = [Math.min(...wave.sel), Math.max(...wave.sel)];
  const stem = wave.src.split("/").pop().replace(/\.[^.]+$/, "");
  await uploadAudio([new File([wavBlob(wave.buffer, from, to)], `${stem}-trim-${from.toFixed(1)}s.wav`, { type: "audio/wav" })]);
}

// ---------- hover previews: sounds play, models turn ----------

let hoverTimer = 0;
let hoverAudio = null;
let hoverModel = null;

function stopHover() {
  clearTimeout(hoverTimer);
  hoverAudio?.pause();
  hoverAudio = null;
  if (hoverModel) { hoverModel.view?.dispose(); hoverModel.host.remove(); hoverModel = null; }
}

// One sound at a time: pressing a tile or starting any player silences the hover preview
// (and any other player). Capture phase, because media "play" events don't bubble.
document.addEventListener("pointerdown", () => stopHover(), true);
document.addEventListener("click", () => stopHover(), true);
document.addEventListener("play", (event) => {
  if (event.target === hoverAudio) return;
  stopHover();
  document.querySelectorAll("audio").forEach((a) => { if (a !== event.target && !a.paused) a.pause(); });
}, true);

document.addEventListener("pointerover", (event) => {
  const tile = event.target.closest?.("[data-hover-audio],[data-hover-model]");
  if (!tile || tile.contains(event.relatedTarget)) return;
  stopHover();
  if (event.pointerType === "touch") return;
  hoverTimer = setTimeout(async () => {
    if (tile.dataset.hoverAudio) {
      hoverAudio = new Audio(fileUrl(tile.dataset.hoverAudio));
      hoverAudio.volume = 0.6;
      hoverAudio.play().catch(() => {});
      return;
    }
    const slot = tile.querySelector(".asset-img");
    const h = hoverModel = { host: Object.assign(document.createElement("div"), { className: "hover-stage" }), view: null };
    slot.append(h.host);
    try {
      const view = await (await viewerModule()).createViewer(h.host, fileUrl(tile.dataset.hoverModel), { autoRotate: true, grid: false });
      if (hoverModel !== h) return view.dispose();
      h.view = view;
      h.host.classList.add("live");
    } catch { /* the picture stays */ }
  }, tile.dataset.hoverAudio ? 250 : 450);
});

document.addEventListener("pointerout", (event) => {
  const tile = event.target.closest?.("[data-hover-audio],[data-hover-model]");
  if (tile && !tile.contains(event.relatedTarget)) stopHover();
});

// ---------- gallery mode and command palette ----------

function setGallery(on) {
  document.body.classList.toggle("gallery-mode", on);
  storeSet("forge.gallery", on ? "1" : "");
  const btn = $("#galleryBtn");
  if (btn) { btn.classList.toggle("on", on); btn.title = on ? "Leave gallery mode (G)" : "Gallery mode: dark, art first (G)"; }
}
setGallery(storeGet("forge.gallery") === "1");

async function paletteItems() {
  if (!state.palette) {
    const board = await api("/api/board").catch(() => ({ groups: [] }));
    state.palette = board.groups.flatMap((g) => g.assets.map((a) => ({ label: a.title, hint: `${g.title} · ${a.kind}`, icon: a.audio ? "play" : a.model ? "cube" : "star", run: () => go({ type: "doc", path: a.path }) })));
  }
  const ov = state.overview || { sections: [] };
  const folders = [];
  const walk = (nodes, trail) => nodes.forEach((n) => { folders.push({ label: n.title, hint: trail, icon: "folder", run: () => go({ type: "folder", path: n.path }) }); walk(n.children || [], `${trail} / ${n.title}`); });
  ov.sections.forEach((s) => { folders.push({ label: s.title, hint: `Section ${s.number}`, icon: "folder", run: () => go({ type: "folder", path: s.path }) }); walk(s.children || [], s.title); });
  const onAsset = state.view.type === "doc" && $("#content #assetPrompt");
  const here = state.view.type === "folder" ? state.view.path : "";
  const actions = [
    ...(onAsset ? [["Generate 2D", "sparkle", () => openPreview("candidates")], ...(is2d() ? [] : [["Make 3D", "cube", () => openPreview("make_3d")], ["Turnaround views", "turn", () => openPreview("views")], ["Add 3D model", "upload", pickModelFile]])] : []),
    ["Overview", "layers", () => go({ type: "home" })], ["Final vault", "box", () => go({ type: "vault" })], ["Levels", "map", () => go({ type: "levels" })],
    [here ? `Generate missing in ${humanize(here.split("/").pop())}` : "Generate all missing", "sparkle", () => batchDialog(here)],
    ...(here ? [["New asset here", "sparkle", () => newAssetDialog(here)]] : []),
    ...(is2d() ? [] : [["3D lineup", "cube", openLineup]]), ["Download press kit", "download", downloadPressKit],
    [document.body.classList.contains("gallery-mode") ? "Leave gallery mode" : "Gallery mode", "eye", () => setGallery(!document.body.classList.contains("gallery-mode"))],
    ["All projects", "grid", openProjects], ["New project", "sparkle", () => startOnboarding()], ["Settings", "edit", () => settingsDialog()],
  ].map(([label, ic, run]) => ({ label, hint: "Action", icon: ic, run }));
  return [...actions, ...folders, ...state.palette];
}

// Subsequence match: every typed letter in order; earlier, tighter matches score higher.
function fuzzy(query, text) {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (!q) return 1;
  const at = t.indexOf(q);
  if (at >= 0) return 100 - at;
  let i = 0;
  let gaps = 0;
  for (let j = 0; j < t.length && i < q.length; j += 1) { if (t[j] === q[i]) i += 1; else if (i) gaps += 1; }
  return i === q.length ? 50 - gaps : 0;
}

async function openPalette() {
  if ($("#palette")) return;
  const items = await paletteItems();
  const box = Object.assign(document.createElement("div"), { id: "palette", className: "palette" });
  box.innerHTML = `<div class="palette-card" role="dialog" aria-label="Go to"><input id="paletteInput" placeholder="Jump to an asset or folder, or run an action…" autocomplete="off" spellcheck="false"><ul id="paletteList" role="listbox"></ul><footer><kbd>↑</kbd><kbd>↓</kbd> move · <kbd>↵</kbd> open · <kbd>esc</kbd> close</footer></div>`;
  document.body.append(box);
  let shown = [];
  let index = 0;
  const draw = () => {
    const q = $("#paletteInput").value.trim();
    shown = items.map((it) => ({ it, score: Math.max(fuzzy(q, it.label), fuzzy(q, `${it.label} ${it.hint}`) - 5) })).filter((x) => x.score > 0).sort((a, b) => b.score - a.score).slice(0, 40).map((x) => x.it);
    index = Math.min(index, Math.max(0, shown.length - 1));
    $("#paletteList").innerHTML = shown.map((it, i) => `<li role="option" aria-selected="${i === index}" class="${i === index ? "on" : ""}" data-pal="${i}">${icon(it.icon)}<span>${esc(it.label)}</span><small>${esc(it.hint)}</small></li>`).join("") || `<li class="none">Nothing matches. Ask Glob in the chat, or make a new asset from the folder you're in.</li>`;
    $("#paletteList .on")?.scrollIntoView({ block: "nearest" });
  };
  const close = () => box.remove();
  const run = (it) => { close(); it?.run(); };
  box.addEventListener("click", (e) => { const li = e.target.closest("[data-pal]"); if (li) run(shown[Number(li.dataset.pal)]); else if (e.target === box) close(); });
  $("#paletteInput").addEventListener("input", () => { index = 0; draw(); });
  $("#paletteInput").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); index = Math.min(shown.length - 1, index + 1); draw(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); index = Math.max(0, index - 1); draw(); }
    else if (e.key === "Enter") { e.preventDefault(); run(shown[index]); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  draw();
  $("#paletteInput").focus();
}

// ---------- events for the tools above ----------

document.addEventListener("click", async (event) => {
  const t = event.target.closest("[data-board],[data-board-zoom],[data-lineup],[data-batch],[data-press-kit],[data-key-art],[data-install-sample],[data-job-cancel],[data-dock-toggle],[data-lightbox],[data-lb-close],[data-lb-step],[data-lb-pick],[data-use-prompt],[data-wave-loop],[data-wave-trim],[data-viewer-export],[data-height-save],#galleryBtn,#paletteBtn");
  if (!t) return;
  const d = t.dataset;
  if (d.lightbox) {
    if (event.metaKey || event.ctrlKey || event.shiftKey) return; // still opens in a new tab
    event.preventDefault();
    return openLightbox(d.lightbox);
  }
  if (d.board !== undefined) return go({ type: "board" });
  if (d.boardZoom !== undefined) { const b = $("#board"); return Number(d.boardZoom) ? b?.zoomBy(Number(d.boardZoom)) : b?.fit(); }
  if (d.lineup !== undefined) { if (t.closest("#dialog")) closeDialog(); return openLineup(); }
  if (d.batch !== undefined) return batchDialog(d.batch);
  if (d.pressKit !== undefined) return downloadPressKit();
  if (d.keyArt) { event.stopPropagation(); return makeKeyArt(d.keyArt, t); }
  if (d.installSample !== undefined) return installSample(t);
  if (d.dockToggle !== undefined) { state.dockOpen = !state.dockOpen; return renderJobDock(); }
  if (d.lbClose !== undefined) return closeLightbox();
  if (d.lbStep) { state.lb.index = Math.max(0, Math.min(state.lb.list.length - 1, state.lb.index + Number(d.lbStep))); return renderLightbox(); }
  if (d.lbPick !== undefined) return pickWinner(state.lb.list[state.lb.index]);
  if (d.usePrompt) return usePrompt(d.usePrompt);
  if (d.waveLoop !== undefined) return playLoopSeam();
  if (d.waveTrim !== undefined) return saveTrim();
  if (t.id === "galleryBtn") return setGallery(!document.body.classList.contains("gallery-mode"));
  if (t.id === "paletteBtn") return openPalette();
  try {
    if (d.jobCancel) {
      state.jobs[d.jobCancel] = await api(`/api/studio/jobs/${encodeURIComponent(d.jobCancel)}/cancel`, { method: "POST" });
      renderJobDock();
    }
    if (d.viewerExport) {
      const blob = await state.viewer.view.exportAs(d.viewerExport);
      const name = `${state.viewer.path.split("/").pop().replace(/\.[^.]+$/, "")}.${d.viewerExport}`;
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    }
    if (d.heightSave !== undefined) {
      const value = Number($("#viewerHeight").value);
      await api("/api/studio/asset", { method: "PATCH", body: { path: state.view.path, fields: { height_m: value > 0 ? value : "" } } });
      state.board = null;
      toast(value > 0 ? `Height set to ${value} m` : "Height reset to the default for its kind");
    }
  } catch (err) { toast(err.message, true); }
});

document.addEventListener("change", async (event) => {
  if (!event.target.matches?.("[data-pillar-strength]")) return;
  const value = event.target.value;
  try {
    await api("/api/studio/asset", { method: "PATCH", body: { path: state.view.path, fields: { pillars: value === "normal" ? "" : value } } });
    toast({ off: "Pillars off for this asset", light: "Pillars: palette only", normal: "Pillars back to normal", strong: "Pillars: match closely" }[value]);
  } catch (err) { toast(err.message, true); }
});

document.addEventListener("keydown", (event) => {
  const typing = event.target.closest?.("input, textarea, select, [contenteditable]");
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); return $("#palette") ? $("#palette").remove() : openPalette(); }
  if (state.lb) {
    if (event.key === "Escape") { event.preventDefault(); return closeLightbox(); }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); state.lb.index = Math.max(0, Math.min(state.lb.list.length - 1, state.lb.index + (event.key === "ArrowRight" ? 1 : -1))); return renderLightbox(); }
    if (event.key === "Enter" && !typing) { event.preventDefault(); return pickWinner(state.lb.list[state.lb.index]); }
    return;
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey || $("#dialog").open || $("#palette")) return;
  if (!$("#projectsView").hidden || !$("#settingsView").hidden || !$("#onboarding").hidden) return;
  if (event.key === "g" || event.key === "G") return setGallery(!document.body.classList.contains("gallery-mode"));
  // 1–9 on an asset page opens that candidate big.
  if (/^[1-9]$/.test(event.key) && state.view.type === "doc") {
    const list = [...document.querySelectorAll("#content [data-lightbox]")];
    const hit = list[Number(event.key) - 1];
    if (hit) { event.preventDefault(); openLightbox(hit.dataset.lightbox); }
  }
});
