"""Portable per-project storage engine with SQLite + JSON manifest sync, DAG lineage,
provider call caching, resumable jobs, activity log, and undo support.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any
from PIL import Image

from forge.config import get_projects_root
from forge.models import (
    ActivityLogEntry,
    ApprovalState,
    Asset,
    AssetStatus,
    AssetType,
    CriticReport,
    GameProfile,
    Job,
    JobStatus,
    ProjectBible,
    QCReport,
)
from forge.storage.demo_assets import generate_demo_image
from forge.storage.glb_generator import generate_demo_glb


def sanitize_project_name(name: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name.strip())
    return cleaned or "default"


# A project can live in its game's folder (`<game>/glob/`). Its entry `<projects>/<name>/` then keeps
# this file pointing there, next to game_link.json; everything else is in the game folder.
HOME_FILE = "home.json"
GAME_HOME = "glob"


def project_home(project_name: str, root_dir: Path | None = None) -> Path:
    """Where a project's files are: its game folder's `glob/` when it has one, else `<projects>/<name>/`."""
    entry = (root_dir or get_projects_root()) / project_name
    try:
        folder = Path(json.loads((entry / HOME_FILE).read_text(encoding="utf-8"))["folder"])
    except (OSError, ValueError, KeyError, TypeError):
        return entry
    # A game folder that is gone (moved, unplugged drive) is never recreated.
    return folder if folder.is_dir() else entry


def home_owner(game_folder: Path, root_dir: Path | None = None) -> str | None:
    """The project already made for this game: living in its `glob/` folder, or linked to it from elsewhere."""
    from forge.game_link import game_folder as linked_game

    target, game = (game_folder / GAME_HOME).resolve(), game_folder.resolve()
    for name in list_all_projects(root_dir):
        linked = linked_game(name, root_dir)
        if project_home(name, root_dir).resolve() == target or (linked and linked.resolve() == game):
            return name
    return None


def home_for(project_name: str, game_folder: Path, home_folder: Path | None = None) -> Path:
    """Where a new project's files go: `<game>/glob/` by default. A chosen folder holds them in a `<name>` subfolder,
    unless it already is a Glob project folder (it has a manifest.db), which is then used as it is."""
    if home_folder is None:
        return game_folder / GAME_HOME
    return home_folder if (home_folder / "manifest.db").exists() else home_folder / project_name


GAME_IGNORE = (
    "# Glob's own records; workspace/, vault/ and ASSETS.md are your project.\n"
    "manifest.db*\nstudio_jobs.json\nlogs/\ninbox_watch/\nassets/\n"
)


def write_home_ignore(home: Path) -> None:
    """Keep Glob's internal files out of the game's git. An existing .gitignore is never overwritten."""
    ignore = home / ".gitignore"
    if not ignore.exists():
        ignore.write_text(GAME_IGNORE, encoding="utf-8")


def make_home(project_name: str, game_folder: Path, root_dir: Path | None = None, home_folder: Path | None = None) -> Path:
    """Put a project's files in its home (see `home_for`) and point its entry there."""
    home = home_for(project_name, game_folder, home_folder)
    home.mkdir(parents=True, exist_ok=True)
    if home.resolve().is_relative_to(game_folder.resolve()):
        # Godot imports every file under res:// unless the folder has a .gdignore.
        (home / ".gdignore").touch()
        write_home_ignore(home)
    entry = (root_dir or get_projects_root()) / project_name
    entry.mkdir(parents=True, exist_ok=True)
    (entry / HOME_FILE).write_text(json.dumps({"folder": str(home.resolve())}, indent=2), encoding="utf-8")
    return home


class ProjectStore:
    def __init__(self, project_name: str = "default", root_dir: Path | None = None) -> None:
        self.project_name = sanitize_project_name(project_name)
        self.base_root = root_dir or get_projects_root()
        # Security: the entry must sit directly in base_root; its home may be a game folder.
        if (self.base_root / self.project_name).resolve().parent != self.base_root.resolve():
            raise ValueError("Invalid project path traversal attempt.")
        self.project_dir = project_home(self.project_name, self.base_root).resolve()

        self.assets_dir = self.project_dir / "assets"
        self.exports_dir = self.project_dir / "exports"
        self.logs_dir = self.project_dir / "logs"
        self.watch_dir = self.project_dir / "inbox_watch"
        self.project_json_path = self.project_dir / "project.json"
        self.manifest_json_path = self.project_dir / "manifest.json"
        self.db_path = self.project_dir / "manifest.db"

        for d in (self.project_dir, self.assets_dir, self.exports_dir, self.logs_dir, self.watch_dir):
            d.mkdir(parents=True, exist_ok=True)

        self._init_db()
        if not self.project_json_path.exists():
            bible = ProjectBible(project_name=self.project_name)
            self.save_bible(bible)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    created_at REAL,
                    archived INTEGER DEFAULT 0,
                    data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT,
                    created_at REAL,
                    data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activity_log (
                    id TEXT PRIMARY KEY,
                    timestamp REAL,
                    undone INTEGER DEFAULT 0,
                    data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_cache (
                    cache_key TEXT PRIMARY KEY,
                    created_at REAL,
                    response_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS game_profile (
                    id TEXT PRIMARY KEY,
                    updated_at REAL,
                    data_json TEXT NOT NULL
                );
                """
            )

    def resolve_safe_path(self, rel_path: str) -> Path:
        """Resolve a relative path within the project folder safely."""
        candidate = (self.project_dir / rel_path).resolve()
        if not str(candidate).startswith(str(self.project_dir)):
            raise ValueError(f"Path traversal blocked: {rel_path}")
        return candidate

    # --- Project Bible ---

    def get_bible(self) -> ProjectBible:
        if self.project_json_path.exists():
            try:
                raw = json.loads(self.project_json_path.read_text(encoding="utf-8"))
                return ProjectBible(**raw.get("bible", raw))
            except Exception:
                pass
        bible = ProjectBible(project_name=self.project_name)
        self.save_bible(bible)
        return bible

    def save_bible(self, bible: ProjectBible) -> ProjectBible:
        bible.project_name = self.project_name
        payload = {"bible": bible.model_dump(), "updated_at": time.time()}
        self.project_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return bible

    # --- Assets & Lineage DAG ---

    def save_asset(self, asset: Asset, sync_json: bool = True) -> Asset:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO assets (id, created_at, archived, data_json)
                VALUES (?, ?, ?, ?)
                """,
                (asset.id, asset.created_at, 1 if asset.archived else 0, asset.model_dump_json()),
            )
        if sync_json:
            self._sync_manifest_json()
        return asset

    def get_asset(self, asset_id: str) -> Asset | None:
        with self._conn() as conn:
            row = conn.execute("SELECT data_json FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if not row:
                return None
            return Asset.model_validate_json(row["data_json"])

    def list_assets(
        self,
        include_archived: bool = False,
        asset_type: str | None = None,
        status: str | None = None,
        approval_state: str | None = None,
        tag: str | None = None,
    ) -> list[Asset]:
        query = "SELECT data_json FROM assets"
        if not include_archived:
            query += " WHERE archived = 0"
        query += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(query).fetchall()
        assets = [Asset.model_validate_json(r["data_json"]) for r in rows]
        if asset_type:
            assets = [a for a in assets if a.type.value == asset_type]
        if status:
            assets = [a for a in assets if a.status.value == status]
        if approval_state:
            assets = [a for a in assets if a.approval_state.value == approval_state]
        if tag:
            assets = [a for a in assets if tag.lower() in [t.lower() for t in a.tags]]
        return assets

    def get_lineage_graph(self) -> dict[str, Any]:
        """Return nodes and DAG edges for all active assets in the project."""
        assets = self.list_assets(include_archived=False)
        node_ids = {a.id for a in assets}
        nodes = []
        edges = []
        for a in assets:
            nodes.append(
                {
                    "id": a.id,
                    "name": a.name,
                    "type": a.type.value,
                    "verb": a.verb,
                    "thumbnail_path": a.thumbnail_path or a.file_path,
                    "file_path": a.file_path,
                    "status": a.status.value,
                    "approval_state": a.approval_state.value,
                    "cost_usd": a.cost_usd,
                    "created_at": a.created_at,
                    "parent_ids": a.parent_ids,
                }
            )
            for pid in a.parent_ids:
                if pid in node_ids:
                    edges.append({"from": pid, "to": a.id, "verb": a.verb})
        return {"nodes": nodes, "edges": edges}

    def ingest_uploaded_file(
        self,
        filename: str,
        content: bytes,
        name: str | None = None,
        tags: list[str] | None = None,
        note: str = "",
    ) -> Asset:
        ext = Path(filename).suffix.lower() or ".png"
        asset_id = f"ast_{hashlib.sha256(content + str(time.time()).encode()).hexdigest()[:10]}"
        asset_type = AssetType.MESH if ext in (".glb", ".gltf", ".obj", ".fbx") else AssetType.IMAGE
        rel_file = f"assets/{asset_id}{ext}"
        abs_file = self.resolve_safe_path(rel_file)
        abs_file.write_bytes(content)

        rel_thumb = rel_file
        metadata: dict[str, Any] = {"original_filename": filename, "size_bytes": len(content)}

        if asset_type == AssetType.IMAGE:
            try:
                img = Image.open(abs_file).convert("RGBA")
                metadata["width"], metadata["height"] = img.size
                thumb_rel = f"assets/{asset_id}_thumb.png"
                thumb_abs = self.resolve_safe_path(thumb_rel)
                thumb = img.copy()
                thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
                thumb.save(thumb_abs, format="PNG")
                rel_thumb = thumb_rel
            except Exception:
                pass
        else:
            # Generate a 2D preview thumbnail for uploaded 3D mesh
            thumb_rel = f"assets/{asset_id}_thumb.png"
            generate_demo_image(self.resolve_safe_path(thumb_rel), prompt=filename)
            rel_thumb = thumb_rel

        asset = Asset(
            id=asset_id,
            name=name or Path(filename).stem.replace("_", " ").title(),
            type=asset_type,
            file_path=rel_file,
            thumbnail_path=rel_thumb,
            verb="upload",
            prompt_raw=f"Uploaded file: {filename}",
            prompt_full=f"Uploaded file: {filename}",
            provider="local",
            tags=tags or ["uploaded"],
            note=note,
            metadata=metadata,
        )
        self.save_asset(asset)
        self.log_activity(
            ActivityLogEntry(
                actor="user",
                action="upload_asset",
                reason=f"Uploaded {filename}",
                created_asset_ids=[asset.id],
            )
        )
        return asset

    # --- Caching by (inputs + prompt + model + seed) ---

    @staticmethod
    def compute_cache_key(
        provider: str,
        model: str,
        prompt: str,
        parent_ids: list[str],
        seed: int | None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        payload = {
            "provider": provider,
            "model": model,
            "prompt": prompt.strip(),
            "parent_ids": sorted(parent_ids),
            "seed": seed,
            "extra": extra or {},
        }
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_cached_result(self, cache_key: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT response_json FROM provider_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if not row:
                return None
            return json.loads(row["response_json"])

    def set_cached_result(self, cache_key: str, data: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO provider_cache (cache_key, created_at, response_json) VALUES (?, ?, ?)",
                (cache_key, time.time(), json.dumps(data)),
            )

    # --- Jobs & Spend Calculation ---

    def save_job(self, job: Job) -> Job:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (id, status, created_at, data_json) VALUES (?, ?, ?, ?)",
                (job.id, job.status.value, job.created_at, job.model_dump_json()),
            )
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self._conn() as conn:
            row = conn.execute("SELECT data_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return Job.model_validate_json(row["data_json"]) if row else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data_json FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Job.model_validate_json(r["data_json"]) for r in rows]

    def get_spend_summary(self) -> dict[str, float]:
        bible = self.get_bible()
        assets = self.list_assets(include_archived=True)
        spent = round(sum(a.cost_usd for a in assets if not a.cached_hit), 4)
        return {
            "spent_usd": spent,
            "spend_cap_usd": bible.spend_cap_usd,
            "per_run_cap_usd": bible.per_run_cap_usd,
            "remaining_usd": round(max(0.0, bible.spend_cap_usd - spent), 4),
        }

    # --- Activity Log & Undo ---

    def log_activity(self, entry: ActivityLogEntry) -> ActivityLogEntry:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO activity_log (id, timestamp, undone, data_json) VALUES (?, ?, ?, ?)",
                (entry.id, entry.timestamp, 1 if entry.undone else 0, entry.model_dump_json()),
            )
        # Also append to readable log file
        log_file = self.logs_dir / "activity.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        return entry

    def list_activity(self, limit: int = 100) -> list[ActivityLogEntry]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data_json FROM activity_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [ActivityLogEntry.model_validate_json(r["data_json"]) for r in rows]

    def undo_activity(self, activity_id: str | None = None) -> dict[str, Any]:
        """Undo a specific activity entry (or the most recent non-undone entry)."""
        entries = self.list_activity(limit=50)
        target: ActivityLogEntry | None = None
        for e in entries:
            if e.undone:
                continue
            if activity_id is None or e.id == activity_id:
                target = e
                break
        if not target:
            return {"undone": False, "message": "Nothing to undo."}

        # Archive any assets created by that action
        for aid in target.created_asset_ids:
            ast = self.get_asset(aid)
            if ast:
                ast.archived = True
                self.save_asset(ast, sync_json=False)

        # Restore previous state on affected assets
        for aid, prev in target.previous_state.items():
            ast = self.get_asset(aid)
            if ast and isinstance(prev, dict):
                for k, v in prev.items():
                    if hasattr(ast, k):
                        setattr(ast, k, v)
                self.save_asset(ast, sync_json=False)

        target.undone = True
        self.log_activity(target)
        self._sync_manifest_json()
        return {
            "undone": True,
            "activity_id": target.id,
            "action": target.action,
            "archived_assets": target.created_asset_ids,
        }

    # --- Game Profile Storage ---

    def save_game_profile(self, profile: GameProfile) -> GameProfile:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO game_profile (id, updated_at, data_json) VALUES (?, ?, ?)",
                ("current", time.time(), profile.model_dump_json()),
            )
        (self.project_dir / "game_profile.json").write_text(
            profile.model_dump_json(indent=2), encoding="utf-8"
        )
        return profile

    def get_game_profile(self) -> GameProfile | None:
        with self._conn() as conn:
            row = conn.execute("SELECT data_json FROM game_profile WHERE id = 'current'").fetchone()
            if row:
                return GameProfile.model_validate_json(row["data_json"])
        gp_file = self.project_dir / "game_profile.json"
        if gp_file.exists():
            return GameProfile.model_validate_json(gp_file.read_text(encoding="utf-8"))
        return None

    def _sync_manifest_json(self) -> None:
        assets = self.list_assets(include_archived=True)
        payload = {
            "project_name": self.project_name,
            "updated_at": time.time(),
            "asset_count": len([a for a in assets if not a.archived]),
            "assets": [a.model_dump() for a in assets],
        }
        self.manifest_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # --- Starter Demo Project Seeding ---

    def ensure_demo_seed_assets(self) -> None:
        """If the project has zero assets, populate a playful, connected lineage tree of demo assets."""
        if len(self.list_assets(include_archived=True)) > 0:
            return

        bible = self.get_bible()
        now = time.time() - 300

        # 1. Root Character Image: Arcane Clockwork Knight
        hero_id = "ast_hero_root"
        hero_img = f"assets/{hero_id}.png"
        hero_meta = generate_demo_image(
            self.resolve_safe_path(hero_img),
            prompt="Stylized fantasy knight hero in emerald and gold armor",
            palette=bible.palette,
            seed=101,
        )
        hero = Asset(
            id=hero_id,
            name="Aethelgard Knight (Base)",
            type=AssetType.IMAGE,
            file_path=hero_img,
            thumbnail_path=f"assets/{hero_meta['thumb_name']}",
            verb="upload",
            prompt_raw="Stylized fantasy knight hero in emerald and gold armor, front 3/4 view",
            prompt_full="Stylized fantasy knight hero in emerald and gold armor, front 3/4 view, transparent background",
            provider="demo",
            model="gemini-2.5-flash-image",
            seed=101,
            cost_usd=0.0,
            created_at=now,
            tags=["character", "hero", "knight"],
            status=AssetStatus.FAVORITE,
            metadata=hero_meta,
            qc_report=QCReport(passed=True, summary="Transparent BG, single subject verified"),
            critic_report=CriticReport(overall_score=0.95, feedback="Strong silhouette and clear palette anchor."),
        )
        self.save_asset(hero, sync_json=False)

        # 2. Variants of Hero (Rare Frost & Legendary Solar)
        v1_id = "ast_hero_var_frost"
        v1_img = f"assets/{v1_id}.png"
        v1_meta = generate_demo_image(
            self.resolve_safe_path(v1_img),
            prompt="Stylized fantasy knight hero rare frost winter crystal armor",
            palette=bible.palette,
            seed=202,
        )
        v1 = Asset(
            id=v1_id,
            name="Aethelgard Knight • Frost Rare",
            type=AssetType.IMAGE,
            file_path=v1_img,
            thumbnail_path=f"assets/{v1_meta['thumb_name']}",
            parent_ids=[hero_id],
            verb="variant",
            recipe_id="variant",
            prompt_raw="Frost crystal trim",
            prompt_full="Preserve subject identity and proportions. Apply Rarity Tier: Rare (glacial cyan glow, frost filigree) + Season: Winter.",
            chips_used=["Rarity tiers", "Seasons"],
            style_packs=["hand_painted"],
            provider="demo",
            model="gemini-2.5-flash-image",
            seed=202,
            cost_usd=0.039,
            created_at=now + 30,
            tags=["character", "variant", "rare", "frost"],
            metadata=v1_meta,
            critic_report=CriticReport(overall_score=0.93, feedback="Identity locked; frost palette reads cleanly."),
        )
        self.save_asset(v1, sync_json=False)

        v2_id = "ast_hero_var_legendary"
        v2_img = f"assets/{v2_id}.png"
        v2_meta = generate_demo_image(
            self.resolve_safe_path(v2_img),
            prompt="Stylized fantasy knight hero legendary gold solar aura armor",
            palette=bible.palette,
            seed=203,
        )
        v2 = Asset(
            id=v2_id,
            name="Aethelgard Knight • Solar Legendary",
            type=AssetType.IMAGE,
            file_path=v2_img,
            thumbnail_path=f"assets/{v2_meta['thumb_name']}",
            parent_ids=[hero_id],
            verb="variant",
            recipe_id="variant",
            prompt_raw="Solar exalted gold tier",
            prompt_full="Preserve subject identity and proportions. Apply Rarity Tier: Legendary (radiant gold crest, luminous aura, ornate runes).",
            chips_used=["Rarity tiers", "Material"],
            style_packs=["hand_painted"],
            provider="demo",
            model="gemini-2.5-flash-image",
            seed=203,
            cost_usd=0.039,
            created_at=now + 35,
            tags=["character", "variant", "legendary"],
            status=AssetStatus.FAVORITE,
            metadata=v2_meta,
            critic_report=CriticReport(overall_score=0.97, feedback="Excellent silhouette retention with legendary gold read."),
        )
        self.save_asset(v2, sync_json=False)

        # 3. Extracted Part: Crested Helmet from Hero
        helm_id = "ast_part_helmet"
        helm_img = f"assets/{helm_id}.png"
        helm_meta = generate_demo_image(
            self.resolve_safe_path(helm_img),
            prompt="Extract part: winged crested knight helmet gold and crimson plume",
            palette=bible.palette,
            seed=301,
        )
        helm = Asset(
            id=helm_id,
            name="Extracted: Winged Crest Helmet",
            type=AssetType.PART,
            file_path=helm_img,
            thumbnail_path=f"assets/{helm_meta['thumb_name']}",
            parent_ids=[v2_id],
            verb="extract_part",
            recipe_id="extract_part",
            prompt_raw="Pull out the winged helmet",
            prompt_full="Isolate part 'winged helmet' from reference, reconstruct occluded bottom rim, center on transparent background.",
            provider="demo",
            model="gemini-2.5-flash-image",
            seed=301,
            cost_usd=0.039,
            created_at=now + 70,
            tags=["part", "helmet", "armor"],
            metadata=helm_meta,
        )
        self.save_asset(helm, sync_json=False)

        # 4. 3D Mesh from Legendary Knight
        mesh_id = "ast_hero_3d"
        mesh_glb = f"assets/{mesh_id}.glb"
        mesh_stats = generate_demo_glb(
            self.resolve_safe_path(mesh_glb),
            prompt="Stylized fantasy knight hero character",
            palette=["#06D6A0", "#FFD166", "#EF233C"],
        )
        mesh = Asset(
            id=mesh_id,
            name="Aethelgard Knight (3D Mesh)",
            type=AssetType.MESH,
            file_path=mesh_glb,
            thumbnail_path=v2.thumbnail_path,
            extra_files={"obj": f"assets/{mesh_id}.obj"},
            parent_ids=[v2_id],
            verb="make_3d",
            recipe_id="make_3d",
            prompt_raw="Make 3D with PBR textures",
            prompt_full="Meshy 7.1 Image-to-3D, smart-topology, target 12000 tris, PBR enabled",
            provider="demo",
            model="meshy-7.1",
            seed=401,
            cost_usd=0.25,
            created_at=now + 110,
            tags=["3d", "character", "glb"],
            status=AssetStatus.FAVORITE,
            metadata=mesh_stats,
            qc_report=QCReport(
                passed=True,
                summary=f"{mesh_stats['triangle_count']} tris (within {bible.polycount_budget} budget)",
            ),
        )
        self.save_asset(mesh, sync_json=False)

        # 5. Alchemical Emerald Potion + 3D Mesh
        pot_id = "ast_potion_base"
        pot_img = f"assets/{pot_id}.png"
        pot_meta = generate_demo_image(
            self.resolve_safe_path(pot_img),
            prompt="Arcane emerald stamina potion flask with gold collar",
            palette=bible.palette,
            seed=501,
        )
        pot = Asset(
            id=pot_id,
            name="Emerald Stamina Elixir",
            type=AssetType.IMAGE,
            file_path=pot_img,
            thumbnail_path=f"assets/{pot_meta['thumb_name']}",
            verb="batch_from_list",
            recipe_id="batch_from_list",
            prompt_raw="Emerald stamina potion flask with ornate brass collar",
            prompt_full="Emerald stamina potion flask with ornate brass collar, hand-painted isometric style, transparent background",
            provider="demo",
            model="gemini-2.5-flash-image",
            seed=501,
            cost_usd=0.039,
            created_at=now + 140,
            tags=["item", "potion", "alchemy"],
            metadata=pot_meta,
        )
        self.save_asset(pot, sync_json=False)

        pot3d_id = "ast_potion_3d"
        pot3d_glb = f"assets/{pot3d_id}.glb"
        pot3d_stats = generate_demo_glb(
            self.resolve_safe_path(pot3d_glb),
            prompt="Arcane emerald potion flask",
            palette=["#06D6A0", "#FFD166", "#118AB2"],
        )
        pot3d = Asset(
            id=pot3d_id,
            name="Emerald Stamina Elixir (3D Prop)",
            type=AssetType.MESH,
            file_path=pot3d_glb,
            thumbnail_path=pot.thumbnail_path,
            extra_files={"obj": f"assets/{pot3d_id}.obj"},
            parent_ids=[pot_id],
            verb="make_3d",
            recipe_id="make_3d",
            prompt_raw="Make 3D prop",
            prompt_full="Meshy 7.1 Image-to-3D prop mesh",
            provider="demo",
            model="meshy-7.1",
            seed=502,
            cost_usd=0.20,
            created_at=now + 170,
            tags=["3d", "prop", "potion"],
            metadata=pot3d_stats,
        )
        self.save_asset(pot3d, sync_json=False)

        # 6. Pending-approval Agent Asset in Approval Inbox so user can try swiping right away!
        inbox_id = "ast_agent_inbox_sword"
        inbox_img = f"assets/{inbox_id}.png"
        inbox_meta = generate_demo_image(
            self.resolve_safe_path(inbox_img),
            prompt="Legendary arcane rune blade sword glowing purple",
            palette=bible.palette,
            seed=601,
        )
        inbox_asset = Asset(
            id=inbox_id,
            name="Voidforged Runeblade (Agent Proposal)",
            type=AssetType.IMAGE,
            file_path=inbox_img,
            thumbnail_path=f"assets/{inbox_meta['thumb_name']}",
            parent_ids=[hero_id],
            verb="variant",
            recipe_id="variant",
            prompt_raw="Agent generated weapon matching hero silhouette",
            prompt_full="Voidforged Runeblade matching Aethelgard Knight palette and hand-painted bible",
            provider="demo",
            model="gemini-2.5-flash-image",
            seed=601,
            cost_usd=0.039,
            created_at=now + 210,
            tags=["weapon", "sword", "agent-proposed"],
            approval_state=ApprovalState.PENDING_REVIEW,
            metadata=inbox_meta,
            critic_report=CriticReport(
                overall_score=0.91,
                feedback="Matches project bible silhouette and gold/crimson accents; queued for human approval.",
            ),
        )
        self.save_asset(inbox_asset, sync_json=True)


def list_all_projects(root_dir: Path | None = None) -> list[str]:
    base = root_dir or get_projects_root()
    if not base.exists():
        return ["default"]
    names = sorted([d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")])
    if "default" not in names:
        names.insert(0, "default")
    return names
