from __future__ import annotations

import contextvars
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import traceback
from typing import Any
import uuid


_CURRENT_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_flow_current_run_id",
    default="",
)

_LOCK = threading.RLock()
_RUN_FILES: dict[str, Path] = {}
_RUN_SEQUENCE: dict[str, int] = {}
_RUN_FINALIZED: set[str] = set()
_RUN_TOOL_EXECUTIONS: dict[str, list[dict[str, Any]]] = {}
_RUN_LLM_EXECUTIONS: dict[str, list[dict[str, Any]]] = {}
_RUN_DEBUG_FILES: dict[str, Path] = {}
_RUN_DEBUG_SEQUENCE: dict[str, int] = {}

_SECRET_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|credential|"
    r"confirmation[_-]?token|tushare[_-]?token)",
    flags=re.IGNORECASE,
)
_PATH_KEY_PATTERN = re.compile(
    r"(?:db[_-]?path|database[_-]?path|file[_-]?path|local[_-]?path|"
    r"absolute[_-]?path|stack[_-]?trace|traceback|raw[_-]?payload)",
    flags=re.IGNORECASE,
)
_SAFE_TOKEN_METRIC_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_prompt_tokens",
    "max_output_tokens",
}
_WINDOWS_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:\\(?:[^\\\r\n]+\\)*[^\\\r\n]*)"
)
_SECRET_TEXT_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password|confirmation[_-]?token)"
        r"\s*[:=：]\s*([^\s,;]+)"
    ),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{8,})\b"),
)

_STAGE_LABELS = {
    "REQUEST": "用户请求",
    "CONTEXT": "上下文构建",
    "RULE_HINTS": "规则提示",
    "LLM_USER_GOAL": "用户目标识别",
    "GOAL_REVIEW": "目标审查",
    "TASK_PLAN": "任务计划",
    "PLAN_REVIEW": "计划审查",
    "SAFETY_VALIDATION": "安全校验",
    "TASK_PLAN_EXECUTION": "任务计划执行",
    "TASK_START": "任务开始",
    "TASK_RESULT": "任务结果",
    "COMPLETION_OBSERVE": "完成度观察",
    "OBSERVATION": "执行观察",
    "REPLAN": "重新规划",
    "REPORT": "回答生成",
    "FINAL_REPORT": "最终回答",
    "CRITIC": "结果审查",
    "UI": "页面输出",
    "EXCEPTION": "异常记录",
    "GRAPH_RUNTIME_INITIALIZATION_STARTED": "金融图运行时初始化开始",
    "GRAPH_RUNTIME_INITIALIZATION_COMPLETED": "金融图运行时初始化完成",
    "GRAPH_RUNTIME_INITIALIZATION_FAILED": "金融图运行时初始化失败",
    "MAIN_ENTRY_DECISION_STARTED": "MainAgent 入口判断开始",
    "MAIN_ENTRY_DECISION_COMPLETED": "MainAgent 入口判断完成",
    "GRAPH_REF_RESOLUTION_STARTED": "GraphRef 解析开始",
    "GRAPH_REF_RESOLUTION_COMPLETED": "GraphRef 解析完成",
    "WORKER_PLANNING_STARTED": "Worker DAG 规划开始",
    "LOCAL_LLM_REQUEST_STARTED": "本地模型规划请求开始",
    "LOCAL_LLM_RESPONSE_RECEIVED": "本地模型规划响应已返回",
    "WORKER_PLAN_CANDIDATE_GENERATED": "候选 Worker DAG 已生成",
    "WORKER_PLAN_VALIDATION_FAILED": "候选 Worker DAG 校验失败",
    "WORKER_PLAN_REPAIR_STARTED": "完整 Worker DAG 重新规划开始",
    "WORKER_PLAN_REPAIR_RESPONSE_RECEIVED": "重新规划响应已返回",
    "WORKER_PLAN_REPAIR_CANDIDATE_GENERATED": "重新规划候选 DAG 已生成",
    "WORKER_PLAN_REPAIR_SUCCEEDED": "重新规划通过",
    "WORKER_PLAN_REPAIR_FAILED": "重新规划失败",
    "WORKER_PLAN_ACCEPTED": "Worker DAG 候选已接受",
    "WORKER_DAG_VALIDATED": "Worker DAG 合同校验通过",
    "WORKER_PLANNING_COMPLETED": "Worker DAG 规划完成",
    "WORKER_PLANNING_FAILED": "Worker DAG 规划失败",
    "WORKER_DAG_REGISTERED": "Worker DAG 已登记",
    "WORKER_EXECUTION_STARTED": "Worker DAG 执行开始",
    "WORKER_EXECUTION_COMPLETED": "Worker DAG 执行完成",
    "LLM_TIMING_BREAKDOWN": "LLM 分阶段耗时",
    "TOOL_EXECUTION_STARTED": "工具执行开始",
    "TOOL_EXECUTION_SUCCEEDED": "工具执行成功",
    "TOOL_EXECUTION_FAILED": "工具执行失败",
    "TOOL_EXECUTION_BLOCKED": "工具执行被阻止",
    "RUN_FAILED": "Agent Run 失败",
}


def _env_truthy(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def flow_trace_enabled() -> bool:
    """Whether the human-readable Agent flow document is enabled."""
    return _env_truthy("AGENT_FLOW_TRACE", default=True)


def console_trace_enabled() -> bool:
    """Whether dense technical trace events should enter the Markdown."""
    return _env_truthy("AGENT_CONSOLE_TRACE", default=False)


is_flow_trace_enabled = flow_trace_enabled
is_console_trace_enabled = console_trace_enabled




def runtime_debug_enabled() -> bool:
    """Whether the code-level runtime JSONL debug log is enabled."""
    return _env_truthy("AGENT_RUNTIME_DEBUG", default=True)


def _runtime_debug_directory() -> Path:
    configured = str(os.getenv("AGENT_RUNTIME_DEBUG_DIR", "")).strip()
    path = Path(configured) if configured else Path.cwd() / "outputs" / "agent_runtime_debug"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_source_file(filename: str) -> str:
    try:
        path = Path(str(filename or "")).resolve()
        cwd = Path.cwd().resolve()
        try:
            return path.relative_to(cwd).as_posix()
        except ValueError:
            return path.name
    except Exception:
        return Path(str(filename or "unknown")).name


def _debug_safe(value: Any, *, depth: int = 0) -> Any:
    """Bounded JSON-safe payload for developer diagnostics.

    Unlike the Markdown renderer this keeps deeper contract structure, but still
    redacts secret-like fields and never writes absolute local paths.
    """
    if depth > 12:
        return "<debug_max_depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _redact_text(value)
        return text if len(text) <= 8000 else text[:8000] + "...[truncated]"
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [_debug_safe(item, depth=depth + 1) for item in items[:120]]
        if len(items) > 120:
            out.append({"truncated_count": len(items) - 120})
        return out
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:240]:
            name = str(key)
            if _SECRET_KEY_PATTERN.search(name):
                out[name] = "[redacted]"
            elif _PATH_KEY_PATTERN.search(name) and name not in {"source_file", "file", "module"}:
                out[name] = "[redacted path]"
            else:
                out[name] = _debug_safe(item, depth=depth + 1)
        if len(value) > 240:
            out["__truncated_key_count__"] = len(value) - 240
        return out
    if hasattr(value, "to_dict"):
        try:
            return _debug_safe(value.to_dict(), depth=depth + 1)
        except Exception:
            pass
    return _debug_safe(str(value), depth=depth + 1)


def runtime_debug_event(
    stage: str,
    payload: Any = None,
    *,
    run_id: str = "",
    task_id: str = "",
    level: str = "DEBUG",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Write one code-level runtime diagnostic event as JSONL.

    This log is deliberately separate from the human-readable Agent flow Markdown.
    It records compiler/binder/gate/state-machine inputs and outputs without
    exposing secrets or absolute local paths.
    """
    if not runtime_debug_enabled():
        return ""
    try:
        canonical_run_id = _resolve_run_id(str(stage or "DEBUG").upper(), payload, run_id)
        with _LOCK:
            path = _RUN_DEBUG_FILES.get(canonical_run_id)
            if path is None:
                path = _runtime_debug_directory() / f"{_safe_file_name(canonical_run_id)}.jsonl"
                _RUN_DEBUG_FILES[canonical_run_id] = path
                _RUN_DEBUG_SEQUENCE[canonical_run_id] = 0
            seq = _RUN_DEBUG_SEQUENCE.get(canonical_run_id, 0) + 1
            _RUN_DEBUG_SEQUENCE[canonical_run_id] = seq
            record = {
                "sequence": seq,
                "time": datetime.now().isoformat(timespec="milliseconds"),
                "run_id": canonical_run_id,
                "task_id": str(task_id or ""),
                "stage": str(stage or "DEBUG"),
                "level": str(level or "DEBUG"),
                "payload": _debug_safe(payload),
                "metadata": _debug_safe(dict(metadata or {})),
            }
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=False, default=str) + "\n")
            return str(path)
    except Exception:
        return ""

def _max_chars() -> int:
    raw = os.getenv("AGENT_FLOW_TRACE_MAX_CHARS", "30000")
    try:
        return max(2000, min(int(raw), 200000))
    except (TypeError, ValueError):
        return 30000


def _max_depth() -> int:
    raw = os.getenv("AGENT_FLOW_TRACE_MAX_DEPTH", "8")
    try:
        return max(2, min(int(raw), 20))
    except (TypeError, ValueError):
        return 8


def _redact_text(value: str) -> str:
    text = str(value or "")
    text = _WINDOWS_PATH_PATTERN.sub("[redacted local path]", text)

    for pattern in _SECRET_TEXT_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(
                lambda match: f"{match.group(1)}=[redacted]",
                text,
            )
        else:
            text = pattern.sub("[redacted secret]", text)

    if "traceback (most recent call last)" in text.lower():
        return "[redacted internal traceback]"
    return text


def sanitize_for_trace(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe, bounded and redacted trace payload."""
    if depth > _max_depth():
        return "<max_depth>"

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        text = _redact_text(value)
        limit = _max_chars()
        return text if len(text) <= limit else text[:limit] + "\n...[truncated]"

    if isinstance(value, Path):
        return "[redacted local path]"

    if isinstance(value, BaseException):
        return {
            "exception_type": type(value).__name__,
            "message": _redact_text(str(value)),
        }

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 200:
                result["..."] = "<remaining keys truncated>"
                break

            key = str(raw_key)
            if key.lower() in _SAFE_TOKEN_METRIC_KEYS and isinstance(item, (int, float)):
                result[key] = item
                continue
            if _SECRET_KEY_PATTERN.search(key):
                result[key] = "[redacted secret]"
                continue
            if _PATH_KEY_PATTERN.search(key):
                result[key] = "[redacted internal value]"
                continue

            result[key] = sanitize_for_trace(item, depth=depth + 1)
        return result

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            sanitize_for_trace(item, depth=depth + 1)
            for item in items[:200]
        ]
        if len(items) > 200:
            result.append(f"<{len(items) - 200} more items truncated>")
        return result

    if hasattr(value, "to_dict"):
        try:
            return sanitize_for_trace(value.to_dict(), depth=depth + 1)
        except Exception:
            pass

    return _redact_text(str(value))


sanitize_trace_payload = sanitize_for_trace
safe_trace_payload = sanitize_for_trace
redact_trace_payload = sanitize_for_trace


def safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            sanitize_for_trace(value),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except Exception:
        return json.dumps(
            {"value": _redact_text(str(value))},
            ensure_ascii=False,
            indent=2,
        )


def _safe_file_name(value: str) -> str:
    """Keep Chinese, letters and numbers while removing Windows-invalid chars."""
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or ""))
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip(" ._")
    if not text:
        text = "agent_response"

    # Avoid Windows reserved basenames.
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if text.upper() in reserved:
        text = f"_{text}"
    return text[:120]


def _extract_question_text(payload: Any, *, depth: int = 0) -> str:
    if depth > 5:
        return ""

    if isinstance(payload, dict):
        for key in (
            "query",
            "question",
            "raw_message",
            "user_query",
            "message",
            "prompt",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        for value in payload.values():
            found = _extract_question_text(value, depth=depth + 1)
            if found:
                return found

    elif isinstance(payload, (list, tuple)):
        for value in payload[:20]:
            found = _extract_question_text(value, depth=depth + 1)
            if found:
                return found

    elif isinstance(payload, str):
        return payload.strip()

    return ""


def _question_filename_stem(question: str) -> str:
    """Use the first ten words; continuous Chinese falls back to ten characters."""
    text = re.sub(r"\s+", " ", str(question or "").strip())
    if not text:
        return "agent_response"

    if " " in text:
        words = re.findall(r"[\w\u4e00-\u9fff]+", text, flags=re.UNICODE)[:10]
        stem = "_".join(words)
    elif re.search(r"[\u4e00-\u9fff]", text):
        # No word segmenter dependency is introduced. Ignore punctuation and
        # take the first ten visible Chinese/letter/number characters.
        units = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text)[:10]
        stem = "".join(units)
    else:
        words = re.findall(r"[A-Za-z0-9._-]+", text)[:10]
        stem = "_".join(words)

    return _safe_file_name(stem or "agent_response")


def _output_directory() -> Path:
    configured = str(os.getenv("AGENT_FLOW_MARKDOWN_DIR", "")).strip()
    path = Path(configured) if configured else Path.cwd() / "outputs" / "agent_flow"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _deduplicated_markdown_path(stem: str) -> Path:
    directory = _output_directory()
    candidate = directory / f"{stem}.md"
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = directory / f"{stem}_{index}.md"
        if not candidate.exists():
            return candidate
        index += 1


def _find_identifier(
    payload: Any,
    names: tuple[str, ...],
    *,
    depth: int = 0,
) -> str:
    if depth > 5:
        return ""

    if isinstance(payload, dict):
        for name in names:
            value = payload.get(name)
            if value not in (None, ""):
                return str(value)

        for value in payload.values():
            found = _find_identifier(value, names, depth=depth + 1)
            if found:
                return found

    elif isinstance(payload, (list, tuple)):
        for value in payload[:30]:
            found = _find_identifier(value, names, depth=depth + 1)
            if found:
                return found

    return ""


def _new_fallback_run_id() -> str:
    return (
        "agent_flow_"
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + "_"
        + uuid.uuid4().hex[:8]
    )


def _resolve_run_id(stage: str, payload: Any, explicit_run_id: str = "") -> str:
    stage_name = str(stage or "").strip().upper()
    discovered = (
        str(explicit_run_id or "").strip()
        or _find_identifier(payload, ("run_id", "agent_run_id"))
    )
    current = _CURRENT_RUN_ID.get()

    if stage_name == "REQUEST":
        run_id = discovered or _new_fallback_run_id()
        _CURRENT_RUN_ID.set(run_id)
        return run_id

    if discovered:
        if current and current != discovered:
            _adopt_run_id(current, discovered)
        _CURRENT_RUN_ID.set(discovered)
        return discovered

    if current:
        return current

    run_id = _new_fallback_run_id()
    _CURRENT_RUN_ID.set(run_id)
    return run_id


def _adopt_run_id(old_run_id: str, new_run_id: str) -> None:
    with _LOCK:
        old_path = _RUN_FILES.get(old_run_id)
        new_path = _RUN_FILES.get(new_run_id)
        if old_path is None or not old_path.exists():
            return

        if new_path is None:
            _RUN_FILES[new_run_id] = old_path
            _RUN_SEQUENCE[new_run_id] = _RUN_SEQUENCE.get(old_run_id, 0)
            _RUN_FILES.pop(old_run_id, None)
            _RUN_SEQUENCE.pop(old_run_id, None)
            return

        try:
            old_content = old_path.read_text(encoding="utf-8")
            with new_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n\n# 合并的早期流程\n\n")
                handle.write(old_content)
            old_path.unlink(missing_ok=True)
        except OSError:
            return


def _write_document_header(path: Path, run_id: str, *, question: str = "") -> None:
    created_at = datetime.now().isoformat(timespec="seconds")
    lines = [
        "> Agent 单次响应工作流程",
        ">",
        f"> Run ID：`{_redact_text(run_id)}`",
    ]
    if question:
        lines.extend([">", f"> 问题：`{_redact_text(question)}`"])
    lines.extend([">", f"> 创建时间：`{created_at}`", "", "---", ""])
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _path_for_run(
    run_id: str,
    *,
    start_new: bool,
    payload: Any = None,
) -> Path:
    with _LOCK:
        if start_new or run_id not in _RUN_FILES:
            question = _extract_question_text(payload)
            question_stem = _question_filename_stem(question)
            run_stem = _safe_file_name(run_id or _new_fallback_run_id())
            path = _output_directory() / f"{question_stem}__{run_stem}.md"
            _RUN_FILES[run_id] = path
            _RUN_SEQUENCE[run_id] = 0
            if not path.exists():
                _write_document_header(path, run_id, question=question)
        return _RUN_FILES[run_id]


def _normalise_event_arguments(
    payload: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[Any, str, str, str, dict[str, Any]]:
    event_payload = payload
    remaining = list(args)
    if event_payload is None and remaining:
        event_payload = remaining.pop(0)

    run_id = str(
        kwargs.pop("run_id", "")
        or kwargs.pop("agent_run_id", "")
        or ""
    )
    task_id = str(kwargs.pop("task_id", "") or "")
    level = str(kwargs.pop("level", "INFO") or "INFO")

    metadata = dict(kwargs)
    if remaining:
        metadata["extra_args"] = remaining

    return event_payload, run_id, task_id, level, metadata


def _append_event(
    *,
    stage: str,
    payload: Any,
    run_id: str,
    task_id: str,
    level: str,
    metadata: dict[str, Any],
    trace_kind: str,
) -> str:
    stage_name = str(stage or "UNKNOWN").strip()
    stage_upper = stage_name.upper()
    canonical_run_id = _resolve_run_id(stage_upper, payload, run_id)
    start_new = stage_upper == "REQUEST"

    with _LOCK:
        path = _path_for_run(
            canonical_run_id,
            start_new=start_new,
            payload=payload,
        )
        sequence = _RUN_SEQUENCE.get(canonical_run_id, 0) + 1
        _RUN_SEQUENCE[canonical_run_id] = sequence

        title_label = _STAGE_LABELS.get(stage_upper, stage_name)
        details = [
            f"# {sequence:02d} · {stage_upper} · {title_label}",
            "",
            f"- 时间：`{datetime.now().isoformat(timespec='milliseconds')}`",
            f"- 类型：`{trace_kind}`",
            f"- 级别：`{_redact_text(level)}`",
        ]
        if task_id:
            details.append(f"- Task ID：`{_redact_text(task_id)}`")

        combined_payload: Any = payload
        if metadata:
            combined_payload = {"payload": payload, "metadata": metadata}

        details.extend(
            [
                "",
                "```json",
                safe_json_dumps(combined_payload),
                "```",
                "",
                "---",
                "",
            ]
        )

        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(details))

    return str(path)


def flow_event(
    stage: str,
    payload: Any = None,
    *args: Any,
    **kwargs: Any,
) -> str:
    """Append a major Agent step to this response's Markdown document."""
    if not flow_trace_enabled():
        return ""

    event_payload, run_id, task_id, level, metadata = _normalise_event_arguments(
        payload,
        args,
        kwargs,
    )
    try:
        path = _append_event(
            stage=stage,
            payload=event_payload,
            run_id=run_id,
            task_id=task_id,
            level=level,
            metadata=metadata,
            trace_kind="AGENT-FLOW",
        )
        runtime_debug_event(
            stage,
            event_payload,
            run_id=run_id,
            task_id=task_id,
            level=level,
            metadata=metadata,
        )
        return path
    except Exception:
        return ""


def record_tool_execution(
    *,
    run_id: str,
    task_id: str,
    tool_call_id: str,
    tool_name: str,
    canonical_tool_name: str = "",
    status: str,
    success: bool,
    started_at: str = "",
    finished_at: str = "",
    duration_ms: float = 0.0,
    argument_keys: list[str] | None = None,
    error_type: str = "",
    error_message: str = "",
    failure_kind: str = "",
    retryable: bool = False,
    warning_count: int = 0,
    error_count: int = 0,
    artifact_id: str = "",
    retry_count: int = 0,
    circuit_state: str = "",
) -> str:
    """Persist one terminal tool-call status without raw arguments or responses."""
    canonical_run_id = str(run_id or "").strip()
    if not canonical_run_id:
        return ""
    normalized_status = str(status or ("succeeded" if success else "failed")).lower()
    record = {
        "tool_call_id": str(tool_call_id or ""),
        "task_id": str(task_id or ""),
        "tool_name": str(tool_name or ""),
        "canonical_tool_name": str(canonical_tool_name or tool_name or ""),
        "status": normalized_status,
        "success": bool(success),
        "started_at": str(started_at or ""),
        "finished_at": str(finished_at or ""),
        "duration_ms": float(duration_ms or 0.0),
        "argument_keys": list(argument_keys or []),
        "error_type": str(error_type or ""),
        "error_message": str(error_message or ""),
        "failure_kind": str(failure_kind or ""),
        "retryable": bool(retryable),
        "warning_count": int(warning_count or 0),
        "error_count": int(error_count or 0),
        "artifact_id": str(artifact_id or ""),
        "retry_count": int(retry_count or 0),
        "circuit_state": str(circuit_state or ""),
    }
    with _LOCK:
        _RUN_TOOL_EXECUTIONS.setdefault(canonical_run_id, []).append(record)
    stage = {
        "succeeded": "TOOL_EXECUTION_SUCCEEDED",
        "completed": "TOOL_EXECUTION_SUCCEEDED",
        "blocked": "TOOL_EXECUTION_BLOCKED",
    }.get(normalized_status, "TOOL_EXECUTION_FAILED")
    return flow_event(
        stage,
        record,
        run_id=canonical_run_id,
        task_id=str(task_id or ""),
        level="INFO" if success else "ERROR",
    )


def record_llm_timing(
    *,
    run_id: str,
    task_id: str = "",
    worker_id: str = "",
    agent_id: str = "",
    event_id: str,
    stage: str,
    operation: str,
    provider: str,
    model: str,
    success: bool,
    duration_ms: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    reasoning_tokens: int = 0,
    thinking_disabled: bool = False,
    timing: dict[str, Any] | None = None,
    error_type: str = "",
) -> str:
    """Save one LLM transport breakdown without prompts or responses."""

    canonical_run_id = str(run_id or "").strip()
    if not canonical_run_id:
        return ""
    observed = dict(timing or {})
    record = {
        "event_id": str(event_id or ""),
        "task_id": str(task_id or ""),
        "worker_id": str(worker_id or ""),
        "agent_id": str(agent_id or ""),
        "stage": str(stage or ""),
        "operation": str(operation or ""),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "success": bool(success),
        "duration_ms": float(duration_ms or 0.0),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "cached_prompt_tokens": int(cached_prompt_tokens or 0),
        "reasoning_tokens": int(reasoning_tokens or 0),
        "thinking_disabled": bool(thinking_disabled),
        "thinking_disable_requested": bool(
            observed.get("thinking_disable_requested", thinking_disabled)
        ),
        "thinking_disable_effective": observed.get("thinking_disable_effective"),
        "thinking_mode": (
            "关闭（已生效）"
            if thinking_disabled and observed.get("thinking_disable_effective") is True
            else "关闭请求未生效"
            if thinking_disabled and observed.get("thinking_disable_effective") is False
            else "关闭（待Provider验证）"
            if thinking_disabled
            else "保留"
        ),
        "measurement_mode": str(observed.get("measurement_mode") or "unknown"),
        "client_setup_ms": observed.get("client_setup_ms"),
        "queue_network_ms": observed.get("queue_network_ms"),
        "input_prefill_ms": observed.get("input_prefill_ms"),
        "thinking_output_ms": observed.get("thinking_output_ms"),
        "request_to_first_token_ms": observed.get("request_to_first_token_ms"),
        "provider_transport_total_ms": observed.get("provider_transport_total_ms"),
        "unattributed_provider_ms": observed.get("unattributed_provider_ms"),
        "first_delta_kind": str(observed.get("first_delta_kind") or ""),
        "reasoning_chars": int(observed.get("reasoning_chars") or 0),
        "content_chars": int(observed.get("content_chars") or 0),
        "timing_note": str(observed.get("timing_note") or ""),
        "error_type": str(error_type or ""),
    }
    with _LOCK:
        _RUN_LLM_EXECUTIONS.setdefault(canonical_run_id, []).append(record)
    return ""


def get_llm_execution_timing(run_id: str, task_id: str = "") -> dict[str, Any]:
    """Return cumulative transport timing for a run or one Worker task."""

    rows = [
        dict(item)
        for item in _RUN_LLM_EXECUTIONS.get(str(run_id or ""), [])
        if isinstance(item, dict)
        and (not task_id or str(item.get("task_id") or "") == str(task_id))
    ]
    return {
        "call_count": len(rows),
        "duration_ms_sum": round(sum(float(item.get("duration_ms") or 0.0) for item in rows), 3),
        "provider_transport_ms_sum": round(sum(float(item.get("provider_transport_total_ms") or 0.0) for item in rows), 3),
        "queue_network_ms_sum": round(sum(float(item.get("queue_network_ms") or 0.0) for item in rows), 3),
        "input_prefill_ms_sum": round(sum(float(item.get("input_prefill_ms") or 0.0) for item in rows), 3),
        "thinking_output_ms_sum": round(sum(float(item.get("thinking_output_ms") or 0.0) for item in rows), 3),
        "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in rows),
        "completion_tokens": sum(int(item.get("completion_tokens") or 0) for item in rows),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in rows),
        "items": rows,
    }


def get_tool_execution_timing(run_id: str, task_id: str = "") -> dict[str, Any]:
    """Return cumulative and wall-clock Tool timing for a run or Worker task."""

    rows = [
        dict(item)
        for item in _RUN_TOOL_EXECUTIONS.get(str(run_id or ""), [])
        if isinstance(item, dict)
        and (not task_id or str(item.get("task_id") or "") == str(task_id))
    ]
    duration_sum = sum(float(item.get("duration_ms") or 0.0) for item in rows)
    starts: list[datetime] = []
    finishes: list[datetime] = []
    for item in rows:
        try:
            starts.append(datetime.fromisoformat(str(item.get("started_at") or "")))
            finishes.append(datetime.fromisoformat(str(item.get("finished_at") or "")))
        except (TypeError, ValueError):
            continue
    wall_ms = 0.0
    if starts and finishes:
        wall_ms = max(0.0, (max(finishes) - min(starts)).total_seconds() * 1000.0)
    return {
        "call_count": len(rows),
        "duration_ms_sum": round(duration_sum, 3),
        "wall_duration_ms": round(wall_ms, 3),
        "parallelism_savings_ms": round(max(0.0, duration_sum - wall_ms), 3),
        "items": rows,
    }


def trace_event(
    stage: str,
    payload: Any = None,
    *args: Any,
    **kwargs: Any,
) -> str:
    """Append an optional dense technical event to the same Markdown."""
    if not console_trace_enabled():
        return ""

    event_payload, run_id, task_id, level, metadata = _normalise_event_arguments(
        payload,
        args,
        kwargs,
    )
    try:
        return _append_event(
            stage=f"TRACE · {stage}",
            payload=event_payload,
            run_id=run_id,
            task_id=task_id,
            level=level,
            metadata=metadata,
            trace_kind="AGENT-TRACE",
        )
    except Exception:
        return ""


def trace_exception(
    stage_or_exception: Any = None,
    exception: BaseException | None = None,
    *args: Any,
    **kwargs: Any,
) -> str:
    """Record an exception without exposing traceback or local paths.

    Compatible call forms include::

        trace_exception("ai_agent.run", exc)
        trace_exception(exc, stage="ai_agent.run")
        trace_exception(stage="ai_agent.run", exception=exc)

    Exception records are treated as major flow events, so they are written
    even when ``AGENT_CONSOLE_TRACE=0``.
    """
    stage_kw = str(
        kwargs.pop("stage", "")
        or kwargs.pop("event", "")
        or kwargs.pop("name", "")
        or ""
    )
    exc_kw = kwargs.pop("exc", None) or kwargs.pop("error", None)

    stage = stage_kw
    exc: BaseException | None = None

    if isinstance(stage_or_exception, BaseException):
        exc = stage_or_exception
    elif stage_or_exception not in (None, ""):
        stage = str(stage_or_exception)

    if isinstance(exception, BaseException):
        exc = exception
    elif exception not in (None, ""):
        # Preserve non-exception second arguments as safe context.
        kwargs.setdefault("exception_context", exception)

    if isinstance(exc_kw, BaseException):
        exc = exc_kw
    elif exc_kw not in (None, ""):
        kwargs.setdefault("error_context", exc_kw)

    if not stage:
        stage = type(exc).__name__ if exc is not None else "EXCEPTION"

    frames: list[dict[str, Any]] = []
    if exc is not None and exc.__traceback__ is not None:
        for frame in traceback.extract_tb(exc.__traceback__)[-12:]:
            frames.append({
                "source_file": _safe_source_file(frame.filename),
                "line": int(frame.lineno),
                "function": str(frame.name or ""),
                "code": _redact_text(str(frame.line or ""))[:1000],
            })
    payload: dict[str, Any] = {
        "stage": stage,
        "exception_type": type(exc).__name__ if exc is not None else "Exception",
        "message": _redact_text(str(exc)) if exc is not None else "",
        "frames": frames,
    }
    if args:
        payload["context"] = list(args)
    if kwargs:
        payload["metadata"] = kwargs

    return flow_event(
        f"EXCEPTION · {stage}",
        payload,
        level="ERROR",
    )




def _markdown_inline(value: Any, *, limit: int = 600) -> str:
    raw = "" if value is None else str(value)
    text = _redact_text(raw).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("|", "\\|").replace("`", "'")
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "-"


def _markdown_json(value: Any) -> list[str]:
    return ["```json", safe_json_dumps(value), "```"]


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def finalize_flow_markdown(
    *,
    run_id: str,
    question: str,
    execution: dict[str, Any] | None,
    runtime_status: str,
    success: bool,
    final_answer: str = "",
    user_id: str = "",
    session_id: str = "",
    language: str = "",
    llm_runtime: dict[str, Any] | None = None,
) -> str:
    """Append one complete, human-readable run archive to the flow Markdown.

    Major trace events remain useful while the request is running. This final
    snapshot guarantees that the saved Markdown still contains the Worker DAG,
    every public WorkerResult, execution batches, evidence references, missing
    context, errors and the final answer even when dense console tracing is off.
    It never mutates the Worker DAG and never exposes private Tool arguments.
    """
    if not flow_trace_enabled():
        return ""

    canonical_run_id = str(run_id or "").strip() or _CURRENT_RUN_ID.get()
    if not canonical_run_id:
        canonical_run_id = _new_fallback_run_id()
    payload = dict(execution or {})

    try:
        with _LOCK:
            path = _path_for_run(
                canonical_run_id,
                start_new=False,
                payload={"query": question},
            )
            marker = f"<!-- AGENT_RUN_FINAL_SNAPSHOT:{canonical_run_id} -->"
            if canonical_run_id in _RUN_FINALIZED:
                return str(path)
            try:
                if marker in path.read_text(encoding="utf-8"):
                    _RUN_FINALIZED.add(canonical_run_id)
                    return str(path)
            except OSError:
                pass

            graph_runtime = _as_mapping(payload.get("graph_runtime"))
            worker_dag = _as_mapping(graph_runtime.get("worker_dag"))
            planned_tasks = _as_rows(worker_dag.get("tasks"))
            worker_results = _as_mapping(payload.get("worker_results"))
            result_items = [
                dict(item)
                for item in worker_results.values()
                if isinstance(item, dict)
            ]
            result_by_task = {
                str(item.get("task_id") or ""): item
                for item in result_items
                if str(item.get("task_id") or "")
            }
            batches = _as_rows(payload.get("execution_batches"))
            timeline = _as_rows(payload.get("agent_timeline"))
            planner = _as_mapping(graph_runtime.get("planner"))
            warnings = [str(item) for item in payload.get("warnings") or []]
            errors = [str(item) for item in payload.get("errors") or []]
            missing_context = _as_rows(payload.get("missing_context"))
            failure = _as_mapping(payload.get("failure"))
            execution_status = str(payload.get("execution_status") or runtime_status or "unknown")
            tool_executions = _as_rows(payload.get("tool_executions"))
            if not tool_executions:
                tool_executions = [
                    dict(item)
                    for item in _RUN_TOOL_EXECUTIONS.get(canonical_run_id, [])
                    if isinstance(item, dict)
                ]
            llm_executions = [
                dict(item)
                for item in _RUN_LLM_EXECUTIONS.get(canonical_run_id, [])
                if isinstance(item, dict)
            ]

            lines: list[str] = [
                marker,
                "",
                "# 运行总览",
                "",
                "| 字段 | 内容 |",
                "|---|---|",
                f"| Run ID | `{_markdown_inline(canonical_run_id)}` |",
                f"| 用户问题 | {_markdown_inline(question, limit=1200)} |",
                f"| 用户 | `{_markdown_inline(user_id)}` |",
                f"| 会话 | `{_markdown_inline(session_id)}` |",
                f"| 回复语言 | `{_markdown_inline(language)}` |",
                f"| Runtime 状态 | `{_markdown_inline(runtime_status)}` |",
                f"| 执行状态 | `{_markdown_inline(execution_status)}` |",
                f"| 是否成功 | `{'true' if success else 'false'}` |",
                f"| 端到端总耗时(ms) | `{payload.get('run_total_duration_ms', 0)}` |",
                f"| Worker 计划数 | `{len(planned_tasks)}` |",
                f"| Worker 结果数 | `{len(result_items)}` |",
                f"| 完成数 | `{graph_results.get('completed_count', 0)}` |",
                f"| 失败数 | `{graph_results.get('failed_count', 0)}` |",
                f"| 等待上下文数 | `{graph_results.get('waiting_context_count', 0)}` |",
                f"| 内部运行计数 | `{payload.get('internal_tool_call_count', 0)}` |",
                f"| 已记录 Tool 调用数 | `{len(tool_executions)}` |",
                f"| 已记录 LLM 调用数 | `{len(llm_executions)}` |",
                f"| 完成时间 | `{datetime.now().isoformat(timespec='milliseconds')}` |",
                "",
            ]

            if llm_runtime:
                lines.extend([
                    "## LLM 运行配置（已脱敏）",
                    "",
                    *_markdown_json(llm_runtime),
                    "",
                ])

            lines.extend([
                "## LLM 分阶段耗时",
                "",
                "测量边界：排队/网络为请求提交至流式响应头；输入预填充为响应头至首个 Provider Delta；模型思考与输出为首个 Delta 至流结束。若 Provider 不支持流式边界，将标记为 aggregate_only，不伪造拆分数据。",
                "",
            ])
            if llm_executions:
                lines.extend([
                    "| Stage | Task ID | Operation | 思考模式 | 排队/网络(ms) | 输入预填充(ms) | 思考与输出(ms) | Provider总耗时(ms) | 输入Token | 输出Token | 测量模式 |",
                    "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
                ])
                for item in llm_executions:
                    lines.append(
                        "| `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` |".format(
                            _markdown_inline(item.get("stage")),
                            _markdown_inline(item.get("task_id") or "-"),
                            _markdown_inline(item.get("operation")),
                            _markdown_inline(item.get("thinking_mode")),
                            _markdown_inline(item.get("queue_network_ms")),
                            _markdown_inline(item.get("input_prefill_ms")),
                            _markdown_inline(item.get("thinking_output_ms")),
                            _markdown_inline(item.get("provider_transport_total_ms")),
                            _markdown_inline(item.get("prompt_tokens")),
                            _markdown_inline(item.get("completion_tokens")),
                            _markdown_inline(item.get("measurement_mode")),
                        )
                    )
                lines.extend(["", "### LLM 耗时详情（已脱敏）", "", *_markdown_json(llm_executions), ""])
            else:
                lines.extend(["本次运行没有保存到 LLM 分阶段耗时。", ""])

            # Worker dependency wait is outside Worker execution duration; LLM
            # and Tool timings are components inside Worker execution duration.
            if timeline:
                tool_by_task: dict[str, float] = {}
                llm_by_task: dict[str, float] = {}
                for item in tool_executions:
                    key = str(item.get("task_id") or "")
                    tool_by_task[key] = tool_by_task.get(key, 0.0) + float(item.get("duration_ms") or 0.0)
                for item in llm_executions:
                    key = str(item.get("task_id") or "")
                    llm_by_task[key] = llm_by_task.get(key, 0.0) + float(item.get("provider_transport_total_ms") or item.get("duration_ms") or 0.0)
                lines.extend([
                    "## Worker 依赖等待与执行耗时",
                    "",
                    "| Task ID | Worker | 依赖等待(ms) | Worker执行(ms) | 内含LLM(ms) | 内含Tool累计(ms) | 未归类执行(ms) |",
                    "|---|---|---:|---:|---:|---:|---:|",
                ])
                for item in timeline:
                    task_key = str(item.get("task_id") or "")
                    worker_ms = float(item.get("duration_ms") or 0.0)
                    llm_ms = float(llm_by_task.get(task_key, 0.0))
                    tool_ms = float(tool_by_task.get(task_key, 0.0))
                    unclassified = max(0.0, worker_ms - llm_ms - tool_ms)
                    lines.append(
                        "| `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` |".format(
                            _markdown_inline(task_key),
                            _markdown_inline(item.get("agent_id")),
                            _markdown_inline(item.get("dependency_wait_ms")),
                            _markdown_inline(round(worker_ms, 3)),
                            _markdown_inline(round(llm_ms, 3)),
                            _markdown_inline(round(tool_ms, 3)),
                            _markdown_inline(round(unclassified, 3)),
                        )
                    )
                lines.append("")

            lines.extend([
                "## MainAgent 规划信息",
                "",
                *_markdown_json(planner),
                "",
            ])
            if failure:
                lines.extend([
                    "## 失败阶段与错误分类",
                    "",
                    *_markdown_json(failure),
                    "",
                ])
            lines.extend([
                "## Worker DAG",
                "",
            ])

            if planned_tasks:
                lines.extend([
                    "| 顺序 | Task ID | Worker ID | Worker 角色 | Task Type | 目标 | 依赖 | 预期输出 | 结果状态 |",
                    "|---:|---|---|---|---|---|---|---|---|",
                ])
                for index, task in enumerate(planned_tasks, start=1):
                    task_id = str(task.get("task_id") or "")
                    result = result_by_task.get(task_id, {})
                    dependencies = ", ".join(
                        str(item) for item in task.get("dependency_task_ids") or []
                    ) or "-"
                    lines.append(
                        "| {index} | `{task_id}` | `{worker_id}` | `{agent}` | `{task_type}` | {objective} | {deps} | `{output}` | `{status}` |".format(
                            index=index,
                            task_id=_markdown_inline(task_id),
                            worker_id=_markdown_inline(task.get("worker_id")),
                            agent=_markdown_inline(task.get("assigned_agent")),
                            task_type=_markdown_inline(task.get("task_type")),
                            objective=_markdown_inline(task.get("objective"), limit=900),
                            deps=_markdown_inline(dependencies),
                            output=_markdown_inline(task.get("expected_output_type")),
                            status=_markdown_inline(result.get("status") or task.get("status")),
                        )
                    )
                lines.append("")
                for task in planned_tasks:
                    task_id = str(task.get("task_id") or "")
                    lines.extend([
                        f"### 任务 `{_markdown_inline(task_id)}` 的结构化输入",
                        "",
                        "- Worker：`{}` / `{}`".format(
                            _markdown_inline(task.get("worker_id")),
                            _markdown_inline(task.get("assigned_agent")),
                        ),
                        "- 任务类型：`{}`".format(_markdown_inline(task.get("task_type"))),
                        "- 预期输出：`{}`".format(_markdown_inline(task.get("expected_output_type"))),
                        "- 约束：{}".format(_markdown_inline(", ".join(map(str, task.get("constraints") or [])) or "-")),
                        "",
                        *_markdown_json(task.get("args") or {}),
                        "",
                    ])
            else:
                lines.extend(["未保存到可公开的 Worker DAG 快照。", ""])

            lines.extend(["## 执行批次", ""])
            if batches:
                lines.extend([
                    "| 批次 | Task IDs | Worker 角色 | 是否并行 |",
                    "|---:|---|---|---|",
                ])
                for batch in batches:
                    lines.append(
                        "| `{}` | {} | {} | `{}` |".format(
                            _markdown_inline(batch.get("batch_index")),
                            _markdown_inline(", ".join(map(str, batch.get("task_ids") or []))),
                            _markdown_inline(", ".join(map(str, batch.get("agents") or []))),
                            _markdown_inline(batch.get("parallel")),
                        )
                    )
                lines.append("")
            else:
                lines.extend(["没有可用的执行批次记录。", ""])

            lines.extend(["## 工具执行状态", ""])
            if tool_executions:
                lines.extend([
                    "| Tool Call ID | Task ID | Tool | 状态 | 成功 | 耗时(ms) | 重试 | 错误类型 |",
                    "|---|---|---|---|---|---:|---:|---|",
                ])
                for item in tool_executions:
                    lines.append(
                        "| `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` |".format(
                            _markdown_inline(item.get("tool_call_id")),
                            _markdown_inline(item.get("task_id")),
                            _markdown_inline(item.get("tool_name")),
                            _markdown_inline(item.get("status")),
                            _markdown_inline(item.get("success")),
                            _markdown_inline(item.get("duration_ms")),
                            _markdown_inline(item.get("retry_count")),
                            _markdown_inline(item.get("error_type")),
                        )
                    )
                lines.extend(["", "### Tool 执行详情（已脱敏）", "", *_markdown_json(tool_executions), ""])
            else:
                lines.extend(["本次运行没有记录到 Tool 调用，或调用发生在未携带 run_id 的独立工具上下文中。", ""])

            lines.extend(["## Worker 执行结果", ""])
            if result_items:
                lines.extend([
                    "| Task ID | Worker | 状态 | 输出类型 | 耗时(ms) | 置信度 | 证据 | 产物 | 摘要 |",
                    "|---|---|---|---|---:|---:|---:|---:|---|",
                ])
                for item in result_items:
                    metadata = _as_mapping(item.get("metadata"))
                    lines.append(
                        "| `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | `{}` | {} |".format(
                            _markdown_inline(item.get("task_id")),
                            _markdown_inline(item.get("agent_id")),
                            _markdown_inline(item.get("status")),
                            _markdown_inline(item.get("output_type")),
                            _markdown_inline(metadata.get("duration_ms")),
                            _markdown_inline(item.get("confidence")),
                            len(item.get("evidence_refs") or []),
                            len(item.get("artifact_refs") or []),
                            _markdown_inline(item.get("summary"), limit=700),
                        )
                    )
                lines.append("")

                for item in result_items:
                    task_id = str(item.get("task_id") or "unknown")
                    metadata = _as_mapping(item.get("metadata"))
                    lines.extend([
                        f"### WorkerResult `{_markdown_inline(task_id)}`",
                        "",
                        "- Worker：`{}`".format(_markdown_inline(item.get("agent_id"))),
                        "- 状态：`{}`".format(_markdown_inline(item.get("status"))),
                        "- 输出类型：`{}`".format(_markdown_inline(item.get("output_type"))),
                        "- 耗时：`{} ms`".format(_markdown_inline(metadata.get("duration_ms"))),
                        "- 摘要：{}".format(_markdown_inline(item.get("summary"), limit=1800)),
                        "",
                    ])
                    tool_execution = _as_mapping(metadata.get("tool_execution"))
                    if tool_execution:
                        lines.extend([
                            "#### 私有 Tool 执行摘要",
                            "",
                            *_markdown_json(tool_execution),
                            "",
                        ])
                    if item.get("error"):
                        lines.extend(["#### 错误", "", *_markdown_json(item.get("error")), ""])
                    if item.get("missing_items"):
                        lines.extend(["#### 缺少上下文", "", *_markdown_json(item.get("missing_items")), ""])
                    if item.get("warnings"):
                        lines.extend(["#### 警告", "", *_markdown_json(item.get("warnings")), ""])
                    if item.get("findings"):
                        lines.extend(["#### 关键发现", "", *_markdown_json(item.get("findings")), ""])
                    if item.get("recommendations"):
                        lines.extend(["#### 建议", "", *_markdown_json(item.get("recommendations")), ""])
                    if item.get("evidence_refs"):
                        lines.extend(["#### 证据引用", "", *_markdown_json(item.get("evidence_refs")), ""])
                    if item.get("graph_path_refs"):
                        lines.extend(["#### 图关系路径", "", *_markdown_json(item.get("graph_path_refs")), ""])
                    if item.get("artifact_refs"):
                        lines.extend(["#### 产物引用", "", *_markdown_json(item.get("artifact_refs")), ""])
                    if item.get("data") is not None:
                        lines.extend(["#### 结构化业务输出", "", *_markdown_json(item.get("data")), ""])
            else:
                lines.extend(["没有 WorkerResult。", ""])

            if timeline:
                lines.extend(["## Worker 时间线", "", *_markdown_json(timeline), ""])

            focus_refs = graph_runtime.get("focus_refs") or []
            if focus_refs:
                lines.extend(["## 已解析 GraphRef", "", *_markdown_json(focus_refs), ""])
            resolution_audit = graph_runtime.get("resolution_audit")
            if resolution_audit:
                lines.extend(["## GraphRef 解析审计", "", *_markdown_json(resolution_audit), ""])

            if missing_context:
                lines.extend(["## 等待用户补充的信息", "", *_markdown_json(missing_context), ""])

            lines.extend(["## 最终回答", ""])
            if final_answer:
                lines.extend([_redact_text(str(final_answer)), ""])
            else:
                lines.extend(["未生成最终回答。", ""])

            if warnings:
                lines.extend(["## 全局警告", "", *_markdown_json(warnings), ""])
            if errors:
                lines.extend(["## 全局错误", "", *_markdown_json(errors), ""])

            lines.extend([
                "## 运行边界说明",
                "",
                "- 本文档保存的是 MainAgent 可见的 Worker DAG、公开 WorkerResult 和安全化运行信息。",
                "- Worker 私有 Tool 的原始参数、原始响应、密钥、数据库路径和内部推理不会写入本文档。",
                "- Tool 的逐次执行状态、耗时、错误分类、重试次数和产物引用会保存；原始参数值、原始响应、密钥、数据库路径和内部 traceback 不会写入本文档。",
                "",
                "---",
                "",
            ])

            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines))

            _RUN_FINALIZED.add(canonical_run_id)
            _RUN_TOOL_EXECUTIONS.pop(canonical_run_id, None)
            _RUN_LLM_EXECUTIONS.pop(canonical_run_id, None)
            return str(path)
    except Exception:
        return ""


def get_flow_markdown_path(run_id: str | None = None) -> str:
    target = str(run_id or "").strip() or _CURRENT_RUN_ID.get()
    path = _RUN_FILES.get(target)
    return str(path) if path is not None else ""


def reset_flow_context() -> None:
    _CURRENT_RUN_ID.set("")


console_event = trace_event
agent_trace = trace_event
emit_trace = trace_event
emit_flow = flow_event
trace = trace_event
flow = flow_event
