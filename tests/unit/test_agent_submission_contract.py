from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import user_scope
from server.api import tasks as task_api
from server.api.security import AuthUser, require_user


@pytest.fixture
def agent_request():
    # Also compared with the actual Axios request by the frontend regression test.
    # Keep the shared contract inside the standalone frontend Docker context.
    fixture = Path(__file__).resolve().parents[2] / "frontend/src/tests/fixtures/agent_task_request.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


@pytest.fixture
def task_client(monkeypatch, tmp_path):
    submit = Mock(return_value={"task_id": "task-offline", "owner_id": "alice", "status": "queued"})
    settings = Mock(return_value=None)
    monkeypatch.setattr(task_api, "task_manager", SimpleNamespace(submit=submit))
    monkeypatch.setattr(task_api, "get_user_llm_settings", settings)
    monkeypatch.setattr(user_scope, "workspace", lambda owner: tmp_path / owner)
    app = FastAPI()
    app.include_router(task_api.router)
    app.dependency_overrides[require_user] = lambda: AuthUser("alice", "Alice", "user")
    with TestClient(app) as client:
        yield client, submit, settings


def test_shared_agent_request_is_accepted_with_keyword_query(agent_request, task_client, tmp_path):
    client, submit, settings = task_client

    response = client.post("/api/v1/tasks", json=agent_request)

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["task_id"] == "task-offline"
    submit.assert_called_once()
    submitted = submit.call_args.kwargs
    assert submitted["task_type"] == "agent.run"
    assert submitted["args"] == []
    assert submitted["kwargs"] == {
        "query": agent_request["kwargs"]["query"],
        "user_id": "alice",
        "top_k": 10,
        "session_id": "conversation-offline",
        "output_dir": str(tmp_path / "alice" / "outputs"),
    }
    assert submitted["owner_id"] == "alice"
    assert submitted["session_id"] == "conversation-offline"
    assert submitted["metadata"] == agent_request["metadata"]
    settings.assert_called_once_with("alice")


def test_legacy_agent_positional_query_is_rejected_before_submission(agent_request, task_client):
    client, submit, settings = task_client
    agent_request["args"] = [agent_request["kwargs"].pop("query")]

    response = client.post("/api/v1/tasks", json=agent_request)

    assert response.status_code == 422
    assert response.json()["detail"] == "task_positional_arguments_not_allowed"
    submit.assert_not_called()
    settings.assert_not_called()


def test_accepted_request_binds_to_agent_service_query(agent_request, task_client, monkeypatch):
    from application import agent_service
    from server.task_runtime.handlers import execute_task

    client, submit, _ = task_client
    response = client.post("/api/v1/tasks", json=agent_request)
    assert response.status_code == 200
    submitted = submit.call_args.kwargs
    # Autospec enforces the real run signature without constructing its repository.
    service = create_autospec(agent_service.AgentApplicationService, instance=True)
    service.run.return_value = {"success": True, "answer": "离线契约验证"}
    service_factory = Mock(return_value=service)
    monkeypatch.setattr(agent_service, "AgentApplicationService", service_factory)

    result = execute_task(
        submitted["task_type"],
        {"args": submitted["args"], "kwargs": submitted["kwargs"]},
        emit=Mock(),
        is_cancelled=lambda: False,
        secrets=submitted["secrets"],
    )

    assert result == service.run.return_value
    service_factory.assert_called_once_with(None)
    service.run.assert_called_once_with(**submitted["kwargs"], llm_settings=None)


def test_agent_resume_cannot_bypass_approval_endpoint(agent_request, task_client):
    client, submit, settings = task_client
    agent_request['task_type'] = 'agent.resume'
    response = client.post('/api/v1/tasks', json=agent_request)
    assert response.status_code == 403
    submit.assert_not_called()
    settings.assert_not_called()
