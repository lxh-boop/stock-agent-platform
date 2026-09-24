"""Data model for registered skills."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Skill 目录名/SKILL.md frontmatter name 的白名单，目录名直接由它构成，
# 与 server/api/security.py 的用户名收口思路一致：源头消除路径穿越。
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

# 与 server/api/security.py 的 _USERNAME_PATTERN 保持一致。
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")

# 准入状态：admitted=可注入；disabled=用户主动停用；rejected=安全扫描未通过。
STATUS_ADMITTED = "admitted"
STATUS_DISABLED = "disabled"
STATUS_REJECTED = "rejected"
VALID_STATUSES = (STATUS_ADMITTED, STATUS_DISABLED, STATUS_REJECTED)

SOURCE_BUILTIN = "builtin"
SOURCE_EDITOR = "editor"
SOURCE_UPLOAD = "upload"
SOURCE_GITHUB = "github"
VALID_SOURCES = (SOURCE_BUILTIN, SOURCE_EDITOR, SOURCE_UPLOAD, SOURCE_GITHUB)

MAX_SKILL_MD_CHARS = 64_000
MAX_DESCRIPTION_CHARS = 500
MAX_PACKAGE_FILES = 20
MAX_PACKAGE_TOTAL_BYTES = 1_000_000


class SkillValidationError(ValueError):
    """Raised when a skill package fails structural or security validation."""


@dataclass(frozen=True)
class SkillEntry:
    name: str
    version: str
    description: str
    body: str
    source_type: str
    owner_user_id: str  # builtin 为空串
    dir_path: str
    content_hash: str
    status: str = STATUS_ADMITTED
    has_scripts: bool = False
    rejection_reason: str = ""
    source_url: str = ""
    commit_hash: str = ""
    created_at: str = ""
    file_names: tuple[str, ...] = field(default_factory=tuple)
    # frontmatter 可选 apply_to_workers: [W04, ...]；为空表示对该用户所有 Worker 注入。
    apply_to_workers: tuple[str, ...] = field(default_factory=tuple)

    def public_dict(self, *, include_body: bool = False) -> dict[str, object]:
        data: dict[str, object] = asdict(self)
        data["file_names"] = list(self.file_names)
        if not include_body:
            data.pop("body", None)
        return data


def admission_status(entry: SkillEntry) -> str:
    return entry.status if entry.status in VALID_STATUSES else STATUS_REJECTED
