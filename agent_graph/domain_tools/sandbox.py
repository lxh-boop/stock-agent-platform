from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any


SANDBOX_LIMITATION_WARNING = (
    "当前实现是单机项目中的受限分析运行器，不是面向不受信任多租户的硬隔离沙箱。"
)

ALLOWED_IMPORT_ROOTS = {
    "json",
    "math",
    "statistics",
    "datetime",
    "collections",
    "decimal",
    "pandas",
    "numpy",
    "matplotlib",
}

BLOCKED_IMPORT_ROOTS = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "shutil",
    "sqlite3",
    "pathlib",
    "builtins",
    "importlib",
    "http",
    "ftplib",
    "glob",
    "site",
}

BLOCKED_NAMES = {
    "open",
    "eval",
    "exec",
    "compile",
    "input",
    "__import__",
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
    "help",
    "breakpoint",
}

BLOCKED_ATTRIBUTES = {
    "environ",
    "getenv",
    "system",
    "popen",
    "spawn",
    "fork",
    "remove",
    "unlink",
    "rmdir",
    "mkdir",
    "makedirs",
    "rename",
    "replace",
    "chmod",
    "chown",
    "connect",
    "request",
    "urlopen",
    "read_csv",
    "read_json",
    "read_excel",
    "read_parquet",
    "read_pickle",
    "to_csv",
    "to_json",
    "to_excel",
    "to_pickle",
    "to_parquet",
}

MAX_CODE_CHARS = 12_000
RESULT_MARKER = "::AGENT_SANDBOX_RESULT::"


class SandboxSecurityError(ValueError):
    pass


class _SandboxAstValidator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            self._check_import(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        self._check_import(node.module or "")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in BLOCKED_NAMES or node.id in BLOCKED_IMPORT_ROOTS:
            self.errors.append(f"blocked_name:{node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if node.attr.startswith("__") or node.attr in BLOCKED_ATTRIBUTES:
            self.errors.append(f"blocked_attribute:{node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        func = node.func
        if isinstance(func, ast.Name) and func.id in BLOCKED_NAMES:
            self.errors.append(f"blocked_call:{func.id}")
        if isinstance(func, ast.Attribute):
            if func.attr.startswith("__") or func.attr in BLOCKED_ATTRIBUTES:
                self.errors.append(f"blocked_call:{func.attr}")
        self.generic_visit(node)

    def _check_import(self, module_name: str) -> None:
        root = str(module_name or "").split(".", 1)[0]
        if not root:
            return
        if root in BLOCKED_IMPORT_ROOTS:
            self.errors.append(f"blocked_import:{root}")
        elif root not in ALLOWED_IMPORT_ROOTS:
            self.errors.append(f"import_not_allowed:{root}")


def validate_python_analysis_code(code: str) -> list[str]:
    if len(str(code or "")) > MAX_CODE_CHARS:
        return ["code_too_large"]
    try:
        tree = ast.parse(str(code or ""), mode="exec")
    except SyntaxError as exc:
        return [f"syntax_error:{exc.msg}"]

    validator = _SandboxAstValidator()
    validator.visit(tree)
    return sorted(set(validator.errors))


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n...[truncated]", True


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _write_runner(script_path: Path, *, code: str, snapshot: dict[str, Any]) -> None:
    snapshot_json = _safe_json(snapshot)
    runner = f"""
import json
import math
import statistics
import datetime
import collections
import decimal

SNAPSHOT = json.loads({snapshot_json!r})
RESULT = None

{code}

print({RESULT_MARKER!r} + json.dumps({{"result": RESULT}}, ensure_ascii=False, default=str))
"""
    script_path.write_text(textwrap.dedent(runner), encoding="utf-8")


def _minimal_subprocess_env() -> dict[str, str]:
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    for key in ["SYSTEMROOT", "WINDIR", "PATH"]:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def run_python_analysis(
    code: str,
    *,
    snapshot: dict[str, Any] | None = None,
    snapshot_id: str = "",
    timeout_seconds: float = 995.0,
    max_output_chars: int = 4000,
) -> dict[str, Any]:
    started = time.perf_counter()
    code_text = str(code or "")
    code_hash = hashlib.sha256(code_text.encode("utf-8")).hexdigest()
    snapshot = dict(snapshot or {})
    warnings = [SANDBOX_LIMITATION_WARNING]

    validation_errors = validate_python_analysis_code(code_text)
    if validation_errors:
        return {
            "success": False,
            "status": "rejected",
            "result": None,
            "stdout_summary": "",
            "generated_files": [],
            "warnings": warnings,
            "refusal_reason": ";".join(validation_errors),
            "error_type": "sandbox_security_rejected",
            "error_message": "",
            "duration_seconds": round(time.perf_counter() - started, 4),
            "code_hash": code_hash,
            "snapshot_id": snapshot_id,
        }

    with tempfile.TemporaryDirectory(prefix="agent_sandbox_") as temp_name:
        temp_dir = Path(temp_name)
        script_path = temp_dir / "runner.py"
        _write_runner(script_path, code=code_text, snapshot=snapshot)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script_path)],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.1, float(timeout_seconds)),
                env=_minimal_subprocess_env(),
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_summary, truncated = _truncate(str(exc.stdout or ""), max_output_chars)
            if truncated:
                warnings.append("stdout_truncated")
            return {
                "success": False,
                "status": "timeout",
                "result": None,
                "stdout_summary": stdout_summary,
                "generated_files": [],
                "warnings": warnings,
                "refusal_reason": "",
                "error_type": "sandbox_timeout",
                "error_message": f"timeout after {timeout_seconds} seconds",
                "duration_seconds": round(time.perf_counter() - started, 4),
                "code_hash": code_hash,
                "snapshot_id": snapshot_id,
            }

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        result_payload: Any = None
        visible_stdout = stdout
        if RESULT_MARKER in stdout:
            visible_stdout, marker_payload = stdout.rsplit(RESULT_MARKER, 1)
            try:
                result_payload = json.loads(marker_payload.strip()).get("result")
            except Exception:
                result_payload = None

        stdout_summary, truncated = _truncate(visible_stdout.strip(), max_output_chars)
        if truncated:
            warnings.append("stdout_truncated")

        generated_files = []
        for path in temp_dir.iterdir():
            if path.name == script_path.name:
                continue
            if path.is_file():
                generated_files.append({"name": path.name, "size_bytes": path.stat().st_size})

        success = proc.returncode == 0
        return {
            "success": success,
            "status": "succeeded" if success else "failed",
            "result": result_payload,
            "stdout_summary": stdout_summary,
            "generated_files": generated_files,
            "warnings": warnings,
            "refusal_reason": "",
            "error_type": "" if success else "sandbox_execution_error",
            "error_message": "" if success else stderr[-max_output_chars:],
            "duration_seconds": round(time.perf_counter() - started, 4),
            "code_hash": code_hash,
            "snapshot_id": snapshot_id,
        }
