---
name: services
description: What each signed-in service (ElevenLabs, Higgsfield, fal, Meshy) can do, how to find and call its tools, and how to save results. Read before answering "can X do Y" or before any service_tools / service_call.
---

# Signed-in services

The user can sign in to generation services in Settings → Generators instead of pasting API keys. The context
lists them under "Connected services". Each one runs on the user's own account, with that account's plan and
credits.

| service | what it makes | in the studio (`generate`) | through its own tools |
|---|---|---|---|
| ElevenLabs | images (GPT Image, Nano Banana, Seedream, FLUX, Recraft...), video, speech, voices | image models `svc:elevenlabs/...` | `creative_generate_image`, `creative_generate_video`, `creative_generate_speech`, `creative_design_voice` |
| Higgsfield | images (GPT Image, Nano Banana, Soul, Seedream...), video, audio, 3D | image and 3D models `svc:higgsfield/...` | `generate_image`, `generate_video`, `generate_3d` |
| fal | hundreds of image, video, 3D and audio models | not yet | its own tools |
| Meshy | 3D models, textures, rigs, animations, images | the `meshy:` models and Make 3D; the sign-in counts as the Meshy key | none: it powers the studio only |

## Look before you answer

A service can have well over a hundred tools: ElevenLabs has its agent-platform tools AND its creative tools.
**Never say a service can't do something until you have searched for it:**

```json
{"tool": "service_tools", "service": "elevenlabs", "query": "image"}
```

Generation tools come first in the list. Read one tool's inputs with `service_tool_info` before calling it.

## Calling a tool directly

- `service_call` spends credits. Call it once; never call it again just to retry.
- Many tools start a job and return an id. Poll their status tool until it is done
  (ElevenLabs: `creative_get_flow_run_status`, Higgsfield: `jobs_wait` / `job_status`), then fetch the file URL.
- Save every result with `save_url` into the asset's folder (images in `candidates/`), so the user sees it on
  the asset page.
- For images and 3D, `generate` with a `svc:` model does all of this for you (see the generating skill).
