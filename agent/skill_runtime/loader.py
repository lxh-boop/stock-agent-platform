"""SKILL.md parsing, validation, security scan and package extraction.

本模块只做纯函数处理：解析、校验、扫描、解压。目录布局与状态落盘由
registry 负责；skill 内容永远不会被执行，``scripts/`` 只作为清单被记录。
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent.mcp.security import SECRET_VALUE_PATTERN
from agent.skill_runtime.models import (
    MAX_DESCRIPTION_CHARS,
    MAX_PACKAGE_FILES,
    MAX_PACKAGE_TOTAL_BYTES,
    MAX_SKILL_MD_CHARS,
    SKILL_NAME_PATTERN,
    SkillEntry,
    SkillValidationError,
    STATUS_ADMITTED,
    STATUS_REJECTED,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md document into frontmatter mapping and markdown body."""
    raw = str(text or "")
    if len(raw) > MAX_SKILL_MD_CHARS:
        raise SkillValidationError("skill_md_too_large")
    if not raw.startswith("---"):
        raise SkillValidationError("frontmatter_missing")
    parts = raw.split("\n---", 1)
    if len(parts) != 2:
        raise SkillValidationError("frontmatter_missing")
    frontmatter_text = parts[0][3:].strip("\r\n")
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise SkillValidationError("frontmatter_invalid_yaml") from exc
    if not isinstance(frontmatter, dict):
        raise SkillValidationError("frontmatter_invalid_yaml")
    return frontmatter, body.strip()


def validate_skill_fields(
    *, name: Any, description: Any, body: str
) -> tuple[str, str, str]:
    """Normalize and validate frontmatter fields; returns (name, description, error)."""
    skill_name = str(name or "").strip()
    if not SKILL_NAME_PATTERN.fullmatch(skill_name):
        raise SkillValidationError("invalid_skill_name")
    skill_description = str(description or "").strip()
    if not skill_description:
        raise SkillValidationError("description_required")
    if len(skill_description) > MAX_DESCRIPTION_CHARS:
        raise SkillValidationError("description_too_long")
    if len(body) > MAX_SKILL_MD_CHARS:
        raise SkillValidationError("skill_md_too_large")
    return skill_name, skill_description, ""


def security_scan(body: str, file_names: list[str]) -> tuple[bool, str, bool]:
    """Scan skill text and package file list.

    Returns (passed, reason, has_scripts). Scripts are never executed; their
    presence is only recorded so the UI can surface them.
    """
    for line in str(body or "").splitlines():
        match = SECRET_VALUE_PATTERN.search(line)
        if match:
            return False, f"secret_like_content:{match.group(1)[:40]}", False
    has_scripts = any(
        name.lower().startswith(("scripts/", "scripts\\"))
        or "/scripts/" in f"/{name.lower()}"
        for name in file_names
    )
    return True, "", has_scripts


def list_package_files(directory: Path) -> list[str]:
    """List files under a skill directory as forward-slash relative paths."""
    root = Path(directory)
    names: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            names.append(path.relative_to(root).as_posix())
    return names


def extract_zip_safely(
    zip_path: Path, target_dir: Path, *, subpath: str = ""
) -> list[str]:
    """Extract a skill zip into target_dir with zip-slip and size guards.

    ``subpath`` 只保留仓库内某个子目录（GitHub 导入用）；为空时，仅当所有
    成员共享同一个顶层目录（GitHub 仓库 zip 的 ``repo-branch/`` 形态）才剥掉
    它，否则按原始结构解压。任何包含 ``..``/``.`` 段的成员直接拒绝。
    """
    target = Path(target_dir).resolve()
    normalized_sub = str(subpath or "").strip().strip("/")
    extracted: list[str] = []
    total_bytes = 0
    with zipfile.ZipFile(zip_path) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_PACKAGE_FILES:
            raise SkillValidationError("too_many_files")
        prefix = ""
        if normalized_sub:
            prefix = f"{normalized_sub}/"
        else:
            names = [info.filename.replace("\\", "/") for info in members]
            tops = {name.split("/", 1)[0] for name in names}
            if any(top in ("..", ".") for top in tops):
                raise SkillValidationError("zip_slip_detected")
            if len(tops) == 1 and all("/" in name for name in names):
                prefix = f"{tops.pop()}/"
        for info in members:
            member_name = info.filename.replace("\\", "/")
            if prefix:
                if not member_name.startswith(prefix):
                    if normalized_sub:
                        continue
                    raise SkillValidationError("ambiguous_package_roots")
                relative = member_name[len(prefix):]
            else:
                relative = member_name
            if not relative or relative.split("/", 1)[0] == "__MACOSX":
                continue
            if any(part in ("..", ".") or ":" in part for part in Path(relative).parts):
                raise SkillValidationError("zip_slip_detected")
            destination = (target / relative).resolve()
            if destination != target and target not in destination.parents:
                raise SkillValidationError("zip_slip_detected")
            total_bytes += int(info.file_size)
            if total_bytes > MAX_PACKAGE_TOTAL_BYTES:
                raise SkillValidationError("package_too_large")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(destination, "wb") as dst:
                dst.write(src.read())
            extracted.append(relative)
    if not extracted:
        raise SkillValidationError("skill_md_not_found_in_package")
    return extracted


_GITHUB_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
    r"(?:/tree/([^/]+)((?:/[^\s]+)?))?/?$"
)


def parse_github_url(url: str) -> tuple[str, str, str, str]:
    """Parse a public GitHub repo URL.

    Returns (owner, repo, branch, subpath); branch 为空表示默认分支，
    subpath 为 repo 内子目录（不带首尾斜杠），不支持 /blob/ 文件页。
    """
    match = _GITHUB_URL_PATTERN.match(str(url or "").strip())
    if not match:
        raise SkillValidationError("unsupported_github_url")
    owner, repo, branch, subpath = match.group(1), match.group(2), match.group(3) or "", match.group(4) or ""
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo, branch, subpath.strip("/")


def load_entry_from_directory(
    directory: Path,
    *,
    source_type: str,
    owner_user_id: str = "",
    source_url: str = "",
    commit_hash: str = "",
    require_name_match: bool = True,
) -> SkillEntry:
    """Load and admit/reject one skill directory; never raises for bad content."""
    skill_dir = Path(directory)
    md_path = skill_dir / "SKILL.md"
    if not md_path.is_file():
        return _rejected_stub(skill_dir, "SKILL.md_missing", source_type, owner_user_id)
    text = md_path.read_text(encoding="utf-8", errors="replace")
    try:
        frontmatter, body = parse_skill_md(text)
        name, description, _ = validate_skill_fields(
            name=frontmatter.get("name"),
            description=frontmatter.get("description"),
            body=body,
        )
    except SkillValidationError as exc:
        return _rejected_stub(skill_dir, str(exc), source_type, owner_user_id)
    if require_name_match and name != skill_dir.name:
        return _rejected_stub(skill_dir, "name_mismatch_with_directory", source_type, owner_user_id)
    file_names = list_package_files(skill_dir)
    passed, reason, has_scripts = security_scan(body, file_names)
    status = STATUS_ADMITTED if passed else STATUS_REJECTED
    version = str(frontmatter.get("version") or "0.0.0")[:40]
    raw_workers = frontmatter.get("apply_to_workers")
    apply_to_workers = tuple(
        str(item).strip()
        for item in (raw_workers if isinstance(raw_workers, list) else [])
        if str(item).strip()
    )[:16]
    return SkillEntry(
        name=name,
        version=version,
        description=description,
        body=body,
        source_type=source_type,
        owner_user_id=owner_user_id,
        dir_path=str(skill_dir),
        content_hash=sha256_text(text),
        status=status,
        has_scripts=has_scripts,
        rejection_reason=reason,
        source_url=source_url,
        commit_hash=commit_hash,
        created_at=utc_now_iso(),
        file_names=tuple(file_names),
        apply_to_workers=apply_to_workers,
    )


def _rejected_stub(
    directory: Path, reason: str, source_type: str, owner_user_id: str
) -> SkillEntry:
    return SkillEntry(
        name=Path(directory).name,
        version="0.0.0",
        description="",
        body="",
        source_type=source_type,
        owner_user_id=owner_user_id,
        dir_path=str(directory),
        content_hash="",
        status=STATUS_REJECTED,
        rejection_reason=reason,
    )
