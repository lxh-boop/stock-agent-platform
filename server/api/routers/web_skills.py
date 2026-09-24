"""User skill management API.

技能只对创建者本人可见；内置技能只读。所有路径由 SkillRegistry 以白名单
用户名/skill 名落盘，杜绝路径穿越；GitHub 仅支持公开仓库，用 codeload zip
拉取，不引入 git 依赖。
"""

from __future__ import annotations

import logging
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, File, UploadFile

from agent.skill_runtime import SkillRegistry, SkillValidationError, get_skill_registry
from agent.skill_runtime.loader import parse_github_url
from server.api.contracts import OperationResponse
from server.api.schemas.skills import SkillCreateRequest, SkillImportGithubRequest
from server.api.security import AuthUser, require_user

router = APIRouter(prefix="/api/v1/web/skills", tags=["web-skills"])
logger = logging.getLogger(__name__)

_MAX_GITHUB_ZIP_BYTES = 20_000_000
_GITHUB_TIMEOUT_SECONDS = 45

_SAFE_REASONS = {
    "invalid_skill_name",
    "description_required",
    "description_too_long",
    "skill_md_too_large",
    "frontmatter_missing",
    "frontmatter_invalid_yaml",
    "secret_like_content",
    "skill_already_exists",
    "skill_not_found",
    "builtin_skill_immutable",
    "skill_rejected_no_toggle",
    "invalid_status",
    "invalid_username",
    "unsupported_github_url",
    "zip_slip_detected",
    "too_many_files",
    "package_too_large",
    "SKILL.md_missing",
    "name_mismatch_with_directory",
    "ambiguous_package_roots",
    "skill_md_not_found_in_package",
    "invalid_source_type",
}


def _safe_reason(exc: Exception) -> str:
    raw = str(exc or "")
    if isinstance(exc, SkillValidationError) and raw:
        base = raw.split(":", 1)[0]
        return base if base in _SAFE_REASONS else "skill_validation_failed"
    if isinstance(exc, KeyError):
        return raw.split(":", 1)[0] if raw else "skill_not_found"
    return "skill_operation_failed"


def _call(action: Callable[[], Any], *, user: AuthUser, write: bool = False) -> OperationResponse:
    request_id = uuid.uuid4().hex
    try:
        return OperationResponse(success=True, data=action(), request_id=request_id)
    except Exception as exc:
        logger.exception("Skill operation failed user=%s write=%s", user.user_id, write)
        code = "SKILL_WRITE_REJECTED" if write else "SKILLS_UNAVAILABLE"
        return OperationResponse(
            success=False,
            data=None,
            error={
                "code": code,
                "message": "技能操作未完成" if write else "技能服务暂时不可用",
                "details": {
                    "error_type": type(exc).__name__,
                    "reason": _safe_reason(exc),
                },
            },
            request_id=request_id,
        )


def _registry() -> SkillRegistry:
    return get_skill_registry()


@router.get("", response_model=OperationResponse)
def list_skills(user: AuthUser = Depends(require_user)) -> OperationResponse:
    def action() -> dict[str, Any]:
        entries = _registry().list_for_user(user.user_id)
        return {
            "skills": [entry.public_dict() for entry in entries],
            "user_id": user.user_id,
        }

    return _call(action, user=user)


@router.post("", response_model=OperationResponse)
def create_skill(
    request: SkillCreateRequest, user: AuthUser = Depends(require_user)
) -> OperationResponse:
    def action() -> dict[str, Any]:
        entry = _registry().create_from_editor(
            user.user_id,
            name=request.name,
            description=request.description,
            body=request.body,
        )
        return {"skill": entry.public_dict()}

    return _call(action, user=user, write=True)


@router.post("/upload", response_model=OperationResponse)
def upload_skill_zip(
    user: AuthUser = Depends(require_user),
    file: UploadFile = File(...),
) -> OperationResponse:
    def action() -> dict[str, Any]:
        suffix = Path(str(file.filename or "skill.zip")).suffix or ".zip"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
            while chunk := file.file.read(1_000_000):
                handle.write(chunk)
        try:
            entry = _registry().install_package(
                user.user_id, temp_path, source_type="upload"
            )
            return {"skill": entry.public_dict()}
        finally:
            temp_path.unlink(missing_ok=True)

    return _call(action, user=user, write=True)


@router.post("/import-github", response_model=OperationResponse)
def import_skill_from_github(
    request: SkillImportGithubRequest, user: AuthUser = Depends(require_user)
) -> OperationResponse:
    def action() -> dict[str, Any]:
        owner, repo, branch, subpath = parse_github_url(request.url)
        ref = branch or "HEAD"
        codeload_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}" if branch else f"https://codeload.github.com/{owner}/{repo}/zip/HEAD"
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed https scheme, host pinned to codeload.github.com
                codeload_url, timeout=_GITHUB_TIMEOUT_SECONDS
            ) as response:
                payload = response.read(_MAX_GITHUB_ZIP_BYTES + 1)
            if len(payload) > _MAX_GITHUB_ZIP_BYTES:
                raise SkillValidationError("package_too_large")
            temp_path.write_bytes(payload)
            entry = _registry().install_package(
                user.user_id,
                temp_path,
                source_type="github",
                source_url=str(request.url),
                subpath=subpath,
            )
            return {"skill": entry.public_dict()}
        finally:
            temp_path.unlink(missing_ok=True)

    return _call(action, user=user, write=True)


@router.get("/{name}", response_model=OperationResponse)
def skill_detail(name: str, user: AuthUser = Depends(require_user)) -> OperationResponse:
    def action() -> dict[str, Any]:
        entry = _registry().get(user.user_id, name)
        if entry is None or (entry.owner_user_id and entry.owner_user_id != user.user_id):
            raise KeyError("skill_not_found")
        return {"skill": entry.public_dict(include_body=True)}

    return _call(action, user=user)


@router.post("/{name}/enable", response_model=OperationResponse)
def enable_skill(name: str, user: AuthUser = Depends(require_user)) -> OperationResponse:
    def action() -> dict[str, Any]:
        entry = _registry().set_status(user.user_id, name, "admitted")
        return {"skill": entry.public_dict()}

    return _call(action, user=user, write=True)


@router.post("/{name}/disable", response_model=OperationResponse)
def disable_skill(name: str, user: AuthUser = Depends(require_user)) -> OperationResponse:
    def action() -> dict[str, Any]:
        entry = _registry().set_status(user.user_id, name, "disabled")
        return {"skill": entry.public_dict()}

    return _call(action, user=user, write=True)


@router.delete("/{name}", response_model=OperationResponse)
def delete_skill(name: str, user: AuthUser = Depends(require_user)) -> OperationResponse:
    def action() -> dict[str, Any]:
        _registry().delete_skill(user.user_id, name)
        return {"deleted": name}

    return _call(action, user=user, write=True)
