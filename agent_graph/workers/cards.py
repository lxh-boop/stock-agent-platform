"""Authoritative Worker implementation directory.

MainAgent receives every eligible Worker's complete public capability description upfront.
Private prompts and private Tool IDs never leave the Worker runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EVIDENCE_COLLECTOR = "EVIDENCE_COLLECTOR"
PORTFOLIO_ANALYST = "PORTFOLIO_ANALYST"
GRAPH_RELATION_RETRIEVER = "GRAPH_RELATION_RETRIEVER"
RISK_ANALYST = "RISK_ANALYST"
STRATEGY_GUARD = "STRATEGY_GUARD"
REPORT_WRITER = "REPORT_WRITER"
SYSTEM_DIAGNOSTIC = "SYSTEM_DIAGNOSTIC"
DATABASE_WRITER = "DATABASE_WRITER"
ENTITY_ANALYST = "ENTITY_ANALYST"


@dataclass(frozen=True)
class CapabilityWorkerCard:
    worker_id: str
    agent_id: str
    role: str
    short_description: str
    full_description: str
    supported_boundary_ids: list[str]
    delegation_description: str = ""
    delegate_when: list[str] = field(default_factory=list)
    capability_tags: list[str] = field(default_factory=list)
    supported_scenarios: list[str] = field(default_factory=list)
    unsupported_scenarios: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    escalation_policy: str = ""
    execution_mode: str = "pure_llm"
    output_publication_mode: str = "worker_synthesized"
    can_mutate: bool = False
    execution_stage: str = "analysis"  # provider | analysis | decision | report | diagnostic | mutation
    private_worker_prompt: str = ""
    private_tool_ids: list[str] = field(default_factory=list)
    model_profile: str = ""
    availability: str = "available"
    working_memory_mode: str = "none"  # provider | consumer | none


_CARDS = {
    "W01": CapabilityWorkerCard(
        worker_id="W01",
        agent_id=EVIDENCE_COLLECTOR,
        role="external_evidence_research",
        short_description="检索并整理目标实体相关的新闻、公告和RAG证据。",
        full_description=(
            "负责外部证据的检索、合并、去重、来源时间保留和业务为空表达。"
            "只产出可追溯证据，不形成最终金融判断，也不写入业务状态。"
        ),
        supported_boundary_ids=["external_evidence.research"],
        capability_tags=["news", "announcement", "rag", "evidence"],
        supported_scenarios=["实体新闻检索", "公告与研报证据收集", "证据去重与覆盖检查"],
        unsupported_scenarios=["最终实体分析", "风险评级", "状态变更"],
        limitations=["依赖已解析的权威实体上下文", "不补造不存在的证据"],
        escalation_policy="私有Tool重试或替换仍无法形成证据槽位时上报MainAgent。",
        execution_mode="hybrid",
        working_memory_mode="provider",
        execution_stage="provider",
        private_worker_prompt="根据合同列表选择最小私有Tool DAG，收集可追溯外部证据；不得分析或写入。",
        private_tool_ids=["evidence.search_news", "evidence.search_rag", "evidence.finalize_collection"],
    ),
    "W02": CapabilityWorkerCard(
        worker_id="W02",
        agent_id=PORTFOLIO_ANALYST,
        role="system_internal_fact_provider",
        short_description="读取本系统内部产生或保存的权威数据，并写入本轮Working Memory的结构化数据标签。",
        full_description=(
            "W02是系统内部权威数据提供者。它从本应用自己的只读数据源读取模型预测、评分/排名、"
            "模型质量、回测摘要、当前选定策略、账户、组合、持仓、用户画像和约束，并写入标准Working Memory数据标签。"
            "它可根据MainAgent下发的业务目标，自主规划私有Tool DAG：既可直接读取已知实体的内部信号，也可先读取全市场排名、"
            "再通过权威实体解析确定目标后继续读取相关内部事实。它不负责最终实体分析或自然语言报告；下游Worker可消费这些工作记忆数据继续分析。"
        ),
        supported_boundary_ids=["internal_fact.retrieval", "portfolio.analysis", "user_context.retrieval"],
        delegation_description=(
            "当用户问题需要引用本系统内部已有或本系统自己生成的权威数据时，考虑委派W02。"
            "这包括但不限于模型预测/评分、市场排名、模型指标、回测、选定策略、账户、组合、持仓、"
            "用户画像或约束；W02只读取并发布结构化事实数据；Runtime负责写入Working Memory供后续分析复用。"
        ),
        delegate_when=[
            "回答需要本系统自己的模型、评分、排名、回测或策略状态",
            "回答需要本系统保存的账户、组合、持仓、画像或约束",
            "其他Worker的分析目标需要复用系统内部已查询事实",
        ],
        capability_tags=[
            "system_data", "internal_fact", "prediction", "ranking", "model_metrics",
            "backtest", "strategy_state", "portfolio", "account", "profile",
        ],
        supported_scenarios=[
            "系统内部模型信号读取", "全市场排名后自主定位目标实体", "评分与排名读取", "模型质量与回测读取",
            "当前策略状态读取", "当前组合与持仓读取", "账户与用户约束读取",
        ],
        unsupported_scenarios=["外部新闻研究", "最终报告写作", "业务写入"],
        limitations=["只读取内部权威数据", "不把读取结果自行扩展成最终业务结论"],
        escalation_policy="私有Tool均无法完成合同结果时上报MainAgent。",
        execution_mode="hybrid",
        working_memory_mode="provider",
        execution_stage="provider",
        output_publication_mode="private_tool_passthrough",
        private_worker_prompt=(
            "根据MainAgent给出的业务目标和合同承诺结果，自主规划最小私有Tool DAG。"
            "只有当业务目标本身是全市场排名、筛选或发现候选证券时，才允许先读取排名并通过"
            "internal.entity.resolve_ranked_security把排名结果解析成GraphRef后继续调用实体Tool。"
            "如果业务目标明确指向某个命名证券/公司，而available_context没有该目标的权威security_node_id/GraphRef，"
            "绝不能用排名第一或其他候选证券替代目标；此时实体特定输出应保持缺失并按现有失败合同上报。"
            "只发布结构化内部事实，不做最终分析或报告。"
        ),
        private_tool_ids=[
            "internal.prediction.get_stock", "internal.ranking.get_latest",
            "internal.entity.resolve_ranked_security",
            "internal.model.get_metrics", "internal.backtest.get_summary",
            "internal.strategy.get_selected", "internal.portfolio.get_state",
            "internal.account.get_state", "internal.user_profile.get",
        ],
    ),
    "W03": CapabilityWorkerCard(
        worker_id="W03",
        agent_id=GRAPH_RELATION_RETRIEVER,
        role="graph_relation_retrieval",
        short_description="根据已绑定GraphRef上下文读取邻域关系、关联事实和可追溯路径。",
        full_description=(
            "负责在锁定GraphRef范围内读取金融图关系。Tool可用性由任务目标、GraphRef上下文和Worker私有Tool合同决定，"
            "不得根据实体数量猜测邻域或路径查询。"
        ),
        supported_boundary_ids=["graph_relation.retrieval"],
        capability_tags=["graph", "relation", "neighborhood", "path"],
        supported_scenarios=["实体邻域关系读取", "显式来源与目标之间的路径读取"],
        unsupported_scenarios=["创建实体", "最终业务解释", "图数据库写入"],
        limitations=["关系端点必须来自权威GraphRef", "不推断未绑定的来源或目标角色"],
        escalation_policy="无兼容私有Tool或局部处理耗尽后上报MainAgent。",
        execution_mode="hybrid",
        working_memory_mode="provider",
        execution_stage="provider",
        output_publication_mode="private_tool_passthrough",
        private_worker_prompt=(
            "只在锁定GraphRef范围内读取关系，不创造实体。不得按实体数量决定工具；"
            "结合合同目标、GraphRef上下文、Tool摘要和produced_outputs规划。"
        ),
        private_tool_ids=["graph.relations.read_neighborhood", "graph.relations.find_paths"],
    ),
    "W04": CapabilityWorkerCard(
        worker_id="W04",
        agent_id=RISK_ANALYST,
        role="portfolio_risk_assessment",
        short_description="读取当前ContextBundle中的组合、账户、用户约束及相关业务数据，计算风险事实并形成风险评估。",
        full_description=(
            "负责基于当前Run的ContextBundle工作记忆完成集中度、暴露、账户风险事实和约束审查，不修改持仓。"
            "W04只面向当前任务对象读取工作记忆中的组合、持仓、账户和用户约束等业务数据，不固定依赖任何Worker；"
            "若任务要求评估外部目标标的/事件对组合的影响，应从当前工作记忆读取已经存在的目标分析或影响事实；"
            "目标配置比例或投入金额等用户决策参数必须由CapabilityContract声明，W04不能自行假设仓位。"
            "W04自行判断当前业务数据质量是否足够支持风险分析；Runtime只校验明确由用户拥有的业务参数，"
            "并以结构化Escalation向MainAgent上报缺失信息。"
            "业务数据不足时W04返回结构化不足说明，由Runtime决定后续调度。"
        ),
        supported_boundary_ids=["portfolio.risk_assessment", "context.resolution"],
        delegation_description=(
            "当用户目标需要对已经取得的组合、账户、持仓、用户约束或其他风险相关事实做集中度、暴露和风险边界分析时委派W04。"
            "W04不负责假设或补造业务事实；业务数据充分性由W04根据任务目标和当前ContextBundle自行判断。"
        ),
        delegate_when=[
            "需要基于已绑定组合或账户事实计算集中度、暴露或风险边界",
            "需要审查已绑定用户约束与当前组合之间的风险关系",
            "需要评估已验证目标标的加入当前组合后的风险/集中度影响",
            "下游调仓或方案Worker需要结构化风险分析数据作为依据",
        ],
        capability_tags=["risk", "concentration", "exposure", "constraint", "portfolio_scenario"],
        supported_scenarios=["组合集中度分析", "账户风险事实分析", "风险约束审查", "目标标的纳入组合的风险情景分析", "为下游方案提供结构化风险数据"],
        unsupported_scenarios=["读取未绑定的原始业务事实", "订单执行", "持仓写入", "最终调仓Proposal生成"],
        limitations=[
            "只读取本轮ContextBundle中与任务对象相关的业务数据",
            "外部目标/事件影响分析需要合同绑定已验证的分析或impact facts工作记忆数据",
            "用户决策参数必须由CapabilityContract声明并由Runtime校验，W04不得自行假设",
            "不与任何特定上游Worker ID硬绑定",
        ],
        escalation_policy="W04判断当前业务信息不足或私有Tool无法完成时，按结构化错误合同上报MainAgent。",
        execution_mode="hybrid",
        working_memory_mode="consumer",
        execution_stage="analysis",
        can_mutate=False,
        private_worker_prompt="基于目标对象当前ContextBundle工作记忆选择最小只读Tool DAG，区分风险事实和建议；自行判断现有数据是否足以支持风险分析，不指定上游Worker或Tool。",
        private_tool_ids=[
            "risk.calculate_concentration", "risk.read_account_risk_facts",
            "risk.summarize_exposure", "risk.finalize_facts",
        ],
    ),
    "W05": CapabilityWorkerCard(
        worker_id="W05",
        agent_id=STRATEGY_GUARD,
        role="state_change_proposal",
        short_description="读取当前ContextBundle中的状态、约束、风险分析等业务数据，生成或审查需要用户审批的状态变更Proposal。",
        full_description=(
            "只基于当前Run工作记忆中的业务事实与分析结果生成和审查待审批方案，不执行Commit，不绕过审批。"
            "调仓场景可以使用工作记忆中已经存在的风险数据，但W05不感知任何数据生产Worker；数据质量由W05结合任务目标自行判断。"
        ),
        supported_boundary_ids=["state_change.proposal"],
        delegation_description=(
            "当用户明确要求形成调仓、配置调整或其他状态变更方案时委派W05。"
            "W05根据本轮ContextBundle中的组合状态、用户约束、风险分析或其他必要业务数据形成待审批Proposal。"
        ),
        delegate_when=["存在明确的调仓或状态变更意图", "需要把当前工作记忆中的已验证事实/风险结论转成待审批Proposal"],
        capability_tags=["proposal", "approval", "strategy_guard"],
        supported_scenarios=["调仓方案生成", "状态变更约束审查", "消费上游风险分析形成Proposal"],
        unsupported_scenarios=["直接Commit", "绕过审批", "替代上游专业风险分析"],
        limitations=["必须存在显式变更意图", "只读取本轮ContextBundle，不固定依赖某个Worker"],
        escalation_policy="缺少形成合规Proposal所需的业务信息时上报MainAgent；只有MainAgent确认属于用户参数后才询问用户。",
        execution_mode="pure_llm",
        working_memory_mode="consumer",
        can_mutate=False,
        execution_stage="decision",
        private_worker_prompt="只读取当前ContextBundle生成或审查待审批Proposal，不得Commit。",
    ),
    "W06": CapabilityWorkerCard(
        worker_id="W06",
        agent_id=REPORT_WRITER,
        role="result_composition",
        short_description="把当前RequestBundle中的已验证Request结果组织为用户可读报告。",
        full_description="负责把Runtime提供的Request结果集合转换为用户自然语言，只保留结果中已经存在的不确定性，不重新查询、不补充专业事实。",
        supported_boundary_ids=["result.composition"],
        capability_tags=["report", "composition", "response"],
        supported_scenarios=["分析报告汇总", "诊断报告汇总", "Proposal说明"],
        unsupported_scenarios=["重新检索数据", "新增金融判断"],
        limitations=["只能使用Runtime提供的已验证Request结果"],
        escalation_policy="Request结果集合不完整时按现有Request状态组织回答，不自行补查。",
        execution_mode="pure_llm",
        execution_stage="report",
        private_worker_prompt=(
            "只把已验证Request结果转换为自然语言，不重新查询、不新增专业事实、不评价未提供的信息域。"
            "如果输入包含presentation_policy，必须把它视为最终呈现的唯一权威来源，严格遵守language/style/length/format；"
            "如果输入包含request_bundle_results，应按Request顺序和Request完成状态组织回答，明确completed、waiting_context、waiting_approval、unsupported、tool_failed等差异。"
        ),
    ),
    "W07": CapabilityWorkerCard(
        worker_id="W07",
        agent_id=SYSTEM_DIAGNOSTIC,
        role="system_diagnosis",
        short_description="诊断参数、上下文、工具、业务为空和运行时问题。",
        full_description="负责区分参数缺失、上下文缺失、工具失败、业务为空和业务不足。",
        supported_boundary_ids=["system.diagnosis", "context.resolution"],
        capability_tags=["diagnosis", "runtime", "context"],
        supported_scenarios=["运行失败诊断", "上下文问题诊断"],
        unsupported_scenarios=["金融研究", "业务写入"],
        limitations=["不替代专业业务Worker"],
        escalation_policy="诊断无法确认时明确返回未知，不猜测。",
        execution_mode="pure_llm",
        execution_stage="diagnostic",
        private_worker_prompt="严格区分调用参数缺失、上下文缺失、工具失败、业务为空和业务不足。",
    ),
    "W08": CapabilityWorkerCard(
        worker_id="W08",
        agent_id=DATABASE_WRITER,
        role="graph_context_write",
        short_description="将当前工作记忆中已验证的非交易结果幂等写入金融图上下文。",
        full_description="只读取当前ContextBundle中的已验证业务数据并执行非交易图上下文写入，保持权限、幂等和审计。",
        supported_boundary_ids=["graph_context.write"],
        capability_tags=["graph_write", "idempotent", "audit"],
        supported_scenarios=["组合图上下文写入", "证据图上下文写入"],
        unsupported_scenarios=["交易下单", "任意数据库写入"],
        limitations=["需要显式写合同和权限"],
        escalation_policy="权限或输入不满足时拒绝并上报MainAgent。",
        execution_mode="workflow_backed",
        working_memory_mode="consumer",
        can_mutate=True,
        execution_stage="mutation",
        private_worker_prompt="只执行已验证的非交易图上下文写入，保持幂等和审计。",
        private_tool_ids=["database.write_portfolio_graph_context", "database.write_evidence_graph_context"],
    ),
    "W09": CapabilityWorkerCard(
        worker_id="W09",
        agent_id=ENTITY_ANALYST,
        role="entity_analysis",
        short_description="分析已确定身份的金融实体，并基于当前工作记忆形成结构化判断。",
        full_description=(
            "W09只负责实体分析与比较。它只读取本轮其他Worker已经产生的GraphWorkerResult以及当前权威GraphRef；"
            "不调用Tool、不访问数据库/RAG/业务模型接口，也不直接调用其他Worker。"
            "其他Worker结果是否足以支撑当前分析目标由W09自行判断；不足时只说明缺少什么业务信息。"
        ),
        supported_boundary_ids=["entity.analysis"],
        capability_tags=["entity_analysis", "synthesis", "comparison", "uncertainty"],
        supported_scenarios=["个股综合分析", "多实体结构化分析", "实体对比分析"],
        unsupported_scenarios=["自行检索证据", "实体发现", "状态变更"],
        limitations=["只分析Runtime提供的目标实体工作记忆", "不指定数据来源Worker或Tool"],
        escalation_policy="当前实体工作记忆不足以支持可靠分析时，仅反馈缺失的信息内容与原因，由Runtime决定是否补查。",
        execution_mode="pure_llm",
        working_memory_mode="consumer",
        execution_stage="analysis",
        private_worker_prompt="只分析本轮其他Worker的GraphWorkerResult；自行判断结果质量与充分性，不调用任何检索、数据库、RAG、业务Tool或其他Worker。",
    ),
}


class CapabilityWorkerDirectory:
    def __init__(self) -> None:
        self._cards = dict(_CARDS)

    def get(self, identifier: str) -> CapabilityWorkerCard:
        key = str(identifier or "").strip().upper()
        if key in self._cards:
            return self._cards[key]
        for card in self._cards.values():
            if card.agent_id == key:
                return card
        raise KeyError(f"unknown_capability_worker:{identifier}")

    def list(self) -> list[CapabilityWorkerCard]:
        return [self._cards[key] for key in sorted(self._cards)]

    def private_tool_ids(self, worker_id: str) -> list[str]:
        return list(self.get(worker_id).private_tool_ids)

    def private_prompt(self, worker_id: str) -> str:
        return str(self.get(worker_id).private_worker_prompt)


__all__ = [
    "CapabilityWorkerCard", "CapabilityWorkerDirectory", "DATABASE_WRITER",
    "ENTITY_ANALYST", "EVIDENCE_COLLECTOR", "GRAPH_RELATION_RETRIEVER",
    "PORTFOLIO_ANALYST", "REPORT_WRITER", "RISK_ANALYST", "STRATEGY_GUARD",
    "SYSTEM_DIAGNOSTIC",
]
