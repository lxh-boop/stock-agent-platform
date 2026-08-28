from agent.context.context_types import ContextBundle


def test_context_bundle_uses_run_context_store_for_single_run_working_memory():
    bundle = ContextBundle(
        user_id="u1",
        conversation_id="c1",
        run_id="run_1",
    )
    assert bundle.metadata["working_memory_model"] == "run_context_store"
    assert bundle.metadata["working_memory_scope"] == "single_agent_run"
    assert bundle.metadata["run_context_store_backend"] == "memory"
    assert bundle.runtime_context.run_id == "run_1"
    assert not hasattr(bundle, "business_data")

    bundle.runtime_context.completed_tasks.append("risk")
    bundle.runtime_context.failed_tasks.append("news")
    bundle.runtime_context.replan_count = 1
    bundle.runtime_context.pending_tasks.append("constraint_check")

    minimal = bundle.to_minimal_context()
    assert minimal["working_state"] == {
        "phase": "",
        "completed_tasks": ["risk"],
        "failed_tasks": ["news"],
        "pending_tasks": ["constraint_check"],
        "replan_count": 1,
        "missing_outputs": [],
        "completion_status": "",
    }
