from __future__ import annotations

import json
from pathlib import Path

from application.web_agent_service import WebAgentApplicationService
from server.api.main import create_app


def test_stage6_4_agent_routes_are_registered() -> None:
    paths = create_app().openapi()["paths"]
    agent_paths = {path: methods for path, methods in paths.items() if path.startswith("/api/v1/web/agent")}
    assert len(agent_paths) >= 9
    assert "post" in agent_paths["/api/v1/web/agent/sessions"]
    assert "patch" in agent_paths["/api/v1/web/agent/sessions/{conversation_id}"]
    assert "delete" in agent_paths["/api/v1/web/agent/sessions/{conversation_id}"]
    assert "post" in agent_paths["/api/v1/web/agent/sessions/{conversation_id}/finalize-task"]
    assert "get" in agent_paths["/api/v1/web/agent/runs/{run_id}"]
    assert "post" in agent_paths["/api/v1/web/agent/pending-actions/{plan_id}/confirm"]


def test_agent_session_message_and_task_finalize_are_user_scoped(tmp_path: Path) -> None:
    service = WebAgentApplicationService(
        output_dir=tmp_path / "outputs",
        db_path=tmp_path / "agent.sqlite3",
    )
    session = service.create_session(user_id="u1", title="", language="zh")
    conversation_id = session["conversation_id"]

    user_message = service.append_message(
        user_id="u1",
        conversation_id=conversation_id,
        role="user",
        content="分析当前组合风险",
        language="zh",
        message_id="msg_user_test",
    )
    assert user_message["role"] == "user"

    task = {
        "task_id": "task_stage64",
        "task_type": "agent.run",
        "owner_id": "u1",
        "session_id": conversation_id,
        "status": "succeeded",
        "result": {
            "success": True,
            "answer": "组合风险已分析。",
            "run_id": "",
            "warnings": [],
        },
    }
    assistant = service.finalize_task(
        user_id="u1",
        conversation_id=conversation_id,
        task=task,
    )
    assert assistant["task_id"] == "task_stage64"
    assert "不构成投资建议" in assistant["content"]

    messages = service.list_messages("u1", conversation_id)["records"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[-1]["task_id"] == "task_stage64"

    try:
        service.list_messages("u2", conversation_id)
    except PermissionError:
        pass
    else:
        raise AssertionError("cross-user conversation read must be rejected")


def test_agent_task_finalize_is_idempotent_and_rejects_mismatched_owner(tmp_path: Path) -> None:
    service = WebAgentApplicationService(
        output_dir=tmp_path / "outputs",
        db_path=tmp_path / "agent.sqlite3",
    )
    session = service.create_session(user_id="u1")
    conversation_id = session["conversation_id"]
    task = {
        "task_id": "task_repeat",
        "task_type": "agent.run",
        "owner_id": "u1",
        "session_id": conversation_id,
        "status": "failed",
        "message": "external configuration missing",
        "error": {"message": "external configuration missing"},
    }
    first = service.finalize_task(user_id="u1", conversation_id=conversation_id, task=task)
    second = service.finalize_task(user_id="u1", conversation_id=conversation_id, task=task)
    assert first["message_id"] == second["message_id"]
    records = service.list_messages("u1", conversation_id)["records"]
    assert len(records) == 1
    assert "Agent 任务未完成" in records[0]["content"]

    task["owner_id"] = "u2"
    try:
        service.finalize_task(user_id="u1", conversation_id=conversation_id, task=task)
    except PermissionError:
        pass
    else:
        raise AssertionError("task owner mismatch must be rejected")


def test_agent_browser_storage_and_contract_are_safe() -> None:
    root = Path(__file__).resolve().parents[2]
    store = (root / "frontend/src/stores/agentTaskStore.ts").read_text(encoding="utf-8")
    partial = store.split("partialize:", 1)[1]
    assert "taskId" in partial and "conversationId" in partial and "lastSequence" in partial
    assert "events: state.events" not in partial
    assert "task: state.task" not in partial
    assert "result" not in partial

    contract = json.loads((root / "contracts/stage6/web-agent-contract.json").read_text(encoding="utf-8"))
    assert contract["execution"]["task_type"] == "agent.run"
    forbidden = " ".join(contract["browser_storage_forbidden"]).lower()
    assert "confirmation token" in forbidden
    assert "task result" in forbidden


def test_agent_frontend_uses_task_api_sse_and_server_finalize() -> None:
    root = Path(__file__).resolve().parents[2]
    page = (root / "frontend/src/pages/agent/AgentPage.tsx").read_text(encoding="utf-8")
    event_hook = (root / "frontend/src/hooks/useAgentTaskEvents.ts").read_text(encoding="utf-8")
    pending = (root / "frontend/src/components/agent/PendingActionPanel.tsx").read_text(encoding="utf-8")
    for marker in ("submitTask", "agent.run", "finalizeTask", "acknowledgeTask", "listTasks"):
        assert marker in page
    assert "connectTaskEvents" in event_hook
    assert "confirmation_phrase" in pending
    assert "confirmation_token" not in pending


def test_agent_frontend_renders_safe_markdown_and_expands_runtime_calls() -> None:
    root = Path(__file__).resolve().parents[2]
    messages = (root / "frontend/src/components/agent/ChatMessageList.tsx").read_text(encoding="utf-8")
    markdown = (root / "frontend/src/components/common/MarkdownContent.tsx").read_text(encoding="utf-8")
    run_panel = (root / "frontend/src/components/agent/AgentRunPanel.tsx").read_text(encoding="utf-8")

    assert "MarkdownContent" in messages
    assert "ReactMarkdown" in markdown
    assert "remarkGfm" in markdown
    assert "rehypeRaw" not in markdown
    assert "dangerouslySetInnerHTML" not in markdown
    assert "defaultActiveKey={['steps', 'tools']}" in run_panel
    assert "expandedRowRender" in run_panel
    assert "Math.random()" not in run_panel


def test_agent_run_detail_combines_tool_execution_and_worker_fallback(tmp_path: Path) -> None:
    service = WebAgentApplicationService(
        output_dir=tmp_path / "outputs",
        db_path=tmp_path / "agent.sqlite3",
    )
    repository = service.agent._repository
    run_id = "agent_run_runtime_calls"
    repository.upsert_agent_run(
        {
            "run_id": run_id,
            "user_id": "u1",
            "goal": "inspect runtime calls",
            "status": "completed",
            "created_at": "2026-07-30T00:00:00+00:00",
            "metadata": {},
        }
    )
    repository.upsert_agent_step(
        {
            "run_id": run_id,
            "step_id": "step_tool",
            "intent": "query_account_state",
            "status": "succeeded",
            "depends_on": [],
            "tool_args_summary": {"user_id": "u1"},
            "observation_summary": "account loaded",
            "duration_seconds": 0.12,
            "metadata": {
                "runtime_layer": "worker_dag",
                "task_type": "query_account_state",
                "tool_execution": {
                    "tool_call_id": "call_worker_tool",
                    "canonical_tool_name": "internal.account.get_state",
                    "status": "succeeded",
                    "success": True,
                    "duration_ms": 80,
                    "retry_count": 0,
                },
            },
        }
    )
    repository.upsert_agent_step(
        {
            "run_id": run_id,
            "step_id": "step_writer",
            "intent": "write_report",
            "status": "succeeded",
            "depends_on": ["step_tool"],
            "observation_summary": "report written",
            "duration_seconds": 0.03,
            "metadata": {
                "runtime_layer": "worker_dag",
                "task_type": "write_report",
                "worker_result_status": "completed",
            },
        }
    )

    detail = service.run_detail("u1", run_id)
    calls = detail["tool_calls"]
    assert detail["counts"]["tool_calls"] == 2
    assert detail["counts"]["persisted_tool_calls"] == 0
    assert detail["counts"]["worker_calls"] == 1
    assert calls[0]["call_kind"] == "tool"
    assert calls[0]["tool_name"] == "internal.account.get_state"
    assert calls[0]["duration_seconds"] == 0.08
    assert calls[1]["call_kind"] == "worker"
    assert calls[1]["tool_name"] == "write_report"


def test_agent_task_api_injects_server_defaults_and_redacts_request(monkeypatch) -> None:
    from server.api import tasks as task_api
    from server.api.serialization import decode_transport

    captured: dict[str, object] = {}

    class Profile:
        context_window = 32768
        supports_json_schema = True
        supports_tools = True

    class Settings:
        profile_id = "profile-test"
        config_hash = "config-test"
        mode = "api"
        provider = "test-provider"
        base_url = "https://private-endpoint.invalid/v1"
        model = "test-model"
        disable_thinking = False
        request_timeout_seconds = 30
        max_retries = 0
        credential = "secret-value"
        endpoint_scope = "api"
        profile = Profile()

    monkeypatch.setattr(task_api.llm_settings_registry, "resolve_current", lambda: Settings())

    def fake_submit(**kwargs):
        captured.update(kwargs)
        return {
            "task_id": "task_public_test",
            "task_type": kwargs["task_type"],
            "status": "queued",
            "owner_id": kwargs["owner_id"],
            "session_id": kwargs["session_id"],
            "request": {"args": kwargs["args"], "kwargs": kwargs["kwargs"]},
            "metadata": kwargs["metadata"],
            "result": None,
            "error": None,
            "progress": 0,
            "message": "",
            "created_at": "",
            "updated_at": "",
            "timeout_seconds": kwargs["timeout_seconds"],
            "max_retries": kwargs["max_retries"],
            "attempt": 0,
            "cancel_requested": False,
        }

    monkeypatch.setattr(task_api.task_manager, "submit", fake_submit)
    response = task_api.submit_task(
        task_api.TaskSubmitRequest(
            task_type="agent.run",
            args=["查看系统状态"],
            kwargs={"user_id": "wrong", "session_id": "wrong"},
            owner_id="u1",
            session_id="conv1",
            metadata={"surface": "react-agent"},
        ),
        user=task_api.AuthUser(user_id="u1", display_name="u1", role="user"),
    )
    assert response.success is True
    public_task = decode_transport(response.data)
    public_kwargs = public_task["request"]["kwargs"]
    assert "output_dir" not in public_kwargs
    assert "base_url" not in str(public_kwargs)
    assert "credential" not in str(public_kwargs)
    assert "confirmation_token" not in str(public_kwargs)

    submitted = captured["kwargs"]
    assert submitted["user_id"] == "u1"
    assert submitted["session_id"] == "conv1"
    assert submitted["output_dir"]
    assert submitted["llm_settings_descriptor"]["base_url"]
    assert captured["secrets"]["llm_credential"] == "secret-value"
