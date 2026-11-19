"""Deterministic Skills for KOF 2002 Character Forge."""

from skills.base import BaseSkill, SkillResult
from skills.build_patch import BuildPatchSkill
from skills.create_project import CreateProjectSkill
from skills.import_sprites import ImportSpritesSkill
from skills.inspect_iso import InspectIsoSkill
from skills.validate_character import ValidateCharacterSkill

skill_inspect_iso = InspectIsoSkill()
skill_create_project = CreateProjectSkill()
skill_import_sprites = ImportSpritesSkill()
skill_validate_character = ValidateCharacterSkill()
skill_build_patch = BuildPatchSkill()

ALL_SKILLS: list[BaseSkill] = [
    skill_inspect_iso,
    skill_create_project,
    skill_import_sprites,
    skill_validate_character,
    skill_build_patch,
]

__all__ = [
    "BaseSkill",
    "SkillResult",
    "InspectIsoSkill",
    "CreateProjectSkill",
    "ImportSpritesSkill",
    "ValidateCharacterSkill",
    "BuildPatchSkill",
    "skill_inspect_iso",
    "skill_create_project",
    "skill_import_sprites",
    "skill_validate_character",
    "skill_build_patch",
    "ALL_SKILLS",
]
