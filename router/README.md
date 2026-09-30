# Adaptive Router · 智能模型路由

Last updated: `2026-09-30T22:54:56+08:00`

个人 Codex 插件：把分析、执行与复核分配给不同模型，支持自动、询问超时和固定预设。默认 **自动＋均衡**。

安装后在新任务中输入：

> 用 $adaptive-route 完成这个任务：……

也可以说：

- “用模型路由，思考用 Astra max，执行用 Terra medium。”
- “路由改为先问我，60 秒不回复就自动选。”
- “用省额度预设执行这个任务。”
- “本任务固定思考 ultra，执行 Luna high。”

## 0.3.0：可更新、可验收、可复跑

- 模型目录独立于策略代码，包含来源、抓取时间、能力、参考价格和内容 hash。正常路由按 TTL 更新公开候选目录，网络失败保留 last-good；新型号通过本地准入和宿主能力检查后参与排序，无须修改源码或 git push。其他供应商可登记，实际执行仍取决于可用接口。
- 两项偏好控制质量/均衡/费用/速度与阅读/写作/代码/研究/混合。具体模型 pin 优先；下表是未校准初始顺序，合格候选还会按可信价格、能力、延迟与本地验收记录排序。
- accepted/rejected/unknown 验收与派发状态分开。生产同类样本达到门槛后才做有界调整，benchmark 数据独立，不把进程正常退出当作质量通过。
- 六任务 benchmark 固定提示、材料、验收器、目录、策略代码、种子与价格快照。支持串行 18 次 pilot、断点恢复、离线 regrade、JSON/CSV 报告；模拟与真实结果分开保存。
- Float 0.5.0 读取同一目录，保存只含数字的用量账本，显示本轮/累计与路由选择依据；旧请求保持首次计量价格。

实现与命令：[模型目录](docs/model-registry.md) · [偏好与验收](docs/policy-feedback.md) · [可复现 benchmark](benchmarks/README.md)。首轮真实试跑18/18通过：[结果与边界](benchmarks/results/pilot-public/REPRODUCE.md) · [离线重验收与再次试跑](benchmarks/results/pilot-public/REPRODUCE.md)。三组均6/6通过，动态参考费用$0.1622、固定Astra$1.2375、固定6.1 Sol$0.1921；六个合成任务只支持pilot范围结论。

```sh
python3 scripts/router.py config --profile balanced --workload mixed
python3 scripts/router.py config --priority cost --workload code
python3 scripts/model_registry.py show
```

## 路由模式

| 模式 | 行为 |
| --- | --- |
| 自动 `auto` | 每个工作块按任务类型、难度分数、风险和失败证据选择 |
| 询问 `ask` | 展示具体选择；问题显示后才计时，默认 60 秒未回复采用推荐 |
| 预设 `preset` | 固定各阶段模型组合，可单独覆盖每个阶段 |

| 预设 | 分析 | 执行 | 复核 |
| --- | --- | --- | --- |
| 省额度 `economy` | Astra xhigh | Luna medium | 6.1 Sol high |
| 均衡 `balanced` | Astra xhigh | Terra medium | Astra xhigh |
| 质量 `quality` | Astra max | 6.1 Sol high | Astra max |
| 深度 `ultra` | Astra ultra | 6.1 Sol high | Astra max |

这里 Astra/6.1 Sol/Luna 分别是 `gpt-6-astra`、`gpt-6.1-sol`、`gpt-6-luna`；Terra 是 `gpt-5.6-terra`。固定预设仅把原 Sol 默认升级 6.1 Sol，其余角色分工保持；用户显式指定的 `gpt-6-sol` 阶段覆盖继续生效。每次路由读取本机模型目录，并与宿主子代理 allowlist 取交集；不能使用的组合明确报错。极高=xhigh，最高=max；ultra 是单独档位。

自动与询问模式按 1.0–10.0 难度评分：

| 选模分数 | 工作类型 | 首选组合 |
| --- | --- | --- |
| 1–1.9 / 2–2.9 | 简单工作 | Luna low / medium |
| 3–4.4 | 阅读、摘录 / 其他轻任务 | Terra medium / Luna medium |
| 4.5–5.9 / 6–6.9 | 常规工作 | 6.1 Sol medium / high |
| 7–8.4 | code、edit、read、extract、general | 6.1 Sol xhigh |
| 7–8.4 | architecture、reason、debug | Astra xhigh |
| 8.5–9.4 / 9.5–10 | 所有类型 | Astra max / ultra |

默认阅读/摘录分别为 3.2/3.4 分，简单单行抽取可降到 Luna。阶段名不决定模型：普通复杂工作即使处于 think，也可用 6.1 Sol；架构与深度诊断在相应难度下保留 Astra。评分由纯标准库的 `scripts/difficulty.py` 完成，主任务也可用已有证据直接给分，不产生额外模型请求。`confidence` 是 low/medium 定性标签，未经概率校准。

auto/ask 的预设偏好对原始分数应用 economy -0.3、balanced 0、quality +0.4、ultra +0.8 的有界偏移。连续失败两次转 think，选模分数至少 7.0，并要求 Astra xhigh 或更高档重新分析；高风险自动选择也至少 7.0，真实首选 Astra。`difficulty.score` 保留原始分数，`route.selection_score` 显示实际选模分数。用户显式阶段覆盖优先，仍须通过能力验证；高风险执行保留强复核标记。没有执行方案的读取工作仍可用轻量模型分析。

轻任务的 Luna/Terra 都不可用时先回退 6.1 Sol medium，再尝试旧 Sol 与 Astra。普通 Sol 档缺少首选组合时，依次尝试 `gpt-6-sol` 同档（xhigh 不支持则 high）、`gpt-5.6-sol` 同档或 high、Astra xhigh，并列出首选和实际回退。要求 Astra 的架构/推理/深度诊断、高风险、连续失败或最高难度工作，Astra 或所需档位不可用就明确报错，不静默降为 Sol。固定预设与显式覆盖的不可用组合也报错。

更新依据为 2026-09-30 读取的官方 [GPT-6.1 Sol 模型文档](https://developers.openai.com/api/docs/models/gpt-6.1-sol)：6.1 Sol 面向复杂编码、computer use 与专业工作，能力接近 Astra。官方 API 单价为每百万 token 普通输入 $2、缓存输入 $0.10、输出 $10；相较 GPT-6 Sol，普通输入/输出同价、缓存输入半价，因此默认升级不代表所有请求都会省钱。API 文档支持 low–max；本机 `codex debug models` 和当前宿主 spawn 支持 low、medium、high、xhigh、max、ultra。脚本按 Codex 实际能力取交集，不把 Codex 的 ultra 推断为 API 支持。

## 实际如何生效

当前桌面可用路径是 **原生子代理**：主任务提供简短计划和必要材料，按选中的模型/档位派发一个可验收的独立工作块，再检查结果。主任务自身的当前生成不能中途换模。没有独立分工空间时由主任务完成，不虚构子代理。

插件不会接管每个 Codex 请求，也不会因安装就修改所有任务。通过 `$adaptive-route` 启用当前任务的路由；语义匹配时技能也可被自动发现。模式里的“自动”指启用后的阶段选择。

另有实验性同轮设置脚本 `scripts/live.py`：通过已有 app-server 的 `turn/settings/update` 发布当前轮后续步骤的 model/effort。它不会重启桌面或自动启动 daemon。当前电脑未开放默认 control socket，因此此路径仅完成模拟验证，不能视为已经实现桌面主任务实时切换。

## 本地设置

脚本只需要 Python 3 标准库与已登录的 `codex` CLI，不需要 API key、付费代理服务或额外常驻进程。

```sh
python3 router/scripts/router.py config
python3 router/scripts/router.py config --mode ask --timeout 60
python3 router/scripts/router.py config --mode preset --preset economy
python3 router/scripts/router.py config --phase think --model gpt-6-astra --effort max
python3 router/scripts/router.py config --phase act --model gpt-5.6-terra --effort high
python3 router/scripts/router.py config --mode auto --preset balanced --clear-overrides
python3 router/scripts/router.py propose --phase think --task-type read --thread-id UUID
python3 router/scripts/router.py propose --phase think --task-type architecture --difficulty 8.2 --score-source caller_assessment --thread-id UUID
```

`propose` 支持 `--task-type read|extract|edit|code|debug|architecture|reason|general`、`--task`、`--difficulty`、`--score-source` 和 `--available-models`。显式分数必须在 1.0–10.0 范围且精确到 0.1；`assess(text=title)` 可供其他本地工具复用。当前宿主子代理不支持 `gpt-5.6-luna`，即使本机 CLI 目录列出它，也不会生成可执行的 spawn 配置。

偏好和路由元数据位于 `~/.codex/adaptive-router/state.sqlite3`，按任务 ID 隔离。只保留阶段、配置、难度摘要特征、决策 ID、时间、派发与结构化验收结果，不保存 `--task` 原文、文件内容或隐藏推理。决策最多保留七天（下次创建决策时清理）；偏好和匿名聚合验收计数长期保留。`ADAPTIVE_ROUTER_HOME` 可指定独立状态目录。

询问计时由当前活动任务驱动；任务结束、关闭或被取消后不会有后台程序自行执行。超时只能决定模型偏好，不能代替实际业务操作的授权。派发前取消会保留当前模型继续已授权任务；迟到选择用于下一块工作。

## 验证与范围

```sh
python3 -m unittest discover -s router/tests -v
python3 router/scripts/router.py models
python3 router/scripts/live.py probe
```

原有回归与新增测试覆盖各阈值、类型与阶段分离、高风险/失败时要求 Astra、原始与选模分数分离、6.1 Sol 与旧 Sol 回退、宿主/目录/档位交集、用户覆盖和持久旧 Sol pin、固定预设、ask 超时采用 6.1 Sol、任务原文不入库，以及实验接口的准确目标/未知写入处理。真实 pilot 按相同 one-shot 工作流比较固定 Astra、固定 6.1 Sol 与本地自动选择；多代理协作与失败升级效果需另设实验。没有承诺固定节省比例：轻量模型、较低推理档位与精简上下文有利于控制开销，但重复上下文和返工可能抵消收益。

官方依据：[Subagents](https://learn.chatgpt.com/docs/subagents) · [App Server](https://learn.chatgpt.com/docs/app-server)。实验性 `turn/settings/update` 依据本机 CLI `0.155.0-alpha.9.2` 生成的协议 schema，公开文档尚未说明该方法。

## 从公开仓库安装

在仓库根目录运行：

```sh
codex plugin marketplace add .
codex plugin add adaptive-router@token-inspector
```

卸载：`codex plugin remove adaptive-router@token-inspector`。插件安装后在新任务显式调用 `$adaptive-route`；安装本身不会修改所有任务的模型。
