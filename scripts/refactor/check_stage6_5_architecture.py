from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED = [
    "docker-compose.yml",
    "Dockerfile.compose",
    "frontend/Dockerfile",
    "frontend/nginx.conf",
    "application/support/__init__.py",
    "application/support/file_loader.py",
    "application/support/backtest_display.py",
    "application/support/model_search_results.py",
    "scripts/docker/start_compose.ps1",
    "scripts/docker/test_stage6_5.ps1",
    "scripts/refactor/stage6_5_browser_acceptance.py",
]

RETIRED = [
    "app.py",
    "app",
    "client",
    "requirements-compose-streamlit.txt",
    "docker-compose.react-preview.yml",
    ".streamlit",
]

SCAN_ROOTS = [
    "agent",
    "application",
    "server",
    "pipelines",
    "portfolio",
    "scheduler",
    "frontend/src",
]


def python_imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except Exception:
        return []
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def main() -> int:
    violations: list[str] = []

    for relative in REQUIRED:
        if not (ROOT / relative).exists():
            violations.append(f"required file missing: {relative}")
    for relative in RETIRED:
        if (ROOT / relative).exists():
            violations.append(f"retired Streamlit/Python-client artifact remains: {relative}")

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service_lines: list[str] = []
    in_services = False
    for line in compose.splitlines():
        if line.strip() == "services:" and not line.startswith((" ", "\t")):
            in_services = True
            continue
        if (
            in_services
            and line.strip()
            and not line.lstrip().startswith("#")
            and not line.startswith((" ", "\t"))
        ):
            break
        if (
            in_services
            and line.startswith("  ")
            and not line.startswith("    ")
            and line.strip().endswith(":")
        ):
            service_lines.append(line.strip())
    required_services = {"api:", "postgres:", "frontend:"}
    if set(service_lines) != required_services:
        violations.append(
            "production Compose services must be api/postgres/frontend only: "
            f"{service_lines}"
        )
    for forbidden in ("streamlit", "react-preview", "8501", "_stcore"):
        if forbidden.lower() in compose.lower():
            violations.append(f"production Compose contains retired marker: {forbidden}")
    for marker in ("${STOCK_APP_WEB_PORT:-3000}:80", "condition: service_healthy", "compose-react"):
        if marker not in compose:
            violations.append(f"production Compose marker missing: {marker}")

    dockerfile = (ROOT / "Dockerfile.compose").read_text(encoding="utf-8")
    for forbidden in ("AS streamlit", "requirements-compose-streamlit", "streamlit run"):
        if forbidden.lower() in dockerfile.lower():
            violations.append(f"Dockerfile contains retired marker: {forbidden}")

    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    for marker in ("proxy_pass http://api:8010", "proxy_buffering off", "X-Accel-Buffering", "try_files $uri $uri/ /index.html"):
        if marker not in nginx:
            violations.append(f"Nginx production marker missing: {marker}")

    layout = (ROOT / "frontend/src/layouts/AppLayout.tsx").read_text(encoding="utf-8")
    for forbidden in ("预览", "Streamlit 对照"):
        if forbidden in layout:
            violations.append(f"production header contains preview marker: {forbidden}")

    import_violations: list[str] = []
    checked_python = 0
    for scan_root in SCAN_ROOTS:
        base = ROOT / scan_root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            checked_python += 1
            for module in python_imports(path):
                if module == "streamlit" or module.startswith("streamlit."):
                    import_violations.append(f"{path.relative_to(ROOT).as_posix()}: {module}")
                if module == "app" or module.startswith("app."):
                    import_violations.append(f"{path.relative_to(ROOT).as_posix()}: {module}")
                if module == "client" or module.startswith("client."):
                    import_violations.append(f"{path.relative_to(ROOT).as_posix()}: {module}")
    violations.extend(f"retired dependency import: {item}" for item in import_violations)

    frontend_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (ROOT / "frontend/src").rglob("*")
        if path.is_file() and path.suffix in {".ts", ".tsx"}
    )
    for forbidden in (r"[A-Za-z]:\\", "confirmation_token", "http://api:8010"):
        if re.search(forbidden, frontend_text, re.I):
            violations.append(f"frontend contains forbidden server/private marker: {forbidden}")

    start_script = (ROOT / "scripts/docker/start_compose.ps1").read_text(encoding="utf-8-sig")
    for marker in ("api frontend", "http://127.0.0.1:3000/healthz", "--remove-orphans"):
        if marker not in start_script:
            violations.append(f"production start script marker missing: {marker}")
    for forbidden in ("8501", "streamlit", "react-preview"):
        if forbidden.lower() in start_script.lower():
            violations.append(f"production start script contains retired marker: {forbidden}")

    report = {
        "stage": "6.5",
        "checked_python_files": checked_python,
        "compose_services": service_lines,
        "violation_count": len(violations),
        "violations": violations,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
