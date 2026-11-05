# ADR 0003：接入外部文件系统 MCP——人工策展的研究文档目录

- 状态：已接受（2026-11）
- 前置：ADR 0002（删除内部 MCP 运输层；评估后拒绝"消费外部 MCP"）

## 背景

ADR 0002 拒绝了联网搜索/实时行情类的外部 MCP，理由是它们会把**未审核的外部内容**
灌进按"策划 RAG 管道 + T+1 日线"设计的系统。但存在一类证据来源与该边界兼容：
**人工策展的本地文档**——用户自己把研报、公告 PDF、分析笔记放进一个专用目录。
这类内容的审核者是人，不是爬虫；它是现有"爬取→入库→RAG"管道之外最自然的
补充证据来源，且无需为此新建入库管线。

## 决策

- 通过 `langchain-mcp-adapters`（官方 LangChain MCP 客户端）消费
  **官方 filesystem MCP server**（`npx @modelcontextprotocol/server-filesystem`，
  stdio，只读），目录默认 `data/research`，可用 `STOCK_RESEARCH_DOCS_DIR` 覆盖。
- 工具经 `tool_name_prefix=True` 以 `research_docs_` 前缀进入主 Agent，
  并按**只读允许清单**过滤（list_directory / read_file / read_multiple_files /
  search_files / directory_tree / get_file_info），写入类工具一律丢弃。
- 加载失败（目录不存在/为空、Node/npx 缺失、server 启动失败）一律**优雅降级**：
  返回空工具列表并在 run 警告里说明，绝不阻塞运行。
- system prompt 增加"research_docs_* 读取人工策展资料，引用时注明来源"的纪律。
- API 镜像加入 Node.js/npm（npx 运行 server 所需）。

## 理由（真实权衡）

- **为什么 filesystem 而不是搜索/行情**：本系统的证据纪律要求内容可追溯、
  未经审核的外部内容不得进入分析链（用户在评审中明确拒绝联网方向）。
  人工策展目录保留了这条边界的语义——"进入目录"本身就是审核动作。
- **为什么消费官方 server 而不是自建**：ADR 0002 删除的是"自己包自己"的
  内向运输层；消费**外部**标准 server 时，官方 server + 官方客户端是
  零自研的组合。自建 MCP server（如实时行情）仍属远期选项，未做。

## 后果

- 正面：人工策展的研报/文档成为一等证据源；接入新的 MCP server = 改配置，
  不改引擎；加载路径有测试保护（含伪造客户端的允许清单/前缀测试）。
- 负面/边界：
  - API 镜像新增 Node.js/npm（镜像体积 +~60MB）。
  - `npx -y` 首次运行需联网拉取 server 包；离线环境应预装该 npm 包。
  - 工具调用时 spawn stdio 子进程，单次调用有百毫秒级开销（读文档场景可接受）。
  - 目录语义是"人工策展"——没有任何机制阻止放入垃圾文件，审核责任在使用者。
