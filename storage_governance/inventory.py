from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "secrets.json", "credentials.json", "service_account.json",
    "local_config.py", "local_config.json", "secrets.toml",
}
SENSITIVE_PARTS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__"}

# Stage 3.6A is storage governance, not a whole-repository source scanner.
# Restrict inventory to roots that are expected to contain persisted data,
# runtime state, logs, models, caches, build outputs or backups.
MANAGED_ROOTS = {
    "data", "outputs", "runtime", "models", "logs", "model_store",
    "checkpoints", "dist", "build", "installer_output", "backups", "benchmarks",
}
ROOT_STORAGE_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".pkl", ".pickle", ".pt", ".pth", ".ckpt",
    ".onnx", ".parquet", ".feather", ".arrow", ".h5", ".hdf5", ".npy", ".npz",
    ".csv", ".tsv", ".jsonl", ".log", ".bin", ".zip", ".7z", ".rar", ".tar",
    ".gz", ".tgz", ".exe", ".pkg",
}


@dataclass(frozen=True)
class StorageFile:
    path: str
    size_bytes: int
    modified_at: datetime
    suffix: str

    @property
    def age_days(self) -> float:
        now = datetime.now(timezone.utc)
        m = self.modified_at
        if m.tzinfo is None:
            m = m.replace(tzinfo=timezone.utc)
        return max(0.0, (now - m).total_seconds() / 86400.0)


def _safe_rel(project: Path, path: Path) -> str:
    return str(path.relative_to(project)).replace("\\", "/")


def _is_sensitive(path: Path) -> bool:
    if path.name.lower() in {x.lower() for x in SENSITIVE_NAMES}:
        return True
    lowered = [p.lower() for p in path.parts]
    if any(x.lower() in SENSITIVE_PARTS for x in lowered):
        return True
    name = path.name.lower()
    return any(token in name for token in ("private_key", "credential", "secret"))


def _managed(relative: str, suffix: str) -> bool:
    parts = Path(relative).parts
    if not parts:
        return False
    if len(parts) == 1:
        return suffix in ROOT_STORAGE_SUFFIXES
    return parts[0].lower() in MANAGED_ROOTS


def scan_files(project: str | Path) -> list[StorageFile]:
    root = Path(project).resolve()
    rows: list[StorageFile] = []
    for path in root.rglob("*"):
        try:
            if not path.is_file() or _is_sensitive(path):
                continue
            rel = _safe_rel(root, path)
            suffix = path.suffix.lower()
            if not _managed(rel, suffix):
                continue
            st = path.stat()
        except OSError:
            continue
        rows.append(StorageFile(
            path=rel,
            size_bytes=int(st.st_size),
            modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            suffix=suffix,
        ))
    return rows
