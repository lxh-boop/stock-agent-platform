"""User-authored and built-in skill runtime.

Skills are method/constraint documents (Agent Skills open standard: SKILL.md
with YAML frontmatter). They are injected into Worker prompts as guidance and
never executed. Built-in skills live in the repository ``skills/`` directory;
user skills live per-user under ``runtime/skills/<user_id>/<skill_name>/``.
"""

from __future__ import annotations

from agent.skill_runtime.models import (
    SKILL_NAME_PATTERN,
    SkillEntry,
    SkillValidationError,
    admission_status,
)
from agent.skill_runtime.registry import SkillRegistry, get_skill_registry

__all__ = [
    "SKILL_NAME_PATTERN",
    "SkillEntry",
    "SkillRegistry",
    "SkillValidationError",
    "admission_status",
    "get_skill_registry",
]
