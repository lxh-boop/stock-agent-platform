from __future__ import annotations

from typing import Any

from .models import CapabilityBoundary


ACCEPTANCE_RULES: dict[str, str] = {
    "schema_valid": "输出必须是已物化的结构化业务数据，并满足声明的required_paths。",
    "entity_scope_consistent": "输出实体范围必须与锁定GraphRef一致。",
    "business_empty_explicit": "查询成功但无记录时也必须产出对应数据名称，值允许为空。",
    "failure_kind_classified": "参数缺失、上下文缺失、工具失败、业务为空和业务不足必须分类。",
    "facts_and_analysis_separated": "事实与分析必须结构化分离。",
    "uncertainty_explicit": "分析必须表达不确定性和局限。",
    "no_new_business_claims": "不得增加当前工作记忆中不存在的业务事实。",
    "proposal_requires_approval": "状态变更建议必须明确只是一份待审批方案。",
    "no_persistent_write": "当前能力不得持久化修改业务状态。",
    "goal_coverage": "输出必须覆盖当前任务目标。",
}


# Canonical Need semantic vocabulary.  Data requirements resolve directly to
# simple ContextBundle labels.  The label means only the name of the data; a
# label is created only after a query/generation successfully completes, and an
# empty value still counts as an existing label.
SEMANTIC_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "authoritative_entity": {
        "kind": "context", "context_name": "authoritative_entity_refs",
        "semantic_role": "已解析并锁定的权威金融实体引用",
        "source_policy": "system", "satisfaction_rule": "non_empty",
    },
    "external_evidence": {
        "kind": "data", "data_name": "evidence",
        "semantic_role": "目标实体已查询完成的外部证据",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "entity_model_signals": {
        "kind": "data", "data_name": "prediction",
        "semantic_role": "本系统针对目标实体产生的模型预测与评分数据",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "market_ranking": {
        "kind": "data", "data_name": "ranking",
        "semantic_role": "本系统产生的市场排名数据",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "entity_analysis": {
        "kind": "data", "data_name": "analysis",
        "semantic_role": "基于当前工作记忆形成的结构化实体分析",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "entity_uncertainty": {
        "kind": "data", "data_name": "analysis_uncertainty",
        "semantic_role": "实体分析的不确定性与数据边界",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "portfolio_state": {
        "kind": "data", "data_name": "portfolio",
        "semantic_role": "当前完整投资组合状态",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "portfolio_positions": {
        "kind": "data", "data_name": "positions",
        "semantic_role": "当前投资组合持仓明细",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "account_state": {
        "kind": "data", "data_name": "account",
        "semantic_role": "当前账户资金状态",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "user_constraints": {
        "kind": "data", "data_name": "user_constraints",
        "semantic_role": "用户投资目标、风险与流动性等约束",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "user_profile": {
        "kind": "data", "data_name": "user_profile",
        "semantic_role": "用户画像与投资偏好状态",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "portfolio_risk": {
        "kind": "data", "data_name": "risk",
        "semantic_role": "组合风险、集中度与约束分析结果",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "risk_constraint_review": {
        "kind": "data", "data_name": "risk_constraints",
        "semantic_role": "用户硬约束与风险边界审查结果",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "rebalance_proposal": {
        "kind": "data", "data_name": "proposal",
        "semantic_role": "等待用户审批的调仓/配置建议",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "rebalance_instructions": {
        "kind": "data", "data_name": "rebalance",
        "semantic_role": "调仓建议中的目标配置与变更指令",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "target_allocation": {
        "kind": "parameter", "parameter_id": "target_asset_allocation",
        "semantic_role": "用户明确指定、用于情景测算的目标配置比例或投入金额",
        "source_policy": "user", "satisfaction_rule": "one_of",
        "satisfy_by": ["target_weight", "target_ratio", "target_amount", "investment_amount"],
        "description": "需用户明确指定的目标配置比例或投入金额",
        "expected_format": "percentage or cash amount",
    },
    "entity_fundamentals": {
        "kind": "data", "data_name": "financial",
        "semantic_role": "连续期间的结构化财务基本面数据",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "market_snapshot": {
        "kind": "data", "data_name": "market",
        "semantic_role": "满足本轮时点要求的行情与估值数据",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "peer_valuation_context": {
        "kind": "data", "data_name": "valuation",
        "semantic_role": "同业可比公司估值与比较数据",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
    "market_flow_context": {
        "kind": "data", "data_name": "market_flow",
        "semantic_role": "目标实体或行业的资金流与市场风格数据",
        "source_policy": "system", "satisfaction_rule": "exists",
    },
}


def _boundary(
    *,
    boundary_id: str,
    name: str,
    description: str,
    responsibilities: list[str],
    non_responsibilities: list[str],
    accepted_data_patterns: list[str],
    produced_data_patterns: list[str],
    input_data_examples: list[str],
    output_data_examples: list[str],
    allowed_acceptance_rule_ids: list[str],
    accepted_business_parameter_patterns: list[str] | None = None,
    required_runtime_context_names: list[str] | None = None,
    allowed_information_sources: list[str] | None = None,
    mutation_allowed: bool = False,
    completion_principles: list[str] | None = None,
) -> CapabilityBoundary:
    return CapabilityBoundary(
        boundary_id=boundary_id,
        name=name,
        description=description,
        responsibilities=responsibilities,
        non_responsibilities=non_responsibilities,
        accepted_data_patterns=accepted_data_patterns,
        produced_data_patterns=produced_data_patterns,
        accepted_business_parameter_patterns=list(accepted_business_parameter_patterns or []),
        input_data_examples=input_data_examples,
        output_data_examples=output_data_examples,
        allowed_acceptance_rule_ids=allowed_acceptance_rule_ids,
        required_runtime_context_names=list(required_runtime_context_names or []),
        allowed_information_sources=list(allowed_information_sources or []),
        mutation_allowed=mutation_allowed,
        completion_principles=list(completion_principles or []),
    )


_BOUNDARIES: dict[str, CapabilityBoundary] = {
    "external_evidence.research": _boundary(
        boundary_id="external_evidence.research",
        name="外部证据研究",
        description="查询并整理目标实体的新闻、公告和RAG证据，成功结束后写入当前Run的evidence数据名称。",
        responsibilities=["查询外部证据", "按实体整理结果", "空结果也形成已查询数据名称"],
        non_responsibilities=["最终实体分析", "风险评级", "状态修改"],
        accepted_data_patterns=[],
        produced_data_patterns=["evidence", "evidence_sources"],
        input_data_examples=[],
        output_data_examples=["evidence"],
        allowed_acceptance_rule_ids=["schema_valid", "entity_scope_consistent", "business_empty_explicit", "no_persistent_write"],
        required_runtime_context_names=["authoritative_entity_refs"],
        allowed_information_sources=["registered_external_evidence_tools"],
    ),
    "internal_fact.retrieval": _boundary(
        boundary_id="internal_fact.retrieval",
        name="内部权威事实读取",
        description="读取模型预测、排名、指标、回测和策略等系统内部数据并写入当前Run工作记忆。",
        responsibilities=["读取内部事实", "按简单数据名称发布"],
        non_responsibilities=["最终实体分析", "业务修改"],
        accepted_data_patterns=[],
        produced_data_patterns=["prediction", "ranking", "model_metrics", "backtest", "strategy"],
        input_data_examples=[],
        output_data_examples=["prediction", "ranking", "model_metrics", "backtest", "strategy"],
        allowed_acceptance_rule_ids=["schema_valid", "entity_scope_consistent", "business_empty_explicit", "failure_kind_classified", "no_persistent_write"],
        required_runtime_context_names=["user_identity"],
        allowed_information_sources=["registered_internal_read_tools"],
    ),
    "portfolio.analysis": _boundary(
        boundary_id="portfolio.analysis",
        name="组合事实读取",
        description="读取当前账户、组合和持仓等内部事实并写入当前Run工作记忆。",
        responsibilities=["读取组合事实", "读取账户状态"],
        non_responsibilities=["风险结论", "调仓建议", "修改持仓"],
        accepted_data_patterns=[],
        produced_data_patterns=["portfolio", "positions", "account"],
        input_data_examples=[],
        output_data_examples=["portfolio", "positions", "account"],
        allowed_acceptance_rule_ids=["schema_valid", "business_empty_explicit", "failure_kind_classified", "no_persistent_write"],
        required_runtime_context_names=["user_identity"],
        allowed_information_sources=["registered_portfolio_read_tools"],
    ),
    "user_context.retrieval": _boundary(
        boundary_id="user_context.retrieval",
        name="用户上下文读取",
        description="读取用户画像与约束并写入当前Run工作记忆。",
        responsibilities=["读取用户画像", "读取用户约束"],
        non_responsibilities=["风险结论", "状态修改"],
        accepted_data_patterns=[],
        produced_data_patterns=["user_profile", "user_constraints"],
        input_data_examples=[],
        output_data_examples=["user_profile", "user_constraints"],
        allowed_acceptance_rule_ids=["schema_valid", "failure_kind_classified", "no_persistent_write"],
        required_runtime_context_names=["user_identity"],
        allowed_information_sources=["registered_user_context_read_tools"],
    ),
    "graph_relation.retrieval": _boundary(
        boundary_id="graph_relation.retrieval",
        name="图关系读取",
        description="在已锁定GraphRef范围内查询关系和路径，成功结束后写入relations数据。",
        responsibilities=["读取图关系", "读取关联路径"],
        non_responsibilities=["创建实体", "最终业务判断", "图写入"],
        accepted_data_patterns=[],
        produced_data_patterns=["relations"],
        input_data_examples=[],
        output_data_examples=["relations"],
        allowed_acceptance_rule_ids=["schema_valid", "entity_scope_consistent", "business_empty_explicit", "no_persistent_write"],
        required_runtime_context_names=["authoritative_entity_refs"],
        allowed_information_sources=["registered_graph_read_tools"],
    ),
    "entity.analysis": _boundary(
        boundary_id="entity.analysis",
        name="实体分析",
        description="对已解析实体的当前Run工作记忆进行结构化分析；不负责查询数据。",
        responsibilities=["分析实体", "比较实体", "判断当前数据质量与充分性", "表达不确定性"],
        non_responsibilities=["定位实体", "查询数据", "指定其他Worker或Tool"],
        accepted_data_patterns=["*"],
        produced_data_patterns=["analysis", "analysis_uncertainty"],
        input_data_examples=["evidence", "prediction", "financial", "market"],
        output_data_examples=["analysis", "analysis_uncertainty"],
        allowed_acceptance_rule_ids=["schema_valid", "facts_and_analysis_separated", "uncertainty_explicit", "no_new_business_claims", "no_persistent_write"],
        allowed_information_sources=["run_context_store"],
    ),
    "portfolio.risk_assessment": _boundary(
        boundary_id="portfolio.risk_assessment",
        name="组合风险分析",
        description="读取当前Run工作记忆中的组合、账户、用户约束和已有分析，形成风险判断；不修改状态。",
        responsibilities=["组合风险分析", "集中度与暴露分析", "约束审查", "判断数据充分性"],
        non_responsibilities=["查询原始业务数据", "修改持仓", "生成Commit"],
        accepted_data_patterns=["*"],
        produced_data_patterns=["risk", "risk_constraints"],
        accepted_business_parameter_patterns=["target_asset_allocation", "target_weight", "target_amount", "target_ratio", "allocation_ratio", "investment_amount", "planned_amount"],
        input_data_examples=["portfolio", "positions", "account", "user_constraints", "analysis"],
        output_data_examples=["risk", "risk_constraints"],
        allowed_acceptance_rule_ids=["schema_valid", "failure_kind_classified", "uncertainty_explicit", "no_persistent_write"],
        allowed_information_sources=["run_context_store", "registered_risk_tools"],
    ),
    "state_change.proposal": _boundary(
        boundary_id="state_change.proposal",
        name="状态变更建议",
        description="读取当前Run工作记忆并生成待审批建议；生成建议不是业务状态修改。",
        responsibilities=["生成待审批建议", "审查当前约束", "明确审批边界"],
        non_responsibilities=["直接Commit", "修改持仓或资金", "绕过Approval"],
        accepted_data_patterns=["*"],
        produced_data_patterns=["proposal", "rebalance"],
        accepted_business_parameter_patterns=["target_asset_allocation", "target_weight", "target_amount", "target_ratio", "allocation_ratio", "investment_amount", "planned_amount"],
        input_data_examples=["portfolio", "positions", "user_constraints", "risk", "analysis"],
        output_data_examples=["proposal", "rebalance"],
        allowed_acceptance_rule_ids=["schema_valid", "proposal_requires_approval", "no_persistent_write", "goal_coverage"],
        allowed_information_sources=["run_context_store"],
        mutation_allowed=False,
        completion_principles=["建议与Commit分离"],
    ),
    "result.composition": _boundary(
        boundary_id="result.composition",
        name="结果汇总",
        description="把Request完成状态和结构化结果组织成用户可读报告。",
        responsibilities=["组织自然语言", "表达局限"],
        non_responsibilities=["重新查询数据", "新增专业判断", "修改业务状态"],
        accepted_data_patterns=[],
        produced_data_patterns=["report", "goal_summary"],
        input_data_examples=[],
        output_data_examples=["report", "goal_summary"],
        allowed_acceptance_rule_ids=["schema_valid", "no_new_business_claims", "goal_coverage", "no_persistent_write"],
        allowed_information_sources=["request_result_state"],
    ),
    "system.diagnosis": _boundary(
        boundary_id="system.diagnosis",
        name="系统诊断",
        description="直接读取Runtime/Request/Task/WorkerResult状态诊断运行问题。",
        responsibilities=["区分失败类型", "检查运行状态", "输出诊断结论"],
        non_responsibilities=["金融研究", "交易建议", "修改业务状态"],
        accepted_data_patterns=[],
        produced_data_patterns=["diagnosis", "runtime_status"],
        input_data_examples=[],
        output_data_examples=["diagnosis", "runtime_status"],
        allowed_acceptance_rule_ids=["schema_valid", "failure_kind_classified", "no_persistent_write"],
        required_runtime_context_names=["runtime_context"],
        allowed_information_sources=["runtime_request_task_worker_state"],
    ),
    "context.resolution": _boundary(
        boundary_id="context.resolution",
        name="上下文补齐",
        description="在已知边界内读取可验证的运行上下文，不猜测用户业务参数。",
        responsibilities=["读取可验证运行上下文"],
        non_responsibilities=["猜测用户参数", "修改业务状态"],
        accepted_data_patterns=[],
        produced_data_patterns=["resolved_context"],
        input_data_examples=[],
        output_data_examples=["resolved_context"],
        allowed_acceptance_rule_ids=["schema_valid", "failure_kind_classified", "no_persistent_write"],
        required_runtime_context_names=["runtime_context"],
        allowed_information_sources=["runtime_context", "verified_memory"],
    ),
    "graph_context.write": _boundary(
        boundary_id="graph_context.write",
        name="图上下文写入",
        description="读取当前Run工作记忆中的已验证业务数据并幂等写入非交易图上下文。",
        responsibilities=["非交易图上下文写入"],
        non_responsibilities=["交易下单", "绕过权限", "任意数据库写入"],
        accepted_data_patterns=["*"],
        produced_data_patterns=["portfolio_graph_context", "evidence_graph_context"],
        input_data_examples=["portfolio", "positions", "evidence"],
        output_data_examples=["portfolio_graph_context", "evidence_graph_context"],
        allowed_acceptance_rule_ids=["schema_valid", "entity_scope_consistent"],
        allowed_information_sources=["run_context_store"],
        mutation_allowed=True,
        completion_principles=["显式写权限", "幂等审计"],
    ),
}


class CapabilityRegistry:
    """Business-boundary registry with simple Working-Memory data names."""

    def __init__(self, directory: Any | None = None) -> None:
        del directory
        self._boundaries = dict(_BOUNDARIES)

    def get_boundary(self, boundary_id: str) -> CapabilityBoundary:
        key = str(boundary_id or "").strip()
        if key not in self._boundaries:
            raise KeyError(f"unknown_capability_boundary:{key}")
        return self._boundaries[key]

    def aggregate_scope(self, boundary_ids: list[str] | tuple[str, ...] | set[str]) -> dict[str, Any]:
        boundaries = [self.get_boundary(str(boundary_id)) for boundary_id in boundary_ids]
        if not boundaries:
            raise KeyError("empty_worker_capability_scope")

        def merge(name: str) -> list[str]:
            return list(dict.fromkeys(
                str(item)
                for boundary in boundaries
                for item in getattr(boundary, name, []) or []
                if str(item)
            ))

        return {
            "source_boundary_ids": [boundary.boundary_id for boundary in boundaries],
            "accepted_data_patterns": merge("accepted_data_patterns"),
            "produced_data_patterns": merge("produced_data_patterns"),
            "accepted_business_parameter_patterns": merge("accepted_business_parameter_patterns"),
            "input_data_examples": merge("input_data_examples"),
            "output_data_examples": merge("output_data_examples"),
            "allowed_acceptance_rule_ids": merge("allowed_acceptance_rule_ids"),
            "required_runtime_context_names": merge("required_runtime_context_names"),
            "allowed_information_sources": merge("allowed_information_sources"),
            "completion_principles": merge("completion_principles"),
            "mutation_allowed": any(boundary.mutation_allowed for boundary in boundaries),
        }

    def semantic_requirement_exists(self, semantic_key: str) -> bool:
        return str(semantic_key or "").strip() in SEMANTIC_REQUIREMENTS

    def semantic_requirement(self, semantic_key: str) -> dict[str, Any]:
        key = str(semantic_key or "").strip()
        if key not in SEMANTIC_REQUIREMENTS:
            raise KeyError(f"unknown_semantic_requirement:{key}")
        return dict(SEMANTIC_REQUIREMENTS[key])

    def semantic_requirement_catalog(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, raw in sorted(SEMANTIC_REQUIREMENTS.items()):
            row = dict(raw)
            row["semantic_key"] = key
            rows.append(row)
        return rows

    def public_catalog(
        self,
        *,
        effect_limit: str,
        boundary_ids: list[str] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        del effect_limit
        allowed = {str(item) for item in (boundary_ids or []) if str(item)}
        rows: list[dict[str, Any]] = []
        for boundary_id, boundary in sorted(self._boundaries.items()):
            if allowed and boundary_id not in allowed:
                continue
            # BUSINESS planning never grants mutation rights. Mutating Workers
            # are used only by the explicit control/write path.
            if boundary.mutation_allowed:
                continue
            rows.append(boundary.safe_for_main_agent())
        return rows


__all__ = ["ACCEPTANCE_RULES", "CapabilityRegistry", "SEMANTIC_REQUIREMENTS"]
