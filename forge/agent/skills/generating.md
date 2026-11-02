---
name: generating
description: How Glob makes images, 3D models, rigs, animations and sounds, which model runs, and how to pick one (API keys, the Meshy sign-in, or a signed-in service). Read before any generate call or any question about how something was made.
---

# Generating assets

Every generation goes through the `generate` tool on an asset `.md` file. It starts a studio job that runs in the
background. The outputs land in the asset's own folder (`<asset>/candidates/`, `<asset>/3d/`, ...) and show up on
the asset page. You never make the image yourself, and there is no "built-in generator" that runs without a model:
something specific always runs, and it is named in the "Generation setup" block of your context.

## Which model runs

- With no `model`, image actions use the **default image model** from Settings → Generators. The context names it,
  for example `meshy:nano-banana-2-1 (Meshy · Nano Banana 2.1, uses your API key)`.
- Pass `"model"` to choose one. There are two kinds of model id:
  - **API keys / Meshy sign-in**: ids like `gemini-3.1-flash-image`, `meshy:nano-banana-2-1` or
    `elevenlabs:gpt-image-2`. These run on the user's pasted keys. Meshy models also run on the Meshy sign-in.
    An `elevenlabs:` id uses the ElevenLabs **API key**. Some plans lock image generation over the API (a 402
    "requires a Pro plan" error).
  - **Signed-in services**: `svc:<service>/<model>`, for example `svc:elevenlabs/gemini-nano-banana-2.1`,
    `svc:elevenlabs/gpt-image-2.5-sunburst` or `svc:higgsfield/gpt_image_2_5`. These run on the user's own account
    through the service's sign-in (MCP), with their plan and credits. This is how the user gets the models their
    plan allows even when the API key is locked.
- When the user names a service and a model ("through ElevenLabs, Nano Banana 2.1"), use the matching `svc:` id.
  Never swap in a different model or service silently. If the exact model is not in the list, call
  `{"tool": "models"}` and choose the closest match, and say which one you used.
- The `generate` result names the model that ran. Repeat it to the user exactly.

## Actions

| action | what it makes | notes |
|---|---|---|
| candidates | new images from the asset prompt | `count` 1-4 |
| variant | a changed copy of the winner | needs `instruction` |
| edit | an edit of the winner | needs `instruction` |
| views | turnaround views (front, side, back...) of the winner | good input for 3D |
| make_3d | a 3D model (GLB) from the winner image | `model`: a `svc:` 3D model (for example `svc:higgsfield/meshy_v7_image_to_3d`), or leave it out for the Meshy / Tripo settings |
| rig | a rigged character from the 3D model | Meshy |
| animate | animations on the rig | needs `action_ids` |

Make 3D, rig and animate need a **winner** first: use `pick_winner` once candidates exist.

## Examples

```json
{"tool": "generate", "path": "02-characters/crew/lantern-beak.md", "action": "candidates", "count": 1, "model": "svc:elevenlabs/gemini-nano-banana-2.1"}
{"tool": "generate", "path": "02-characters/crew/lantern-beak.md", "action": "make_3d", "model": "svc:higgsfield/meshy_v7_image_to_3d"}
```

## Rules

- Generate only when the user asked. It spends their keys or credits.
- One `generate` call per asset. Jobs run in the background: say they started, not that they are done.
- To check on finished work, `list` the asset's folder. Never claim a result you haven't seen.
- Prefer `generate` with a `svc:` model over calling a service's raw tools: `generate` uploads the references,
  waits for the result and files it on the asset. Use `service_call` only for things the studio can't do
  (video, voice design, a service-specific feature).
