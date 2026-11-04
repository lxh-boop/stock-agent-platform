from __future__ import annotations

from agent_graph.workers.cards import CapabilityWorkerDirectory


def test_w04_public_description_declares_slot_driven_target_asset_scenario_requirements() -> None:
    w04 = CapabilityWorkerDirectory().get("W04")
    assert "目标标的" in w04.full_description
    assert "目标配置比例或投入金额" in w04.full_description
    assert "不能自行假设仓位" in w04.full_description
    assert "不与任何特定上游Worker ID硬绑定" in " ".join(w04.limitations)
    assert "目标标的纳入组合的风险情景分析" in w04.supported_scenarios
