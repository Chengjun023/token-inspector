# 可复现 router selector pilot

本框架使用 Python 3.9 标准库，默认离线。固定 `router-mini-v1.0.0` 的六个公开合成任务和独立确定性 grader；三组为 `fixed_strong`（`gpt-6-astra xhigh`）、`fixed_mid`（`gpt-6.1-sol high`）与当前 `router.select`。同一任务三组使用完全相同的 prompt、一次请求和验收标准；默认固定 seed 乱序、串行，总请求上限 18。首轮每任务每策略一次，共 18 个请求；它检验确定性 selector，不能证明完整多代理协作、重试或失败升级的收益。

默认 router 使用 `difficulty.assess(text=instruction, task_type='general', complexity='normal', risk='low')` 自动评分，再以 balanced、空历史 `history={}` 选择。任务的 `human_annotation` 只用于分析，不参与选模；benchmark 不写生产反馈。manifest 保存 prompt、任务版本、评分、选择、目录、价格、代码/数据 hash 和 seed。执行时只使用已冻结选择，不静默切换不支持的模型。

在插件根目录执行：

```sh
python3 benchmarks/benchmark.py demo --run-dir /tmp/router-demo-20260930
python3 benchmarks/benchmark.py report --run-dir /tmp/router-demo-20260930 --output /tmp/router-demo-report-20260930
```

`demo` 明确标记 `simulation`，使用参考成果和虚拟用量，只验证框架，不是模型效果证据。默认没有价格，费用为 unknown。

真实运行先保存实际 CLI 目录（该目录操作不调用模型），并冻结注册表。`registry.json` 是 `model_registry.freeze_snapshot` 生成的快照；也可使用已验证的 bundled snapshot。CLI 目录支持原始 `codex debug models` JSON，以及 `{ "models": { "slug": ["effort"] } }` 格式。

```sh
codex debug models > /tmp/router-catalog-20260930.json
python3 benchmarks/benchmark.py plan \
  --output /tmp/router-live-20260930 \
  --catalog /tmp/router-catalog-20260930.json \
  --registry-snapshot /tmp/router-registry-20260930.json \
  --prices /tmp/router-registry-20260930.json \
  --seed 20260930 --repetitions 1 --max-runs 18
python3 benchmarks/benchmark.py run --run-dir /tmp/router-live-20260930 \
  --live --executor codex --timeout 300 --max-runs 1
python3 benchmarks/benchmark.py run --run-dir /tmp/router-live-20260930 \
  --live --executor codex --timeout 300 --max-runs 18
python3 benchmarks/benchmark.py report --run-dir /tmp/router-live-20260930 \
  --output /tmp/router-live-report-20260930
```

先执行的一个 job 是这 18 次中的首个，不是额外 smoke 请求。`--max-runs` 表示同一 manifest 总 started 上限，完成结果会跳过；不自动重试失败。plan 与报告目标必须使用新目录，结果、快照、重验收结果均不覆盖。

`plan` 将评测 Python、公开 fixtures、选择器和注册表源码/数据复制到 `source-snapshot`，逐文件校验 hash；模型 workspace 只有公开 fixture，不包含答案和验收器。真实执行前校验 manifest、当前代码与快照，代码漂移会阻止新请求。报告和重验收会列出漂移。原源码以后改变时可使用当次源快照离线重放：

```sh
python3 /tmp/router-live-20260930/source-snapshot/benchmarks/benchmark.py replay \
  --run-dir /tmp/router-live-20260930 --output /tmp/router-regrade-20260930.json
```

`regrade`/`replay` 只重跑验收，不调用模型，不修改原始成绩；保存当次 grader hash、原 grader hash与代码漂移。旧路由规则完整保存在 `benchmarks/baselines/pre-router-20260930/`。要创建旧规则的独立对照计划，可加 `--router-dir benchmarks/baselines/pre-router-20260930`，其评分、规则及 hash 会单独冻结。Python 接口 `build_manifest(..., callback=callable)` 还可接 `(public_task, catalog) -> {model, reasoning_effort, ...}` 的 selector；正式回调应保存自身源码版本并在计划中冻结返回值。

每个 job 有独立 `workspace`、`artifacts`、`started.json` 和不可覆盖的 `result.json`。若中断留下 started 而没有 result，续跑停止。先确认没有仍在运行的自有 CLI 进程，才可移除过期 `execution.lock`；再明确标记未知中断：

```sh
python3 benchmarks/benchmark.py resolve-interrupted --run-dir /tmp/router-live-20260930 --job JOB_ID
```

该 job 将永久记为 `interrupted_unknown`，费用未知，仍计入完成分母；不会再次发请求。若要进行新实验，创建新 manifest，不能把不确定收费的请求无声重放。

CLI 适配器复用已有 Codex 登录，不读取 auth 或 API key；子进程移除 `OPENAI_API_KEY`/`CODEX_API_KEY`/`OPENAI_BASE_URL`，使用 `--ignore-user-config --ephemeral --skip-git-repo-check --json --output-schema --output-last-message -s read-only -C ... -m ...`。不使用危险 bypass、shell=True 或模型自动替换。prompt 要求仅输出结构化文件内容、不运行命令；出现命令、MCP、web、文件写入等工具事件则标记 protocol violation。默认不保存 raw stream、推理/item 正文或 stderr，仅保存结构化成果、事件类型及 completion 用量。

成果路径必须精确匹配任务输出白名单，拒绝绝对路径、目录穿越、重复项、缺失和过大成果。grader 在最小环境的独立有限时子进程运行，只执行合成成果，限制可导入模块、文件/网络/进程/动态反射能力；支持的系统施加 CPU、地址空间、文件和 fd 限制。这是受限合成程序验收器，不是对抗任意恶意 Python 的完整 OS 安全边界。

JSON/CSV/Markdown 报告提供验收率、首次/最终通过（one-shot 下相同）、耗时、input/cache/output/reasoning tokens、usage/price coverage、失败开销、费用/成功数。input 已含 cached，output 已含 reasoning，均不重复加计。不同 turn 的可识别 usage 汇总一次，重复 ID 去重；无 ID 的相同重复 usage 含糊时标未知。缺失、负数、无效、成功伪零 usage 与未知价格都保留 unknown；完整费用只在全部执行都有已知用量和价格时给出。费用/成功数包含失败开销。订阅 CLI 的 USD 是冻结的 requested-model 参考 token 价格乘实测用量，不是订阅账单；未声称取得服务端 actual-model 遥测。组间费用差不能直接称为逐任务节省。

```sh
python3 -m unittest discover -s tests -p test_benchmark.py -v
```
