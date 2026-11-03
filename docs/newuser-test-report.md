# Glob v3: new-user test report

Tester: a brand-new user (Claude, driving the built-in browser pane at `http://127.0.0.1:8420`), 2026-11-03.
Build: Glob 2.1.0, installed from this repo at commits 1f13b9a / 546d82c. No code was changed.

## Summary

Glob is already charming. When it works it feels close to magic:

- The welcome screens are lovely.
- Chat built a whole project (pitch, style bible, four sections, four assets) in about 30 s.
- One level picture became a prop sheet, which "Cut" split into **11 named prop assets in 5 s**.
- Sounds arrive with a waveform and an auto-picked winner.
- The Generate windows show exactly what is sent.

What stops it being "plug and play, idiot proof":

1. **A new user cannot create a project without a native OS folder dialog.** The wizard has no typed-path fallback, so it cannot be driven from the pane at all. I had to call the API directly.
2. **Phone width is broken in the Generate window.** The Generate and Cancel buttons are pushed off-screen.
3. **Money is blind.** The $ counter says "$0.00 spent" after my ElevenLabs spend. ElevenLabs models have no price anywhere. The "Images ✓ your API keys" chip suggests the paid key is the image source, while the real default is ElevenLabs. Two defaults are 2 images or 2 takes per run.
4. **The product's centre of gravity is still 3D.** Make 3D, Rig, Animate, Turnaround, "Make them 3D", FBX/GLB export and "Export-ready: 0" show up in a 2D pixel-art project. A sound asset gets a "Make 3D" button and a broken thumbnail in the Vault.
5. **Silent or unexplained dead ends.** Disabled buttons look enabled. Make 3D lets you press go with "2 Meshy credits left" against a 30-credit job. After Cut you are left on the sheet with no pointer to the new props. The icons you make have no visible home.
6. **The model ignored the edit.** I asked "make the plume blue"; it stayed red and the pose changed. Nothing tells the user an edit "did not take".

## How the test went

| Area | Result |
|---|---|
| Launch from the Start menu shortcut (`Glob.lnk` → `glob-app.exe`) | Server reachable after **10.9 s**. It opens an Edge app window titled "Glob". |
| Welcome setup (replay) | Works. The Claude Code check took about 4 s. |
| Project creation | **Blocked in the UI** (see B1). Created through `POST /api/projects` with `D:\glob-tester-game`. |
| Folder, asset kinds, kind icons | Worked. Kind icons show on assets without art. |
| Generate (images), pick winner, edit | Worked; the edit was ignored (M7). |
| Sounds | Worked well. |
| Level kit | Worked end to end; see the notes under Major. |
| Split, icons, export, Final Vault | Mixed (M4, M9, M10). |
| Chat | Works with Claude Code; about 25-30 s per reply. |
| Settings (5 tabs) | Mostly clear. Several misleading labels. |
| "Your game" | Wrote `AGENTS.md` and `glob/ASSETS.md` correctly. |
| Palette (Ctrl+K) | Fast and good. Dead-end empty state. |
| Phone, tablet, desktop | Tablet fine. Phone has a blocker. |

---

## Blocker

### B1. Creating a project needs a native folder dialog; no typed path
- **Where:** welcome → "Start a new project" → "Choose game folder…"; "Skip · start blank" → "Create blank workspace"; "Keep it somewhere else…".
- **What happened:** The "Create blank workspace" button stays disabled with no hint ("Choose your game's folder first." appears only as a toast if you could click it). "Choose game folder…" and "Keep it somewhere else…" call `/api/system/choose-folder`, a server-side OS dialog. It is invisible to the browser and cannot be automated. Typing a name and pressing the button does nothing. If the dialog opens behind the app window, the user is stuck with no feedback.
- **Fix:**
  - Add a text field "or paste a folder path" under the picker, with a "Create the folder for me" default (`Documents\Glob\<name>`).
  - Make "blank" work with no game folder at all.
  - Enable the button with a reason shown when it cannot proceed.
  - Show "A folder window is open. Look behind this one." while the dialog is pending.

### B2. Generate window is unusable at phone width
- **Where:** Generate 2D (and the same footer in Variant, Edit, Prop sheet) at 375 px.
- **What happened:** The footer overflows sideways. Measured: Cancel at x=350 and "Generate 2D" at x=432 on a 375 px viewport (off-screen). Only "Copy prompt + images" is visible. The dialog title wraps into three lines.
- **Fix:** Stack the footer on narrow screens: a full-width primary button, and Cancel as a text link above or below it.

---

## Major

### M1. Spend is invisible and the cost displays disagree
- **Where:** Settings → Accounts footer; Generate window; batch dialog; chat.
- **What happened:**
  - The footer says "$0.00 spent in Tester Playground of a $10.00 limit" after I made 4 images and 3 sounds. ElevenLabs credits are never counted.
  - The ElevenLabs image list (28 models) has no prices; the Gemini list does.
  - The Generate window says only "paid in your ElevenLabs credits".
  - The batch dialog says "2 images · paid in your signed-in service's credits".
  - The Make 3D window shows a Meshy credit cost.
  - The chat said: "I can't give you an exact price. Glob doesn't show per-image prices."
- **Why it matters:** The user is short on money. It is the one thing they need to see.
- **Fix:**
  - Read the ElevenLabs credit balance and per-model cost and show "≈ 8 credits, you have 4,210" in every Generate footer.
  - Count ElevenLabs spend in the per-project total.
  - Show "credits left today" in the header.

### M2. "Images ✓ your API keys" chip and model lists mislead on which key is paid
- **Where:** Settings → Accounts summary chips; Settings → New art dropdown.
- **What happened:**
  - The chip says Images come from "your API keys" (the Gemini key), yet the working default is "ElevenLabs · signed in".
  - The same model appears three times under different groups ("Nano Banana 2.1" under Gemini, Meshy and "Gemini Nano Banana 2.1" under ElevenLabs).
  - A closed dropdown shows only "Gemini Nano Banana 2.1", with no source.
  - Group headings "Gemini / Meshy / ElevenLabs" mean "billed through…", which is not obvious.
- **Risk:** A new user can pick the paid Gemini route by accident.
- **Fix:** Label each option "Nano Banana 2.1 · via ElevenLabs credits" or "· your Gemini key ($0.035)". Have the chip show the real default. Show a "free / paid" colour or badge.

### M3. 3D tooling dominates a 2D project
- **Where:** every asset page (Make 3D with a purple outline, Turnaround, Rig, Animate, Add 3D model, Body parts); palette ("Make 3D", "Turnaround views", "3D lineup" are the first actions); Level kit step 4 "Make them 3D"; Export "FBX and GLB".
- **What happened:** The project dimension is pixel-art 2D. Make 3D, Rig and Animate are shown (greyed out) on a Level and even Final Vault rows for a **sound**.
- **Fix:** Filter by the project's dimension and asset kind. Hide 3D tools on 2D projects and offer a "Turn on 3D tools" toggle. For sounds, show only sound tools. Export for 2D should offer a sprite sheet or PNG pack.

### M4. Final Vault is wrong for 2D and for sounds
- **Where:** Final Vault.
- **What happened:**
  - The "Splash" sound shows a **broken image thumbnail** and a "Make 3D" button.
  - "Export-ready: 0" stays at 0 for a finished 2D project because only 3D models count.
  - "Final images: 15" counts the sound.
  - Prop cut-outs are grouped under "Levels (12)".
  - "Write to exports folder" vs "Your game" vs "Export for Godot/Unity/Unreal" are three competing ways to deliver.
- **Fix:** Give each kind its own thumbnail and action set. Define "ready" per kind (a picked image or sound is ready). Merge delivery into the "Your game" flow, with the ZIP as a secondary option.

### M5. Disabled buttons look enabled; no reason
- **Where:** Level kit "Make sheet" and "Cut" before a winner exists; Variant, Edit image and Make 3D before a winner.
- **What happened:** "Make sheet" is rendered bright and white, but is `disabled` with only a hover title ("Pick a level picture first"). Clicking gives no toast.
- **Fix:** Grey it out and write the reason under the step ("Pick a picture first"). Make a click on a disabled button scroll to or flash the missing step.

### M6. Make 3D allows an unaffordable run
- **Where:** Make 3D window.
- **What happened:** The footer reads "≈ $0.30 (30 Meshy credits) · 2 Meshy credits left", but the **Make 3D button is still active**. (I cancelled.)
- **Fix:** Show an inline "You are 28 credits short. Top up Meshy" and disable the button. Show the balance in the header.

### M7. Edit image ignored the instruction and silently redrew the figure
- **Where:** Frog Knight → Edit image, "Make the helmet plume blue".
- **What happened:** It cost an image. The plume stayed red, the sword changed hands, and the figure's pose changed. The result lands in a separate "EDITS" row far down the page. No before/after, and no hint that the edit "did not take".
- **Fix:**
  - Show the source and the result side by side.
  - Add "Try again (free if it ignores you)" or "keep the original".
  - Suggest a model that edits well.
  - Move the edit result above the fold.

### M8. Results are hidden below the fold; a single candidate still needs a click
- **Where:** asset page after Generate.
- **What happened:** The big picture slot says "Pick one of the 1 candidates below"; the candidate sits far down. After "Edits" there is a third section below. A single result is not auto-picked as winner, although a lone sound is.
- **Fix:** Auto-pick the only result, or show candidates directly in the main frame. Fix "1 candidates". Scroll to the new result when the job finishes.

### M9. After "Cut", the user is not taken to the props
- **Where:** Level kit → Cut into props.
- **What happened:** The dialog closes; the only sign is a new "Mushroom forest props" subfolder in the sidebar. No toast was seen. The 11 props are placed under "Levels" rather than "Props". Two were named identically ("red mushroom"). One filename ends in `-split.png`, and their pixel scale differs.
- **Fix:** Navigate to the new folder with "11 props made from Mushroom Forest" and an Undo. Make names unique ("red mushroom 2" was applied, good). Rename `-split`. Offer to move the props into the Props section.

### M10. Prop sheet prompt is tuned for 3D and ignores the pixel style
- **Where:** Level kit → Make sheet.
- **What happened:**
  - The dialog says "No style bible yet. Add one under Bible pillars", but this project **has** a style bible (`pillars/style-bible.md`). The warning is misleading, or the style was not sent.
  - The generated prompt says "seen from the front at a slight top-down angle, **as for a 3D model**" in a side-view 2D platformer.
- **Fix:** Build the sheet prompt from the project dimension and camera ("side view, flat, pixel art"). Include the style bible and fix the "no style bible" check.

### M11. Glob chat: leaks tool names, invents steps, and does not know the project
- **Where:** chat panel.
- **What happened:**
  - It said "run prop_sheet and Cut", "make_3d, rig and animate", and "views", which are raw tool names.
  - It said "I also noticed `01-characters/splash/` has an audio folder but no art yet. Tell me if it's a sound asset", although `splash.md` was created as an SFX asset.
  - Each reply takes 20-30 s and shows only "Forging…" or a spinner, with no streaming and no tool progress.
  - Earlier in the session, in the Tarky project, the chat showed "Not logged in · Please run /login" while Settings said "Claude Code ✓ Logged in" and the Test button said "Works".
- **Fix:** Rewrite chat tool names into plain words. Give the chat the asset kinds. Stream text and tool progress. Make the model status consistent between chat and Settings, with a one-click "Fix" button on that error.

### M12. Duplicate project names; project list is confusing
- **Where:** Projects page.
- **What happened:** "ImportTest" displays as "TARKY" (a second Tarky) next to the real "Tarky". Delete and download icons sit next to each other on cards.
- **Fix:** Show the folder or path under the name, and ask for a new name on import. Confirm delete (not tested).

### M13. Clutter inside the user's game folder
- **Where:** `D:\glob-tester-game`.
- **What happened:** Glob writes `glob/manifest.db`, `studio_jobs.json`, `logs/`, `inbox_watch/`, `assets/ast_*.png|mp3` (hash names), plus `AGENTS.md`, and creates no `.gitignore`. "Your game" says the note goes in "AGENTS.md, CLAUDE.md", but only `AGENTS.md` was written.
- **Fix:** Write a `glob/.gitignore` for the db, logs and jobs. Fix the card text to match what was written. In ASSETS.md, drop the "3D model / Rig" columns for 2D projects and add image size and a transparent flag.

---

## Minor

- **m1. Project startup is slow:** the shortcut takes about 11 s before the server answers. A splash or "Starting Glob…" window is not shown, so users may think nothing happened or double-click again. Two windows ("Glob" in Edge and "Glob – Opera") were open after launch; confirm the shortcut opens one.
- **m2. Welcome copy says "Two quick steps"** but the wizard shows four progress dots. "Skip · start blank" leads to a page titled "Where does it live?" (different wording from "Where's your game?"). "Workspace", "project" and "game" are used interchangeably.
- **m3. OpenCode status disagrees:** "Installed · Connect" in the welcome, "Didn't answer · Try again" in Settings.
- **m4. Empty-project page:** title "Let's set up glob-tester-game" uses the folder name, not "Tester Playground". Copy reads "connected to your Folder game". Subtitle "Your game project" is a placeholder. There is no way to add an asset without the chat (no "+ New asset" on the empty overview).
- **m5. New asset dialog:**
  - The name placeholder "Bronze Knight" and description "A stout knight in dented bronze armour…" stay on screen for Sfx, Voice and Music. They need sound-appropriate examples.
  - The Sfx, Voice and Music chips have a "•" instead of an icon.
  - "Sfx" and "VFX" are jargon.
  - Pressing Create with an empty name only focuses the field, with no message.
- **m6. New folder:** an input like `../Mush?rooms` is accepted, shown as a title `../Mush?rooms` and a slug `mush-rooms`. Sanitise the title as well. The placeholder "Creatures" appears inside Props, and the copy mentions `02-props` (raw folder name). An empty folder has no empty state.
- **m7. Section toolbar overlaps path:** on the Characters page the file path `…ters/README.md` floats left of the buttons with a clipped start. In the chat+project split layout the buttons stack vertically and overlap the path. The breadcrumb collapses to "W… / M… / L… / Mushroom".
- **m8. Asset page:** breadcrumb says "Frog knight" while the title says "Frog Knight" (case). The tooltip "Drop an image to replace" covers the bottom of the picture. The candidate menu is a "---" button, not a recognisable "more" icon. Several unlabelled icon buttons on the project overview have no accessible names.
- **m9. Generate defaults:** Generate shows Images=2 (cost doubled by default) while Edit and Prop sheet default to 1. Style bible is on in Generate, off in Variant and Edit. Sound variations default to 2. The batch dialog ("Generate all missing") uses 2 images per asset with no per-asset control. A cost-per-image estimate is missing for ElevenLabs.
- **m10. Sound length ignored the prompt:** the prompt said "under half a second"; the take is 3 s. The first sound was 1 s for a "splash". "Length: Automatic" has no hint. File names are `frog-jump-20261103-024812-1.mp3`, which are meaningless to users. "Loop check" and "Save selection" are unexplained. A take next to the winner is sorted before it.
- **m11. Jargon and AI-sounding copy:**
  - "Bible pillars", "STYLE BIBLE · PUT IN FRONT OF EVERY PROMPT", "Pillars sent with every generation: Normal", and "How the prompt is built".
  - "root overview", "Forging…", "Make pillar", "Cut-outs under Parts", "Separate figures / Body parts" in the Cut dialog for props.
  - The Make 3D window uses "PBR maps", "Remesh to a target polycount" and "Mesh Standard · high detail".
  - The project overview page says "Nothing has been generated yet: open an asset and press Generate" even when assets exist (seen on Tarky).
  - Chat prompts say "Create a project structure for my game. It's about…".
- **m12. Palette:** Ctrl+K is advertised as "⌘K" on Windows. "Nothing matches." is a dead end; offer "Ask Glob about xyzzy" or "Create asset xyzzy".
- **m13. Layout shifts:** the "Generate running…" banner pushes the whole page down by about 40 px when a job starts and shifts it back at the end, moving the button you just clicked. The "N running" pill and toasts cover content (also on phone). A stale "Generate 2D finished" toast appeared minutes after the work was done, on another page.
- **m14. Tool Split / Body parts:** boxes returned by Glob for the frog overlapped heavily (11 boxes, labels colliding) and took about 25 s. Useful, but the quality for a pixel-art frog is poor; show a preview, a "Reset" and an undo.
- **m15. Settings clarity:** the "Folders Glob may use" list in Advanced contains an old temp folder (`…\Temp\tmp9a2m3wiq\g`). "Sign out" sits right next to the normal account details with no confirm shown (not tested). The Accounts page says "Glob is free" and then lists seven services to pay.
- **m16. Phone layout (other than B2):** the Settings tab strip wraps "Files & app" to three lines. The Chat and Project tabs work well.
- **m17. Speed (measured):**
  - `/api/overview`: avg 99 ms, max **834 ms**, called 37 times in one session (every navigation refetches it).
  - `/api/projects/cards`: 873 ms.
  - `/api/studio/credits`: **515 ms** each, so Make 3D opens half a second late.
  - `/api/studio/name-parts`: 4.4 s. `/api/studio/split` (cut 11 props): 0.9 s.
  - `/api/projects` create: 840 ms.
  - Chat reply: 17-25 s.
  - Image generation: about 55 s (single image); prop sheet about 45 s; sound about 10 s.
  - In-app navigation (section, asset) is fast (about 35 ms).
  - No job shows elapsed time or an estimate, and there is no Cancel for a running job.

---

## Polish

- The ElevenLabs Generate row says "Transparent background · ElevenLabs cuts it out after (a few extra credits)". Say how many.
- "1 candidates" and "Pick one of the 1 candidates below" need pluralisation.
- "Press kit" and "Make key art" CTAs are unexplained in a new project.
- Gallery mode's button highlights but changes nothing visible in the Vault.
- The kind chips and the "Kind" badges use different colours per kind (orange for all). A colour per kind would make the lists scannable.
- The empty "Final Vault" and the empty "Sounds" section have no tailored empty state.
- Cut-out props have uneven edges and lose the pixel grid of the sheet; mention "smooth" vs "pixel" scaling in the icon dialog.
- Prop and asset names from the cut come out in lowercase ("green vine"), while others use Title Case.
- Export ZIP asks for no confirm and does not say where it is saved (not tested; I did not download).

---

## Delight ideas

1. **"Spend meter" in the header:** a small coin with "≈ 18 credits today". It fills as you generate and warns before a big batch ("This batch costs about 40 credits. You have 120.").
2. **One big "Make my first asset" button** on an empty project: picks the hero, generates 1 image with the cheapest model, shows it popping into place.
3. **Instant placeholders:** the moment you create an asset, generate a free local pixel placeholder (palette from the style bible) so the game folder is never empty and ASSETS.md always has a file.
4. **Level kit as a guided strip:** make the 4 steps a progress bar across the top of the Level page, and animate the cut: boxes appear, props "fly out" to the new folder. Offer "Place props on the level picture" as a preview.
5. **Sound pad:** a keyboard-triggered preview (space to play the winner, arrows to flip takes). Add "make it shorter / longer / punchier" chips on a take.
6. **Compare mode:** side-by-side previous vs new for Edit and Variant, with "keep both" and "undo".
7. **"Try it in the game" button:** copy the agent prompt from "Your game" with the asset path filled in, or send it straight to the user's coding agent.
8. **Ask-Glob everywhere:** every empty state, error and "Nothing matches" gets a one-click "Ask Glob" chip pre-filled with the context.
9. **Style from one picture:** drag a game screenshot onto the overview to extract the style bible and palette (free local colour pick plus the chat for words).
10. **Sounds for what is on screen:** from a level picture, offer a list of "ambience this level needs" with one free chat call, then batch the cheap sound jobs.
11. **Celebrate:** a small confetti and a sound when a winner is picked or the last missing asset of a section is filled; the pink blob mascot waves.
12. **Time estimate and cancel on jobs:** "about 50 s left", a cancel icon on the running pill, and a peek preview as soon as the first image arrives.

---

## Budget spent

| Item | Used | Limit |
|---|---|---|
| Images (all "ElevenLabs · signed in (MCP)", Images = 1 each) | **4**: Frog Knight (1), Frog Knight edit (1), Mushroom Forest level (1), Mushroom forest prop sheet (1) | 6 |
| Sound takes (ElevenLabs sound effects) | **3**: Splash (1), Frog Jump (2, left at the default of 2) | 4 |
| 3D (Meshy / Tripo) | **0**. I only opened the Make 3D window and cancelled. | 0 |
| Gemini / "Glob · your API keys" | **0** | 0 |
| Free local work | Cut (11 props), Make icons, Split box detection (cancelled, not cut), the chat | n/a |
| Signing in or out, API keys, projects folder | none touched | n/a |

Not tested: Export ZIP download (needs a download), Unlink, project delete, "Sign out" confirms, the key-art flow, the questionnaire wizard path ("Answer the questions instead"), and project creation through the UI (B1). Everything I created lives in the project **Tester Playground** (`D:\glob-tester-game\glob`). Existing projects were only viewed.
