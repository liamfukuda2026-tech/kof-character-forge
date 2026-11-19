"""Deterministic Skill: skill_create_project.

Creates a new Character Forge project based on an existing character slot.
Extracts base character information, palettes from FIRST.GRC, initializes
character.json, and prepares workspace directories.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.characters import list_character_slots
from core.forge_project import ForgeProject
from skills.base import BaseSkill, SkillResult


class CreateProjectSkill(BaseSkill):
    name = "skill_create_project"
    display_name = "Create Character Project"
    description = (
        "Creates a new Character Forge project using an existing character slot as a base "
        "(e.g. Terry Bogard -> Terry Legendary). Initializes character.json, extracts base "
        "palettes, and establishes memory/LBA constraints."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Name of the new character project (e.g. 'Terry Legendary').",
            },
            "base_slot": {
                "type": "string",
                "description": (
                    "Base character slot name, slug, or hex ID "
                    "(e.g. 'Terry Bogard', 'terry', '0x03', 'Rugal', '0x27')."
                ),
            },
            "author": {
                "type": "string",
                "description": "Optional author name.",
            },
            "description": {
                "type": "string",
                "description": "Optional project description.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Whether to overwrite existing project directory if present.",
                "default": False,
            },
        },
        "required": ["project_name", "base_slot"],
    }

    def execute(
        self,
        project_name: str,
        base_slot: str | int,
        author: str = "",
        description: str = "",
        overwrite: bool = False,
        workspace_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> SkillResult:
        errors: list[str] = []
        warnings: list[str] = []

        if not str(project_name).strip():
            return SkillResult(
                skill_name=self.name,
                success=False,
                message="Project name cannot be empty.",
                errors=["project_name is required"],
            )

        ws_path = Path(workspace_dir) if workspace_dir else None

        try:
            proj = ForgeProject.create(
                name=project_name,
                base_slot=base_slot,
                author=author,
                description=description,
                workspace_dir=ws_path,
                overwrite=overwrite,
            )
            data = {
                "project_name": proj.name,
                "project_slug": proj.slug,
                "project_dir": str(proj.project_dir),
                "base_slot": proj.base_slot.to_dict(),
                "palettes_available": list(proj.data.get("palettes", {}).keys()),
                "max_memory_bytes": proj.base_slot.size_bytes,
                "frames_dir": str(proj.frames_dir),
            }
            return SkillResult(
                skill_name=self.name,
                success=True,
                message=f"Character project '{proj.name}' created successfully from base '{proj.base_slot.display_name}'.",
                data=data,
            )
        except FileExistsError as exc:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=str(exc),
                errors=[str(exc)],
            )
        except KeyError as exc:
            available = [s.display_name for s in list_character_slots()[:8]]
            msg = f"{exc} Valid examples: {', '.join(available)}..."
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=msg,
                errors=[str(exc)],
            )
        except Exception as exc:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"Failed to create project: {exc}",
                errors=[str(exc)],
            )
