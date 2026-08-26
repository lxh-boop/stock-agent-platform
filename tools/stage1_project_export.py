# -*- coding: utf-8 -*-
"""
Stage 1 project exporter for D:\stock_daily_app.

Purpose:
1. Inventory every .py file in the project.
2. Produce SHA-256 manifest and project tree.
3. Run syntax checks.
4. Run lightweight architecture checks without importing project modules.
5. Export all .py files, preserving relative paths, into a single upload ZIP.

No third-party dependency is required.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import shutil
import sys
import traceback
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def iter_py_files(project: Path) -> list[Path]:
    # 用户明确要求第一阶段收集“所有 .py 文件”，因此这里不排除 .venv、build 等目录。
    # 只要文件位于项目根目录下并以 .py 结尾，就会被收集。
    files = []
    for p in project.rglob("*.py"):
        try:
            if p.is_file():
                files.append(p)
        except OSError:
            continue
    return sorted(files, key=lambda p: str(p.relative_to(project)).lower())


def rel_posix(path: Path, project: Path) -> str:
    return path.relative_to(project).as_posix()


def write_sha_manifest(files: Iterable[Path], project: Path, dest: Path) -> list[dict]:
    rows = []
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["relative_path", "size_bytes", "sha256"])
        for p in files:
            try:
                row = {
                    "relative_path": rel_posix(p, project),
                    "size_bytes": p.stat().st_size,
                    "sha256": sha256_file(p),
                }
                rows.append(row)
                w.writerow([row["relative_path"], row["size_bytes"], row["sha256"]])
            except Exception as e:
                rows.append({
                    "relative_path": rel_posix(p, project),
                    "size_bytes": None,
                    "sha256": f"ERROR:{type(e).__name__}:{e}",
                })
                w.writerow([rows[-1]["relative_path"], "", rows[-1]["sha256"]])
    return rows


def syntax_check(files: Iterable[Path], project: Path, dest: Path) -> dict:
    ok = 0
    failed = []
    unreadable = []
    dest.parent.mkdir(parents=True, exist_ok=True)

    with dest.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["status", "relative_path", "line", "message"])
        for p in files:
            rel = rel_posix(p, project)
            try:
                raw = p.read_bytes()
                # tokenize.detect_encoding-like behavior through compile() is avoided;
                # utf-8-sig first, then source-declared encoding fallback.
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    import tokenize
                    with p.open("rb") as bf:
                        enc, _ = tokenize.detect_encoding(bf.readline)
                    text = raw.decode(enc)
                ast.parse(text, filename=str(p))
                ok += 1
                w.writerow(["PASS", rel, "", ""])
            except SyntaxError as e:
                failed.append({"path": rel, "line": e.lineno, "message": e.msg})
                w.writerow(["FAIL", rel, e.lineno or "", e.msg or "SyntaxError"])
            except Exception as e:
                unreadable.append({"path": rel, "message": f"{type(e).__name__}: {e}"})
                w.writerow(["ERROR", rel, "", f"{type(e).__name__}: {e}"])

    return {"passed": ok, "failed": failed, "unreadable": unreadable}


def module_name_from_path(rel: str) -> str | None:
    if not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if not all(part.isidentifier() for part in parts):
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def extract_imports(path: Path) -> tuple[set[str], str | None]:
    try:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            import tokenize
            with path.open("rb") as bf:
                enc, _ = tokenize.detect_encoding(bf.readline)
            text = raw.decode(enc)
        tree = ast.parse(text, filename=str(path))
    except Exception as e:
        return set(), f"{type(e).__name__}: {e}"

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports, None


def detect_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    visited = set()
    on_stack = set()
    stack = []
    found = set()
    cycles = []

    def canon_cycle(cyc: list[str]) -> tuple[str, ...]:
        body = cyc[:-1]
        if not body:
            return tuple(cyc)
        rotations = []
        for i in range(len(body)):
            r = body[i:] + body[:i]
            rotations.append(tuple(r))
        best = min(rotations)
        return best

    def dfs(node: str):
        visited.add(node)
        on_stack.add(node)
        stack.append(node)
        for nxt in graph.get(node, set()):
            if nxt not in visited:
                dfs(nxt)
            elif nxt in on_stack:
                try:
                    idx = stack.index(nxt)
                    cyc = stack[idx:] + [nxt]
                    key = canon_cycle(cyc)
                    if key not in found:
                        found.add(key)
                        cycles.append(cyc)
                except ValueError:
                    pass
        stack.pop()
        on_stack.remove(node)

    for node in sorted(graph):
        if node not in visited:
            dfs(node)
    return cycles


def architecture_check(files: list[Path], project: Path, dest: Path) -> dict:
    rels = [rel_posix(p, project) for p in files]
    module_to_rel = {}
    rel_to_module = {}
    warnings = []

    for rel in rels:
        mod = module_name_from_path(rel)
        if mod:
            if mod in module_to_rel:
                warnings.append(f"Duplicate module name: {mod} -> {module_to_rel[mod]} / {rel}")
            else:
                module_to_rel[mod] = rel
                rel_to_module[rel] = mod
        else:
            warnings.append(f"Non-importable Python path name: {rel}")

    graph = defaultdict(set)
    parse_errors = []
    for p in files:
        rel = rel_posix(p, project)
        mod = rel_to_module.get(rel)
        if not mod:
            continue
        imports, err = extract_imports(p)
        if err:
            parse_errors.append(f"{rel}: {err}")
            continue
        for imp in imports:
            # Match the closest project-local module prefix.
            candidates = [m for m in module_to_rel if imp == m or imp.startswith(m + ".") or m.startswith(imp + ".")]
            if candidates:
                target = max(candidates, key=len)
                if target != mod:
                    graph[mod].add(target)

    cycles = detect_cycles(dict(graph))

    top_levels = defaultdict(int)
    for rel in rels:
        top = rel.split("/", 1)[0]
        top_levels[top] += 1

    large_files = []
    for p in files:
        try:
            if p.stat().st_size > 2 * 1024 * 1024:
                large_files.append((rel_posix(p, project), p.stat().st_size))
        except OSError:
            pass

    report = {
        "python_file_count": len(files),
        "importable_module_count": len(module_to_rel),
        "top_level_python_distribution": dict(sorted(top_levels.items())),
        "local_import_cycle_count": len(cycles),
        "local_import_cycles": cycles[:100],
        "large_python_files_over_2mb": large_files,
        "warnings": warnings[:500],
        "parse_errors": parse_errors[:500],
        "note": (
            "Architecture check is static and non-invasive: it does not import or execute project modules. "
            "Circular-import detection is best-effort and reports local static import cycles as warnings."
        ),
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def write_tree(files: list[Path], project: Path, dest: Path) -> None:
    rels = [rel_posix(p, project) for p in files]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(rels) + ("\n" if rels else ""), encoding="utf-8")


def export_zip(
    project: Path,
    files: list[Path],
    inventory_dir: Path,
    output_zip: Path,
) -> tuple[int, list[dict]]:
    copied = 0
    errors = []
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in files:
            rel = rel_posix(p, project)
            arc = f"project_py/{rel}"
            try:
                zf.write(p, arc)
                copied += 1
            except Exception as e:
                errors.append({"path": rel, "error": f"{type(e).__name__}: {e}"})

        for meta in inventory_dir.rglob("*"):
            if meta.is_file():
                arc = f"_inventory/{meta.relative_to(inventory_dir).as_posix()}"
                zf.write(meta, arc)

    return copied, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--delivery", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    project = Path(args.project).resolve()
    delivery = Path(args.delivery).resolve()
    run_dir = Path(args.run_dir).resolve()
    inventory = run_dir / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)

    if not project.exists() or not project.is_dir():
        print(f"[FATAL] Project directory not found: {project}")
        return 10

    files = iter_py_files(project)
    if not files:
        print(f"[FATAL] No .py files found under: {project}")
        return 11

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = inventory / "all_py_sha256.csv"
    rows = write_sha_manifest(files, project, manifest_path)
    write_tree(files, project, inventory / "all_py_tree.txt")
    syntax = syntax_check(files, project, inventory / "syntax_check.tsv")
    arch = architecture_check(files, project, inventory / "architecture_check.json")

    summary = {
        "stage": "Stage 1 - All Python Files Export",
        "generated_at_local": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(project),
        "delivery_root": str(delivery),
        "python_file_count": len(files),
        "syntax_pass_count": syntax["passed"],
        "syntax_fail_count": len(syntax["failed"]),
        "syntax_unreadable_count": len(syntax["unreadable"]),
        "architecture_local_import_cycle_count": arch["local_import_cycle_count"],
        "architecture_warning_count": len(arch["warnings"]),
        "sha256_manifest": "all_py_sha256.csv",
        "tree": "all_py_tree.txt",
        "scope_note": "Every .py file under the project root is included; no directory exclusions are applied.",
    }
    (inventory / "project_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    output_zip = delivery / f"stock_daily_app_STAGE1_ALL_PY_UPLOAD_{timestamp}.zip"
    copied, export_errors = export_zip(project, files, inventory, output_zip)

    results = {
        **summary,
        "exported_python_file_count": copied,
        "export_error_count": len(export_errors),
        "export_errors": export_errors,
        "output_zip": str(output_zip),
        "output_zip_sha256": sha256_file(output_zip) if output_zip.exists() else None,
    }
    (run_dir / "stage1_test_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 72)
    print("STAGE 1 ALL-PY EXPORT RESULT")
    print("=" * 72)
    print(f"Project:             {project}")
    print(f"Python files found:  {len(files)}")
    print(f"Syntax PASS:         {syntax['passed']}")
    print(f"Syntax FAIL:         {len(syntax['failed'])}")
    print(f"Unreadable:          {len(syntax['unreadable'])}")
    print(f"Import cycles:       {arch['local_import_cycle_count']} (warning only)")
    print(f"Exported files:      {copied}")
    print(f"Export errors:       {len(export_errors)}")
    print(f"Upload ZIP:          {output_zip}")
    if output_zip.exists():
        print(f"Upload ZIP SHA-256:  {sha256_file(output_zip)}")
    print("=" * 72)

    # For the upload/inspection stage, syntax or architecture warnings do not block packaging.
    # Fatal export errors do produce a non-zero status.
    return 20 if export_errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[ABORTED] Interrupted by user.")
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(99)
