"""Autonomous Agent Brief Runner (Section 5 & Section 13).

Executes natural-language briefs end to end, such as:
- "Make 20 fantasy potions matching the project bible, game-ready, transparent background, then 3D for the 5 best."
- "Make 10 matching props for this character"
- "Fulfill the top 3 items from the Game Scan wishlist"

Respects:
- Project Autonomy Dial (`suggest`, `checkpoints`, `autopilot`)
- Hard Budget Guard
- Vision Critic scoring & ranking
- Queues generated outputs into the Approval Inbox (`PENDING_REVIEW`) and records every reason in the Activity Log.
"""

from __future__ import annotations

import re
from typing import Any
from forge.agent.action_layer import ForgeActionLayer
from forge.models import ActivityLogEntry, AutonomyMode


POTION_ARCHETYPES = [
    "Crimson Vitality Potion in bulbous ruby crystal flask",
    "Starlight Mana Elixir in spiral sapphire bottle",
    "Emerald Stamina Tonic in leaf-etched green vial",
    "Solar Phoenix Draught with glowing golden plume stopper",
    "Frostbite Ward Potion in faceted glacier decanter",
    "Voidwalker Shadow Brew in obsidian hourglass flask",
    "Alchemist's Liquid Luck in clover-shaped gilded phial",
    "Ironbark Stoneskin Potion in oak-caged flask",
    "Stormcaller Lightning Vial with copper coil neck",
    "Moonwell Serenity Draught in pearlescent crescent bottle",
    "Dragonbreath Fire Oil in volcanic basalt gourd",
    "Sylvan Feywild Nectar in dewdrop blossom ampoule",
    "Chronos Sand-Time Elixir in twin-chamber brass vial",
    "Berserker Rage Blood-Tonic in horned iron flask",
    "Astral Levitation Mist in floating spherical orb",
    "Venomfang Antidote in serpent-coiled jade bottle",
    "Sunburst Clarity Potion in prism-cut citrine flask",
    "Abyssal Water-Breathing Elixir in coral-trimmed shell bottle",
    "Arcane Overcharge Essence in runic amethyst vial",
    "Celestial Resurrection Ambrosia in winged chalice flask",
]

PROP_ARCHETYPES = [
    "Ornate kite shield with matching crest and gold trim",
    "Runic lantern staff with glowing emerald core",
    "Alchemical belt satchel with brass buckles and potion vials",
    "Winged war-hammer with engraved runes",
    "Reinforced travel chest bound in gilded iron",
    "Arcane compass astrolabe prop",
    "Crested banner standard on iron pole",
    "Enchanted grimoire spellbook with gemstone clasp",
    "ceremonial gauntlet bracers with glowing filigree",
    "Ancient waystone obelisk prop",
]


def parse_agent_brief(brief: str) -> dict[str, Any]:
    """Parse natural language brief into a structured multi-step execution plan."""
    b_low = brief.lower()
    numbers = [int(n) for n in re.findall(r"\b(\d+)\b", brief)]

    total_count = numbers[0] if numbers else 4
    total_count = max(1, min(25, total_count))

    then_3d_count = 0
    m_3d = re.search(r"3d\s+for\s+(?:the\s+)?(\d+)\s+best|top\s+(\d+)\s+(?:to\s+)?3d", b_low)
    if m_3d:
        then_3d_count = int(m_3d.group(1) or m_3d.group(2))
    elif "then 3d" in b_low or "and 3d" in b_low:
        then_3d_count = numbers[1] if len(numbers) > 1 and numbers[1] != 3 else min(total_count, 3)

    is_wishlist = "wishlist" in b_low or "missing" in b_low or "placeholder" in b_low
    is_potion = "potion" in b_low or "elixir" in b_low or "flask" in b_low
    is_prop = "prop" in b_low or "item" in b_low or "weapon" in b_low or "equipment" in b_low

    return {
        "brief": brief,
        "total_count": total_count,
        "then_3d_count": then_3d_count,
        "is_wishlist": is_wishlist,
        "archetype": "wishlist" if is_wishlist else ("potion" if is_potion else ("prop" if is_prop else "custom")),
    }


async def execute_agent_brief(
    action_layer: ForgeActionLayer,
    brief: str,
    parent_asset_id: str | None = None,
    project: str = "default",
    dry_run: bool = False,
    approve_checkpoint: bool = False,
) -> dict[str, Any]:
    store = action_layer.get_store(project)
    bible = store.get_bible()
    plan = parse_agent_brief(brief)

    # Check if Game Profile has low-confidence fields and agent should note it
    profile = store.get_game_profile()
    low_conf_questions: list[str] = []
    if profile:
        for fname in ("art_style", "camera_and_perspective", "scale_and_units"):
            f_obj = getattr(profile, fname, None)
            if f_obj and f_obj.confidence < 0.65:
                low_conf_questions.append(
                    f"Game Profile field '{fname}' has low confidence ({int(f_obj.confidence * 100)}%). Confirm '{f_obj.value}'?"
                )

    # Build item list based on brief archetype
    items: list[str] = []
    if plan["is_wishlist"] and profile and profile.wishlist:
        for w in profile.wishlist[: plan["total_count"]]:
            items.append(f"{w.title.replace('Missing: ', '').replace('Placeholder: ', '')} — {w.description}")
    elif plan["archetype"] == "potion":
        items = POTION_ARCHETYPES[: plan["total_count"]]
    elif plan["archetype"] == "prop":
        parent = store.get_asset(parent_asset_id) if parent_asset_id else None
        anchor_name = parent.name.split("•")[0].strip() if parent else "Aethelgard Knight"
        items = [f"{p} matching {anchor_name}" for p in PROP_ARCHETYPES[: plan["total_count"]]]
    else:
        items = [f"{brief.strip()} (Design #{i + 1})" for i in range(plan["total_count"])]

    batch_inputs = {
        "items": items,
        "asset_kind": "image",
        "then_3d_top_n": plan["then_3d_count"],
        "human_approved": approve_checkpoint,
    }

    # Check Autonomy Dial
    if bible.autonomy_mode == AutonomyMode.SUGGEST and not approve_checkpoint and not dry_run:
        est = await action_layer.run_recipe(
            recipe_id="batch_from_list",
            parent_asset_ids=[parent_asset_id] if parent_asset_id else [],
            inputs=batch_inputs,
            project=project,
            initiated_by="agent",
            agent_reason=f"Agent Brief Proposal: {brief}",
            dry_run=True,
        )
        store.log_activity(
            ActivityLogEntry(
                actor="agent",
                action="propose_agent_brief",
                reason=f"[Suggest Mode] Proposed {len(items)} items + {plan['then_3d_count']} 3D conversions (Est. ${est['estimate']['estimated_usd']:.2f})",
            )
        )
        return {
            "status": "proposed_waiting_approval",
            "autonomy_mode": bible.autonomy_mode.value,
            "plan": plan,
            "items": items,
            "estimate": est["estimate"],
            "low_confidence_questions": low_conf_questions,
            "message": "Autonomy dial is set to 'Suggest'. Review the plan and click Approve to execute.",
        }

    if bible.autonomy_mode == AutonomyMode.CHECKPOINTS and plan["then_3d_count"] > 0 and not approve_checkpoint and not dry_run:
        # In Checkpoints mode, run the 2D generation milestone first, then pause before 3D!
        step1_inputs = {**batch_inputs, "then_3d_top_n": 0, "human_approved": True}
        res_2d = await action_layer.run_recipe(
            recipe_id="batch_from_list",
            parent_asset_ids=[parent_asset_id] if parent_asset_id else [],
            inputs=step1_inputs,
            project=project,
            initiated_by="agent",
            agent_reason=f"[Checkpoint 1/2] Generated {len(items)} 2D assets for brief: {brief}",
        )
        return {
            "status": "checkpoint_reached",
            "autonomy_mode": bible.autonomy_mode.value,
            "checkpoint_message": f"Completed {len(res_2d.get('assets', []))} 2D assets. Ready for Checkpoint 2: Make 3D for the top {plan['then_3d_count']} Critic-scored assets.",
            "plan": plan,
            "result": res_2d,
            "low_confidence_questions": low_conf_questions,
        }

    # Autopilot (or approved run): execute full end-to-end brief under budget guard!
    result = await action_layer.run_recipe(
        recipe_id="batch_from_list",
        parent_asset_ids=[parent_asset_id] if parent_asset_id else [],
        inputs={**batch_inputs, "human_approved": True},
        project=project,
        initiated_by="agent",
        agent_reason=f"Autonomous execution of brief: '{brief}'",
        dry_run=dry_run,
    )
    return {
        "status": "completed" if not result.get("budget_paused") else "paused_budget",
        "autonomy_mode": bible.autonomy_mode.value,
        "plan": plan,
        "items_planned": len(items),
        "low_confidence_questions": low_conf_questions,
        "result": result,
    }
