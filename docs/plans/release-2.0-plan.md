# Glob 2.0 – Release-Plan

> **Ein Satz:** Glob ist dein Asset-Studio. Dein Coding-Agent bleibt, wo er ist – er bestellt, Glob liefert.

## Der Workflow, um den alles gebaut wird

1. Du codest dein Spiel mit *irgendeinem* Agenten (Claude Code, Codex, OpenCode, Cursor, Copilot …). Er benutzt Platzhalter.
2. Der Agent **bestellt** über das Glob-MCP („ich brauche einen Ork-Krieger für `res://units/orc.tscn`“).
3. Die Bestellung erscheint in Glob unter **Agent Orders** als *Entwurf*.
4. In einer zweiten Session desselben Agenten – mit dem **Glob-Prompt** – sagst du: „Sortier die Entwürfe ein.“ Glob legt sie sinnvoll in den Workspace (Charaktere/Blaue Fraktion, Items …).
5. Du generierst (2D → Gewinner → 3D/Audio). Sobald das Asset alles hat, was sein Typ braucht, springt die Bestellung **automatisch auf Ready**.
6. Ready-Bestellungen werden in einen **Lieferordner im Spiel-Repo** exportiert, mit Manifest. Der Coding-Agent findet seine Bestellung per ID und ersetzt den Platzhalter.

Einzige Grenzen: API-Guthaben und das Nutzungslimit des Agenten.

---

## Kernentscheidungen

### Glob ist ein Prompt, kein eigenes Programm
- **Eine** Prompt-Datei `forge/agent/glob_prompt.md` = Single Source of Truth.
- Ausgeliefert an drei Stellen: (a) als MCP-Prompt `glob`, (b) als Skill/AGENTS.md im Spiel-Repo („Agent verbinden“), (c) als System-Prompt des optionalen In-App-Chats.
- **Ein** Tool-Set: die MCP-Tools. Der externe Agent bekommt vollen Workspace-Zugriff (list/read/write/move/create_folder/generate/pick_winner …). Der In-App-Chat ruft dieselben Funktionen auf.
- In-App-Chat: **optional** (Einstellung „Glob-Chat anzeigen“), nutzt weiterhin beliebige Modelle (Anthropic, OpenAI, Copilot, Custom …).
- Die Coding-Agent-Tabs (Claude Code/Codex/OpenCode/Gemini im Chat) **entfallen**.

### Eine Bestellung = ein Order-Objekt
Heute gibt es drei parallele Systeme (ASSET_REQUIREMENTS.md, Blueprint-`agent_requests`, PipelineItems). Sie werden zu **einem** zusammengeführt:

`workspace/ORDERS.json` (Wahrheit) + automatisch gerendertes `ORDERS.md` (lesbar für Mensch und Agent).

| Feld | Bedeutung |
|---|---|
| `id` | kurz & stabil, z. B. `ord_orc_warrior` |
| `title`, `brief` | was gebraucht wird, Prompt-Idee |
| `kind` | character · prop · environment · ui · sprite · texture · sfx · voice · music |
| `placeholder` | Pfad im Spiel, den das Asset ersetzt (optional) |
| `needs` | Liefer-Profil, aus `kind` abgeleitet, pro Bestellung überschreibbar |
| `asset` | Pfad zum Workspace-Asset, sobald einsortiert |
| `status` | draft → filed → in_progress → ready → delivered |
| `requested_by` | z. B. `claude-code` |

**Ready-Regeln (Standard-Profile):**

| kind | ready, wenn … |
|---|---|
| ui, sprite, texture, card art | Gewinner-Bild existiert |
| prop | Gewinner + 3D-Modell |
| character | Gewinner + 3D-Modell (+ Rig, wenn `needs` es verlangt) |
| environment | Gewinner (+ 3D, wenn verlangt) |
| sfx, voice, music | Audiodatei als Gewinner gewählt |

Status wird bei jeder Workspace-Änderung neu berechnet → UI aktualisiert live.

### Lieferung ins Spiel
- Pro Projekt **ein** Spiel-Ordner (ersetzt die drei heutigen Verknüpfungen).
- Lieferung nach `<spiel>/glob_delivery/<order_id>/…` + `glob_delivery/manifest.json` (Order-ID → Dateien, Platzhalter, SHA-256, Format).
- Schreibt **nur** in `glob_delivery/` → kann nichts im Spiel kaputt machen, daher ohne Rückfrage. Option „automatisch liefern, sobald ready“ (Standard: an).
- MCP-Tool `get_deliveries` für den Agenten.

---

## Phasen

### Phase 1 – Fundament & Aufräumen
- `glob_prompt.md` + MCP-Prompt `glob`; In-App-Chat nutzt ihn.
- MCP: `--project <name>`, Projektordner unabhängig vom Arbeitsverzeichnis (Bug: heute landet alles in `default` bzw. im falschen Ordner).
- Workspace-Tools ins MCP (die heutigen Chat-Tools).
- Screen **„Coding-Agent verbinden“**: Spiel-Ordner wählen → schreibt `.mcp.json`, Codex-Config, `.claude/skills/glob/`, AGENTS.md-Snippet; zeigt „verbunden ✓“.
- Coding-Agent-Tabs raus, Chat optional.
- Aufräumen: doppeltes Handoff-System, erfundener Seed-Eintrag, falsche QC-Defaults in Quittungen, Demo-Modus-Versprechen, Doku (AGENTS.md, README, Tool-Anzahl).

### Phase 2 – Agent Orders
- Order-Modell + Migration aus ASSET_REQUIREMENTS / agent_requests.
- MCP-Tools: `place_order`, `list_orders`, `file_order` (einsortieren), `update_order`, `get_deliveries`.
- Neuer Tab **Agent Orders**: Spalten Entwurf · Eingeordnet · In Arbeit · Ready · Geliefert; Klick führt zum Asset; „Einsortieren“ per Drag & Drop oder via Glob.
- Ready-Berechnung live; Vault nutzt dieselben Regeln (heute gilt dort fälschlich „ready = Bild + 3D“ für alles).

### Phase 3 – Lieferung
- Spiel-Ordner-Einstellung pro Projekt, Lieferordner + Manifest, Auto-Lieferung.
- Status → delivered; Agent bekommt klare Anleitung im Prompt.

### Phase 4 – Import Game neu
- Analyse → **Prüfschritt**: Bereiche als Baum, pro Bereich/Asset an/aus, Bereiche umbenennen/zusammenlegen → erst dann Import.
- Platzhalter aus dem Scan können direkt als Entwurfs-Bestellungen übernommen werden.

### Phase 5 – Neue Anbieter & Audio
- Neuer Asset-Typ **audio** (Player, Kandidaten, Gewinner).
- **ElevenLabs**: Soundeffekte (0,5–30 s, Loops) und Stimmen (TTS + Voice Design) – Key existiert schon.
- **Stable Audio**: Musik/Ambient-Loops.
- **Tripo**: zweiter 3D-Anbieter (Image/Text→3D, Rig, Retopo) neben Meshy.
- Später möglich: Hyper3D Rodin (High-End-3D), OpenAI gpt-image / FLUX als weitere Bildmodelle.
- **Budget-Übersicht** in der UI (Endpoint existiert, wird nie angezeigt); Rig/Animate-Kosten korrekt erfassen.

### Phase 6 – Release
- Onboarding auf den neuen Workflow, README + Landingpage, Demo-Video-Skript.
- Tests für Orders, Ready-Regeln, Lieferung, MCP-Projekt-Flag.
- Version 2.0, Patch Notes.

---

## Offene Fragen (mit meinem Vorschlag)
1. **Auto-Lieferung bei Ready?** → Ja, standardmäßig an, abschaltbar.
2. **Lieferordner-Name?** → `glob_delivery/` (Godot: `res://glob_delivery/`).
3. **Glob-Chat standardmäßig an?** → An, solange kein externer Agent verbunden ist.
4. **Version?** → 2.0, weil sich das Bedienkonzept ändert.
