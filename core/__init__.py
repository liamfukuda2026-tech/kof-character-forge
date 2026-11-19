"""Character Forge Core Modules for KOF 2002 PS2 Modding."""

from core.ai_assistant import ForgeAIAssistant
from core.characters import CharacterSlot, list_character_slots, resolve_character_slot
from core.forge_project import ForgeProject
from core.skills import SkillRegistry, execute_skill, get_available_skills, get_skill, registry

__all__ = [
    "ForgeProject",
    "ForgeAIAssistant",
    "SkillRegistry",
    "registry",
    "execute_skill",
    "get_skill",
    "get_available_skills",
    "CharacterSlot",
    "resolve_character_slot",
    "list_character_slots",
]
