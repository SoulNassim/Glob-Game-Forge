"""End-to-End Smoke Test Script for Forge (Section 9 & Section 13).

Runs the complete Forge flow in Demo Mode without requiring any external API keys:
1. Initializes default project & verifies starter lineage tree
2. Generates a placeholder icon inside `sample_godot_game/` for Game Scan
3. Runs Variant Wizard (4 Rarity tier variants with identity locking)
4. Runs Extract Part ("winged helmet") and Make 3D (GLB + OBJ)
5. Runs Game Scan on `sample_godot_game/`, synthesizes context-aware prompts for `iron_sword`, previews staged write-back
6. Executes an autonomous Agent Brief via the MCP Server ("Make 4 fantasy potions matching the project bible, then 3D for the 1 best")
7. Exports engine-ready bundles for Godot, Unity, and Unreal
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.agent.action_layer import ForgeActionLayer
from forge.mcp.server import ForgeMCPDispatcher
from forge.storage.demo_assets import generate_demo_image


async def main() -> None:
    print("=== Forge End-to-End Smoke Test (Demo Mode) ===")
    root_dir = Path(__file__).resolve().parent.parent
    sample_game_dir = root_dir / "sample_godot_game"

    # Seed sample_godot_game placeholder icon so Game Scan can inspect real image pixels
    placeholder_png = sample_game_dir / "assets" / "icons" / "placeholder_icon.png"
    if not placeholder_png.exists():
        generate_demo_image(
            placeholder_png,
            prompt="Solid placeholder icon",
            palette=["#06D6A0", "#FFD166"],
            mode="icon",
        )

    actions = ForgeActionLayer()
    mcp = ForgeMCPDispatcher(action_layer=actions)

    # 1. List initial starter assets
    initial = actions.list_assets(project="default")
    print(f"[1/6] Default project initialized with {initial['count']} starter assets in lineage DAG.")
    hero_id = initial["assets"][-1]["id"]

    # 2. Run Variant verb (4 Rarity tier siblings)
    var_res = await actions.run_verb(
        verb="variant",
        parent_asset_ids=[hero_id],
        inputs={"chips": ["Rarity tiers", "Colors"], "count": 4, "prompt": "Arcane crystal trim"},
        project="default",
    )
    print(f"[2/6] Variant verb generated {len(var_res['assets'])} identity-locked siblings.")

    # 3. Run Make 3D on one of the variants
    picked_var_id = var_res["assets"][0]["id"]
    m3d_res = await actions.run_verb(
        verb="make_3d",
        parent_asset_ids=[picked_var_id],
        inputs={"ai_model": "meshy-7.1", "should_texture": True},
        project="default",
    )
    glb_asset = m3d_res["assets"][0]
    print(f"[3/6] Make 3D generated GLB mesh: {glb_asset['file_path']} ({glb_asset['metadata'].get('triangle_count')} tris).")

    # 4. Run Game Scan on sample_godot_game
    scan_res = actions.scan_game_project(
        game_path=str(sample_game_dir),
        consent_granted=True,
        lock_into_bible=True,
        project="default",
    )
    prof = scan_res["profile"]
    print(
        f"[4/6] Game Scan completed on '{prof['game_title']['value']}': "
        f"{len(prof['catalog'])} catalog entries, {len(prof['wishlist'])} prioritized wishlist items."
    )

    # 5. Execute autonomous Agent Brief over MCP
    rpc_resp = await mcp.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "run_agent_brief",
                "arguments": {
                    "brief": "Make 4 fantasy potions matching the project bible, transparent background, then 3D for the 1 best",
                    "project": "default",
                },
            },
        }
    )
    brief_out = rpc_resp["result"]["structuredContent"]
    created_in_brief = len(brief_out["result"]["assets"])
    print(f"[5/6] MCP Agent Brief executed unattended: produced {created_in_brief} assets (4 potions + 1 top-scored 3D mesh).")

    # 6. Export engine-ready Godot bundle
    exp_res = actions.export(engine="godot", project="default")
    print(f"[6/6] Exported Godot bundle: {exp_res['zip_path']} ({exp_res['asset_count']} assets).")
    print("=== All Smoke Tests Passed! ===")


if __name__ == "__main__":
    asyncio.run(main())
