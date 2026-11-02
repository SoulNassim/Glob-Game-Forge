"""Build the read-only live demo for the website (docs/demo/): the real Glob UI on the Pocket Kaiju sample.

The sample is installed into a throwaway Glob, every answer the UI reads is recorded into data.json, and
the UI is copied next to it with demo-shim.js in front: reads come from data.json, anything that would
change the project or spend credits says it's a demo instead.

    uv run python scripts/build_demo.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "demo"
UI = ROOT / "forge" / "ui"
PROJECT = "pocket_kaiju"

# Static answers the UI asks for at start or when a panel opens.
STATIC = [
    "/api/config", "/api/projects", "/api/projects/cards", "/api/overview", "/api/board", "/api/orders",
    "/api/agent/sessions", "/api/access", "/api/storage", "/api/connect", "/api/vault", "/api/studio/jobs",
    "/api/studio/options", "/api/studio/icons", "/api/studio/animations", "/api/budget", "/api/studio/credits",
    "/api/update/state", "/api/agent/endpoints", "/api/services", "/api/studio/service-models", "/api/studio/voices",
]

# A short scripted chat, so the left panel shows what talking to Glob looks like.
CHAT = [
    ("user", "Pocket Kaiju: a bite-sized 3D arcade brawler. You play Mochi, a pocket-sized pastel kaiju, defending a neon "
             "miniature Tokyo from clumsy tin-toy robots. Set the project up."),
    ("agent", "Done. The style bible asks for chunky vinyl-toy proportions, candy pastels with neon magenta and cyan, and a "
              "cel-shaded game look. I made sections for design, characters, locations, props, UI, sound and VFX, with first assets in each.",
     ["Wrote pillars/style-bible.md", "Created 02-characters/player/mochi.md", "Created 02-characters/invaders/tin-scout.md",
      "Created 06-sound/combat/mochi-roar.md"]),
    ("user", "Make the three tin robots and Mochi's roar."),
    ("agent", "Candidates are waiting on each robot page, and Mochi's roar is on its sound page. Pick a winner and I'll turn the robots into 3D.",
     ["Generated 02-characters/invaders/tin-scout.md", "Generated 02-characters/invaders/tin-brute.md",
      "Generated 02-characters/invaders/tin-titan.md", "Generated 06-sound/combat/mochi-roar.md"]),
]


def _key(path: str, params: dict[str, str]) -> str:
    """Same key demo-shim.js builds: the path plus its sorted query, without `project`."""
    query = urlencode(sorted((k, v) for k, v in params.items() if k != "project"))
    return f"{path}?{query}" if query else path


def record(client, ws: Path, scrub: list[str]) -> dict[str, dict]:
    from forge.blueprint_contract import split_frontmatter

    data: dict[str, dict] = {}

    def get(endpoint: str, **params: str) -> dict | None:
        res = client.get(endpoint, params={**params, "project": PROJECT})
        try:
            body = res.json()
        except ValueError:
            return None
        data[_key(endpoint, params)] = {"status": res.status_code, "body": body}
        return body

    for path in STATIC:
        get(path)
    for session in (get("/api/agent/sessions") or {}).get("sessions", []):
        get(f"/api/agent/sessions/{session['id']}")
    for folder in sorted(p for p in ws.rglob("*") if p.is_dir() and not p.name.startswith(".")):
        rel = folder.relative_to(ws).as_posix()
        if (folder / "README.md").is_file() or any(folder.glob("*.md")):
            get("/api/overview/folder", path=rel)
            get("/api/studio/batch", folder=rel)
    for doc in sorted(ws.rglob("*.md")):
        rel = doc.relative_to(ws).as_posix()
        get("/api/overview/file", path=rel)
        if split_frontmatter(doc.read_text(encoding="utf-8"))[0].get("kind"):
            get("/api/studio/asset", path=rel)
            get("/api/studio/history", path=rel)
            # The send sheet builds its prompt with a POST that changes nothing; the shim keys it like a GET.
            for action in ("candidates", "make_3d", "views"):
                res = client.post("/api/studio/preview", params={"project": PROJECT}, json={"path": rel, "action": action})
                if res.status_code == 200:
                    data[_key("/api/studio/preview", {"action": action, "path": rel})] = {"status": 200, "body": res.json()}
    # This Glob has no keys; show the generators as set up, the way a visitor's own Glob would (sending stays blocked).
    for model in [*data["/api/studio/options"]["body"]["models"], *data["/api/studio/options"]["body"]["audio_models"]]:
        model["available"] = True
    # Only the sample, not the empty "default" project every Glob starts with.
    data["/api/projects"]["body"]["projects"] = [PROJECT]
    data["/api/projects"]["body"]["default_project"] = PROJECT
    data["/api/projects/cards"]["body"]["projects"] = [c for c in data["/api/projects/cards"]["body"]["projects"] if c["project"] == PROJECT]
    # One chat model, so the composer looks connected; sending a message gets the read-only note.
    data["/api/agent/models"] = {"status": 200, "body": {"models": [{"id": "demo", "label": "Demo (read-only)", "ready": True, "provider": "demo"}], "selected": "demo"}}

    text = json.dumps(data, separators=(",", ":"))
    for secret in scrub:
        text = text.replace(json.dumps(secret)[1:-1], "~/Glob")
    return json.loads(text)


def copy_ui() -> None:
    (OUT / "ui").mkdir(parents=True)
    for item in UI.iterdir():
        if item.name == "index.html":
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, OUT / "ui" / item.name)
    app = (OUT / "ui" / "app.js").read_text(encoding="utf-8")
    # Pages live under /<repo>/demo/, so the app's root-absolute links become relative to the page.
    # Workspace files get an absolute path again (three.js resolves a relative model URL twice).
    files = '`${new URL("projects/", document.baseURI).pathname}${'
    fixed = app.replace('"/ui/', '"ui/').replace("`/projects/${", files)
    assert fixed.count(files) == 2, "fileUrl/projectFileUrl changed: update build_demo.py"
    (OUT / "ui" / "app.js").write_text(fixed, encoding="utf-8")
    html = (UI / "index.html").read_text(encoding="utf-8").replace('"/ui/', '"ui/')
    head = ('<script src="demo-shim.js"></script>\n  <link rel="stylesheet" href="demo.css" />\n'
            '  <meta name="robots" content="noindex" />\n</head>')
    banner = ('<body>\n  <div class="demo-pill" role="note"><b>Live demo</b><span>Read-only · nothing is generated</span>'
              '<a href="../#install">Download Glob</a></div>')
    # On a phone the demo opens on the project, not the chat.
    html = (html.replace('<div class="shell" data-active="chat">', '<div class="shell" data-active="workspace">')
            .replace('data-show="chat" class="active"', 'data-show="chat"').replace('data-show="workspace"', 'data-show="workspace" class="active"'))
    html = html.replace("<title>Glob</title>", "<title>Glob · Live demo</title>").replace("</head>", head, 1).replace("<body>", banner, 1)
    (OUT / "index.html").write_text(html, encoding="utf-8")
    for name in ("demo-shim.js", "demo.css"):
        shutil.copy2(ROOT / "scripts" / "demo" / name, OUT / name)


def main() -> None:
    for name in list(os.environ):
        if re.search(r"KEY|TOKEN|SECRET", name):
            os.environ.pop(name)
    tmp = Path(tempfile.mkdtemp(prefix="glob-demo-"))
    os.environ["FORGE_CONFIG_DIR"] = str(tmp / "config")
    os.environ["FORGE_PROJECTS_DIR"] = str(tmp / "projects")
    os.environ["FORGE_DEMO_MODE"] = "1"
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient

    from forge.server.app import app, workspace_agent

    client = TestClient(app, base_url="http://127.0.0.1:8420")
    client.put("/api/config", json={"onboarded": True, "auto_update_check": False})
    assert client.post("/api/projects/sample", json={}).json()["project"] == PROJECT
    ws = Path(client.get("/api/storage", params={"project": PROJECT}).json()["workspace_dir"])
    now = time.time() - 3600
    messages = [{"role": m[0], "text": m[1], "actions": [{"tool": "workspace_write", "summary": s} for s in (m[2] if len(m) > 2 else [])],
                 "at": now + i * 60} for i, m in enumerate(CHAT)]
    workspace_agent.create_session(PROJECT, title="Set up Pocket Kaiju", messages=messages)

    if OUT.exists():
        shutil.rmtree(OUT)
    data = record(client, ws, [str(tmp.resolve()), str(tmp), str(Path.home())])
    copy_ui()
    shutil.copytree(ws, OUT / "projects" / PROJECT / "workspace", ignore=shutil.ignore_patterns(".*"))
    (OUT / "data.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"{OUT}: {len(data)} answers, {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
