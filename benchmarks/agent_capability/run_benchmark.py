"""Execute L1 through the ordinary Agent entry point, never through a mock."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.console_trace import sanitize_for_trace
from agent.executor import run_agent_request
from agent.llm_audit import load_llm_events
from agent.runtime import load_run_snapshot
from core.llm import LLMRuntimeSettings
from evaluation.agent_harness.runner import _write_basic_fixture
from portfolio.storage import PortfolioStorage
from storage_governance.lifecycle import cleanup_checkpointed_benchmark_workspace, prune_stale_benchmark_workspaces

from .case_dataset import DATASET_VERSION, build_cases, build_hidden_gold, ensure_case_files
from .config import BENCHMARK_ROOT, BenchmarkRuntimeConfig, ensure_roots, load_llm_settings
from .scoring import (
    aggregate_metrics,
    assess_trace_validity,
    evaluate_gates,
    failure_record,
    infrastructure_metrics,
    metric_records,
    normalize_trace,
    score_trace,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return str(value)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(sanitize_for_trace(row), ensure_ascii=False, sort_keys=True, default=_json_default) + "\n" for row in rows),
        encoding="utf-8",
    )


def _portfolio_digest(db_path: Path, output_dir: Path, user_id: str) -> str:
    """Hash only the synthetic paper account/positions, excluding Agent audit tables."""
    storage = PortfolioStorage(db_path, output_dir=output_dir / "portfolio" / user_id)
    account = storage.load_account(f"paper_{user_id}")
    positions = storage.load_positions(user_id)
    def normalize(value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    payload = json.dumps(normalize({"account": account, "positions": positions}), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _case_gold(case: dict[str, Any]) -> dict[str, Any]:
    if case.get("split") == "hidden":
        return dict(build_hidden_gold().get(str(case.get("case_id"))) or {})
    return dict(case.get("gold") or {})


def _run_one(case: dict[str, Any], iteration: int, config: BenchmarkRuntimeConfig, settings: LLMRuntimeSettings) -> dict[str, Any]:
    """One isolated fixture, user and conversation per case iteration."""
    case_id = str(case["case_id"])
    workspace = BENCHMARK_ROOT / "isolated_workspaces" / case_id / f"iter_{iteration}_{config.config_hash}_{uuid4().hex[:8]}"
    output_dir = workspace / "outputs"
    db_path = workspace / "agent_capability.db"
    user_id = f"benchmark_user_{case_id.lower().replace('-', '_')}_{iteration}_{uuid4().hex[:6]}"
    session_id = f"benchmark_session_{case_id.lower().replace('-', '_')}_{iteration}_{uuid4().hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=False)
    _write_basic_fixture(output_dir=output_dir, db_path=db_path, user_id=user_id, setup=dict(case.get("fixture") or {}))
    before = _portfolio_digest(db_path, output_dir, user_id)
    started = time.perf_counter()
    turns: list[dict[str, Any]] = []
    exception = ""
    try:
        for index, query in enumerate(case.get("turns") or [], start=1):
            result = run_agent_request(
                str(query),
                user_id=user_id,
                output_dir=output_dir,
                db_path=db_path,
                session_id=session_id,
                reply_language="zh",
                llm_settings=settings,
                decomposition_context={
                    "benchmark_mode": "L1 isolated real-LLM capability evaluation",
                    "benchmark_case_id": case_id,
                    "benchmark_iteration": iteration,
                    "turn_index": index,
                },
            )
            run_id = str(result.get("run_id") or "")
            snapshot = load_run_snapshot(db_path, run_id) if run_id else {}
            llm_events = load_llm_events(output_dir, run_id) if run_id else []
            turns.append({"turn": index, "query": str(query), "result": result, "snapshot": snapshot, "llm_events": llm_events})
    except Exception as exc:  # Preserve failure as benchmark evidence, never silently retry a new fixture.
        exception = f"{type(exc).__name__}: {exc}"
    duration = time.perf_counter() - started
    after = _portfolio_digest(db_path, output_dir, user_id)
    trace = normalize_trace(case, turns, duration_seconds=duration, state_changed=before != after)
    if exception:
        trace["errors"] = sorted(set([*list(trace.get("errors") or []), exception[:800]]))
        trace["runtime_status"] = trace.get("runtime_status") or "benchmark_exception"
    trace["case_timeout_exceeded"] = duration > config.case_timeout_seconds
    gold = _case_gold(case)
    validity = assess_trace_validity(trace)
    if validity["valid_for_agent_scoring"]:
        score = score_trace(case, gold, trace)
    else:
        score = {
            "success": None,
            "real_llm": None,
            "chain_complete": None,
            "status": "N/A",
            "reason": ",".join(validity.get("failure_reasons") or ["invalid_for_agent_scoring"]),
            "latency_seconds": float(trace.get("duration_seconds") or 0.0),
        }
    if exception or trace["case_timeout_exceeded"]:
        score["success"] = False
    if trace["case_timeout_exceeded"] and validity["valid_for_agent_scoring"]:
        validity.update(
            {
                "valid_for_agent_scoring": False,
                "infrastructure_failure": True,
                "failure_classification": "infrastructure_failure",
                "failure_reasons": [*list(validity.get("failure_reasons") or []), "case_timeout"],
            }
        )
    elif exception and validity["valid_for_agent_scoring"]:
        validity.update(
            {
                "valid_for_agent_scoring": False,
                "infrastructure_failure": True,
                "failure_classification": "infrastructure_failure",
                "failure_reasons": [*list(validity.get("failure_reasons") or []), "runner_exception"],
            }
        )
    elif validity["valid_for_agent_scoring"] and score.get("success") is False:
        validity["failure_classification"] = "agent_capability_failure"
    public_case = {key: value for key, value in case.items() if key not in {"gold"}}
    row = {
        "run_key": f"{case_id}:{iteration}:{config.config_hash}",
        "case_id": case_id,
        "iteration": iteration,
        "category": case["category"],
        "split": case["split"],
        "dataset_version": DATASET_VERSION,
        "model_config_hash": config.config_hash,
        "model_config": config.public_dict(),
        "started_at": _utc_now(),
        "input": {"turns": list(case.get("turns") or []), "fixture": dict(case.get("fixture") or {})},
        "case": public_case,
        "trace": trace,
        "validity": validity,
        "failure_classification": validity.get("failure_classification"),
        "score": score,
        "exception": exception[:800],
        "isolated": {
            "new_user": True,
            "new_conversation": True,
            "new_sqlite": True,
            "production_data_used": False,
            "workspace_rel": workspace.relative_to(BENCHMARK_ROOT).as_posix(),
        },
    }
    # Keep hidden gold out of raw/normalized traces.  It is used only by the scorer/diagnostic.
    if case.get("split") != "hidden":
        row["gold"] = gold
    return sanitize_for_trace(row)


def _rows_for_cases(existing: list[dict[str, Any]], cases: list[dict[str, Any]], iterations: int, config_hash: str) -> list[tuple[dict[str, Any], int]]:
    # A provider/trace failure is evidence, not a completed capability sample.
    # It must be retried after a repair or configuration change.
    completed = {
        str(row.get("run_key"))
        for row in existing
        if bool((row.get("validity") or {}).get("valid_for_agent_scoring"))
    }
    wanted = []
    for case in cases:
        for iteration in range(1, iterations + 1):
            key = f"{case['case_id']}:{iteration}:{config_hash}"
            if key not in completed:
                wanted.append((case, iteration))
    return wanted


def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
    keys = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _category_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    categories = sorted({str(row.get("category")) for row in rows})
    return {category: aggregate_metrics([row for row in rows if row.get("category") == category]) for category in categories}


def _markdown_report(*, rows: list[dict[str, Any]], metrics: dict[str, Any], category_metrics: dict[str, dict[str, Any]], gates: dict[str, Any], config: BenchmarkRuntimeConfig, official_scope: str) -> str:
    lines = [
        "# 真实 LLM Agent 能力测评报告（L1）", "",
        f"- 数据集版本：`{DATASET_VERSION}`", f"- 模型配置哈希：`{config.config_hash}`（模型：`{config.model}`；温度：{config.temperature}）",
        f"- 统计范围：{official_scope}；样本数：{metrics.get('sample_count')}；案例数：{metrics.get('case_count')}",
        "- 真实模型要求：每个计分样本通过正式 `run_agent_request` 入口；未记录规划器与复核器真实调用的样本计为失败。",
        "- 隔离：每个 case × iteration 均为新的 SQLite、用户、会话与 synthetic paper fixture；不会读取生产数据库或提交真实/模拟盘订单。", "",
        "## 核心结果", "",
        "| 指标 | 值 |", "|---|---:|",
    ]
    for name in (
        "real_llm_run_rate", "task_success_rate", "pass_at_1", "pass_at_3", "pass_at_5",
        "intent_action_accuracy", "intent_macro_f1", "intent_object_f1", "constraint_precision", "constraint_recall", "constraint_f1", "clarification_decision_accuracy", "write_intent_accuracy",
        "planning_task_recall", "planning_task_precision", "planning_dependency_validity", "planning_output_validity", "forbidden_capability_rate",
        "tool_precision", "tool_recall", "tool_f1", "tool_argument_exactness", "tool_argument_field_accuracy", "invalid_tool_rate", "duplicate_tool_rate", "excessive_tool_rate", "normalized_step_efficiency", "tool_call_count",
        "replan_trigger_precision", "replan_trigger_recall", "replan_success_rate", "replan_no_progress_rate", "replan_duplicate_rate", "replan_limit_violation_rate", "average_replan_count",
        "context_carryover_accuracy", "context_reference_resolution", "context_parameter_override_accuracy", "pending_action_handling_accuracy", "cross_conversation_isolation", "context_state_consistency",
        "failure_detector_accuracy", "failure_recovery_rate", "unsupported_disclosure_rate", "terminal_state_correctness",
        "final_state_consistency", "false_success_rate", "failure_disclosure_rate", "no_write_disclosure_rate", "unauthorized_write_rate", "approval_bypass_rate", "cross_user_access_rate", "expired_confirmation_accepted_rate", "duplicate_commit_rate", "terminal_write_rate",
    ):
        value = metrics.get(name)
        display = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        lines.append(f"| {name} | {display if display is not None else 'N/A'} |")
    lines.extend(["", "## 类别结果", "", "| 类别 | Task Success | Tool F1 | Final state |", "|---|---:|---:|---:|"])
    for category, value in category_metrics.items():
        lines.append(f"| {category} | {value.get('task_success_rate')} | {value.get('tool_f1')} | {value.get('final_state_consistency')} |")
    lines.extend(["", "## 质量门禁", "", f"- 结论：{'通过' if gates.get('passed') else '未通过，已生成自动诊断报告。'}"])
    for failure in gates.get("failures") or []:
        lines.append(f"- `{failure.get('gate')}`：实际 `{failure.get('actual')}`，阈值 `{failure.get('threshold')}`。")
    latency = metrics.get("latency_seconds") or {}
    lines.extend(["", "## 运行与限制", "", f"- 延迟：平均 {latency.get('average')}s，P50 {latency.get('p50')}s，P95 {latency.get('p95')}s。", f"- 重试上限：{config.request_retries}；单案例超时：{config.case_timeout_seconds}s；Replan 上限：{config.max_replans}；工具上限：{config.max_tool_calls}。", "- 成本：兼容 API 没有返回可核验 token/cost，因此报告为 `N/A`，不估算或伪造。", "", "## 可恢复继续", "", "1. 直接运行 `python -m benchmarks.agent_capability.resume --split hidden --iterations 5` 会按 case + iteration + model config hash 跳过已记录运行。", "2. `raw_runs.jsonl` 和 `failures.jsonl` 保留失败轨迹；修复后可用相同命令续跑，不会清除证据。", "3. 只有隐藏集的真实 LLM 结果会写为最终 `metrics.json` 与本报告的正式统计范围。", "", "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。", ""])
    return "\n".join(lines)


def _diagnostic_markdown(failures: list[dict[str, Any]], gates: dict[str, Any], config: BenchmarkRuntimeConfig) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in failures:
        grouped.setdefault(str(item.get("failure_category")), []).append(item)
    lines = ["# L1 Agent 能力测评低分自动诊断", "", f"模型配置哈希：`{config.config_hash}`。本报告仅基于保留的真实 LLM 失败轨迹；没有足够直接证据时只标注阶段，不宣称代码根因。", "", "## 门禁失败", ""]
    for item in gates.get("failures") or []:
        lines.append(f"- `{item.get('gate')}`：actual={item.get('actual')} threshold={item.get('threshold')}。")
    for stage, items in sorted(grouped.items()):
        lines.extend(["", f"## {stage}（{len(items)}）", ""])
        for item in items[:10]:
            lines.append(f"- `{item.get('case_id')}` / iteration {item.get('iteration')}：{'; '.join(item.get('evidence') or [])}")
            if item.get("code_path_hypothesis"):
                lines.append(f"  - 证据关联代码路径：`{item.get('code_path_hypothesis')}`。")
            lines.append(f"  - 最小复现：`{item.get('minimal_reproduction')}`")
            lines.append(f"  - 建议：{item.get('regression_suggestion')}")
    lines.extend(["", "重复模式（同阶段 ≥3）应优先按上述最小复现建立回归；若只有阶段证据，先补充可观测性再改 Agent 行为。", "", "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。", ""])
    return "\n".join(lines)


def publish(rows: list[dict[str, Any]], config: BenchmarkRuntimeConfig) -> dict[str, Any]:
    return _publish_repaired(rows, config)


def _legacy_publish(rows: list[dict[str, Any]], config: BenchmarkRuntimeConfig) -> dict[str, Any]:
    ensure_roots(BENCHMARK_ROOT)
    # Scoring changes must be reproducible without making a second paid model
    # call.  Raw real-LLM traces remain intact; only their derived score is
    # regenerated from the versioned case contract.
    case_index = {case["case_id"]: case for case in build_cases()}
    for row in rows:
        source_case = case_index.get(str(row.get("case_id")))
        if source_case and isinstance(row.get("trace"), dict):
            row["dataset_version"] = DATASET_VERSION
            row["case"] = {key: value for key, value in source_case.items() if key != "gold"}
            row["score"] = score_trace(source_case, _case_gold(source_case), row["trace"])
            if row.get("exception") or bool((row.get("trace") or {}).get("case_timeout_exceeded")):
                row["score"]["success"] = False
    _write_jsonl(BENCHMARK_ROOT / "raw_runs.jsonl", rows)
    normalized = [{key: row.get(key) for key in ("run_key", "case_id", "iteration", "category", "split", "model_config_hash", "trace", "score", "exception")} for row in rows]
    _write_jsonl(BENCHMARK_ROOT / "normalized_traces.jsonl", normalized)
    hidden_attempts = [
        row for row in rows
        if row.get("split") == "hidden" and row.get("model_config_hash") == config.config_hash
    ]
    hidden = [row for row in hidden_attempts if (row.get("score") or {}).get("real_llm")]
    if hidden_attempts:
        # The formal score is *only* a hidden set score.  In particular, an
        # exhausted provider must not silently turn development/validation
        # results into a misleading official metric.
        scoped = hidden
        scope = (
            "hidden real-LLM test set"
            if hidden
            else "hidden test set: zero real-LLM samples (provider/blocker; official metrics unavailable)"
        )
    else:
        scoped = [row for row in rows if row.get("model_config_hash") == config.config_hash]
        scope = "current non-hidden run (provisional; hidden set not yet available)"
    metrics = aggregate_metrics(scoped)
    category_metrics = _category_metrics(scoped)
    gates = evaluate_gates(metrics, category_metrics)
    failures = []
    diagnostic_rows = hidden_attempts if hidden_attempts else scoped
    for row in diagnostic_rows:
        record = failure_record({**row, "gold": _case_gold(dict(row.get("case") or {}))})
        if record:
            failures.append(record)
    (BENCHMARK_ROOT / "metrics.json").write_text(json.dumps({"dataset_version": DATASET_VERSION, "model_config": config.public_dict(), "model_config_hash": config.config_hash, "official_scope": scope, "metrics": metrics, "gates": gates}, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _csv(BENCHMARK_ROOT / "metrics.csv", [{key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in metrics.items()}])
    _csv(BENCHMARK_ROOT / "category_metrics.csv", [{"category": category, **{key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in values.items()}} for category, values in category_metrics.items()])
    _write_jsonl(BENCHMARK_ROOT / "failures.jsonl", failures)
    (BENCHMARK_ROOT / "benchmark_report.md").write_text(_markdown_report(rows=scoped, metrics=metrics, category_metrics=category_metrics, gates=gates, config=config, official_scope=scope), encoding="utf-8")
    diagnostics = Path(__file__).resolve().parents[2] / "docs" / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    (diagnostics / "AGENT_CAPABILITY_BENCHMARK_REPORT.md").write_text(_markdown_report(rows=scoped, metrics=metrics, category_metrics=category_metrics, gates=gates, config=config, official_scope=scope), encoding="utf-8")
    diagnostic_path = diagnostics / "AGENT_CAPABILITY_BENCHMARK_DIAGNOSTIC_REPORT.md"
    if not gates["passed"]:
        diagnostic_path.write_text(_diagnostic_markdown(failures, gates, config), encoding="utf-8")
    return {"metrics": metrics, "gates": gates, "failures": len(failures), "official_scope": scope}


def _repair_report_markdown(
    *,
    scope: str,
    config: BenchmarkRuntimeConfig,
    infrastructure: dict[str, dict[str, Any]],
    metrics: dict[str, Any],
) -> str:
    def value(name: str) -> Any:
        return infrastructure.get(name, {}).get("value")

    def display(item: Any) -> str:
        return "N/A" if item is None else str(item)

    lines = [
        "# Real LLM Agent Capability Benchmark Pipeline Repair Report",
        "",
        f"- Scope: {scope}",
        f"- Model configuration hash: `{config.config_hash}`",
        f"- Deployment: `{config.deployment_mode}` / provider `{config.provider}` / model `{config.model}` / endpoint `{config.endpoint_scope}`",
        f"- Trace schema: `{config.trace_schema_version}`; scorer: `{config.scorer_version}`",
        "",
        "## Evidence quality",
        "",
        "| Metric | Value | Numerator | Denominator |",
        "|---|---:|---:|---:|",
    ]
    for name, record in infrastructure.items():
        lines.append(f"| {name} | {display(record.get('value'))} | {record.get('numerator')} | {record.get('denominator')} |")
    lines.extend(["", "## Agent metrics", "", "| Metric | Value |", "|---|---:|"])
    for name in ("task_success_rate", "intent_macro_f1", "tool_f1", "final_state_consistency", "forbidden_capability_rate"):
        lines.append(f"| {name} | {display(metrics.get(name))} |")
    lines.extend([
        "",
        "Invalid provider and infrastructure samples are retained as evidence and are excluded from Agent capability metrics.",
        "",
        "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。",
        "",
    ])
    return "\n".join(lines)


def _infrastructure_diagnostic_markdown(rows: list[dict[str, Any]], config: BenchmarkRuntimeConfig) -> str:
    categories = {
        "provider_failure": 0,
        "trace_incomplete": 0,
        "formal_entry_missing": 0,
        "planner_event_missing": 0,
        "reviewer_event_missing": 0,
        "fixture_failure": 0,
        "scorer_failure": 0,
        "serialization_failure": 0,
        "unknown": 0,
    }
    examples: list[str] = []
    for row in rows:
        validity = dict(row.get("validity") or {})
        if validity.get("valid_for_agent_scoring"):
            continue
        reasons = list(validity.get("failure_reasons") or [])
        if validity.get("provider_failure"):
            categories["provider_failure"] += 1
        elif not reasons:
            categories["unknown"] += 1
        else:
            matched = False
            for reason in reasons:
                name = str(reason)
                if name in categories:
                    categories[name] += 1
                    matched = True
            if not matched:
                categories["unknown"] += 1
        if len(examples) < 12:
            examples.append(
                f"- `{row.get('case_id')}` / iteration {row.get('iteration')}: "
                f"{validity.get('failure_classification')} — {', '.join(reasons) or 'unknown'}"
            )
    lines = [
        "# Agent Capability Benchmark Infrastructure Diagnostic",
        "",
        f"- Model configuration hash: `{config.config_hash}`",
        "",
        "| Category | Count |",
        "|---|---:|",
        *[f"| {name} | {count} |" for name, count in categories.items()],
        "",
        "## Preserved examples",
        "",
        *(examples or ["- No invalid samples in this configuration."]),
        "",
        "These failures are not scored as Agent capability failures.",
        "",
        "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。",
        "",
    ]
    return "\n".join(lines)


def _publish_repaired(rows: list[dict[str, Any]], config: BenchmarkRuntimeConfig) -> dict[str, Any]:
    """Publish only valid, auditable samples as Agent metrics.

    Prior raw traces and failure evidence are retained.  The formal report is
    intentionally left untouched when the hidden set has no valid evidence.
    """
    ensure_roots(BENCHMARK_ROOT)
    # ``run_benchmark`` checkpoints raw evidence before it calls this
    # publisher.  Do not rewrite historical JSONL here: publishing metrics
    # must never mutate or discard previous provider/trace evidence.
    normalized = [
        {key: row.get(key) for key in (
            "run_key", "case_id", "iteration", "category", "split",
            "model_config_hash", "trace", "validity", "score", "exception",
        )}
        for row in rows
    ]
    _write_jsonl(BENCHMARK_ROOT / "normalized_traces.jsonl", normalized)
    current = [row for row in rows if row.get("model_config_hash") == config.config_hash]
    hidden_attempts = [row for row in current if row.get("split") == "hidden"]
    hidden_valid = [row for row in hidden_attempts if bool((row.get("validity") or {}).get("valid_for_agent_scoring"))]
    if hidden_attempts:
        scoped = hidden_valid
        scope = "hidden real-LLM test set" if hidden_valid else "hidden test set: no valid auditable samples; Agent metrics N/A"
    else:
        scoped = [row for row in current if bool((row.get("validity") or {}).get("valid_for_agent_scoring"))]
        scope = "current non-hidden run (provisional)" if scoped else "current non-hidden run: no valid auditable samples; Agent metrics N/A"
    metrics = aggregate_metrics(scoped)
    records = metric_records(metrics, scored_sample_count=len(scoped))
    infrastructure = infrastructure_metrics(hidden_attempts if hidden_attempts else current)
    category_metrics = _category_metrics(scoped)
    gates = evaluate_gates(metrics, category_metrics)

    failures = [record for row in current if (record := failure_record(row))]
    failure_path = BENCHMARK_ROOT / "failures.jsonl"
    previous_failures = _read_jsonl(failure_path)
    previous_keys = {
        (str(item.get("case_id")), str(item.get("iteration")), str(item.get("failure_classification")), str(item.get("failure_category")))
        for item in previous_failures
    }
    additions = [
        item for item in failures
        if (str(item.get("case_id")), str(item.get("iteration")), str(item.get("failure_classification")), str(item.get("failure_category"))) not in previous_keys
    ]
    _write_jsonl(failure_path, [*previous_failures, *additions])

    payload = {
        "dataset_version": DATASET_VERSION,
        "model_config": config.public_dict(),
        "model_config_hash": config.config_hash,
        "official_scope": scope,
        # Compatibility projection for existing readers; it is identical to
        # the explicitly named Agent-only metric set below.
        "metrics": metrics,
        "agent_metrics": metrics,
        "metric_records": records,
        "infrastructure_metrics": infrastructure,
        "gates": gates,
    }
    serialized_payload = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    pipeline_metrics_path = BENCHMARK_ROOT / "metrics_pipeline_repair.json"
    pipeline_metrics_path.write_text(serialized_payload, encoding="utf-8")
    metrics_rows = [
        {"metric": name, **record}
        for name, record in records.items()
    ]
    _csv(BENCHMARK_ROOT / "metrics_pipeline_repair.csv", metrics_rows)
    # Preserve the old official aggregate before correcting its zero-sample
    # representation.  Reports remain untouched until hidden scoring has a
    # valid sample, but the canonical metric payload must never report a
    # numeric Agent/safety metric for zero valid samples.
    canonical_metrics_path = BENCHMARK_ROOT / "metrics.json"
    archive_dir = BENCHMARK_ROOT / "historical_metrics"
    if canonical_metrics_path.exists():
        try:
            previous = json.loads(canonical_metrics_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        previous_hash = str(previous.get("model_config_hash") or "pre_repair")
        archive_path = archive_dir / f"metrics_{previous_hash}.json"
        if previous_hash != config.config_hash and not archive_path.exists():
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(canonical_metrics_path, archive_path)
    canonical_metrics_path.write_text(serialized_payload, encoding="utf-8")
    _csv(BENCHMARK_ROOT / "metrics.csv", metrics_rows)
    diagnostics = Path(__file__).resolve().parents[2] / "docs" / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    report = _repair_report_markdown(scope=scope, config=config, infrastructure=infrastructure, metrics=metrics)
    diagnostic = _infrastructure_diagnostic_markdown(current, config)
    (diagnostics / "AGENT_CAPABILITY_BENCHMARK_INFRASTRUCTURE_DIAGNOSTIC.md").write_text(diagnostic, encoding="utf-8")
    if not scoped and not (diagnostics / "AGENT_CAPABILITY_BENCHMARK_PIPELINE_REPAIR_DIAGNOSTIC.md").exists():
        (diagnostics / "AGENT_CAPABILITY_BENCHMARK_PIPELINE_REPAIR_DIAGNOSTIC.md").write_text(report, encoding="utf-8")
    if hidden_valid:
        (diagnostics / "AGENT_CAPABILITY_BENCHMARK_PIPELINE_REPAIR_REPORT.md").write_text(report, encoding="utf-8")
    return {
        "metrics": metrics,
        "metric_records": records,
        "infrastructure_metrics": infrastructure,
        "gates": gates,
        "failures": len(failures),
        "official_scope": scope,
        "valid_samples": len(scoped),
    }


def run_benchmark(*, split: str, iterations: int, workers: int, case_id: str = "") -> dict[str, Any]:
    ensure_case_files()
    ensure_roots(BENCHMARK_ROOT)
    # Owner-side cleanup: prior interrupted workspaces get a forensic retention window.
    # Canonical raw/normalized/failure evidence lives outside isolated_workspaces.
    stale_cleanup = prune_stale_benchmark_workspaces(BENCHMARK_ROOT)
    settings, config = load_llm_settings()
    workers = 1 if config.deployment_mode == "local" else 2
    if not settings.is_configured:
        raise RuntimeError("Real LLM configuration is incomplete. Configure the local app LLM fields before running L1.")
    cases = [case for case in build_cases() if (split == "all" or case["split"] == split) and (not case_id or case["case_id"] == case_id)]
    if not cases:
        raise ValueError(f"No cases for split={split!r}, case_id={case_id!r}")
    raw_path = BENCHMARK_ROOT / "raw_runs.jsonl"
    existing = _read_jsonl(raw_path)
    pending = _rows_for_cases(existing, cases, iterations, config.config_hash)
    results: list[dict[str, Any]] = []
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_run_one, case, iteration, config, settings): (case, iteration) for case, iteration in pending}
            for future in as_completed(futures):
                case, iteration = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"run_key": f"{case['case_id']}:{iteration}:{config.config_hash}", "case_id": case["case_id"], "iteration": iteration, "category": case["category"], "split": case["split"], "dataset_version": DATASET_VERSION, "model_config_hash": config.config_hash, "model_config": config.public_dict(), "input": {"turns": case.get("turns") or []}, "case": {key: value for key, value in case.items() if key != "gold"}, "trace": {"real_llm": False, "runtime_status": "benchmark_exception", "errors": [f"runner_exception:{type(exc).__name__}"], "stages": {}}, "score": {"success": False, "real_llm": False, "chain_complete": False, "latency_seconds": 0.0}, "exception": f"{type(exc).__name__}: {exc}"[:800], "isolated": {"new_user": True, "new_conversation": True, "new_sqlite": True, "production_data_used": False}})
                # A process interruption must not discard completed paid model
                # calls.  The final aggregate/report is written after the
                # batch, while this checkpoint makes resume immediately safe.
                checkpoint_keys = {str(item.get("run_key")) for item in results}
                _write_jsonl(raw_path, [row for row in existing if str(row.get("run_key")) not in checkpoint_keys] + results)
                # Canonical evidence is durable now, so the per-case SQLite/output workspace
                # can be removed immediately. A crash before this point leaves it for 14-day cleanup.
                workspace_rel = str(((results[-1].get("isolated") or {}).get("workspace_rel") or ""))
                cleaned = cleanup_checkpointed_benchmark_workspace(workspace_rel, benchmark_root=BENCHMARK_ROOT) if workspace_rel else False
                print(
                    f"checkpoint {len(results)}/{len(pending)}: {case['case_id']} iteration {iteration}; workspace_cleaned={cleaned}",
                    flush=True,
                )
    merged = [row for row in existing if str(row.get("run_key")) not in {str(item.get("run_key")) for item in results}] + results
    summary = publish(merged, config)
    summary.update({
        "executed": len(results),
        "resumed": len(pending) == 0,
        "pending_before_run": len(pending),
        "model_config_hash": config.config_hash,
        "stale_workspaces_removed": int(stale_cleanup.get("removed_count") or 0),
    })
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated real-LLM L1 Agent capability benchmark")
    parser.add_argument("--split", choices=("development", "validation", "hidden", "all"), default="development")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--case-id", default="")
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    try:
        result = run_benchmark(split=args.split, iterations=args.iterations, workers=args.workers, case_id=args.case_id)
    except Exception as exc:
        print(f"L1 benchmark failed to start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
