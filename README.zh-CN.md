# Agent Smith · 史密斯专员

**Token 超支？快给史密斯打电话！**<br>
*最贵的模型，不必每次都出庭。*

![Better Call Saul 风格的恶搞广告：GPT 花形标志剪贴到西装律师头上](assets/agent-smith-ad.jpg)

[English](README.md) · [架构](docs/architecture.md) · [工程说明](docs/engineering.md) · [路线图](docs/roadmap.md)

还在让最贵的模型帮你改标点？史密斯专员受理此案。

Agent Smith 是 Chengjun 做的 Codex 配套工具：按工作块选择模型，用原生悬浮窗盯住用量，再通过可复现 benchmark 核对结果。简单活儿交给轻量模型，难题请强模型出庭。

**你的 token 有权保持沉默。每一笔用量，都得交代去向。**

仓库包含两个组件：

| 组件 | 当前版本 | 职责 |
| --- | --- | --- |
| [Adaptive Router](router/README.md) | 0.3.0 | 本地选模、个人偏好、有证据的验收与可复现评测 |
| [Codex Float](float/README.md) | 0.5.0 | 原生 macOS 悬浮窗，展示本机 Codex 任务、子代理、用量与参考费用 |

这是独立个人项目。Logo 头律师广告由 AI 生成，采用 Better Call Saul 广告的恶搞风格；[配图与资料来源](THIRD_PARTY_NOTICES.md)。

## 用量看得见，来源查得到

<p align="center"><img src="assets/float-demo.png" width="460" alt="原生 Codex Float 窗口，使用合成 Demo 数据" /></p>

*原生 Float 演示，任务名称与用量均为合成 Demo 数据，图中记录用于展示界面。*

蓝色看推理强度，绿／黄／红看难度，橙色看 token，绿色看 Astra 等价节省。玻璃悬浮窗支持调整尺寸、透明度和字号，可切换本轮与累计用量；展开主任务能看到子代理关系和路由依据。

官方 Codex 数据库以只读方式打开。Float 只写自己的数字 SQLite 账本，包含线程／请求标识和读取位置，用于跨重启、日志轮换的持久去重；账本不保存对话正文或推理正文。未知用量、未知价格保持未知，每个已计量请求冻结首次采用的价格快照。

## 异议：路由决定有据可查

难度是本地规则给出的 1.0–10.0 刻度，步长 0.1，尚未校准。评分、计量、比价都不新增模型调用。两道可选偏好问题分别询问优先目标——质量、均衡、费用或速度——以及主要工作类型。

模型目录按 TTL 从 models.dev 更新候选元数据，失败时保留 last-good 快照。新型号经过价格、能力、执行端点与宿主证据准入后才参与选模；目录发现本身不授予执行权限。当前执行端是 Codex，其他供应商仅进入目录。

任务启用路由后，通过原生子代理派发可独立验收的工作块。主任务不能因技能调用而在生成途中换模。派发与验收分开记录：`accepted`、`rejected`、`unknown` 有明确证据语义，benchmark 反馈与生产学习隔离。

## 第一轮 pilot：规模小，经得起逐项盘问

六个公开合成任务 × 三种策略 × 每种一次：确定性验收 **18/18 通过**。

| 策略 | 通过数 | 实际 tokens | API Standard 参考费用（USD） |
| --- | ---: | ---: | ---: |
| 固定 Astra | 6/6 | 102,162 | $1.237460 |
| 固定 6.1 Sol | 6/6 | 105,285 | $0.1921092 |
| 本地 router | 6/6 | 101,392 | $0.1621807 |

![Pilot token 与参考费用对比](assets/benchmark.svg)

相对固定 Astra，router 在这轮 pilot 中的参考费用低 **86.89%**，实际 token 少 **0.75%**。固定 6.1 Sol 有 24,832 个缓存输入 token，其余两组为零；解读费用对比时需一并看缓存差异。

三组都通过了这六题。这个 one-shot selector 实验支持该任务集内的结论；总体质量等价、多代理协作、重试及失败升级收益需要另设实验。USD 是实测用量乘冻结的 API Standard 参考价，不是 Codex 订阅账单。

[公开结果与复现文件](router/benchmarks/results/pilot-public/) · [评测协议](router/benchmarks/README.md)

## 本地试用

Python 部分只依赖标准库，router 执行复用已有 Codex 登录。Float 需要 macOS 14+、`/usr/bin/python3` 和 Xcode Command Line Tools。

在克隆的仓库根目录注册本地市场并安装 router：

```sh
codex plugin marketplace add .
codex plugin add adaptive-router@agent-smith
```

以下命令检查 router，不调用模型：

```sh
python3 router/scripts/router.py --help
python3 router/scripts/model_registry.py show
```

在新的 Codex 任务中输入：

```text
用 $adaptive-route 完成这个任务：……
```

模式、偏好与显式模型 pin 见 [router 说明](router/README.md)。

构建并打开 Float：

```sh
cd float
sh scripts/build.sh
open "build/Codex Float.app"
```

构建产物采用 ad-hoc 签名，当前通过源码分发，没有预构建 DMG。

复现截图的合成演示时，在 `float/` 执行 `sh scripts/build.sh --demo`，打开沿用旧名的 `build/Token Inspector Demo.app`。现有独立应用身份保留演示偏好；演示采用合成 fixture，不启用真实任务读取器。

在仓库根目录运行回归或离线评测演示：

```sh
python3 -m unittest discover -s router/tests -q
python3 -m unittest discover -s float/tests -q
python3 router/benchmarks/benchmark.py demo --run-dir /tmp/agent-smith-demo
python3 router/benchmarks/benchmark.py report --run-dir /tmp/agent-smith-demo --output /tmp/agent-smith-demo-report
```

输出使用新目录。`demo` 使用合成答案与用量，`replay` 重验收已有成果，两者都不调用模型。导入基线包含 **130 项 router 测试、50 项 Float 测试**，后续覆盖以实际命令结果为准。

## 我想讨论的设计取舍

未知值保留未知，历史费用采用当时价格，任务成功用实际验收判断。难度规则能逐条看，pilot 能逐题查，方便同行指出哪一步值得改。

规则评分何时该让位于真实工作负载证据？同类任务通过多少次，才足以调整个人选模偏好？缓存差异和部分用量覆盖，应怎样出现在费用比较里？这是我希望这个项目能具体讨论的问题。

[参与贡献](CONTRIBUTING.md) · [MIT 许可证](LICENSE)
