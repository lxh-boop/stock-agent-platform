from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import agent.llm_audit as llm_audit
from agent.skill_runtime.injection import (
    PER_SKILL_BODY_CHARS,
    build_skill_block,
)
from agent.skill_runtime.loader import (
    extract_zip_safely,
    parse_github_url,
    parse_skill_md,
    security_scan,
)
from agent.skill_runtime.models import (
    STATUS_ADMITTED,
    STATUS_DISABLED,
    STATUS_REJECTED,
    SkillEntry,
    SkillValidationError,
)
from agent.skill_runtime.registry import SkillRegistry

VALID_MD = """---
name: my_checklist
version: 1.2.0
description: 检查信号的方法清单。
---

## Purpose

按清单核对信号。

## Constraints

- 不做预测。
"""


def _write_skill(root: Path, name: str, body: str = VALID_MD) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def _entry(**overrides: object) -> SkillEntry:
    base = dict(
        name="alpha_skill",
        version="1.0.0",
        description="测试技能",
        body="方法正文",
        source_type="editor",
        owner_user_id="alice",
        dir_path="/tmp/alpha_skill",
        content_hash="a" * 64,
        status=STATUS_ADMITTED,
    )
    base.update(overrides)
    return SkillEntry(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------- loader


def test_parse_skill_md_splits_frontmatter_and_body() -> None:
    frontmatter, body = parse_skill_md(VALID_MD)
    assert frontmatter["name"] == "my_checklist"
    assert frontmatter["version"] == "1.2.0"
    assert body.startswith("## Purpose")


def test_parse_skill_md_rejects_missing_frontmatter() -> None:
    with pytest.raises(SkillValidationError):
        parse_skill_md("# no frontmatter")


def test_security_scan_flags_secret_like_lines() -> None:
    passed, reason, _ = security_scan("api_key = sk-123abc", [])
    assert not passed
    assert reason.startswith("secret_like_content")


def test_security_scan_detects_script_files_but_never_runs_them() -> None:
    passed, _, has_scripts = security_scan("正文", ["scripts/run.py", "SKILL.md"])
    assert passed
    assert has_scripts


def test_parse_github_url_supports_repo_and_subpath() -> None:
    assert parse_github_url("https://github.com/owner/repo") == ("owner", "repo", "", "")
    assert parse_github_url("https://github.com/owner/repo.git") == ("owner", "repo", "", "")
    assert parse_github_url(
        "https://github.com/owner/repo/tree/main/skills/demo"
    ) == ("owner", "repo", "main", "skills/demo")
    with pytest.raises(SkillValidationError):
        parse_github_url("https://gitlab.com/owner/repo")


def test_extract_zip_safely_blocks_zip_slip(tmp_path: Path) -> None:
    malicious = tmp_path / "evil.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    target = tmp_path / "out"
    with pytest.raises(SkillValidationError):
        extract_zip_safely(malicious, target)
    assert not target.exists()


def test_extract_zip_safely_extracts_and_strips_single_root(tmp_path: Path) -> None:
    package = tmp_path / "pkg.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("repo-main/SKILL.md", VALID_MD)
        archive.writestr("repo-main/references/notes.md", "note")
    target = tmp_path / "out"
    extracted = extract_zip_safely(package, target)
    assert sorted(extracted) == ["SKILL.md", "references/notes.md"]
    assert (target / "SKILL.md").is_file()


# ------------------------------------------------------------------- registry


def test_registry_separates_builtin_and_user_skills_with_isolation(tmp_path: Path) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "runtime" / "skills"
    _write_skill(builtin_root, "platform_skill")
    registry = SkillRegistry(builtin_dir=builtin_root, user_root=user_root)

    created = registry.create_from_editor(
        "alice",
        name="alice_skill",
        description="Alice 的技能",
        body="## Purpose\n检查。",
    )
    assert created.status == STATUS_ADMITTED
    assert created.owner_user_id == "alice"

    # 内置技能对所有人可见；个人技能只对本人可见。
    assert [entry.name for entry in registry.list_for_user("alice")] == [
        "platform_skill",
        "alice_skill",
    ]
    assert [entry.name for entry in registry.list_for_user("bob")] == ["platform_skill"]

    # 目录布局：runtime/skills/<user_id>/<skill_name>/SKILL.md
    assert (user_root / "alice" / "alice_skill" / "SKILL.md").is_file()


def test_registry_rejects_secret_like_body_without_writing(tmp_path: Path) -> None:
    registry = SkillRegistry(builtin_dir=tmp_path / "none", user_root=tmp_path / "u")
    with pytest.raises(SkillValidationError):
        registry.create_from_editor(
            "alice", name="leaky", description="泄漏", body="password = hunter2!"
        )
    assert not (tmp_path / "u" / "alice" / "leaky").exists()


def test_registry_disable_and_reject_priority(tmp_path: Path) -> None:
    registry = SkillRegistry(builtin_dir=tmp_path / "none", user_root=tmp_path / "u")
    registry.create_from_editor("alice", name="toggled", description="d", body="正文")
    disabled = registry.set_status("alice", "toggled", STATUS_DISABLED)
    assert disabled.status == STATUS_DISABLED
    assert registry.admitted_for_user("alice") == []

    # 落盘内容被改成含密钥样式 → 扫描 rejected 优先于用户开关。
    skill_md = registry.user_root / "alice" / "toggled" / "SKILL.md"
    skill_md.write_text(
        "---\nname: toggled\ndescription: d\n---\n\napi_key = supersecret",
        encoding="utf-8",
    )
    registry.refresh()
    entry = registry.get("alice", "toggled")
    assert entry is not None and entry.status == STATUS_REJECTED
    with pytest.raises(SkillValidationError):
        registry.set_status("alice", "toggled", STATUS_ADMITTED)


def test_registry_delete_and_builtin_immutability(tmp_path: Path) -> None:
    builtin_root = tmp_path / "builtin"
    _write_skill(builtin_root, "platform_skill")
    registry = SkillRegistry(builtin_dir=builtin_root, user_root=tmp_path / "u")
    registry.create_from_editor("alice", name="mine", description="d", body="正文")

    with pytest.raises(SkillValidationError):
        registry.delete_skill("alice", "platform_skill")
    registry.delete_skill("alice", "mine")
    assert registry.get("alice", "mine") is None
    assert not (tmp_path / "u" / "alice" / "mine").exists()


def test_registry_install_zip_with_github_provenance(tmp_path: Path) -> None:
    registry = SkillRegistry(builtin_dir=tmp_path / "none", user_root=tmp_path / "u")
    package = tmp_path / "gh.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("repo-main/SKILL.md", VALID_MD.replace("my_checklist", "imported_skill"))
    entry = registry.install_package(
        "alice",
        package,
        source_type="github",
        source_url="https://github.com/owner/repo",
        commit_hash="abc123",
    )
    assert entry.status == STATUS_ADMITTED
    assert entry.source_url == "https://github.com/owner/repo"
    assert entry.commit_hash == "abc123"
    assert registry.get("alice", "imported_skill") is not None

    # 同名重复导入被拒绝。
    with pytest.raises(SkillValidationError):
        registry.install_package("alice", package, source_type="github")


# ------------------------------------------------------------------ injection


def test_build_skill_block_puts_platform_constraints_last() -> None:
    block, manifest = build_skill_block([_entry()])
    assert block.startswith("## 适用技能")
    assert "方法正文" in block
    assert block.rstrip().endswith("一律以平台约束为准。")
    assert manifest == [
        {"name": "alpha_skill", "version": "1.0.0", "content_hash": "a" * 64}
    ]


def test_build_skill_block_skips_non_admitted_and_respects_worker_filter() -> None:
    entries = [
        _entry(name="off_skill", status=STATUS_DISABLED),
        _entry(name="w04_only", apply_to_workers=("W04",)),
    ]
    assert build_skill_block(entries, worker_id="W05")[0] == ""
    block, manifest = build_skill_block(entries, worker_id="W04")
    assert "w04_only" in block and manifest[0]["name"] == "w04_only"


def test_build_skill_block_truncates_body_budget() -> None:
    entry = _entry(name="long_skill", body="x" * (PER_SKILL_BODY_CHARS * 4))
    block, _ = build_skill_block([entry])
    assert "x" * (PER_SKILL_BODY_CHARS + 1) not in block


def test_note_applied_skills_binds_manifest_into_audit_context() -> None:
    llm_audit.note_applied_skills(
        [{"name": "alpha_skill", "version": "1.0.0", "content_hash": "a" * 64}]
    )
    context = llm_audit._CONTEXT.get()
    assert context["applied_skills"][0]["name"] == "alpha_skill"
    llm_audit.note_applied_skills([])
    assert json.dumps(llm_audit._CONTEXT.get()["applied_skills"]) == "[]"
