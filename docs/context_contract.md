# 上下文链路结构清单

> 本文档按「字段名 → 中文说明」扁平模板记录上下文链路的核心数据结构。
> 所有英文标识符均附中文注释。定义代码为本项目唯一权威来源。

## RequestItem（请求项）

定义：`agent/collaboration/request_bundle.py:65`

```
RequestItem
request_id → 请求唯一 ID（包内不得重复，Runtime 管理用）
source_index → 来源索引（保留用户消息结构，对应确定性分条序号）
category → 请求大类（business 业务 / presentation 展示）
objective → 用户真正目标（规范化后的明确业务动作）
request_type → 请求类型（read 只读 / write 写入，仅 business 使用）
proposal_required → 是否需要提案确认（写链路安全标记）
target → 操作对象（动作作用的对象和参数）
constraints → 用户限制（执行时须遵守的约束列表）
depends_on → 请求级依赖（前置请求的 request_id 列表）
scope → 作用范围（current_turn 当前轮 / session 会话等）
status → 生命周期状态（pending / running / completed / waiting_context 等 14 个枚举值）
status_reason → 状态原因（阻塞或失败时的说明）
action_type → 动作类型（仅写请求：confirm_execute 确认执行 / reject 拒绝 / cancel 取消）
presentation → 展示配置（仅展示类请求使用）
context_binding → 上下文关联（实体范围语义指令）
    entity_scope → 实体范围（explicit_entities 显式提及 / conversation_focus 对话焦点 / portfolio 持仓 / account 账户 / global 全局 / none 无）
    inherit_previous_focus → 是否继承上轮焦点（布尔值，LLM 语义判断）
    reference_entity_type → 引用实体类型（security 证券 / portfolio 组合 / account 账户 / event 事件 / unknown 未知 / none 无）
    reason → LLM 的理由（截断 500 字符）
```

## ContextBinding（上下文绑定）

定义：`agent/collaboration/context_binding.py:26`（`@dataclass(frozen=True)` 不可变）

```
ContextBinding
entity_scope → 实体范围（explicit_entities / conversation_focus / portfolio / account / global / none）
inherit_previous_focus → 是否继承上轮焦点（布尔值，决定是否生成 previous_focus_entities 必需需求）
reference_entity_type → 引用实体类型（security / portfolio / account / event / unknown / none）
reason → LLM 的语义判断理由（截断 500 字符，供审计追溯）
```

## ContextRequirement（上下文需求）

定义：`agent/context/context_hydrator.py:13`（`@dataclass(frozen=True)` 不可变）

```
ContextRequirement
context_key → 需求键（如 session_summary / previous_focus_entities / typed_focus:security）
required → 是否必需（true=取不到要阻塞；false=尽力取，取不到兜底）
source_preferences → 来源优先级（如 ["session_state", "run_checkpoint"]）
entity_role → 实体角色（可选，标记该需求对应哪类实体）
freshness_policy → 新鲜度策略（默认 request_default）
authority_policy → 权威策略（默认 verified，只取已验证实体）
allow_session_inheritance → 是否允许会话继承（false 时即使声明也不读，防止不该继承时继承）
```

## HydratedContext（已补充上下文）

定义：`agent/context/context_hydrator.py:24`（`@dataclass(frozen=True)` 不可变）

```
HydratedContext
user_id → 用户身份标识（长期记忆检索、事件审计用）
session_id → 会话标识（session_state 读写、checkpoint 定位用）
session_summary → 会话摘要（最近 8 轮、截 2400 字符，过滤后进 planner 规划记忆）
previous_focus_refs → 上轮焦点实体（继承自 active_graph_refs，焦点合成第 3 级来源）
typed_focus_refs → 类型焦点指针（按 security/portfolio/event 各自持久，焦点合成第 4 级来源）
pending_run_ids → 待恢复的中断任务 ID（进入 CONTEXT_HYDRATED 审计事件）
pending_proposal_ids → 待确认的提案 ID（写链路二次确认用）
permission_context → 权限上下文（planner 允许读取的白名单）
available_parameters → 可用参数（top_k/model_name/trade_date/holding_period/as_of_time 等，防 Worker 编造决策参数）
long_term_memory_summary → 长期记忆摘要（相关度 0.42、预算 420 token，过滤后进 planner）
long_term_memory_refs → 长期记忆引用 ID 列表（审计用）
source_audit → 来源审计（逐字段记录"从哪读的"，可追溯）
to_dict() → 序列化方法（GraphRef 递归转 dict，用于事件审计 / checkpoint 持久化）
```

## 生命周期速览

```
① 上游：ContextBinding（LLM 语义决策，声明"要什么"）
   ↓
② 规则翻译：Coordinator 生成 ContextRequirement 列表（固定 3 个 + 实体 2 个）
   ↓
③ 生产：ContextHydrator.hydrate() 从 4 个来源取水 → HydratedContext（frozen 不可变）
   ↓
④ 消费：同一请求内 6 路分发（规划记忆 / 焦点合成 / 参数 / 权限 / 恢复 / 审计）
   ↓
⑤ 回写：焦点解析成功后写回 session_state（active_graph_refs + typed_graph_focus:{type}）
   ↓
⑥ 终结：对象随请求丢弃；信息沉淀到 session_state / checkpoint / 审计事件
   └── 下一轮 hydrate() 时，⑤ 写入的内容正是 ③ 的取水来源（闭环）
```
