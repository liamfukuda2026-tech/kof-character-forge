"""Base definitions for KOF 2002 Character Forge Deterministic Skills."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SkillResult:
    """Standard result contract returned by all deterministic skills."""

    skill_name: str
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BaseSkill:
    """Abstract base class for all deterministic skills."""

    name: str = ""
    display_name: str = ""
    description: str = ""
    parameters_schema: dict[str, Any] = {}

    def execute(self, **kwargs: Any) -> SkillResult:
        raise NotImplementedError("Skills must implement execute()")

    def get_tool_declaration(self) -> dict[str, Any]:
        """Return tool declaration compatible with structured tool calling / AI assistant."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters_schema,
        }
