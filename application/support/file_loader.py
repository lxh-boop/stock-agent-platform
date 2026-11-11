from __future__ import annotations

from core.user_scope import owned_path

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class LoadResult:
    ok: bool
    data: Any
    message: str = ""
    path: str = ""
    missing_columns: list[str] | None = None


def safe_read_csv(
    path: str | Path,
    required_columns: list[str] | tuple[str, ...] | None = None,
    dtype: dict | None = None,
    parse_dates: list[str] | tuple[str, ...] | None = None,
) -> LoadResult:
    file_path = owned_path(path)
    required = list(required_columns or [])

    if not file_path.exists():
        return LoadResult(False, pd.DataFrame(), f"文件不存在：{file_path}", str(file_path), required)
    if file_path.stat().st_size <= 0:
        return LoadResult(False, pd.DataFrame(), f"文件为空：{file_path}", str(file_path), required)

    errors = []
    for encoding in ["utf-8-sig", "utf-8", "gbk"]:
        try:
            df = pd.read_csv(file_path, dtype=dtype, encoding=encoding)
            break
        except Exception as exc:
            errors.append(f"{encoding}: {type(exc).__name__}: {exc}")
    else:
        return LoadResult(False, pd.DataFrame(), f"CSV 读取失败：{'; '.join(errors)}", str(file_path), required)

    missing = [col for col in required if col not in df.columns]
    if missing:
        return LoadResult(False, df, f"CSV 缺少必要字段：{missing}", str(file_path), missing)

    df = df.replace([np.inf, -np.inf], np.nan)

    date_errors = []
    for col in parse_dates or []:
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce")
            invalid_count = int(parsed.isna().sum() - df[col].isna().sum())
            if invalid_count > 0:
                date_errors.append(f"{col} 有 {invalid_count} 个无法解析的日期")
            df[col] = parsed

    message = "读取成功"
    if date_errors:
        message = "；".join(date_errors)
    return LoadResult(True, df, message, str(file_path), [])


def safe_read_json(path: str | Path) -> LoadResult:
    file_path = owned_path(path)

    if not file_path.exists():
        return LoadResult(False, {}, f"文件不存在：{file_path}", str(file_path), [])
    if file_path.stat().st_size <= 0:
        return LoadResult(False, {}, f"文件为空：{file_path}", str(file_path), [])

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return LoadResult(False, {}, f"JSON 读取失败：{type(exc).__name__}: {exc}", str(file_path), [])

    return LoadResult(True, data, "读取成功", str(file_path), [])
