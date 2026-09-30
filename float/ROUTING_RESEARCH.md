# 低成本动态 LLM 路由：公开资料核查

调研日期：2026-09-29（仅读取下列公开 GitHub 文件；未安装、未调用模型或 API）。

## 已核实的事实

| 项目 | 已实现的路由能力 | 路由本身是否会产生模型调用 | token / 费用记录方式 | 能否直接控制官方 Codex 桌面中正在运行的主任务 |
| --- | --- | --- | --- | --- |
| [RouteLLM](https://github.com/lm-sys/RouteLLM) | 在 OpenAI 客户端替代层或 OpenAI-compatible server 中，按 prompt 对强/弱模型的胜率和阈值二选一路由；`mf`、`sw_ranking`、BERT、causal LLM 等策略。 | **会，默认 `mf` 也会。**其源码对每个 prompt 调 OpenAI `text-embedding-3-small`；README 也明确说 `mf` 与 `sw_ranking` 需要 `OPENAI_API_KEY` 生成 embedding。BERT/causal LLM 是本地加载分类模型推理，但仍需模型权重/算力。随后还会向被选中的目标模型发出正常生成调用。 | README 关注阈值校准、性能/成本质量评估，所读公开文件没有提供对 Codex 本地会话 token 或订阅实际账单的采集功能。 | **不能由已读资料证明。**它只能处理接入其 Python client 或其 HTTP server 的请求；没有发现对 Codex 桌面运行中任务的控制接口。 |
| [vLLM Semantic Router](https://github.com/vllm-project/semantic-router) | 可编程 MoM 路由层；配置包含 signals、decisions、algorithms、模型卡及后端。公开样例有静态选择、用 helper model 的 `prompt` 决策，以及带最多两次升级、cost-aware 的 `automix`。 | **取决于配置。**静态规则可不调用辅助模型；`prompt` 样例明确以 `qwen3-8b` 选择候选，因而额外产生一次模型请求；`automix` 的候选执行/升级也涉及模型执行。 | 配置说明称 `providers.models` 持有部署 pricing metadata；未在所读文件中看到读取 Codex 本地 token 日志或结算官方 Codex 订阅费用的功能。 | **不能由已读资料证明。**它是需部署、由 listener 暴露的推理路由层；没有公开证据表明可附着或改写桌面任务。 |
| [LiteLLM](https://github.com/BerriAI/litellm) | Python SDK / Proxy Gateway 支持多模型；已读源码有按成本、延迟等选择，也有 `complexity_router`（SIMPLE/MEDIUM/COMPLEX/REASONING）与 adaptive bandit（请求类型、质量和归一化成本）。每个 tier 可映射不同模型与参数。 | **默认复杂度打分不消耗外部 API。**README 明示规则/关键词打分本地、零外部调用；改用 LLM/capability classifier 或 cache-aware token prediction 时会调用配置的分类器或 token-count 服务；最终仍会调用路由选中的目标模型。 | Router 的成功回调从响应 `usage.total_tokens` 记录 TPM/RPM；模型配置可含每 token 价格。README 还说明 Proxy 有 spend tracking。所读内容没有将此等同于官方 Codex 订阅账单。 | **不能由已读资料证明。**它要求应用将请求发向 LiteLLM SDK/Proxy；没有发现针对官方桌面当前主任务的控制路径。 |
| [ccusage](https://github.com/ryoppippi/ccusage) | 不是路由器；它按日、周、月、session 汇总多个 coding-agent CLI（含 Codex）使用量，包含模型拆分与 JSON 输出。 | **不调用模型。**它读取本机数据并解析事件。 | Codex adapter 搜索 `$CODEX_HOME/sessions` 和 `archived_sessions`，解析 JSONL 中的 `token_count` 事件（输入、缓存输入、输出、reasoning、总 token、模型等）。费用用 LiteLLM/models.dev/内置历史价格按事件时间估算；README 提供 `--offline` 与自定义 pricing override。估算值不应表述为官方订阅实际扣费。 | **不能。**它只读本地会话数据并生成报表，未见任务控制功能。 |

## 逐项依据（实际读取，均于 2026-09-29）

- RouteLLM：[`README.md`](https://raw.githubusercontent.com/lm-sys/RouteLLM/main/README.md)；[`routers.py`](https://raw.githubusercontent.com/lm-sys/RouteLLM/main/routellm/routers/routers.py)；[`matrix_factorization/model.py`](https://raw.githubusercontent.com/lm-sys/RouteLLM/main/routellm/routers/matrix_factorization/model.py)。
- vLLM Semantic Router：[`README.md`](https://raw.githubusercontent.com/vllm-project/semantic-router/main/README.md)；[`config/README.md`](https://raw.githubusercontent.com/vllm-project/semantic-router/main/config/README.md)；[`prompt.yaml`](https://raw.githubusercontent.com/vllm-project/semantic-router/main/config/fragments/algorithm/selection/prompt.yaml)；[`automix.yaml`](https://raw.githubusercontent.com/vllm-project/semantic-router/main/config/fragments/algorithm/selection/automix.yaml)。
- LiteLLM：[`README.md`](https://raw.githubusercontent.com/BerriAI/litellm/main/README.md)；[`complexity router README`](https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/router_strategy/complexity_router/README.md)；[`adaptive router README`](https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/router_strategy/adaptive_router/README.md)；[`lowest_cost.py`](https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/router_strategy/lowest_cost.py)。
- ccusage：[`apps/ccusage README`](https://raw.githubusercontent.com/ryoppippi/ccusage/main/apps/ccusage/README.md)；[`Codex loader`](https://raw.githubusercontent.com/ryoppippi/ccusage/main/rust/adapters/codex/src/loader.rs)；[`Codex paths`](https://raw.githubusercontent.com/ryoppippi/ccusage/main/rust/adapters/codex/src/paths.rs)；[`cost modes`](https://raw.githubusercontent.com/ryoppippi/ccusage/main/docs/guide/cost-modes.md)。

## 建议（基于上述事实）

1. 对“按任务难度/子阶段选模型”这一需求，LiteLLM 的复杂度分层最接近可复用的请求级模式；RouteLLM 是强弱二选一的研究型方案；Semantic Router 适合自建推理基础设施。三者都需要把**新发出的**调用接入各自的 client/proxy/listener，不能据此声称能接管当前官方 Codex 桌面主任务。
2. 对当前官方 Codex 工作流，先采用只读统计更稳妥：ccusage 能从本地会话记录按模型、时间段或 session 汇总 token，并给出基于价格表的参考费用。把它作为路由效果的观测器，不作为路由执行器。
3. 若要验证桌面任务可被外部路由器直接控制，需要另行找到并实测官方支持的控制 API 或设置入口；本次读取的四个项目均未提供该证据。
