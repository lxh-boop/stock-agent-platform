from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .policy import load_default_policy


def _policy_value(name: str, fallback: int) -> int:
    try:
        return int(getattr(load_default_policy(), name))
    except Exception:
        return int(fallback)


def select_recent_trading_days(frame, *, keep_trading_days: int | None = None, date_col: str = "date"):
    """Return a copy limited to the latest N distinct trading dates.

    This is a persistence lifecycle helper only. Callers may keep using the full
    in-memory feature frame for model inference/training in the current process.
    """
    import pandas as pd

    if frame is None:
        return frame
    if getattr(frame, "empty", False) or date_col not in frame.columns:
        return frame.copy()

    keep = max(1, int(keep_trading_days or _policy_value("factor_cache_days", 120)))
    dates = pd.to_datetime(frame[date_col], errors="coerce").dt.normalize()
    valid_dates = sorted(d for d in dates.dropna().unique())
    if len(valid_dates) <= keep:
        return frame.copy()

    cutoff = valid_dates[-keep]
    return frame.loc[dates >= cutoff].copy()


def persist_recent_factor_cache(
    frame,
    path: str | Path,
    *,
    keep_trading_days: int | None = None,
    date_col: str = "date",
) -> dict[str, Any]:
    """Atomically persist only the recent factor-cache window."""
    import pandas as pd

    if frame is None:
        raise ValueError("factor cache frame cannot be None")
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    keep = max(1, int(keep_trading_days or _policy_value("factor_cache_days", 120)))
    cache = select_recent_trading_days(frame, keep_trading_days=keep, date_col=date_col)
    tmp = out_path.with_name(f"{out_path.name}.{uuid4().hex}.tmp")
    try:
        cache.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    def _date_count(value) -> int:
        if getattr(value, "empty", False) or date_col not in value.columns:
            return 0
        return int(pd.to_datetime(value[date_col], errors="coerce").dt.normalize().nunique(dropna=True))

    return {
        "path": str(out_path),
        "keep_trading_days": keep,
        "source_rows": int(len(frame)),
        "persisted_rows": int(len(cache)),
        "source_trading_days": _date_count(frame),
        "persisted_trading_days": _date_count(cache),
    }


def rotate_text_log(path: str | Path, *, keep_segments: int | None = None) -> list[str]:
    """Rotate one text log in place using .1 .. .N segments."""
    log_path = Path(path)
    keep = max(1, int(keep_segments or _policy_value("log_rotation_keep", 5)))
    if not log_path.exists():
        return []

    removed: list[str] = []
    oldest = log_path.with_name(f"{log_path.name}.{keep}")
    if oldest.exists():
        oldest.unlink()
        removed.append(str(oldest))
    for index in range(keep - 1, 0, -1):
        source = log_path.with_name(f"{log_path.name}.{index}")
        if source.exists():
            os.replace(source, log_path.with_name(f"{log_path.name}.{index + 1}"))
    os.replace(log_path, log_path.with_name(f"{log_path.name}.1"))
    return removed


def append_bounded_text_log(
    path: str | Path,
    text: str,
    *,
    segment_mb: int | None = None,
    keep_segments: int | None = None,
) -> dict[str, Any]:
    """Append text through the existing owner while bounding file growth."""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    segment = max(1, int(segment_mb or _policy_value("log_rotation_segment_mb", 20)))
    keep = max(1, int(keep_segments or _policy_value("log_rotation_keep", 5)))
    max_bytes = segment * 1024 * 1024
    payload = (str(text) + "\n").encode("utf-8", errors="replace")
    rotated = False
    removed: list[str] = []
    current_size = log_path.stat().st_size if log_path.exists() else 0
    if log_path.exists() and current_size > 0 and current_size + len(payload) > max_bytes:
        removed = rotate_text_log(log_path, keep_segments=keep)
        rotated = True
    with log_path.open("ab") as handle:
        handle.write(payload)
    return {
        "path": str(log_path),
        "rotated": rotated,
        "segment_mb": segment,
        "keep_segments": keep,
        "active_size_bytes": int(log_path.stat().st_size),
        "removed_segments": removed,
    }


def prune_stale_atomic_temp_files(
    target_path: str | Path,
    *,
    max_age_seconds: int = 3600,
) -> list[str]:
    """Remove only stale temp siblings created by atomic-save owners."""
    target = Path(target_path)
    parent = target.parent
    if not parent.exists():
        return []
    threshold = time.time() - max(60, int(max_age_seconds))
    removed: list[str] = []
    pattern = f"{target.name}.*.tmp"
    for candidate in parent.glob(pattern):
        try:
            if candidate.is_file() and candidate.stat().st_mtime < threshold:
                candidate.unlink()
                removed.append(str(candidate))
        except FileNotFoundError:
            continue
    return removed


def _parse_artifact_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        return None


def prune_expired_artifact_payloads(
    artifact_root: str | Path,
    *,
    now: datetime | None = None,
    payload_days: int | None = None,
    expiry_grace_days: int | None = None,
) -> dict[str, Any]:
    """Delete only old tool-result JSON payloads; database metadata remains untouched.

    Reuse expiry and physical retention are intentionally separate. A payload is
    eligible only after BOTH the physical age window and reuse-expiry grace window
    have elapsed. Non-tool artifacts and protected/pinned/audit-like statuses stay.
    """
    root = Path(artifact_root)
    current = now or datetime.now()
    keep_days = max(1, int(payload_days or _policy_value("artifact_payload_days", 30)))
    grace_days = max(0, int(expiry_grace_days if expiry_grace_days is not None else _policy_value("artifact_expiry_grace_days", 7)))
    protected_statuses = {"pinned", "protected", "audit", "retained", "legal_hold"}
    removed: list[str] = []
    skipped_invalid = 0
    scanned = 0
    if not root.exists():
        return {"scanned": 0, "removed": [], "removed_count": 0, "skipped_invalid": 0}

    physical_cutoff = current - timedelta(days=keep_days)
    expiry_cutoff = current - timedelta(days=grace_days)
    for path in root.rglob("artifact_*.json"):
        if not path.is_file():
            continue
        scanned += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            skipped_invalid += 1
            continue
        if str(payload.get("artifact_type") or "") != "tool_result":
            continue
        if str(payload.get("status") or "active").strip().lower() in protected_statuses:
            continue
        created_at = _parse_artifact_datetime(str(payload.get("created_at") or ""))
        expires_at = _parse_artifact_datetime(str(payload.get("expires_at") or ""))
        if created_at is None or expires_at is None:
            skipped_invalid += 1
            continue
        if created_at > physical_cutoff or expires_at > expiry_cutoff:
            continue
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            continue
    return {
        "scanned": scanned,
        "removed": removed,
        "removed_count": len(removed),
        "skipped_invalid": skipped_invalid,
        "payload_days": keep_days,
        "expiry_grace_days": grace_days,
    }


def maybe_prune_expired_artifact_payloads(
    artifact_root: str | Path,
    *,
    state_name: str = ".retention_gc_state",
    interval_minutes: int | None = None,
) -> dict[str, Any]:
    """Throttle artifact payload GC so normal tool writes do not rescan every save."""
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    interval = max(1, int(interval_minutes or _policy_value("artifact_gc_interval_minutes", 60)))
    state = root / state_name
    now_ts = time.time()
    if state.exists():
        try:
            last = float(state.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            last = 0.0
        if now_ts - last < interval * 60:
            return {"ran": False, "reason": "interval_not_elapsed", "interval_minutes": interval}
    result = prune_expired_artifact_payloads(root)
    try:
        tmp = state.with_name(f"{state.name}.{uuid4().hex}.tmp")
        tmp.write_text(str(now_ts), encoding="utf-8")
        os.replace(tmp, state)
    except Exception:
        pass
    return {"ran": True, "interval_minutes": interval, **result}


def _safe_remove_benchmark_workspace(workspace: Path, isolated_root: Path) -> bool:
    """Remove one isolated benchmark workspace only when it is below the owned root."""
    try:
        root = isolated_root.resolve()
        candidate = workspace.resolve(strict=False)
    except OSError:
        return False
    if candidate == root or root not in candidate.parents:
        return False
    try:
        if workspace.is_symlink():
            workspace.unlink(missing_ok=True)
        elif workspace.exists():
            shutil.rmtree(workspace)
        parent = workspace.parent
        if parent != isolated_root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        return True
    except OSError:
        return False


def cleanup_checkpointed_benchmark_workspace(
    workspace_rel: str,
    *,
    benchmark_root: str | Path,
) -> bool:
    """Delete one workspace only after the caller has checkpointed canonical evidence."""
    base = Path(benchmark_root)
    isolated_root = base / "isolated_workspaces"
    rel = Path(str(workspace_rel or ""))
    if not rel.parts or rel.is_absolute() or rel.parts[0] != "isolated_workspaces":
        return False
    return _safe_remove_benchmark_workspace(base / rel, isolated_root)


def prune_stale_benchmark_workspaces(
    benchmark_root: str | Path,
    *,
    max_age_days: int | None = None,
    now_timestamp: float | None = None,
) -> dict[str, Any]:
    """Prune interrupted benchmark workspaces after the configured forensic window."""
    base = Path(benchmark_root)
    isolated_root = base / "isolated_workspaces"
    days = max(1, int(max_age_days or _policy_value("benchmark_workspace_days", 14)))
    threshold = (now_timestamp if now_timestamp is not None else time.time()) - days * 86400
    removed: list[str] = []
    if not isolated_root.exists():
        return {"removed": [], "removed_count": 0, "max_age_days": days}
    for case_dir in list(isolated_root.iterdir()):
        if not case_dir.is_dir() or case_dir.is_symlink():
            continue
        for workspace in list(case_dir.iterdir()):
            if not workspace.name.startswith("iter_"):
                continue
            try:
                modified = workspace.lstat().st_mtime
            except FileNotFoundError:
                continue
            if modified >= threshold:
                continue
            if _safe_remove_benchmark_workspace(workspace, isolated_root):
                removed.append(str(workspace))
    return {"removed": removed, "removed_count": len(removed), "max_age_days": days}

