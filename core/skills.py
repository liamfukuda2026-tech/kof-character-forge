"""Core registry and dispatcher for Character Forge Deterministic Skills."""

from __future__ import annotations

from typing import Any

from skills.base import BaseSkill, SkillResult


def _load_default_skills() -> list[BaseSkill]:
    """Load and instantiate default skills on-demand to avoid circular imports."""
    from skills.inspect_iso import InspectIsoSkill
    from skills.create_project import CreateProjectSkill
    from skills.import_sprites import ImportSpritesSkill
    from skills.validate_character import ValidateCharacterSkill
    from skills.build_patch import BuildPatchSkill

    return [
        InspectIsoSkill(),
        CreateProjectSkill(),
        ImportSpritesSkill(),
        ValidateCharacterSkill(),
        BuildPatchSkill(),
    ]


class SkillRegistry:
    """Central registry and executor for deterministic skills."""

    def __init__(self, load_defaults: bool = True) -> None:
        self._skills: dict[str, BaseSkill] = {}
        self._defaults_loaded: bool = False
        self._should_load_defaults: bool = load_defaults

    def _ensure_loaded(self) -> None:
        if self._should_load_defaults and not self._defaults_loaded:
            self._defaults_loaded = True
            for s in _load_default_skills():
                self.register(s)

    def register(self, skill: BaseSkill) -> None:
        """Register a skill instance."""
        self._skills[skill.name] = skill

    def get(self, name: str) -> BaseSkill | None:
        """Retrieve a skill by name."""
        self._ensure_loaded()
        return self._skills.get(name)

    def list_skills(self) -> list[dict[str, Any]]:
        """Return list of all registered skill metadata and tool declarations."""
        self._ensure_loaded()
        return [
            {
                "name": s.name,
                "display_name": s.display_name,
                "description": s.description,
                "parameters": s.parameters_schema,
            }
            for s in self._skills.values()
        ]

    def get_tool_declarations(self) -> list[dict[str, Any]]:
        """Return tool declarations for structured tool-calling engines (LLM / Forge AI)."""
        self._ensure_loaded()
        return [s.get_tool_declaration() for s in self._skills.values()]

    def execute(self, skill_name: str, **kwargs: Any) -> SkillResult:
        """Execute a skill by name with arguments."""
        self._ensure_loaded()
        skill = self.get(skill_name)
        if not skill:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                message=f"Skill '{skill_name}' is not registered.",
                errors=[f"Unknown skill '{skill_name}'. Available: {list(self._skills.keys())}"],
            )
        try:
            return skill.execute(**kwargs)
        except Exception as exc:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                message=f"Unhandled error executing '{skill_name}': {exc}",
                errors=[str(exc)],
            )


# Global registry singleton
registry = SkillRegistry()


def execute_skill(skill_name: str, **kwargs: Any) -> SkillResult:
    """Convenience helper to execute a skill directly."""
    return registry.execute(skill_name, **kwargs)


def get_skill(skill_name: str) -> BaseSkill | None:
    """Get a skill instance by name."""
    return registry.get(skill_name)


def get_available_skills() -> list[dict[str, Any]]:
    """Get available skills list."""
    return registry.list_skills()
