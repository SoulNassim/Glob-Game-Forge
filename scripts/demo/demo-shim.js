// Live demo: answers the Glob UI's reads from data.json (recorded by scripts/build_demo.py).
// Anything that would change the project, spend credits or reach the machine says it's a demo.
(() => {
  const PROJECT = "pocket_kaiju";
  const READ_ONLY = "This is the read-only demo. Download Glob to generate, edit and pick winners.";
  const realFetch = window.fetch.bind(window);
  const data = realFetch("data.json").then((r) => r.json());

  try { localStorage.setItem("forge.project", PROJECT); } catch { /* private mode */ }

  const key = (url) => {
    const u = new URL(url, "http://demo");
    const params = [...u.searchParams].filter(([k]) => k !== "project").sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    const query = new URLSearchParams(params).toString();
    return query ? `${u.pathname}?${query}` : u.pathname;
  };
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    if (!url.startsWith("/api/")) return realFetch(input, init);
    const method = (init.method || "GET").toUpperCase();
    const body = typeof init.body === "string" ? JSON.parse(init.body || "{}") : {};
    let lookup = key(url);
    // Reads that the app sends as writes: building the send sheet's prompt, and saving an unchanged prompt before it.
    if (method === "POST" && lookup === "/api/studio/preview") lookup = key(`/api/studio/preview?path=${encodeURIComponent(body.path)}&action=${body.action}`);
    else if (method === "PATCH" && lookup === "/api/studio/asset" && Object.keys(body.fields || {}).join() === "prompt") lookup = key(`/api/studio/asset?path=${encodeURIComponent(body.path)}`);
    else if (method !== "GET") return json({ detail: READ_ONLY }, 403);
    const hit = (await data)[lookup];
    if (!hit) return json({ detail: method === "GET" ? "Not part of the live demo." : READ_ONLY }, method === "GET" ? 404 : 403);
    return json(hit.body, hit.status);
  };

  // No server to stream job updates from.
  window.WebSocket = class {
    constructor() { this.readyState = 3; }
    send() {}
    close() {}
    addEventListener() {}
  };
})();
