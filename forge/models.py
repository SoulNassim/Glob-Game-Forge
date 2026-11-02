"""Core data models for Forge."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


def new_id(prefix: str = "ast") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class AssetType(str, Enum):
    IMAGE = "image"
    MESH = "mesh"
    SPRITE_SHEET = "sprite_sheet"
    TEXTURE = "texture"
    PART = "part"
    ICON = "icon"
    TURNAROUND = "turnaround"
    AUDIO = "audio"


class AssetStatus(str, Enum):
    NONE = "none"
    FAVORITE = "favorite"
    REJECTED = "rejected"


class ApprovalState(str, Enum):
    APPROVED = "approved"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"


class AutonomyMode(str, Enum):
    SUGGEST = "suggest"          # Agent proposes, human approves before running
    CHECKPOINTS = "checkpoints"  # Agent runs, asks at defined milestones
    AUTOPILOT = "autopilot"      # Agent runs autonomously within budget


class QCReport(BaseModel):
    passed: bool = True
    checks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    summary: str = "All checks passed"
    timestamp: float = Field(default_factory=time.time)


class CriticReport(BaseModel):
    passed: bool = True
    overall_score: float = 0.92
    scores: dict[str, float] = Field(
        default_factory=lambda: {
            "single_object": 0.95,
            "clean_background": 0.95,
            "on_style": 0.90,
            "identity_match": 0.90,
            "no_artifacts": 0.92,
        }
    )
    feedback: str = "Matches project bible and maintains clear silhouette."
    action_recommendation: Literal["keep", "retry", "branch", "escalate"] = "keep"
    provider: str = "gemini"
    model: str = "gemini-2.5-flash"


class Asset(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ast"))
    name: str = "Untitled Asset"
    type: AssetType = AssetType.IMAGE
    file_path: str = ""          # Relative to project root, e.g. "assets/ast_123.png"
    thumbnail_path: str = ""     # Relative to project root, e.g. "assets/ast_123_thumb.png"
    extra_files: dict[str, str] = Field(default_factory=dict)  # e.g. {"obj": "...", "fbx": "...", "pre_remeshed_glb": "..."}
    parent_ids: list[str] = Field(default_factory=list)        # Lineage DAG parents
    verb: str = "upload"         # Verb or recipe id that produced this asset
    recipe_id: str = ""
    prompt_raw: str = ""         # User input prompt
    prompt_full: str = ""        # Full synthesized prompt including injected presets, bible & style packs
    negative_prompt: str = ""
    chips_used: list[str] = Field(default_factory=list)
    style_packs: list[str] = Field(default_factory=list)
    provider: str = "local"
    model: str = ""
    seed: int | None = None
    cost_usd: float = 0.0
    cost_estimated: bool = True
    cached_hit: bool = False
    created_at: float = Field(default_factory=time.time)
    tags: list[str] = Field(default_factory=list)
    status: AssetStatus = AssetStatus.NONE
    approval_state: ApprovalState = ApprovalState.APPROVED
    note: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)     # Resolution, polycount, views, item_id, etc.
    qc_report: QCReport | None = None
    critic_report: CriticReport | None = None
    archived: bool = False       # Soft-delete / undo flag (never mutate or hard-destroy)


class StylePack(BaseModel):
    id: str
    name: str
    description: str
    prompt_prefix: str = ""
    prompt_suffix: str = ""
    negative_prompt: str = ""
    reference_images: list[str] = Field(default_factory=list)
    provider_settings: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    palette_hex: list[str] = Field(default_factory=list)
    builtin: bool = True


class ProjectBible(BaseModel):
    project_name: str = "default"
    display_name: str = ""
    art_style: str = ""
    genre: str = ""
    tone: str = ""
    lore: str = ""
    custom_sections: list[dict[str, str]] = Field(default_factory=list)
    lore_keywords: list[str] = Field(default_factory=list)
    camera_perspective: str = ""
    project_dimension: Literal["3d", "2d", "hybrid"] = "3d"
    scale_benchmarks: list[dict[str, Any]] = Field(default_factory=list)
    global_style_tokens: dict[str, Any] = Field(default_factory=dict)
    palette: list[str] = Field(default_factory=lambda: ["#2B2D42", "#8D99AE", "#EF233C", "#FFD166", "#06D6A0", "#118AB2"])
    naming_scheme: str = "{category}_{name}_{variant}"
    polycount_budget: int = 12000
    texture_resolution: str = "2k"
    unit_scale_meters: float = 1.0
    character_height_units: float = 1.8
    target_engine: Literal["godot", "unity", "unreal", "custom", "generic"] = "godot"
    background_rule: Literal["transparent", "solid_neutral", "none"] = "transparent"
    default_style_packs: list[str] = Field(default_factory=list)
    locked_style_packs: bool = False
    spend_cap_usd: float = 10.00
    per_run_cap_usd: float = 2.50
    autonomy_mode: AutonomyMode = AutonomyMode.AUTOPILOT
    watch_folder_enabled: bool = False
    watch_default_recipe: str = "make_3d"
    reference_anchor_asset_ids: list[str] = Field(default_factory=list)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_BUDGET = "paused_budget"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class Job(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    project_name: str = "default"
    verb: str
    recipe_id: str = ""
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    eta_seconds: int = 0
    step_label: str = "Queued"
    parent_asset_ids: list[str] = Field(default_factory=list)
    output_asset_ids: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    provider: str = ""
    model: str = ""
    retries: int = 0
    max_retries: int = 3
    cost_estimate_usd: float = 0.0
    cost_actual_usd: float = 0.0
    error_message: str = ""
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    initiated_by: Literal["user", "agent", "watch_folder"] = "user"
    agent_reason: str = ""


class ActivityLogEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("act"))
    timestamp: float = Field(default_factory=time.time)
    actor: Literal["user", "agent", "system", "critic"] = "user"
    action: str
    reason: str = ""
    job_id: str = ""
    created_asset_ids: list[str] = Field(default_factory=list)
    affected_asset_ids: list[str] = Field(default_factory=list)
    previous_state: dict[str, Any] = Field(default_factory=dict)
    undone: bool = False
    cost_usd: float = 0.0


# --- Section 14: Game Scan & Game Profile models ---

class EvidenceField(BaseModel):
    value: Any
    confidence: float = 0.85  # 0.0 to 1.0 (< 0.65 highlighted as low confidence)
    evidence: list[str] = Field(default_factory=list)  # e.g. ["project.godot:14", "scenes/player.tscn:22"]
    notes: str = ""


class CatalogEntry(BaseModel):
    id: str
    name: str
    category: Literal["item", "enemy", "npc", "prop", "icon", "environment"] = "item"
    description: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)
    source_file: str = ""
    existing_asset_path: str = ""
    has_missing_art: bool = False
    is_placeholder: bool = False


class WishlistItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("wish"))
    title: str
    category: str = "prop"
    description: str = ""
    source_type: Literal["missing_ref", "placeholder", "todo_comment", "catalog_gap"] = "missing_ref"
    target_res_path: str = ""       # e.g. "res://assets/items/iron_sword.glb"
    evidence: list[str] = Field(default_factory=list)
    usage_count: int = 1
    priority_score: float = 1.0     # Ranked by usage frequency and scene centrality
    suggested_verb: str = "make_3d"
    catalog_entry_id: str = ""
    status: Literal["open", "queued", "generated", "written_back"] = "open"
    generated_asset_id: str = ""


class GameProfile(BaseModel):
    scan_id: str = Field(default_factory=lambda: new_id("scan"))
    game_path: str = ""
    engine: str = "godot"
    engine_version: str = "4.3"
    scanned_at: float = Field(default_factory=time.time)
    approved_by_user: bool = False
    files_read_log: list[str] = Field(default_factory=list)

    game_title: EvidenceField = Field(default_factory=lambda: EvidenceField(value="Unknown Game", confidence=0.5))
    summary: EvidenceField = Field(default_factory=lambda: EvidenceField(value="", confidence=0.5))
    genre: EvidenceField = Field(default_factory=lambda: EvidenceField(value="Action RPG", confidence=0.7))
    tone: EvidenceField = Field(default_factory=lambda: EvidenceField(value="Stylized Fantasy", confidence=0.7))
    setting_and_lore: EvidenceField = Field(default_factory=lambda: EvidenceField(value=[], confidence=0.7))
    camera_and_perspective: EvidenceField = Field(default_factory=lambda: EvidenceField(value="Isometric 3D", confidence=0.8))
    art_style: EvidenceField = Field(default_factory=lambda: EvidenceField(value="Hand-painted low-poly", confidence=0.8))
    palette: EvidenceField = Field(default_factory=lambda: EvidenceField(value=["#2B2D42", "#EF233C", "#FFD166"], confidence=0.85))
    scale_and_units: EvidenceField = Field(default_factory=lambda: EvidenceField(value={"unit_meters": 1.0, "char_height": 1.8}, confidence=0.85))
    budgets: EvidenceField = Field(default_factory=lambda: EvidenceField(value={"max_tris": 10000, "texture_size": "2k"}, confidence=0.8))
    naming_and_folders: EvidenceField = Field(default_factory=lambda: EvidenceField(value={"pattern": "snake_case", "models_dir": "res://assets/models", "icons_dir": "res://assets/icons"}, confidence=0.9))

    auto_style_pack: StylePack | None = None
    catalog: list[CatalogEntry] = Field(default_factory=list)
    wishlist: list[WishlistItem] = Field(default_factory=list)
    placeholders: list[WishlistItem] = Field(default_factory=list)
    drift_warnings: list[str] = Field(default_factory=list)
    diff_from_previous: dict[str, Any] = Field(default_factory=dict)
