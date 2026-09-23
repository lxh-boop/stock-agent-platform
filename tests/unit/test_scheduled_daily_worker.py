from scheduler.daily_worker import _resolve_trade_date
from scheduler.daily_worker import run_scheduled_daily_update
from scheduler.schemas import SchedulerStatus


def test_auto_trade_date_waits_for_completed_daily_data() -> None:
    assert _resolve_trade_date(None, "2026-09-23T13:26:00", force=False) == (
        "2026-09-22",
        True,
    )
    assert _resolve_trade_date(None, "2026-09-23T16:00:00", force=False) == (
        "2026-09-23",
        True,
    )


def test_explicit_trade_date_is_not_changed() -> None:
    assert _resolve_trade_date("2026-09-23", "2026-09-23T13:26:00", force=False) == (
        "2026-09-23",
        True,
    )


def test_scheduled_worker_skips_non_trading_day_without_force(tmp_path) -> None:
    result = run_scheduled_daily_update(
        run_time="2026-06-13",
        root=tmp_path,
        output_dir=tmp_path / "outputs",
        db_path=tmp_path / "agent.db",
        public_task_runner=lambda **_: {"status": SchedulerStatus.SUCCESS},
        user_task_runner=lambda **_: {"status": SchedulerStatus.SUCCESS},
    )

    assert result.overall_status == SchedulerStatus.SKIPPED
    assert "not an A-share trading day" in result.warnings[0]


def test_scheduled_worker_runs_public_and_user_tasks_with_force(tmp_path) -> None:
    seen_users: list[str] = []

    def public_runner(**kwargs):
        return {
            "status": SchedulerStatus.SUCCESS,
            "metadata": {
                "ranking_output_path": "outputs/ranking_latest.csv",
                "news_event_count": 2,
                "news_chunk_count": 5,
            },
        }

    def user_runner(**kwargs):
        seen_users.append(kwargs["user_id"])
        return {
            "status": SchedulerStatus.SUCCESS,
            "recommendation_count": 4,
            "paper_order_count": 1,
            "position_count": 1,
            "report_path": f"report_{kwargs['user_id']}.md",
        }

    result = run_scheduled_daily_update(
        run_time="2026-06-13",
        user_ids=["u1", "u2"],
        force=True,
        root=tmp_path,
        output_dir=tmp_path / "outputs",
        db_path=tmp_path / "agent.db",
        public_task_runner=public_runner,
        user_task_runner=user_runner,
    )

    assert result.overall_status == SchedulerStatus.SUCCESS
    assert seen_users == ["u1", "u2"]
    assert result.news_count == 2
    assert result.recommendation_count == 8
    assert result.paper_order_count == 2
