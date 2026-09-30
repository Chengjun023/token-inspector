---
name: adaptive-route
description: Route a Codex task's analysis, implementation and review to different models and reasoning efforts to control token use. Use when the user requests model routing, deep thinking with lighter execution, asks for automatic/preset/ask-with-timeout routing, or has enabled adaptive routing for the current task.
---

# 按工作难度路由

Last updated: `2026-09-30T22:54:56+08:00`

首次使用说明：按工作块难度选择实际模型；默认自动＋均衡。主任务当前正在生成的请求不能中途换模。安装插件不会自动修改所有任务的模型。

路径以本 SKILL.md 所在目录为基准：`../../scripts/router.py`（以下记作 ROUTER，执行时替换成绝对路径并正确引用）。仅需要 Python 3 和已登录的 Codex CLI。`CODEX_THREAD_ID` 自动绑定当前任务；缺失时从已有任务工具取得确切 ID 后传 `--thread-id`，不能猜测或取最近任务。

## 开始与偏好

1. 告知用户正在使用本技能。运行 `python3 ROUTER config` 读取偏好。用户明确给出的模式、模型和档位优先。
2. 默认 `auto / balanced / 60 秒`。持久设置用 `config --mode auto|ask|preset --preset economy|balanced|quality|ultra --timeout 60`。仅本次变化用 `propose` 的同名参数，不修改全局偏好。
3. 自定义阶段：`config --phase think|act|review --model gpt-6-astra --effort max`；重置阶段覆盖用 `config --clear-overrides`。中文“极高/最高/中/高”分别映射 `xhigh/max/medium/high`。只改变用户要求的字段。
4. 修改偏好本身不需要启动模型。已有任务明确指定模型时，先保持该选择；不要以省 token 为由静默替换。模型/档位不支持会返回错误，告知并提出目录中存在的替代项。

## 可更新目录与个人偏好

首次配置或用户希望调整时，最多问两项：更看重质量/均衡/费用/速度，以及主要做阅读/写作/代码/研究/混合。已有明确选择直接采用；没有答案继续沿用现有均衡设置。用 `config --profile quality|balanced|economy|speed --workload read|write|code|research|mixed` 保存；具体模型 pin 始终优先。

正常 `propose` 按 TTL 尝试刷新公开目录；本地评分、选模和计量没有额外模型调用。目录把供应商型号、价格、能力证据与实际执行接口分开；新型号先进入候选，只有本地准入且宿主证明确实支持时才进入排序。不要把公开目录的 Opus/Sonnet 名字当作当前 Codex 子代理可调用。价格采用与准入命令见 [模型目录说明](../../docs/model-registry.md)。公开目录失败时保留已验证快照并报告 stale，不自动注册付费供应商。

下列模型梯度是**未校准初始顺序**。通过能力门槛的候选还会按偏好、已验证价格/能力/延迟及同类任务的验收记录排序。没有足够验收样本时保留初始顺序；分数和排序分都不是成功概率。详见 [偏好与验收](../../docs/policy-feedback.md)。

## 工作块评分与选择

工作块开始先按任务类型、复杂度、风险和失败证据给 1.0–10.0 分，每次只评估可验收的一块。`scripts/difficulty.py` 的 `assess(text='', phase='act', complexity='normal', risk='low', failures=0, score=None, task_type='general', source='heuristic')` 是纯标准库规则，不触发额外模型请求。含糊或高风险工作可由主任务根据本轮已有证据给 `--difficulty` 分数，`--score-source caller_assessment`；无须为评分再派模型。返回的 `confidence` 仅是 `low`/`medium` 定性标签，未经概率校准；调用者给分也不是校准概率。文本只在内存用于摘要特征，路由记录不保存原文。

- `think` 用于架构和诊断；读取、摘录可用较低分数的模型完成分析。`act` 用于落实方案；`review` 只检查确有需要的结果。这三个阶段都按分数选择，不固定使用 Astra。
- 分数 1–1.9 选 `gpt-6-luna` low，2–2.9 为 medium；3–4.4 的快速阅读与摘录选 `gpt-5.6-terra` medium，其他轻任务选 Luna medium；4.5–5.9 选 `gpt-6.1-sol` medium，6–6.9 为 high；7–8.4 的 code/edit/read/extract/general 选 6.1 Sol xhigh，architecture/reason/debug 选 `gpt-6-astra` xhigh；8.5–9.4 统一选 Astra max，9.5–10 为 Astra ultra。`think` 阶段本身不强制 Astra。
- 轻任务的 Luna/Terra 都不可用时先回退 6.1 Sol medium，再尝试旧 Sol 与 Astra。普通 Sol 档首选不可用时依次尝试 `gpt-6-sol` 同档（xhigh 不支持则 high）、`gpt-5.6-sol` 同档或 high、Astra xhigh，理由必须列出首选与实际回退。要求 Astra 的架构/推理/深度诊断、高风险、连续失败及最高难度工作块，要求 Astra 或有明确安全等价证据的已准入替代项；没有合格组合就报错。
- 默认阅读和摘录分别为 3.2、3.4 分，快速浏览通常用 Terra medium；简单单行抽取可降至 Luna。执行方案未明确的非读取工作转 `think`；读取即使没有 `--plan-ready` 也可分析。
- 首次失败按新证据评分；两次失败转 `think`，选模分数至少 7.0，并要求 Astra xhigh 或更高档重新分析，避免连续 Sol 重试。高风险自动选择也至少 7.0，且真实首选 Astra。原始难度保留在 `difficulty.score`，实际选模分数写入 `selection_score`。用户显式阶段覆盖仍优先，且必须通过能力验证；高风险执行仍标记 `requires_strong_review`。
- `preset` 保留固定矩阵的原角色分工，未被用户覆盖的 Sol 默认升级 `gpt-6.1-sol`；已有显式 `gpt-6-sol` 阶段覆盖保持原样。`auto` 与 `ask` 在原始分数上应用 economy -0.3、balanced 0、quality +0.4、ultra +0.8 的有界偏移，再应用安全下限。用户指定的模型与档位优先于自动选择。评分不能代替业务操作授权。

每个有意义的阶段调用一次，例如：

```sh
python3 ROUTER propose --phase act --task-type code --complexity normal --risk low --plan-ready
python3 ROUTER propose --phase think --task-type architecture --difficulty 8.2 --score-source caller_assessment
```

2026-09-30 官方 [GPT-6.1 Sol 模型说明](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 将其定位于复杂编码、computer use 与专业工作，能力接近 Astra；因此普通复杂工作优先 6.1 Sol，架构与难题分析保留 Astra。API 文档列出的推理档位为 low–max；本机 Codex 目录和当前宿主 spawn 另支持 ultra。以当前执行环境的实际能力交集为准，不将 Codex 的 ultra 推断为 API 支持。

可选 `--task` 供本地规则识别类型，`--available-models` 传逗号分隔的宿主可用模型以缩小候选范围。最终仍需同时通过本机目录和宿主内置 allowlist；当前宿主不支持 `gpt-5.6-luna` 子代理，因此不会输出它的可执行 spawn 配置。返回值包含难度摘要、具体 model/effort、理由和 `native_spawn_settings`。`selected` 只是选择完成，`execution=not_started` 表示尚未派发。

## 询问与超时

`ask` 返回 `awaiting_question` 时：

1. 用可用的异步提问工具呈现具体推荐的模型/档位、备选预设和“暂不路由”。说明只询问模型偏好，60 秒（或配置值）无回复采用推荐。没有异步工具时，明确说明无法显示非阻塞问题，用相同参数重新 `propose --mode auto`，不伪造用户已看到问题或已作选择。
2. 问题成功展示后才运行 `arm --id ID` 开始计时。然后可做与选择无关的轻量工作。等待采用 clock sleep 或可被新输入打断的等待，每次最多 30 秒；不使用阻塞的终端 sleep。
3. 用户回复后 `resolve --id ID --choice recommended|economy|balanced|quality|ultra|cancel`；自定义用 `--choice custom --model MODEL --effort EFFORT`。给用户的固定预设选项应注明它会替换本次自定义阶段覆盖。
4. 未回复时运行 `resolve --id ID`。只在返回 `selected` 后执行；`pending` 时继续等待。`awaiting_question` 表示计时尚未开始或时钟变化，重新展示问题后 arm。新问题会使同任务的旧未决路由失效，跨任务状态独立。
5. 取消后继续以当前模型完成已授权工作，不再为同一阶段追问。迟到的用户选择用于下一工作块；不自动打断已运行代理或重复派发。

超时仅为事先说明的模型偏好兜底，不是删除、发布、外发、付款、扩大权限或其他实质操作的授权。若工具问题本身涉及必要授权，保持待定，不能用本技能超时绕过。

## 实际执行：原生子代理

本技能明确请求在适合分工的独立工作块使用子代理路由。选择完成后，仅在确有独立工作且宿主允许时用 `collaboration.spawn_agent`（或宿主等价工具）派发。主任务同时承担独立的准备或验收工作。没有这种分工空间就由主任务完成，并准确说明没有切换模型。

- 传返回的 `model`、`reasoning_effort` 和 `fork_turns="none"`，避免复制整段历史。先确认宿主 spawn 工具支持此组合；本机 CLI 目录不等于所有远端/子代理均可用。
- `message` 包含目标、文件绝对路径、最小必要上下文、既有变更、允许改动范围、验收条件、资源限制。对新 worker 直接说明“执行此任务，不再套用 adaptive-route 或递归委派”；需要改变方案时返回主任务。
- 默认同一写入范围最多一个执行代理。主任务不重复做代理的同一工作，不向低档代理倾倒全量资料，不为了复核把全部上下文再复制一遍。复用空闲代理只能用于相同已确认模型/档位；需换模型时创建新的有界工作块。
- 只有 spawn 成功返回 agent ID 后，运行 `mark --id ID --agent-id <实际ID> --evidence 'agent:<实际ID>'`。确切轮次 ID 可用时追加 `--turn-id`；不可用就省略，不能猜测。失败则报告路由未执行，维持当前模型；不通过独立 CLI 绕过宿主委派限制。
- 等待结果并核验实际产物。若代理的实际模型信息与选择不一致，按实际结果说明。升级只针对有证据的难点，默认两次失败即停止低档试错。
- 结尾简述哪些阶段实际使用了哪些模型；没有使用量对照时不编造节省百分比。低价模型降低成本不必然降低总 token；重复上下文、返工和委派也计入开销。

## 验收与重复评测

代理返回后检查交付物，使用 `outcome --id ID --status accepted|rejected|unknown` 记录。accepted 必须有用户确认、自动测试或独立 reviewer 证据，提供 `--verification-source user|automated_tests|reviewer --evidence-id <简短证据标识>`；同时按实际证据填写 `--actual-model` / `--actual-effort`。进程完成、代理自称完成都不足以标记 accepted。本次验收无法判断时记 unknown；仍在等待测试或用户验收时保持未验收 null，避免过早写入不可变终态。失败类别、使用量与耗时参数见偏好文档；不把正文写入证据字段。

benchmark 使用独立 `scope=benchmark`，不学习到日常偏好。可重跑的六任务套件支持冻结清单、串行执行、断点恢复、离线重新评分和 JSON/CSV 报告；见 [benchmark 使用说明](../../benchmarks/README.md)。真实运行需要用户给定次数预算，不能静默重试不确定是否计费的请求。相同用量下的参考价格差与同任务实测 token 差分别报告。

## 实验性同轮切换

用户要求直接切换当前主任务且本机已有 app-server control socket 时，读取 [live-routing.md](references/live-routing.md)。这条路径只发布当前轮后续步骤的 model/effort；不能把接口的 `applied` 当作已生成 token 的模型证明。连接不可用时使用上面的原生路由，不启动新 daemon，不改全局配置，不重启桌面，不逆向连接私人 IPC。

更多用法、预设表与已验证边界见插件根目录 `README.md`。
