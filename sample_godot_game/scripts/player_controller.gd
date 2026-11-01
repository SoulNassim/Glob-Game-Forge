extends CharacterBody3D

# Player controller for Chronicles of Aethelgard
# TODO: Replace placeholder_icon.png with proper 64x64 hand-painted stamina potion icon
# FIXME: Need 3D GLB mesh for ancient_treasure_chest.glb in boss room

const MOVE_SPEED: float = 6.5
var equipped_weapon_mesh: String = "res://assets/models/iron_sword.glb"
var shield_mesh: String = "res://assets/models/aethelgard_shield.glb"
