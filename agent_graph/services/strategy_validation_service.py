from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


FORBIDDEN_IMPORT_ROOTS = {
    "ccxt",
    "easytrader",
    "ib_insync",
    "pytdx",
    "requests",
    "socket",
    "subprocess",
}
FORBIDDEN_CALLS = {
    "eval",
    "exec",
    "compile",
    "open",
    "__import__",
    "system",
    "popen",
}
FORBIDDEN_TEXT = {
    "WriteGateway",
    "execute_confirmed_plan",
    "confirmation_token",
    "TUSHARE_TOKEN",
    "api_key=",
    "secret=",
}


class StrategyValidationService:
    """Phase-2 static boundary scan; Phase 3 performs the full validation."""

    def scan_generated_code(self, generated_root: Path) -> dict[str, Any]:
        errors: list[str] = []
        files = sorted(generated_root.rglob("*.py")) if generated_root.exists() else []
        for path in files:
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                errors.append(f"syntax_error:{path.name}:{exc.lineno}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else [str(node.module or "")]
                    )
                    for name in names:
                        root = name.split(".", 1)[0]
                        if root in FORBIDDEN_IMPORT_ROOTS:
                            errors.append(f"forbidden_import:{root}")
                if isinstance(node, ast.Call):
                    func_name = ""
                    if isinstance(node.func, ast.Name):
                        func_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        func_name = node.func.attr
                    if func_name in FORBIDDEN_CALLS:
                        errors.append(f"forbidden_call:{func_name}")
            for marker in FORBIDDEN_TEXT:
                if marker in source:
                    errors.append(f"forbidden_text:{marker}")
        return {
            "status": "passed" if not errors else "failed",
            "scanned_files": [str(path) for path in files],
            "errors": sorted(set(errors)),
            "phase": "prepare_implementation",
        }
