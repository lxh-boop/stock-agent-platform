"""Registry of built-in and per-user skills.

权威存储是文件系统本身：内置 skill 在仓库 ``skills/``，用户 skill 在
``runtime/skills/<user_id>/<skill_name>/``。准入状态持久化在各自目录的
``skill.meta.json``；安全扫描结果 rejected 永远优先于用户开关。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from agent.skill_runtime.loader import (
    extract_zip_safely,
    load_entry_from_directory,
    security_scan,
    utc_now_iso,
    validate_skill_fields,
)
from agent.skill_runtime.models import (
    SOURCE_BUILTIN,
    SOURCE_EDITOR,
    SOURCE_GITHUB,
    SOURCE_UPLOAD,
    STATUS_ADMITTED,
    STATUS_DISABLED,
    STATUS_REJECTED,
    USER_ID_PATTERN,
    SkillEntry,
    SkillValidationError,
)
from core.config.paths import get_runtime_dir

_META_FILE = "skill.meta.json"
_SKILL_MD = "SKILL.md"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class SkillRegistry:
    def __init__(
        self,
        builtin_dir: Path | None = None,
        user_root: Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._builtin_dir = Path(builtin_dir) if builtin_dir else _project_root() / "skills"
        self._user_root = Path(user_root) if user_root else get_runtime_dir() / "skills"
        self._builtin: dict[str, SkillEntry] = {}
        self._users: dict[str, dict[str, SkillEntry]] = {}
        self.refresh()

    # ------------------------------------------------------------------ reads

    @property
    def user_root(self) -> Path:
        return self._user_root

    def refresh(self) -> None:
        with self._lock:
            self._builtin = self._scan_builtin()
            self._users = self._scan_users()

    def list_for_user(self, user_id: str) -> list[SkillEntry]:
        """Built-in skills plus the caller's own skills; other users are invisible."""
        safe_user = self._require_user(user_id)
        with self._lock:
            entries = list(self._builtin.values())
            entries.extend(self._users.get(safe_user, {}).values())
        return sorted(entries, key=lambda item: (item.source_type != SOURCE_BUILTIN, item.name))

    def admitted_for_user(self, user_id: str) -> list[SkillEntry]:
        return [entry for entry in self.list_for_user(user_id) if entry.status == STATUS_ADMITTED]

    def get(self, user_id: str, name: str) -> SkillEntry | None:
        safe_user = self._require_user(user_id)
        with self._lock:
            if name in self._builtin:
                return self._builtin[name]
            return self._users.get(safe_user, {}).get(name)

    def summary_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """One-line entries for catalog/prompt summary use (no body)."""
        return [
            {
                "name": entry.name,
                "version": entry.version,
                "description": entry.description,
                "source_type": entry.source_type,
                "status": entry.status,
            }
            for entry in self.admitted_for_user(user_id)
        ]

    # ---------------------------------------------------------------- writes

    def create_from_editor(
        self, user_id: str, *, name: str, description: str, body: str
    ) -> SkillEntry:
        safe_user = self._require_user(user_id)
        skill_name, safe_description, _ = validate_skill_fields(
            name=name, description=description, body=body
        )
        passed, reason, _ = security_scan(body, [])
        if not passed:
            raise SkillValidationError(reason)
        text = self._compose_skill_md(skill_name, safe_description, body)
        with self._lock:
            self._ensure_free(safe_user, skill_name)
            skill_dir = self._skill_dir(safe_user, skill_name)
            skill_dir.mkdir(parents=True, exist_ok=False)
            (skill_dir / _SKILL_MD).write_text(text, encoding="utf-8")
            entry = load_entry_from_directory(skill_dir, source_type=SOURCE_EDITOR, owner_user_id=safe_user)
            entry = self._persist_meta(entry)
            self._users.setdefault(safe_user, {})[skill_name] = entry
            return entry

    def install_package(
        self,
        user_id: str,
        zip_path: Path,
        *,
        source_type: str,
        source_url: str = "",
        commit_hash: str = "",
        subpath: str = "",
    ) -> SkillEntry:
        safe_user = self._require_user(user_id)
        if source_type not in (SOURCE_UPLOAD, SOURCE_GITHUB):
            raise SkillValidationError("invalid_source_type")
        self._ensure_user_root(safe_user)
        work_root = Path(tempfile.mkdtemp(prefix=".skill_tmp_", dir=self._user_root))
        try:
            extract_zip_safely(Path(zip_path), work_root, subpath=subpath)
            if not (work_root / _SKILL_MD).is_file():
                raise SkillValidationError("SKILL.md_missing")
            entry = load_entry_from_directory(
                work_root,
                source_type=source_type,
                owner_user_id=safe_user,
                source_url=source_url,
                commit_hash=commit_hash,
                require_name_match=False,
            )
            # 结构性失败（缺 SKILL.md、frontmatter 非法、目录名不匹配）没有
            # content_hash，直接报错；安全扫描失败（含密钥样式）才落盘为 rejected。
            if not entry.content_hash:
                raise SkillValidationError(entry.rejection_reason or "invalid_skill_package")
            with self._lock:
                self._ensure_free(safe_user, entry.name)
                final_dir = self._skill_dir(safe_user, entry.name)
                shutil.move(str(work_root), str(final_dir))
                entry = load_entry_from_directory(
                    final_dir,
                    source_type=source_type,
                    owner_user_id=safe_user,
                    source_url=source_url,
                    commit_hash=commit_hash,
                )
                entry = self._persist_meta(entry)
                self._users.setdefault(safe_user, {})[entry.name] = entry
                return entry
        finally:
            shutil.rmtree(work_root, ignore_errors=True)

    def set_status(self, user_id: str, name: str, status: str) -> SkillEntry:
        safe_user = self._require_user(user_id)
        if status not in (STATUS_ADMITTED, STATUS_DISABLED):
            raise SkillValidationError("invalid_status")
        with self._lock:
            entry = self._require_owned(safe_user, name)
            if entry.status == STATUS_REJECTED:
                raise SkillValidationError("skill_rejected_no_toggle")
            updated = self._replace_entry(entry, status=status)
            self._persist_meta(updated)
            self._users[safe_user][name] = updated
            return updated

    def delete_skill(self, user_id: str, name: str) -> None:
        safe_user = self._require_user(user_id)
        with self._lock:
            self._require_owned(safe_user, name)
            shutil.rmtree(self._skill_dir(safe_user, name), ignore_errors=True)
            self._users.get(safe_user, {}).pop(name, None)

    # --------------------------------------------------------------- internals

    def _scan_builtin(self) -> dict[str, SkillEntry]:
        entries: dict[str, SkillEntry] = {}
        if not self._builtin_dir.is_dir():
            return entries
        for child in sorted(self._builtin_dir.iterdir()):
            if child.is_dir() and (child / _SKILL_MD).is_file():
                entry = load_entry_from_directory(child, source_type=SOURCE_BUILTIN)
                entries[entry.name] = entry
        return entries

    def _scan_users(self) -> dict[str, dict[str, SkillEntry]]:
        users: dict[str, dict[str, SkillEntry]] = {}
        if not self._user_root.is_dir():
            return users
        for user_dir in sorted(self._user_root.iterdir()):
            if not user_dir.is_dir() or not USER_ID_PATTERN.fullmatch(user_dir.name):
                continue
            for skill_dir in sorted(user_dir.iterdir()):
                if not skill_dir.is_dir() or not (skill_dir / _SKILL_MD).is_file():
                    continue
                meta = self._meta_of(skill_dir)
                entry = load_entry_from_directory(
                    skill_dir,
                    source_type=str(meta.get("source_type") or SOURCE_UPLOAD),
                    owner_user_id=user_dir.name,
                    source_url=str(meta.get("source_url") or ""),
                    commit_hash=str(meta.get("commit_hash") or ""),
                )
                if entry.status == STATUS_ADMITTED and meta.get("status") == STATUS_DISABLED:
                    entry = self._replace_entry(entry, status=STATUS_DISABLED)
                users.setdefault(user_dir.name, {})[entry.name] = entry
        return users

    def _meta_of(self, skill_dir: Path) -> dict[str, Any]:
        meta_path = Path(skill_dir) / _META_FILE
        if not meta_path.is_file():
            return {}
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _persist_meta(self, entry: SkillEntry) -> SkillEntry:
        skill_dir = Path(entry.dir_path)
        meta_path = skill_dir / _META_FILE
        payload = {
            "status": entry.status,
            "source_type": entry.source_type,
            "source_url": entry.source_url,
            "commit_hash": entry.commit_hash,
            "created_at": entry.created_at or utc_now_iso(),
            "rejection_reason": entry.rejection_reason,
        }
        meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return entry

    def _compose_skill_md(self, name: str, description: str, body: str) -> str:
        return (
            "---\n"
            f"name: {name}\n"
            "version: 1.0.0\n"
            f"description: {json.dumps(description, ensure_ascii=False)}\n"
            "---\n\n"
            + str(body or "").strip()
            + "\n"
        )

    def _ensure_user_root(self, safe_user: str) -> Path:
        root = self._user_root / safe_user
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _skill_dir(self, safe_user: str, skill_name: str) -> Path:
        return self._user_root / safe_user / skill_name

    def _ensure_free(self, safe_user: str, skill_name: str) -> None:
        if self._skill_dir(safe_user, skill_name).exists():
            raise SkillValidationError("skill_already_exists")

    def _require_owned(self, safe_user: str, name: str) -> SkillEntry:
        entry = self._users.get(safe_user, {}).get(name)
        if entry is None:
            if name in self._builtin:
                raise SkillValidationError("builtin_skill_immutable")
            raise KeyError(f"skill_not_found:{name}")
        return entry

    @staticmethod
    def _require_user(user_id: str) -> str:
        safe_user = str(user_id or "").strip()
        if not USER_ID_PATTERN.fullmatch(safe_user):
            raise SkillValidationError("invalid_username")
        return safe_user

    @staticmethod
    def _replace_entry(entry: SkillEntry, **changes: Any) -> SkillEntry:
        data = entry.public_dict(include_body=True)
        data.update(changes)
        return SkillEntry(**data)


_REGISTRY: SkillRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_skill_registry() -> SkillRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = SkillRegistry()
        return _REGISTRY
