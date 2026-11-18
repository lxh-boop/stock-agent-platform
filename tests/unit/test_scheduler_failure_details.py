from __future__ import annotations

import pytest

from scheduler.daily_worker import _process_failure_details


WARNING = (
    "/app/factor_storage.py:90: PerformanceWarning: DataFrame is highly fragmented.\n"
    "  data['future_5d_up'] = (data['future_5d_ret'] > 0).astype(int)\n"
)


@pytest.fixture(autouse=True)
def no_account_credentials(monkeypatch):
    monkeypatch.setattr("core.user_scope.current_owner", lambda: "")


def error_log(tmp_path, text: str):
    path = tmp_path / "market_update.err.log"
    path.write_text(text, encoding="utf-8")
    return path


def traceback_for(summary: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        '  File "/private/workspace/daily_incremental_update.py", line 277, in main\n'
        '    raise RuntimeError("source-only-secret")\n'
        f"{summary}\n"
    )


def test_warning_does_not_hide_the_terminal_exception(tmp_path):
    summary = "RuntimeError: 主动排名所需 Tushare 特征未就绪：index_daily"
    path = error_log(tmp_path, WARNING + traceback_for(summary))

    assert _process_failure_details(path) == summary


def test_long_error_line_keeps_exception_type_and_redacts_before_limit(tmp_path):
    summary = "RuntimeError: index_daily incomplete token=synthetic-secret " + "missing-date " * 2000
    path = error_log(tmp_path, WARNING * 10000 + traceback_for(summary))

    result = _process_failure_details(path)

    assert result.startswith("RuntimeError: index_daily incomplete token=[redacted]")
    assert "synthetic-secret" not in result
    assert len(result) <= 1200


@pytest.mark.parametrize("summary", [
    "MemoryError",
    "KeyboardInterrupt",
    "SystemExit: 1",
    "psycopg.errors.UndefinedTable: relation missing",
    "application.CustomFailure: unable to finish",
    "RuntimeWarning: explicitly raised as an exception",
])
def test_exception_classes_and_messages_are_preserved(tmp_path, summary):
    path = error_log(tmp_path, traceback_for(summary) + "HINT: private database detail\n")

    assert _process_failure_details(path) == summary


def test_last_chained_exception_wins_even_with_later_warning(tmp_path):
    path = error_log(
        tmp_path,
        traceback_for("ValueError: inner failure")
        + "\nThe above exception was the direct cause of the following exception:\n\n"
        + traceback_for("RuntimeError: final failure")
        + WARNING,
    )

    assert _process_failure_details(path) == "RuntimeError: final failure"


def test_truncated_traceback_header_does_not_expose_frames(tmp_path):
    frame = '  File "/private/workspace/worker.py", line 10, in run\n    source_only_secret()\n'
    path = error_log(tmp_path, "Traceback (most recent call last):\n" + frame * 2000 + "MemoryError\n")

    assert _process_failure_details(path) == "MemoryError"


def test_error_summary_uses_existing_credential_and_url_redaction(tmp_path, monkeypatch):
    monkeypatch.setattr("core.user_scope.current_owner", lambda: "test-owner")
    monkeypatch.setattr(
        "application.user_credentials_service.private_credential_for_user",
        lambda _owner, kind: "saved-test-credential" if kind == "tushare_token" else "",
    )
    path = error_log(tmp_path, WARNING + traceback_for(
        "RuntimeError: token=inline-token password=inline-password api_key=inline-key "
        "saved-test-credential https://example.invalid/private?token=url-token"
    ))

    result = _process_failure_details(path)

    assert result.startswith("RuntimeError:")
    assert "[redacted]" in result and "[url-redacted]" in result
    for secret in ("inline-token", "inline-password", "inline-key", "saved-test-credential", "example.invalid"):
        assert secret not in result
    assert "Traceback" not in result and "/private/workspace" not in result


def test_credential_lookup_failure_remains_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("core.user_scope.current_owner", lambda: "test-owner")

    def unavailable(*_args):
        raise RuntimeError("credential store unavailable")

    monkeypatch.setattr("application.user_credentials_service.private_credential_for_user", unavailable)
    path = error_log(tmp_path, traceback_for("RuntimeError: unsanitized-private-value"))

    assert _process_failure_details(path) == "日更失败；读取安全诊断失败，请检查服务器任务日志。"


def test_native_failure_without_traceback_remains_visible_and_redacted(tmp_path):
    path = error_log(tmp_path, WARNING + "libgomp: Thread creation failed: Resource temporarily unavailable token=private-value\n")

    result = _process_failure_details(path)

    assert "libgomp: Thread creation failed" in result
    assert "private-value" not in result
    assert "PerformanceWarning" not in result


def test_missing_log_has_fixed_message(tmp_path):
    assert _process_failure_details(tmp_path / "missing.err.log") == "未能读取子进程错误日志"


@pytest.mark.parametrize("text", ["", WARNING, "Traceback (most recent call last):\n", "x" * 100000])
def test_incomplete_diagnostics_do_not_present_warning_or_stack_as_root_cause(tmp_path, text):
    assert _process_failure_details(error_log(tmp_path, text)) == "未找到完整的子进程异常摘要，请检查服务器任务日志。"
