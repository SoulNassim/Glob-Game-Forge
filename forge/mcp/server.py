"""Model Context Protocol (MCP) JSON-RPC 2.0 Server for Forge (Section 5 & Section 14.6).

Supports:
1. Standard MCP over `stdio` (`uv run forge-mcp` or `python -m forge.mcp.server`)
2. Direct HTTP JSON-RPC invocation at `POST /api/mcp` and tool catalog at `GET /api/mcp/tools`

Exposes every Forge verb, recipe, Project Bible, Critic, Budget, Export, and Game Scan tool
through the exact same `ForgeActionLayer` used by the Web UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from forge.agent.action_layer import ForgeActionLayer
from forge.agent.autonomous import execute_agent_brief
from forge.config import get_projects_root

from forge import __version__ as SERVER_VERSION  # noqa: E402
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def default_project() -> str:
    """The project an MCP client works on when a tool call names none (set by `forge --mcp --project`)."""
    return os.environ.get("FORGE_PROJECT", "").strip() or "default"


def _ws_tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {**properties, "project": {"type": "string"}}, "required": required},
    }


_PATH = {"type": "string", "description": "Path relative to the workspace root."}

# The same tools the in-app Glob chat uses, so an external agent can manage the workspace just like Glob.
WORKSPACE_TOOLS_SCHEMA: list[dict[str, Any]] = [
    _ws_tool(
        "workspace_overview",
        "Start here. Returns the Glob workspace's folder on disk, its tree, the front page, granted outside folders "
        "and the allowed asset kinds. Load the `glob` prompt to learn how the workspace is organized.",
        {}, [],
    ),
    _ws_tool("workspace_list", "List a workspace folder (\"\" = root).", {"path": _PATH}, []),
    _ws_tool("workspace_read", "Read a workspace file (Markdown, YAML front matter included).", {"path": _PATH}, ["path"]),
    _ws_tool(
        "workspace_write",
        "Create or overwrite a workspace file; parent folders are created. Set append=true to add to the end instead.",
        {"path": _PATH, "content": {"type": "string"}, "append": {"type": "boolean", "default": False}},
        ["path", "content"],
    ),
    _ws_tool("workspace_create_folder", "Create a folder (a section or a collection).", {"path": _PATH}, ["path"]),
    _ws_tool("workspace_move", "Move or rename a file or folder.", {"from": _PATH, "to": _PATH}, ["from", "to"]),
    _ws_tool("workspace_delete", "Move a file or folder to the workspace's .trash/ (recoverable).", {"path": _PATH}, ["path"]),
    _ws_tool(
        "generate",
        "Start image/3D generation for an asset .md (costs provider credits; runs in the Glob app, which must be open). "
        "action: candidates (new images from the asset prompt), variant / edit (need instruction, work on the winner), "
        "views (turnaround of the winner), make_3d, rig, animate (needs action_ids).",
        {
            "path": _PATH,
            "action": {"type": "string", "enum": ["candidates", "variant", "edit", "views", "make_3d", "rig", "animate"]},
            "count": {"type": "integer", "minimum": 1, "maximum": 4},
            "instruction": {"type": "string"},
            "source": {"type": "string", "description": "Image to work on instead of the winner."},
            "action_ids": {"type": "array", "items": {"type": "integer"}},
        },
        ["path", "action"],
    ),
    _ws_tool(
        "pick_winner",
        "Choose the winning image for an asset (sets `winner:` in its header).",
        {"path": _PATH, "file": {"type": "string", "description": "Workspace path of the chosen image."}},
        ["path", "file"],
    ),
]

# Opening Glob: the one tool for a coding agent. The game's finished art is listed in glob/ASSETS.md.
APP_TOOLS_SCHEMA: list[dict[str, Any]] = [
    _ws_tool(
        "open_glob",
        "Open Glob, the asset studio, on this project so the user sees every prompt, image and model. "
        "Starts it on this computer first if it isn't running. Call it when the user says 'open Glob' or "
        "'show me the assets'. Returns the URL.",
        {}, [],
    ),
]

# Legacy tools that still answer calls but are not advertised: they work on the old asset store, not the workspace.
LISTED_LEGACY_TOOLS = ("get_budget", "queue_status", "find_placeholders", "scan_game_project", "import_game")

WORKSPACE_TOOL_CALLS = {
    "workspace_list": "list",
    "workspace_read": "read",
    "workspace_create_folder": "create_folder",
    "workspace_move": "move",
    "workspace_delete": "delete",
    "generate": "generate",
    "pick_winner": "pick_winner",
}
MUTATING_TOOLS = {
    "workspace_write", "workspace_create_folder", "workspace_move", "workspace_delete", "generate", "pick_winner",
    "write_workspace_doc", "add_asset_requirement", "fulfill_asset_requirements", "import_game",
}

SERVER_INSTRUCTIONS = """Glob is the user's game-asset studio (2D art, 3D models, rigs, animations, sounds), running locally.
- When the user wants to see the assets ("open Glob", "run Glob"), call open_glob: it starts Glob if needed
  and opens it on this project.
- Finished art is listed in the game's glob/ASSETS.md. Copy the files you need into the game's own folders;
  don't edit anything inside glob/.
- To work as Glob itself (organize the asset workspace, write prompts, generate, pick winners), load the
  `glob` prompt and call workspace_overview first.
- Generation spends the user's provider credits: only generate when the user asked for it."""

MCP_ADDENDUM = """## Tools in this session

You reach the workspace through the Glob MCP server: open_glob (shows the user this project in Glob, starting
it if needed), workspace_overview (call it first), workspace_list,
workspace_read, workspace_write (append=true to append), workspace_create_folder, workspace_move,
workspace_delete (to .trash/), generate (path + action, see above) and pick_winner. All paths are relative to the workspace root. You may also open the workspace files directly at the folder workspace_overview reports;
after editing files that way, Glob's overview refreshes on its own."""


def advertised_tools() -> list[dict[str, Any]]:
    legacy = [tool for tool in MCP_TOOLS_SCHEMA if tool["name"] in LISTED_LEGACY_TOOLS]
    return APP_TOOLS_SCHEMA + WORKSPACE_TOOLS_SCHEMA + legacy


def glob_session_prompt(project: str) -> str:
    from forge.agent.workspace_agent import glob_prompt

    return f"{glob_prompt(project)}\n\n{MCP_ADDENDUM}"


MCP_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "list_assets",
        "description": "List all assets and the lineage DAG in a Forge project, optionally filtered by type, status, approval_state, or tag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "default"},
                "asset_type": {"type": "string", "enum": ["image", "mesh", "sprite_sheet", "texture", "part", "icon", "turnaround"]},
                "status": {"type": "string", "enum": ["none", "favorite", "rejected"]},
                "approval_state": {"type": "string", "enum": ["approved", "pending_review", "rejected"]},
                "tag": {"type": "string"},
            },
        },
    },
    {
        "name": "get_asset",
        "description": "Get full metadata, prompt lineage, QC report, Critic report, parents, and children for a single asset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["asset_id"],
        },
    },
    {
        "name": "run_verb",
        "description": "Run a Forge verb (variant, make_3d, turnaround, extract_part, kitbash, retexture, icon_maker, sprite_sheet, tileable_texture, batch_from_list, qc_check) on parent asset(s).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "verb": {
                    "type": "string",
                    "enum": [
                        "variant",
                        "make_3d",
                        "turnaround",
                        "extract_part",
                        "kitbash",
                        "retexture",
                        "icon_maker",
                        "sprite_sheet",
                        "tileable_texture",
                        "batch_from_list",
                        "qc_check",
                    ],
                },
                "parent_asset_ids": {"type": "array", "items": {"type": "string"}},
                "inputs": {"type": "object"},
                "project": {"type": "string", "default": "default"},
                "reason": {"type": "string", "description": "Why the agent is running this verb (recorded in Activity Log)"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["verb"],
        },
    },
    {
        "name": "run_recipe",
        "description": "Run any declarative YAML/JSON recipe by ID with loops, critic checks, and budget guards.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string"},
                "parent_asset_ids": {"type": "array", "items": {"type": "string"}},
                "inputs": {"type": "object"},
                "project": {"type": "string", "default": "default"},
                "reason": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["recipe_id"],
        },
    },
    {
        "name": "create_recipe",
        "description": "Register a new declarative YAML or JSON recipe without touching code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string"},
                "yaml_content": {"type": "string"},
            },
            "required": ["recipe_id", "yaml_content"],
        },
    },
    {
        "name": "list_style_packs",
        "description": "List all available built-in and auto-scanned Style Packs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_project_bible",
        "description": "Get the Project Bible (art style, palette, polycount budget, target engine, autonomy mode, spend caps).",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "update_project_bible",
        "description": "Update fields in the Project Bible.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "updates": {"type": "object"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["updates"],
        },
    },
    {
        "name": "rate_asset",
        "description": "Rate or swipe an asset (favorite, rejected, keep, more_like_this) and record taste signals.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "rating": {"type": "string", "enum": ["favorite", "rejected", "keep", "more_like_this", "none"]},
                "note": {"type": "string"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["asset_id", "rating"],
        },
    },
    {
        "name": "queue_status",
        "description": "Inspect live job queue status, progress, and current budget spend.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "get_budget",
        "description": "Get current project spend, per-run cap, project cap, and remaining USD budget.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "export",
        "description": "Export engine-ready folders and metadata for Godot, Unity, or Unreal (never includes API keys).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "engine": {"type": "string", "enum": ["godot", "unity", "unreal"], "default": "godot"},
                "asset_ids": {"type": "array", "items": {"type": "string"}},
                "formats": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "scan_game_project",
        "description": "Read-only scan of an existing game project folder (Godot 4 primary) to build a Game Profile with evidence & confidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "game_path": {"type": "string"},
                "consent_granted": {"type": "boolean", "default": True},
                "lock_into_bible": {"type": "boolean", "default": False},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["game_path"],
        },
    },
    {
        "name": "import_game",
        "description": (
            "Import a Godot, Unity or Unreal project (or any folder of game art) into a Glob project: characters with "
            "their 3D models, animation clips, textures and variants, weapons and items with stats from the game data, "
            "kits, skies, textures and design docs, laid out as sections and asset pages. Files are copied; the game "
            "folder is not changed. Existing sections and same-named assets are merged. The user must have granted "
            "Glob access to the folder (Settings > Storage & access); set preview=true to only list what would be imported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "game_path": {"type": "string"},
                "preview": {"type": "boolean", "default": False},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["game_path"],
        },
    },
    {
        "name": "get_game_profile",
        "description": "Retrieve the scanned Game Profile (including evidence, confidence levels, catalog, and wishlist).",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "update_game_profile",
        "description": "Update and approve fields in the Game Profile and lock them into the Project Bible.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "updates": {"type": "object"},
                "approve_and_lock": {"type": "boolean", "default": True},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["updates"],
        },
    },
    {
        "name": "list_asset_wishlist",
        "description": "List prioritized missing assets, placeholders, and TODO art items discovered by Game Scan.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "find_placeholders",
        "description": "List placeholder or temp assets found in the scanned game project for replacement.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "propose_assets_for",
        "description": "Synthesize context-aware prompts (icon, 3D prop, variants) for a specific catalog item ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["item_id"],
        },
    },
    {
        "name": "writeback_preview",
        "description": "Stage assets for export into the game project and return a non-destructive diff preview.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_ids": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "writeback_apply",
        "description": "Write staged assets and engine glue stubs into the game project (never overwrites existing files).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_ids": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "run_agent_brief",
        "description": "Execute a high-level natural language agent brief end to end within budget (e.g. 'Make 20 fantasy potions matching the project bible, then 3D for the 5 best').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "brief": {"type": "string"},
                "parent_asset_id": {"type": "string"},
                "project": {"type": "string", "default": "default"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["brief"],
        },
    },
    {
        "name": "undo_action",
        "description": "Undo the last user or agent action (or a specific activity_id).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "activity_id": {"type": "string"},
                "project": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "read_workspace_doc",
        "description": "Read a shared Markdown file in the project workspace (e.g. GAME.md, ASSET_REQUIREMENTS.md, HANDOFFS.md, KNOWLEDGE.md, DELIVERY_MANIFEST.json).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_name": {"type": "string", "default": "GAME.md"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["doc_name"],
        },
    },
    {
        "name": "write_workspace_doc",
        "description": "Write or update a shared Markdown file in the project workspace (e.g. GAME.md or ASSET_REQUIREMENTS.md). Editing GAME.md automatically syncs the Project Bible.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_name": {"type": "string"},
                "content": {"type": "string"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["doc_name", "content"],
        },
    },
    {
        "name": "list_asset_requirements",
        "description": "Parse workspace/ASSET_REQUIREMENTS.md and return open [ ], in-progress [~], and completed [x] asset requirements with SHA-256 receipts.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "add_asset_requirement",
        "description": "Add a new asset requirement row (`- [ ] id | **Kind** | target_path | description`) to workspace/ASSET_REQUIREMENTS.md so Forge can generate it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "req_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["3D Mesh", "2D Icon", "2D Sprite", "2D Texture", "2D Image"], "default": "3D Mesh"},
                "target_path": {"type": "string"},
                "description": {"type": "string"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["req_id", "description"],
        },
    },
    {
        "name": "fulfill_asset_requirements",
        "description": "Automatically generate open [ ] asset requirements in workspace/ASSET_REQUIREMENTS.md, compute SHA-256 delivery receipts, mark them [x], and append a handoff note to HANDOFFS.md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "req_ids": {"type": "array", "items": {"type": "string"}},
                "max_items": {"type": "integer", "default": 6},
                "project": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "create_blueprint_folder",
        "description": "Create a nested blueprint folder under a category, such as characters/bosses/infernal. Does not create or delete blueprints.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "delete_blueprint",
        "description": "Permanently delete one blueprint. Requires confirm=true. Generated assets stay in the vault unless delete_generated_assets is also true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
                "delete_generated_assets": {"type": "boolean", "default": False},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["item_id", "confirm"],
        },
    },
    {
        "name": "post_agent_handoff",
        "description": "Append a structured handoff message to workspace/HANDOFFS.md to communicate between the external game-coding agent and Forge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "message": {"type": "string"},
                "author": {"type": "string", "default": "coding_agent"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["title", "message"],
        },
    },
]


class ForgeMCPDispatcher:
    def __init__(self, action_layer: ForgeActionLayer | None = None, workspace_agent: Any = None, in_app: bool = False) -> None:
        self.actions = action_layer or ForgeActionLayer()
        self.in_app = in_app  # inside the running studio: open_glob only needs the browser
        if workspace_agent is None:
            from forge.agent.workspace_agent import WorkspaceAgent

            workspace_agent = WorkspaceAgent(self.actions, model_call=None)
        # Runs the workspace tools; in the Glob app it also starts generation jobs.
        self.workspace_agent = workspace_agent

    def _workspace_tool(self, name: str, args: dict[str, Any], proj: str) -> dict[str, Any]:
        agent = self.workspace_agent
        if name == "workspace_overview":
            from forge import asset_studio

            context, _ = agent._context_block(proj, {}, {}, [])
            return {"project": proj, "workspace": str(agent._ws(proj).resolve()), "kinds": list(asset_studio.ASSET_KINDS), "context": context}
        if name == "generate" and agent.job_starter is None:
            raise ValueError("Generation runs in the Glob app. Ask the user to open Glob (`glob` or the desktop app), then try again.")
        if name == "workspace_write":
            call = {"tool": "append" if args.get("append") else "write", "path": args.get("path"), "content": args.get("content", "")}
        else:
            call = {key: value for key, value in args.items() if key != "project"} | {"tool": WORKSPACE_TOOL_CALLS[name]}
        outcome = agent.run_tool(proj, call)
        result = {"ok": True, "summary": outcome.get("summary", ""), "path": outcome.get("path")}
        if outcome.get("result") is not None:
            result["content"] = outcome["result"]
        if outcome.get("job"):
            result["job"] = outcome["job"]
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        proj = str(args.get("project") or "").strip() or default_project()

        if name == "workspace_overview" or name == "workspace_write" or name in WORKSPACE_TOOL_CALLS:
            return self._workspace_tool(name, args, proj)
        if name == "open_glob":
            from asyncio import to_thread

            from forge import launcher

            return await to_thread(launcher.open_glob, proj, start=not self.in_app)

        if name == "list_assets":
            return self.actions.list_assets(
                project=proj,
                asset_type=args.get("asset_type"),
                status=args.get("status"),
                approval_state=args.get("approval_state"),
                tag=args.get("tag"),
            )
        if name == "get_asset":
            return self.actions.get_asset(args["asset_id"], project=proj)
        if name == "run_verb":
            return await self.actions.run_verb(
                verb=args["verb"],
                parent_asset_ids=args.get("parent_asset_ids"),
                inputs=args.get("inputs"),
                project=proj,
                initiated_by="agent",
                agent_reason=args.get("reason", f"MCP tool call: run_verb({args['verb']})"),
                dry_run=bool(args.get("dry_run", False)),
            )
        if name == "run_recipe":
            return await self.actions.run_recipe(
                recipe_id=args["recipe_id"],
                parent_asset_ids=args.get("parent_asset_ids"),
                inputs=args.get("inputs"),
                project=proj,
                initiated_by="agent",
                agent_reason=args.get("reason", f"MCP tool call: run_recipe({args['recipe_id']})"),
                dry_run=bool(args.get("dry_run", False)),
            )
        if name == "create_recipe":
            return self.actions.create_recipe(args["recipe_id"], args["yaml_content"])
        if name == "list_style_packs":
            return self.actions.list_style_packs()
        if name == "get_project_bible":
            return self.actions.get_project_bible(project=proj)
        if name == "update_project_bible":
            return self.actions.update_project_bible(args["updates"], project=proj, actor="agent")
        if name == "rate_asset":
            return await self.actions.rate_asset(
                asset_id=args["asset_id"],
                rating=args["rating"],
                note=args.get("note"),
                project=proj,
                actor="agent",
            )
        if name == "queue_status":
            return self.actions.queue_status(project=proj)
        if name == "get_budget":
            return self.actions.get_budget(project=proj)
        if name == "export":
            return self.actions.export(
                engine=args.get("engine", "godot"),
                asset_ids=args.get("asset_ids"),
                formats=args.get("formats"),
                project=proj,
            )
        if name == "scan_game_project":
            return self.actions.scan_game_project(
                game_path=args["game_path"],
                consent_granted=bool(args.get("consent_granted", True)),
                lock_into_bible=bool(args.get("lock_into_bible", False)),
                project=proj,
            )
        if name == "import_game":
            import asyncio

            from forge import access, game_import

            folder = access.check(str(args.get("game_path") or ""))
            if args.get("preview"):
                return await asyncio.to_thread(game_import.preview, folder)
            ws_dir = self.actions.get_workspace(proj).ws_dir
            result = await asyncio.to_thread(game_import.import_game, folder, ws_dir)
            return {"project": proj, "summary": result["summary"], "copied": result["copied"], "assets": result["assets"], "sections": result["sections"]}
        if name == "get_game_profile":
            return self.actions.get_game_profile(project=proj)
        if name == "update_game_profile":
            return self.actions.update_game_profile(
                updates=args["updates"],
                approve_and_lock=bool(args.get("approve_and_lock", True)),
                project=proj,
            )
        if name == "list_asset_wishlist":
            return self.actions.list_asset_wishlist(project=proj)
        if name == "find_placeholders":
            return self.actions.find_placeholders(project=proj)
        if name == "propose_assets_for":
            return self.actions.propose_assets_for(args["item_id"], project=proj)
        if name == "writeback_preview":
            return self.actions.writeback_preview(asset_ids=args.get("asset_ids"), project=proj)
        if name == "writeback_apply":
            return self.actions.writeback_apply(asset_ids=args.get("asset_ids"), project=proj)
        if name == "run_agent_brief":
            return await execute_agent_brief(
                action_layer=self.actions,
                brief=args["brief"],
                parent_asset_id=args.get("parent_asset_id"),
                project=proj,
                dry_run=bool(args.get("dry_run", False)),
            )
        if name == "undo_action":
            return self.actions.undo_action(activity_id=args.get("activity_id"), project=proj)
        if name == "read_workspace_doc":
            return self.actions.read_workspace_doc(args["doc_name"], project=proj)
        if name == "write_workspace_doc":
            return self.actions.write_workspace_doc(
                args["doc_name"], args["content"], project=proj, actor="agent"
            )
        if name == "list_asset_requirements":
            return self.actions.list_asset_requirements(project=proj)
        if name == "add_asset_requirement":
            return self.actions.add_asset_requirement(
                req_id=args["req_id"],
                kind=args.get("kind", "3D Mesh"),
                target_path=args.get("target_path", ""),
                description=args.get("description", ""),
                project=proj,
                actor="agent",
            )
        if name == "fulfill_asset_requirements":
            return await self.actions.fulfill_asset_requirements(
                req_ids=args.get("req_ids"),
                max_items=int(args.get("max_items", 6)),
                project=proj,
                actor="agent",
            )
        if name == "create_blueprint_folder":
            pipeline = self.actions.get_pipeline(proj)
            folder = pipeline.create_folder(str(args.get("path") or ""), actor="agent")
            return {"folder": folder}
        if name == "delete_blueprint":
            return self.actions.delete_pipeline_item(
                str(args.get("item_id") or ""),
                project=proj,
                confirm=bool(args.get("confirm", False)),
                delete_generated_assets=bool(args.get("delete_generated_assets", False)),
            )
        if name == "post_agent_handoff":
            return self.actions.post_agent_handoff(
                title=args["title"],
                message=args["message"],
                author=args.get("author", "coding_agent"),
                project=proj,
            )

        raise ValueError(f"Unknown MCP tool '{name}'.")

    async def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params") or {}

        if method.startswith("notifications/") or "id" not in request:
            return None

        try:
            if method == "initialize":
                wanted = params.get("protocolVersion")
                result: Any = {
                    "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "serverInfo": {"name": "glob", "title": "Glob asset studio", "version": SERVER_VERSION},
                    "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                    "instructions": SERVER_INSTRUCTIONS,
                }
            elif method == "tools/list":
                result = {"tools": advertised_tools()}
            elif method == "prompts/list":
                result = {"prompts": [{"name": "glob", "title": "Become Glob", "description": "Manage this project's Glob asset workspace."}]}
            elif method == "prompts/get":
                if params.get("name") != "glob":
                    raise ValueError(f"Unknown prompt '{params.get('name')}'.")
                project = str((params.get("arguments") or {}).get("project") or "").strip() or default_project()
                result = {
                    "description": "Manage this project's Glob asset workspace.",
                    "messages": [{"role": "user", "content": {"type": "text", "text": glob_session_prompt(project)}}],
                }
            elif method == "tools/call":
                t_name = params.get("name", "")
                t_args = params.get("arguments") or {}
                try:
                    tool_out = await self.call_tool(t_name, t_args)
                except Exception as exc:  # noqa: BLE001 — tool failures go back to the agent as readable results
                    detail = str(exc).strip("'\"") or type(exc).__name__
                    result = {"content": [{"type": "text", "text": detail}], "isError": True}
                else:
                    result = {
                        "content": [{"type": "text", "text": json.dumps(tool_out, indent=2)}],
                        "structuredContent": tool_out,
                        "isError": False,
                    }
            elif method == "ping":
                result = {}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }


# Calls that need the studio running; the first one in a session starts it and opens the project.
AUTOSTART_TOOLS = {"generate", "pick_winner"}


def _with_project(request: dict[str, Any]) -> dict[str, Any]:
    """Name this client's project in calls forwarded to the Glob app, which serves every project."""
    params = request.get("params")
    if request.get("method") not in ("tools/call", "prompts/get") or not isinstance(params, dict):
        return request
    arguments = dict(params.get("arguments") or {})
    arguments.setdefault("project", default_project())
    return request | {"params": params | {"arguments": arguments}}


class AppBridge:
    """Forwards stdio requests to the running Glob app so jobs and live updates show up there.

    Falls back to an in-process dispatcher while the app is closed (generation then asks the user to open it).
    """

    def __init__(self) -> None:
        from forge.config import load_config

        cfg = load_config()
        self.base = (os.environ.get("FORGE_APP_URL") or f"http://127.0.0.1:{cfg.port or 8420}").rstrip("/")
        self._local: ForgeMCPDispatcher | None = None
        self._app_ok: bool | None = None
        self._autostart_failed = False

    def local(self) -> ForgeMCPDispatcher:
        if self._local is None:
            self._local = ForgeMCPDispatcher()
        return self._local

    async def _autostart(self, request: dict[str, Any]) -> None:
        """Ordering or generating needs the studio: start it when it isn't running and show the project."""
        from forge import launcher

        params = request.get("params") if request.get("method") == "tools/call" else None
        if not isinstance(params, dict) or params.get("name") not in AUTOSTART_TOOLS or self._autostart_failed:
            return
        if not launcher.autostart_enabled() or await asyncio.to_thread(launcher.is_running, self.base):
            return
        project = str((params.get("arguments") or {}).get("project") or default_project())
        try:
            await asyncio.to_thread(launcher.open_glob, project)
        except RuntimeError:
            self._autostart_failed = True  # don't make every call wait for a start that can't work; stay on the files

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if request.get("method") in ("initialize", "ping") or request.get("method", "").startswith("notifications/"):
            return await self.local().handle_jsonrpc(request)
        import httpx

        await self._autostart(request)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=1.5)) as client:
                if not self._app_ok:
                    health = (await client.get(f"{self.base}/api/health")).json()
                    self._app_ok = Path(str(health.get("projects_dir") or "")).resolve() == get_projects_root().resolve()
                if self._app_ok:
                    response = await client.post(f"{self.base}/api/mcp", json=_with_project(request))
                    if response.status_code == 200:
                        return response.json()
        except (httpx.HTTPError, ValueError, AttributeError):
            self._app_ok = None  # the app may start later; check again on the next call
        return await self.local().handle_jsonrpc(_with_project(request))


async def _stdio_loop() -> None:
    bridge = AppBridge()
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line_str = line.strip()
        if not line_str:
            continue
        try:
            req = json.loads(line_str)
            resp = await bridge.handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            err_resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


def main() -> None:
    asyncio.run(_stdio_loop())


if __name__ == "__main__":
    main()
