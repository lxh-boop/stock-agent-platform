# 用户技能运行时（Skill Runtime）修改说明

更新时间：2026-09-23。本文描述本轮修改的**设计逻辑**：为什么这么改、链路怎么走、边界在哪里。文件清单以提交 `cd94f84` 为准。

## 1. 背景与问题

- 平台的 Agent 执行时**不存在任何 skill 机制**：旧 `skills/` 目录的 9 个 SKILL.md 是纯文档规格，运行时零引用；方法知识全部硬编码在各 Worker 的 prompt 字符串里，改一条口径就要改代码、走发版。
- 需求：让**最终用户**自己编写技能、或从 GitHub 导入技能，供本人会话使用。

## 2. 已锁定的需求口径

1. 技能**仅本人可见**，没有共享库、没有 admin 审批流。
2. GitHub 仅支持**公开仓库**；私有仓库 token 属敏感凭据，本期不碰。
3. 技能是**知识/约束文档**，`scripts/` 永不执行——skill 约束 Agent 使用既有工具，不新增工具能力。
4. 格式采用 **Agent Skills 开放标准**（SKILL.md + YAML frontmatter：name/description/version）。
5. 存储与**本地机器绑定**：不进 Git、不进镜像、不进交付包，换机需自行备份。

## 3. 总体链路

```text
用户(React /skills 页)
  → 在线编写 / zip 上传 / 公开 GitHub URL
  → POST /api/v1/web/skills/**（web_skills.py，require_user 身份）
  → SkillRegistry：校验 → 安全扫描 → 落盘 → 准入状态机
  → 文件系统权威：内置 skills/；个人 runtime/skills/<user_id>/<skill_name>/
  → Worker 执行时：admitted skill 正文注入 system prompt（W04/W05/W09）
  → 平台约束块拼接在 skill 内容之后
  → LLM_CALL 审计事件记录 applied_skills（name/version/content_hash）
```

## 4. 核心设计决策与理由

### 4.1 为什么文件系统权威，而不是 PostgreSQL

- 用户明确"技能和本地机器绑定"。`runtime/` 本就在"宿主机挂载、不进镜像/交付包"的口径内，api 容器整目录 bind 到 `/app`，开发和 compose 两种模式零新增挂载配置。
- 砍掉了原方案里的 `user_skills` 表和 migration；准入状态写在技能目录自己的 `skill.meta.json`，发现即目录扫描。
- **代价（已接受）**：PG 备份不含技能，换机/重装会丢，需用户自行备份 `runtime/skills/`。
- **审计仍留在 PG 侧链路**：`agent_decision_log`/`llm_audit` 继续记录 name + content_hash——文件事后被篡改，hash 对不上即被发现。

### 4.2 准入状态机：fail-closed 对齐 Phase 5

```text
创建/导入 → 安全扫描 → admitted（可注入）
                      → rejected（含密钥样式等，永不注入）
用户开关 admitted ⇄ disabled
```

- 规则：**rejected 永远优先于用户开关**。已 admit 的技能事后被改成含密钥内容，下一次扫描即 rejected，用户无法再启用。这与外部 MCP 的"无 allowlist 不启用、fail closed"是同一安全哲学。
- 结构性失败（缺 SKILL.md、frontmatter 非法）直接报错不落盘；扫描性失败（密钥样式）落盘为 rejected 并展示原因，让用户知道为什么被拒。

### 4.3 注入链路：三层顺序是刻意的

Worker system prompt 的最终结构：

```text
[Worker 自身方法约束]  ← 原有硬编码 prompt，保持权威
[技能正文]             ← admitted skill，按名称排序，单条 4000 / 总 12000 字符截断
[平台约束块]           ← 永远最后，声明冲突时以平台为准
```

- **约束后置**的理由：LLM 对后置内容权重更高；skill 是不可信输入（用户写的/社区来的），平台红线不能依赖它"自觉遵守"。
- **双防线**：prompt 层的约束后置只是第一层；真正的防线是输出侧的结构校验（schema/标准动作校验在服务端，schema 校验不过即无效）——prompt 被诱导也突破不了这层。
- **预算控制**：单技能 4000 字符、总计 12000 字符、每 Worker 最多 4 条，防止 prompt 膨胀拖垮 16GB 内存机器上的推理成本。
- **可选过滤**：frontmatter `apply_to_workers: [W04, ...]` 可声明只注入指定 Worker，缺省注入该用户全部 LLM Worker。
- 注入点选择 W04 风险 / W05 策略守卫 / W09 实体分析——它们是仅有的三个经 `generate_json_with_local_structural_repair` 出结构化结论的 LLM Worker，一处辅助函数 `append_skill_guidance` 各接一行。

### 4.4 用户隔离：目录即边界

- 存储布局 `runtime/skills/<user_id>/<skill_name>/`，`user_id` 与 `skill_name` 都过白名单正则（与 `server/api/security.py` 同一套收口思路），`../`、绝对路径在源头不可能成立。
- API 层所有读写以 `require_user` 的登录身份为准，查询永远带 owner 过滤——个人技能对他人在 API 视角不可见，不是"隐藏"而是"不存在"。
- 内置技能（`skills/`）对所有人只读可见，删除/启停操作直接拒绝。

### 4.5 zip 与 GitHub 导入的安全取舍

- **zip**：文件数 ≤20、总量 ≤1MB（防解压炸弹）；逐成员拒绝 `..`/`.` 段（zip-slip）；仅当全部成员共享同一顶层目录（GitHub 仓库 zip 的 `repo-branch/` 形态）才剥掉它，否则按原结构解压——先实现里"无条件剥第一层"会把 `../x` 静默改名为 `x`，已修正为直接拒绝。
- **GitHub**：解析 `github.com/{owner}/{repo}[/tree/{branch}/{subpath}]`，用 **codeload zip 接口**拉取——不引入 git 依赖、不拉全仓库历史；host 固定 codeload.github.com、超时 45s、包体上限 20MB。拉取失败降级为"手动下载 zip 后走上传通道"（前端文案已提示）。
- **来源追溯**：source_url + content_hash 持久化在 meta，页详情可见。

### 4.6 脚本为什么永不执行

用户 skill/GitHub skill 的 `scripts/` 属于**未经审查的第三方代码**，执行即供应链风险。本期技能定位是"约束/方法论"——`scripts/` 只被扫描记录清单（UI 标记"含脚本(不执行)"），不进入任何执行路径。若未来需要可执行技能，作为独立立项（sandbox + allowlist + 逐个审批），不搭本期便车。

### 4.7 审计：哈希绑定防漂移

`llm_audit` 新增 `applied_skills` 字段：每次 LLM 调用事件携带当次注入的 skill 清单（name/version/content_hash）。这与"任务绑定提交时 config_hash 防配置漂移"的既有口径一致——事后可以回答"这次结论当时受了哪些技能影响、内容是什么版本"。

## 5. 改动清单（逻辑模块视角）

| 模块 | 内容 |
|---|---|
| `agent/skill_runtime/` | 新增。models（数据模型+状态常量）、loader（解析/校验/扫描/解压）、registry（扫描/创建/安装/启停/删除）、injection（prompt 块构建+预算） |
| `agent/collaboration/workers/{risk,strategy_guard,entity_analysis}.py` | 各一行注入接线；prompt 文本本身未改 |
| `agent/llm_audit.py` | 新增 `note_applied_skills` 与事件字段 `applied_skills` |
| `server/api/routers/web_skills.py` + `schemas/skills.py` | 新增。7 个端点，错误按安全 reason 白名单透出，服务器路径不外泄 |
| `server/api/main.py` | 注册新路由进 authenticated 组 |
| `frontend/src/{types,api,pages/skills}` + 路由/菜单 | 技能管理页四件套 |
| `skills/compliance_risk_control/SKILL.md` | 首个内置技能：合规与风险边界检查 v1.0.0 |
| `tests/unit/test_skill_runtime.py`、`test_web_skills_api.py` | 23 项：用户隔离、rejected 优先、zip-slip、约束后置、审计 manifest |
| `docs/handoff/PROJECT_STATUS.md`、`.gitignore` | 状态与口径更新 |

## 6. 明确不做的事（本期边界）

- 不做共享库/admin 审批（技能仅本人可见）。
- 不做私有 GitHub 仓库（token 走 secret store 另议）。
- 不执行技能脚本。
- 不动 Tool DAG、executor、WriteGateway、数据库 schema、Phase 5 外部 MCP 口径。

## 7. 验证与已知存量问题

- 新增 23 项单测通过；连同认证/路由/ZAI 共 34 项通过；前端 `tsc --noEmit` 通过。
- **存量失败（非本次引入，已用 stash 在改动前代码复现）**：2026-09-23 上午清理导致 `test_llm_io_optimization_v183_safe` 收集失败（`agent.collaboration.agent_directory` 已删）、`test_local_llm_runtime` 等 11 项引用旧签名 `working_memory_context`。已记录于 `PROJECT_STATUS.md`，建议后续单独修复。
- 推送：本地 main 领先远端 2 个提交（`31baf5e`、`cd94f84`），等待凭据处理（当前 gh 账号 `lin0919603888-dev` 对 `lxh-boop/stock-agent-platform` 无推送权限）。
