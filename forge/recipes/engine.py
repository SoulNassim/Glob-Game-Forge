"""Declarative Recipe Engine for Forge.

Supports:
- Loading built-in and user/agent-created YAML/JSON recipes
- Read-only DAG graph projection of any recipe (`get_recipe_graph`)
- Pre-flight cost estimation (`estimate_recipe_cost` / `dry_run=True`)
- Loops: `for_each`, `repeat: N`, `repeat_until: critic_passes` (with `max_iterations`), `parallel` (with concurrency semaphore)
- Conditionals (`when`)
- Hard Budget Guard (per-run and per-project spend cap, pausing the job with status `PAUSED_BUDGET` if exceeded)
- Deterministic provider response caching by SHA-256(inputs + prompt + model + seed)
"""

from __future__ import annotations

import asyncio
import json
import random
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Awaitable
import yaml
from PIL import Image

from forge.config import get_config_dir
from forge.models import (
    ActivityLogEntry,
    ApprovalState,
    Asset,
    AssetType,
    AutonomyMode,
    CriticReport,
    Job,
    JobStatus,
    ProjectBible,
    QCReport,
    new_id,
)
from forge.providers.base import ProviderCapability
from forge.providers.elevenlabs_image import resolve_image_provider
from forge.providers.registry import REGISTRY
from forge.recipes.qc import run_qc_checks
from forge.storage.project_store import ProjectStore
from forge.prompt_sanitize import strip_palette_values
from forge.stylepacks.builtins import (
    IDENTITY_LOCK_INSTRUCTION,
    build_variant_prompts,
    list_all_style_packs,
)


BUILTINS_DIR = Path(__file__).parent / "builtins"
SCALPEL_EDIT_LOCK = (
    "Edit the attached image. Apply only the requested change and keep everything not mentioned identical: "
    "subject, identity, pose, proportions, framing, camera angle, art style, lighting, and background."
)


class BudgetExceededError(Exception):
    def __init__(self, message: str, estimated_or_spent: float, cap: float) -> None:
        super().__init__(message)
        self.message = message
        self.estimated_or_spent = estimated_or_spent
        self.cap = cap


def get_custom_recipes_dir() -> Path:
    d = get_config_dir() / "recipes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_recipes() -> list[dict[str, Any]]:
    recipes: dict[str, dict[str, Any]] = {}
    for folder, is_builtin in [(BUILTINS_DIR, True), (get_custom_recipes_dir(), False)]:
        if not folder.exists():
            continue
        for f in sorted(folder.glob("*.y*ml")) + sorted(folder.glob("*.json")):
            try:
                raw_text = f.read_text(encoding="utf-8")
                data = yaml.safe_load(raw_text) if f.suffix.lower() in (".yaml", ".yml") else json.loads(raw_text)
                if isinstance(data, dict) and "id" in data:
                    data["builtin"] = is_builtin
                    data["raw_yaml"] = raw_text
                    data["graph"] = build_recipe_graph(data)
                    recipes[data["id"]] = data
            except Exception:
                continue
    return list(recipes.values())


def get_recipe(recipe_id: str) -> dict[str, Any]:
    for r in list_recipes():
        if r["id"] == recipe_id:
            return r
    raise KeyError(f"Recipe '{recipe_id}' not found.")


def save_recipe(recipe_id: str, content_str: str, fmt: str = "yaml") -> dict[str, Any]:
    """Create or update a recipe from YAML or JSON text."""
    parsed = yaml.safe_load(content_str) if fmt == "yaml" else json.loads(content_str)
    if not isinstance(parsed, dict):
        raise ValueError("Recipe must be a YAML/JSON object.")
    parsed["id"] = recipe_id
    if "name" not in parsed:
        parsed["name"] = recipe_id.replace("_", " ").title()
    if "steps" not in parsed or not isinstance(parsed["steps"], list):
        raise ValueError("Recipe must define a 'steps' list.")

    clean_id = "".join(c if (c.isalnum() or c in "-_") else "_" for c in recipe_id)
    out_file = get_custom_recipes_dir() / f"{clean_id}.yaml"
    yaml_str = yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True)
    out_file.write_text(yaml_str, encoding="utf-8")
    parsed["builtin"] = False
    parsed["raw_yaml"] = yaml_str
    parsed["graph"] = build_recipe_graph(parsed)
    return parsed


def build_recipe_graph(recipe_data: dict[str, Any]) -> dict[str, Any]:
    """Generate a read-only visual graph model (nodes + edges) for transparency in Advanced Mode."""
    nodes: list[dict[str, Any]] = [
        {"id": "input", "label": "Inputs & Project Bible", "kind": "input", "loop": None}
    ]
    edges: list[dict[str, str]] = []
    steps = recipe_data.get("steps", [])
    prev_id = "input"
    for step in steps:
        sid = step.get("id", "step")
        loop_cfg = step.get("loop")
        loop_desc = None
        if isinstance(loop_cfg, dict):
            parts = []
            if "for_each" in loop_cfg:
                parts.append(f"for_each({loop_cfg['for_each']})")
            if "repeat" in loop_cfg:
                parts.append(f"repeat({loop_cfg['repeat']})")
            if "repeat_until" in loop_cfg:
                parts.append(f"until({loop_cfg['repeat_until']}, max={loop_cfg.get('max_iterations', 2)})")
            if loop_cfg.get("parallel"):
                parts.append(f"parallel(×{loop_cfg.get('concurrency', 4)})")
            loop_desc = " • ".join(parts)
        nodes.append(
            {
                "id": sid,
                "label": f"{step.get('action', sid)} ({step.get('provider', 'local')})",
                "kind": step.get("action", "step"),
                "provider": step.get("provider", "local"),
                "loop": loop_desc,
                "when": step.get("when"),
            }
        )
        deps = step.get("depends_on") or [prev_id]
        for d in deps:
            edges.append({"from": d, "to": sid})
        prev_id = sid

    nodes.append({"id": "output", "label": "Lineage Tree & Critic", "kind": "output", "loop": None})
    edges.append({"from": prev_id, "to": "output"})
    return {"nodes": nodes, "edges": edges}


def synthesize_prompt_with_bible(
    raw_prompt: str,
    bible: ProjectBible,
    style_pack_ids: list[str] | None = None,
    identity_lock_subject: str | None = None,
) -> tuple[str, str]:
    """Combine user prompt + Project Bible + Style Packs + optional Identity Lock."""
    all_packs = {p.id: p for p in list_all_style_packs()}
    active_ids = style_pack_ids if style_pack_ids is not None else bible.default_style_packs
    if bible.locked_style_packs:
        active_ids = bible.default_style_packs

    prefixes = [
        strip_palette_values(all_packs[pid].prompt_prefix)
        for pid in active_ids
        if pid in all_packs and all_packs[pid].prompt_prefix
    ]
    suffixes = [
        strip_palette_values(all_packs[pid].prompt_suffix)
        for pid in active_ids
        if pid in all_packs and all_packs[pid].prompt_suffix
    ]
    negatives = [all_packs[pid].negative_prompt for pid in active_ids if pid in all_packs and all_packs[pid].negative_prompt]

    segments: list[str] = []
    if identity_lock_subject:
        segments.append(f"{IDENTITY_LOCK_INSTRUCTION} Reference subject: '{identity_lock_subject}'.")
    segments.extend(prefixes)
    segments.append(strip_palette_values(raw_prompt))
    segments.append(f"Art style: {bible.art_style}. Perspective: {bible.camera_perspective}.")
    if bible.lore_keywords:
        segments.append(f"World motifs: {', '.join(bible.lore_keywords[:4])}.")
    segments.extend(suffixes)
    if bible.background_rule == "transparent":
        segments.append("Isolated single subject on clean transparent background.")
    elif bible.background_rule == "solid_neutral":
        segments.append("Isolated single subject on flat neutral studio grey background.")

    positive = strip_palette_values(" ".join(segment.strip() for segment in segments if segment.strip()))
    return positive, strip_palette_values(", ".join(negatives))


def estimate_recipe_cost(recipe_id: str, inputs: dict[str, Any], bible: ProjectBible) -> dict[str, Any]:
    """Calculate estimated cost before executing a recipe or verb (also used for dry_run)."""
    recipe = get_recipe(recipe_id)
    image_params = dict(inputs)
    image_provider = resolve_image_provider("gemini", image_params)
    img_adapter = REGISTRY.get(image_provider)
    mesh_adapter = REGISTRY.get("meshy")

    img_unit = img_adapter.estimate_cost(ProviderCapability.TEXT_TO_IMAGE, inputs).estimated_usd
    mesh_unit = mesh_adapter.estimate_cost(
        ProviderCapability.IMAGE_TO_3D,
        {"should_texture": inputs.get("should_texture", True), "ai_model": inputs.get("ai_model")},
    ).estimated_usd

    total_usd = 0.0
    breakdown: list[dict[str, Any]] = []

    if recipe_id == "edit_image":
        count = max(1, min(4, int(inputs.get("count", 1))))
        total_usd = round(img_unit * count, 4)
        breakdown.append({"step": f"Scalpel edit × {count}", "cost_usd": total_usd})
    elif recipe_id == "variant":
        count = int(inputs.get("count", 4))
        total_usd = round(img_unit * count, 4)
        breakdown.append({"step": f"Generate {count} identity-locked variants", "cost_usd": total_usd})
    elif recipe_id == "make_3d":
        total_usd = mesh_unit
        breakdown.append({"step": "Meshy 3D generation", "cost_usd": mesh_unit})
    elif recipe_id == "turnaround":
        total_usd = img_unit
        breakdown.append({"step": "3-view orthographic turnaround", "cost_usd": img_unit})
        if inputs.get("auto_make_3d"):
            total_usd = round(total_usd + mesh_unit, 4)
            breakdown.append({"step": "Multi-image 3D mesh", "cost_usd": mesh_unit})
    elif recipe_id == "extract_part":
        total_usd = img_unit
        breakdown.append({"step": "Extract & reconstruct part", "cost_usd": img_unit})
        if inputs.get("send_to_3d"):
            total_usd = round(total_usd + mesh_unit, 4)
            breakdown.append({"step": "Convert extracted part to 3D", "cost_usd": mesh_unit})
    elif recipe_id == "batch_from_list":
        raw_items = inputs.get("items") or []
        if isinstance(raw_items, str):
            raw_items = [line.strip() for line in raw_items.splitlines() if line.strip()]
        n = max(1, len(raw_items))
        asset_kind = inputs.get("asset_kind", "image")
        unit = mesh_unit if asset_kind == "3d_prop" else img_unit
        sub = round(unit * n, 4)
        total_usd += sub
        breakdown.append({"step": f"Batch generate {n} items ({asset_kind})", "cost_usd": sub})
        top_n = int(inputs.get("then_3d_top_n", 0))
        if top_n > 0:
            sub3d = round(mesh_unit * min(n, top_n), 4)
            total_usd = round(total_usd + sub3d, 4)
            breakdown.append({"step": f"Promote top {min(n, top_n)} items to 3D", "cost_usd": sub3d})
    elif recipe_id == "qc_check":
        total_usd = 0.0
        breakdown.append({"step": "Local QC inspection", "cost_usd": 0.0})
    else:
        # Generic step-based estimation
        for step in recipe.get("steps", []):
            action = step.get("action", "")
            if action == "qc_check":
                continue
            step_params = dict(step.get("params") or {})
            if not step_params.get("model") and inputs.get("model"):
                step_params["model"] = inputs.get("model")
            prov = resolve_image_provider(step.get("provider", "gemini"), step_params)
            loop_cfg = step.get("loop") or {}
            mult = 1
            if "repeat" in loop_cfg:
                try:
                    val = loop_cfg["repeat"]
                    mult = int(inputs.get("count", 4)) if isinstance(val, str) and "count" in val else int(val)
                except Exception:
                    mult = 1
            elif "for_each" in loop_cfg:
                items = inputs.get("items") or []
                if isinstance(items, str):
                    items = [x.strip() for x in items.splitlines() if x.strip()]
                mult = max(1, len(items))
            if prov == "meshy":
                u = mesh_unit
            elif prov == "elevenlabs":
                cap_name = str(step.get("capability") or "text_to_image")
                cap = ProviderCapability(cap_name) if cap_name in {item.value for item in ProviderCapability} else ProviderCapability.TEXT_TO_IMAGE
                u = REGISTRY.get("elevenlabs").estimate_cost(cap, step_params).estimated_usd
            else:
                u = img_unit
            c = round(u * mult, 4)
            total_usd = round(total_usd + c, 4)
            breakdown.append({"step": step.get("id", action), "cost_usd": c})

    recipe_cap = float(recipe.get("budget", {}).get("max_run_usd", bible.per_run_cap_usd))
    effective_run_cap = min(bible.per_run_cap_usd, max(recipe_cap, bible.per_run_cap_usd))
    return {
        "recipe_id": recipe_id,
        "estimated_usd": round(total_usd, 4),
        "per_run_cap_usd": effective_run_cap,
        "project_spend_cap_usd": bible.spend_cap_usd,
        "within_run_cap": total_usd <= effective_run_cap,
        "breakdown": breakdown,
    }


EventBroadcastCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class RecipeExecutor:
    """Executes any built-in or custom recipe with loops, caching, budget guards, and critic checks."""

    def __init__(
        self,
        store: ProjectStore,
        broadcast_cb: EventBroadcastCallback | None = None,
    ) -> None:
        self.store = store
        self.broadcast_cb = broadcast_cb
        self._canceled_jobs: set[str] = set()

    def cancel_job(self, job_id: str) -> None:
        self._canceled_jobs.add(job_id)

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.broadcast_cb:
            res = self.broadcast_cb({"type": event_type, "project": self.store.project_name, **payload})
            if hasattr(res, "__await__"):
                await res

    def _check_budget_guard(self, additional_cost: float, run_spent_so_far: float, bible: ProjectBible) -> None:
        spend = self.store.get_spend_summary()
        if run_spent_so_far + additional_cost > bible.per_run_cap_usd + 1e-6:
            raise BudgetExceededError(
                f"Run budget guard triggered: ${run_spent_so_far + additional_cost:.3f} exceeds per-run cap of ${bible.per_run_cap_usd:.2f}.",
                run_spent_so_far + additional_cost,
                bible.per_run_cap_usd,
            )
        if spend["spent_usd"] + additional_cost > bible.spend_cap_usd + 1e-6:
            raise BudgetExceededError(
                f"Project budget guard triggered: ${spend['spent_usd'] + additional_cost:.3f} exceeds project cap of ${bible.spend_cap_usd:.2f}.",
                spend["spent_usd"] + additional_cost,
                bible.spend_cap_usd,
            )

    async def _generate_single_asset(
        self,
        *,
        provider_id: str,
        capability: ProviderCapability,
        name: str,
        asset_type: AssetType,
        verb: str,
        recipe_id: str,
        prompt_raw: str,
        prompt_full: str,
        negative_prompt: str = "",
        parent_ids: list[str] | None = None,
        reference_files: list[Path] | None = None,
        chips_used: list[str] | None = None,
        style_packs: list[str] | None = None,
        params: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        initiated_by: str = "user",
        force_uncached: bool = False,
        repeat_until_critic: bool = False,
        max_iterations: int = 2,
        bible: ProjectBible,
        run_spent_tracker: list[float],
        progress_cb: Callable[[int, str], Awaitable[None] | None] | None = None,
    ) -> Asset:
        parent_ids = parent_ids or []
        params = dict(params or {})
        params.pop("palette", None)
        params.setdefault("transparent", bible.background_rule == "transparent")
        provider_id = resolve_image_provider(provider_id, params)
        seed = params.get("seed")
        adapter = REGISTRY.get(provider_id)
        model_name = params.get("model") or params.get("ai_model") or (
            "meshy-7.1" if provider_id == "meshy" else "elevenlabs:gpt-image-2" if provider_id == "elevenlabs" else "gemini-2.5-flash-image"
        )

        # 1. Check deterministic cache by (inputs + prompt + model + seed)
        cache_key = ProjectStore.compute_cache_key(
            provider=provider_id,
            model=model_name,
            prompt=prompt_full,
            parent_ids=parent_ids,
            seed=seed,
            extra={
                "capability": capability.value,
                "render_mode": params.get("render_mode", "standard"),
                "should_texture": params.get("should_texture", True),
            },
        )
        if not force_uncached:
            cached = self.store.get_cached_result(cache_key)
            if cached and (self.store.project_dir / cached.get("file_path", "")).exists():
                ast_id = new_id("ast")
                ext = Path(cached["file_path"]).suffix
                new_rel = f"assets/{ast_id}{ext}"
                shutil.copy2(
                    self.store.resolve_safe_path(cached["file_path"]),
                    self.store.resolve_safe_path(new_rel),
                )
                thumb_rel = new_rel
                if cached.get("thumbnail_path") and (self.store.project_dir / cached["thumbnail_path"]).exists():
                    thumb_rel = f"assets/{ast_id}_thumb.png"
                    shutil.copy2(
                        self.store.resolve_safe_path(cached["thumbnail_path"]),
                        self.store.resolve_safe_path(thumb_rel),
                    )
                asset = Asset(
                    id=ast_id,
                    name=name,
                    type=asset_type,
                    file_path=new_rel,
                    thumbnail_path=thumb_rel,
                    extra_files=cached.get("extra_files", {}),
                    parent_ids=parent_ids,
                    verb=verb,
                    recipe_id=recipe_id,
                    prompt_raw=prompt_raw,
                    prompt_full=prompt_full,
                    negative_prompt=negative_prompt,
                    chips_used=chips_used or [],
                    style_packs=style_packs or bible.default_style_packs,
                    provider=cached.get("provider", provider_id),
                    model=model_name,
                    seed=cached.get("seed", seed),
                    cost_usd=0.0,  # Cached hit costs $0.00!
                    cost_estimated=False,
                    cached_hit=True,
                    tags=(tags or []) + ["cached"],
                    approval_state=(
                        ApprovalState.PENDING_REVIEW if initiated_by == "agent" else ApprovalState.APPROVED
                    ),
                    metadata=cached.get("metadata", {}),
                )
                if asset_type == AssetType.AUDIO:
                    asset.thumbnail_path = ""
                    asset.qc_report = QCReport(summary="Audio: image QC skipped")
                    asset.critic_report = CriticReport(feedback="Audio: visual critic skipped")
                else:
                    asset.qc_report = run_qc_checks(asset, self.store.resolve_safe_path(asset.file_path), bible)
                    asset.critic_report = await REGISTRY.critic.evaluate_asset(
                        self.store.resolve_safe_path(asset.thumbnail_path or asset.file_path),
                        prompt_full,
                        bible,
                    )
                self.store.save_asset(asset)
                return asset

        # 2. Execute with optional `repeat_until: critic_passes` loop
        iterations = max(1, max_iterations) if repeat_until_critic else 1
        last_asset: Asset | None = None

        for attempt in range(iterations):
            est = adapter.estimate_cost(capability, params).estimated_usd
            self._check_budget_guard(est, run_spent_tracker[0], bible)

            ast_id = new_id("ast")
            attempt_params = dict(params)
            if attempt > 0:
                attempt_params["seed"] = random.randint(1000, 999999)

            res = await adapter.execute(
                capability=capability,
                prompt=prompt_full,
                output_dir=self.store.assets_dir,
                filename_stem=ast_id,
                reference_files=reference_files,
                params=attempt_params,
                progress_cb=progress_cb,
            )
            run_spent_tracker[0] = round(run_spent_tracker[0] + res.cost_usd, 4)

            asset = Asset(
                id=ast_id,
                name=name if attempt == 0 else f"{name} (Retry #{attempt + 1})",
                type=asset_type,
                file_path=res.file_path,
                thumbnail_path=res.thumbnail_path or res.file_path,
                extra_files=res.extra_files,
                parent_ids=parent_ids,
                verb=verb,
                recipe_id=recipe_id,
                prompt_raw=prompt_raw,
                prompt_full=prompt_full,
                negative_prompt=negative_prompt,
                chips_used=chips_used or [],
                style_packs=style_packs or bible.default_style_packs,
                provider=res.provider,
                model=res.model,
                seed=res.seed,
                cost_usd=res.cost_usd,
                cost_estimated=res.cost_estimated,
                cached_hit=False,
                tags=tags or [verb],
                approval_state=(
                    ApprovalState.PENDING_REVIEW if initiated_by == "agent" else ApprovalState.APPROVED
                ),
                metadata={**res.metadata, "critic_iteration": attempt + 1},
            )
            if asset_type == AssetType.AUDIO:
                # Audio has no pixels: skip image QC/critic (the default reports already pass).
                asset.thumbnail_path = res.thumbnail_path
                asset.qc_report = QCReport(summary="Audio: image QC skipped")
                asset.critic_report = CriticReport(feedback="Audio: visual critic skipped")
            else:
                abs_file = self.store.resolve_safe_path(asset.file_path)
                asset.qc_report = run_qc_checks(asset, abs_file, bible)
                critic_img = self.store.resolve_safe_path(asset.thumbnail_path or asset.file_path)
                asset.critic_report = await REGISTRY.critic.evaluate_asset(critic_img, prompt_full, bible)

            self.store.save_asset(asset)
            self.store.set_cached_result(
                cache_key,
                {
                    "file_path": asset.file_path,
                    "thumbnail_path": asset.thumbnail_path,
                    "extra_files": asset.extra_files,
                    "provider": asset.provider,
                    "seed": asset.seed,
                    "metadata": asset.metadata,
                },
            )
            last_asset = asset
            if asset.critic_report.passed and asset.qc_report.passed:
                break

        assert last_asset is not None
        return last_asset

    async def run(
        self,
        verb_or_recipe_id: str,
        parent_asset_ids: list[str] | None = None,
        inputs: dict[str, Any] | None = None,
        initiated_by: str = "user",
        agent_reason: str = "",
        dry_run: bool = False,
        force_uncached: bool = False,
    ) -> dict[str, Any]:
        parent_asset_ids = parent_asset_ids or []
        inputs = dict(inputs or {})
        bible = self.store.get_bible()
        est_info = estimate_recipe_cost(verb_or_recipe_id, inputs, bible)

        if dry_run:
            return {
                "dry_run": True,
                "estimate": est_info,
                "spend_summary": self.store.get_spend_summary(),
            }

        # Check Autonomy mode if initiated by agent
        if initiated_by == "agent" and bible.autonomy_mode == AutonomyMode.SUGGEST and not inputs.get("human_approved"):
            job = Job(
                project_name=self.store.project_name,
                verb=verb_or_recipe_id,
                recipe_id=verb_or_recipe_id,
                status=JobStatus.WAITING_APPROVAL,
                step_label="Waiting for human approval (Suggest mode)",
                parent_asset_ids=parent_asset_ids,
                params=inputs,
                cost_estimate_usd=est_info["estimated_usd"],
                initiated_by="agent",
                agent_reason=agent_reason or f"Proposed running {verb_or_recipe_id}",
            )
            self.store.save_job(job)
            await self._emit("job_updated", {"job": job.model_dump()})
            return {"job": job.model_dump(), "assets": [], "waiting_approval": True}

        job = Job(
            project_name=self.store.project_name,
            verb=verb_or_recipe_id,
            recipe_id=verb_or_recipe_id,
            status=JobStatus.RUNNING,
            progress=5,
            eta_seconds=4,
            step_label=f"Starting {verb_or_recipe_id}...",
            parent_asset_ids=parent_asset_ids,
            params=inputs,
            cost_estimate_usd=est_info["estimated_usd"],
            started_at=time.time(),
            initiated_by=initiated_by,  # type: ignore[arg-type]
            agent_reason=agent_reason,
        )
        self.store.save_job(job)
        await self._emit("job_updated", {"job": job.model_dump()})

        run_spent_tracker = [0.0]
        created_assets: list[Asset] = []

        async def _update_progress(pct: int, label: str) -> None:
            if job.id in self._canceled_jobs:
                raise asyncio.CancelledError("Job canceled by user.")
            job.progress = min(99, max(job.progress, pct))
            job.step_label = label
            job.cost_actual_usd = run_spent_tracker[0]
            self.store.save_job(job)
            await self._emit("job_updated", {"job": job.model_dump()})

        try:
            parent_assets = [a for pid in parent_asset_ids if (a := self.store.get_asset(pid)) is not None]
            primary_parent = parent_assets[0] if parent_assets else None
            explicit_reference_selection = "reference_asset_ids" in inputs
            if explicit_reference_selection:
                selected_reference_ids = inputs.get("reference_asset_ids")
                if not isinstance(selected_reference_ids, list) or len(selected_reference_ids) > 8:
                    raise ValueError("Choose no more than 8 project image references.")
                ref_files = []
                for reference_id in selected_reference_ids:
                    reference_asset = self.store.get_asset(str(reference_id))
                    if not reference_asset or reference_asset.type != AssetType.IMAGE:
                        raise ValueError(f"Reference asset '{reference_id}' is not a project image asset.")
                    if Path(reference_asset.file_path).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                        raise ValueError(f"Reference asset '{reference_id}' is not a supported image file.")
                    reference_path = self.store.resolve_safe_path(reference_asset.file_path)
                    if not reference_path.is_file():
                        raise ValueError(f"Reference asset '{reference_id}' has no image file.")
                    if reference_path not in ref_files:
                        ref_files.append(reference_path)
                source_ref_files = list(ref_files)
            else:
                ref_files = [
                    self.store.resolve_safe_path(a.thumbnail_path if a.type == AssetType.MESH else a.file_path)
                    for a in parent_assets
                ]
                source_ref_files = list(ref_files)
                # Also attach reference anchors from Project Bible if present
                for anchor_id in bible.reference_anchor_asset_ids:
                    if anchor_id not in parent_asset_ids:
                        anchor = self.store.get_asset(anchor_id)
                        if anchor:
                            ref_files.append(self.store.resolve_safe_path(anchor.thumbnail_path or anchor.file_path))

            if verb_or_recipe_id == "variant":
                source_name = primary_parent.name.split("•")[0].strip() if primary_parent else "Fantasy Asset"
                chips = inputs.get("chips") or ["Rarity tiers"]
                if isinstance(chips, str):
                    chips = [c.strip() for c in chips.split(",") if c.strip()]
                user_prompt = str(inputs.get("prompt", ""))
                count = max(1, min(12, int(inputs.get("count", 4))))
                custom_prompts: list[str] | None = inputs.get("custom_prompts")

                expanded = build_variant_prompts(
                    source_name=source_name,
                    chips=chips,
                    user_prompt=user_prompt,
                    count=count,
                    bible=bible,
                    style_pack_ids=inputs.get("style_packs"),
                )
                sem = asyncio.Semaphore(4)

                async def _make_one_variant(idx: int, spec: dict[str, Any]) -> Asset:
                    async with sem:
                        p_full = (
                            custom_prompts[idx]
                            if custom_prompts and idx < len(custom_prompts) and custom_prompts[idx].strip()
                            else spec["prompt_full"]
                        )
                        if IDENTITY_LOCK_INSTRUCTION not in p_full:
                            p_full = f"{IDENTITY_LOCK_INSTRUCTION} {p_full}"
                        await _update_progress(
                            int(15 + (idx / count) * 70),
                            f"Generating variant {idx + 1}/{count}: {spec['tier_label']}...",
                        )
                        return await self._generate_single_asset(
                            provider_id="gemini",
                            capability=ProviderCapability.IMAGE_EDIT if ref_files else ProviderCapability.TEXT_TO_IMAGE,
                            name=spec["title"],
                            asset_type=AssetType.IMAGE,
                            verb="variant",
                            recipe_id="variant",
                            prompt_raw=user_prompt or ", ".join(chips),
                            prompt_full=p_full,
                            negative_prompt=spec["negative_prompt"],
                            parent_ids=parent_asset_ids,
                            reference_files=source_ref_files[:1],
                            chips_used=chips,
                            style_packs=spec["style_packs"],
                            params={
                                "seed": (inputs.get("seed") or 1000) + idx * 17,
                                "use_reference_image": bool(source_ref_files),
                            },
                            tags=["variant"] + [c.lower().replace(" ", "_") for c in chips],
                            initiated_by=initiated_by,
                            force_uncached=force_uncached,
                            repeat_until_critic=bool(inputs.get("repeat_until_critic", False)),
                            max_iterations=2,
                            bible=bible,
                            run_spent_tracker=run_spent_tracker,
                        )

                created_assets = list(
                    await asyncio.gather(*[_make_one_variant(i, s) for i, s in enumerate(expanded)])
                )

            elif verb_or_recipe_id == "edit_image":
                instruction = str(inputs.get("instruction") or "").strip()
                if not instruction:
                    raise ValueError("Describe the change to make.")
                if not primary_parent:
                    raise ValueError("Choose the image to edit.")
                source_rel = primary_parent.thumbnail_path if primary_parent.type == AssetType.MESH else primary_parent.file_path
                source_path = self.store.resolve_safe_path(source_rel)
                if not source_path.is_file():
                    raise ValueError("The source image file is missing.")
                if source_path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                    converted = source_path.with_name(f"{source_path.stem}_edit_source.png")
                    if not converted.is_file():
                        with Image.open(source_path) as source_image:
                            source_image.convert("RGBA").save(converted, format="PNG")
                    source_path = converted
                count = max(1, min(4, int(inputs.get("count", 1))))
                prompt_full = f"{SCALPEL_EDIT_LOCK} Change: {instruction}"
                source_name = primary_parent.name.split("•")[0].strip()

                async def _make_one_edit(idx: int) -> Asset:
                    await _update_progress(int(15 + (idx / count) * 70), f"Editing image {idx + 1}/{count}…")
                    return await self._generate_single_asset(
                        provider_id="gemini",
                        capability=ProviderCapability.IMAGE_EDIT,
                        name=f"{source_name} · {instruction[:48]}",
                        asset_type=AssetType.IMAGE,
                        verb="edit_image",
                        recipe_id="edit_image",
                        prompt_raw=instruction,
                        prompt_full=prompt_full,
                        negative_prompt="",
                        parent_ids=[primary_parent.id],
                        reference_files=[source_path],
                        chips_used=[],
                        style_packs=[],
                        params={
                            "model": str(inputs.get("model") or "gemini-nano-banana-2.1"),
                            "seed": (inputs.get("seed") or 4242) + idx * 31,
                            "use_reference_image": True,
                            "pose_mode": "",
                            "aspect_ratio": "1:1",
                            "transparent": False,
                        },
                        tags=["edit"],
                        initiated_by=initiated_by,
                        force_uncached=force_uncached,
                        bible=bible,
                        run_spent_tracker=run_spent_tracker,
                    )

                created_assets = list(await asyncio.gather(*[_make_one_edit(i) for i in range(count)]))

            elif verb_or_recipe_id == "make_3d":
                source_name = primary_parent.name if primary_parent else inputs.get("name", "3D Asset")
                mode = inputs.get("mode", "single")
                cap = (
                    ProviderCapability.MULTI_IMAGE_TO_3D
                    if (mode == "multi_image" or len(parent_assets) > 1 or (primary_parent and primary_parent.type == AssetType.TURNAROUND))
                    else ProviderCapability.IMAGE_TO_3D
                )
                prompt_raw = inputs.get("prompt") or (primary_parent.prompt_raw if primary_parent else source_name)
                prompt_full, _ = synthesize_prompt_with_bible(prompt_raw, bible)
                ast = await self._generate_single_asset(
                    provider_id="meshy",
                    capability=cap,
                    name=f"{source_name} (3D Mesh)",
                    asset_type=AssetType.MESH,
                    verb="make_3d",
                    recipe_id="make_3d",
                    prompt_raw=prompt_raw,
                    prompt_full=prompt_full,
                    parent_ids=parent_asset_ids,
                    reference_files=ref_files,
                    params={
                        **inputs,
                        "ai_model": inputs.get("ai_model", "meshy-7.1"),
                        "model_type": inputs.get("model_type", "standard"),
                        "should_texture": inputs.get("should_texture", True),
                        "enable_pbr": inputs.get("enable_pbr", True),
                        "target_polycount": int(inputs.get("target_polycount", bible.polycount_budget)),
                    },
                    tags=["3d", "mesh", "glb"],
                    initiated_by=initiated_by,
                    force_uncached=force_uncached,
                    bible=bible,
                    run_spent_tracker=run_spent_tracker,
                    progress_cb=_update_progress,
                )
                created_assets.append(ast)

            elif verb_or_recipe_id == "turnaround":
                source_name = primary_parent.name if primary_parent else "Character"
                raw_p = inputs.get("prompt") or f"Orthographic front, side, and back turnaround views of {source_name}"
                p_full, neg = synthesize_prompt_with_bible(raw_p, bible, identity_lock_subject=source_name)
                turn_ast = await self._generate_single_asset(
                    provider_id="gemini",
                    capability=ProviderCapability.IMAGE_EDIT,
                    name=f"{source_name} • Turnaround (3-View)",
                    asset_type=AssetType.TURNAROUND,
                    verb="turnaround",
                    recipe_id="turnaround",
                    prompt_raw=raw_p,
                    prompt_full=p_full,
                    negative_prompt=neg,
                    parent_ids=parent_asset_ids,
                    reference_files=ref_files[:1],
                    params={"render_mode": "turnaround"},
                    tags=["turnaround", "multi-view", "orthographic"],
                    initiated_by=initiated_by,
                    force_uncached=force_uncached,
                    bible=bible,
                    run_spent_tracker=run_spent_tracker,
                    progress_cb=_update_progress,
                )
                created_assets.append(turn_ast)

                if inputs.get("auto_make_3d"):
                    mesh_ast = await self._generate_single_asset(
                        provider_id="meshy",
                        capability=ProviderCapability.MULTI_IMAGE_TO_3D,
                        name=f"{source_name} (Multi-View 3D)",
                        asset_type=AssetType.MESH,
                        verb="make_3d",
                        recipe_id="turnaround",
                        prompt_raw=raw_p,
                        prompt_full=p_full,
                        parent_ids=[turn_ast.id],
                        reference_files=[self.store.resolve_safe_path(turn_ast.file_path)],
                        params={"ai_model": "meshy-7.1", "should_texture": True},
                        tags=["3d", "multi-view-3d", "glb"],
                        initiated_by=initiated_by,
                        force_uncached=force_uncached,
                        bible=bible,
                        run_spent_tracker=run_spent_tracker,
                        progress_cb=_update_progress,
                    )
                    created_assets.append(mesh_ast)

            elif verb_or_recipe_id == "extract_part":
                source_name = primary_parent.name if primary_parent else "Subject"
                part_desc = str(inputs.get("part_description") or inputs.get("prompt") or "winged helmet").strip()
                coords = inputs.get("click_coords", "")
                coord_clause = f" Focus around region ({coords})." if coords else ""
                raw_p = f"Extract part: {part_desc}{coord_clause}"
                p_full, neg = synthesize_prompt_with_bible(
                    f"Isolate ONLY the '{part_desc}' from '{source_name}'. Reconstruct any occluded/hidden surfaces cleanly, center the isolated part, remove the rest of the body.{coord_clause}",
                    bible,
                    identity_lock_subject=source_name,
                )
                part_ast = await self._generate_single_asset(
                    provider_id="gemini",
                    capability=ProviderCapability.IMAGE_EDIT,
                    name=f"Extracted: {part_desc.title()}",
                    asset_type=AssetType.PART,
                    verb="extract_part",
                    recipe_id="extract_part",
                    prompt_raw=raw_p,
                    prompt_full=p_full,
                    negative_prompt=neg,
                    parent_ids=parent_asset_ids,
                    reference_files=ref_files[:1],
                    params={"click_coords": coords},
                    tags=["part", "extracted", part_desc.lower().replace(" ", "_")],
                    initiated_by=initiated_by,
                    force_uncached=force_uncached,
                    repeat_until_critic=bool(inputs.get("repeat_until_critic", False)),
                    bible=bible,
                    run_spent_tracker=run_spent_tracker,
                    progress_cb=_update_progress,
                )
                created_assets.append(part_ast)

                if inputs.get("send_to_3d"):
                    part_3d = await self._generate_single_asset(
                        provider_id="meshy",
                        capability=ProviderCapability.IMAGE_TO_3D,
                        name=f"{part_desc.title()} (3D Part)",
                        asset_type=AssetType.MESH,
                        verb="make_3d",
                        recipe_id="extract_part",
                        prompt_raw=part_desc,
                        prompt_full=p_full,
                        parent_ids=[part_ast.id],
                        reference_files=[self.store.resolve_safe_path(part_ast.file_path)],
                        params={"ai_model": "meshy-7.1", "should_texture": True},
                        tags=["3d", "part", "glb"],
                        initiated_by=initiated_by,
                        force_uncached=force_uncached,
                        bible=bible,
                        run_spent_tracker=run_spent_tracker,
                        progress_cb=_update_progress,
                    )
                    created_assets.append(part_3d)

            elif verb_or_recipe_id == "kitbash":
                sec_id = inputs.get("secondary_asset_id")
                if sec_id and sec_id not in parent_asset_ids:
                    parent_asset_ids.append(sec_id)
                    sec_ast = self.store.get_asset(sec_id)
                    if sec_ast:
                        parent_assets.append(sec_ast)
                        ref_files.append(self.store.resolve_safe_path(sec_ast.thumbnail_path or sec_ast.file_path))

                names = " + ".join(a.name.split("•")[0].strip() for a in parent_assets) or "Kitbashed Asset"
                raw_p = inputs.get("prompt") or f"Kitbash fusion of {names}"
                p_full, neg = synthesize_prompt_with_bible(
                    f"Kitbash and seamlessly fuse these reference assets ({names}) into a single unified design: {raw_p}",
                    bible,
                    identity_lock_subject=names,
                )
                kb_ast = await self._generate_single_asset(
                    provider_id="gemini",
                    capability=ProviderCapability.MULTI_REFERENCE_IMAGE,
                    name=f"Kitbash: {names}",
                    asset_type=AssetType.IMAGE,
                    verb="kitbash",
                    recipe_id="kitbash",
                    prompt_raw=raw_p,
                    prompt_full=p_full,
                    negative_prompt=neg,
                    parent_ids=parent_asset_ids,
                    reference_files=ref_files,
                    tags=["kitbash", "multi-parent"],
                    initiated_by=initiated_by,
                    force_uncached=force_uncached,
                    bible=bible,
                    run_spent_tracker=run_spent_tracker,
                    progress_cb=_update_progress,
                )
                created_assets.append(kb_ast)

            elif verb_or_recipe_id == "retexture":
                source_name = primary_parent.name if primary_parent else "Asset"
                raw_p = inputs.get("prompt") or "Celestial gold and emerald rune retexture"
                p_full, neg = synthesize_prompt_with_bible(
                    f"Retexture / palette swap '{source_name}' with: {raw_p}. Preserve exact silhouette and geometry.",
                    bible,
                    identity_lock_subject=source_name,
                )
                is_mesh = primary_parent and primary_parent.type == AssetType.MESH
                ast = await self._generate_single_asset(
                    provider_id="meshy" if is_mesh else "gemini",
                    capability=ProviderCapability.RETEXTURE_3D if is_mesh else ProviderCapability.IMAGE_EDIT,
                    name=f"{source_name} • Retextured",
                    asset_type=AssetType.MESH if is_mesh else AssetType.IMAGE,
                    verb="retexture",
                    recipe_id="retexture",
                    prompt_raw=raw_p,
                    prompt_full=p_full,
                    negative_prompt=neg,
                    parent_ids=parent_asset_ids,
                    reference_files=ref_files[:1],
                    tags=["retexture", "palette-swap"],
                    initiated_by=initiated_by,
                    force_uncached=force_uncached,
                    bible=bible,
                    run_spent_tracker=run_spent_tracker,
                    progress_cb=_update_progress,
                )
                created_assets.append(ast)

            elif verb_or_recipe_id in ("icon_maker", "sprite_sheet", "tileable_texture"):
                source_name = primary_parent.name if primary_parent else inputs.get("name", "")
                raw_p = inputs.get("prompt") or source_name or f"Stylized {verb_or_recipe_id}"
                mode_map = {
                    "icon_maker": ("icon", AssetType.ICON, "Icon"),
                    "sprite_sheet": ("sprite_sheet", AssetType.SPRITE_SHEET, "Sprite Sheet"),
                    "tileable_texture": ("tileable_texture", AssetType.TEXTURE, "Tileable Texture"),
                }
                r_mode, a_type, suffix_lbl = mode_map[verb_or_recipe_id]
                style_override = ["clean_icon"] if verb_or_recipe_id == "icon_maker" else None
                if inputs.get("prompt_full"):
                    p_full = str(inputs["prompt_full"])
                    neg = str(inputs.get("negative_prompt_full") or "")
                else:
                    p_full, neg = synthesize_prompt_with_bible(
                        raw_p,
                        bible,
                        style_pack_ids=style_override,
                        identity_lock_subject=source_name if primary_parent else None,
                    )
                title_base = source_name or raw_p[:28].strip().title()
                ast = await self._generate_single_asset(
                    provider_id="gemini",
                    capability=ProviderCapability.IMAGE_EDIT if (ref_files and explicit_reference_selection) else (ProviderCapability.IMAGE_EDIT if ref_files and inputs.get("use_reference_image") else ProviderCapability.TEXT_TO_IMAGE),
                    name=f"{title_base} ({suffix_lbl})",
                    asset_type=a_type,
                    verb=verb_or_recipe_id,
                    recipe_id=verb_or_recipe_id,
                    prompt_raw=raw_p,
                    prompt_full=p_full,
                    negative_prompt=neg,
                    parent_ids=parent_asset_ids,
                    reference_files=ref_files if explicit_reference_selection else ref_files[:1],
                    params={
                        **inputs,
                        "render_mode": r_mode,
                        "transparent": verb_or_recipe_id != "tileable_texture",
                        "negative_prompt": neg,
                        "use_reference_image": bool(ref_files) if explicit_reference_selection else bool(inputs.get("use_reference_image", False)),
                    },
                    tags=[verb_or_recipe_id, r_mode],
                    initiated_by=initiated_by,
                    force_uncached=force_uncached,
                    bible=bible,
                    run_spent_tracker=run_spent_tracker,
                    progress_cb=_update_progress,
                )
                created_assets.append(ast)

            elif verb_or_recipe_id == "batch_from_list":
                raw_items = inputs.get("items") or [
                    "Crimson Healing Potion",
                    "Starlight Mana Potion",
                    "Emerald Stamina Elixir",
                ]
                if isinstance(raw_items, str):
                    raw_items = [line.strip() for line in raw_items.splitlines() if line.strip()]
                asset_kind = inputs.get("asset_kind", "image")
                top_n_3d = int(inputs.get("then_3d_top_n", 0))
                sem = asyncio.Semaphore(int(inputs.get("concurrency", 4)))

                async def _run_batch_item(idx: int, item_desc: str) -> Asset:
                    async with sem:
                        await _update_progress(
                            int(10 + (idx / max(1, len(raw_items))) * 65),
                            f"Batch ({idx + 1}/{len(raw_items)}): {item_desc[:36]}...",
                        )
                        p_full, neg = synthesize_prompt_with_bible(item_desc, bible)
                        custom_prompts = inputs.get("custom_prompts")
                        custom_negative_prompts = inputs.get("custom_negative_prompts")
                        if isinstance(custom_prompts, list) and idx < len(custom_prompts):
                            p_full = str(custom_prompts[idx])
                        if isinstance(custom_negative_prompts, list) and idx < len(custom_negative_prompts):
                            neg = str(custom_negative_prompts[idx])
                        positive_injections = inputs.get("positive_injections") or ""
                        negative_injections = inputs.get("negative_injections") or ""
                        if isinstance(positive_injections, list):
                            positive_injections = "\n".join(str(value) for value in positive_injections if value)
                        if isinstance(negative_injections, list):
                            negative_injections = ", ".join(str(value) for value in negative_injections if value)
                        if positive_injections:
                            p_full = "\n".join(part for part in (p_full, str(positive_injections)) if part)
                        if negative_injections:
                            neg = ", ".join(part for part in (neg, str(negative_injections)) if part)
                        is_3d = asset_kind == "3d_prop"
                        r_mode = "icon" if asset_kind == "icon" else "standard"
                        explicit_name = str(inputs.get("name") or "").strip()
                        if not explicit_name:
                            if "Subject:" in item_desc:
                                explicit_name = item_desc.split("Subject:", 1)[1].split("—")[0].strip()[:48]
                            else:
                                explicit_name = item_desc[:42].strip().title()
                        return await self._generate_single_asset(
                            provider_id="meshy" if is_3d else "gemini",
                            capability=ProviderCapability.TEXT_TO_3D if is_3d else (ProviderCapability.IMAGE_EDIT if ref_files and explicit_reference_selection else ProviderCapability.TEXT_TO_IMAGE),
                            name=explicit_name,
                            asset_type=AssetType.MESH if is_3d else (AssetType.ICON if asset_kind == "icon" else AssetType.IMAGE),
                            verb="batch_from_list",
                            recipe_id="batch_from_list",
                            prompt_raw=item_desc,
                            prompt_full=p_full,
                            negative_prompt=neg,
                            parent_ids=parent_asset_ids,
                            reference_files=ref_files if explicit_reference_selection else ref_files[:1],
                            params={
                                **inputs,
                                "render_mode": r_mode,
                                "seed": (inputs.get("seed") or 2000) + idx * 31,
                                "negative_prompt": neg,
                                "use_reference_image": bool(ref_files) if explicit_reference_selection else bool(inputs.get("use_reference_image", False)),
                            },
                            tags=["batch", asset_kind],
                            initiated_by=initiated_by,
                            force_uncached=force_uncached,
                            repeat_until_critic=bool(inputs.get("repeat_until_critic", False)),
                            max_iterations=2,
                            bible=bible,
                            run_spent_tracker=run_spent_tracker,
                        )

                batch_assets = list(
                    await asyncio.gather(*[_run_batch_item(i, desc) for i, desc in enumerate(raw_items)])
                )
                created_assets.extend(batch_assets)

                if top_n_3d > 0:
                    # Rank by Critic overall_score and convert top N to 3D!
                    ranked = sorted(
                        [a for a in batch_assets if a.type != AssetType.MESH],
                        key=lambda a: a.critic_report.overall_score if a.critic_report else 0.0,
                        reverse=True,
                    )[:top_n_3d]
                    for j, winner in enumerate(ranked):
                        await _update_progress(
                            80 + int((j / max(1, len(ranked))) * 15),
                            f"Making 3D for top-scored #{j + 1}: {winner.name}...",
                        )
                        m_ast = await self._generate_single_asset(
                            provider_id="meshy",
                            capability=ProviderCapability.IMAGE_TO_3D,
                            name=f"{winner.name} (3D Winner)",
                            asset_type=AssetType.MESH,
                            verb="make_3d",
                            recipe_id="batch_from_list",
                            prompt_raw=winner.prompt_raw,
                            prompt_full=winner.prompt_full,
                            parent_ids=[winner.id],
                            reference_files=[self.store.resolve_safe_path(winner.file_path)],
                            params={"ai_model": "meshy-7.1", "should_texture": True},
                            tags=["3d", "batch-winner", "glb"],
                            initiated_by=initiated_by,
                            force_uncached=force_uncached,
                            bible=bible,
                            run_spent_tracker=run_spent_tracker,
                        )
                        created_assets.append(m_ast)

            elif verb_or_recipe_id == "qc_check":
                for p_ast in parent_assets:
                    abs_p = self.store.resolve_safe_path(p_ast.file_path)
                    p_ast.qc_report = run_qc_checks(p_ast, abs_p, bible)
                    self.store.save_asset(p_ast)
                    created_assets.append(p_ast)

            else:
                # Execute custom user/agent declarative YAML recipe!
                recipe = get_recipe(verb_or_recipe_id)
                steps = recipe.get("steps", [])
                current_parents = list(parent_asset_ids)
                for s_idx, step in enumerate(steps):
                    action = step.get("action", "generate")
                    if action == "qc_check":
                        for a in created_assets:
                            a.qc_report = run_qc_checks(a, self.store.resolve_safe_path(a.file_path), bible)
                            self.store.save_asset(a)
                        continue
                    prov = step.get("provider", "gemini")
                    cap_str = step.get("capability", "text_to_image")
                    cap = ProviderCapability(cap_str) if cap_str in [c.value for c in ProviderCapability] else ProviderCapability.TEXT_TO_IMAGE
                    loop_cfg = step.get("loop") or {}
                    repeat_n = int(loop_cfg.get("repeat", 1)) if isinstance(loop_cfg.get("repeat", 1), int) else int(inputs.get("count", 1))
                    raw_p = str(inputs.get("prompt") or step.get("params", {}).get("prompt") or recipe.get("name", verb_or_recipe_id))
                    p_full, neg = synthesize_prompt_with_bible(raw_p, bible)

                    step_outputs: list[Asset] = []
                    for r_i in range(max(1, repeat_n)):
                        await _update_progress(
                            int(15 + ((s_idx + 1) / max(1, len(steps))) * 75),
                            f"Step {step.get('id', s_idx + 1)} ({r_i + 1}/{repeat_n})...",
                        )
                        ast = await self._generate_single_asset(
                            provider_id=prov,
                            capability=cap,
                            name=f"{recipe.get('name', verb_or_recipe_id)} #{r_i + 1}" if repeat_n > 1 else recipe.get("name", verb_or_recipe_id),
                            asset_type=AssetType.MESH if prov == "meshy" else AssetType.IMAGE,
                            verb=verb_or_recipe_id,
                            recipe_id=verb_or_recipe_id,
                            prompt_raw=raw_p,
                            prompt_full=p_full,
                            negative_prompt=neg,
                            parent_ids=current_parents,
                            reference_files=ref_files[:1],
                            params={"seed": 5000 + r_i * 19, **(step.get("params") or {})},
                            tags=[verb_or_recipe_id, "custom-recipe"],
                            initiated_by=initiated_by,
                            force_uncached=force_uncached,
                            repeat_until_critic=loop_cfg.get("repeat_until") == "critic_passes",
                            max_iterations=int(loop_cfg.get("max_iterations", 2)),
                            bible=bible,
                            run_spent_tracker=run_spent_tracker,
                        )
                        step_outputs.append(ast)
                    created_assets.extend(step_outputs)
                    if step_outputs:
                        current_parents = [a.id for a in step_outputs]

            job.status = JobStatus.SUCCEEDED
            job.progress = 100
            job.step_label = f"Completed ({len(created_assets)} asset{'s' if len(created_assets) != 1 else ''})"
            job.output_asset_ids = [a.id for a in created_assets]
            job.cost_actual_usd = run_spent_tracker[0]
            job.finished_at = time.time()
            self.store.save_job(job)

            self.store.log_activity(
                ActivityLogEntry(
                    actor="agent" if initiated_by == "agent" else "user",
                    action=f"run_{verb_or_recipe_id}",
                    reason=agent_reason or f"Ran {verb_or_recipe_id} creating {len(created_assets)} asset(s)",
                    job_id=job.id,
                    created_asset_ids=[a.id for a in created_assets if a.id not in parent_asset_ids],
                    cost_usd=run_spent_tracker[0],
                )
            )
            await self._emit(
                "job_completed",
                {
                    "job": job.model_dump(),
                    "assets": [a.model_dump() for a in created_assets],
                    "spend": self.store.get_spend_summary(),
                },
            )
            return {
                "job": job.model_dump(),
                "assets": [a.model_dump() for a in created_assets],
                "spend": self.store.get_spend_summary(),
            }

        except BudgetExceededError as b_err:
            job.status = JobStatus.PAUSED_BUDGET
            job.error_message = b_err.message
            job.step_label = "Paused by Hard Budget Guard"
            job.output_asset_ids = [a.id for a in created_assets]
            job.cost_actual_usd = run_spent_tracker[0]
            self.store.save_job(job)
            await self._emit("job_updated", {"job": job.model_dump()})
            return {
                "job": job.model_dump(),
                "assets": [a.model_dump() for a in created_assets],
                "budget_paused": True,
                "error": b_err.message,
            }
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELED
            job.step_label = "Canceled"
            job.output_asset_ids = [a.id for a in created_assets]
            self.store.save_job(job)
            await self._emit("job_updated", {"job": job.model_dump()})
            return {"job": job.model_dump(), "assets": [a.model_dump() for a in created_assets], "canceled": True}
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error_message = str(exc)
            job.step_label = f"Failed: {str(exc)[:80]}"
            job.output_asset_ids = [a.id for a in created_assets]
            job.finished_at = time.time()
            self.store.save_job(job)
            await self._emit("job_updated", {"job": job.model_dump()})
            raise
