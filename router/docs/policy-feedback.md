# 本地偏好、路由策略与验收反馈

Last updated: `2026-09-30T22:54:56+08:00`

策略版本：`1.0`。全部评分、排序和反馈汇总使用 Python 3.9 标准库；不调用模型。普通 CLI `propose` 可以按注册表 TTL 刷新公开元数据，冻结评测使用传入的本地快照。

## 偏好与兼容

默认仍为 `auto`、`balanced`；首次偏好问答是可选项，不阻断路由。两个问题分别询问优先目标和主要工作：

| 问答输入 `priority` | 本地 `profile` | 作用 |
| --- | --- | --- |
| `accuracy` | `quality` | 提高已验证工作类型能力的排序权重 |
| `balance` | `balanced` | 保留有界先验，少量考虑可比较价格与延迟 |
| `cost` | `economy` | 在合格候选内提高已验证价格的排序权重 |
| `latency` | `speed` | 在合格候选内提高已验证延迟的排序权重 |

`workload` 为 `read|write|code|research|mixed`。`mixed` 默认根据当前任务类型确定实际排序工作类型。`policy.preference_answers(priority, workload)` 返回配置更新字典；它不修改阶段 pins。

```sh
python3 scripts/router.py config --priority cost --workload write
python3 scripts/router.py config --profile quality --workload research
python3 scripts/router.py propose --phase act --plan-ready --task-type edit --difficulty 2.0 --thread-id THREAD_UUID
```

原来的 `mode=auto|ask|preset`、`preset=economy|balanced|quality|ultra` 和阶段显式覆盖均保留。`preset` 模式仍使用固定矩阵；旧 `preset` 在 `auto/ask` 下仍保留原有难度偏置。新 `profile` 控制自动候选排序。显式阶段 pin 优先于偏好、历史计数和安全升级建议，执行前仍须满足目录、宿主 effort 和声明的工具/上下文能力约束。

`ask` 的推荐在 `propose` 时冻结。后续 `recommended` 或超时仅验证该组合仍可用，不根据新反馈或新目录重新选模。选择自定义模型或固定预设属于新的用户选择。定时器仍仅在问题已展示并执行 `arm` 后开始；时钟跳变重新等待问题展示。

## 评分与候选约束

`difficulty.assess()` 先提取动作，再提取局部范围、证明义务、反例检查及保留其他内容的约束。引号与代码跨度中的词是修改对象，不作为动作信号。只把 README 中的“架构”替换为“设计”会得到局部 edit；证明定理并找反例会得到较高难度 reason。显式 `task_type` 和合法的 `score` 优先。

`confidence` 保留旧分类标签，并明确 `confidence_calibrated=false`；它不是概率。代码不读取 benchmark ID、答案或评分结果。

自动候选同时满足：

1. 目录中实际公布该 endpoint/effort。
2. 原生宿主工具基线或证据准入的宿主扩展允许该 endpoint/effort。
3. 注册表模型已验证或已准入，能力已验证，effort 有交集。
4. 新候选达到该工作块的最低能力 tier；轻型新模型不能靠低价进入普通或前沿任务。
5. 高风险、连续两次失败和高难度推理门禁使用 Astra，或明确验证的同等安全能力及所需 effort。
6. 声明的上下文与工具能力足够。未知能力不算满足约束。

`--available-models` 只能缩小宿主集合，不能把目录中任意新模型变成宿主可用模型。新型号先保留 `candidate`，经注册表能力/endpoint 准入和独立宿主证据准入后，无需改路由代码即可参与排序。

```sh
python3 scripts/router.py propose --phase think --task-type code --difficulty 6.0 --require-tools --thread-id THREAD_UUID
python3 scripts/router.py propose --phase act --plan-ready --context-tokens 120000 --thread-id THREAD_UUID
```

`--require-tools` 要求已验证的模型工具调用能力。Python API 的命名工具约束还须传入实际宿主 `available_tools`；注册表的布尔工具能力不会被解释成拥有某个连接器。种子注册表上下文窗口为未知时，非零上下文要求会拒绝路由，等待能力证据。

排序保留现有候选顺序作为明确未校准先验。仅已验证的工作类型能力、价格和单独验证的延迟可改变相关排名；缺失值保持未知。价格比较使用每百万输入加输出的公开 USD 参考费率，不预测当前任务账单。排序分数不是成功率。每次决策记录策略版本、候选及理由、注册表 hash、宿主 hash、反馈 hash。

## 执行与验收分别记录

`mark` 仅记录实际派发。兼容旧 `--evidence`；新接口可明确 `agent_id` 和 `turn_id`，至少提供一项执行证据。结构化 ID 为 1–200 个 ASCII 字符，允许 `[A-Za-z0-9_.:/-]`，包括 UUID 与 `/root/worker` 等任务路径；不接受任务原文或推理。

```sh
python3 scripts/router.py mark --id DECISION_UUID --thread-id THREAD_UUID --agent-id /root/worker --turn-id turn-123
python3 scripts/router.py mark --id DECISION_UUID --thread-id THREAD_UUID --evidence agent:123
python3 scripts/router.py outcome --id DECISION_UUID --thread-id THREAD_UUID --status accepted --verification-source automated_tests --actual-model gpt-6-luna --actual-effort medium --evidence-id tests:run123 --usage-json '{"input_tokens":100,"output_tokens":20}' --cost-usd 0.0001 --latency-ms 800
python3 scripts/router.py feedback --id DECISION_UUID --thread-id THREAD_UUID --status rejected --failure quality --verification-source user --actual-model gpt-6-luna --actual-effort medium --evidence-id turn:review123
```

`outcome` 和 `feedback` 是同一个接口，按决策 ID 记录一次不可变验收：

- `accepted` 必须有 `user|automated_tests|reviewer` 验证来源且没有失败分类。
- `rejected` 必须有失败分类：`quality|tool|availability|timeout|cost|latency|cancelled|unknown`。
- `unknown` 表示无法判断，不能断言质量通过。未派发决策不能记录验收。

自动结束、派发成功、live 设置应用成功及用户沉默均不构成验收。尚待验收时 `outcome=null`，界面摘要显示 `acceptance=unknown`。不要为了结束工作自动写 `accepted`。已写的 `unknown` 也是不可变终态；等待后续用户验收时应保留 `null`。

相同反馈重试幂等，不重复计数；同一 ID 的不同反馈拒绝。使用计数仅接受非负整数的 `input_tokens/cached_input_tokens/output_tokens/reasoning_tokens/total_tokens`；费用与延迟仅接受有限非负数。记录实际模型与 effort，缺失时不归因给所选模型，不参与学习。来源名称及证据 ID用于追溯声明，不替代实际测试或用户判断。

## 匿名汇总与评测隔离

学习 cohort 由任务类型、实际阶段、工作类型和难度档组成，不存任务原文、模型推理或输出。每个模型/effort 至少有 5 次明确 accepted/rejected 后，局部计数才会形成有界排序修正；unknown 不满足该门槛。计数仍不代表校准成功率。用户 pins 始终保持。

升级前缺少cohort的旧决策可以记录验收与幂等收据，但标明`legacy_decision_without_cohort`并不进入学习，不推断旧任务分组。

七天清理只清理详细决策；匿名 `route_stats` 和幂等 `outcome_receipts` 保留。收据只保存已校验的结构化字段、决策/任务 ID及时间，以便清理后继续拒绝冲突重试。

`--scope benchmark` 的反馈完全不进入正式聚合。`propose --no-learning` 使用空历史并禁止该决策结果学习。冻结评测建议调用：

```python
host_snapshot = router.host_capability_snapshot()
route = router.select(config, phase, complexity, risk, plan_ready, failures,
                      advertised_models, rating, available_models,
                      snapshot=frozen_registry, profile="balanced", history={},
                      host_snapshot=host_snapshot)
```

`select` 保留原有位置参数；新增参数均为 keyword-only。它不读取反馈数据库；显式 registry/history/host 快照下不会刷新注册表或读取后续宿主准入。`Store.propose` 显式载入正式聚合；库调用默认离线，普通 CLI 可按 TTL 刷新元数据。评测保存代码与策略快照 hash，执行阶段复用冻结决策，不再选择。

## 目录波动与界面摘要

CLI 目录记录内容 hash 和观察时间。型号暂时消失时，目录为 `reduced`，明确列出 `removed_since_previous`，只按当前交集回退；不把上次型号偷偷并回当前目录。整个目录读取失败时最多使用 24 小时内的缓存，明确 `stale/fallback`，不扩大宿主权限。缓存过期则拒绝选择。

`get` 输出保留旧字段，并提供 `summary`：`decision_id/phase/model/effort/reason/profile/policy_version/status/execution/acceptance/agent_id/turn_id/catalog_status`。完整决策另有顶层 `agent_id`、`turn_id`、`catalog`、`registry`、`host`、`outcome` 和 `route.policy`。Float 可只读 SQLite 的决策 JSON，展示原因、偏好、目录状态、执行及验收摘要。

## 验证

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

新增行为测试覆盖动作与引号对象、显式覆盖、价格/延迟验证、最低 tier、模型与宿主准入、三重 effort 交集、上下文/工具未知、反馈校验与幂等、任务/cohort 隔离、评测隔离、问答决策冻结、目录变动和清理后汇总保留。
